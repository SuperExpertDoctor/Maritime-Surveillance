from __future__ import annotations

import numpy as np

from src.env.simulation import SimulationEngine
from src.env.ais_signal import generate_ais_signal
from src.mission.contracts import EvidenceRecord, PointKernel
from src.mission.llm_gateway import ModelResult
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord


class OfflineGateway:
    def request_json(self, **_kwargs):
        return ModelResult(
            call_id="offline",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )

    def request_text(self, **_kwargs):
        return ModelResult(
            call_id="offline-text",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )


def _engine() -> SimulationEngine:
    return SimulationEngine(
        ConfigLoader.load(), seed=42, llm_gateway=OfflineGateway(),
    )


def force_one_valid_sar_cell(engine: SimulationEngine) -> GridCoord:
    searchable = engine.allocator.sm.get_searchable_mask()
    col, row = map(int, np.argwhere(searchable)[0])
    uav = engine.uavs[0]
    uav.position = GridCoord(col, row)
    uav.status = "searching"
    uav.sensor_mode = "sar"
    uav.sar_imaging = True
    uav.sar_look_direction = "right"
    engine._update_sensors_and_detections(engine.clock.time)
    return GridCoord(col, row)


def seed_scanned_information(engine: SimulationEngine, at_min: float = 0.0) -> int:
    engine.allocator.sm.current_time = at_min
    engine.allocator.sm.scan_cell(GridCoord(1, 1), at_min)
    return engine.allocator.sm.information_version


def advance_without_new_observations(
    engine: SimulationEngine, until_material_decay: bool = True,
) -> dict:
    engine.allocator.sm.cycle = 1
    while True:
        now = engine.clock.tick()
        delta = engine.allocator.sm.information_policy.advance_time(now)
        engine._publish_information_delta(delta, now)
        result, _ = engine.allocator.mission_step(now)
        engine.last_result = result
        if not until_material_decay or delta is not None:
            return result


def test_sar_scan_delta_reaches_trigger_manager():
    engine = _engine()
    force_one_valid_sar_cell(engine)

    engine.step()

    events = engine.allocator.trigger_manager.pending_events_for_test()
    assert any(event["type"] == "information_delta" for event in events)


def test_time_decay_can_trigger_before_periodic_cycle():
    engine = _engine()
    seed_scanned_information(engine, at_min=0.0)

    result = advance_without_new_observations(engine)

    assert result["trigger_type"] == "heavy"
    assert result["llm_cycle"]["trigger_information_version"] > 0


def test_expired_evidence_is_a_heavy_information_event():
    engine = _engine()
    evidence = EvidenceRecord(
        evidence_id="EXP-LOOP-1",
        kind="passive_position",
        source_id="SRC-LOOP-1",
        contact_id=None,
        observed_at_min=0.0,
        expires_at_min=1.0,
        strength=1.0,
        spatial=PointKernel((5.0, 5.0), 1.0),
    )
    engine.allocator.sm.apply_information_facts([evidence], 0.0)
    engine.allocator.sm.cycle = 1
    delta = engine.allocator.sm.information_policy.advance_time(1.0)
    engine._publish_information_delta(delta, 1.0)

    assert engine.allocator.trigger_manager.check(1.0).trigger_type == "heavy"


def test_light_trigger_respects_five_minute_dedup_window():
    engine = _engine()
    engine.allocator.sm.cycle = 1
    manager = engine.allocator.trigger_manager

    manager.notify_event("search_complete", 1.0, uav_id="UAV-1")
    first = manager.check(1.0)
    manager.mark_triggered(first.trigger_type, 1.0)

    manager.notify_event("search_complete", 2.0, uav_id="UAV-1")
    assert manager.check(2.0).trigger_type == "none"

    manager.notify_event("search_complete", 6.1, uav_id="UAV-1")
    assert manager.check(6.1).trigger_type == "light"


def test_ais_off_does_not_append_evidence_and_forced_enable_refreshes_once():
    engine = _engine()
    ship = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    ship.set_ais_enabled(True)
    target_mmsi = generate_ais_signal(ship, 1.0).mmsi
    for vessel in engine.ships:
        if vessel.vessel_class == "type_ii":
            vessel.set_ais_enabled(False)

    engine._last_ais_update = 0.0
    engine._refresh_ais_signals(1.0)
    assert not any(
        record.source_id == target_mmsi
        for record in engine.allocator.sm.information_policy.evidence_store.all_records()
    )

    ship.set_ais_enabled(True)
    engine._ais_force_refresh_ids.add(ship.id)
    engine._refresh_ais_signals(1.0)
    after = engine.allocator.sm.information_policy.evidence_store.all_records()
    assert any(record.source_id == target_mmsi for record in after)
    assert ship.id not in engine._ais_force_refresh_ids
