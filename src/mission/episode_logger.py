"""Reproducible, role-separated JSONL episode logs.

The logger is intentionally small: the simulation thread owns the writes and
each episode gets a fresh directory.  The checks here are a last boundary
against accidentally persisting credentials or evaluator-only truth in a
blue-role stream; they are not a replacement for the role-specific builders.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from threading import RLock
from uuid import uuid4


class SensitiveLogError(ValueError):
    """A record contains credentials or data outside its declared domain."""


_DOMAIN_STREAMS = {
    "blue": {"observations", "decisions"},
    "red": {"decisions"},
    "evaluation": {"truth", "outcomes", "handoffs"},
    "intents": {"intents"},
    "frames": {"frames"},
}
_SENSITIVE_KEY_PARTS = (
    "authorization", "api_key", "apikey", "secret", "password", "credential",
)
_BLUE_FORBIDDEN_KEYS = {
    "truth",
    "actual_military",
    "truth_identity",
    "is_military",
    "is_evasive",
    "identity",
    "gate_state",
    "physical_ship_id",
    "ship_truth",
    "red_plan",
    "red_snapshot",
}
_EPISODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class EpisodeLogger:
    """Write one immutable-once-finished episode under a unique directory."""

    def __init__(self, output_dir: str | os.PathLike[str] = "outputs/runs") -> None:
        self.output_dir = Path(output_dir)
        self._lock = RLock()
        self._episode_id: str | None = None
        self._episode_dir: Path | None = None
        self._manifest: dict | None = None
        self._record_counts: dict[str, int] = {}
        self._finished = False

    @staticmethod
    def serialize_outcome(outcome) -> dict:
        """Serialize only the current outcome contract, including derived cost."""
        if not is_dataclass(outcome):
            raise TypeError("outcome must be a dataclass instance")
        payload = asdict(outcome)
        cost = getattr(outcome, "type_i_probe_cost", None)
        if cost is not None:
            payload["type_i_probe_cost"] = cost
        return payload

    @property
    def episode_id(self) -> str | None:
        return self._episode_id

    @property
    def episode_dir(self) -> Path | None:
        return self._episode_dir

    @property
    def manifest_path(self) -> Path | None:
        return None if self._episode_dir is None else self._episode_dir / "manifest.json"

    @property
    def record_counts(self) -> dict[str, int]:
        return dict(self._record_counts)

    def start(self, manifest: dict) -> str:
        """Create an episode directory and atomically publish its manifest."""
        if not isinstance(manifest, dict):
            raise TypeError("manifest must be a mapping")
        with self._lock:
            if self._episode_id is not None:
                raise RuntimeError("episode logger has already started")
            requested_id = manifest.get("episode_id")
            if requested_id is None:
                requested_id = f"episode-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid4().hex[:12]}"
            if not isinstance(requested_id, str) or not _EPISODE_ID_RE.fullmatch(requested_id):
                raise ValueError("episode_id must be a safe non-empty identifier")

            episode_dir = self.output_dir / requested_id
            if episode_dir.exists():
                raise FileExistsError(f"episode directory already exists: {episode_dir}")
            episode_dir.mkdir(parents=True, exist_ok=False)
            for domain in ("blue", "red", "evaluation"):
                (episode_dir / domain).mkdir()

            payload = deepcopy(manifest)
            payload["episode_id"] = requested_id
            payload["started_at_utc"] = datetime.now(timezone.utc).isoformat()
            payload["status"] = "running"
            payload.pop("finished_at_utc", None)
            payload.pop("record_counts", None)
            self._episode_id = requested_id
            self._episode_dir = episode_dir
            self._write_manifest(payload)
            self._manifest = payload
            self._finished = False
            return requested_id

    def append(self, domain: str, stream: str, record: dict) -> None:
        """Append one JSON record to an allowed domain stream."""
        with self._lock:
            self._require_started()
            if self._finished:
                raise RuntimeError("episode logger is already finished")
            path = self.path_for(domain, stream)
            if not isinstance(record, dict):
                raise TypeError("log record must be a mapping")
            payload = deepcopy(record)
            self._check_payload(payload, domain)
            try:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("log record must be finite JSON") from exc
            with path.open("a", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            key = f"{domain}/{stream}"
            self._record_counts[key] = self._record_counts.get(key, 0) + 1

    def append_trace(self, trace: dict) -> None:
        """Append a six-link observation-to-assignment audit trace."""
        if not isinstance(trace, dict):
            raise TypeError("trace must be a mapping")
        required = {
            "observation_id", "evidence_id", "information_version",
            "task_id", "decision_id",
        }
        missing = sorted(required - set(trace))
        if missing:
            raise ValueError(f"trace requires: {', '.join(missing)}")
        status = trace.get("status", "success")
        if status == "success" and not trace.get("assignment_id"):
            raise ValueError("trace requires assignment_id for success")
        if not isinstance(trace["information_version"], int) or trace["information_version"] < 0:
            raise ValueError("trace information_version must be a nonnegative integer")
        payload = deepcopy(trace)
        payload.setdefault("status", status)
        self.append("blue", "decisions", {"audit_trace": payload})

    def append_handoff(self, event: dict) -> None:
        """Append one handoff ledger row; retries reuse the interruption ID."""
        if not isinstance(event, dict):
            raise TypeError("handoff event must be a mapping")
        required = {"interruption_id", "status"}
        missing = sorted(required - set(event))
        if missing:
            raise ValueError(f"handoff requires: {', '.join(missing)}")
        self.append("evaluation", "handoffs", deepcopy(event))

    def finish(self, status: str) -> None:
        """Atomically mark the episode complete without deleting any records."""
        with self._lock:
            self._require_started()
            if self._finished:
                raise RuntimeError("episode logger is already finished")
            if not isinstance(status, str) or not status.strip():
                raise ValueError("finish status must be a non-empty string")
            payload = deepcopy(self._manifest or {})
            payload["status"] = status.strip()
            payload["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
            payload["record_counts"] = dict(sorted(self._record_counts.items()))
            self._write_manifest(payload)
            self._manifest = payload
            self._finished = True

    def path_for(self, domain: str, stream: str) -> Path:
        """Return the canonical path for an allowed stream."""
        self._require_started()
        if domain not in _DOMAIN_STREAMS or stream not in _DOMAIN_STREAMS[domain]:
            raise ValueError(f"unsupported log stream: {domain}/{stream}")
        if domain in {"blue", "red", "evaluation"}:
            return self._episode_dir / domain / f"{stream}.jsonl"
        return self._episode_dir / f"{stream}.jsonl"

    def read_manifest(self) -> dict:
        self._require_started()
        return deepcopy(self._manifest or {})

    def _require_started(self) -> None:
        if self._episode_id is None or self._episode_dir is None:
            raise RuntimeError("episode logger has not started")

    def _write_manifest(self, payload: dict) -> None:
        self._require_episode_dir()
        temporary = self._episode_dir / ".manifest.tmp"
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._episode_dir / "manifest.json")

    def _require_episode_dir(self) -> None:
        if self._episode_dir is None:
            raise RuntimeError("episode logger has not started")

    @classmethod
    def _check_payload(cls, value, domain: str, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if not isinstance(key, str):
                    raise ValueError("log record keys must be strings")
                normalized = key.lower().replace("-", "_")
                if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                    raise SensitiveLogError(f"sensitive log field: {'.'.join((*path, key))}")
                if domain == "blue" and normalized in _BLUE_FORBIDDEN_KEYS:
                    raise SensitiveLogError(f"truth field in blue log: {'.'.join((*path, key))}")
                cls._check_payload(child, domain, (*path, key))
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                cls._check_payload(child, domain, (*path, str(index)))
        elif isinstance(value, str):
            lowered = value.lower()
            if "bearer " in lowered or "api_key=" in lowered:
                raise SensitiveLogError(f"credential-like value in log: {'.'.join(path)}")


__all__ = ["EpisodeLogger", "SensitiveLogError"]
