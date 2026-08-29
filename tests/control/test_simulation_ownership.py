from __future__ import annotations

import pytest

from src.control.common.base import ControllerBase
from src.control.common.contracts import (
    ActionSpec,
    ControlCommand,
    ControlDecision,
    ControlEvent,
    ControlMode,
    ControlOwner,
    OperationMode,
    ObservationSpec,
    SensorMode,
)
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


def _install_deterministic_llm(engine: SimulationEngine) -> None:
    def decide(_sm, _ivt, candidate_result, required_search_regions=0):
        engine.allocator.llm_client.last_interaction = {
            "success": True,
            "attempts": 1,
        }
        return {
            "search_regions": [
                {
                    "id": f"S{index + 1}",
                    "bbox": list(candidate["bbox"]),
                    "priority": "medium",
                }
                for index, candidate in enumerate(
                    candidate_result.candidate_regions[:required_search_regions]
                )
            ],
            "notes": "test",
        }

    engine.allocator.llm_client.decide = decide


def test_default_simulation_starts_heuristic_leases_after_real_scheduler_tick():
    engine = SimulationEngine(ConfigLoader.load(), seed=9)
    _install_deterministic_llm(engine)

    result = engine.step()

    assert result["trigger_type"] == "heavy"
    assert engine.uavs
    assert all(
        engine.control_coordinator.current_lease(uav.id).owner
        is ControlOwner.HEURISTIC
        for uav in engine.uavs
    )


def test_detection_replaces_heuristic_task_without_system_command():
    engine = SimulationEngine(ConfigLoader.load(), seed=9)
    _install_deterministic_llm(engine)
    engine.step()
    uav = engine.uavs[0]
    old = engine.control_coordinator.current_lease(uav.id)

    engine._handle_detection(uav, engine.ships[0], engine.clock.time)
    engine.step()
    new = engine.control_coordinator.current_lease(uav.id)

    assert old.owner is ControlOwner.HEURISTIC
    assert new.owner is ControlOwner.HEURISTIC
    assert new.generation == old.generation + 1
    assert new.controller_id.startswith("tracking:")


class _LearningProbe(ControllerBase):
    def __init__(self, *, invalid: bool = False) -> None:
        self.invalid = invalid
        self._observation_spec = ObservationSpec("control-observation/v1", 11)
        self._action_spec = ActionSpec(-1.0, 1.0, 0.1, 1.0)

    @property
    def control_mode(self) -> ControlMode:
        return ControlMode.BC

    @property
    def observation_spec(self) -> ObservationSpec:
        return self._observation_spec

    @property
    def action_spec(self) -> ActionSpec:
        return self._action_spec

    def act(self, observation) -> ControlDecision:
        command = ControlCommand(
            0.0,
            max(0.1, observation.self_state.speed_cells_min),
            SensorMode.OFF,
            OperationMode.TRANSIT,
            schema_version="bad" if self.invalid else "control-command/v1",
        )
        return ControlDecision(command)


def test_target_event_is_observed_once_without_replacing_learning_lease():
    config = ConfigLoader.load()
    config.control.per_uav["UAV-1"] = "bc"
    engine = SimulationEngine(
        config,
        seed=9,
        control_providers={
            ControlMode.BC: lambda _uav_id: _LearningProbe(),
        },
    )
    uav = engine.uavs[0]
    before = engine.control_coordinator.current_lease(uav.id)
    engine.control_coordinator.queue_event(
        ControlEvent(1, 1.0, "target_found", "test", uav.id, {"contact_id": "G1"})
    )

    result = engine.control_coordinator.step_uav(
        uav,
        current_time=1.0,
        dt_min=1.0,
    )

    assert result.observation.events[0].event_type == "target_found"
    assert engine.control_coordinator.current_lease(uav.id) == before
    assert engine.control_coordinator.operation_mode(uav.id) is OperationMode.TRANSIT


def test_controller_validation_fault_recovers_before_applying_motion():
    config = ConfigLoader.load()
    config.control.per_uav["UAV-1"] = "bc"
    engine = SimulationEngine(
        config,
        seed=9,
        control_providers={
            ControlMode.BC: lambda _uav_id: _LearningProbe(invalid=True),
        },
    )
    uav = engine.uavs[0]
    before = (uav.float_position, uav.remaining_range_cells)

    engine._step_controlled_uav(uav, 1.0)

    lease = engine.control_coordinator.current_lease(uav.id)
    assert lease.owner is ControlOwner.SYSTEM
    assert engine.control_coordinator.operation_mode(uav.id) is OperationMode.RETURN
    assert uav.float_position == pytest.approx(before[0])
    assert uav.remaining_range_cells == pytest.approx(before[1])
