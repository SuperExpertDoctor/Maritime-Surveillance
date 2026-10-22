"""Decision-evidence logging + periodic skill refinement.

Closes the skills self-evolution loop. Every resolved scheduling
decision — a committed plan, a rejected/expired plan, or a failed agent
run — appends a ``mission_decision`` record carrying a compact
situation fingerprint (contact count, UAV ops mix, candidate-kind mix,
cumulative coverage) to ``outputs/skills_evidence/<episode_id>.jsonl``.
Runtime journal milestones (task completed/failed, target found, UAV
returned, probe timed out, …) are drained into ``decision_outcome``
records so the extractor can backfill each decision's later result —
a decision card without its outcome has no strategy value.

After ``refine_every`` successful decisions the refinery runs
``src.schedule.skill_extractor.extract_skills`` over the evidence
directory, merging signatures into the skills ledger
(``skills/index.json``) and regenerating ``SKILL.md`` files under the
agent skills root. The worker re-scans that directory per job, so
refined skills enter ``<available_skills>`` without a restart.

Runtime settings (``outputs/skills_evidence/settings.json``, editable
via ``GET/POST /api/skills/settings`` and the settings menu) control
whether auto-refinement runs, its cadence, and the minimum cumulative
coverage an episode needs before its trajectory supports a skill.
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
DEFAULT_MIN_COVERAGE_PCT = 60.0

# Journal statuses that describe what happened *after* a decision and
# therefore close the action→outcome loop for evidence backfill.
OUTCOME_STATUSES = frozenset({
    "task_completed", "task_failed", "uav_returned", "target_found",
    "search_complete", "assessment_applied", "probe_timed_out",
    "mission_model_retry_succeeded", "mission_model_retry_failed",
    "mission_assignment_committed",
})


def _coverage_pct(job) -> float | None:
    """Cumulative (ever-scanned) coverage from the job's snapshot.

    Prefers ``cumulative_pct`` — the same ever-scanned fraction the UI and
    the extractor's coverage threshold refer to. Falls back to the
    zone-gap (fresh-window) computation for snapshots built before the
    field existed.
    """
    snapshot = getattr(getattr(job, "runtime", None), "internal_snapshot", None)
    summary = getattr(snapshot, "coverage_summary", None) or {}
    cumulative = summary.get("cumulative_pct")
    if cumulative is not None:
        return round(float(cumulative), 2)
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


def _task_ids(plan) -> list[str]:
    summary = getattr(plan, "summary", None) or {}
    return [
        str(row.get("task_id"))
        for row in summary.get("assignments") or ()
        if isinstance(row, dict) and row.get("task_id")
    ]


def _situation(job) -> dict:
    """Compact situation fingerprint (~150 token when serialised).

    Counts and buckets only — never raw candidate lists or bboxes. This
    is the "under what situation" half of a decision card; the selected
    task kinds are the "what posture" half.
    """
    snapshot = getattr(getattr(job, "runtime", None), "internal_snapshot", None)
    if snapshot is None:
        return {}
    candidate_kinds: dict[str, int] = {}
    for cand in getattr(snapshot, "candidates", ()) or ():
        kind = str(getattr(cand, "kind", "") or "unknown")
        candidate_kinds[kind] = candidate_kinds.get(kind, 0) + 1
    uav_ops: dict[str, int] = {}
    for res in getattr(snapshot, "resources", ()) or ():
        op = str(getattr(res, "operation", "") or "unknown")
        uav_ops[op] = uav_ops.get(op, 0) + 1
    return {
        "contacts": len(getattr(snapshot, "contacts", ()) or ()),
        "uav_ops": uav_ops,
        "uavs_available": len(getattr(snapshot, "available_uav_ids", ()) or ()),
        "active_tasks": len(getattr(snapshot, "active_tasks", ()) or ()),
        "candidate_kinds": candidate_kinds,
    }


class SkillRefinery:
    """Append-only evidence writer with a success-count refine trigger."""

    def __init__(
        self,
        evidence_dir: str | Path,
        skills_dir: str | Path,
        *,
        refine_every: int = DEFAULT_REFINE_EVERY,
        auto_refine: bool = True,
        min_coverage_pct: float = DEFAULT_MIN_COVERAGE_PCT,
    ) -> None:
        self.evidence_dir = Path(evidence_dir)
        self.skills_dir = Path(skills_dir)
        self.refine_every = max(1, int(refine_every))
        self.auto_refine = bool(auto_refine)
        self.min_coverage_pct = float(min_coverage_pct)
        self._since_refine = 0
        self._lock = threading.Lock()
        self._journal = None
        self._journal_cursor = 0
        self._settings_path = self.evidence_dir / "settings.json"
        self._load_settings()

    # ------------------------------------------------------------------
    # Runtime settings (settings menu -> /api/skills/settings)
    # ------------------------------------------------------------------
    def _load_settings(self) -> None:
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception:
            return
        try:
            self.auto_refine = bool(data.get("auto_refine", self.auto_refine))
            self.refine_every = max(1, int(data.get("refine_every", self.refine_every)))
            self.min_coverage_pct = float(data.get("min_coverage_pct", self.min_coverage_pct))
        except (TypeError, ValueError):
            pass

    def get_settings(self) -> dict:
        return {
            "auto_refine": self.auto_refine,
            "refine_every": self.refine_every,
            "min_coverage_pct": self.min_coverage_pct,
            "since_refine": self._since_refine,
        }

    def set_settings(self, data: dict) -> dict:
        with self._lock:
            if "auto_refine" in data:
                self.auto_refine = bool(data["auto_refine"])
            if "refine_every" in data:
                self.refine_every = max(1, int(data["refine_every"]))
            if "min_coverage_pct" in data:
                self.min_coverage_pct = max(0.0, min(100.0, float(data["min_coverage_pct"])))
        try:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            self._settings_path.write_text(json.dumps({
                "auto_refine": self.auto_refine,
                "refine_every": self.refine_every,
                "min_coverage_pct": self.min_coverage_pct,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("skill settings persist failed: %s", exc)
        return self.get_settings()

    # ------------------------------------------------------------------
    # Journal drain -> decision_outcome records
    # ------------------------------------------------------------------
    def attach_journal(self, journal) -> None:
        self._journal = journal

    def drain_journal(self) -> int:
        """Append runtime-journal outcome milestones to evidence."""
        journal = self._journal
        if journal is None:
            return 0
        try:
            entries = journal.since(self._journal_cursor)
        except Exception:
            return 0
        written = 0
        for entry in entries:
            if int(entry.get("id") or 0) > self._journal_cursor:
                self._journal_cursor = int(entry["id"])
            status = str(entry.get("status") or "")
            if status not in OUTCOME_STATUSES:
                continue
            episode_id = str(entry.get("episode_id") or "default")
            self._append(episode_id, {
                "type": "decision_outcome",
                "episode_id": episode_id,
                "ts": time.time(),
                "data": {
                    "status": status,
                    "level": str(entry.get("level") or "info"),
                    "uav_id": str(entry.get("uav_id") or ""),
                    "task_id": str(entry.get("task_id") or ""),
                    "sim_time_min": entry.get("sim_time_min"),
                },
            })
            written += 1
        return written

    # ------------------------------------------------------------------
    # Evidence append
    # ------------------------------------------------------------------
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
        self.drain_journal()
        episode_id = getattr(job, "episode_id", "") or "default"
        coverage = _coverage_pct(job)
        summary = getattr(plan, "summary", None) or {}
        data = {
            "success": bool(success),
            "decision_reason": (
                getattr(plan, "decision_reason", "") or reason or ""
            )[:400],
            "task_kinds": _task_kinds(plan),
            "task_ids": _task_ids(plan),
            "assignments": len(summary.get("assignments") or ()),
            "trigger": getattr(plan, "trigger", "") or getattr(job, "trigger", ""),
            "kind": getattr(plan, "kind", "") or getattr(job, "kind", ""),
            "situation": _situation(job),
        }
        if coverage is not None:
            data["coverage_pct"] = coverage
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
            due = self.auto_refine and self._since_refine >= self.refine_every
        if due:
            self.refine()

    def refine(self) -> list[str]:
        """Run the extractor over all accumulated evidence."""
        from src.schedule.skill_extractor import extract_skills

        self.drain_journal()
        with self._lock:
            self._since_refine = 0
        try:
            written = extract_skills(
                self.evidence_dir, self.skills_dir,
                min_coverage_pct=self.min_coverage_pct,
            )
        except Exception as exc:
            log.warning("skill refine failed: %s", exc)
            return []
        if written:
            log.info(
                "skill refinery updated %d skill(s): %s",
                len(written), ", ".join(written),
            )
        return written
