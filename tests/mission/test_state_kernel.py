"""Unified state kernel: single Fact -> Rule -> EntityState -> Event pathway."""
import pytest

from src.mission.state_kernel import (
    Derivation,
    Fact,
    StateKernel,
)


class _FlagRule:
    """Minimal rule: entity is 'on' while its 'flag' fact is truthy."""

    domain = "demo"

    def evaluate(self, entity_id, facts, prior, now_min):
        active = any(
            fact.channel == "flag" and fact.value for fact in facts.values()
        )
        return Derivation("on" if active else "off", cause_id="flag_eval")


def _kernel() -> StateKernel:
    kernel = StateKernel()
    kernel.register_rule(_FlagRule())
    return kernel


def test_ingest_fact_derives_state_and_emits_transition():
    kernel = _kernel()
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    kernel.ingest_fact(Fact("demo", "e1", "flag", True, 1.0, "sensor"))
    entity = kernel.state("demo", "e1")
    assert entity.state == "on"
    assert entity.revision == 1
    assert entity.cause_id == "flag_eval"
    (transition,) = kernel.transitions()
    assert transition.previous_state == "off"
    assert transition.state == "on"


def test_identical_derivation_does_not_bump_revision_or_emit():
    kernel = _kernel()
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    kernel.ingest_fact(Fact("demo", "e1", "flag", True, 1.0, "s"))
    # Different source id but same derived (state, cause) -> deduped.
    kernel.ingest_fact(Fact("demo", "e1", "flag", True, 2.0, "s2"))
    assert kernel.state("demo", "e1").revision == 1
    assert len(kernel.transitions()) == 1


def test_identical_fact_short_circuits_before_derivation():
    kernel = _kernel()
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    fact = Fact("demo", "e1", "flag", True, 1.0, "s")
    kernel.ingest_fact(fact)
    assert kernel.ingest_fact(fact) is None
    assert len(kernel.transitions()) == 1


def test_subscribers_receive_transitions_in_order():
    kernel = _kernel()
    seen = []
    kernel.subscribe(seen.append)
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    kernel.ingest_fact(Fact("demo", "e1", "flag", True, 1.0, "s"))
    kernel.ingest_fact(Fact("demo", "e1", "flag", False, 2.0, "s"))
    assert [t.state for t in seen] == ["on", "off"]
    assert seen[1].previous_state == "on"
    assert [t.sequence for t in seen] == [0, 1]


def test_emit_transition_is_the_adapter_path_for_external_stores():
    kernel = StateKernel()
    seen = []
    kernel.subscribe(seen.append)
    transition = kernel.emit_transition(
        "contact", "C1", None, "pending", 3.0, "contact_created", revision=0
    )
    assert transition.previous_state is None
    assert transition.revision == 0
    kernel.emit_transition(
        "contact", "C1", "pending", "confirmed", 4.0, "sample_ingest",
        revision=1,
    )
    assert [t.state for t in seen] == ["pending", "confirmed"]


def test_first_fact_materializes_unregistered_entity():
    kernel = _kernel()
    kernel.ingest_fact(Fact("demo", "late", "flag", True, 1.0, "s"))
    entity = kernel.state("demo", "late")
    assert entity.state == "on"
    assert entity.revision == 1


def test_derive_on_unknown_entity_raises_key_error():
    kernel = _kernel()
    with pytest.raises(KeyError):
        kernel.derive("demo", "ghost", 0.0)


def test_remove_clears_entity_state_and_facts():
    kernel = _kernel()
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    kernel.ingest_fact(Fact("demo", "e1", "flag", True, 1.0, "s"))
    kernel.remove("demo", "e1")
    with pytest.raises(KeyError):
        kernel.state("demo", "e1")
    assert kernel.facts("demo", "e1") == {}


def test_ingest_batch_processes_facts_in_order():
    kernel = _kernel()
    kernel.register_entity("demo", "e1", "off", 0.0, "init")
    for value, at in ((True, 1.0), (False, 2.0)):
        kernel.ingest("demo", "e1", "flag", value, at, "s")
    assert kernel.state("demo", "e1").state == "off"
    assert [t.state for t in kernel.transitions()] == ["on", "off"]
