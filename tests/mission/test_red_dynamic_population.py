"""Canonical red-side contracts for a dynamic type-II population."""
import json

import pytest

from src.mission.llm_gateway import LLMGateway
from src.schedule.config_loader import ConfigLoader


class JsonTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def red_module():
    from src.mission import red_commander

    return red_commander


def ship(ship_id, stage="detected", vessel_class="type_ii"):
    red = red_module()
    return red.RedShipSnapshot(
        ship_id=ship_id,
        vessel_class=vessel_class,
        surveillance_stage=stage,
        position_cells=(float(len(ship_id)), 4.0),
        heading_deg=30.0,
        speed_kn=18.0,
        normal_tangent_deg=20.0,
        ais_enabled=True,
    )


def snapshot(snapshot_id="S1", now=0.0, active=(('V1', 'detected'),)):
    red = red_module()
    ships = tuple(ship(ship_id, stage=stage) for ship_id, stage in active)
    return red.RedSnapshot(
        snapshot_id=snapshot_id,
        sim_time_min=now,
        ships=ships,
        uavs=(("U1", (2.0, 4.0), (-1.0, 0.0)),),
        active_signature=tuple(active),
        land_mask_version=1,
    )


def plan_payload(snapshot_id, active):
    return {
        "schema_version": "red-plan/v1",
        "snapshot_id": snapshot_id,
        "valid_for_min": 3.0,
        "commands": [
            {
                "ship_id": ship_id,
                "heading_offset_deg": 12.0,
                "speed_kn": 18.0,
                "zigzag_heading_deg": 0.0,
                "zigzag_period_min": 10.0,
                "phase_deg": 37.0,
            }
            for ship_id, _stage in active
        ],
        "notes": "fleet maneuver",
    }


def commander(responses):
    transport = JsonTransport([json.dumps(item) for item in responses])
    gateway = LLMGateway(transport=transport)
    return red_module().RedCommander(gateway, ConfigLoader.load().ship), transport


def test_stage_change_bypasses_periodic_reuse():
    first = snapshot(active=(('V1', 'detected'),))
    second = snapshot("S2", 1.0, (('V1', 'probing'),))
    commander_instance, transport = commander([
        plan_payload("S1", first.active_signature),
        plan_payload("S2", second.active_signature),
    ])

    commander_instance.decide(first)
    installed = commander_instance.decide(second)

    assert len(transport.calls) == 2
    assert commander_instance.installation.active_signature == second.active_signature
    assert installed.snapshot_id == "S2"


def test_response_must_exactly_cover_dynamic_signature():
    current = snapshot(
        active=(('V1', 'detected'), ('V2', 'tracking')),
    )
    commander_instance, _transport = commander([
        plan_payload("S1", (('V1', 'detected'),)),
    ] * 3)

    with pytest.raises(red_module().RedDecisionBlocked, match="active_signature"):
        commander_instance.decide(current)


def test_only_active_type_ii_stages_enter_signature_and_output_has_no_trajectory_fields():
    red = red_module()
    current = red.RedSnapshot(
        snapshot_id="S1",
        sim_time_min=0.0,
        ships=(
            ship("V1", "undetected"),
            ship("V2", "detected"),
            ship("V3", "undetected", "type_i"),
        ),
        uavs=(),
        active_signature=(("V2", "detected"),),
        land_mask_version=1,
    )
    commander_instance, transport = commander([
        plan_payload("S1", current.active_signature),
    ])

    plan = commander_instance.decide(current)
    assert tuple(command.ship_id for command in plan.commands) == ("V2",)
    assert set(vars(plan.commands[0])) == {
        "ship_id",
        "heading_offset_deg",
        "speed_kn",
        "zigzag_heading_deg",
        "zigzag_period_min",
        "phase_deg",
    }
    request = json.loads(transport.calls[0]["messages"][1]["content"])
    assert "trajectory" not in json.dumps(request)
