from src.env.emitter import EmissionInterval, RadarEmitter
from src.mission.config import EmitterConfig


def _timeline(parts):
    emitter = RadarEmitter(
        "Ship-1",
        seed=7,
        config=EmitterConfig(
            mean_silent_interval_min=2.0,
            burst_duration_min=(1.0, 1.0),
            source_power_at_reference_db=-40.0,
        ),
    )
    result = []
    for end_min in parts:
        result.extend(emitter.advance(end_min))
    return tuple(result)


def test_emission_timeline_is_independent_of_step_partition():
    whole = _timeline([60.0])
    split = _timeline([float(index) for index in range(1, 61)])
    assert whole == split
    assert all(isinstance(interval, EmissionInterval) for interval in whole)
    assert all(interval.end_min > interval.start_min for interval in whole)


def test_emitter_reports_active_burst_without_revealing_position():
    emitter = RadarEmitter(
        "Ship-2",
        seed=1,
        config=EmitterConfig(mean_silent_interval_min=0.5, burst_duration_min=(1.0, 1.0)),
    )
    emitter.advance(5.0)
    assert emitter.current_burst_at(1.25) is not None
    assert not hasattr(emitter.current_burst_at(1.25), "position_cells")


def test_civilian_ship_does_not_create_research_emitter():
    from src.env.ship import Ship
    from src.schedule.datatypes import GridCoord

    ship = Ship("Ship-3", GridCoord(10, 10), 10.0, truth_identity="civilian")
    assert ship.radar_emitter is None
