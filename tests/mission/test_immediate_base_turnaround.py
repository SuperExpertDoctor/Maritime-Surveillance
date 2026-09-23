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
        engine._land_for_refuelling(uav)
        assert uav.status == "idle"
        assert uav.fuel_remaining_pct == 1.0
        assert uav.id in {u.id for u in engine.allocator.sm.get_available_uavs()}
        assert uav.id not in engine._return_base_by_uav
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
