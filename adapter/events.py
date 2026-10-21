"""Agent event hub: worker reports -> frontend websocket frames.

Every worker-reported session event, tool call lifecycle marker and plan
approval transition becomes a ``WsAgentEvent`` pushed to /ws/agent
subscribers, plus a bounded ring buffer used to hydrate reconnecting
clients. A ``record_sink`` callback mirrors concise milestones into the
simulation's event stream so the decisions drawer and replay log keep the
same history.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from adapter.contracts import WsAgentEvent

_LOGGER = logging.getLogger(__name__)

_PUBLIC_EVENTS = {
    "message_start", "message_update", "message_end", "tool_execution_start",
    "tool_execution_update", "tool_execution_end", "compaction_start",
    "compaction_end", "auto_retry_start", "auto_retry_end", "agent_start",
    "agent_end", "agent_settled", "queue_update", "turn_start", "turn_end",
    "summarization_retry_scheduled", "summarization_retry_attempt_start",
    "summarization_retry_finished",
}

_MAX_TEXT = 12000
_BUFFER_LIMIT = 500


class AgentEventHub:
    """EventsPort implementation: fan-out plus ring buffer."""

    def __init__(
        self,
        *,
        record_sink: Callable[[str, dict], None] | None = None,
    ) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._buffer: list[dict] = []
        self._record_sink = record_sink

    # ------------------------------------------------------------------
    # EventsPort
    # ------------------------------------------------------------------
    def emit(self, event: WsAgentEvent) -> None:
        frame = event.model_dump(mode="json")
        payload = frame["payload"]
        self._buffer.append({"type": frame["type"], **payload})
        if len(self._buffer) > _BUFFER_LIMIT:
            self._buffer = self._buffer[-_BUFFER_LIMIT:]
        dead = []
        for queue in self._subscribers:
            try:
                queue.put_nowait({"type": frame["type"], **payload})
            except asyncio.QueueFull:
                dead.append(queue)
        for queue in dead:
            self._subscribers.discard(queue)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def recent(self, limit: int = 200) -> list[dict]:
        return list(self._buffer[-max(1, min(limit, _BUFFER_LIMIT)):])

    # ------------------------------------------------------------------
    # Worker session events -> public frames
    # ------------------------------------------------------------------
    def session_event(self, job_run_id: str, broker, event: dict) -> None:
        """Project one worker session_event into messages + frames."""
        kind = event.get("type")
        if kind not in _PUBLIC_EVENTS:
            raise ValueError("unknown_session_event")
        data = {
            key: event[key]
            for key in (
                "type", "message_id", "tool_call_id", "tool_name", "is_error",
                "count", "reason", "attempt", "will_retry", "status",
            )
            if key in event
        }
        text = str(event.get("text", ""))[:_MAX_TEXT]
        data.update(run_id=job_run_id, text=text)
        if kind.startswith("message_"):
            message_id = str(event.get("message_id", ""))[:160]
            if not message_id:
                raise ValueError("message_id_required")
            message = broker.upsert_message(
                message_id,
                run_id=job_run_id,
                role="assistant",
                text=text,
                append=kind == "message_update",
                status=(
                    "streaming" if kind != "message_end" else (
                        "failed" if event.get("status") == "failed" else "completed"
                    )
                ),
            )
            self.emit(WsAgentEvent(type="agent_message", payload={"message": message}))
        elif kind.startswith("tool_execution_"):
            self.emit(WsAgentEvent(type="tool_event", payload={"event": data}))
        else:
            self.emit(WsAgentEvent(type="session_event", payload={"event": data}))

    def record_milestone(self, event_type: str, data: dict) -> None:
        """Mirror one concise milestone into the sim event stream."""
        if self._record_sink is not None:
            try:
                self._record_sink(event_type, dict(data))
            except Exception:
                _LOGGER.debug("agent event sink failed", exc_info=True)
