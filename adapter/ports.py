"""Abstract ports: the only legal seams across the adapter boundary.

- ``SchedulerPort`` receives an ``AgentJobDTO`` for every heavy scheduling
  decision. The scheduling pipeline never calls a model client directly.
- ``ToolsPort`` is the dispatch surface the worker's tool calls resolve
  through.
- ``EventsPort`` publishes adapter events for the websocket layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from adapter.contracts import (
    AgentJobDTO,
    SelectionPayloadDTO,
    ToolSpec,
    WsAgentEvent,
)


class SchedulerPort(ABC):
    """Delivers scheduling decisions to an agent and awaits the result."""

    @abstractmethod
    def provide_selection(self, snapshot, payload: dict) -> dict | None:
        """Resolve a mission-selection job; returns the raw selection dict
        or None when no valid selection arrived before the deadline."""

    @abstractmethod
    def decide_regions(
        self,
        sm,
        ivt,
        candidate_result,
        required_search_regions: int,
    ) -> dict:
        """Resolve a legacy-mission-plan job; returns the llm_output dict."""

    @property
    def last_failure_category(self) -> str | None:
        """Reported to MissionScheduler when provide_selection returns None."""
        return None

    @property
    def deadline_budget_seconds(self) -> float:
        """Extra wall-clock the port needs beyond the normal LLM latency."""
        return 0.0


class ToolsPort(ABC):
    """Serves the worker's tool catalog and executes calls."""

    @abstractmethod
    def catalog(self) -> tuple[ToolSpec, ...]:
        """Every tool the worker may invoke."""

    @abstractmethod
    def invoke(
        self,
        name: str,
        params: dict,
        job: AgentJobDTO,
    ) -> dict:
        """Execute a tool call on behalf of a running job."""


class EventsPort(ABC):
    """Publishes adapter events for websocket subscribers and the journal."""

    @abstractmethod
    def emit(self, event: WsAgentEvent) -> None:
        """Fan out one event to every live subscriber."""

    @abstractmethod
    def subscribe(self) -> Any:
        """Return a subscriber handle yielding WsAgentEvent frames."""
