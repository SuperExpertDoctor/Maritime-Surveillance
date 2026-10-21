"""Extract successful decision trajectories into PI-agent SKILL.md files.

Reads the run artifacts under ``outputs/`` (decision logs + frame logger
output), scores recent trajectories by mission-completion metrics — rolling
coverage, sustained tracking time and decision success rate — and writes a
YAML-frontmattered ``SKILL.md`` per qualifying trajectory into
``harness/agent/skills/<name>/``. pi-coding-agent discovers these at session
start and injects ``<available_skills>`` entries (name + description); the
model ``read``s the full file only when relevant.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Metric thresholds for a trajectory to count as "successful" enough to
# codify into a skill. Values are conservative so only clearly good runs
# produce reusable guidance.
MIN_DECISION_SUCCESS_RATE = 0.8
MIN_COVERAGE_PCT = 60.0
MIN_TRACK_HOLD_MIN = 10.0
MIN_DECISIONS = 3


@dataclass
class TrajectoryMetrics:
    """Aggregate metrics for one episode's decision trajectory."""

    episode_id: str = ""
    decisions_total: int = 0
    decisions_succeeded: int = 0
    coverage_pct: float = 0.0
    track_hold_min: float = 0.0
    selected_kinds: dict = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return self.decisions_succeeded / self.decisions_total if self.decisions_total else 0.0

    @property
    def qualifies(self) -> bool:
        return (
            self.decisions_total >= MIN_DECISIONS
            and self.success_rate >= MIN_DECISION_SUCCESS_RATE
            and self.coverage_pct >= MIN_COVERAGE_PCT
        )


def _iter_jsonl(path: Path):
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _metrics_from_events(episode_id: str, events) -> TrajectoryMetrics:
    metrics = TrajectoryMetrics(episode_id=episode_id)
    for event in events:
        etype = event.get("type") or event.get("event") or ""
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        if etype in {"mission_decision", "allocation_decision"}:
            metrics.decisions_total += 1
            if data.get("success") or data.get("assignments", 0) > 0:
                metrics.decisions_succeeded += 1
            reason = data.get("decision_reason") or data.get("notes") or ""
            if reason:
                metrics.reasons.append(str(reason)[:200])
            for kind in data.get("task_kinds") or ():
                metrics.selected_kinds[kind] = metrics.selected_kinds.get(kind, 0) + 1
        elif etype in {"coverage_update", "coverage_report"}:
            pct = data.get("coverage_pct") or data.get("rolling_coverage_pct")
            if pct is not None:
                metrics.coverage_pct = max(metrics.coverage_pct, float(pct))
        elif etype in {"track_hold", "tracking_milestone"}:
            hold = data.get("hold_min") or data.get("duration_min")
            if hold is not None:
                metrics.track_hold_min = max(metrics.track_hold_min, float(hold))
    return metrics


def collect_metrics(outputs_dir: Path) -> list[TrajectoryMetrics]:
    """Score every episode with logged decisions under outputs/."""
    results: dict[str, TrajectoryMetrics] = {}
    candidates = sorted(outputs_dir.glob("**/*.jsonl"))
    for path in candidates:
        if "pi" in path.parts or "worker" in path.name:
            continue
        episode_metrics: dict[str, TrajectoryMetrics] = {}
        for record in _iter_jsonl(path):
            episode_id = str(record.get("episode_id") or "default")
            bundle = episode_metrics.setdefault(episode_id, [])
            bundle.append(record)
        for episode_id, events in episode_metrics.items():
            metrics = _metrics_from_events(episode_id, events)
            if metrics.decisions_total:
                results[episode_id] = metrics
    return list(results.values())


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "trajectory"


def render_skill(metrics: TrajectoryMetrics) -> tuple[str, str]:
    """Produce ``(skill_name, SKILL.md body)`` for one good trajectory."""
    kinds = "、".join(sorted(metrics.selected_kinds)[:6]) or "search/track"
    name = f"ops-pattern-{_slug(metrics.episode_id)}"
    reasons = "\n".join(
        f"- {reason}" for reason in metrics.reasons[:8]
    ) or "- （决策日志未记录理由字段）"
    body = f"""---
name: {name}
description: "Successful multi-UAV selection trajectory: {metrics.decisions_succeeded}/{metrics.decisions_total} decisions committed, coverage {metrics.coverage_pct:.1f}%, sustained tracking {metrics.track_hold_min:.1f}min. Use when a similar candidate mix ({kinds}) appears."
---

# Reusable Selection Trajectory — {metrics.episode_id}

Derived from a real episode whose decisions validated and committed. Treat as
guidance, not authority: re-run `validate_selection` for the current snapshot.

## Outcome Metrics

- decision success rate: {metrics.success_rate:.0%} ({metrics.decisions_succeeded}/{metrics.decisions_total})
- rolling coverage: {metrics.coverage_pct:.1f}%
- longest sustained tracking hold: {metrics.track_hold_min:.1f}min
- task kinds exercised: {kinds}

## What Worked

{reasons}

## How to Reuse

1. Call `get_mission_snapshot`; compare this episode's candidate mix to the current one — apply only if kinds and coverage constraints rhyme.
2. Prefer the same selection posture (coverage duty first, preserve viable tracking, preempt only for clearly better work).
3. Always dry-run `validate_selection`; metrics from another episode do not make an invalid current selection valid.
"""
    return name, body


def extract_skills(
    outputs_dir: str | Path = "outputs",
    skills_dir: str | Path = "harness/agent/skills",
    *,
    keep: int = 8,
) -> list[str]:
    """Write SKILL.md files for the best trajectories; returns written names."""
    outputs = Path(outputs_dir)
    skills = Path(skills_dir)
    metrics = [m for m in collect_metrics(outputs) if m.qualifies]
    metrics.sort(key=lambda m: (m.success_rate, m.coverage_pct), reverse=True)
    written = []
    for item in metrics[:keep]:
        name, body = render_skill(item)
        target = skills / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "SKILL.md").write_text(body, encoding="utf-8")
        written.append(name)
        _LOGGER.info("extracted skill %s from episode %s", name, item.episode_id)
    return written


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--skills", default="harness/agent/skills")
    args = parser.parse_args()
    written = extract_skills(args.outputs, args.skills)
    print(f"extracted {len(written)} skill(s): {', '.join(written) or 'none'}")


if __name__ == "__main__":
    main()
