"""Agent-facing contracts: tools, jobs, messages, websocket frames."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

JOB_KIND_SELECTION = "mission-selection/v1"
JOB_KIND_LEGACY_PLAN = "legacy-plan/v1"
JOB_KIND_OPERATOR = "operator-message/v1"

JobStatus = Literal[
    "queued", "running", "pending_approval", "completed", "failed", "cancelled"
]
PlanStatus = Literal[
    "pending_approval", "approved", "auto_approved", "rejected", "expired"
]
AutonomyMode = Literal["request", "assisted", "full"]

# --- AssignmentView: validate_selection 通过后的确定性配对结果 ---


class AssignmentViewDTO(BaseModel):
    """One deterministic task<->UAV pairing, produced by the adapter only.

    The frontend renders this verbatim; it never derives pairings itself.
    """

    model_config = ConfigDict(frozen=True)

    task_id: str
    uav_id: str
    kind: str = ""
    label: str = ""
    bbox: tuple[float, float, float, float] | None = None
    center: tuple[float, float] | None = None
    target_id: str | None = None
    source: str = "selection"


class ToolSpec(BaseModel):
    """Backend tool descriptor served to the PI worker."""

    model_config = ConfigDict(frozen=True)

    name: str
    label: str | None = None
    description: str = ""
    snippet: str | None = None
    guidelines: tuple[str, ...] = ()
    parameters: dict = Field(default_factory=dict)
    constrained_sampling: Any | None = None
    execution_mode: Literal["sequential", "parallel"] | None = None
    plugin: bool = False


class FeedbackItem(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    delivery: Literal["steer", "followUp"] = "followUp"
    attempt: int = 1


class AgentJobDTO(BaseModel):
    """One unit of work delivered to the PI worker."""

    run_id: str
    episode_id: str
    kind: str = JOB_KIND_OPERATOR
    source: str = "human"
    text: str = ""
    status: JobStatus = "queued"
    created_at_min: float = 0.0
    feedback: tuple[FeedbackItem, ...] = ()
    accepted_feedback: tuple[FeedbackItem, ...] = ()
    coalesced_count: int = 0
    error: str | None = None
    result_summary: dict | None = None


class AgentMessageDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    role: Literal["user", "assistant", "tool", "system"]
    text: str = ""
    time_min: float = 0.0
    model: str = ""
    run_id: str | None = None
    status: str = "completed"
    delivery: Literal["steer", "followUp"] | None = None
    delivery_status: str | None = None
    feedback_id: str | None = None
    plan_id: str | None = None


class AgentPlanDTO(BaseModel):
    """A submitted mission plan awaiting or past operator approval."""

    model_config = ConfigDict(frozen=True)

    plan_id: str
    run_id: str
    job_id: str = ""
    episode_id: str = ""
    kind: str = JOB_KIND_SELECTION
    status: PlanStatus = "pending_approval"
    decision_reason: str = ""
    summary: dict = Field(default_factory=dict)
    created_at_min: float = 0.0
    decided_at_min: float = 0.0
    decided_by: str = ""
    decision_comment: str = ""


class AgentStatusDTO(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["idle", "running", "degraded"] = "idle"
    error: str | None = None
    cycle: int = 0
    worker_connected: bool = False
    autonomy_mode: AutonomyMode = "full"
    queued: int = 0
    episode_id: str = ""


class WsAgentEvent(BaseModel):
    """One frame pushed over /ws/agent."""

    model_config = ConfigDict(frozen=True)

    type: str
    payload: dict = Field(default_factory=dict)


class AgentStateFrame(BaseModel):
    """Full-state snapshot sent to a freshly connected websocket client."""

    model_config = ConfigDict(frozen=True)

    type: Literal["agent_state"] = "agent_state"
    agent: dict = Field(default_factory=dict)
    messages: tuple[AgentMessageDTO, ...] = ()
    jobs: tuple[AgentJobDTO, ...] = ()
    plans: tuple[AgentPlanDTO, ...] = ()
    episode_id: str = ""
