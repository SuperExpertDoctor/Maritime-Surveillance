"""Shared deterministic scenarios for persistent-coverage evaluation."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import inspect
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.evaluate_mixed_maritime import _FixtureGateway  # noqa: E402
from src.schedule.config_loader import ConfigLoader  # noqa: E402


COVERAGE_SCENARIOS = ("coverage-open-water", "coverage-mixed-weather")


class CoverageFixtureGateway(_FixtureGateway):
    """Fixture model that honors only constraints visible in the prompt."""

    @staticmethod
    def _constraint(snapshot: dict) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        raw = snapshot.get("coverage_constraint")
        if not isinstance(raw, dict):
            return 0, (), ()
        required = raw.get("required_new_search_count", 0)
        required = required if type(required) is int and required > 0 else 0
        must_service = tuple(
            item for item in raw.get("must_service_task_ids", ())
            if isinstance(item, str)
        )
        representatives = tuple(
            item for item in raw.get("representative_task_ids", ())
            if isinstance(item, str)
        )
        return required, must_service, representatives

    def _build_decision_selection(self, snapshot: dict, validate):
        required, must_service, representatives = self._constraint(snapshot)
        if required == 0 and not must_service:
            return super()._build_decision_selection(snapshot, validate)

        candidates = {
            item.get("task_id"): item
            for item in snapshot.get("candidates", ())
            if isinstance(item, dict) and isinstance(item.get("task_id"), str)
        }
        feasible_ids = {
            edge.get("task_id")
            for edge in snapshot.get("feasible_edges", ())
            if isinstance(edge, dict) and isinstance(edge.get("task_id"), str)
        }
        visible = {
            task_id: item for task_id, item in candidates.items()
            if task_id in feasible_ids
        }
        kind_rank = {"investigation": 0, "direction_search": 1, "search": 2}
        ordered = []
        for task_id in (*must_service, *representatives):
            if task_id in visible and task_id not in ordered:
                ordered.append(task_id)
        ordered.extend(
            task_id
            for task_id, item in sorted(
                visible.items(),
                key=lambda pair: (
                    kind_rank.get(pair[1].get("kind"), 3),
                    0 if pair[1].get("priority") == "high" else 1,
                    pair[0],
                ),
            )
            if task_id not in ordered
        )

        selected: list[str] = []
        tolerated = (
            "underutilized_feasible_work:",
            "coverage_floor_not_met",
            "coverage_oldest_not_selected",
        )
        for task_id in ordered:
            proposed = [*selected, task_id]
            payload = self._selection_payload(snapshot, proposed)
            errors = tuple(validate(payload)) if validate else ()
            if not errors or all(error.startswith(tolerated) for error in errors):
                selected = proposed

        payload = self._selection_payload(snapshot, selected)
        errors = tuple(validate(payload)) if validate else ()
        return payload, errors


def coverage_fixture_source_hash() -> str:
    source = "\n".join(
        inspect.getsource(cls) for cls in (_FixtureGateway, CoverageFixtureGateway)
    ).encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def build_coverage_scenario(name: str, *, seed: int, transport: str):
    """Build one unmodified real engine with an explicit model transport."""
    if name not in COVERAGE_SCENARIOS:
        raise ValueError(f"unknown persistent coverage scenario: {name}")
    if transport not in {"fixture", "live"}:
        raise ValueError("transport must be fixture or live")

    from src.env.simulation import SimulationEngine

    config = ConfigLoader.load(str(PROJECT_ROOT / "configs"))
    if name == "coverage-open-water":
        config = replace(
            config,
            environment=replace(
                config.environment,
                island_count_min=0,
                island_count_max=0,
                thunderstorm_count_min=0,
                thunderstorm_count_max=0,
            ),
            ship=replace(
                config.ship,
                population=replace(config.ship.population, total_count=0),
            ),
        )
    gateway = CoverageFixtureGateway() if transport == "fixture" else None
    return SimulationEngine(
        config,
        seed=int(seed),
        llm_gateway=gateway,
        episode_id=f"{name}-seed-{int(seed)}",
    )


__all__ = [
    "COVERAGE_SCENARIOS",
    "CoverageFixtureGateway",
    "build_coverage_scenario",
    "coverage_fixture_source_hash",
]
