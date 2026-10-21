"""SchedulerPort/ToolsPort implementations over the scheduling pipeline.

``InProcessSchedulerPort`` reproduces the previous in-process behavior: the
selection job resolves synchronously through the shared LongCat gateway, and
the legacy region plan delegates to ``LLMClient.decide``. It exists so tests
and ``--no-agent`` runs keep a working decision authority.

``HarnessSchedulerPort`` posts every heavy decision to the PI worker as an
AgentJob and blocks the calling simulation thread until the worker submits a
validated selection, the operator resolves a pending plan approval, or the
deadline expires. Validation and matching never move out of
``MissionScheduler`` — the worker only proposes task IDs.

``MissionToolsPort`` is the tool surface the worker sees: a snapshot read, a
dry-run validator, a submit tool per job kind, and a status poller.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from adapter.broker import AgentBroker, AgentJob
from adapter.contracts import (
    SelectionPayloadDTO,
    ToolSpec,
    JOB_KIND_LEGACY_PLAN,
    JOB_KIND_OPERATOR,
    JOB_KIND_SELECTION,
)
from adapter.mission_adapter import snapshot_dto
from adapter.ports import SchedulerPort, ToolsPort
from src.mission.mission_scheduler import MissionScheduler, _selection_object
from src.mission.prompt_payload import encode_selection_payload

_LOGGER = logging.getLogger(__name__)

DEFAULT_AGENT_TIMEOUT_SECONDS = 240.0
_WORKER_BOOT_GRACE_SECONDS = 30.0


def _enum_strings(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


class InProcessSchedulerPort(SchedulerPort):
    """Resolves jobs locally through the existing model client."""

    def __init__(self, *, scheduler: MissionScheduler | None = None, llm_client=None) -> None:
        self.scheduler = scheduler
        self.llm_client = llm_client
        self._last_failure_category: str | None = None
        self._last_call_id: str | None = None
        self._pending_deadline: float | None = None

    @property
    def last_failure_category(self) -> str | None:
        return self._last_failure_category

    @property
    def last_call_id(self) -> str | None:
        return self._last_call_id

    def begin_decision(self, deadline_monotonic: float | None) -> None:
        self._pending_deadline = deadline_monotonic

    def provide_selection(self, snapshot, payload: dict) -> dict | None:
        """Same request the old gateway branch of ``decide`` issued."""
        scheduler = self.scheduler
        if scheduler is None:
            return None
        self._last_failure_category = None
        self._last_call_id = None
        visible_task_ids = frozenset(
            candidate.get("task_id")
            for candidate in (payload.get("snapshot", {}) or {}).get("candidates", ())
            if isinstance(candidate, dict) and candidate.get("task_id")
        )
        deadline = self._pending_deadline
        if deadline is None:
            deadline = time.perf_counter() + scheduler.planning_deadline_seconds
        result = scheduler.gateway.request_json(
            role="decision_maker",
            snapshot_id=snapshot.snapshot_id,
            system_prompt=scheduler.system_prompt,
            user_payload=encode_selection_payload(payload),
            validate=lambda candidate: (
                scheduler._selection_schema_errors(candidate)
                or scheduler.validate_selection(
                    candidate, snapshot, visible_task_ids=visible_task_ids,
                )
            ),
            post_validate=lambda candidate: scheduler.validate_selection(
                candidate, snapshot, visible_task_ids=visible_task_ids,
            ),
            deadline_monotonic=deadline,
            transport_deadline_monotonic=deadline - scheduler.postprocess_reserve_seconds,
        )
        self._last_call_id = result.call_id
        self._last_failure_category = result.failure_category
        return result.payload if result.success else None

    def decide_regions(self, sm, ivt, candidate_result, required_search_regions: int) -> dict:
        if self.llm_client is None:
            return {"search_regions": [], "notes": "LLM failed after max retries"}
        return self.llm_client.decide(
            sm, ivt, candidate_result,
            required_search_regions=required_search_regions,
        )


class HarnessSchedulerPort(SchedulerPort):
    """Posts heavy decisions to the PI worker and awaits its submission."""

    def __init__(
        self,
        broker: AgentBroker,
        scheduler: MissionScheduler,
        *,
        llm_client=None,
        timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS,
        agent_md_path: str | os.PathLike | None = None,
    ) -> None:
        self.broker = broker
        self.scheduler = scheduler
        self.llm_client = llm_client
        self.agent_md_path = (
            Path(agent_md_path) if agent_md_path
            else Path(__file__).resolve().parent.parent / "src" / "schedule" / "AGENT.md"
        )
        self.timeout_seconds = max(30.0, float(timeout_seconds))
        self._last_failure_category: str | None = None
        self._pending_deadline: float | None = None
        self._pending_trigger: str = ""

    @property
    def last_failure_category(self) -> str | None:
        return self._last_failure_category

    @property
    def deadline_budget_seconds(self) -> float:
        return self.timeout_seconds

    def begin_decision(self, deadline_monotonic: float | None, trigger: str = "") -> None:
        self._pending_deadline = deadline_monotonic
        self._pending_trigger = trigger

    def _await(self, job: AgentJob, deadline_monotonic: float) -> dict | None:
        """Block until the job resolves or the deadline expires."""
        started = time.monotonic()
        while True:
            remaining = deadline_monotonic - time.monotonic()
            if remaining <= 0:
                self.broker.cancel(job.run_id)
                return None
            if job.runtime.done.wait(min(2.0, remaining)):
                return job.runtime.result
            if (
                job.status == "queued"
                and time.monotonic() - started > _WORKER_BOOT_GRACE_SECONDS
                and not self.broker.worker_alive()
            ):
                self.broker.cancel(job.run_id)
                return None

    def _deadline(self) -> float:
        deadline = time.monotonic() + self.timeout_seconds
        if self._pending_deadline is not None:
            deadline = min(deadline, self._pending_deadline)
        return deadline

    # The job carries only the delta (why it woke); the agent pulls the
    # frozen snapshot back on demand via get_mission_snapshot.
    external = True

    def _system_prompt(self, base: str) -> str:
        """Append the operator-maintained AGENT.md style guide (live re-read)."""
        try:
            guide = self.agent_md_path.read_text(encoding="utf-8").strip()
        except OSError:
            return base
        if not guide:
            return base
        return f"{base}\n\n# 人工风格指导（AGENT.md）\n\n{guide}"

    def provide_selection(self, snapshot, payload: dict) -> dict | None:
        """Enqueue the mission-selection job; None on failure or timeout."""
        self._last_failure_category = None
        deadline = self._deadline()
        try:
            dto = snapshot_dto(snapshot, episode_id=self.broker.episode_id_fn())
        except Exception:
            _LOGGER.exception("mission snapshot DTO conversion failed")
            self._last_failure_category = "validation"
            return None
        delta = {
            "snapshot_id": snapshot.snapshot_id,
            "sim_time_min": snapshot.sim_time_min,
            "candidates": len(payload.get("snapshot", {}).get("candidates", ())),
            "pending_search_task_ids": list(
                payload.get("snapshot", {}).get("pending_search_task_ids", ())
            ),
        }
        job = self.broker.enqueue(
            JOB_KIND_SELECTION,
            "重量触发：为当前态势做出任务选择（mission-selection/v1）",
            source="scheduler",
            payload={
                "system_prompt": self._system_prompt(self.scheduler.system_prompt),
                "delta": delta,
            },
            deadline_monotonic=deadline,
            trigger=self._pending_trigger,
        )
        job.runtime.internal_snapshot = snapshot
        job.runtime.selection_payload = payload
        job.runtime.snapshot_dto = dto.model_dump(mode="json")
        result = self._await(job, deadline)
        if result is None or not isinstance(result.get("selection"), dict):
            self._last_failure_category = (
                "timeout" if job.status in ("queued", "running", "pending_approval")
                else "transport"
            )
            return None
        return result["selection"]

    def decide_regions(self, sm, ivt, candidate_result, required_search_regions: int) -> dict:
        """Legacy heavy path, delivered through the same job pipeline."""
        if self.llm_client is None:
            return {"search_regions": [], "notes": "LLM failed after max retries"}
        system_prompt, user_prompt, validate_plan = self.llm_client.prepare_decision(
            sm, ivt, candidate_result,
            required_search_regions=required_search_regions,
        )
        deadline = self._deadline()
        job = self.broker.enqueue(
            JOB_KIND_LEGACY_PLAN,
            "搜索区规划：按态势输出 search_regions 方案",
            source="scheduler",
            payload={"system_prompt": self._system_prompt(system_prompt), "user_prompt": user_prompt},
            validators={"plan": validate_plan},
            deadline_monotonic=deadline,
            trigger=self._pending_trigger,
        )
        result = self._await(job, deadline)
        success = isinstance(result, dict) and isinstance(result.get("search_regions"), list)
        payload = (
            {"search_regions": result["search_regions"], "notes": str(result.get("notes", ""))}
            if success
            else {"search_regions": [], "notes": "LLM failed after max retries"}
        )
        self.llm_client.last_interaction = {
            "success": success,
            "model": "pi-agent",
            "call_id": job.run_id,
            "attempts": [],
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "response": json.dumps(payload, ensure_ascii=False) if success else "",
            "validation": {"is_valid": success, "errors": []},
        }
        return payload


def _mission_tool_catalog() -> tuple[ToolSpec, ...]:
    ids_schema = {
        "type": "array", "items": {"type": "string", "minLength": 1},
        "maxItems": 64, "uniqueItems": True,
    }
    selection_schema = {
        "type": "object",
        "properties": {
            "schema_version": {"const": "mission-selection/v1"},
            "snapshot_id": {"type": "string", "minLength": 1},
            "selected_task_ids": ids_schema,
            "preempt_uav_ids": ids_schema,
            "defer_reason": {"type": ["string", "null"]},
            "notes": {"type": "string", "maxLength": 160},
            "information_version": {"type": "integer", "minimum": 0},
        },
        "required": ["schema_version", "snapshot_id", "selected_task_ids",
                     "preempt_uav_ids", "defer_reason", "notes"],
        "additionalProperties": False,
    }
    return (
        ToolSpec(
            name="get_mission_snapshot",
            label="获取任务态势",
            description=(
                "Return the current scheduling job's full input: the system "
                "prompt, the mission-selection/v1 payload (snapshot, "
                "candidates, feasible edges, coverage constraint) and the "
                "MissionSnapshot DTO. Always call this first; job payloads "
                "carry only the wake delta, situation lives here."
            ),
            snippet="Fetch this job's selection payload",
            parameters={"type": "object", "properties": {}, "additionalProperties": False},
        ),
        ToolSpec(
            name="validate_selection",
            label="校验任务选择",
            description=(
                "Dry-run a mission-selection/v1 payload through the exact "
                "validators and deterministic matching the committed path "
                "uses. Returns {valid, errors, matched}; nothing is applied."
            ),
            snippet="Validate a candidate selection before submitting",
            parameters={
                "type": "object",
                "properties": {"selection": selection_schema},
                "required": ["selection"],
                "additionalProperties": False,
            },
        ),
        ToolSpec(
            name="submit_selection",
            label="提交任务选择",
            description=(
                "Submit the final mission-selection/v1 payload. Requires a "
                "non-empty public decision_reason (<=500 chars) explaining "
                "the scheduling choice in Chinese. On success the selection "
                "is committed after operator approval when the autonomy mode "
                "requires it. Fails with structured errors when invalid."
            ),
            snippet="Submit the final selection",
            guidelines=[
                "decision_reason must describe the actual choice and the UAVs involved; never fabricate it.",
                "If the tool returns status=invalid_selection, fix the reported errors and resubmit; never resubmit an identical payload.",
            ],
            parameters={
                "type": "object",
                "properties": {
                    "selection": selection_schema,
                    "decision_reason": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["selection", "decision_reason"],
                "additionalProperties": False,
            },
            execution_mode="sequential",
        ),
        ToolSpec(
            name="submit_plan_regions",
            label="提交搜索区规划",
            description=(
                "Legacy planner job only: submit {search_regions, notes} for "
                "the legacy-mission-plan/v1 prompt. decision_reason required."
            ),
            snippet="Submit legacy region plan",
            parameters={
                "type": "object",
                "properties": {
                    "search_regions": {"type": "array", "items": {"type": "object"}, "maxItems": 24},
                    "notes": {"type": "string", "maxLength": 400},
                    "decision_reason": {"type": "string", "minLength": 1, "maxLength": 500},
                },
                "required": ["search_regions", "notes", "decision_reason"],
                "additionalProperties": False,
            },
            execution_mode="sequential",
        ),
        ToolSpec(
            name="get_action_status",
            label="查询审批状态",
            description=(
                "Poll a plan or run status by action_id (plan_id or run_id). "
                "Use after submit_selection returns pending_approval."
            ),
            snippet="Poll approval status",
            parameters={
                "type": "object",
                "properties": {"action_id": {"type": "string", "minLength": 1}},
                "required": ["action_id"],
                "additionalProperties": False,
            },
        ),
    )


class MissionToolsPort(ToolsPort):
    """Dispatches worker tool calls against the live scheduler state."""

    def __init__(
        self,
        broker: AgentBroker,
        scheduler: MissionScheduler,
    ) -> None:
        self.broker = broker
        self.scheduler = scheduler
        self._catalog = _mission_tool_catalog()

    def catalog(self) -> tuple[ToolSpec, ...]:
        return self._catalog

    def invoke(self, name: str, params: dict, job: AgentJob) -> dict:
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            raise ValueError("unknown_tool")
        return handler(params, job)

    # ------------------------------------------------------------------
    def _tool_get_mission_snapshot(self, params: dict, job: AgentJob) -> dict:
        del params
        payload = job.payload
        if job.kind == JOB_KIND_LEGACY_PLAN:
            return {
                "episode_id": job.episode_id,
                "run_id": job.run_id,
                "kind": job.kind,
                "system_prompt": payload.get("system_prompt", ""),
                "user_prompt": payload.get("user_prompt", ""),
            }
        return {
            "episode_id": job.episode_id,
            "run_id": job.run_id,
            "kind": job.kind,
            "system_prompt": payload.get("system_prompt", ""),
            "delta": payload.get("delta", {}),
            "snapshot": job.runtime.snapshot_dto or {},
            "selection_payload": encode_selection_payload(
                job.runtime.selection_payload or {}
            ),
        }

    def _selection_errors(self, selection: dict, job: AgentJob) -> tuple[str, ...]:
        """Full dry-run validation against the job's frozen snapshot."""
        snapshot = job.runtime.internal_snapshot
        prompt = job.runtime.selection_payload or {}
        visible_task_ids = frozenset(
            candidate.get("task_id")
            for candidate in (prompt.get("snapshot", {}) or {}).get("candidates", ())
            if isinstance(candidate, dict) and candidate.get("task_id")
        )
        parsed, schema_errors = _selection_object(selection)
        if schema_errors:
            return tuple(schema_errors)
        if parsed is None:
            return ("selection_parse_failed",)
        if snapshot is None:
            return ("snapshot_unavailable",)
        if parsed.snapshot_id != snapshot.snapshot_id:
            return ("snapshot_mismatch",)
        errors = self.scheduler.validate_selection(
            selection, snapshot, visible_task_ids=visible_task_ids,
        )
        if errors:
            return tuple(errors)
        pairing = self.scheduler.pair_selected_tasks(
            parsed, snapshot, visible_task_ids=visible_task_ids,
        )
        if not pairing.is_valid:
            return tuple(pairing.errors)
        return ()

    def _tool_validate_selection(self, params: dict, job: AgentJob) -> dict:
        if job.kind != JOB_KIND_SELECTION:
            raise ValueError("wrong_job_kind")
        selection = params.get("selection")
        if not isinstance(selection, dict):
            raise ValueError("invalid_selection")
        errors = self._selection_errors(selection, job)
        snapshot = getattr(job.runtime, "internal_snapshot", None)
        matched: list[dict] = []
        if not errors and snapshot is not None:
            parsed, _ = _selection_object(selection)
            pairing = self.scheduler.pair_selected_tasks(
                parsed, snapshot,
                visible_task_ids=frozenset(
                    c.get("task_id")
                    for c in (job.runtime.selection_payload or {})
                    .get("snapshot", {}).get("candidates", ())
                    if isinstance(c, dict) and c.get("task_id")
                ),
            )
            if pairing.is_valid:
                matched = [
                    {"task_id": a.task_id, "uav_id": a.uav_id}
                    for a in pairing.assignments
                ]
        return {"valid": not errors, "errors": list(errors), "matched": matched}

    def _tool_submit_selection(self, params: dict, job: AgentJob) -> dict:
        if job.kind != JOB_KIND_SELECTION:
            raise ValueError("wrong_job_kind")
        selection = params.get("selection")
        if not isinstance(selection, dict):
            raise ValueError("invalid_selection")
        reason = str(params.get("decision_reason") or "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("decision_reason_required")
        errors = self._selection_errors(selection, job)
        if errors:
            return {"status": "invalid_selection", "errors": list(errors)}
        risky = self._is_risky(selection, job)
        auto = (
            self.broker.autonomy_mode == "full"
            or (self.broker.autonomy_mode == "assisted" and not risky)
        )
        domain = self._assignment_domain(selection, job)
        plan = self.broker.submit_plan(
            job,
            kind=job.kind,
            summary={
                "selected_task_ids": list(selection.get("selected_task_ids") or ()),
                "preempt_uav_ids": list(selection.get("preempt_uav_ids") or ()),
                "assignments": domain["assignments"],
                "notes": selection.get("notes", ""),
                "snapshot_id": selection.get("snapshot_id"),
                "risky": risky,
            },
            decision_reason=reason,
            auto_approve=auto,
            trigger=job.trigger,
            reason="risk_threshold" if risky else "human_required",
            risk=0.75 if risky else 0.2,
            members=domain["members"],
            execution_domain=domain["domain"],
            domain_policy=(
                f"选中 {len(selection.get('selected_task_ids') or ())} 个任务"
                f" / 抢占 {len(selection.get('preempt_uav_ids') or ())} 架 UAV"
            ),
            fallback="审批拒绝或过期后维持现有任务分配，不执行本计划",
        )
        if auto:
            self._publish_assignments(selection, job)
        # The job stays running until the worker posts its completed/failed
        # event; the port resolves from job.runtime.result at that point.
        job.runtime.result = {"selection": selection, "plan_id": plan.plan_id}
        if plan.status == "auto_approved":
            return {
                "status": "approved",
                "plan_id": plan.plan_id,
                "auto_approved": True,
            }
        return {"status": "pending_approval", "plan_id": plan.plan_id}

    def _tool_submit_plan_regions(self, params: dict, job: AgentJob) -> dict:
        if job.kind != JOB_KIND_LEGACY_PLAN:
            raise ValueError("wrong_job_kind")
        reason = str(params.get("decision_reason") or "").strip()
        if not reason or len(reason) > 500:
            raise ValueError("decision_reason_required")
        payload = {
            "search_regions": params.get("search_regions") or [],
            "notes": str(params.get("notes") or ""),
        }
        validator = job.runtime.validators.get("plan")
        if validator is not None:
            errors = validator(payload)
            if errors:
                return {"status": "invalid_plan", "errors": list(errors)}
        auto = self.broker.autonomy_mode == "full" or (
            self.broker.autonomy_mode == "assisted"
            and len(payload["search_regions"]) <= 8
        )
        regions_domain = self._regions_domain(payload["search_regions"])
        plan = self.broker.submit_plan(
            job,
            kind=job.kind,
            summary={"search_region_count": len(payload["search_regions"]),
                     "notes": payload["notes"]},
            decision_reason=reason,
            auto_approve=auto,
            trigger=job.trigger,
            reason="risk_threshold" if len(payload["search_regions"]) > 8 else "human_required",
            risk=0.6 if len(payload["search_regions"]) > 8 else 0.2,
            members=(),
            execution_domain=regions_domain,
            domain_policy=f"新规划 {len(payload['search_regions'])} 个搜索子区域",
            fallback="审批拒绝或过期后沿用既有搜索区域划分",
        )
        result = {**payload, "plan_id": plan.plan_id}
        job.runtime.result = result
        if plan.status == "auto_approved":
            return {"status": "approved", "plan_id": plan.plan_id, "auto_approved": True}
        return {"status": "pending_approval", "plan_id": plan.plan_id}

    # --- autonomy-mode risk split & assignment views ---------------------
    _ROUTINE_KINDS = {"search", "coverage"}

    def _is_risky(self, selection: dict, job: AgentJob) -> bool:
        """assisted 模式的高风险定义：抢占在飞任务或非例行任务类型。"""
        if selection.get("preempt_uav_ids"):
            return True
        kinds = {
            c.get("kind"): c.get("task_id")
            for c in (job.runtime.selection_payload or {})
            .get("snapshot", {}).get("candidates", ())
            if isinstance(c, dict)
        }
        task_kind = {
            c.get("task_id"): c.get("kind")
            for c in (job.runtime.selection_payload or {})
            .get("snapshot", {}).get("candidates", ())
            if isinstance(c, dict) and c.get("task_id")
        }
        del kinds
        return any(
            task_kind.get(task_id) not in self._ROUTINE_KINDS
            for task_id in selection.get("selected_task_ids") or ()
        )

    def _selection_rows(self, selection: dict, job: AgentJob) -> list[dict]:
        """Deterministic pairing -> AssignmentView-shaped rows."""
        snapshot = job.runtime.internal_snapshot
        if snapshot is None:
            return []
        parsed, _ = _selection_object(selection)
        if parsed is None:
            return []
        visible = frozenset(
            c.get("task_id")
            for c in (job.runtime.selection_payload or {})
            .get("snapshot", {}).get("candidates", ())
            if isinstance(c, dict) and c.get("task_id")
        )
        pairing = self.scheduler.pair_selected_tasks(parsed, snapshot,
                                                     visible_task_ids=visible)
        if not pairing.is_valid:
            return []
        candidates = {
            c.task_id: c for c in getattr(snapshot, "candidates", ())
        }
        rows = []
        for assignment in pairing.assignments:
            candidate = candidates.get(assignment.task_id)
            bbox = getattr(candidate, "bbox", None)
            center = getattr(candidate, "center", None)
            rows.append({
                "task_id": assignment.task_id,
                "uav_id": assignment.uav_id,
                "kind": getattr(candidate, "kind", "") or "",
                "label": getattr(candidate, "label", "") or assignment.task_id,
                "bbox": list(bbox) if bbox else None,
                "center": list(center) if center else None,
                "target_id": getattr(candidate, "contact_id", None),
            })
        return rows

    def _assignment_domain(self, selection: dict, job: AgentJob) -> dict:
        """Plan members (assigned UAVs) + merged bbox of all touched areas."""
        rows = self._selection_rows(selection, job)
        boxes = [tuple(r["bbox"]) for r in rows if r.get("bbox")]
        centers = [tuple(r["center"]) for r in rows if r.get("center")]
        domain = None
        if boxes or centers:
            xs1 = [b[0] for b in boxes] + [c[0] for c in centers]
            ys1 = [b[1] for b in boxes] + [c[1] for c in centers]
            xs2 = [b[2] for b in boxes] + [c[0] for c in centers]
            ys2 = [b[3] for b in boxes] + [c[1] for c in centers]
            domain = (min(xs1), min(ys1), max(xs2), max(ys2))
        members = tuple(sorted({r["uav_id"] for r in rows if r.get("uav_id")}))
        kind_labels = {
            "probe": "查证接触", "investigate": "查证接触", "track": "持续跟踪",
            "search": "区域搜索", "coverage": "区域覆盖", "partition": "分区覆盖",
        }
        assignments = []
        for row in rows:
            target = row.get("target_id")
            bbox = row.get("bbox")
            kind_cn = kind_labels.get(row.get("kind"), "执行任务")
            if target:
                desc = f"查证接触 {target}"
            elif bbox:
                x1, y1, x2, y2 = (int(v) for v in bbox)
                desc = f"{kind_cn} ({x1},{y1})–({x2},{y2})"
            else:
                desc = f"{kind_cn} {row['task_id']}"
            assignments.append({
                "uav_id": row.get("uav_id"), "task_id": row.get("task_id"),
                "kind": row.get("kind"), "desc": desc,
            })
        return {"members": members, "domain": domain, "assignments": assignments}

    @staticmethod
    def _regions_domain(search_regions: list) -> tuple | None:
        boxes = [
            tuple(r["bbox"]) for r in search_regions
            if isinstance(r, dict) and isinstance(r.get("bbox"), (list, tuple))
            and len(r["bbox"]) == 4
        ]
        if not boxes:
            return None
        return (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )

    def _publish_assignments(self, selection: dict, job: AgentJob) -> None:
        """Project the committed selection into AssignmentView rows."""
        rows = self._selection_rows(selection, job)
        if rows:
            self.broker.set_assignments(rows)

    def _tool_get_action_status(self, params: dict, job: AgentJob) -> dict:
        action_id = str(params.get("action_id") or "")
        plan = self.broker.plan_status(action_id)
        if plan is not None:
            return {"status": plan["status"], "plan_id": action_id}
        target = next((j for j in self.broker.jobs if j.run_id == action_id), None)
        if target is not None:
            return {"status": target.status, "run_id": action_id}
        raise ValueError("action_not_found")
