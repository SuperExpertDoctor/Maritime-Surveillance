"""FastAPI adapter surface between the PI worker, the sim and the frontend.

Routes (mounted on the existing vis backend app):

Worker-facing (``Authorization: Bearer <ADAPTER_WORKER_TOKEN>`` when set):
- GET  /internal/agent/tools          — ToolSpec catalog
- POST /internal/agent/jobs           — poll next job ({action:"next"})
- GET  /internal/agent/jobs           — job list for diagnostics
- POST /internal/agent/heartbeat      — lease renewal + feedback delivery
- POST /internal/agent/event          — session_event | completed | failed
- POST /internal/tools/{name}         — dispatch a declared tool

Operator-facing (frontend LeftSidebar):
- GET  /api/pi-agent/status
- GET  /api/pi-agent/messages
- POST /api/pi-agent/messages         — {text, delivery: steer|followUp}
- GET  /api/pi-agent/plans            — pending plan approvals
- POST /api/pi-agent/plans/{id}/decision — {decision: approve|reject, comment}
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from adapter.broker import AgentBroker
from adapter.contracts import WsAgentEvent
from adapter.ports import ToolsPort

_LOGGER = logging.getLogger(__name__)


def _error(code: str, status: int = 422, **extra: Any) -> JSONResponse:
    return JSONResponse({"error_code": code, "message": code, **extra},
                        status_code=status)


def build_agent_router(
    broker: AgentBroker,
    tools: ToolsPort,
    *,
    worker_token: str = "",
    skills_dir: str | os.PathLike = "harness/agent/skills",
    agent_md_path: str | os.PathLike | None = None,
) -> APIRouter:
    """Create the adapter router; all state lives in ``broker``."""
    router = APIRouter()

    def authorized(request: Request) -> bool:
        if not worker_token:
            return True
        provided = request.headers.get("authorization", "")
        return secrets.compare_digest(provided, f"Bearer {worker_token}")

    async def _json(request: Request) -> dict | JSONResponse:
        try:
            data = await request.json()
        except Exception:
            return _error("object_required")
        if not isinstance(data, dict):
            return _error("object_required")
        return data

    # --------------------------------------------------------------
    # Worker-facing endpoints
    # --------------------------------------------------------------
    @router.get("/internal/agent/tools")
    async def agent_tools(request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        return {"tools": [spec.model_dump() for spec in tools.catalog()]}

    @router.post("/internal/agent/jobs")
    @router.post("/internal/agent/next")
    async def agent_next(request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        defer_routine = data.get("defer_routine", False)
        if not isinstance(defer_routine, bool):
            return _error("boolean_required")
        job = broker.claim_next(defer_routine)
        return {
            "job": job.model_dump() if job else None,
            "episode_id": broker.episode_id_fn(),
        }

    @router.get("/internal/agent/jobs")
    async def agent_jobs(request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        return {"jobs": [j.model_dump(exclude={"payload"}) for j in broker.jobs]}

    @router.post("/internal/agent/heartbeat")
    async def agent_heartbeat(request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        acknowledged = data.get("acknowledged_feedback_ids", [])
        if (not isinstance(acknowledged, list) or len(acknowledged) > 20
                or not all(isinstance(v, str) and len(v) <= 160 for v in acknowledged)):
            return _error("invalid_feedback_acknowledgment")
        return broker.heartbeat(
            str(data.get("run_id") or ""),
            str(data.get("episode_id") or ""),
            acknowledged,
            model=str(data.get("model") or ""),
        )

    @router.post("/internal/agent/event")
    async def agent_event(request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        try:
            job = broker.active_run(
                str(data.get("run_id") or ""),
                str(data.get("episode_id") or ""),
            )
        except ValueError as exc:
            return _error(str(exc), 409)
        kind = data.get("type")
        if kind == "session_event":
            event = data.get("event")
            if not isinstance(event, dict):
                return _error("invalid_session_event")
            try:
                broker.hub.session_event(job.run_id, broker, event)
            except ValueError as exc:
                return _error(str(exc))
            return {"status": "ok"}
        if kind not in ("completed", "failed"):
            return _error("invalid_agent_event")
        planned = any(
            plan.run_id == job.run_id and plan.plan_id
            for plan in broker.plans.values()
        )
        reason = data.get("decision_reason")
        if (kind == "completed" and not planned
                and (not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500)):
            return _error("decision_reason_required")
        text = str(data.get("text", ""))[:8000]
        if kind == "completed" and not any(
            m.get("run_id") == job.run_id and m.get("role") == "assistant"
            for m in broker.messages
        ):
            message = broker.upsert_message(
                f"message-{job.run_id}", role="assistant", text=text,
                run_id=job.run_id, status="completed",
            )
            broker.hub.emit(WsAgentEvent(
                type="agent_message", payload={"message": message}))
        if kind == "completed":
            for message in broker.messages:
                if (message.get("feedback_id") in {f.id for f in job.accepted_feedback}
                        and message.get("run_id") == job.run_id):
                    message.update(status="completed", delivery_status="processed")
        broker.complete(
            job, job.runtime.result,
            failed=kind == "failed",
            error=str(data.get("error") or "")[:300] or None,
        )
        if kind == "completed" and not planned:
            broker.hub.record_milestone("agent_decision_recorded", {
                "run_id": job.run_id, "decision_reason": str(reason).strip(),
            })
        return {"status": "ok"}

    @router.post("/internal/tools/{name}")
    async def invoke_tool(name: str, request: Request):
        if not authorized(request):
            return _error("worker_auth_required", 403)
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        try:
            job = broker.active_run(
                str(data.get("run_id") or ""),
                str(data.get("episode_id") or ""),
            )
        except ValueError as exc:
            return _error(str(exc), 409)
        params = {
            key: value for key, value in data.items()
            if key not in ("run_id", "episode_id")
        }
        broker.hub.record_milestone("tool_started", {
            "tool": name, "run_id": job.run_id,
        })
        try:
            result = tools.invoke(name, params, job)
        except ValueError as exc:
            broker.hub.record_milestone("tool_failed", {
                "tool": name, "run_id": job.run_id, "error_code": str(exc),
            })
            return _error(str(exc))
        except Exception:
            _LOGGER.exception("tool %s failed", name)
            broker.hub.record_milestone("tool_failed", {
                "tool": name, "run_id": job.run_id, "error_code": "tool_error",
            })
            return _error("tool_error", 500)
        broker.hub.record_milestone("tool_completed", {
            "tool": name, "run_id": job.run_id,
            "status": result.get("status", "succeeded"),
        })
        return result

    # --------------------------------------------------------------
    # Operator-facing endpoints (LeftSidebar)
    # --------------------------------------------------------------
    @router.get("/api/pi-agent/status")
    async def agent_status():
        return broker.status_payload()

    @router.get("/api/pi-agent/messages")
    async def agent_messages():
        with broker.lock:
            return {"messages": list(broker.messages)}

    @router.post("/api/pi-agent/messages")
    async def post_message(request: Request):
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        text = data.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            return _error("invalid_message")
        delivery = data.get("delivery", "followUp")
        annotation = data.get("annotation")
        if annotation is not None:
            if (not isinstance(annotation, dict)
                    or not isinstance(annotation.get("message_id"), str)
                    or not isinstance(annotation.get("quote"), str)
                    or len(annotation["quote"]) > 2000):
                return _error("invalid_annotation")
            annotation = {
                "message_id": annotation["message_id"][:160],
                "quote": annotation["quote"][:2000],
            }
        try:
            job = broker.queue_human(text, delivery, annotation)
        except ValueError as exc:
            return _error(str(exc))
        return {"run_id": job.run_id, "status": job.status}

    # --------------------------------------------------------------
    # Skills: distilled SKILL.md files the PI worker loads at start;
    # operators may inspect and hand-edit them from the skills panel.
    # --------------------------------------------------------------
    _SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")

    # AGENT.md lives in the algorithm dir and styles every scheduling
    # decision; surface it in the skills view as a first-class editable doc.
    _agent_md = Path(agent_md_path) if agent_md_path else None
    AGENT_DOC_NAME = "AGENT.md"

    def _skill_file(name: str) -> Path | None:
        if name == AGENT_DOC_NAME and _agent_md is not None:
            return _agent_md
        if not _SKILL_NAME.match(name):
            return None
        root = Path(skills_dir).resolve()
        target = (root / name / "SKILL.md").resolve()
        return target if root in target.parents else None

    def _skill_description(text: str) -> str:
        match = re.search(r'^description:\s*["\']?(.*?)["\']?\s*$', text, re.M)
        return match.group(1)[:200] if match else ""

    @router.get("/api/skills")
    async def list_skills():
        root = Path(skills_dir)
        skills = []
        if _agent_md is not None and _agent_md.is_file():
            text = _agent_md.read_text(encoding="utf-8", errors="replace")
            skills.append({
                "name": AGENT_DOC_NAME,
                "description": "任务分配风格指导（人工维护，注入每次调度决策）",
                "updated_at": _agent_md.stat().st_mtime,
            })
        if root.is_dir():
            for child in sorted(root.iterdir()):
                doc = child / "SKILL.md"
                if child.is_dir() and doc.is_file():
                    text = doc.read_text(encoding="utf-8", errors="replace")
                    skills.append({
                        "name": child.name,
                        "description": _skill_description(text),
                        "updated_at": doc.stat().st_mtime,
                    })
        return {"skills": skills}

    @router.get("/api/skills/{name}")
    async def get_skill(name: str):
        target = _skill_file(name)
        if target is None or not target.is_file():
            return _error("skill_not_found", 404)
        return {"name": name, "content": target.read_text(encoding="utf-8", errors="replace")}

    @router.put("/api/skills/{name}")
    async def save_skill(name: str, request: Request):
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        content = data.get("content")
        if not isinstance(content, str) or not content.strip() or len(content) > 200_000:
            return _error("invalid_skill_content")
        target = _skill_file(name)
        if target is None:
            return _error("skill_not_found", 404)
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"name": name, "saved": True}

    @router.get("/api/pi-agent/plans")
    @router.get("/api/plans")
    async def pending_plans():
        return {"plans": broker.pending_plans()}

    @router.post("/api/pi-agent/jobs/{run_id}/cancel")
    async def cancel_job(run_id: str):
        cancelled = broker.cancel(run_id)
        if not cancelled:
            return _error("job_not_cancellable", 409)
        return {"cancelled": cancelled}

    @router.post("/api/plans/{plan_id}/approve")
    async def approve_plan(plan_id: str, request: Request):
        return await _decide(plan_id, request, approved=True)

    @router.post("/api/plans/{plan_id}/reject")
    async def reject_plan(plan_id: str, request: Request):
        return await _decide(plan_id, request, approved=False)

    async def _decide(plan_id: str, request: Request, *, approved: bool):
        data = await _json(request)
        if isinstance(data, JSONResponse):
            data = {}
        try:
            plan = broker.decide_plan(
                plan_id,
                decision="approve_once" if approved else "reject",
                comment=str(data.get("comment") or "") if isinstance(data, dict) else "",
            )
        except ValueError as exc:
            return _error(str(exc), 409)
        return {"plan": plan.model_dump()}

    @router.get("/api/autonomy-mode")
    async def get_mode():
        return {"mode": broker.autonomy_mode}

    @router.post("/api/autonomy-mode")
    async def set_mode(request: Request):
        data = await _json(request)
        if not isinstance(data, dict):
            return data
        try:
            mode = broker.set_autonomy_mode(str(data.get("mode") or ""))
        except ValueError as exc:
            return _error(str(exc))
        return {"mode": mode}

    @router.get("/api/pi-agent/assignments")
    async def assignments():
        return {"assignments": broker.assignments()}

    @router.post("/api/pi-agent/plans/{plan_id}/decision")
    async def decide_plan(plan_id: str, request: Request):
        data = await _json(request)
        if isinstance(data, JSONResponse):
            return data
        if data.get("decision") not in (
                "approve", "approve_once", "approve_session", "reject", "adjust"):
            return _error("invalid_decision")
        try:
            plan = broker.decide_plan(
                plan_id,
                decision=data["decision"],
                comment=str(data.get("comment") or ""),
            )
        except ValueError as exc:
            return _error(str(exc), 409)
        return {"plan": plan.model_dump()}

    @router.get("/api/pi-agent/state")
    async def agent_state():
        return broker.state_frame()

    return router
