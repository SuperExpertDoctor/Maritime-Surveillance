from dataclasses import replace

from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


def _engine():
    config = ConfigLoader.load()
    config = replace(
        config,
        environment=replace(
            config.environment,
            base_capacity=1,
            island_count_min=0,
            island_count_max=0,
            thunderstorm_count_min=0,
            thunderstorm_count_max=0,
        ),
        uav=replace(config.uav, count_max=2),
    )
    return SimulationEngine(config, seed=42, llm_gateway=object())


def test_default_return_is_available_without_advancing_clock():
    engine = _engine()
    assert engine.config.uav.refuel_time_min == 0
    base = engine.base
    now = engine.clock.time
    for uav in engine.uavs:
        uav.position = base.position
        uav.status = "refueling"
        uav.fuel_remaining_pct = 0.1
        engine._return_base_by_uav[uav.id] = base
        engine._holding_base_by_uav[uav.id] = base
        engine._land_for_refuelling(uav)
        assert uav.status == "idle"
        assert uav.fuel_remaining_pct == 1.0
        assert uav.id in {u.id for u in engine.allocator.sm.get_available_uavs()}
        assert uav.id not in engine._return_base_by_uav
        assert uav.id not in engine._holding_base_by_uav
        assert base.occupancy == 0
    assert engine.clock.time == now
    assert base.refuel_count == 2


def test_same_tick_arrivals_do_not_wait_for_a_base_slot():
    engine = _engine()
    base = engine.base
    for uav in engine.uavs:
        uav.position = base.position
        uav.status = "refueling"
        uav.fuel_remaining_pct = 0.1
        engine._return_base_by_uav[uav.id] = base
    engine._process_refuelling(0.0)
    assert all(u.status == "idle" and u.fuel_remaining_pct == 1.0 for u in engine.uavs)
    assert base.refuel_count == 2
    assert base.occupancy == 0


def test_holding_release_completes_service_in_same_tick():
    engine = _engine()
    base = engine.base
    for uav in engine.uavs:
        uav.position = base.position
        uav.fuel_remaining_pct = 0.1
        uav.start_holding(base.position)
        engine._holding_base_by_uav[uav.id] = base

    engine._process_refuelling(0.0)

    assert all(u.status == "idle" and u.fuel_remaining_pct == 1.0 for u in engine.uavs)
    assert base.refuel_count == 2
    assert base.occupancy == 0
    assert not engine._holding_base_by_uav
    assert not engine._return_base_by_uav


def test_queued_landing_away_from_base_flies_back_before_service():
    from src.control.common.contracts import ControlTask, OperationMode
    from src.schedule.datatypes import BBox, GridCoord

    engine = _engine()
    uav, base = engine.uavs[0], engine.base
    uav.position = GridCoord(base.position.col + 2, base.position.row)
    uav.fuel_remaining_pct = .8
    engine.control_coordinator.start_work(
        uav.id, sortie_number=1, current_time=0., dt_min=1.,
        task=ControlTask("coverage-before-return", OperationMode.COVERAGE, BBox(3, 3, 5, 5)))
    engine._promote_work_controller_to_holding(uav, 0.)
    uav.start_holding(base.position)
    engine._holding_base_by_uav[uav.id] = base
    before = uav.float_position
    engine._process_refuelling(0.)
    assert uav.float_position == before
    assert uav.status == "returning"
    assert engine.control_coordinator.active_task(uav.id).task_type is OperationMode.RETURN
    assert uav.id not in engine._holding_base_by_uav
    assert base.refuel_count == 0
    for tick in range(1000):
        engine._step_controlled_uav(uav, float(tick + 1))
        if base.refuel_count:
            break
    assert uav.fuel_remaining_pct == 1.
    assert base.refuel_count == 1


def test_refueling_status_without_registered_physical_arrival_is_not_service():
    from src.schedule.datatypes import GridCoord

    engine = _engine()
    uav = engine.uavs[0]
    uav.position = GridCoord(15, 15)
    uav.status = "refueling"
    uav.fuel_remaining_pct = .4
    engine._process_refuelling(0.)
    assert uav.fuel_remaining_pct == .4
    assert engine.base.refuel_count == 0


def _airborne_landing_queue():
    from src.control.common.contracts import ControlTask, OperationMode
    from src.schedule.datatypes import BBox, GridCoord

    engine = _engine()
    uav, base = engine.uavs[0], engine.base
    engine.control_coordinator.start_work(
        uav.id, sortie_number=1, current_time=0., dt_min=1.,
        task=ControlTask("coverage-before-return", OperationMode.COVERAGE, BBox(3, 3, 5, 5)))
    engine._promote_work_controller_to_holding(uav, 0.)
    uav.position = GridCoord(base.position.col + 2, base.position.row)
    uav.start_holding(base.position)
    engine._holding_base_by_uav[uav.id] = base
    engine._return_base_by_uav[uav.id] = base
    return engine, uav, base


def test_impossible_queued_return_fails_airframe_without_crashing_mission():
    engine, uav, base = _airborne_landing_queue()
    engine.obstacle_mask[:] = True
    engine.allocator.sm.obstacle_mask[:] = True
    before = uav.float_position
    engine._process_refuelling(0.)
    assert engine._emergency_failures[uav.id] == "no_safe_recovery_path"
    assert uav.status == "failed"
    assert uav.float_position == before
    assert uav.id not in engine._holding_base_by_uav
    assert uav.id not in engine._return_base_by_uav
    assert base.refuel_count == 0


def test_queued_return_requires_route_plus_fuel_reserve():
    engine, uav, base = _airborne_landing_queue()
    engine.config = replace(engine.config, control=replace(engine.config.control,
        safety=replace(engine.config.control.safety,
                       reserve_range_cells=uav.remaining_range_cells + 1.)))
    engine._process_refuelling(0.)
    assert engine._emergency_failures[uav.id] == "no_safe_recovery_path"
    assert uav.status == "failed"
    assert base.refuel_count == 0


def test_queued_return_install_rejection_cleans_reservations_without_teleport(monkeypatch):
    engine, uav, base = _airborne_landing_queue()
    before = uav.float_position

    def reject(*args, **kwargs):
        assert uav.status == "holding"
        assert engine._holding_base_by_uav[uav.id] is base
        raise RuntimeError("return install rejected")

    monkeypatch.setattr(engine.control_coordinator, "assign_system_task", reject)
    engine._process_refuelling(0.)
    assert engine._emergency_failures[uav.id] == "controller_fault"
    assert uav.status == "failed"
    assert uav.float_position == before
    assert uav.id not in engine._holding_base_by_uav
    assert uav.id not in engine._return_base_by_uav
    assert engine.control_coordinator.active_task(uav.id) is None
    assert base.refuel_count == 0
