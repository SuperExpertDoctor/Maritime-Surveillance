"""Adapter layer: the single seam for cross-module data exchange.

Subpackages:
- contracts — pydantic DTOs shared by every module boundary
- ports — abstract SchedulerPort/ToolsPort/EventsPort
- mission_adapter — internal sim objects -> MissionSnapshotDTO
- schedule_adapter — SchedulerPort/ToolsPort implementations
- broker — agent job queue, messages, plan approvals
- events — worker events -> websocket frames
- agent_api — FastAPI router mounted by the vis backend (imported lazily so
  the scheduler path never requires fastapi)
"""

from adapter.broker import AgentBroker, AgentJob
from adapter.events import AgentEventHub
from adapter.schedule_adapter import (
    HarnessSchedulerPort,
    InProcessSchedulerPort,
    MissionToolsPort,
)

__all__ = [
    "AgentBroker",
    "AgentEventHub",
    "AgentJob",
    "build_agent_router",
    "HarnessSchedulerPort",
    "InProcessSchedulerPort",
    "MissionToolsPort",
]


def __getattr__(name):
    if name == "build_agent_router":
        from adapter.agent_api import build_agent_router
        return build_agent_router
    raise AttributeError(name)
