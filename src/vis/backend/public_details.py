"""Detached public visualization data; never publish private reasoning channels."""
import json
import re

_PRIVATE = {'reasoning_content', 'think', 'thinking', 'chain_of_thought', 'reasoning'}
_THINK = re.compile(r'<(?:think|thinking|reasoning)\b[^>]*>[\s\S]*?(?:</(?:think|thinking|reasoning)>|$)', re.I)


def public_value(value):
    if isinstance(value, dict):
        return {key: (item if key == "provider_channels" and isinstance(item, list)
                      and all(isinstance(channel, dict) and channel.get("provenance") == "external_api_response"
                              for channel in item) else public_value(item)) for key, item in value.items()
                if str(key).lower() not in _PRIVATE}
    if isinstance(value, (list, tuple)):
        return [public_value(item) for item in value]
    if isinstance(value, str):
        value = _THINK.sub('', value)
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        if isinstance(parsed, (dict, list)):
            return json.dumps(public_value(parsed), ensure_ascii=False)
    return value


def public_call(call):
    # Prompt snapshots dominate call-log size and are not decision telemetry.
    # Omit before recursively sanitizing to avoid traversing multi-MB messages.
    lean = {key: value for key, value in call.items()
            if key not in {"system_prompt", "user_prompt", "raw_attempts", "messages"}}
    lean["attempts"] = [{key: value for key, value in attempt.items() if key != "messages"}
                        for attempt in call.get("attempts", [])]
    result = public_value(lean)
    result.setdefault("provider_channels", [])
    result.setdefault("thinking_mode", "not provided")
    candidates = [result]
    for attempt in reversed(result.get('attempts') or []):
        raw = attempt.get('raw_output', attempt.get('response'))
        try:
            candidates.append(json.loads(raw) if isinstance(raw, str) else raw)
        except ValueError:
            pass
    for raw in (result.get('response'), result.get('parsed_output')):
        try:
            candidates.append(json.loads(raw) if isinstance(raw, str) else raw)
        except ValueError:
            pass
    summary = None
    label = 'Decision notes / rationale'
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ('decision_notes', 'notes', 'reason', 'decision_rationale'):
            if candidate.get(key):
                summary = candidate[key]
                break
        if summary is not None:
            break
    result['decision_summary'] = summary if summary is not None else 'not provided'
    result['decision_summary_label'] = label
    return result


def model_calls(engine, episode_id, *, limit=10):
    allocator = getattr(engine, 'allocator', None)
    gateway = getattr(getattr(allocator, 'llm_client', None), 'gateway', None)
    calls = getattr(gateway, 'call_log', ())
    selected = [call for call in list(calls) if call.get("episode_id") == episode_id][-limit:]
    return [public_call(call) for call in selected]


def public_frame(frame):
    result = public_value({key: value for key, value in frame.items()
                           if key not in {"llm_cycle", "model_calls"}})
    if "llm_cycle" in frame:
        result["llm_cycle"] = public_call(frame["llm_cycle"]) if isinstance(frame["llm_cycle"], dict) else None
    if "model_calls" in frame:
        result["model_calls"] = [public_call(call) for call in frame.get("model_calls") or []]
    return result
