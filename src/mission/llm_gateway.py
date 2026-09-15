"""Role-isolated model access with strict response validation."""
from __future__ import annotations

import json
import math
import os
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable
from uuid import uuid4

from src.mission.config import load_strict_yaml
from src.schedule.env_loader import EnvLoader


_JSON_FENCE = re.compile(
    r"```(?:json)?[ \t]*\r?\n(?P<body>[\s\S]*?)\r?\n?```",
    re.IGNORECASE,
)
_ROLE_TOKEN_LIMITS = {
    "decision_maker": 4096,
    "contact_assessor": 2048,
    "red_commander": 4096,
    "reviewer": 2048,
}


class LLMConfigurationError(RuntimeError):
    """The required LongCat model route is not configured for use."""


@dataclass(frozen=True)
class ModelResult:
    call_id: str
    success: bool
    payload: dict | None
    errors: tuple[str, ...]
    failure_category: str | None


class OpenAICompatibleTransport:
    """Existing LongCat route through the OpenAI-compatible chat API."""

    def complete(
        self,
        *,
        role: str,
        model: str,
        messages: list[dict[str, str]],
        temperature: float,
        max_tokens: int,
        thinking: str | None,
        json_mode: bool,
        api_base: str,
        api_key_env: str,
        supports_json_mode: bool,
        timeout_seconds: float | None,
    ) -> str:
        from openai import OpenAI

        api_key = os.environ.get(api_key_env, "")
        if not api_key:
            raise LLMConfigurationError(
                f"{api_key_env} is required; no synthetic response is permitted"
            )
        client = OpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=120.0 if timeout_seconds is None else timeout_seconds,
            max_retries=0,
        )
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode and supports_json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if thinking in {"enabled", "disabled"}:
            kwargs["extra_body"] = {"thinking": {"type": thinking}}
        try:
            response = client.chat.completions.create(**kwargs)
            return response.choices[0].message.content or ""
        finally:
            client.close()


def reject_constant(value: str):
    raise ValueError(f"non-finite JSON constant: {value}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_object(raw: str) -> dict:
    candidate = raw.strip()
    fence = _JSON_FENCE.fullmatch(candidate)
    if fence is not None:
        candidate = fence.group("body")
    result = json.loads(
        candidate,
        parse_constant=reject_constant,
        object_pairs_hook=unique_object,
    )
    if not isinstance(result, dict):
        raise ValueError("expected JSON object")
    return result


class LLMGateway:
    """Issue isolated model requests through an injected transport."""

    def __init__(
        self,
        llm_params_path: str = "configs/llm_params.yaml",
        transport=None,
    ):
        EnvLoader.load_dotenv()
        params = load_strict_yaml(llm_params_path)
        self._providers = {
            provider["name"]: provider for provider in params.get("providers", [])
        }
        self._models = {model["id"]: model for model in params.get("models", [])}
        self._bindings = params.get("bindings", {})
        configured_retries = params.get("cycles", {}).get("max_retries", 2)
        if (
            isinstance(configured_retries, bool)
            or not isinstance(configured_retries, int)
            or configured_retries < 0
        ):
            raise LLMConfigurationError("cycles.max_retries must be a non-negative integer")
        self._max_correction_retries = min(configured_retries, 2)
        self._validate_required_bindings()
        self.transport = transport if transport is not None else OpenAICompatibleTransport()
        self.call_log: list[dict] = []
        self._context = {
            "episode_id": "",
            "sim_time_min": 0.0,
            "memory_version": "baseline",
        }

    def set_context(
        self,
        episode_id: str,
        memory_version: str,
        sim_time_min: float,
    ) -> None:
        """Attach non-sensitive episode metadata to every subsequent call."""
        if not isinstance(episode_id, str) or not episode_id:
            raise ValueError("episode_id must be a non-empty string")
        if not isinstance(memory_version, str) or not memory_version:
            raise ValueError("memory_version must be a non-empty string")
        if isinstance(sim_time_min, bool) or not isinstance(sim_time_min, (int, float)):
            raise TypeError("sim_time_min must be numeric")
        if not math.isfinite(float(sim_time_min)) or sim_time_min < 0:
            raise ValueError("sim_time_min must be finite and non-negative")
        self._context = {
            "episode_id": episode_id,
            "sim_time_min": float(sim_time_min),
            "memory_version": memory_version,
        }

    def _binding(self, role: str) -> dict:
        configured = self._bindings.get(role, {})
        model_id = configured.get("model", "")
        model = self._models.get(model_id, {})
        provider_name = model.get("provider", "")
        provider = self._providers.get(provider_name, {})
        return {
            "role": role,
            "model": model_id,
            "provider": provider_name,
            "temperature": configured.get("temperature", 0.3),
            "max_tokens": configured.get("max_tokens", 4096),
            "thinking": configured.get("thinking"),
            "api_base": provider.get("api_base", ""),
            "api_key_env": provider.get("api_key_env", ""),
            "supports_json_mode": bool(provider.get("supports_json_mode", False)),
        }

    def _validate_required_bindings(self) -> None:
        if not isinstance(self._bindings, dict):
            raise LLMConfigurationError("bindings must be a mapping")
        for role, expected_tokens in _ROLE_TOKEN_LIMITS.items():
            if role not in self._bindings:
                raise LLMConfigurationError(f"missing required {role} binding")
            if not isinstance(self._bindings[role], dict):
                raise LLMConfigurationError(f"{role} binding must be a mapping")
            binding = self._binding(role)
            if binding["provider"] != "longcat":
                raise LLMConfigurationError(
                    f"{role} must use the LongCat provider"
                )
            if binding["model"] != "LongCat-2.0":
                raise LLMConfigurationError(f"{role} must use LongCat-2.0")
            if binding["max_tokens"] != expected_tokens:
                raise LLMConfigurationError(
                    f"{role} max_tokens must be {expected_tokens}"
                )
            if not binding["api_base"]:
                raise LLMConfigurationError(f"{role} has no API base URL")
            if not binding["api_key_env"]:
                raise LLMConfigurationError(f"{role} has no API key environment name")

    def resolve_binding(self, role: str) -> dict:
        if role not in _ROLE_TOKEN_LIMITS:
            raise LLMConfigurationError(f"unsupported model role: {role}")
        return dict(self._binding(role))

    def assert_ready(self) -> None:
        for role in _ROLE_TOKEN_LIMITS:
            binding = self._binding(role)
            if not os.environ.get(binding["api_key_env"], ""):
                raise LLMConfigurationError(
                    f"{binding['api_key_env']} is required for the {role} binding"
                )

    def _redact(self, message: str) -> str:
        redacted = message
        for provider in self._providers.values():
            secret = os.environ.get(provider.get("api_key_env", ""), "")
            if secret:
                # Messages contain serialized JSON; redact its escaped forms too.
                variants = {secret, json.dumps(secret)[1:-1],
                            json.dumps(secret, ensure_ascii=False)[1:-1]}
                for variant in sorted(variants, key=len, reverse=True):
                    redacted = redacted.replace(variant, "[REDACTED]")
        return redacted

    def redact_log(self, value):
        """Copy log data, preserving raw formatting except configured secrets."""
        if isinstance(value, str):
            return self._redact(value)
        if isinstance(value, dict):
            return {self.redact_log(key): self.redact_log(item)
                    for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact_log(item) for item in value]
        return value

    @staticmethod
    def _is_timeout(exc: Exception) -> bool:
        from openai import APITimeoutError

        return isinstance(exc, (TimeoutError, APITimeoutError))

    @staticmethod
    def _status_code(exc: BaseException) -> int | None:
        status_code = getattr(exc, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
        return status_code if isinstance(status_code, int) else None

    @staticmethod
    def _correction_message(
        errors: tuple[str, ...],
        instruction: str = "Return exactly one corrected JSON object.",
    ) -> dict[str, str]:
        return {
            "role": "user",
            "content": json.dumps(
                {
                    "type": "validation_correction",
                    "errors": list(errors),
                    "instruction": instruction,
                },
                ensure_ascii=False,
            ),
        }

    def request_json(
        self,
        *,
        role: str,
        snapshot_id: str,
        system_prompt: str,
        user_payload: dict,
        validate: Callable[[dict], tuple[str, ...]],
        deadline_monotonic: float | None = None,
        transport_deadline_monotonic: float | None = None,
    ) -> ModelResult:
        return self._request(
            role=role, snapshot_id=snapshot_id, system_prompt=system_prompt,
            user_payload=user_payload, validate=validate,
            deadline_monotonic=deadline_monotonic,
            transport_deadline_monotonic=transport_deadline_monotonic,
        )

    def request_text(
        self,
        *,
        role: str,
        snapshot_id: str,
        system_prompt: str,
        user_payload: dict,
        deadline_monotonic: float | None = None,
        transport_deadline_monotonic: float | None = None,
    ) -> ModelResult:
        return self._request(
            role=role, snapshot_id=snapshot_id, system_prompt=system_prompt,
            user_payload=user_payload, validate=None,
            deadline_monotonic=deadline_monotonic,
            transport_deadline_monotonic=transport_deadline_monotonic,
        )

    def request_probe(
        self,
        *,
        role: str,
        snapshot_id: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> ModelResult:
        """Issue one audited short-text request for connectivity checks."""
        return self._request(
            role=role, snapshot_id=snapshot_id, system_prompt=system_prompt,
            user_payload=None, user_content=user_prompt, validate=None,
            attempt_limit=1, max_tokens=8, timeout_seconds=timeout_seconds,
        )

    def _request(
        self,
        *,
        role: str,
        snapshot_id: str,
        system_prompt: str,
        user_payload: dict | None,
        validate: Callable[[dict], tuple[str, ...]] | None,
        user_content: str | None = None,
        attempt_limit: int | None = None,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        deadline_monotonic: float | None = None,
        transport_deadline_monotonic: float | None = None,
    ) -> ModelResult:
        if role not in _ROLE_TOKEN_LIMITS:
            raise LLMConfigurationError(f"unsupported model role: {role}")
        self._validate_deadline(deadline_monotonic, "deadline_monotonic")
        self._validate_deadline(
            transport_deadline_monotonic, "transport_deadline_monotonic"
        )
        if (
            deadline_monotonic is not None
            and transport_deadline_monotonic is not None
            and transport_deadline_monotonic > deadline_monotonic
        ):
            raise ValueError("transport deadline must not exceed absolute deadline")
        call_id = uuid4().hex
        if user_content is None:
            user_content = json.dumps(
                user_payload,
                ensure_ascii=False,
                allow_nan=False,
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        binding = self._binding(role)
        call = {
            "call_id": call_id,
            "role": role,
            "episode_id": self._redact(self._context["episode_id"]),
            "snapshot_id": self._redact(snapshot_id),
            "sim_time_min": self._context["sim_time_min"],
            "memory_version": self._redact(self._context["memory_version"]),
            "model": self._redact(binding["model"]),
            "attempts": [],
            "raw_attempts": [],
            "validation_errors": [],
            "success": False,
            "failure_category": None,
        }
        self.call_log.append(call)

        last_errors: tuple[str, ...] = ()
        failure_category = "transport"
        total_attempts = (
            attempt_limit
            if attempt_limit is not None
            else self._max_correction_retries + 1
        )
        for attempt_number in range(1, total_attempts + 1):
            attempt = {
                "attempt": attempt_number,
                "messages": self.redact_log(messages),
                "raw_output": None,
                "errors": [],
            }
            call["attempts"].append(attempt)
            if self._deadline_expired(deadline_monotonic):
                last_errors = ("decision_deadline_exceeded",)
                attempt["errors"] = list(last_errors)
                failure_category = "timeout"
                break
            transport_timeout = self._transport_timeout(
                timeout_seconds=timeout_seconds,
                deadline_monotonic=deadline_monotonic,
                transport_deadline_monotonic=transport_deadline_monotonic,
            )
            if transport_timeout == 0.0:
                last_errors = ("decision_deadline_exceeded",)
                attempt["errors"] = list(last_errors)
                failure_category = "timeout"
                break
            try:
                raw = self.transport.complete(
                    role=role,
                    model=binding["model"],
                    messages=deepcopy(messages),
                    temperature=binding["temperature"],
                    max_tokens=(
                        max_tokens if max_tokens is not None else binding["max_tokens"]
                    ),
                    thinking=binding["thinking"],
                    json_mode=validate is not None,
                    api_base=binding["api_base"],
                    api_key_env=binding["api_key_env"],
                    supports_json_mode=binding["supports_json_mode"],
                    timeout_seconds=transport_timeout,
                )
            except AssertionError:
                raise
            except Exception as exc:
                error = self._redact(str(exc)) or type(exc).__name__
                last_errors = (error,)
                attempt["errors"] = list(last_errors)
                if isinstance(exc, LLMConfigurationError):
                    failure_category = "configuration"
                    break
                status_code = self._status_code(exc)
                if status_code in {400, 401, 402, 403}:
                    failure_category = f"http_{status_code}"
                    break
                failure_category = (
                    "timeout" if self._is_timeout(exc) else "transport"
                )
                continue

            if self._transport_deadline_expired(
                transport_deadline_monotonic, deadline_monotonic
            ):
                last_errors = ("decision_deadline_exceeded",)
                attempt["errors"] = list(last_errors)
                failure_category = "timeout"
                break
            if not isinstance(raw, str):
                raw = str(raw)
            attempt["raw_output"] = self._redact(raw)
            call["raw_attempts"].append(self._redact(raw))
            if validate is None:
                payload = {"text": raw}
                last_errors = () if raw.strip() else ("response text is empty",)
            else:
                try:
                    payload = parse_object(raw)
                except (TypeError, ValueError) as exc:
                    last_errors = (f"response is not valid JSON: {exc}",)
                else:
                    last_errors = tuple(validate(payload))
            if self._deadline_expired(deadline_monotonic):
                last_errors = ("decision_deadline_exceeded",)
                attempt["errors"] = list(last_errors)
                failure_category = "timeout"
                break
            if not last_errors:
                call["success"] = True
                return ModelResult(call_id, True, payload, (), None)

            failure_category = "validation"
            attempt["errors"] = self.redact_log(last_errors)
            call["validation_errors"].append(self.redact_log(last_errors))
            messages.extend(
                [
                    {"role": "assistant", "content": raw},
                    self._correction_message(
                        last_errors,
                        "Return exactly one corrected JSON object."
                        if validate is not None else
                        "Return a non-empty corrected text response.",
                    ),
                ]
            )

        call["failure_category"] = failure_category
        return ModelResult(
            call_id=call_id,
            success=False,
            payload=None,
            errors=tuple(self._redact(error) for error in last_errors),
            failure_category=failure_category,
        )

    @staticmethod
    def _validate_deadline(value: float | None, name: str) -> None:
        if value is None:
            return
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError(f"{name} must be a finite number")

    @staticmethod
    def _deadline_expired(deadline_monotonic: float | None) -> bool:
        return (
            deadline_monotonic is not None
            and time.perf_counter() >= deadline_monotonic
        )

    @staticmethod
    def _transport_deadline_expired(
        transport_deadline_monotonic: float | None,
        deadline_monotonic: float | None,
    ) -> bool:
        effective_deadline = transport_deadline_monotonic
        if effective_deadline is None:
            effective_deadline = deadline_monotonic
        return LLMGateway._deadline_expired(effective_deadline)

    @staticmethod
    def _transport_timeout(
        *,
        timeout_seconds: float | None,
        deadline_monotonic: float | None,
        transport_deadline_monotonic: float | None,
    ) -> float | None:
        effective_deadline = transport_deadline_monotonic
        if effective_deadline is None:
            effective_deadline = deadline_monotonic
        if effective_deadline is None:
            return timeout_seconds
        remaining = effective_deadline - time.perf_counter()
        if remaining <= 0.0:
            return 0.0
        if timeout_seconds is None:
            return remaining
        return min(float(timeout_seconds), remaining)


__all__ = [
    "LLMConfigurationError",
    "LLMGateway",
    "ModelResult",
    "OpenAICompatibleTransport",
    "parse_object",
    "reject_constant",
    "unique_object",
]
