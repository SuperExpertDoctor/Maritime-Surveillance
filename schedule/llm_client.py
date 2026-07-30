import json
import os
import re

import yaml

from schedule.config_loader import AppConfig
from schedule.state_manager import StateManager
from schedule.info_value_table import InfoValueTable
from schedule.candidate_extractor import CandidateResult
from schedule.prompt_builder import PromptBuilder
from schedule.output_validator import validate


class LLMClient:
    def __init__(self, config: AppConfig, llm_params_path: str = "configs/llm_params.yaml"):
        self.config = config
        self.prompt_builder = PromptBuilder()
        self._reviewer_memory: str = ""

        # --- Load llm_params.yaml ---
        with open(llm_params_path, "r", encoding="utf-8") as f:
            params = yaml.safe_load(f)

        # Index providers and models by name/id
        self._providers: dict[str, dict] = {
            p["name"]: p for p in params.get("providers", [])
        }
        self._models: dict[str, dict] = {
            m["id"]: m for m in params.get("models", [])
        }
        self._bindings: dict[str, dict] = params.get("bindings", {})
        self._cycles: dict = params.get("cycles", {})

        # --- Resolve the decision_maker binding ---
        self._resolve_binding("decision_maker")

    def _resolve_binding(self, role: str) -> None:
        """Resolve a binding name to concrete model + provider + API key."""
        binding = self._bindings.get(role, {})
        model_id = binding.get("model", "deepseek-v4-pro")

        # Look up model definition
        model_info = self._models.get(model_id, {})
        provider_name = model_info.get("provider", "deepseek")

        # Look up provider definition
        provider_info = self._providers.get(provider_name, {})

        # Store resolved values
        self._model = model_id
        self._temperature = binding.get("temperature", 0.3)
        self._max_tokens = binding.get("max_tokens", 4096)
        self._api_base = provider_info.get("api_base", "")
        api_key_env = provider_info.get("api_key_env", "DEEPSEEK_API_KEY")
        self._api_key = os.environ.get(api_key_env, "")

    def resolve_binding(self, role: str) -> dict:
        """Public resolver: return a dict of model, api_base, api_key, etc. for a role."""
        binding = self._bindings.get(role, {})
        model_id = binding.get("model", "")
        model_info = self._models.get(model_id, {})
        provider_name = model_info.get("provider", "")
        provider_info = self._providers.get(provider_name, {})
        api_key_env = provider_info.get("api_key_env", "")
        return {
            "role": role,
            "model": model_id,
            "temperature": binding.get("temperature", 0.3),
            "max_tokens": binding.get("max_tokens", 4096),
            "api_base": provider_info.get("api_base", ""),
            "api_key": os.environ.get(api_key_env, ""),
        }

    def set_reviewer_memory(self, memory: str) -> None:
        self._reviewer_memory = memory

    def decide(self, sm: StateManager, ivt: InfoValueTable,
               candidate_result: CandidateResult) -> dict:
        """调用 LLM 决策管线，返回解析后的 JSON dict。"""
        system_prompt, user_prompt = self.prompt_builder.build(
            sm, ivt, candidate_result, self._reviewer_memory
        )

        for attempt in range(self.config.llm.max_retries + 1):
            raw = self._call_api(system_prompt, user_prompt)
            parsed = self._parse_json(raw)

            if parsed is None:
                user_prompt += f"\n\n[上轮输出不是有效JSON，请严格按照JSON格式输出]\n原始输出: {raw[:200]}"
                continue

            # 校验
            result = validate(parsed, self.config, sm.get_track_regions(),
                            sm.get_previous_search_regions())
            if result.is_valid:
                return parsed

            # 回注错误
            error_msg = "\n".join(result.errors)
            user_prompt += f"\n\n[上轮输出校验失败，请修正以下错误]\n{error_msg}"

        # 兜底：返回空方案
        return {"search_regions": [], "notes": "LLM failed after max retries"}

    def _call_api(self, system_prompt: str, user_prompt: str) -> str:
        """OpenAI-compatible API 调用。"""
        try:
            from openai import OpenAI
        except ImportError:
            # 模拟返回（离线测试用）
            return self._mock_response()

        client = OpenAI(api_key=self._api_key, base_url=self._api_base)
        response = client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )
        return response.choices[0].message.content or ""

    def _mock_response(self) -> str:
        """离线测试用的模拟响应。"""
        return json.dumps({
            "search_regions": [
                {"id": "S1", "bbox": [0, 0, 5, 6], "priority": "high", "reason": "mock"},
                {"id": "S2", "bbox": [10, 10, 15, 14], "priority": "medium", "reason": "mock"},
            ],
            "notes": "mock response"
        })

    def _parse_json(self, raw: str) -> dict | None:
        """从 LLM 响应中提取 JSON。"""
        raw = raw.strip()
        # 尝试直接解析
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
        # 尝试从 ```json ``` 块中提取
        match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', raw)
        if match:
            try:
                return json.loads(match.group(1))
            except json.JSONDecodeError:
                pass
        return None
