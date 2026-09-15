from copy import deepcopy
from dataclasses import replace

import pytest

from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.env.simulation import SimulationEngine


class ScriptedTransport:
    def __init__(self, responses_by_role):
        self._responses = {
            role: list(responses) for role, responses in responses_by_role.items()
        }
        self.calls = []

    def complete(self, **kwargs):
        call = deepcopy(kwargs)
        self.calls.append(call)
        role = kwargs["role"]
        responses = self._responses.get(role, [])
        if not responses:
            raise AssertionError(f"no scripted response remaining for role {role}")
        response = responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.fixture
def scripted_transport():
    return ScriptedTransport


class FixtureGateway:
    """Explicit fixture-only failure transport for end-to-end boundaries."""

    def request_json(self, *, role, **_kwargs):
        return ModelResult(
            call_id=f"fixture-{role}",
            success=False,
            payload=None,
            errors=("fixture response unavailable",),
            failure_category="fixture",
        )

    def request_text(self, *, role, **_kwargs):
        return ModelResult(
            call_id=f"fixture-{role}",
            success=False,
            payload=None,
            errors=("fixture response unavailable",),
            failure_category="fixture",
        )


class ScenarioFactory:
    SCENARIOS = (
        "mixed-ais",
        "all-civilian",
        "silent-target",
        "disguised-target",
        "island-confounder",
        "no-resources",
        "intent-overlap",
        "model-failure",
    )

    @staticmethod
    def config(scenario: str):
        config = ConfigLoader.load()
        if scenario == "all-civilian":
            config = replace(
                config,
                ship=replace(config.ship, target_ship_count=0),
            )
        elif scenario == "silent-target":
            config = replace(
                config,
                ship=replace(config.ship, target_ais_on_probability=0.0),
            )
        elif scenario == "island-confounder":
            config = replace(
                config,
                environment=replace(config.environment, island_count_min=1, island_count_max=1),
            )
        elif scenario == "no-resources":
            # The coordinator intentionally rejects an empty ownership set;
            # represent no usable resources with one protected return sortie.
            config = replace(config, uav=replace(config.uav, count_max=1))
        return config

    @classmethod
    def engine(cls, scenario: str, *, seed: int = 42, gateway=None):
        if scenario not in cls.SCENARIOS:
            raise ValueError(f"unknown fixture scenario: {scenario}")
        engine = SimulationEngine(
            cls.config(scenario),
            seed=seed,
            llm_gateway=gateway or FixtureGateway(),
        )
        if scenario == "no-resources":
            uav = engine.uavs[0]
            uav.status = "returning"
            engine.allocator.sm.update_uav_status(
                uav.id, "returning", uav.position,
            )
        return engine


@pytest.fixture
def scenario_factory():
    return ScenarioFactory
