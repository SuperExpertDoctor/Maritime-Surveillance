from types import SimpleNamespace

import pytest

from src.mission.config import EvasionConfig
from src.mission.evasion_detector import EvasionDetector


def _ais(times, positions, *, mmsi="AIS-1"):
    return tuple(
        SimpleNamespace(
            mmsi=mmsi,
            timestamp=float(time),
            reported_position=tuple(position),
            reported_speed_kn=10.0,
            reported_heading_deg=0.0,
        )
        for time, position in zip(times, positions)
    )


def _probe_history(*positions, uav_id="U1", contact_id="C1"):
    return tuple(
        SimpleNamespace(
            uav_id=uav_id,
            contact_id=contact_id,
            position_cells=tuple(position),
            observed_at_min=float(index * 1.5),
            operation="track",
        )
        for index, position in enumerate(positions)
    )


@pytest.fixture
def detector():
    return EvasionDetector(
        EvasionConfig(
            enabled=True,
            confirmation_samples=2,
            history_window_min=6.0,
            response_window_min=3.0,
        ),
        cell_size_km=10.0,
    )


def _evasive_track(offset=0.0):
    # The pre-window moves east. The response window turns north-east and
    # increases distance from the observer at (10, 10).
    times = (0.0 + offset, 1.5 + offset, 2.9 + offset,
             3.0 + offset, 4.5 + offset, 6.0 + offset)
    positions = ((12.0, 10.0), (13.0, 10.0), (14.0, 10.0),
                 (14.2, 10.8), (14.6, 11.4), (15.0, 12.0))
    return _ais(times, positions)


def test_ais_track_turning_away_from_assigned_observer_confirms_evasion(detector):
    history = _probe_history((10.0, 10.0), (10.0, 10.0), (10.0, 10.0),
                             (10.0, 10.0), (10.0, 10.0))

    first = detector.evaluate(_evasive_track(), history, now_min=6.0)
    second = detector.evaluate(_evasive_track(), history, now_min=6.1)

    assert first == ()
    assert second[-1].kind == "evasive_maneuver"
    assert second[-1].strength == 1.0
    assert second[-1].episode_started is True


def test_boundary_avoidance_is_not_evasion(detector):
    history = _probe_history((10.0, 10.0), (10.0, 10.0), (10.0, 10.0),
                             (10.0, 10.0), (10.0, 10.0))
    track = _evasive_track()

    assert detector.evaluate(track, history, now_min=6.0,
                             mission_boundary=(0.0, 0.0, 16.0, 16.0)) == ()


def test_detector_requires_window_support_and_assigned_close_uav(detector):
    history = _probe_history((0.0, 0.0), (0.0, 0.0), (0.0, 0.0),
                             (0.0, 0.0), (0.0, 0.0))
    assert detector.evaluate(_evasive_track(), history, now_min=6.0) == ()

    sparse = _ais((0.0, 1.0, 2.0, 3.0, 6.0),
                   ((12.0, 10.0), (13.0, 10.0), (14.0, 10.0),
                    (14.2, 10.8), (15.4, 12.8)))
    close = _probe_history((10.0, 10.0), (10.0, 10.0), (10.0, 10.0),
                           (10.0, 10.0), (10.0, 10.0))
    assert detector.evaluate(sparse, close, now_min=6.0) == ()


def test_detector_deduplicates_episode_and_rearms_after_clear(detector):
    history = _probe_history((10.0, 10.0), (10.0, 10.0), (10.0, 10.0),
                             (10.0, 10.0), (10.0, 10.0))
    assert detector.evaluate(_evasive_track(), history, now_min=6.0) == ()
    confirmed = detector.evaluate(_evasive_track(), history, now_min=6.1)
    assert confirmed[-1].episode_started is True
    assert detector.evaluate(_evasive_track(), history, now_min=6.2)[-1].episode_started is False

    calm = _ais((7.0, 8.5, 10.0), ((15.4, 12.8), (15.6, 12.9), (15.8, 13.0)))
    assert detector.evaluate(calm, history, now_min=10.0) == ()
    assert detector.evaluate(calm, history, now_min=11.0) == ()
    assert detector.evaluate(calm, history, now_min=12.0) == ()
    assert detector.evaluate(calm, history, now_min=13.0) == ()
    assert detector.evaluate(calm, history, now_min=14.0) == ()
    assert detector.evaluate(_evasive_track(offset=9.0), history, now_min=15.0) == ()
    rearmed = detector.evaluate(_evasive_track(offset=9.0), history, now_min=15.1)
    assert rearmed[-1].episode_started is True
