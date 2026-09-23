from src.mission.llm_gateway import ModelResult
from src.mission.contracts import RedMotionParameters, RedPlan
from src.schedule.config_loader import ConfigLoader
from src.env.simulation import SimulationEngine


class BlockedGateway:
    """A real gateway-shaped failure used to verify fail-closed runtime behavior."""

    def request_json(self, **kwargs):
        return ModelResult(
            call_id="red-blocked",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )

    def request_text(self, **kwargs):
        return ModelResult(
            call_id="text-blocked",
            success=False,
            payload=None,
            errors=("offline",),
            failure_category="transport",
        )


def test_red_failure_does_not_advance_clock_or_move_vessels(monkeypatch):
    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=BlockedGateway())
    target = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    engine.surveillance_stages.set_fact(target.id, "sar", True, 0.0, "fixture-sar")
    uav = engine.uavs[0]
    target._col = float(uav.position.col)
    target._row = float(uav.position.row)
    before = (engine.clock.time, target.float_position)

    engine.step()

    assert engine.clock.time == before[0]
    assert target.float_position == before[1]
    assert engine.runtime_status == "paused_model"
    assert engine.blocked_role == "red_commander"
    assert engine.last_result["action"] == "waiting_manual_retry"
    assert engine.last_result["blocked_reason"] == "offline"


def test_run_stops_after_publishing_one_terminal_model_block_frame():
    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=BlockedGateway())
    target = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    engine.surveillance_stages.set_fact(target.id, "sar", True, 0.0, "fixture-sar")
    uav = engine.uavs[0]
    target._col = float(uav.position.col)
    target._row = float(uav.position.row)
    published = []

    summary = engine.run(
        steps=5,
        on_step=lambda current_engine, result: published.append(
            (current_engine.clock.time, result["trigger_type"])
        ),
    )

    assert summary["steps"] == 0
    assert engine.runtime_status == "paused_model"
    assert published == [(0.0, "model_blocked")]


def test_valid_red_plan_is_installed_before_ship_motion():
    engine = SimulationEngine(ConfigLoader.load(), seed=42, llm_gateway=BlockedGateway())
    target = next(ship for ship in engine.ships if ship.vessel_class == "type_ii")
    engine.surveillance_stages.set_fact(target.id, "sar", True, 0.0, "fixture-sar")
    uav = engine.uavs[0]
    target._navigation_params = None
    uav._col, uav._row = target.float_position
    target_id = target.id
    plan = RedPlan(
        "red-plan/v1",
        "red-0-1",
        3.0,
        (RedMotionParameters(target_id, 12.0, 18.0, 0.0, 10.0, 37.0),),
        "test plan",
    )
    engine.red_commander.decide = lambda snapshot: plan
    engine.allocator.step = lambda current_time: {
        "trigger_type": "none",
        "action": None,
    }

    before = target.float_position
    engine.step()

    assert engine.clock.time == engine.clock.dt_min
    assert target.float_position != before
    assert target.is_evading
    assert target._navigation_params == plan.commands[0]
