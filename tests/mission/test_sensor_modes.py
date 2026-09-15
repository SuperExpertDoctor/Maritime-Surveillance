from src.env.uav_entity import UAVEntity
from src.sensor.models import SensorBase, SensorSuite
from src.schedule.datatypes import GridCoord


def test_passive_remains_enabled_during_active_payload_switch():
    uav = UAVEntity("U1", GridCoord(5, 5), 8.0, 120.0)

    uav.request_active_mode("eo")

    assert uav.active_mode == "switching_to_eo"
    assert uav.passive_enabled is True
    assert uav.transition_remaining_min == 0.5
    assert uav.sensor_mode == "off"


def test_active_sensor_switch_finishes_after_half_minute():
    uav = UAVEntity("U1", GridCoord(5, 5), 8.0, 120.0)

    uav.request_active_mode("sar")
    uav.advance_sensor_transition(0.25)
    assert uav.active_mode == "switching_to_sar"
    assert uav.sensor_mode == "off"

    uav.advance_sensor_transition(0.25)
    assert uav.active_mode == "sar"
    assert uav.transition_remaining_min == 0.0
    assert uav.sensor_mode == "sar"


def test_passive_cannot_be_disabled():
    uav = UAVEntity("U1", GridCoord(5, 5), 8.0, 120.0)

    try:
        uav.set_passive_enabled(False)
    except ValueError as exc:
        assert "passive" in str(exc)
    else:
        raise AssertionError("disabling passive sensing must be rejected")


def test_legacy_sensor_suite_does_not_turn_radar_hits_into_discovery():
    suite = SensorSuite(
        SensorBase("SAR", 100.0, 0.0, 0.0),
        SensorBase("EO", 100.0, 0.0, 0.0),
        SensorBase("Radar", 100.0, 1.0, 0.0),
    )

    class Target:
        detected = False
        position = GridCoord(1, 1)

    assert suite.detect(GridCoord(0, 0), [Target()], 1.0) == []
