"""Legacy schedule wrappers over the role-isolated LongCat gateway."""
from __future__ import annotations

import re

from src.mission.llm_gateway import LLMConfigurationError, LLMGateway, ModelResult, parse_object
from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import AppConfig
from src.schedule.info_value_table import InfoValueTable
from src.schedule.output_validator import validate
from src.schedule.prompt_builder import PromptBuilder
from src.schedule.state_manager import StateManager


REVIEWER_MEMORY_LIMIT = 200

_CONTROL_CHARACTER = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# Reviewer memory is quoted verbatim into the next decision prompt, so reject
# text that could impersonate prompt structure instead of staying a summary.
_IMPERSONATION_MARKERS = (
    "```", "<|", "<system", "</system", "[system]", "<assistant",
    "system:", "assistant:", "developer:",
    "ignore previous", "ignore all previous", "ignore the above",
    "disregard previous", "忽略以上", "忽略上述", "忽略之前", "忽略前述",
)


def reviewer_memory_errors(text) -> tuple[str, ...]:
    """Validate reviewer free text before it can reach the decision prompt."""
    if not isinstance(text, str) or not text.strip():
        return ("reviewer memory is empty",)
    stripped = text.strip()
    errors = []
    if len(stripped) > REVIEWER_MEMORY_LIMIT:
        errors.append(
            f"reviewer memory is {len(stripped)} characters: "
            f"must be at most {REVIEWER_MEMORY_LIMIT}"
        )
    if _CONTROL_CHARACTER.search(stripped):
        errors.append("reviewer memory must be a single line of plain text")
    lowered = stripped.lower()
    if any(marker in lowered for marker in _IMPERSONATION_MARKERS):
        errors.append("reviewer memory must not contain prompt-structure text")
    return tuple(errors)


class LLMClient:
    def __init__(
        self, config: AppConfig,
        llm_params_path: str = "configs/llm_params.yaml", *, transport=None,
        gateway=None,
    ):
        self.config = config
        self.prompt_builder = PromptBuilder()
        self._reviewer_memory = ""
        self.last_interaction: dict | None = None
        self.last_reviewer_interaction: dict | None = None
        self.gateway = gateway if gateway is not None else LLMGateway(
            llm_params_path, transport=transport
        )

    def resolve_binding(self, role: str) -> dict:
        return self.gateway.resolve_binding(role)

    def assert_ready(self) -> None:
        """Fail early instead of silently replacing an unavailable model."""
        self.gateway.assert_ready()

    def probe(self, timeout_seconds: float = 20.0) -> str:
        """Verify the configured LongCat route with a bounded live request."""
        self.assert_ready()
        result = self.gateway.request_probe(
            role="decision_maker",
            snapshot_id="connectivity-probe",
            system_prompt="You are a connectivity probe. Reply with OK only.",
            user_prompt="Reply with OK.",
            timeout_seconds=timeout_seconds,
        )
        if not result.success:
            if result.failure_category == "validation":
                raise LLMConfigurationError("LongCat probe returned an empty response")
            detail = result.errors[0] if result.errors else result.failure_category
            raise LLMConfigurationError(f"LongCat probe failed: {detail}")
        return result.payload["text"].strip()

    def set_reviewer_memory(self, memory: str) -> None:
        self._reviewer_memory = memory

    @staticmethod
    def _schema_errors(payload: dict) -> tuple[str, ...]:
        """Check JSON types before the existing geometric plan validator."""
        errors = []
        extra = set(payload) - {"search_regions", "notes"}
        if extra:
            errors.append(f"unexpected top-level fields: {sorted(extra)}")
        if "notes" in payload and not isinstance(payload["notes"], str):
            errors.append("notes must be a string")
        regions = payload.get("search_regions")
        if not isinstance(regions, list):
            errors.append("search_regions must be an array")
            return tuple(errors)
        for index, region in enumerate(regions):
            if not isinstance(region, dict):
                errors.append(f"Region {index}: expected object")
                continue
            extra = set(region) - {"id", "bbox", "priority", "reason"}
            if extra:
                errors.append(f"Region {index}: unexpected fields: {sorted(extra)}")
            if not isinstance(region.get("id"), str) or not region["id"].strip():
                errors.append(f"Region {index}: id must be a non-empty string")
            if "priority" in region and region["priority"] not in ("high", "medium", "low"):
                errors.append(f"Region {index}: priority must be high, medium or low")
            if "reason" in region and not isinstance(region["reason"], str):
                errors.append(f"Region {index}: reason must be a string")
            bbox = region.get("bbox")
            if (not isinstance(bbox, list) or len(bbox) != 4
                    or any(type(value) is not int for value in bbox)):
                errors.append(f"Region {index}: bbox must contain four integers")
        return tuple(errors)

    def decide(
        self,
        sm: StateManager,
        ivt: InfoValueTable,
        candidate_result: CandidateResult,
        required_search_regions: int = 0,
    ) -> dict:
        system_prompt, user_prompt = self.prompt_builder.build(
            sm, ivt, candidate_result, self._reviewer_memory, required_search_regions,
        )
        reserved_regions = sm.get_active_search_regions()
        tracks = sm.get_track_regions()
        remaining_slots = max(0, 10 - len(tracks) - len(reserved_regions))

        def validate_plan(payload: dict) -> tuple[str, ...]:
            errors = self._schema_errors(payload)
            if errors:
                return errors
            result = validate(
                payload,
                self.config,
                [*tracks, *reserved_regions],
                sm.get_previous_search_regions(),
                sm.obstacle_mask,
                base_positions=sm.get_base_positions(),
                # Retained work can consume every legal search/track slot.
                allow_empty=(not candidate_result.candidate_regions or remaining_slots == 0),
                allowed_search_bboxes=[
                    candidate["bbox"] for candidate in candidate_result.candidate_regions
                ],
                required_search_regions=min(
                    max(0, int(required_search_regions)), remaining_slots,
                    len(candidate_result.candidate_regions),
                ),
            )
            return tuple(result.errors)

        result = self.gateway.request_json(
            role="decision_maker", snapshot_id=f"decision-{sm.current_time}",
            system_prompt=system_prompt, user_payload={"prompt": user_prompt},
            validate=validate_plan,
        )
        self.last_interaction = self._interaction(result, system_prompt, user_prompt)
        if result.success:
            return result.payload
        # Preserve the legacy explicit failure marker; the gateway returns no plan.
        return {"search_regions": [], "notes": "LLM failed after max retries"}

    def review(
        self, system_prompt: str, user_prompt: str, *, snapshot_id: str = "legacy-review",
    ) -> str:
        result = self.gateway.request_text(
            role="reviewer", snapshot_id=snapshot_id,
            system_prompt=system_prompt, user_payload={"prompt": user_prompt},
            validate_text=reviewer_memory_errors,
        )
        self.last_reviewer_interaction = self._interaction(result, system_prompt, user_prompt)
        if not result.success:
            return ""
        return result.payload["text"].strip()

    def _interaction(self, result: ModelResult, system_prompt: str, user_prompt: str) -> dict:
        # Synchronous wrappers: the latest call is the request just completed.
        interaction = self.gateway.redact_log(self.gateway.call_log[-1])
        for attempt in interaction["attempts"]:
            attempt["response"] = attempt["raw_output"] or ""
        interaction.update(self.gateway.redact_log({
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": interaction["attempts"][-1]["response"] if result.success else "",
            "validation": {"is_valid": result.success, "errors": list(result.errors)},
        }))
        return interaction

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        try:
            return parse_object(raw)
        except (TypeError, ValueError):
            return None


__all__ = ["LLMClient", "LLMConfigurationError", "reviewer_memory_errors"]
