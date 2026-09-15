"""Deterministic, bounded and starvation-resistant task prompt selection."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CandidatePool:
    candidates: tuple
    unschedulable_cells: tuple[tuple[int, int], ...]
    geometry_version: int
    information_version: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(self, "unschedulable_cells", tuple(
            tuple(int(value) for value in cell) for cell in self.unschedulable_cells
        ))
        if self.geometry_version < 0 or self.information_version < 0:
            raise ValueError("candidate pool versions must be non-negative")


@dataclass(frozen=True)
class PoolCandidate:
    task_id: str
    kind: str
    bbox: tuple[int, int, int, int]
    cells: tuple[tuple[int, int], ...]
    total_value: float
    mean_value: float
    max_value: float
    unseen_fraction: float
    utility: float
    information_version: int

    @property
    def eligible_since_min(self) -> float:
        return 0.0


@dataclass(frozen=True)
class PromptSelection:
    tasks: tuple
    sources: dict[str, str]
    skip_cycles: dict[str, int]
    fairness_bound_cycles: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "tasks", tuple(self.tasks))
        object.__setattr__(self, "sources", dict(self.sources))
        object.__setattr__(self, "skip_cycles", dict(self.skip_cycles))


class PromptWindow:
    """Keep a FIFO fairness queue while selecting useful bounded prompts."""

    _URGENT_KINDS = frozenset({
        "handoff", "probe", "track", "investigation", "direction_search",
    })

    def __init__(self):
        self._fifo: list[str] = []
        self._skip_cycles: dict[str, int] = {}

    @staticmethod
    def fairness_bound_cycles(candidate_count: int, fair_quota: int) -> int:
        if candidate_count < 0 or fair_quota <= 0:
            raise ValueError("candidate_count must be non-negative and fair_quota positive")
        return math.ceil(candidate_count / fair_quota) if candidate_count else 0

    def select(self, tasks, capacity: int, cycle: int = 0) -> PromptSelection:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if isinstance(cycle, bool) or not isinstance(cycle, int) or cycle < 0:
            raise ValueError("cycle must be a non-negative integer")
        normalized = {task.task_id: task for task in tasks if getattr(task, "task_id", None)}
        ordered_ids = sorted(normalized)
        self._fifo = [task_id for task_id in self._fifo if task_id in normalized]
        for task_id in ordered_ids:
            if task_id not in self._fifo:
                self._fifo.append(task_id)
            self._skip_cycles.setdefault(task_id, 0)
        for task_id in tuple(self._skip_cycles):
            if task_id not in normalized:
                del self._skip_cycles[task_id]

        urgent = sorted(
            (task for task in normalized.values()
             if bool(getattr(task, "urgent", False))
             or task.kind in self._URGENT_KINDS
             or str(getattr(task, "task_id", "")).startswith("investigation:")),
            key=lambda task: (-self._priority(task), -self._utility(task), task.task_id),
        )
        selected = []
        sources: dict[str, str] = {}
        for task in urgent[:capacity]:
            selected.append(task)
            sources[task.task_id] = "urgent"
        ordinary = [task for task in normalized.values() if task.task_id not in sources]
        remaining = capacity - len(selected)
        if remaining > 0 and ordinary:
            fair_quota = max(1, remaining // 4)
            geography_quota = remaining // 4 if remaining >= 3 else 0
            utility_quota = remaining - fair_quota - geography_quota
            selected_ids = set(sources)

            utility = sorted(
                ordinary,
                key=lambda task: (-self._utility(task), self._eligible(task), task.task_id),
            )
            for task in utility:
                if utility_quota <= 0:
                    break
                if task.task_id in selected_ids:
                    continue
                selected.append(task)
                selected_ids.add(task.task_id)
                sources[task.task_id] = "utility"
                utility_quota -= 1

            fair_taken = 0
            for task_id in self._fifo:
                if fair_taken >= fair_quota or len(selected) >= capacity:
                    break
                if task_id in selected_ids or task_id not in normalized:
                    continue
                selected.append(normalized[task_id])
                selected_ids.add(task_id)
                sources[task_id] = "fair"
                fair_taken += 1

            geo_taken = 0
            buckets: dict[tuple[int, int], list] = {}
            for task in sorted(ordinary, key=lambda item: item.task_id):
                if task.task_id in selected_ids:
                    continue
                bucket = self._bucket(task)
                buckets.setdefault(bucket, []).append(task)
            for bucket in sorted(buckets):
                if geo_taken >= geography_quota or len(selected) >= capacity:
                    break
                task = buckets[bucket][0]
                selected.append(task)
                selected_ids.add(task.task_id)
                sources[task.task_id] = "geography"
                geo_taken += 1

            # If a small capacity or sparse geography left slots unused, fill
            # them deterministically without changing the quota sources.
            for task in utility:
                if len(selected) >= capacity:
                    break
                if task.task_id not in selected_ids:
                    selected.append(task)
                    selected_ids.add(task.task_id)
                    sources[task.task_id] = "utility"

        selected_ids = {task.task_id for task in selected}
        for task_id in normalized:
            if task_id in selected_ids:
                self._skip_cycles[task_id] = 0
            else:
                self._skip_cycles[task_id] += 1
        # Every service path advances the FIFO queue. New tasks stay at the
        # tail and are never promoted by their utility score.
        self._fifo = [task_id for task_id in self._fifo if task_id not in selected_ids]
        self._fifo.extend(task_id for task_id in ordered_ids if task_id in selected_ids)
        fair_quota = max(1, (remaining // 4) if remaining > 0 else 1)
        return PromptSelection(
            tasks=tuple(selected),
            sources=sources,
            skip_cycles={task.task_id: self._skip_cycles[task.task_id] for task in selected},
            fairness_bound_cycles=self.fairness_bound_cycles(len(ordinary), fair_quota),
        )

    @staticmethod
    def _utility(task) -> float:
        value = getattr(task, "utility", 0.0)
        try:
            return float(value) if math.isfinite(float(value)) else 0.0
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _eligible(task) -> float:
        try:
            return float(getattr(task, "eligible_since_min", 0.0))
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _priority(task) -> int:
        return {"high": 2, "medium": 1, "low": 0}.get(
            getattr(task, "priority", "medium"), 0
        )

    @staticmethod
    def _bucket(task) -> tuple[int, int]:
        bbox = getattr(task, "bbox", None)
        if bbox is None:
            return (-1, -1)
        x = (int(bbox[0]) + int(bbox[2]) - 1) // 2
        y = (int(bbox[1]) + int(bbox[3]) - 1) // 2
        return x // 5, y // 5


__all__ = ["CandidatePool", "PoolCandidate", "PromptSelection", "PromptWindow"]
