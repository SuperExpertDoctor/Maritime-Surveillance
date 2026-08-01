"""Real LongCat decision and reviewer client with validation retries."""
from __future__ import annotations

import json
import logging
import os
import re

import yaml

from src.schedule.candidate_extractor import CandidateResult
from src.schedule.config_loader import AppConfig
from src.schedule.env_loader import EnvLoader
from src.schedule.info_value_table import InfoValueTable
from src.schedule.output_validator import validate
from src.schedule.prompt_builder import PromptBuilder
from src.schedule.state_manager import StateManager


logger = logging.getLogger(__name__)


class LLMConfigurationError(RuntimeError):
    """The required real LongCat provider is not ready."""


class LLMClient:
    def __init__(self, config: AppConfig, llm_params_path: str = "configs/llm_params.yaml"):
        self.config = config
        self.prompt_builder = PromptBuilder()
        self._reviewer_memory = ""
        self.last_interaction: dict | None = None
        self.last_reviewer_interaction: dict | None = None
        EnvLoader.load_dotenv()

        with open(llm_params_path, "r", encoding="utf-8") as stream:
            params = yaml.safe_load(stream)
        self._providers = {provider["name"]: provider for provider in params.get("providers", [])}
        self._models = {model["id"]: model for model in params.get("models", [])}
        self._bindings = params.get("bindings", {})
        self._cycles = params.get("cycles", {})
        self._validate_required_bindings()

    def _binding(self, role: str) -> dict:
        binding = self._bindings.get(role, {})
        model_id = binding.get("model", "")
        model_info = self._models.get(model_id, {})
        provider_name = model_info.get("provider", "")
        provider_info = self._providers.get(provider_name, {})
        api_key_env = provider_info.get("api_key_env", "")
        return {
            "role": role,
            "model": model_id,
            "provider": provider_name,
            "temperature": binding.get("temperature", 0.3),
            "max_tokens": binding.get("max_tokens", 4096),
            "thinking": binding.get("thinking"),
            "api_base": provider_info.get("api_base", ""),
            "api_key_env": api_key_env,
            "api_key": os.environ.get(api_key_env, ""),
            "supports_json_mode": bool(
                provider_info.get("supports_json_mode", False)
            ),
        }

    def _validate_required_bindings(self) -> None:
        for role in ("decision_maker", "reviewer"):
            binding = self._binding(role)
            if binding["provider"] != "longcat":
                raise LLMConfigurationError(f"{role} must use the LongCat provider")
            if binding["model"] != "LongCat-2.0":
                raise LLMConfigurationError(f"{role} must use LongCat-2.0")
            if not binding["api_base"]:
                raise LLMConfigurationError(f"{role} has no API base URL")

    def resolve_binding(self, role: str) -> dict:
        return self._binding(role)

    def assert_ready(self) -> None:
        """Fail early instead of silently replacing an unavailable model."""
        for role in ("decision_maker", "reviewer"):
            binding = self._binding(role)
            if not binding["api_key"]:
                raise LLMConfigurationError(
                    f"{binding['api_key_env']} is required for the {role} binding"
                )

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

    def decide(
        self,
        sm: StateManager,
        ivt: InfoValueTable,
        candidate_result: CandidateResult,
        required_search_regions: int = 0,
    ) -> dict:
        system_prompt, user_prompt = self.prompt_builder.build(
            sm,
            ivt,
            candidate_result,
            self._reviewer_memory,
            required_search_regions,
        )
        interaction = {
            "role": "decision_maker",
            "model": self._binding("decision_maker")["model"],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "attempts": [],
            "response": "",
            "validation": {"is_valid": False, "errors": []},
            "success": False,
        }

        for attempt in range(self.config.llm.max_retries + 1):
            try:
                raw = self._call_api(
                    system_prompt,
                    user_prompt,
                    role="decision_maker",
                    json_mode=True,
                )
            except Exception as exc:
                logger.error("LongCat decision call failed on attempt %s: %s", attempt + 1, exc)
                interaction["attempts"].append({
                    "attempt": attempt + 1,
                    "response": "",
                    "errors": [str(exc)],
                })
                if getattr(exc, "status_code", None) in {400, 401, 402, 403}:
                    break
                continue

            parsed = self._parse_json(raw)
            if parsed is None:
                errors = ["response is not valid JSON"]
                interaction["attempts"].append({
                    "attempt": attempt + 1,
                    "response": raw,
                    "errors": errors,
                })
                user_prompt += (
                    "\n\n[上轮输出不是有效JSON，请严格按照JSON格式输出]"
                    f"\n原始输出: {raw[:200]}"
                )
                continue

            reserved_regions = sm.get_active_search_regions()
            remaining_slots = max(
                0,
                10 - len(sm.get_track_regions()) - len(reserved_regions),
            )
            result = validate(
                parsed,
                self.config,
                [*sm.get_track_regions(), *reserved_regions],
                sm.get_previous_search_regions(),
                sm.obstacle_mask,
                base_positions=sm.get_base_positions(),
                # An empty plan is a valid model decision when retained work
                # already consumes every search/track slot. Retrying it only
                # creates artificial failures and cannot add legal regions.
                allow_empty=(
                    not candidate_result.candidate_regions
                    or remaining_slots == 0
                ),
                allowed_search_bboxes=[
                    candidate["bbox"]
                    for candidate in candidate_result.candidate_regions
                ],
                required_search_regions=min(
                    max(0, int(required_search_regions)),
                    remaining_slots,
                    len(candidate_result.candidate_regions),
                ),
            )
            errors = list(result.errors)
            interaction["attempts"].append({
                "attempt": attempt + 1,
                "response": raw,
                "errors": errors,
            })
            if result.is_valid:
                interaction["response"] = raw
                interaction["validation"] = {"is_valid": True, "errors": []}
                interaction["success"] = True
                self.last_interaction = interaction
                return parsed

            user_prompt += "\n\n[上轮输出校验失败，请修正以下错误]\n" + "\n".join(errors)

        errors = interaction["attempts"][-1]["errors"] if interaction["attempts"] else ["no API attempt"]
        interaction["validation"] = {"is_valid": False, "errors": errors}
        self.last_interaction = interaction
        # Explicit fail-closed output; it never claims to be a model response.
        return {"search_regions": [], "notes": "LLM failed after max retries"}

    def review(self, system_prompt: str, user_prompt: str) -> str:
        raw = self._call_api(system_prompt, user_prompt, role="reviewer", json_mode=False).strip()
        memory = raw[:200]
        self.last_reviewer_interaction = {
            "role": "reviewer",
            "model": self._binding("reviewer")["model"],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": raw,
            "success": bool(memory),
        }
        return memory

    def _call_api(
        self,
        system_prompt: str,
        user_prompt: str,
        role: str = "decision_maker",
        json_mode: bool = True,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        """Call LongCat through its OpenAI-compatible ChatCompletions API."""
        from openai import OpenAI

        binding = self._binding(role)
        if not binding["api_key"]:
            raise LLMConfigurationError(
                f"{binding['api_key_env']} is required; no synthetic response is permitted"
            )
        client = OpenAI(
            api_key=binding["api_key"],
            base_url=binding["api_base"],
            timeout=timeout_seconds or 120.0,
            max_retries=0,
        )
        kwargs = {
            "model": binding["model"],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": binding["temperature"],
            "max_tokens": max_tokens or binding["max_tokens"],
        }
        if json_mode and binding["supports_json_mode"]:
            kwargs["response_format"] = {"type": "json_object"}
        if binding["thinking"] in {"enabled", "disabled"}:
            kwargs["extra_body"] = {
                "thinking": {"type": binding["thinking"]}
            }
        response = client.chat.completions.create(**kwargs)
        return response.choices[0].message.content or ""

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        raw = raw.strip()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None


__all__ = ["LLMClient", "LLMConfigurationError"]
