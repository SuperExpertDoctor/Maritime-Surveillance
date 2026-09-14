"""Legacy schedule wrappers over the role-isolated LongCat gateway."""
from __future__ import annotations

from src.mission.llm_gateway import LLMConfigurationError, LLMGateway, ModelResult, parse_object
from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import AppConfig
from src.schedule.info_value_table import InfoValueTable
from src.schedule.output_validator import validate
from src.schedule.prompt_builder import PromptBuilder
from src.schedule.state_manager import StateManager


class LLMClient:
    def __init__(
        self, config: AppConfig,
        llm_params_path: str = "configs/llm_params.yaml", *, transport=None,
    ):
        self.config = config
        self.prompt_builder = PromptBuilder()
        self._reviewer_memory = ""
        self.last_interaction: dict | None = None
        self.last_reviewer_interaction: dict | None = None
        self.gateway = LLMGateway(llm_params_path, transport=transport)

    def resolve_binding(self, role: str) -> dict:
        return self.gateway.resolve_binding(role)

    def assert_ready(self) -> None:
        """Fail early instead of silently replacing an unavailable model."""
        self.gateway.assert_ready()

    def probe(self, timeout_seconds: float = 20.0) -> str:
        """Verify the configured LongCat route with a bounded live request."""
        self.assert_ready()
        response = self._call_api(
            "You are a connectivity probe. Reply with OK only.",
            "Reply with OK.",
            role="decision_maker",
            json_mode=False,
            max_tokens=8,
            timeout_seconds=timeout_seconds,
        ).strip()
        if not response:
            raise LLMConfigurationError("LongCat probe returned an empty response")
        return response

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
        )
        self.last_reviewer_interaction = self._interaction(result, system_prompt, user_prompt)
        if not result.success:
            return ""
        return result.payload["text"].strip()[:200]

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

    def _call_api(
        self,
        system_prompt: str,
        user_prompt: str,
        role: str = "decision_maker",
        json_mode: bool = True,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Single transport call for the legacy bounded connectivity probe."""
        binding = self.resolve_binding(role)
        return self.gateway.transport.complete(
            role=role, model=binding["model"],
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=binding["temperature"],
            max_tokens=max_tokens if max_tokens is not None else binding["max_tokens"],
            thinking=binding["thinking"], json_mode=json_mode,
            api_base=binding["api_base"], api_key_env=binding["api_key_env"],
            supports_json_mode=binding["supports_json_mode"],
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        try:
            return parse_object(raw)
        except (TypeError, ValueError):
            return None


__all__ = ["LLMClient", "LLMConfigurationError"]
