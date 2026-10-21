"""Decision-evidence logging + periodic skill refinement.

Closes the skills self-evolution loop: every scheduling decision that
resolves — a committed plan, a rejected/expired plan, or a failed
agent run — appends a ``mission_decision`` record (plus a
``coverage_update`` sample when the snapshot carries zone coverage) to
``outputs/skills_evidence/<episode_id>.jsonl``.

After ``refine_every`` successful decisions the refinery runs
``src.schedule.skill_extractor.extract_skills`` over the evidence
directory, regenerating ``SKILL.md`` files under the agent skills root.
The worker re-scans that directory per job, so refined skills enter
``<available_skills>`` without a restart.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_EVIDENCE_SUBDIR = Path("outputs") / "skills_evidence"
DEFAULT_REFINE_EVERY = 5


def _coverage_pct(job) -> float | None:
    """Weighted seen-and-fresh fraction from the job's snapshot zones."""
    snapshot = getattr(getattr(job, "runtime", None), "internal_snapshot", None)
    summary = getattr(snapshot, "coverage_summary", None) or {}
    zones = summary.get("zones") or ()
    fixed = sum(int(z.get("fixed_cells") or 0) for z in zones)
    if not fixed:
        return None
    gap = sum(
        int(z.get("unseen_cells") or 0) + int(z.get("overdue_cells") or 0)
        for z in zones
    )
    return round(100.0 * (1.0 - gap / fixed), 2)


def _task_kinds(plan) -> list[str]:
    summary = getattr(plan, "summary", None) or {}
    kinds = {
        str(row.get("kind"))
        for row in summary.get("assignments") or ()
        if isinstance(row, dict) and row.get("kind")
    }
    return sorted(kinds)


class SkillRefinery:
    """Append-only evidence writer with a success-count refine trigger."""

    def __init__(
        self,
        evidence_dir: str | Path,
        skills_dir: str | Path,
        *,
        refine_every: int = DEFAULT_REFINE_EVERY,
    ) -> None:
        self.evidence_dir = Path(evidence_dir)
        self.skills_dir = Path(skills_dir)
        self.refine_every = max(1, int(refine_every))
        self._since_refine = 0
        self._lock = threading.Lock()

    def _append(self, episode_id: str, record: dict) -> None:
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            path = self.evidence_dir / f"{episode_id or 'default'}.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # evidence must never break scheduling
            log.warning("skill evidence write failed: %s", exc)

    def record_decision(
        self,
        job,
        *,
        success: bool,
        plan=None,
        reason: str = "",
        error: str = "",
    ) -> None:
        """Broker hook: one record per resolved scheduling decision."""
        episode_id = getattr(job, "episode_id", "") or "default"
        coverage = _coverage_pct(job)
        summary = getattr(plan, "summary", None) or {}
        data = {
            "success": bool(success),
            "decision_reason": (
                getattr(plan, "decision_reason", "") or reason or ""
            )[:400],
            "task_kinds": _task_kinds(plan),
            "assignments": len(summary.get("assignments") or ()),
            "trigger": getattr(plan, "trigger", "") or getattr(job, "trigger", ""),
            "kind": getattr(plan, "kind", "") or getattr(job, "kind", ""),
        }
        if plan is not None:
            data["plan_id"] = getattr(plan, "plan_id", "")
            data["plan_status"] = getattr(plan, "status", "")
        if reason:
            data["outcome"] = reason[:120]
        if error:
            data["error"] = str(error)[:200]
        ts = time.time()
        self._append(episode_id, {
            "type": "mission_decision",
            "episode_id": episode_id,
            "ts": ts,
            "data": data,
        })
        if coverage is not None:
            self._append(episode_id, {
                "type": "coverage_update",
                "episode_id": episode_id,
                "ts": ts,
                "data": {"coverage_pct": coverage},
            })
        with self._lock:
            if success:
                self._since_refine += 1
            due = self._since_refine >= self.refine_every
        if due:
            self.refine()

    def refine(self) -> list[str]:
        """Run the extractor over all accumulated evidence."""
        from src.schedule.skill_extractor import extract_skills

        with self._lock:
            self._since_refine = 0
        try:
            written = extract_skills(self.evidence_dir, self.skills_dir)
        except Exception as exc:
            log.warning("skill refine failed: %s", exc)
            return []
        if written:
            log.info(
                "skill refinery updated %d skill(s): %s",
                len(written), ", ".join(written),
            )
        return written
