import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest
import yaml

from src.mission.llm_gateway import (
    LLMConfigurationError,
    LLMGateway,
    ModelResult,
    parse_object,
)


def test_parse_object_accepts_one_complete_json_object():
    assert parse_object(' {"answer": 1} ') == {"answer": 1}


def test_parse_object_accepts_one_complete_json_code_fence():
    assert parse_object('```json\n{"answer": 1}\n```') == {"answer": 1}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ('{"answer": 1, "answer": 2}', "duplicate JSON key: answer"),
        ('{"answer": NaN}', "non-finite JSON constant: NaN"),
        ('{"answer": Infinity}', "non-finite JSON constant: Infinity"),
        ('{"answer": -Infinity}', "non-finite JSON constant: -Infinity"),
        ('[1, 2]', "expected JSON object"),
        ('null', "expected JSON object"),
    ],
)
def test_parse_object_rejects_non_strict_json(raw, message):
    with pytest.raises(ValueError, match=message):
        parse_object(raw)


@pytest.mark.parametrize(
    "raw",
    [
        'prefix {"answer": 1} suffix',
        'prefix ```json\n{"answer": 1}\n```',
        '```json\n{"answer": 1}\n``` trailing',
        '```json\n{"answer": 1}\n```\n```json\n{"answer": 2}\n```',
        '{bad} {"answer": 1}',
    ],
)
def test_parse_object_rejects_arbitrary_json_fragments(raw):
    with pytest.raises(ValueError):
        parse_object(raw)


def _validate_answer(payload):
    errors = []
    extra = set(payload) - {"answer"}
    if extra:
        errors.append(f"extra fields: {sorted(extra)}")
    if not isinstance(payload.get("answer"), int):
        errors.append("answer: expected integer")
    return tuple(errors)


def _request_json(gateway, role="decision_maker", marker="blue"):
    return gateway.request_json(
        role=role,
        snapshot_id=f"snapshot-{marker}",
        system_prompt=f"system-{marker}",
        user_payload={"marker": marker},
        validate=_validate_answer,
    )


def test_invalid_json_is_corrected_with_exact_assistant_output(scripted_transport):
    raw = "  {bad}\n"
    transport = scripted_transport({
        "decision_maker": [raw, '{"answer": 7}'],
    })
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway)

    assert result == ModelResult(
        call_id=result.call_id,
        success=True,
        payload={"answer": 7},
        errors=(),
        failure_category=None,
    )
    assert len(transport.calls) == 2
    correction_messages = transport.calls[1]["messages"][-2:]
    assert correction_messages[0] == {"role": "assistant", "content": raw}
    correction = json.loads(correction_messages[1]["content"])
    assert correction["type"] == "validation_correction"
    assert correction["errors"]
    assert "valid JSON" in correction["errors"][0]

    attempts = gateway.call_log[-1]["attempts"]
    assert attempts[0]["raw_output"] == raw
    assert attempts[1]["raw_output"] == '{"answer": 7}'
    assert attempts[0]["messages"] == transport.calls[0]["messages"]
    assert attempts[1]["messages"] == transport.calls[1]["messages"]


def test_schema_errors_and_extra_fields_are_corrected(scripted_transport):
    transport = scripted_transport({
        "decision_maker": [
            '{"answer": "wrong", "unexpected": true}',
            '{"answer": 8}',
        ],
    })
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway)

    assert result.success
    assert result.payload == {"answer": 8}
    correction = json.loads(transport.calls[1]["messages"][-1]["content"])
    assert correction["errors"] == [
        "extra fields: ['unexpected']",
        "answer: expected integer",
    ]
    assert transport.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": '{"answer": "wrong", "unexpected": true}',
    }


def test_continuous_validation_failure_returns_no_business_payload(scripted_transport):
    transport = scripted_transport({"decision_maker": ["{bad}"] * 3})
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway)

    assert not result.success
    assert result.payload is None
    assert result.failure_category == "validation"
    assert len(transport.calls) == 3
    assert len(gateway.call_log[-1]["attempts"]) == 3


class _HTTPError(RuntimeError):
    def __init__(self, status_code):
        super().__init__(f"request failed with status {status_code}")
        self.status_code = status_code


@pytest.mark.parametrize("status_code", [400, 401, 402, 403])
def test_non_retryable_http_errors_fail_immediately(
    scripted_transport, status_code
):
    transport = scripted_transport({
        "decision_maker": [_HTTPError(status_code), '{"answer": 1}'],
    })
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway)

    assert not result.success
    assert result.payload is None
    assert result.failure_category == f"http_{status_code}"
    assert len(transport.calls) == 1


def test_timeout_is_bounded_by_application_retry_limit(scripted_transport):
    transport = scripted_transport({
        "decision_maker": [TimeoutError("slow model")] * 3,
    })
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway)

    assert not result.success
    assert result.failure_category == "timeout"
    assert result.errors == ("slow model",)
    assert len(transport.calls) == 3


def test_role_requests_do_not_share_conversation_messages(scripted_transport):
    transport = scripted_transport({
        "decision_maker": ['{"answer": 1}'],
        "red_commander": ['{"answer": 2}'],
    })
    gateway = LLMGateway(transport=transport)

    blue = _request_json(gateway, marker="blue")
    red = _request_json(gateway, role="red_commander", marker="red")

    assert blue.success and red.success
    assert transport.calls[0]["messages"] == [
        {"role": "system", "content": "system-blue"},
        {"role": "user", "content": '{"marker": "blue"}'},
    ]
    assert transport.calls[1]["messages"] == [
        {"role": "system", "content": "system-red"},
        {"role": "user", "content": '{"marker": "red"}'},
    ]


def test_attempt_logs_and_fixture_calls_do_not_contain_api_key(
    monkeypatch, scripted_transport
):
    secret = "task-three-secret-key"
    monkeypatch.setenv("LONGCAT_API_KEY", secret)
    transport = scripted_transport({"decision_maker": ['{"answer": 1}']})
    gateway = LLMGateway(transport=transport)

    assert _request_json(gateway).success

    serialized = json.dumps(
        {"call_log": gateway.call_log, "transport_calls": transport.calls}
    )
    assert secret not in serialized


def _write_llm_config(tmp_path: Path, mutate):
    data = yaml.safe_load(Path("configs/llm_params.yaml").read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "llm_params.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


@pytest.mark.parametrize(
    ("role", "expected_max_tokens"),
    [
        ("decision_maker", 4096),
        ("contact_assessor", 2048),
        ("red_commander", 4096),
        ("reviewer", 2048),
    ],
)
def test_all_required_role_bindings_are_used(
    scripted_transport, role, expected_max_tokens
):
    transport = scripted_transport({role: ['{"answer": 1}']})
    gateway = LLMGateway(transport=transport)

    result = _request_json(gateway, role=role, marker=role)

    assert result.success
    assert transport.calls[0]["model"] == "LongCat-2.0"
    assert transport.calls[0]["max_tokens"] == expected_max_tokens


@pytest.mark.parametrize(
    "role",
    ["decision_maker", "contact_assessor", "red_commander", "reviewer"],
)
def test_each_required_role_binding_is_validated(tmp_path, scripted_transport, role):
    path = _write_llm_config(
        tmp_path,
        lambda data: data["bindings"].pop(role),
    )

    with pytest.raises(LLMConfigurationError, match=role):
        LLMGateway(llm_params_path=path, transport=scripted_transport({}))


@pytest.mark.parametrize(
    ("role", "wrong_max_tokens"),
    [
        ("decision_maker", 2048),
        ("contact_assessor", 4096),
        ("red_commander", 2048),
        ("reviewer", 4096),
    ],
)
def test_each_required_role_token_budget_is_validated(
    tmp_path, scripted_transport, role, wrong_max_tokens
):
    def mutate(data):
        data["bindings"][role]["max_tokens"] = wrong_max_tokens

    path = _write_llm_config(tmp_path, mutate)

    with pytest.raises(LLMConfigurationError, match=role):
        LLMGateway(llm_params_path=path, transport=scripted_transport({}))


def test_unknown_role_is_rejected_before_transport(scripted_transport):
    transport = scripted_transport({"invented_role": ['{"answer": 1}']})
    gateway = LLMGateway(transport=transport)

    with pytest.raises(LLMConfigurationError, match="invented_role"):
        _request_json(gateway, role="invented_role")

    assert transport.calls == []


def test_default_transport_uses_openai_compatible_api_without_sdk_retries(
    monkeypatch
):
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self.create)
            )

        def close(self):
            pass

        @staticmethod
        def create(**kwargs):
            captured["request"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(content='{"answer": 9}')
                    )
                ]
            )

    monkeypatch.setenv("LONGCAT_API_KEY", "offline-test-key")
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=FakeOpenAI))
    gateway = LLMGateway()

    result = _request_json(gateway)

    assert result.success
    assert captured["client"] == {
        "api_key": "offline-test-key",
        "base_url": "https://api.longcat.chat/openai/v1",
        "timeout": 120.0,
        "max_retries": 0,
    }
    assert captured["request"]["model"] == "LongCat-2.0"
    assert captured["request"]["max_tokens"] == 4096


def test_text_request_returns_text_payload_and_logs_exact_raw(scripted_transport):
    raw = "  concise reviewer memory\n"
    transport = scripted_transport({"reviewer": [raw]})
    gateway = LLMGateway(transport=transport)

    result = gateway.request_text(
        role="reviewer",
        snapshot_id="review-15",
        system_prompt="review system",
        user_payload={"events": ["contact_created"]},
    )

    assert result.success
    assert result.payload == {"text": raw}
    assert gateway.call_log[-1]["attempts"][0]["raw_output"] == raw
    assert transport.calls[0]["json_mode"] is False


def test_call_context_records_episode_time_and_memory_version(scripted_transport):
    transport = scripted_transport({"reviewer": ["ok"]})
    gateway = LLMGateway(transport=transport)
    gateway.set_context("episode-live-01", "7", 12.0)

    gateway.request_text(
        role="reviewer",
        snapshot_id="review-12",
        system_prompt="review system",
        user_payload={},
    )

    assert gateway.call_log[-1]["episode_id"] == "episode-live-01"
    assert gateway.call_log[-1]["sim_time_min"] == 12.0
    assert gateway.call_log[-1]["memory_version"] == "7"


def test_empty_text_response_has_bounded_corrections_and_no_payload(
    scripted_transport,
):
    transport = scripted_transport({"reviewer": ["", "  ", "\n"]})
    gateway = LLMGateway(transport=transport)

    result = gateway.request_text(
        role="reviewer",
        snapshot_id="review-empty",
        system_prompt="review system",
        user_payload={"events": []},
    )

    assert not result.success
    assert result.payload is None
    assert result.failure_category == "validation"
    assert result.errors == ("response text is empty",)
    assert len(transport.calls) == 3
    assert transport.calls[1]["messages"][-2] == {
        "role": "assistant",
        "content": "",
    }


@pytest.mark.parametrize("text_mode", [False, True])
def test_logs_redact_secrets_in_all_fields_without_changing_wire_messages(
    monkeypatch, scripted_transport, text_mode,
):
    secret = 'test-secret-"quoted"-密钥'
    monkeypatch.setenv("LONGCAT_API_KEY", secret)
    raw = json.dumps({"answer": secret}, ensure_ascii=False)
    transport = scripted_transport({"reviewer": [raw] * 3})
    gateway = LLMGateway(transport=transport)
    kwargs = dict(
        role="reviewer", snapshot_id=secret, system_prompt=secret,
        user_payload={"prompt": secret},
    )
    if text_mode:
        result = gateway.request_text(**kwargs)
    else:
        result = gateway.request_json(
            **kwargs, validate=lambda payload: (f"invalid answer: {payload['answer']}",),
        )
        assert transport.calls[1]["messages"][-2]["content"] == raw
    assert transport.calls[0]["messages"][0]["content"] == secret
    serialized = json.dumps(gateway.call_log, ensure_ascii=False)
    assert "test-secret-" not in serialized
    assert "test-secret-" not in str(result.errors)
    assert "[REDACTED]" in serialized


@pytest.mark.parametrize("text_mode", [False, True])
def test_real_sdk_timeout_has_timeout_failure_category(scripted_transport, text_mode):
    import httpx
    from openai import APITimeoutError

    timeout = APITimeoutError(request=httpx.Request("POST", "https://offline.invalid"))
    transport = scripted_transport({"reviewer": [timeout] * 3})
    gateway = LLMGateway(transport=transport)
    kwargs = dict(role="reviewer", snapshot_id="t", system_prompt="s", user_payload={})
    result = (gateway.request_text(**kwargs) if text_mode else
              gateway.request_json(**kwargs, validate=_validate_answer))
    assert result.failure_category == "timeout"
    assert result.payload is None
    assert len(transport.calls) == 3


def test_script_exhaustion_is_not_swallowed_as_model_failure(scripted_transport):
    gateway = LLMGateway(transport=scripted_transport({}))
    with pytest.raises(AssertionError, match="no scripted response"):
        _request_json(gateway)


@pytest.mark.parametrize("role", ["decision_maker", "contact_assessor", "red_commander", "reviewer"])
def test_malformed_role_binding_is_configuration_error(tmp_path, scripted_transport, role):
    path = _write_llm_config(tmp_path, lambda data: data["bindings"].update({role: None}))
    with pytest.raises(LLMConfigurationError, match=role):
        LLMGateway(path, transport=scripted_transport({}))


@pytest.mark.parametrize("configured,attempts", [(0, 1), (1, 2), (2, 3), (20, 3)])
def test_application_retry_configuration_is_capped(tmp_path, scripted_transport, configured, attempts):
    path = _write_llm_config(tmp_path, lambda data: data["cycles"].update(max_retries=configured))
    transport = scripted_transport({"decision_maker": ["{bad}"] * attempts})
    result = _request_json(LLMGateway(path, transport=transport))
    assert not result.success
    assert len(transport.calls) == attempts


@pytest.mark.parametrize("invalid", [-1, True, "2", 1.5])
def test_invalid_retry_configuration_is_rejected(tmp_path, scripted_transport, invalid):
    path = _write_llm_config(tmp_path, lambda data: data["cycles"].update(max_retries=invalid))
    with pytest.raises(LLMConfigurationError, match="max_retries"):
        LLMGateway(path, transport=scripted_transport({}))


@pytest.mark.parametrize("status_code", [400, 401, 402, 403])
def test_text_http_failures_are_immediate_and_redacted(monkeypatch, scripted_transport, status_code):
    secret = "offline-secret-status"
    monkeypatch.setenv("LONGCAT_API_KEY", secret)
    exc = _HTTPError(status_code)
    exc.args = (f"Authorization: Bearer {secret}",)
    transport = scripted_transport({"reviewer": [exc]})
    gateway = LLMGateway(transport=transport)
    result = gateway.request_text(role="reviewer", snapshot_id="r", system_prompt="s", user_payload={})
    assert result.failure_category == f"http_{status_code}"
    assert len(transport.calls) == 1
    assert secret not in json.dumps(gateway.call_log)
    assert secret not in str(result.errors)


def test_same_role_starts_fresh_after_correction_and_logs_have_unique_ids(scripted_transport):
    transport = scripted_transport({"decision_maker": ["{bad}", '{"answer": 1}', '{"answer": 2}']})
    gateway = LLMGateway(transport=transport)
    first = _request_json(gateway, marker="first")
    second = _request_json(gateway, marker="second")
    assert first.call_id != second.call_id
    assert len(transport.calls[2]["messages"]) == 2
    assert "first" not in json.dumps(transport.calls[2])
    assert gateway.call_log[0]["snapshot_id"] == "snapshot-first"
    assert gateway.call_log[1]["snapshot_id"] == "snapshot-second"


def test_standalone_gateway_loads_existing_dotenv_convention(monkeypatch, tmp_path):
    config_path = str(Path("configs/llm_params.yaml").resolve())
    monkeypatch.chdir(tmp_path)
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / ".env").write_text("LONGCAT_API_KEY=offline-dotenv-key\n")
    monkeypatch.delenv("LONGCAT_API_KEY", raising=False)
    gateway = LLMGateway(config_path)
    gateway.assert_ready()


def test_missing_credentials_fail_once_without_creating_sdk_client(monkeypatch):
    import openai
    from src.schedule.env_loader import EnvLoader

    monkeypatch.setattr(EnvLoader, "load_dotenv", lambda: None)
    monkeypatch.delenv("LONGCAT_API_KEY", raising=False)

    def forbidden_client(**kwargs):
        raise AssertionError("SDK client must not be constructed without credentials")

    monkeypatch.setattr(openai, "OpenAI", forbidden_client)
    gateway = LLMGateway()
    result = _request_json(gateway)
    assert result.failure_category == "configuration"
    assert result.payload is None
    assert len(gateway.call_log[-1]["attempts"]) == 1


@pytest.mark.parametrize("outcome,expected_calls", [(200, 1), (400, 1), (401, 1), (402, 1), (403, 1), ("timeout", 3)])
def test_real_sdk_uses_no_hidden_retries_and_closes_http_clients(monkeypatch, outcome, expected_calls):
    import httpx
    import openai

    real_openai = openai.OpenAI
    requests = []
    http_clients = []

    def handle(request):
        requests.append(request)
        if outcome == "timeout":
            raise httpx.ReadTimeout("offline timeout", request=request)
        if outcome != 200:
            return httpx.Response(outcome, json={"error": {"message": "offline rejection"}})
        return httpx.Response(200, json={
            "id": "offline", "object": "chat.completion", "created": 0,
            "model": "LongCat-2.0",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": '{"answer": 9}'}}],
        })

    def make_client(**kwargs):
        assert kwargs["max_retries"] == 0
        client = httpx.Client(transport=httpx.MockTransport(handle))
        http_clients.append(client)
        return real_openai(http_client=client, **kwargs)

    monkeypatch.setenv("LONGCAT_API_KEY", "offline-sdk-key")
    monkeypatch.setattr(openai, "OpenAI", make_client)
    gateway = LLMGateway()
    result = _request_json(gateway)
    assert result.success == (outcome == 200)
    assert len(requests) == expected_calls
    assert all(client.is_closed for client in http_clients)
    assert "offline-sdk-key" not in json.dumps(gateway.call_log)
