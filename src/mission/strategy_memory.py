"""Bounded, evidence-backed scheduling memories.

Strategy memory is deliberately a data store, not an execution engine.  A
memory can suggest how to prioritize already-generated tasks, but it cannot
carry code, identities, truth labels, or changes to navigation and sensing
constraints.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4

from src.mission.outcome_evaluator import EpisodeOutcome


MEMORY_SCHEMA = "strategy-memory/v1"
_ALLOWED_STATUS = {"candidate", "validated", "active", "rejected", "retired"}
_ALLOWED_CONDITION_VALUES = {
    "ais_contact_load": {"low", "medium", "high"},
    "available_uav_fraction": {"low", "medium", "high"},
    "has_active_intent": {"yes", "no"},
    "weather_disruption": {"low", "high"},
}
_FORBIDDEN_KEYS = {
    "actual_military", "truth_identity", "is_military", "is_evasive",
    "gate_state", "physical_ship_id", "ship_truth", "red_plan", "red_snapshot",
    "truth", "identity", "contact_id", "ship_id", "uav_id", "model_call_id",
    "raw_attempts", "selected_task_ids", "preempt_uav_ids", "task_id", "region_id",
    "probe_id", "assessment_id", "snapshot_id",
}
_CODE_MARKERS = (
    "```", "eval(", "exec(", "import ", "def ", "class ", "lambda ",
    "subprocess", "os.system", "__import__", "<script",
)
_ID_PATTERN = re.compile(r"(?:\bC\d{2,}\b|\bS\d{1,}\b|\bUAV[-_]?[A-Za-z0-9]+\b)", re.IGNORECASE)
_SAFE_EPISODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class StrategyMemory:
    memory_id: str
    version: int
    status: str
    applies_when: dict[str, str]
    advice: str
    supporting_episode_ids: tuple[str, ...]
    supporting_metrics: dict[str, float]
    validation_report_id: str | None
    created_at_utc: str
    parent_version: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.memory_id, str) or not self.memory_id:
            raise ValueError("memory_id must be a non-empty string")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 1:
            raise ValueError("version must be a positive integer")
        if self.status not in _ALLOWED_STATUS:
            raise ValueError("invalid strategy memory status")
        _validate_conditions(self.applies_when)
        _validate_advice(self.advice)
        if len(self.supporting_episode_ids) < 3:
            raise ValueError("at least three supporting episodes are required")
        if len(set(self.supporting_episode_ids)) != len(self.supporting_episode_ids):
            raise ValueError("supporting episode IDs must be unique")
        for episode_id in self.supporting_episode_ids:
            if not _safe_episode_id(episode_id):
                raise ValueError("supporting episode IDs must not contain vessel or UAV IDs")
        metrics = dict(self.supporting_metrics)
        for key, value in metrics.items():
            if not isinstance(key, str) or not key:
                raise ValueError("supporting metric names must be non-empty strings")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("supporting metrics must be numeric")
            if not _finite(float(value)):
                raise ValueError("supporting metrics must be finite")
        object.__setattr__(self, "applies_when", dict(self.applies_when))
        object.__setattr__(self, "supporting_episode_ids", tuple(self.supporting_episode_ids))
        object.__setattr__(self, "supporting_metrics", metrics)


@dataclass(frozen=True)
class ValidationReport:
    report_id: str
    memory_id: str
    baseline_version: str
    validation_episode_pairs: tuple[tuple[str, str], ...]
    holdout_episode_pairs: tuple[tuple[str, str], ...]
    mean_score_gain: float | None
    component_deltas: dict[str, float]
    type_ii_misclassified_as_type_i_delta: float | None
    passed: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("report_id", "memory_id", "baseline_version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("validation_episode_pairs", "holdout_episode_pairs", "reasons"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        for pair in (*self.validation_episode_pairs, *self.holdout_episode_pairs):
            if len(pair) != 2 or not all(isinstance(item, str) and item for item in pair):
                raise ValueError("episode pairs must contain two non-empty IDs")
        for name in ("mean_score_gain", "type_ii_misclassified_as_type_i_delta"):
            value = getattr(self, name)
            if value is not None and not _finite(float(value)):
                raise ValueError(f"{name} must be finite or None")
        deltas = dict(self.component_deltas)
        if any(not isinstance(key, str) or not key for key in deltas):
            raise ValueError("component delta names must be non-empty strings")
        if any(not _finite(float(value)) for value in deltas.values()):
            raise ValueError("component deltas must be finite")
        object.__setattr__(self, "component_deltas", deltas)


class StrategyMemoryStore:
    """Persist candidates and immutable version manifests under one root."""

    def __init__(self, output_dir: str | Path = "outputs/strategy_memory") -> None:
        self.output_dir = Path(output_dir)
        self.manifest_path = self.output_dir / "manifest.json"
        self._lock = RLock()

    def propose(
        self,
        outcomes: tuple[EpisodeOutcome, ...],
        decision_summaries: tuple[dict, ...],
        *,
        advice: str | None = None,
    ) -> StrategyMemory | None:
        """Propose one inactive candidate from at least three live outcomes."""
        outcomes = tuple(outcomes)
        summaries = tuple(decision_summaries)
        if len(outcomes) < 3 or len(summaries) < 3 or len(outcomes) != len(summaries):
            return None
        if any(not isinstance(outcome, EpisodeOutcome) for outcome in outcomes):
            return None
        if any(not isinstance(outcome, EpisodeOutcome) or not outcome.valid for outcome in outcomes):
            return None
        if len({outcome.episode_id for outcome in outcomes}) < 3:
            return None
        if any("fixture" in outcome.episode_id.lower() for outcome in outcomes):
            return None
        if any(not isinstance(summary, dict) or not _safe_summary(summary) for summary in summaries):
            return None

        conditions = _common_conditions(summaries)
        selected_advice = advice
        if selected_advice is None:
            selected_advice = next(
                (
                    summary.get("advice")
                    for summary in summaries
                    if isinstance(summary.get("advice"), str) and summary["advice"].strip()
                ),
                "Prioritize the oldest feasible probe before low-value coverage.",
            )
        try:
            _validate_advice(selected_advice)
            _validate_conditions(conditions)
        except ValueError:
            return None

        score_values = [outcome.score for outcome in outcomes if outcome.score is not None]
        metrics = {
            "supporting_episode_count": float(len(outcomes)),
            "mean_unique_coverage_ratio": _mean(
                outcome.unique_coverage_ratio for outcome in outcomes
            ),
        }
        if score_values:
            metrics["mean_score"] = _mean(score_values)
        for name, values in (
            ("intent_satisfaction_ratio", (item.intent_satisfaction_ratio for item in outcomes)),
            ("type_ii_tracking_ratio", (item.type_ii_tracking_ratio for item in outcomes)),
            ("classification_accuracy", (item.classification_accuracy for item in outcomes)),
            ("mean_probe_wait_min", (item.mean_probe_wait_min for item in outcomes)),
        ):
            filtered = [value for value in values if value is not None]
            if filtered:
                metrics[f"mean_{name}" if name.endswith("ratio") else name] = _mean(filtered)

        with self._lock:
            manifest = self._read_manifest()
            next_version = 1 + max(
                (int(version) for version in manifest["versions"] if str(version).isdigit()),
                default=0,
            )
            memory_id = f"M{_next_memory_number(manifest):04d}"
        return StrategyMemory(
            memory_id,
            next_version,
            "candidate",
            conditions,
            selected_advice.strip(),
            tuple(outcome.episode_id for outcome in outcomes),
            metrics,
            None,
            datetime.now(timezone.utc).isoformat(),
            manifest.get("active_version", "baseline"),
        )

    @staticmethod
    def is_safe_episode_id(episode_id: str) -> bool:
        """Check the non-sensitive identifier allowed in reviewer evidence."""
        return _safe_episode_id(episode_id)

    @staticmethod
    def is_safe_decision_summary(summary: dict) -> bool:
        """Check a de-identified summary before it reaches an LLM."""
        return isinstance(summary, dict) and _safe_summary(summary)

    def save_candidate(self, memory: StrategyMemory) -> None:
        """Persist a candidate without making it eligible for prompt injection."""
        if not isinstance(memory, StrategyMemory):
            raise TypeError("memory must be a StrategyMemory")
        if memory.status not in {"candidate", "validated"}:
            raise ValueError("save_candidate accepts candidate or validated memories")
        with self._lock:
            manifest = self._read_manifest()
            version_key = str(memory.version)
            entry = manifest["versions"].setdefault(
                version_key,
                {"parent_version": memory.parent_version or manifest.get("active_version", "baseline"), "memories": []},
            )
            serialized = asdict(memory)
            serialized["content_hash"] = _content_hash(memory)
            existing = next(
                (item for item in entry["memories"] if item.get("memory_id") == memory.memory_id),
                None,
            )
            if existing is not None and existing.get("content_hash") != serialized["content_hash"]:
                raise ValueError("memory_id already contains different content")
            if existing is None:
                entry["memories"].append(serialized)
            self._write_manifest(manifest)

    def load_manifest(self, version: str) -> tuple[StrategyMemory, ...]:
        """Load all memories recorded in one version, including candidates."""
        if not isinstance(version, str) or not version:
            raise ValueError("version must be a non-empty string")
        with self._lock:
            entry = self._read_manifest()["versions"].get(version)
            if entry is None:
                return ()
            return tuple(
                _memory_from_record(record)
                for record in sorted(entry.get("memories", ()), key=lambda item: item["memory_id"])
            )

    def find_memory(self, memory_id: str) -> tuple[str, StrategyMemory]:
        """Return the manifest version and memory with one stable ID."""
        if not isinstance(memory_id, str) or not memory_id:
            raise ValueError("memory_id must be a non-empty string")
        with self._lock:
            manifest = self._read_manifest()
            for version, entry in manifest["versions"].items():
                for record in entry.get("memories", ()):
                    if record.get("memory_id") == memory_id:
                        return str(version), _memory_from_record(record)
        raise KeyError(f"unknown memory: {memory_id}")

    def select_for_context(self, context: dict, version: str) -> tuple[StrategyMemory, ...]:
        """Select at most five validated/active memories matching a fixed context."""
        if not isinstance(context, dict) or not isinstance(version, str) or not version:
            return ()
        if not _valid_context(context):
            return ()
        selected = []
        for memory in self.load_manifest(version):
            if memory.status not in {"validated", "active"}:
                continue
            if all(context.get(key) == value for key, value in memory.applies_when.items()):
                selected.append(memory)
        return tuple(selected[:5])

    def save_validation_report(self, report: ValidationReport) -> None:
        if not isinstance(report, ValidationReport):
            raise TypeError("report must be a ValidationReport")
        with self._lock:
            manifest = self._read_manifest()
            reports = manifest.setdefault("reports", {})
            serialized = asdict(report)
            serialized["validation_episode_pairs"] = [
                list(pair) for pair in report.validation_episode_pairs
            ]
            serialized["holdout_episode_pairs"] = [
                list(pair) for pair in report.holdout_episode_pairs
            ]
            previous = reports.get(report.report_id)
            if previous is not None and previous != serialized:
                raise ValueError("report_id already contains different content")
            reports[report.report_id] = serialized
            self._write_manifest(manifest)

    def load_validation_report(self, report_id: str) -> ValidationReport:
        with self._lock:
            record = self._read_manifest().get("reports", {}).get(report_id)
            if record is None:
                raise KeyError(f"unknown validation report: {report_id}")
            return ValidationReport(
                record["report_id"],
                record["memory_id"],
                record["baseline_version"],
                tuple(tuple(pair) for pair in record.get("validation_episode_pairs", ())),
                tuple(tuple(pair) for pair in record.get("holdout_episode_pairs", ())),
                record.get("mean_score_gain"),
                record.get("component_deltas", {}),
                record.get("type_ii_misclassified_as_type_i_delta"),
                bool(record["passed"]),
                tuple(record.get("reasons", ())),
            )

    def validation_reports(self) -> tuple[ValidationReport, ...]:
        """Return stored reports in deterministic ID order for phase merging."""
        with self._lock:
            report_ids = sorted(self._read_manifest().get("reports", {}))
        return tuple(self.load_validation_report(report_id) for report_id in report_ids)

    def active_version(self) -> str:
        with self._lock:
            return str(self._read_manifest().get("active_version", "baseline"))

    def activate(self, memory_id: str, report_id: str) -> str:
        """Activate only a stored passing report with both validation phases."""
        with self._lock:
            report = self.load_validation_report(report_id)
            if report.memory_id != memory_id:
                raise ValueError("validation report does not belong to memory")
            if not report.passed:
                raise ValueError("validation report did not pass")
            if not report.validation_episode_pairs or not report.holdout_episode_pairs:
                raise ValueError("both validation and holdout phases are required")
            manifest = self._read_manifest()
            active_version = str(manifest.get("active_version", "baseline"))
            found = None
            for version, entry in manifest["versions"].items():
                for index, record in enumerate(entry.get("memories", ())):
                    if record.get("memory_id") == memory_id:
                        found = (version, index, record)
                        break
                if found:
                    break
            if found is None:
                raise KeyError(f"unknown memory: {memory_id}")
            version, index, record = found
            memory = _memory_from_record(record)
            if memory.status not in {"candidate", "validated", "active"}:
                raise ValueError("memory is not eligible for activation")
            if active_version != version:
                for old_entry in manifest["versions"].values():
                    for old_index, old_record in enumerate(old_entry.get("memories", ())):
                        if old_record.get("status") != "active":
                            continue
                        old_memory = _memory_from_record(old_record)
                        retired = _memory_with_status(old_memory, "retired")
                        retired_record = asdict(retired)
                        retired_record["content_hash"] = _content_hash(retired)
                        old_entry["memories"][old_index] = retired_record
            activated = _memory_with_status(
                memory, "active", validation_report_id=report_id,
            )
            activated_record = asdict(activated)
            activated_record["content_hash"] = _content_hash(activated)
            manifest["versions"][version]["memories"][index] = activated_record
            manifest["active_version"] = version
            self._write_manifest(manifest)
            return version

    def rollback(self, version: str) -> str:
        """Select a previous manifest version while retaining all evidence."""
        if not isinstance(version, str) or not version:
            raise ValueError("version must be a non-empty string")
        with self._lock:
            manifest = self._read_manifest()
            if version != "baseline" and version not in manifest["versions"]:
                raise KeyError(f"unknown strategy version: {version}")
            for entry in manifest["versions"].values():
                for index, record in enumerate(entry.get("memories", ())):
                    if record.get("status") != "active":
                        continue
                    retired = _memory_with_status(_memory_from_record(record), "retired")
                    updated = asdict(retired)
                    updated["content_hash"] = _content_hash(retired)
                    entry["memories"][index] = updated
            manifest["active_version"] = version
            self._write_manifest(manifest)
            return version

    def set_status(
        self,
        memory_id: str,
        status: str,
        *,
        validation_report_id: str | None = None,
    ) -> None:
        if status not in _ALLOWED_STATUS:
            raise ValueError("invalid strategy memory status")
        if status == "active":
            raise ValueError("use activate() to install an active memory version")
        with self._lock:
            manifest = self._read_manifest()
            found = None
            found_version = None
            for version, entry in manifest["versions"].items():
                for index, record in enumerate(entry.get("memories", ())):
                    if record.get("memory_id") == memory_id:
                        found = (version, index, record)
                        found_version = version
                        break
                if found:
                    break
            if found is None:
                raise KeyError(f"unknown memory: {memory_id}")
            version, index, record = found
            memory = _memory_from_record(record)
            updated = StrategyMemory(
                memory.memory_id,
                memory.version,
                status,
                memory.applies_when,
                memory.advice,
                memory.supporting_episode_ids,
                memory.supporting_metrics,
                validation_report_id if validation_report_id is not None else memory.validation_report_id,
                memory.created_at_utc,
                memory.parent_version,
            )
            updated_record = asdict(updated)
            updated_record["content_hash"] = _content_hash(updated)
            manifest["versions"][version]["memories"][index] = updated_record
            if status == "active":
                manifest["active_version"] = found_version
            self._write_manifest(manifest)

    def _read_manifest(self) -> dict:
        if not self.manifest_path.exists():
            return {
                "schema_version": MEMORY_SCHEMA,
                "active_version": "baseline",
                "versions": {},
                "reports": {},
            }
        try:
            value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("strategy memory manifest is unreadable") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != MEMORY_SCHEMA
            or not isinstance(value.get("versions"), dict)
        ):
            raise RuntimeError("strategy memory manifest has an invalid schema")
        value.setdefault("reports", {})
        return value

    def _write_manifest(self, value: dict) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.manifest_path.with_suffix(".tmp")
        encoded = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(self.manifest_path)


def evaluate_paired_outcomes(
    baseline: tuple[EpisodeOutcome, ...],
    candidate: tuple[EpisodeOutcome, ...],
    config=None,
) -> ValidationReport:
    """Evaluate aligned baseline/candidate outcomes with fixed admission gates."""
    baseline = tuple(baseline)
    candidate = tuple(candidate)
    phase = _config_value(config, "phase", "validation")
    reasons: list[str] = []
    if _config_value(config, "baseline_config_hash") is not None and _config_value(config, "candidate_config_hash") is not None:
        if _config_value(config, "baseline_config_hash") != _config_value(config, "candidate_config_hash"):
            reasons.append("config_mismatch")
    if _config_value(config, "baseline_config") is not None and _config_value(config, "candidate_config") is not None:
        if _canonical_config(_config_value(config, "baseline_config")) != _canonical_config(_config_value(config, "candidate_config")):
            reasons.append("config_mismatch")
    if len(baseline) != len(candidate):
        reasons.append("pair_count_mismatch")

    pair_count = min(len(baseline), len(candidate))
    pairs = []
    valid_pairs = []
    for index in range(pair_count):
        left, right = baseline[index], candidate[index]
        if not isinstance(left, EpisodeOutcome) or not isinstance(right, EpisodeOutcome):
            continue
        pairs.append((left.episode_id, right.episode_id))
        fixture = "fixture" in left.episode_id.lower() or "fixture" in right.episode_id.lower()
        if fixture:
            reasons.append("fixture_pair")
            continue
        if not left.valid or not right.valid:
            continue
        valid_pairs.append((left, right))
    if not valid_pairs:
        reasons.append("no_valid_pair")

    score_gains = [right.score - left.score for left, right in valid_pairs
                   if left.score is not None and right.score is not None]
    mean_score_gain = _mean(score_gains) if score_gains else None
    if mean_score_gain is None:
        reasons.append("score_missing")
    elif mean_score_gain < 0.02:
        reasons.append("score_regression")

    deltas: dict[str, float] = {}
    for name in (
        "unique_coverage_ratio", "intent_satisfaction_ratio",
        "type_ii_tracking_ratio", "classification_accuracy",
    ):
        values = [
            getattr(right, name) - getattr(left, name)
            for left, right in valid_pairs
            if getattr(left, name) is not None and getattr(right, name) is not None
        ]
        if values:
            deltas[name] = _mean(values)
            if deltas[name] < -0.02:
                reasons.append(f"{name}_regression")
    accuracy_missing = any(
        left.classification_accuracy is None or right.classification_accuracy is None
        for left, right in valid_pairs
    )
    if accuracy_missing:
        reasons.append("classification_metric_missing")

    type_ii_misclassification_deltas = [
        right.type_ii_misclassified_as_type_i_ratio
        - left.type_ii_misclassified_as_type_i_ratio
        for left, right in valid_pairs
        if (left.type_ii_misclassified_as_type_i_ratio is not None
            and right.type_ii_misclassified_as_type_i_ratio is not None)
    ]
    type_ii_misclassified_as_type_i_delta = (
        _mean(type_ii_misclassification_deltas)
        if type_ii_misclassification_deltas else None
    )
    if (type_ii_misclassified_as_type_i_delta is not None
            and type_ii_misclassified_as_type_i_delta > 1e-12):
        reasons.append("type_ii_misclassification_regression")

    coverage_pairs = [
        (left.terminal_classification_coverage, right.terminal_classification_coverage)
        for left, right in valid_pairs
        if left.terminal_classification_coverage is not None
        and right.terminal_classification_coverage is not None
    ]
    if any(right < left - 1e-12 for left, right in coverage_pairs):
        reasons.append("terminal_coverage_regression")
    if any(
        left.terminal_classification_coverage is None
        or right.terminal_classification_coverage is None
        for left, right in valid_pairs
    ):
        reasons.append("terminal_coverage_missing")

    cost_deltas = [
        right.type_i_probe_cost - left.type_i_probe_cost
        for left, right in valid_pairs
    ]
    wait_deltas = [
        right.mean_probe_wait_min - left.mean_probe_wait_min
        for left, right in valid_pairs
        if left.mean_probe_wait_min is not None and right.mean_probe_wait_min is not None
    ]
    efficient = (
        (bool(cost_deltas) and _mean(cost_deltas) < -1e-12)
        or (bool(wait_deltas) and _mean(wait_deltas) < -1e-12)
    )
    if valid_pairs and not efficient:
        reasons.append("no_efficiency_improvement")
    if phase not in {"validation", "holdout"}:
        reasons.append("invalid_phase")

    # Preserve order while making report reasons deterministic.
    reasons = list(dict.fromkeys(reasons))
    validation_pairs = tuple(pairs) if phase != "holdout" else ()
    holdout_pairs = tuple(pairs) if phase == "holdout" else ()
    return ValidationReport(
        report_id=f"R{uuid4().hex[:10]}",
        memory_id=str(_config_value(config, "memory_id", "candidate")),
        baseline_version=str(_config_value(config, "baseline_version", "baseline")),
        validation_episode_pairs=validation_pairs,
        holdout_episode_pairs=holdout_pairs,
        mean_score_gain=mean_score_gain,
        component_deltas=deltas,
        type_ii_misclassified_as_type_i_delta=type_ii_misclassified_as_type_i_delta,
        passed=not reasons,
        reasons=tuple(reasons),
    )


def _memory_from_record(record: dict) -> StrategyMemory:
    fields = {key: value for key, value in record.items() if key != "content_hash"}
    return StrategyMemory(**fields)


def _memory_with_status(
    memory: StrategyMemory,
    status: str,
    *,
    validation_report_id: str | None = None,
) -> StrategyMemory:
    return StrategyMemory(
        memory.memory_id,
        memory.version,
        status,
        memory.applies_when,
        memory.advice,
        memory.supporting_episode_ids,
        memory.supporting_metrics,
        memory.validation_report_id if validation_report_id is None else validation_report_id,
        memory.created_at_utc,
        memory.parent_version,
    )


def _content_hash(memory: StrategyMemory) -> str:
    value = asdict(memory)
    value.pop("status", None)
    value.pop("validation_report_id", None)
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _next_memory_number(manifest: dict) -> int:
    numbers = []
    for entry in manifest["versions"].values():
        for record in entry.get("memories", ()):
            match = re.fullmatch(r"M(\d+)", str(record.get("memory_id", "")))
            if match:
                numbers.append(int(match.group(1)))
    return max(numbers, default=0) + 1


def _validate_conditions(conditions: dict) -> None:
    if not isinstance(conditions, dict):
        raise ValueError("applies_when must be a mapping")
    for key, value in conditions.items():
        if key not in _ALLOWED_CONDITION_VALUES:
            raise ValueError(f"applies_when contains unsupported key: {key}")
        if value not in _ALLOWED_CONDITION_VALUES[key]:
            raise ValueError(f"applies_when contains unsupported value: {key}")


def _valid_context(context: dict) -> bool:
    try:
        _validate_conditions(context)
    except (TypeError, ValueError):
        return False
    return True


def _validate_advice(advice: str) -> None:
    if not isinstance(advice, str) or not 1 <= len(advice.strip()) <= 400:
        raise ValueError("advice must contain 1-400 characters")
    lowered = advice.lower()
    if any(marker in lowered for marker in _CODE_MARKERS) or _ID_PATTERN.search(advice):
        raise ValueError("advice contains code or an operational identity")


def _safe_episode_id(episode_id: str) -> bool:
    return (
        isinstance(episode_id, str)
        and bool(_SAFE_EPISODE_ID.fullmatch(episode_id))
        and not _ID_PATTERN.search(episode_id)
    )


def _safe_summary(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _FORBIDDEN_KEYS or (
                normalized != "episode_id"
                and (normalized == "id" or normalized.endswith("_id") or normalized.endswith("_ids"))
            ):
                return False
            if normalized in {"fixture", "is_fixture"} and child is True:
                return False
            if not _safe_summary(child):
                return False
        return True
    if isinstance(value, (list, tuple)):
        return all(_safe_summary(child) for child in value)
    if isinstance(value, str):
        lowered = value.lower()
        return not any(marker in lowered for marker in _CODE_MARKERS) and not _ID_PATTERN.search(value)
    return True


def _common_conditions(summaries: tuple[dict, ...]) -> dict[str, str]:
    contexts = []
    for summary in summaries:
        context = summary.get("context", {})
        if not isinstance(context, dict) or not _valid_context(context):
            continue
        contexts.append(context)
    if not contexts:
        return {}
    keys = set(contexts[0])
    for context in contexts[1:]:
        keys &= set(context)
    return {
        key: contexts[0][key]
        for key in sorted(keys)
        if all(context[key] == contexts[0][key] for context in contexts)
    }


def _mean(values) -> float:
    values = tuple(float(value) for value in values)
    return sum(values) / len(values) if values else 0.0


def _config_value(config, name: str, default=None):
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(name, default)
    return getattr(config, name, default)


def _canonical_config(value) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


__all__ = [
    "MEMORY_SCHEMA",
    "StrategyMemory",
    "StrategyMemoryStore",
    "ValidationReport",
    "evaluate_paired_outcomes",
]
