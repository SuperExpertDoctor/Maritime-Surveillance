"""Signature-ledger skill extractor — real skills self-evolution.

Evidence pipeline (``outputs/skills_evidence/<episode>.jsonl``):

* ``mission_decision``  — one compact decision card per resolved
  scheduling decision: situation fingerprint (contact count, UAV ops
  mix, candidate-kind mix), posture (selected task-kind groups),
  trigger, success, reason, chosen ``task_ids``.
* ``decision_outcome`` — runtime milestones drained from the journal
  (task_completed/failed, target_found, uav_returned, probe_timed_out,
  …) joined back to decisions via ``task_id`` so each card carries its
  later result.
* ``coverage_update``  — cumulative coverage timeline, interpolated
  onto each card's band.

Every decision maps to a **signature** ``posture|trigger|cov-band|ct-band``.
The signature is the skill's unique key: new episodes merge into the
same skill — evidence accumulates, file count does not. Skills that
share a *situation* signature but differ in posture are marked
``conflicted`` (同局面多策略) for human adjudication instead of being
silently deduplicated.

Skills are progressive disclosure:

* ring 1 — frontmatter ``description`` (confidence + signature) which
  ``<available_skills>`` injects for every job;
* ring 2 — ``SKILL.md`` four-section body (什么时候使用 / 策略是什么 /
  使用后的预期效果 / 事实证据);
* ring 3 — ``references/evidence.md`` appendix with every recorded
  reason, phase narratives and per-episode stats.

``skills/index.json`` is the ledger: per-signature episode stats,
weighted confidence (24h evidence half-life), conflict/alternate marks
and the ``human_reviewed`` flag that freezes the body against automatic
rewrites.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

# Qualification thresholds — an episode "supports" a signature only when
# its decisions under it were numerous enough, mostly successful, and
# the episode reached a healthy cumulative coverage.
MIN_DECISION_SUCCESS_RATE = 0.8
MIN_COVERAGE_PCT = 60.0          # overridable via extract_skills(min_coverage_pct=…)
MIN_DECISIONS = 3                # per signature within one episode
STALE_CONFIDENCE = 0.15          # below this a skill is marked stale
EVIDENCE_HALF_LIFE_S = 86_400.0  # 24h — old episodes count less

INDEX_NAME = "index.json"

KIND_GROUP = {
    "search": "cov", "coverage": "cov", "partition": "cov", "zone": "cov",
    "direction_search": "cov", "investigation": "trk",
    "probe": "trk", "investigate": "trk", "track": "trk", "reacquire": "trk",
    "rtb": "rtb", "return": "rtb", "returning": "rtb", "base": "rtb",
}
GROUP_LABEL = {"cov": "区域覆盖", "trk": "目标跟踪", "rtb": "返航回收", "none": "无任务"}
TRIG_LABEL = {
    "evt": "事件触发", "per": "周期性触发", "init": "首次部署",
    "re": "失败重试", "man": "人工消息", "other": "其他",
}
TRIG_MAP = {
    "event": "evt", "periodic": "per", "initial": "init", "init_deploy": "init",
    "retry": "re", "failed_retry": "re", "manual": "man", "message": "man",
    "operator": "man",
}
COV_LABEL = {"low": "覆盖率低位(<30%)", "mid": "覆盖率中位(30–60%)", "high": "覆盖率高位(≥60%)", "na": "覆盖率未知"}
CT_LABEL = {"none": "无接触", "one": "单一接触", "few": "少量接触(2–3)", "many": "多接触(≥4)", "na": "接触未知"}


def _posture(kinds) -> str:
    groups = {KIND_GROUP.get(str(k).lower(), "other") for k in kinds}
    groups.discard("other")
    return "+".join(sorted(groups)) if groups else "none"


def _cov_band(pct) -> str:
    if pct is None:
        return "na"
    if pct >= 60.0:
        return "high"
    if pct >= 30.0:
        return "mid"
    return "low"


def _ct_band(n) -> str:
    if n is None:
        return "na"
    if n >= 4:
        return "many"
    if n >= 2:
        return "few"
    if n == 1:
        return "one"
    return "none"


def _trig(trigger: str) -> str:
    return TRIG_MAP.get(str(trigger or "").lower(), "other")


@dataclass
class DecisionCard:
    """One condensed decision — the ~150-token trajectory record."""
    episode_id: str = ""
    seq: int = 0
    ts: float = 0.0
    sim_time_min: float = 0.0
    trigger: str = "other"
    success: bool = False
    reason: str = ""
    kinds: tuple = ()
    task_ids: tuple = ()
    coverage_pct: float | None = None
    contacts: int | None = None
    posture: str = "none"
    sig_key: str = ""
    sit_key: str = ""


@dataclass
class EpisodeEvidence:
    episode_id: str = ""
    cards: list = field(default_factory=list)
    outcomes: list = field(default_factory=list)
    coverage_samples: list = field(default_factory=list)   # (ts, pct)
    first_ts: float = 0.0
    last_ts: float = 0.0


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


def load_episodes(evidence_dir: Path) -> dict[str, EpisodeEvidence]:
    episodes: dict[str, EpisodeEvidence] = {}
    for path in sorted(Path(evidence_dir).glob("*.jsonl")):
        ep = episodes.setdefault(path.stem, EpisodeEvidence(episode_id=path.stem))
        for record in _iter_jsonl(path):
            episode_id = str(record.get("episode_id") or path.stem)
            if episode_id != ep.episode_id:
                ep = episodes.setdefault(episode_id, EpisodeEvidence(episode_id=episode_id))
            rtype = str(record.get("type") or "")
            data = record.get("data") if isinstance(record.get("data"), dict) else {}
            ts = float(record.get("ts") or 0.0)
            ep.first_ts = ep.first_ts or ts
            ep.last_ts = max(ep.last_ts, ts)
            if rtype in {"mission_decision", "allocation_decision"}:
                situation = data.get("situation") if isinstance(data.get("situation"), dict) else {}
                card = DecisionCard(
                    episode_id=episode_id,
                    seq=len(ep.cards) + 1,
                    ts=ts,
                    trigger=_trig(data.get("trigger")),
                    success=bool(data.get("success")),
                    reason=str(data.get("decision_reason") or data.get("notes") or "")[:240],
                    kinds=tuple(sorted(data.get("task_kinds") or ())),
                    task_ids=tuple(data.get("task_ids") or ()),
                    coverage_pct=(
                        float(data["coverage_pct"])
                        if data.get("coverage_pct") is not None else None
                    ),
                    contacts=situation.get("contacts"),
                )
                ep.cards.append(card)
            elif rtype == "decision_outcome":
                ep.outcomes.append(data)
            elif rtype in {"coverage_update", "coverage_report"}:
                pct = data.get("coverage_pct") or data.get("rolling_coverage_pct")
                if pct is not None:
                    ep.coverage_samples.append((ts, float(pct)))
    # Interpolate coverage onto cards lacking their own sample, then sign.
    for ep in episodes.values():
        samples = sorted(ep.coverage_samples)
        for card in ep.cards:
            if card.coverage_pct is None and samples:
                prior = [pct for ts, pct in samples if ts <= card.ts]
                card.coverage_pct = prior[-1] if prior else samples[0][1]
            if card.contacts is None:
                card.contacts = 0
            card.posture = _posture(card.kinds)
            trig = card.trigger
            cov = _cov_band(card.coverage_pct)
            ct = _ct_band(card.contacts)
            card.sig_key = f"{card.posture}|{trig}|cov-{cov}|ct-{ct}"
            card.sit_key = f"{trig}|cov-{cov}|ct-{ct}"
    return episodes


def _episode_peak_coverage(ep: EpisodeEvidence) -> float:
    pcts = [c.coverage_pct for c in ep.cards if c.coverage_pct is not None]
    pcts += [pct for _, pct in ep.coverage_samples]
    return max(pcts) if pcts else 0.0


def _phases(cards) -> list[str]:
    """Runs of consecutive decisions sharing a signature → phase lines."""
    lines = []
    start = 0
    while start < len(cards):
        sig = cards[start].sig_key
        end = start
        while end + 1 < len(cards) and cards[end + 1].sig_key == sig:
            end += 1
        run = cards[start:end + 1]
        succeeded = sum(1 for c in run if c.success)
        posture_lbl = "+".join(GROUP_LABEL.get(g, g) for g in run[0].posture.split("+"))
        lines.append(
            f"D{run[0].seq}–D{run[-1].seq}（{TRIG_LABEL.get(run[0].trigger, run[0].trigger)}·"
            f"{posture_lbl}）：{len(run)} 轮决策，{succeeded} 轮生效"
        )
        start = end + 1
    return lines


def _weight(ep_ts: float, now: float) -> float:
    return 0.5 ** (max(0.0, now - ep_ts) / EVIDENCE_HALF_LIFE_S)


def _load_index(skills_dir: Path) -> dict:
    path = skills_dir / INDEX_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("skills"), dict):
            return data
    except Exception:
        pass
    return {"version": 1, "skills": {}}


def _skill_name(sig_key: str) -> str:
    slug = re.sub(r"[^a-z0-9+]+", "-", sig_key.replace("|", "-").replace("+", "-").lower())
    return f"ops-{re.sub('-+', '-', slug).strip('-')[:52]}"


def _sig_description(sig: dict, entry: dict) -> str:
    posture = "+".join(GROUP_LABEL.get(g, g) for g in str(sig.get("posture", "none")).split("+"))
    return (
        f"姿态 {posture}｜{TRIG_LABEL.get(sig.get('trigger'), sig.get('trigger'))}"
        f"·{COV_LABEL.get(sig.get('coverage_band'), '?')}"
        f"·{CT_LABEL.get(sig.get('contact_band'), '?')}｜"
        f"已验证 {entry.get('support', 0)}/{entry.get('occurrence', 0)} 回合"
        f"·置信 {entry.get('confidence', 0):.2f}"
    )


def _render_body(name: str, sig: dict, entry: dict, evidence: dict) -> str:
    posture_lbl = "+".join(GROUP_LABEL.get(g, g) for g in str(sig.get("posture", "none")).split("+"))
    trig_lbl = TRIG_LABEL.get(sig.get("trigger"), sig.get("trigger"))
    cov_lbl = COV_LABEL.get(sig.get("coverage_band"), "?")
    ct_lbl = CT_LABEL.get(sig.get("contact_band"), "?")
    kinds_lbl = "、".join(evidence.get("kinds") or ()) or "search/probe"
    episodes = entry.get("episodes") or {}
    succeeded = sum(int(e.get("succeeded") or 0) for e in episodes.values())
    decisions = sum(int(e.get("decisions") or 0) for e in episodes.values())
    rate = succeeded / decisions if decisions else 0.0
    peaks = [float(e.get("coverage_pct") or 0.0) for e in episodes.values()]
    peak = max(peaks) if peaks else 0.0
    reasons = "\n".join(f"- {r}" for r in evidence.get("reasons", [])[:5]) or "- （暂无决策理由记录）"
    outcomes = Counter(evidence.get("outcomes") or ())
    outcome_lbl = "、".join(f"{k}×{v}" for k, v in outcomes.most_common(4)) or "暂无"
    alternates = "、".join(entry.get("alternates") or ())
    conflict_note = (
        f"\n> ⚠ 同局面存在备选策略：{alternates}。界面已标待裁决，人工确认前按本策略执行。\n"
        if alternates else ""
    )
    reviewed = "\n> ✎ 本文已经人工审定，自动凝练只更新账本与附录，不改正文。\n" if entry.get("human_reviewed") else ""
    return f"""---
name: {name}
description: "{_sig_description(sig, entry)}"
---

# {posture_lbl} 策略 — {trig_lbl}
{conflict_note}{reviewed}
本 skill 由 {entry.get('occurrence', 0)} 个真实回合的决策轨迹凝练而成，是经验参考而非
硬性规则：使用前务必对当前态势重新执行 `validate_selection` 校验。

## 什么时候使用

触发方式为**{trig_lbl}**，态势处于**{cov_lbl}**、**{ct_lbl}**时，采用**{posture_lbl}**姿态——
即候选任务混合 {kinds_lbl} 等类型、编队需在覆盖值班与查证跟踪之间分配的局面。

## 策略是什么

1. 先调用 `get_mission_snapshot` 拉取当前态势，核对触发方式、覆盖区间与接触规模是否与本签名吻合。
2. 保持已验证的{posture_lbl}姿态：覆盖任务保底轮换，接触类任务就近查证/跟踪，回转按电量有序回收。
3. 提交前必须 `validate_selection` 预校验；其他回合的指标不能让当前非法选择变合法。

## 使用后的预期效果

- 决策成功率约 {rate:.0%}（{succeeded}/{decisions} 次提交通过校验并生效）
- 回合累计覆盖率峰值约 {peak:.1f}%
- 已验证 {entry.get('support', 0)}/{entry.get('occurrence', 0)} 个回合 · 置信度 {entry.get('confidence', 0):.2f}
- 关联任务结局：{outcome_lbl}

## 事实证据

{reasons}

详见 `references/evidence.md`（全部决策理由、阶段叙事与逐回合统计）。
"""


def _render_evidence_md(name: str, sig: dict, entry: dict, evidence: dict, episodes: dict) -> str:
    lines = [
        f"# {name} — 证据附录", "",
        f"签名：`{entry.get('sig_key', '')}`", "",
        "## 逐回合统计", "",
        "| 回合 | 决策数 | 生效数 | 覆盖率峰值 | 是否支撑 |",
        "| --- | ---: | ---: | ---: | --- |",
    ]
    for ep_id, stats in sorted(entry.get("episodes", {}).items(), key=lambda kv: -kv[1].get("ts", 0)):
        lines.append(
            f"| {ep_id[:24]} | {stats.get('decisions', 0)} | {stats.get('succeeded', 0)} "
            f"| {float(stats.get('coverage_pct') or 0):.1f}% | {'✓' if stats.get('support') else '·'} |"
        )
    if entry.get("alternates"):
        lines += ["", "## 同局面备选策略（待人工裁决）", ""]
        for alt in entry["alternates"]:
            lines.append(f"- `{alt}` —— 同局面不同姿态，人工确认前两条并存")
    lines += ["", "## 阶段叙事", ""]
    for ep_id, cards in episodes.items():
        phase_lines = _phases(cards)
        if phase_lines:
            lines.append(f"### {ep_id[:28]}")
            lines += [f"- {p}" for p in phase_lines]
            lines.append("")
    lines += ["## 全部决策理由", ""]
    for ep_id, reasons in evidence.get("reasons_by_episode", {}).items():
        lines.append(f"### {ep_id[:28]}")
        for reason in reasons:
            lines.append(f"- {reason}")
        lines.append("")
    outcomes = Counter(evidence.get("outcomes") or ())
    if outcomes:
        lines += ["## 决策结局回填", ""]
        for status, count in outcomes.most_common():
            lines.append(f"- `{status}` × {count}")
    return "\n".join(lines) + "\n"


def extract_skills(
    outputs_dir: str | Path = "outputs",
    skills_dir: str | Path = "harness/agent/skills",
    *,
    min_coverage_pct: float = MIN_COVERAGE_PCT,
    max_reasons: int = 8,
) -> list[str]:
    """Merge evidence into the signature ledger; returns written names."""
    outputs = Path(outputs_dir)
    skills = Path(skills_dir)
    episodes = load_episodes(outputs)
    if not episodes:
        return []
    index = _load_index(skills)
    ledger = index["skills"]
    now = time.time()

    # Per-signature evidence aggregation.
    sig_cards: dict[str, list] = defaultdict(list)          # sig_key -> cards (all episodes)
    sig_ep_cards: dict[str, dict[str, list]] = defaultdict(dict)  # sig_key -> ep -> cards
    for ep in episodes.values():
        for card in ep.cards:
            sig_cards[card.sig_key].append(card)
            sig_ep_cards[card.sig_key].setdefault(ep.episode_id, []).append(card)

    written: list[str] = []
    pending: list[tuple] = []
    for sig_key, ep_cards in sig_ep_cards.items():
        cards = sig_cards[sig_key]
        if len(cards) < MIN_DECISIONS:
            continue
        posture, trig, cov_band, ct_band = sig_key.split("|")
        sit_key = cards[0].sit_key
        name = _skill_name(sig_key)

        entry = ledger.setdefault(sig_key, {
            "name": name,
            "signature": {
                "posture": posture, "trigger": trig,
                "coverage_band": cov_band[4:], "contact_band": ct_band[3:],
            },
            "sig_key": sig_key, "situation_key": sit_key,
            "episodes": {}, "occurrence": 0, "support": 0,
            "confidence": 0.0, "conflicted": False, "alternates": [],
            "human_reviewed": False, "stale": False,
            "first_seen": now, "updated_at": now, "last_evidence_ts": 0.0,
        })
        entry_episodes = entry.setdefault("episodes", {})

        # Fold every episode's per-signature slice into the ledger.
        for ep_id, run in ep_cards.items():
            peak = _episode_peak_coverage(episodes[ep_id])
            succeeded = sum(1 for c in run if c.success)
            occurrence = len(run) >= MIN_DECISIONS
            support = (
                occurrence
                and succeeded / len(run) >= MIN_DECISION_SUCCESS_RATE
                and peak >= min_coverage_pct
            )
            ep_ts = episodes[ep_id].last_ts or now
            entry_episodes[ep_id] = {
                "ts": ep_ts,
                "decisions": len(run),
                "succeeded": succeeded,
                "coverage_pct": round(peak, 2),
                "support": bool(support),
            }
        entry["occurrence"] = sum(
            1 for stats in entry_episodes.values() if stats.get("decisions", 0) >= MIN_DECISIONS
        )
        entry["support"] = sum(1 for stats in entry_episodes.values() if stats.get("support"))
        num = sum(_weight(s["ts"], now) for s in entry_episodes.values() if s.get("support"))
        den = sum(_weight(s["ts"], now) for s in entry_episodes.values())
        entry["confidence"] = round(num / den, 2) if den else 0.0
        entry["stale"] = entry["confidence"] < STALE_CONFIDENCE
        entry["last_evidence_ts"] = max(
            [stats["ts"] for stats in entry_episodes.values()] or [now]
        )
        entry["updated_at"] = now

        # Evidence bundle for rendering.
        reasons_all = [
            c.reason for c in sorted(cards, key=lambda c: -c.ts)
            if c.reason
        ]
        seen, reasons = set(), []
        for r in reasons_all:
            if r not in seen:
                seen.add(r)
                reasons.append(r)
            if len(reasons) >= max_reasons * 3:
                break
        outcome_labels = [
            o.get("status") for ep_id in ep_cards for o in episodes[ep_id].outcomes
            if o.get("task_id") and o.get("task_id") in {
                tid for c in ep_cards[ep_id] for tid in c.task_ids
            }
        ]
        evidence = {
            "kinds": sorted({k for c in cards for k in c.kinds}),
            "reasons": reasons[:5],
            "reasons_by_episode": {
                ep_id: [c.reason for c in run if c.reason][:12]
                for ep_id, run in ep_cards.items()
            },
            "outcomes": outcome_labels,
        }
        pending.append((sig_key, entry, evidence, ep_cards))

    # Second pass — alternate/conflict marks need the whole situation_key
    # group populated first (order-independent), including entries
    # written by earlier refine runs.
    by_situation: dict[str, list[dict]] = defaultdict(list)
    for entry in ledger.values():
        if entry.get("situation_key"):
            by_situation[entry["situation_key"]].append(entry)
    for group in by_situation.values():
        if len(group) < 2:
            continue
        supported = sum(1 for e in group if e.get("support", 0) > 0)
        for entry in group:
            entry["alternates"] = [e["name"] for e in group if e is not entry]
            # Same situation, different postures, and the evidence cannot
            # yet pick a winner → flag for human adjudication.
            entry["conflicted"] = supported >= 2 or (supported == 0 and len(group) > 1)

    for sig_key, entry, evidence, ep_cards in pending:
        name = entry["name"]
        sig = entry["signature"]
        target = skills / name
        target.mkdir(parents=True, exist_ok=True)
        (target / "references").mkdir(exist_ok=True)
        (target / "references" / "evidence.md").write_text(
            _render_evidence_md(name, sig, entry, evidence, ep_cards), encoding="utf-8",
        )
        if entry.get("human_reviewed"):
            _LOGGER.info("skill %s human-reviewed: ledger+appendix only", name)
        else:
            (target / "SKILL.md").write_text(
                _render_body(name, sig, entry, evidence), encoding="utf-8",
            )
        written.append(name)
        _LOGGER.info("skill %s merged (%d episode(s), conf %.2f)", name, len(entry_episodes), entry["confidence"])

    skills.mkdir(parents=True, exist_ok=True)
    (skills / INDEX_NAME).write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return written


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outputs", default="outputs")
    parser.add_argument("--skills", default="harness/agent/skills")
    parser.add_argument("--min-coverage", type=float, default=MIN_COVERAGE_PCT)
    args = parser.parse_args()
    written = extract_skills(args.outputs, args.skills, min_coverage_pct=args.min_coverage)
    print(f"extracted {len(written)} skill(s): {', '.join(written) or 'none'}")


if __name__ == "__main__":
    main()
