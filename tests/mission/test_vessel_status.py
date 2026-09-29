from dataclasses import replace

from src.env.simulation import SimulationEngine
from src.mission.contracts import RedMotionParameters, RedPlan
from src.schedule.config_loader import ConfigLoader
from scripts.evaluate_mixed_maritime import _FixtureGateway


def test_installed_maneuver_reason_belongs_only_to_commanded_ship():
    gateway = _FixtureGateway()
    config = ConfigLoader.load()
    config = replace(config, ship=replace(config.ship, population=replace(
        config.ship.population, total_count=2, type_i_ratio=0.0, type_ii_ratio=1.0,
    )))
    engine = SimulationEngine(config, seed=42, llm_gateway=gateway,
                              episode_id="vessel-status")
    targets = [ship for ship in engine.ships if ship.vessel_class == "type_ii"]
    assert len(targets) >= 2
    selected, other = targets[:2]
    engine.surveillance_stages.set_fact(selected.id, "sar", True, 5, "detection")
    engine.surveillance_stages.set_fact(other.id, "sar", True, 5, "detection")
    params = RedMotionParameters(selected.id, 12.0, 13.0, 8.0, 4.0, 90.0, "Avoid UAV near selected ship")
    other_params = RedMotionParameters(other.id, 13.0, 14.0, 8.0, 4.0, 90.0, "Keep clear of southern patrol")

    def decide(snapshot):
        gateway.call_log.extend([
            {"role": "decision_maker", "snapshot_id": snapshot.snapshot_id,
             "success": True, "reason_content": "Unrelated task choice"},
            {"role": "red_commander", "snapshot_id": snapshot.snapshot_id,
             "success": True, "reason_content": "Avoid nearby UAV", "call_id": "red-1"},
        ])
        return RedPlan(snapshot.snapshot_id, 10, (params, other_params), "")

    engine.red_commander.decide = decide
    engine._prepare_red_decision(5)
    engine._publish_vessel_inventory()
    inventory = {item["scenario_entity_id"]: item
                 for item in engine.allocator.sm.get_vessel_inventory()}

    assert inventory[selected.id]["motion_parameters"]["speed_kn"] == 13.0
    assert inventory[selected.id]["motion_reason_content"] == params.reason_content
    assert inventory[selected.id]["motion_plan_id"] == engine._installed_red_plan_id
    assert inventory[other.id]["motion_reason_content"] == other_params.reason_content
    assert "reason_content" not in inventory[selected.id]["motion_parameters"]
    assert inventory[selected.id]["heading_deg"] is not None

    engine.red_commander.decide = lambda snapshot: None
    engine._prepare_red_decision(6)
    engine._publish_vessel_inventory()
    retired = {item["scenario_entity_id"]: item
               for item in engine.allocator.sm.get_vessel_inventory()}
    assert retired[selected.id]["motion_parameters"] is None
    assert retired[selected.id]["motion_reason_content"] is None
    assert retired[other.id]["motion_reason_content"] is None

    # The new command owns its explanation even when no provider-level reason exists.
    engine.red_commander.decide = lambda snapshot: RedPlan(
        snapshot.snapshot_id, 10,
        (RedMotionParameters(selected.id, 13.0, 14.0, 9.0, 5.0, 95.0, "Change course after contact"),), "",
    )
    engine._prepare_red_decision(7)
    active = {item["scenario_entity_id"]: item for item in engine.scenario_vessels()}
    assert active[selected.id]["motion_parameters"]["speed_kn"] == 14.0
    assert active[selected.id]["motion_reason_content"] == "Change course after contact"
