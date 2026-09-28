"""Bounded, cursor-addressable status stream for live operator diagnostics."""
from collections import deque
from datetime import datetime, timezone
from threading import Lock


class RuntimeJournal:
    def __init__(self, capacity: int = 600):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._items = deque(maxlen=capacity)
        self._sequence = 0
        self._lock = Lock()

    def append(self, source: str, level: str, status: str, **metadata) -> dict:
        # Only fixed operational fields cross the public boundary.
        allowed = ("episode_id", "sim_time_min", "role", "call_id", "attempt",
                   "trigger_type", "uav_id", "task_id", "failure_category")
        with self._lock:
            self._sequence += 1
            entry = {
                "id": self._sequence,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "level": level,
                "status": status,
                **{key: metadata[key] for key in allowed if key in metadata},
            }
            self._items.append(entry)
            return dict(entry)

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._sequence

    def since(self, after: int, *, episode_id: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._items
                    if item["id"] > after and (episode_id is None or item.get("episode_id") == episode_id)]


_ALGORITHM_EVENTS = {
    "mission_assignment_committed", "mission_selection_failed", "decision_failed",
    "mission_model_failure", "mission_model_paused", "mission_model_retry_succeeded",
    "mission_model_retry_failed", "route_plan_failed", "task_failed",
    "task_completed", "uav_returned", "target_found", "search_complete",
    "assessment_applied", "probe_timed_out",
}
_ERROR_EVENTS = {"decision_failed", "mission_model_failure", "mission_model_paused",
                 "mission_model_retry_failed", "route_plan_failed", "task_failed"}


def publish_algorithm_events(state, journal: RuntimeJournal | None, seen: set[str]) -> None:
    """Mirror significant simulator events to live diagnostics and replay frames."""
    for event in state.get_recent_events(state.current_time - 1, until_time=state.current_time,
                                         include_since=False):
        if event["type"] not in _ALGORITHM_EVENTS or event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        data = event.get("data") or {}
        level = "error" if event["type"] in _ERROR_EVENTS else "info"
        metadata = {key: data[key] for key in ("uav_id", "task_id", "failure_category")
                    if isinstance(data.get(key), str)}
        state.add_event("runtime_log", {
            "source": "algorithm", "level": level, "status": event["type"], **metadata,
        })
        if journal is not None:
            journal.append("algorithm", level, event["type"],
                           episode_id=state.episode_id,
                           sim_time_min=event["time"], **metadata)
