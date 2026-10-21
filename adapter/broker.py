"""Agent job broker: the adapter-side queue the PI worker drains.

Mirrors the queue semantics of under-water-harness ``runtime.queue_agent``:
human/feedback jobs outrank material jobs, feedback attaches to a running
job, leases expire on heartbeat timeout, and every mutation fans out to
the event hub so /ws/agent stays live. All state is guarded by one lock;
the scheduler port waits on per-job ``threading.Event`` handles.
"""

from __future__ import annotations

import itertools
import threading
import time
from typing import Any, Callable

from adapter.contracts import (
    AgentJobDTO,
    AgentMessageDTO,
    AgentPlanDTO,
    FeedbackItem,
    WsAgentEvent,
    JOB_KIND_LEGACY_PLAN,
    JOB_KIND_OPERATOR,
    JOB_KIND_SELECTION,
)
from adapter.events import AgentEventHub

WORKER_LEASE_SECONDS = 15.0
WORKER_LIVENESS_SECONDS = 20.0
_MAX_JOBS = 40
_MAX_MESSAGES = 100
_MAX_FEEDBACK_PER_RUN = 8
_MAX_MESSAGE_CHARS = 4000

_SOURCE_PRIORITY = {"human": 0, "feedback": 0}
_JOB_PRIORITY = {JOB_KIND_OPERATOR: 0, JOB_KIND_LEGACY_PLAN: 1, JOB_KIND_SELECTION: 1}

_id_counter = itertools.count(1)


def _identifier(prefix: str) -> str:
    return f"{prefix}-{next(_id_counter):06d}"


class _JobRuntime:
    """Non-serializable per-job internals (validators, completion signal)."""

    __slots__ = ("done", "result", "validators", "deadline", "internal_snapshot",
                 "selection_payload", "snapshot_dto")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: dict | None = None
        self.validators: dict[str, Callable] = {}
        self.deadline: float | None = None
        self.internal_snapshot: Any | None = None
        self.selection_payload: dict | None = None
        self.snapshot_dto: dict | None = None


class AgentJob(AgentJobDTO):
    """Public job plus internal wait/validation handles."""

    model_config = {"frozen": False, "arbitrary_types_allowed": True}

    lease_deadline: float | None = None
    payload: dict = {}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        object.__setattr__(self, "_runtime", _JobRuntime())

    @property
    def runtime(self) -> _JobRuntime:
        return self._runtime


class AgentBroker:
    """Owns jobs, the public message timeline, and plan approvals."""

    def __init__(
        self,
        hub: AgentEventHub,
        *,
        episode_id_fn: Callable[[], str] = lambda: "",
        sim_time_fn: Callable[[], float] = lambda: 0.0,
        autonomy_mode: str = "full",
    ) -> None:
        # Reentrant: queue_human/claim_next/etc. call upsert_message and
        # _publish_state while already holding the lock.
        self.lock = threading.RLock()
        self.hub = hub
        self.episode_id_fn = episode_id_fn
        self.sim_time_fn = sim_time_fn
        self.autonomy_mode = (
            autonomy_mode
            if autonomy_mode in ("full", "assisted", "request")
            else "full"
        )
        self.jobs: list[AgentJob] = []
        self.messages: list[dict] = []
        self.plans: dict[str, AgentPlanDTO] = {}
        self.agent: dict = {"status": "idle", "error": None, "cycle": 0}
        self.last_heartbeat: float | None = None
        self.last_job_finished_monotonic: float | None = None
        self._assignment_view: list[dict] = []
        # "本会话允许同类操作": one grant per kind+domain, recorded on
        # approve_session and cleared when the autonomy mode changes.
        self._session_grants: list[dict] = []

    # ------------------------------------------------------------------
    # Messages / plans
    # ------------------------------------------------------------------
    def upsert_message(self, message_id: str, **fields) -> dict:
        with self.lock:
            message = next((m for m in self.messages if m["id"] == message_id), None)
            if message is None:
                message = {
                    "id": message_id,
                    "role": fields.get("role", "assistant"),
                    "text": "",
                    "time_min": self.sim_time_fn(),
                    "model": fields.get("model", ""),
                    "run_id": fields.get("run_id"),
                    "status": "streaming",
                    "delivery": fields.get("delivery"),
                    "delivery_status": fields.get("delivery_status"),
                    "feedback_id": fields.get("feedback_id"),
                    "plan_id": fields.get("plan_id"),
                    "annotation": fields.get("annotation"),
                }
                self.messages.append(message)
            text = fields.get("text")
            if text:
                if fields.get("append"):
                    message["text"] = (message["text"] + text)[-_MAX_MESSAGE_CHARS * 3:]
                else:
                    message["text"] = text[:_MAX_MESSAGE_CHARS * 3]
            for key in ("status", "delivery_status", "plan_id", "model", "delivery", "feedback_id", "annotation"):
                if fields.get(key) is not None:
                    message[key] = fields[key]
            self.messages = self.messages[-_MAX_MESSAGES:]
            return dict(message)

    def append_operator_message(self, text: str, delivery: str, job: AgentJob, feedback_id: str | None, annotation: dict | None = None) -> dict:
        message = self.upsert_message(
            _identifier("message"), role="user", text=text, run_id=job.run_id,
            status="queued" if job.status == "running" else "completed",
            delivery=delivery, feedback_id=feedback_id, annotation=annotation,
        )
        self.hub.emit(WsAgentEvent(type="agent_message", payload={"message": message}))
        return message

    # ------------------------------------------------------------------
    # Enqueue
    # ------------------------------------------------------------------
    def enqueue(
        self,
        kind: str,
        text: str,
        *,
        source: str,
        payload: dict | None = None,
        validators: dict[str, Callable] | None = None,
        deadline_monotonic: float | None = None,
        trigger: str = "",
    ) -> AgentJob:
        """Post one agent job; material jobs coalesce by kind."""
        with self.lock:
            if kind != JOB_KIND_OPERATOR:
                pending = next(
                    (job for job in self.jobs if job.status == "queued" and job.kind == kind),
                    None,
                )
                if pending is not None:
                    pending.text = text[:_MAX_MESSAGE_CHARS]
                    pending.payload = payload or pending.payload
                    pending.runtime.deadline = deadline_monotonic
                    if validators:
                        pending.runtime.validators = validators
                    pending.coalesced_count += 1
                    self.hub.emit(WsAgentEvent(
                        type="agent_event",
                        payload={"event": {"type": "agent_wakeup_coalesced",
                                           "run_id": pending.run_id, "kind": kind}},
                    ))
                    self._publish_state()
                    return pending
            job = AgentJob(
                run_id=_identifier("run"),
                episode_id=self.episode_id_fn(),
                kind=kind,
                source=source,
                text=text[:_MAX_MESSAGE_CHARS],
                status="queued",
                created_at_min=self.sim_time_fn(),
                trigger=trigger,
                payload=payload or {},
            )
            job.runtime.validators = validators or {}
            job.runtime.deadline = deadline_monotonic
            self.jobs.append(job)
            self.jobs = self.jobs[-_MAX_JOBS:]
            self.hub.emit(WsAgentEvent(
                type="agent_event",
                payload={"event": {"type": "agent_queued", "run_id": job.run_id,
                                   "kind": kind, "source": source}},
            ))
            self.hub.record_milestone("agent_job_queued", {
                "run_id": job.run_id, "kind": kind, "source": source,
            })
            self._publish_state()
            return job

    def queue_human(self, text: str, delivery: str, annotation: dict | None = None) -> AgentJob:
        """Operator input: attaches as feedback to a running job, else queues."""
        text = str(text or "").strip()
        if not text:
            raise ValueError("invalid_message")
        if delivery not in ("steer", "followUp"):
            raise ValueError("invalid_delivery")
        with self.lock:
            self.expire_leases(time.monotonic())
            running = next((j for j in self.jobs if j.status == "running"), None)
            if running is not None:
                if len(running.feedback) + len(running.accepted_feedback) >= _MAX_FEEDBACK_PER_RUN:
                    raise ValueError("feedback_queue_full")
                item = FeedbackItem(
                    id=_identifier("feedback"), text=text[:_MAX_MESSAGE_CHARS],
                    delivery=delivery, annotation=annotation,
                )
                running.feedback = (*running.feedback, item)
                self.append_operator_message(text, delivery, running, item.id, annotation)
                self._publish_state()
                return running
        job = self.enqueue(JOB_KIND_OPERATOR, text, source="human", trigger="human")
        self.append_operator_message(text, delivery, job, None, annotation)
        return job

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------
    def worker_alive(self) -> bool:
        with self.lock:
            return (
                self.last_heartbeat is not None
                and time.monotonic() - self.last_heartbeat < WORKER_LIVENESS_SECONDS
            )

    def expire_leases(self, now: float) -> None:
        for job in self.jobs:
            if job.status == "running" and job.lease_deadline is not None and job.lease_deadline <= now:
                job.status = "failed"
                job.error = "worker_lease_expired"
                job.runtime.result = None
                job.runtime.done.set()
                self.agent.update(status="degraded", error="worker_lease_expired")
                self.hub.emit(WsAgentEvent(
                    type="agent_event",
                    payload={"event": {"type": "agent_failed", "run_id": job.run_id,
                                       "error": "worker_lease_expired"}},
                ))
                self.hub.record_milestone("agent_failed", {
                    "run_id": job.run_id, "error": "worker_lease_expired",
                })

    def claim_next(self, defer_routine: bool) -> AgentJob | None:
        """Worker poll: lease the highest-priority queued job."""
        with self.lock:
            now = time.monotonic()
            self.expire_leases(now)
            self._expire_plans()
            self.last_heartbeat = now
            running = next((j for j in self.jobs if j.status == "running"), None)
            if running is not None:
                return None
            pending_approval = any(p.status == "pending_approval" for p in self.plans.values())
            if pending_approval:
                return None
            eligible = [
                job for job in self.jobs
                if job.status == "queued"
                and (not defer_routine or _JOB_PRIORITY.get(job.kind, 2) < 2
                     or _SOURCE_PRIORITY.get(job.source, 2) < 2)
            ]
            job = min(
                eligible,
                key=lambda j: (_JOB_PRIORITY.get(j.kind, 2), _SOURCE_PRIORITY.get(j.source, 2)),
                default=None,
            )
            if job is not None:
                job.status = "running"
                job.lease_deadline = now + WORKER_LEASE_SECONDS
                self.agent.update(status="running", error=None, cycle=self.agent["cycle"] + 1)
                self.hub.emit(WsAgentEvent(
                    type="agent_event",
                    payload={"event": {"type": "agent_started", "run_id": job.run_id}},
                ))
                self._publish_state()
            elif running is None:
                self.agent["status"] = "idle"
            return job

    def active_run(self, run_id: str, episode_id: str) -> AgentJob:
        if episode_id and episode_id != self.episode_id_fn():
            raise ValueError("episode_mismatch")
        job = next((j for j in self.jobs if j.run_id == run_id), None)
        if job is None or job.status != "running" or job.episode_id != self.episode_id_fn():
            raise ValueError("run_not_active")
        return job

    def heartbeat(self, run_id: str, episode_id: str, acknowledged_ids: list[str], model: str = "") -> dict:
        with self.lock:
            self.last_heartbeat = time.monotonic()
            if model:
                self.agent["model"] = model[:80]
            try:
                job = self.active_run(run_id, episode_id)
            except ValueError:
                return {"cancel": True}
            job.lease_deadline = time.monotonic() + WORKER_LEASE_SECONDS
            if acknowledged_ids:
                accepted = {item.id for item in job.feedback}.intersection(acknowledged_ids)
                if accepted:
                    job.accepted_feedback = (
                        *job.accepted_feedback,
                        *(item for item in job.feedback if item.id in accepted),
                    )
                    job.feedback = tuple(item for item in job.feedback if item.id not in accepted)
                    for message in self.messages:
                        if message.get("run_id") == job.run_id and message.get("feedback_id") in accepted:
                            message.update(status="completed", delivery_status="delivered")
                    self.hub.emit(WsAgentEvent(
                        type="agent_event",
                        payload={"event": {"type": "agent_feedback_delivered",
                                           "run_id": job.run_id,
                                           "feedback_ids": sorted(accepted)}},
                    ))
            return {"cancel": False, "feedback": [item.model_dump() for item in job.feedback]}

    def cancel(self, run_id: str | None = None) -> list[str]:
        cancelled = []
        with self.lock:
            for job in self.jobs:
                if (run_id is None or job.run_id == run_id) and job.status in ("queued", "running"):
                    job.status = "cancelled"
                    job.feedback = ()
                    job.runtime.result = None
                    job.runtime.done.set()
                    cancelled.append(job.run_id)
            for message in self.messages:
                if message.get("run_id") in cancelled and message.get("status") in ("queued", "streaming"):
                    message["status"] = "cancelled"
            for run_id_ in cancelled:
                self.hub.emit(WsAgentEvent(
                    type="agent_event",
                    payload={"event": {"type": "agent_cancelled", "run_id": run_id_}},
                ))
            if cancelled:
                self.agent["status"] = "idle"
                self._publish_state()
        return cancelled

    def complete(self, job: AgentJob, result: dict | None, *, failed: bool = False, error: str | None = None) -> None:
        with self.lock:
            if job.status not in ("running", "queued"):
                return
            job.status = "failed" if failed else "completed"
            job.error = error
            job.result_summary = {
                key: result[key]
                for key in ("plan_id", "status", "selected_task_ids")
                if isinstance(result, dict) and key in result
            } if result else None
            job.runtime.result = result
            job.runtime.done.set()
            self.last_job_finished_monotonic = time.monotonic()
            self.agent.update(status="idle" if not failed else "degraded", error=error)
            for message in self.messages:
                if message.get("run_id") == job.run_id and message.get("status") == "streaming":
                    message["status"] = "failed" if failed else "completed"
                    if failed:
                        message["error"] = error
                        if not message["text"]:
                            message["text"] = f"任务失败：{error}" if error else "任务失败"
            kind = "failed" if failed else "completed"
            self.hub.emit(WsAgentEvent(
                type="agent_event",
                payload={"event": {"type": f"agent_{kind}", "run_id": job.run_id,
                                   "error": error}},
            ))
            self.hub.record_milestone(f"agent_{kind}", {
                "run_id": job.run_id, "kind": job.kind, "error": error,
            })
            # Operator input the run never consumed is requeued as its own
            # job so a completed/failed run cannot silently drop it. A failed
            # run also requeues delivered-but-unprocessed feedback.
            remaining = [
                item for item in (
                    (*job.accepted_feedback, *job.feedback) if failed
                    else job.feedback
                )
            ]
            job.feedback = ()
            job.accepted_feedback = ()
            for item in remaining:
                retry = self.enqueue(
                    JOB_KIND_OPERATOR, item.text, source="feedback",
                )
                for message in self.messages:
                    if message.get("feedback_id") == item.id:
                        message.update(
                            run_id=retry.run_id, status="queued",
                            delivery_status="requeued",
                        )
            self._publish_state()

    # ------------------------------------------------------------------
    # Plans / approvals
    # ------------------------------------------------------------------
    def set_autonomy_mode(self, mode: str) -> str:
        if mode not in ("full", "assisted", "request"):
            raise ValueError("invalid_autonomy_mode")
        with self.lock:
            self.autonomy_mode = mode
            # Session grants die with the mode they were issued under.
            self._session_grants = []
            self.hub.emit(WsAgentEvent(
                type="autonomy_mode",
                payload={"mode": mode},
            ))
            self.hub.record_milestone("autonomy_mode_changed", {"mode": mode})
            self._publish_state()
            return self.autonomy_mode

    def _grant_covers(self, kind: str, domain: tuple | None) -> bool:
        """A session grant covers a new plan when kind and domain match."""
        return any(
            grant["kind"] == kind and grant["domain"] == (domain or ())
            for grant in self._session_grants
        )

    def submit_plan(
        self,
        job: AgentJob,
        *,
        kind: str,
        summary: dict,
        decision_reason: str,
        auto_approve: bool | None = None,
        trigger: str = "",
        reason: str = "",
        risk: float | None = None,
        members: tuple[str, ...] = (),
        execution_domain: tuple[float, float, float, float] | None = None,
        domain_policy: str = "",
        fallback: str = "",
        expires_in_s: float = 600.0,
    ) -> AgentPlanDTO:
        """Register a submitted plan; the caller decides auto-approval."""
        sim_now_s = self.sim_time_fn() * 60.0
        auto = self.autonomy_mode == "full" if auto_approve is None else auto_approve
        decided_by = "autonomy" if auto else ""
        if not auto and self._grant_covers(kind, execution_domain):
            auto = True
            decided_by = "session_grant"
        plan = AgentPlanDTO(
            plan_id=_identifier("plan"),
            run_id=job.run_id,
            job_id=job.run_id,
            episode_id=job.episode_id,
            kind=kind,
            status="auto_approved" if auto else "pending_approval",
            decision_reason=decision_reason,
            summary=summary,
            created_at_min=self.sim_time_fn(),
            decided_at_min=self.sim_time_fn() if auto else 0.0,
            decided_by=decided_by,
            trigger=trigger,
            reason=reason,
            risk=risk,
            members=members,
            execution_domain=execution_domain,
            domain_policy=domain_policy,
            fallback=fallback,
            approval_scope="approve_session" if decided_by == "session_grant" else "",
            created_at_s=sim_now_s,
            expires_at_s=sim_now_s + expires_in_s,
        )
        self.plans[plan.plan_id] = plan
        self.hub.emit(WsAgentEvent(type="plan_event", payload={"plan": plan.model_dump()}))
        self.hub.record_milestone("agent_plan_submitted", {
            "plan_id": plan.plan_id, "run_id": job.run_id,
            "status": plan.status, "decision_reason": decision_reason,
        })
        self._publish_state()
        return plan

    def _expire_plans(self) -> None:
        """Rejection/expiry is final — sweep pending plans past their window."""
        sim_now_s = self.sim_time_fn() * 60.0
        for plan_id, plan in list(self.plans.items()):
            if (plan.status == "pending_approval" and plan.expires_at_s is not None
                    and sim_now_s >= plan.expires_at_s):
                updated = plan.model_copy(update={
                    "status": "expired", "decided_at_min": self.sim_time_fn(),
                    "decided_by": "timeout",
                })
                self.plans[plan_id] = updated
                self.hub.emit(WsAgentEvent(type="plan_event", payload={"plan": updated.model_dump()}))

    def decide_plan(self, plan_id: str, decision: str = "approve_once", comment: str = "") -> AgentPlanDTO:
        """Human decision: approve_once | approve_session | reject | adjust."""
        if decision in ("approve", "approved"):
            decision = "approve_once"
        if decision not in ("approve_once", "approve_session", "reject", "adjust"):
            raise ValueError("invalid_decision")
        if decision == "adjust" and not str(comment or "").strip():
            raise ValueError("comment_required")
        with self.lock:
            self._expire_plans()
        plan = self.plans.get(plan_id)
        if plan is None:
            raise ValueError("plan_not_found")
        if plan.status != "pending_approval":
            raise ValueError("plan_not_pending")
        approved = decision in ("approve_once", "approve_session")
        scope = decision if decision != "reject" else ""
        self.plans[plan_id] = plan.model_copy(update={
            "status": "approved" if approved else "rejected",
            "decided_at_min": self.sim_time_fn(),
            "decided_by": "operator",
            "decision_comment": str(comment or "")[:500],
            "approval_scope": scope,
        })
        if decision == "approve_session":
            self._session_grants = [
                g for g in self._session_grants if g["kind"] != plan.kind
            ] + [{"kind": plan.kind, "domain": plan.execution_domain or ()}]
        updated = self.plans[plan_id]
        job = next((j for j in self.jobs if j.run_id == updated.run_id), None)
        if job is not None:
            job.runtime.result = {
                **(job.runtime.result or {}),
                "plan_id": plan_id,
                "plan_approved": approved,
            }
            if not approved:
                job.status = "failed"
                job.error = "plan_rejected"
                job.runtime.result = None
            job.runtime.done.set()
        self.hub.emit(WsAgentEvent(type="plan_event", payload={"plan": updated.model_dump()}))
        self.hub.record_milestone("agent_plan_decided", {
            "plan_id": plan_id, "approved": approved, "comment": comment,
        })
        self._publish_state()
        return updated

    def pending_plans(self) -> list[dict]:
        with self.lock:
            self._expire_plans()
        return [p.model_dump() for p in self.plans.values() if p.status == "pending_approval"]

    def plan_status(self, plan_id: str) -> dict | None:
        plan = self.plans.get(plan_id)
        return plan.model_dump() if plan else None

    def assignments(self) -> list[dict]:
        """Latest committed AssignmentView rows for the assignment map mode."""
        return list(self._assignment_view)

    def set_assignments(self, rows: list[dict]) -> None:
        self._assignment_view = list(rows)
        self.hub.emit(WsAgentEvent(
            type="assignment_view",
            payload={"assignments": list(rows)},
        ))

    # ------------------------------------------------------------------
    # State frame
    # ------------------------------------------------------------------
    def _publish_state(self) -> None:
        self.hub.emit(WsAgentEvent(
            type="agent_state",
            payload={
                "agent": {**self.agent, "autonomy_mode": self.autonomy_mode},
                "queued": sum(j.status == "queued" for j in self.jobs),
                "episode_id": self.episode_id_fn(),
                "plans": [p.model_dump() for p in self.plans.values()][-30:],
                "jobs": [j.model_dump(exclude={"payload"}) for j in self.jobs][-_MAX_JOBS:],
            },
        ))

    def state_frame(self) -> dict:
        with self.lock:
            self._expire_plans()
            return {
                "type": "agent_state",
                "agent": {**self.agent, "autonomy_mode": self.autonomy_mode},
                "queued": sum(j.status == "queued" for j in self.jobs),
                "episode_id": self.episode_id_fn(),
                "sim_time_min": self.sim_time_fn(),
                "messages": list(self.messages),
                "plans": [p.model_dump() for p in self.plans.values()][-30:],
                "jobs": [j.model_dump(exclude={"payload"}) for j in self.jobs][-_MAX_JOBS:],
                "assignments": list(self._assignment_view),
                # Recent tool/session/agent event payloads (unwrapped to the
                # inner event objects the timeline renders) so a freshly
                # connected client sees the same timeline as a live one.
                "events": [
                    f["event"] for f in self.hub.recent(120)
                    if f.get("type") in ("tool_event", "session_event", "agent_event")
                    and isinstance(f.get("event"), dict)
                ],
            }

    def status_payload(self) -> dict:
        with self.lock:
            return {
                **self.agent,
                "autonomy_mode": self.autonomy_mode,
                "worker_connected": (
                    self.last_heartbeat is not None
                    and time.monotonic() - self.last_heartbeat < WORKER_LIVENESS_SECONDS
                ),
                "queued": sum(j.status == "queued" for j in self.jobs),
                "episode_id": self.episode_id_fn(),
            }
