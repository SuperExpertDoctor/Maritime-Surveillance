"""Regression: every entity lifecycle flows through the unified state stream."""
from src.env.simulation import SimulationEngine
from src.schedule.config_loader import ConfigLoader


_SURVEILLANCE_STATES = {"undetected", "detected", "probing", "tracking"}
_UAV_STATES = {"search", "tracking", "returning", "crashed"}
_INTENT_STATES = {"active", "cancelled", "expired"}
_THREAT_STATES = {"normal", "evasive", "recovering"}
_DOMAINS = {
    "surveillance", "contact", "intent", "threat",
    "uav", "evasion", "scheduler", "blue_plan",
}


def _run(steps: int = 30) -> SimulationEngine:
    engine = SimulationEngine(ConfigLoader.load(), seed=42)
    for _ in range(steps):
        engine.step()
    return engine


def test_stream_sequences_are_gapless_and_domain_typed():
    engine = _run()
    transitions = engine.state_kernel.transitions()
    assert transitions, "expected a non-empty unified stream"
    assert [t.sequence for t in transitions] == list(range(len(transitions)))
    assert {t.domain for t in transitions} <= _DOMAINS


def test_every_transition_is_bridged_into_the_event_log():
    engine = _run()
    transitions = engine.state_kernel.transitions()
    events = engine.allocator.sm.get_events_by_type(
        "state_transition", limit=len(transitions) + 10
    )
    assert len(events) == len(transitions)
    for event, transition in zip(events, transitions):
        payload = event["data"]
        assert payload["domain"] == transition.domain
        assert payload["entity_id"] == transition.entity_id
        assert payload["state"] == transition.state


def test_domain_states_stay_within_their_legal_sets():
    engine = _run()
    transitions = engine.state_kernel.transitions()
    legal = {
        "surveillance": _SURVEILLANCE_STATES,
        "uav": _UAV_STATES,
        "intent": _INTENT_STATES,
        "threat": _THREAT_STATES,
    }
    for domain, states in legal.items():
        emitted = {t.state for t in transitions if t.domain == domain}
        assert emitted <= states, f"{domain}: illegal states {emitted - states}"


def test_kernel_current_state_matches_last_emitted_transition():
    engine = _run()
    for uav in engine.uavs:
        emitted = engine.state_kernel.transitions("uav", uav.id)
        assert emitted, f"{uav.id} never emitted a canonical state"
        entity = engine.state_kernel.state("uav", uav.id)
        assert entity.state == emitted[-1].state
        assert entity.state in _UAV_STATES


def test_scheduler_cycles_carry_trigger_reasons():
    engine = _run()
    cycles = engine.state_kernel.transitions("scheduler", "decision_cycle")
    assert cycles, "mission_step never recorded a decision cycle"
    assert all(t.cause_id for t in cycles)
    assert {t.state for t in cycles} <= {
        "heavy_trigger", "light_trigger", "none_trigger",
    }
