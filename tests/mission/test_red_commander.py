"""Offline behavior tests for red-only gating and centralized decisions."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import importlib
import json
import math

import pytest

from src.env.ship import Ship
from src.mission.contracts import RedMotionParameters, RedPlan
from src.mission.llm_gateway import LLMGateway
from src.schedule.config_loader import ConfigLoader
from src.schedule.datatypes import GridCoord


def red_module():
    assert importlib.util.find_spec("src.mission.red_commander") is not None, (
        "T04 red commander module is missing"
    )
    return importlib.import_module("src.mission.red_commander")


@pytest.fixture
def ship_config():
    return ConfigLoader.load().ship


@pytest.mark.parametrize("distance,expected", [(1.49, "evasive"), (1.5, "normal"), (1.51, "normal")])
def test_detect_threshold_is_strict(ship_config, distance, expected):
    gate = red_module().ThreatGate(ship_config)
    assert gate.update("V1", "target", distance, 0.0) == expected


@pytest.mark.parametrize("distance,expected", [(2.19, "evasive"), (2.2, "evasive"), (2.21, "recovering")])
def test_clear_threshold_is_strict(ship_config, distance, expected):
    gate = red_module().ThreatGate(ship_config)
    gate.update("V1", "target", 1.0, 0.0)
    assert gate.update("V1", "target", distance, 1.0) == expected


@pytest.mark.parametrize("interruption", [1.0, 2.0, 2.2])
def test_clear_hold_requires_five_continuous_minutes(ship_config, interruption):
    gate = red_module().ThreatGate(ship_config)
    assert gate.update("V1", "target", 1.0, 0.0) == "evasive"
    assert gate.update("V1", "target", 3.0, 1.0) == "recovering"
    assert gate.update("V1", "target", 3.0, 5.99) == "recovering"
    assert gate.update("V1", "target", interruption, 6.0) == "evasive"
    assert gate.update("V1", "target", 3.0, 7.0) == "recovering"
    assert gate.update("V1", "target", 3.0, 11.99) == "recovering"
    assert gate.update("V1", "target", 3.0, 12.0) == "normal"


def test_civilian_gate_is_always_normal_and_ship_states_are_independent(ship_config):
    gate = red_module().ThreatGate(ship_config)
    assert gate.update("V1", "target", 0.0, 0.0) == "evasive"
    for now, distance in enumerate([0.0, 1.0, 3.0, math.inf]):
        assert gate.update("V2", "civilian", distance, float(now)) == "normal"
    assert gate.update("V3", "target", 2.0, 0.0) == "normal"
    assert gate.update("V1", "target", 2.0, 4.0) == "evasive"


@pytest.mark.parametrize(
    "ship_start,ship_end,uav_start,uav_end,expected",
    [
        ((0, 0), (0, 0), (-3, 0), (3, 0), 0.0),
        ((0, 0), (2, 0), (2, 1), (0, 1), 1.0),
        ((0, 0), (1, 1), (3, 4), (4, 5), 5.0),
        ((0, 0), (0, 0), (2, 0), (3, 0), 2.0),
        ((0, 0), (0, 0), (3, 0), (2, 0), 2.0),
        # Geometric paths cross, but the ships are not there simultaneously.
        ((0, 0), (2, 0), (1, -2), (1, 0), math.sqrt(0.5)),
    ],
)
def test_swept_distance_uses_synchronous_relative_segment(
    ship_start, ship_end, uav_start, uav_end, expected,
):
    assert red_module().swept_min_distance_cells(
        ship_start, ship_end, uav_start, uav_end,
    ) == pytest.approx(expected)


def test_swept_flyby_survives_until_next_main_frame(ship_config):
    red = red_module()
    gate = red.ThreatGate(ship_config)
    distance = red.swept_min_distance_cells((0, 0), (0, 0), (-3, 0), (3, 0))
    assert gate.update("V1", "target", 3.0, 0.0) == "normal"
    assert gate.observe_swept_distance("V1", "target", distance, 0.1) is None
    assert gate.update("V1", "target", 3.0, 1.0) == "recovering"
    gate.observe_swept_distance("V1", "target", 2.2, 5.9)
    assert gate.update("V1", "target", 3.0, 6.0) == "recovering"
    assert gate.update("V1", "target", 3.0, 10.99) == "recovering"
    assert gate.update("V1", "target", 3.0, 11.0) == "normal"


@pytest.mark.parametrize("identity", ["target", "civilian"])
def test_set_tracked_changes_only_compatibility_flag(identity):
    ship = Ship("V1", GridCoord(3, 4), 18.0, truth_identity=identity)
    original = deepcopy(vars(ship))
    for tracked in (True, False, True):
        ship.set_tracked(tracked)
        assert vars(ship) == {**original, "_being_tracked": tracked}


@pytest.mark.parametrize("state,distance", [("normal", 3.0), ("evasive", 1.0), ("recovering", 3.0)])
def test_tracking_does_not_change_gate_or_clear_timer(ship_config, state, distance):
    gate = red_module().ThreatGate(ship_config)
    ship = Ship("V1", GridCoord(3, 4), 18.0, truth_identity="target")
    if state != "normal":
        gate.update(ship.id, ship.truth_identity, 1.0, 0.0)
    assert gate.update(ship.id, ship.truth_identity, distance, 1.0) == state
    for tracked in (False, True):
        ship.set_tracked(tracked)
        assert gate.update(ship.id, ship.truth_identity, distance, 2.0) == state
    if state == "recovering":
        assert gate.update(ship.id, ship.truth_identity, distance, 6.0) == "normal"


def snapshot(snapshot_id="S1", now=0.0, active=("V1", "V2")):
    red = red_module()
    ships = tuple(
        red.RedShipSnapshot(
            ship_id=ship_id, identity=identity, position_cells=(float(i), 4.0),
            heading_deg=30.0, speed_kn=18.0, normal_tangent_deg=20.0,
            gate_state="evasive" if ship_id in active else "normal", ais_on=True,
        )
        for i, (ship_id, identity) in enumerate(
            [("V1", "target"), ("V2", "target"), ("V3", "target"), ("C1", "civilian")]
        )
    )
    return red.RedSnapshot(
        snapshot_id=snapshot_id, sim_time_min=now, ships=ships,
        uavs=(("U1", (2.0, 4.0), (-1.0, 0.0)),),
        active_ship_ids=active, land_mask_version=1,
    )


def plan_payload(snapshot_id="S1", active=("V1", "V2"), **changes):
    return {
        "schema_version": "red-plan/v1", "snapshot_id": snapshot_id,
        "valid_for_min": 3.0,
        "commands": [
            {"ship_id": ship_id, "heading_offset_deg": 12.0, "speed_kn": 18.0,
             "zigzag_heading_deg": 0.0, "zigzag_period_min": 10.0, "phase_deg": 37.0}
            for ship_id in active
        ],
        "notes": "fleet maneuver", **changes,
    }


def commander_with(scripted_transport, ship_config, responses, threat_gate=None):
    transport = scripted_transport({
        "red_commander": [json.dumps(r) if isinstance(r, dict) else r for r in responses],
    })
    gateway = LLMGateway(transport=transport)
    if threat_gate is None:
        commander = red_module().RedCommander(gateway, ship_config)
    else:
        commander = red_module().RedCommander(
            gateway, ship_config, threat_gate=threat_gate,
        )
    return commander, gateway, transport


def test_snapshots_are_frozen_contracts():
    current = snapshot()
    with pytest.raises(FrozenInstanceError):
        current.sim_time_min = 4.0
    with pytest.raises(FrozenInstanceError):
        current.ships[0].gate_state = "normal"


def test_two_targets_in_one_frame_receive_one_centralized_plan(scripted_transport, ship_config):
    commander, gateway, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    current = snapshot(now=10.0)
    plan = commander.decide(current)
    assert plan == RedPlan(
        "red-plan/v1", "S1", 3.0,
        tuple(RedMotionParameters(ship_id, 12.0, 18.0, 0.0, 10.0, 37.0) for ship_id in ("V1", "V2")),
        "fleet maneuver",
    )
    assert len(transport.calls) == len(gateway.call_log) == 1
    assert transport.calls[0]["role"] == "red_commander"
    context = json.loads(transport.calls[0]["messages"][1]["content"])
    assert context["snapshot"]["active_ship_ids"] == ["V1", "V2"]
    assert len(context["snapshot"]["ships"]) == 4
    assert context["snapshot"]["uavs"] == [["U1", [2.0, 4.0], [-1.0, 0.0]]]
    assert commander.installation.plan is plan
    assert commander.installation.installed_at_min == 10.0
    assert commander.installation.expires_at_min == 13.0


def test_no_active_targets_means_no_call_or_plan(scripted_transport, ship_config):
    commander, gateway, transport = commander_with(scripted_transport, ship_config, [])
    assert commander.decide(snapshot(active=())) is None
    assert commander.installation is None
    assert transport.calls == gateway.call_log == []


def test_duplicate_delivery_and_new_frame_preserve_install_time_and_phase(scripted_transport, ship_config):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    current = snapshot(now=10.0)
    first = commander.decide(current)
    installation = commander.installation
    assert commander.decide(current) is first
    assert commander.decide(snapshot("S2", 11.0, ("V2", "V1"))) is first
    assert commander.installation is installation
    assert installation.installed_at_min == 10.0
    assert [c.phase_deg for c in installation.plan.commands] == [37.0, 37.0]
    assert len(transport.calls) == 1


def test_expiry_at_exact_deadline_installs_new_plan(scripted_transport, ship_config):
    next_plan = plan_payload("S3")
    next_plan["commands"][0]["phase_deg"] = 115.0
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload(), next_plan])
    first = commander.decide(snapshot(now=10.0))
    assert commander.decide(snapshot("S2", 12.99)) is first
    plan = commander.decide(snapshot("S3", 13.0))
    assert plan.snapshot_id == "S3"
    assert commander.installation.installed_at_min == 13.0
    assert plan.commands[0].phase_deg == 115.0
    assert len(transport.calls) == 2


def test_active_set_change_batches_a_new_complete_plan(scripted_transport, ship_config):
    commander, _, transport = commander_with(
        scripted_transport, ship_config,
        [plan_payload(active=("V1",)), plan_payload("S2", active=("V1", "V2"))],
    )
    commander.decide(snapshot(active=("V1",)))
    plan = commander.decide(snapshot("S2", 1.0))
    assert {c.ship_id for c in plan.commands} == {"V1", "V2"}
    assert plan.snapshot_id == "S2"
    assert len(transport.calls) == 2


def test_recovering_remains_active_without_restarting_plan(scripted_transport, ship_config):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    first = commander.decide(snapshot())
    current = snapshot("S2", 1.0)
    current = replace(current, ships=tuple(
        replace(ship, gate_state="recovering") if ship.ship_id in current.active_ship_ids else ship
        for ship in current.ships
    ))
    assert commander.decide(current) is first
    assert len(transport.calls) == 1


@pytest.mark.parametrize("expired", [False, True])
def test_first_or_expired_failure_blocks_without_synthetic_plan(scripted_transport, ship_config, expired):
    responses = ([plan_payload()] if expired else []) + [TimeoutError("offline timeout")] * 3
    commander, gateway, _ = commander_with(scripted_transport, ship_config, responses)
    if expired:
        commander.decide(snapshot())
    with pytest.raises(red_module().RedDecisionBlocked, match="offline timeout"):
        commander.decide(snapshot("S2", 3.0))
    assert commander.installation is None
    assert gateway.call_log[-1]["failure_category"] == "timeout"


@pytest.mark.parametrize("field,value", [
    ("heading_offset_deg", -90.01), ("heading_offset_deg", 90.01),
    ("speed_kn", 9.99), ("speed_kn", 24.01),
    ("zigzag_heading_deg", -0.01), ("zigzag_heading_deg", 35.01),
    ("zigzag_period_min", 3.99), ("zigzag_period_min", 20.01),
    ("phase_deg", -0.01), ("phase_deg", 360.0),
] + [(field, value)
     for field in ("heading_offset_deg", "speed_kn", "zigzag_heading_deg", "zigzag_period_min", "phase_deg")
     for value in (True, "18", None, math.nan, math.inf, -math.inf)])
def test_invalid_command_number_rejects_whole_batch(scripted_transport, ship_config, field, value):
    payload = plan_payload()
    payload["commands"][1][field] = value
    commander, gateway, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())
    assert commander.installation is None
    assert not gateway.call_log[-1]["success"]
    assert gateway.call_log[-1]["failure_category"] == "validation"


@pytest.mark.parametrize("changes", [
    {"schema_version": "red-plan/v2"}, {"schema_version": True},
    {"snapshot_id": "older-snapshot"}, {"snapshot_id": None},
    {"valid_for_min": 0}, {"valid_for_min": -1}, {"valid_for_min": 3.01},
    {"valid_for_min": True}, {"valid_for_min": "3"}, {"valid_for_min": None},
    {"valid_for_min": math.nan}, {"valid_for_min": math.inf},
    {"notes": []}, {"unexpected": "field"},
    {"commands": None}, {"commands": {}}, {"commands": [None]}, {"commands": ["V1"]},
])
def test_invalid_plan_schema_rejects_whole_batch(scripted_transport, ship_config, changes):
    payload = plan_payload(**changes)
    commander, gateway, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())
    assert commander.installation is None
    assert gateway.call_log[-1]["failure_category"] == "validation"


@pytest.mark.parametrize("level,field", [
    ("plan", field) for field in ("schema_version", "snapshot_id", "valid_for_min", "commands", "notes")
] + [("command", field) for field in (
    "ship_id", "heading_offset_deg", "speed_kn", "zigzag_heading_deg", "zigzag_period_min", "phase_deg",
)])
def test_missing_required_field_is_a_validation_failure(scripted_transport, ship_config, level, field):
    payload = plan_payload()
    del (payload if level == "plan" else payload["commands"][0])[field]
    commander, gateway, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())
    assert commander.installation is None
    assert gateway.call_log[-1]["failure_category"] == "validation"


@pytest.mark.parametrize("command_ids", [
    (), ("V1",), ("V1", "V1"), ("V1", "V2", "V2"),
    ("V1", "V2", "C1"), ("V1", "V2", "V3"), ("V1", "departed"),
    ("V1", None), ("V1", ["V2"]), ("V1", True),
])
def test_commands_exactly_cover_active_targets(scripted_transport, ship_config, command_ids):
    payload = plan_payload(active=command_ids)
    commander, gateway, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())
    assert commander.installation is None
    assert gateway.call_log[-1]["failure_category"] == "validation"


def test_unknown_command_field_is_rejected(scripted_transport, ship_config):
    payload = plan_payload()
    payload["commands"][1]["ais_on"] = False
    commander, _, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())
    assert commander.installation is None


@pytest.mark.parametrize("heading,amplitude,speed", [(0, 0, 18), (7.99, 7.99, 19.99), (-7.99, 0, 16.01)])
def test_each_active_ship_must_receive_a_minimum_maneuver(scripted_transport, ship_config, heading, amplitude, speed):
    payload = plan_payload()
    payload["commands"][1].update(heading_offset_deg=heading, zigzag_heading_deg=amplitude, speed_kn=speed)
    commander, gateway, _ = commander_with(scripted_transport, ship_config, [payload] * 3)
    with pytest.raises(red_module().RedDecisionBlocked, match="maneuver"):
        commander.decide(snapshot())
    assert commander.installation is None
    assert "maneuver" in str(gateway.call_log[-1]["validation_errors"])


@pytest.mark.parametrize("changes", [
    {"heading_offset_deg": 8.0}, {"heading_offset_deg": -8.0},
    {"heading_offset_deg": 0.0, "zigzag_heading_deg": 8.0},
    {"heading_offset_deg": 0.0, "speed_kn": 20.0},
    {"heading_offset_deg": 0.0, "speed_kn": 16.0},
    {"heading_offset_deg": -90.0, "speed_kn": 10.0, "zigzag_heading_deg": 35.0, "zigzag_period_min": 4.0, "phase_deg": 0.0},
    {"heading_offset_deg": 90.0, "speed_kn": 24.0, "zigzag_period_min": 20.0, "phase_deg": 359.999},
])
def test_valid_maneuvers_and_inclusive_bounds_are_accepted_unchanged(scripted_transport, ship_config, changes):
    payload = plan_payload()
    payload["commands"][0].update(changes)
    commander, _, transport = commander_with(scripted_transport, ship_config, [payload])
    current = snapshot()
    # Minimum speed change is relative to normal config, not current evasive speed.
    current = replace(current, ships=(replace(current.ships[0], speed_kn=20.0), *current.ships[1:]))
    plan = commander.decide(current)
    assert plan.commands[0] == RedMotionParameters(**payload["commands"][0])
    assert len(transport.calls) == 1


def test_invalid_batch_can_be_corrected_without_partial_install(scripted_transport, ship_config):
    invalid = plan_payload(active=("V1", "C1"))
    valid = plan_payload()
    commander, gateway, transport = commander_with(scripted_transport, ship_config, [invalid, valid])
    assert commander.decide(snapshot()).commands == tuple(RedMotionParameters(**c) for c in valid["commands"])
    assert len(gateway.call_log) == 1
    assert len(transport.calls) == 2
    correction = json.loads(transport.calls[1]["messages"][-1]["content"])
    assert correction["errors"]
    assert "active_ship_ids" in str(correction["errors"])


def test_prompt_states_schema_authority_motion_semantics_and_config_limits(scripted_transport, ship_config):
    config = replace(ship_config, heading_offset_max_deg=75.0, red_plan_valid_min=2.0)
    commander, _, transport = commander_with(scripted_transport, config, [plan_payload(valid_for_min=2.0)])
    commander.decide(snapshot())
    system, user = transport.calls[0]["messages"]
    for required in ("red-plan/v1", "active_ship_ids", "normal_tangent_deg", "phase_deg", "civilian", "normal", "commands", "valid_for_min"):
        assert required in system["content"]
    constraints = json.loads(user["content"])["constraints"]
    assert constraints["heading_offset_deg"] == [-75.0, 75.0]
    assert constraints["speed_kn"] == [10.0, 24.0]
    assert constraints["zigzag_heading_deg"] == [0.0, 35.0]
    assert constraints["zigzag_period_min"] == [4.0, 20.0]
    assert constraints["max_valid_for_min"] == 2.0
    assert constraints["normal_speed_kn"] == 18.0
    assert constraints["min_evasion_heading_deg"] == 8.0
    assert constraints["min_evasion_speed_delta_kn"] == 2.0


def test_configured_validity_limit_is_enforced(scripted_transport, ship_config):
    config = replace(ship_config, red_plan_valid_min=1.0)
    commander, _, _ = commander_with(scripted_transport, config, [plan_payload()] * 3)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot())


def test_unexpired_plan_survives_failed_scheduled_refresh_without_phase_reset(scripted_transport, ship_config):
    config = replace(ship_config, red_decision_cycle_min=1.0)
    commander, gateway, transport = commander_with(
        scripted_transport, config, [plan_payload()] + [TimeoutError("refresh failed")] * 3,
    )
    first = commander.decide(snapshot())
    installed = commander.installation
    current = snapshot("S2", 1.0)
    assert commander.decide(current) is first
    assert len(gateway.call_log) == 2
    assert gateway.call_log[-1]["failure_category"] == "timeout"
    assert commander.installation is installed
    assert installed.expires_at_min == 3.0
    assert commander.decide(current) is first
    assert commander.decide(snapshot("S3", 1.5)) is first
    assert len(transport.calls) == 4


def test_failed_refresh_never_extends_original_expiry(scripted_transport, ship_config):
    config = replace(ship_config, red_decision_cycle_min=1.0)
    commander, _, _ = commander_with(
        scripted_transport, config, [plan_payload()] + [TimeoutError("refresh failed")] * 6,
    )
    first = commander.decide(snapshot())
    assert commander.decide(snapshot("S2", 1.0)) is first
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot("S3", 3.0))
    assert commander.installation is None


def test_invalid_refresh_keeps_only_the_previous_validated_batch(scripted_transport, ship_config):
    invalid = plan_payload("S2")
    invalid["commands"][1]["speed_kn"] = 100
    commander, gateway, _ = commander_with(
        scripted_transport, replace(ship_config, red_decision_cycle_min=1.0),
        [plan_payload()] + [invalid] * 3,
    )
    first = commander.decide(snapshot())
    assert commander.decide(snapshot("S2", 1.0)) is first
    assert len(gateway.call_log) == 2
    assert gateway.call_log[-1]["failure_category"] == "validation"
    assert commander.installation.plan is first


@pytest.mark.parametrize("new_active", [("V1",), ("V1", "V2", "V3")])
def test_changed_active_set_cannot_reuse_even_unexpired_plan(scripted_transport, ship_config, new_active):
    commander, _, _ = commander_with(
        scripted_transport, ship_config, [plan_payload()] + [TimeoutError("set changed")] * 3,
    )
    commander.decide(snapshot())
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot("S2", 1.0, new_active))
    assert commander.installation is None


@pytest.mark.parametrize("failure", [TimeoutError("reentry failed"), plan_payload()])
def test_clear_and_reentry_require_fresh_snapshot_plan(scripted_transport, ship_config, failure):
    commander, _, _ = commander_with(scripted_transport, ship_config, [plan_payload()] + [failure] * 3)
    commander.decide(snapshot())
    assert commander.decide(snapshot("clear", 1.0, ())) is None
    assert commander.installation is None
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot("reentry", 2.0))
    assert commander.installation is None


def test_between_frame_clear_and_reentry_cannot_reuse_old_installation(
    scripted_transport, ship_config,
):
    config = replace(ship_config, clear_hold_min=0.5)
    gate = red_module().ThreatGate(config)
    commander, gateway, transport = commander_with(
        scripted_transport, config,
        [plan_payload()] + [TimeoutError("reentry failed")] * 3,
        threat_gate=gate,
    )
    assert gate.update("V1", "target", 1.0, 0.0) == "evasive"
    assert gate.update("V2", "target", 1.0, 0.0) == "evasive"
    first = commander.decide(snapshot())
    installed = commander.installation

    gate.observe_swept_distance("V1", "target", 3.0, 0.1)
    assert gate.update("V1", "target", 3.0, 0.1) == "recovering"
    gate.observe_swept_distance("V1", "target", 3.0, 0.6)
    assert gate.update("V1", "target", 3.0, 0.6) == "normal"
    assert commander.installation is None
    gate.observe_swept_distance("V1", "target", 1.0, 0.7)
    assert gate.update("V1", "target", 1.0, 0.7) == "evasive"

    assert installed.plan is first
    assert installed.expires_at_min > 1.0
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot("S2", 1.0))
    assert commander.installation is None
    assert len(gateway.call_log) == 2
    assert len(transport.calls) == 4


def test_clear_one_target_then_reentry_does_not_restore_previous_fleet_plan(scripted_transport, ship_config):
    commander, _, _ = commander_with(
        scripted_transport, ship_config,
        [plan_payload(), plan_payload("S2", ("V1",))] + [plan_payload()] * 3,
    )
    commander.decide(snapshot())
    assert {c.ship_id for c in commander.decide(snapshot("S2", 1.0, ("V1",))).commands} == {"V1"}
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(snapshot("S3", 2.0))
    assert commander.installation is None


@pytest.mark.parametrize("stale", [
    lambda old: old,
    lambda old: replace(old, snapshot_id="unseen-old", sim_time_min=-1.0),
    lambda old: replace(old, snapshot_id="unseen-old", sim_time_min=0.5),
])
def test_stale_snapshot_cannot_restore_plan_after_clear(scripted_transport, ship_config, stale):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    old = snapshot()
    commander.decide(old)
    commander.decide(snapshot("clear", 1.0, ()))
    with pytest.raises(red_module().RedDecisionBlocked, match="snapshot"):
        commander.decide(stale(old))
    assert commander.installation is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize("changes", [
    {"sim_time_min": 4.0}, {"land_mask_version": 2},
    {"uavs": (("U1", (9.0, 9.0), (0.0, 0.0)),)},
])
def test_snapshot_id_cannot_be_reused_for_different_content(scripted_transport, ship_config, changes):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    current = snapshot()
    first = commander.decide(current)
    with pytest.raises(red_module().RedDecisionBlocked, match="snapshot"):
        commander.decide(replace(current, **changes))
    assert commander.installation.plan is first
    assert len(transport.calls) == 1


def test_old_same_time_snapshot_is_stale_after_a_new_snapshot(scripted_transport, ship_config):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    first = snapshot()
    commander.decide(first)
    commander.decide(snapshot("clear", 0.0, ()))
    with pytest.raises(red_module().RedDecisionBlocked, match="snapshot"):
        commander.decide(first)
    assert len(transport.calls) == 1


def test_blocked_same_snapshot_can_be_retried_after_resume(scripted_transport, ship_config):
    commander, _, transport = commander_with(
        scripted_transport, ship_config, [TimeoutError("pause")] * 3 + [plan_payload()],
    )
    current = snapshot(now=9.0)
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(current)
    assert commander.decide(current).snapshot_id == "S1"
    installed = commander.installation
    assert installed.installed_at_min == 9.0
    assert commander.decide(current) is installed.plan
    assert len(transport.calls) == 4


@pytest.mark.parametrize("mutation", [
    lambda s: replace(s, active_ship_ids=("V1",)),
    lambda s: replace(s, active_ship_ids=("V1", "V2", "V2")),
    lambda s: replace(s, active_ship_ids=("V1", "V2", "C1")),
    lambda s: replace(s, active_ship_ids=("V1", "V2", "V3")),
    lambda s: replace(s, active_ship_ids=("V1", "V2", "departed")),
    lambda s: replace(s, active_ship_ids=("V1", ["V2"])),
    lambda s: replace(s, ships=(*s.ships, s.ships[0])),
    lambda s: replace(s, ships=(replace(s.ships[0], identity="civilian"), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], gate_state="normal"), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], gate_state="departed"), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], identity="unknown"), *s.ships[1:])),
    lambda s: replace(s, snapshot_id=""),
    lambda s: replace(s, sim_time_min=math.nan),
    lambda s: replace(s, sim_time_min=True),
    lambda s: replace(s, land_mask_version=True),
    lambda s: replace(s, land_mask_version=-1),
    lambda s: replace(s, ships=(replace(s.ships[0], position_cells=(math.inf, 0.0)), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], heading_deg=math.nan), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], normal_tangent_deg=True), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], speed_kn=-1), *s.ships[1:])),
    lambda s: replace(s, ships=(replace(s.ships[0], ais_on=1), *s.ships[1:])),
    lambda s: replace(s, uavs=(("U1", (0.0, 0.0), (math.nan, 0.0)),)),
    lambda s: replace(s, uavs=(*s.uavs, s.uavs[0])),
])
def test_invalid_snapshot_is_blocked_before_model_call(scripted_transport, ship_config, mutation):
    commander, gateway, transport = commander_with(scripted_transport, ship_config, [])
    with pytest.raises(red_module().RedDecisionBlocked, match="snapshot"):
        commander.decide(mutation(snapshot()))
    assert transport.calls == gateway.call_log == []
    assert commander.installation is None


def test_changed_snapshot_authority_is_checked_even_when_plan_unexpired(scripted_transport, ship_config):
    commander, _, transport = commander_with(scripted_transport, ship_config, [plan_payload()])
    commander.decide(snapshot())
    current = snapshot("S2", 1.0)
    current = replace(current, ships=(replace(current.ships[0], identity="civilian"), *current.ships[1:]))
    with pytest.raises(red_module().RedDecisionBlocked):
        commander.decide(current)
    assert len(transport.calls) == 1
