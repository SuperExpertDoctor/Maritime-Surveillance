"""Unified state kernel — one pathway for every entity state change.

All entity state in the algorithm follows a single pipeline:

    Fact (an asserted observation or command, immutable)
      -> Rule (a pure derivation registered for a domain)
      -> EntityState (revisioned record; the only legal state representation)
      -> StateTransition (recorded in one log; notified to subscribers)

Domains converge onto this kernel in layers: surveillance stages are fully
fact-driven (register/set_fact/snapshot APIs unchanged), while intent and
contact lifecycles currently route their transitions through
``emit_transition`` so the audit stream is uniform even before those stores
are internalized.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from typing import Callable, Mapping, Protocol, Sequence


@dataclass(frozen=True)
class Fact:
    """One immutable observation or command asserted about an entity."""

    domain: str
    entity_id: str
    channel: str
    value: object
    at_min: float
    source_id: str


@dataclass(frozen=True)
class Derivation:
    """A rule's verdict: next state, the cause behind it, and whether a
    command-style update bumps the revision even when state is unchanged."""

    state: str
    cause_id: str
    bump_revision: bool = False


@dataclass(frozen=True)
class EntityState:
    """The only legal state record: derived, revisioned, causally stamped."""

    domain: str
    entity_id: str
    state: str
    revision: int
    changed_at_min: float
    cause_id: str


@dataclass(frozen=True)
class StateTransition:
    """One entity's state change in the unified log."""

    sequence: int
    domain: str
    entity_id: str
    previous_state: str | None
    state: str
    revision: int
    at_min: float
    cause_id: str


class Rule(Protocol):
    """Pure derivation for one domain: facts + prior state + time -> state."""

    domain: str

    def evaluate(
        self,
        entity_id: str,
        facts: Mapping[str, Fact],
        prior: EntityState | None,
        now_min: float,
    ) -> Derivation | None: ...


def _time(value: object, name: str = "at_min") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name}: expected finite non-negative number")
    number = float(value)
    if not math.isfinite(number) or number < 0.0:
        raise ValueError(f"{name}: expected finite non-negative number")
    return number


class StateKernel:
    """Fact table + rule engine + revision counter + transition log."""

    def __init__(self) -> None:
        self._rules: dict[str, Rule] = {}
        self._facts: dict[str, dict[str, dict[str, Fact]]] = {}
        self._states: dict[str, dict[str, EntityState]] = {}
        self._transitions: list[StateTransition] = []
        self._subscribers: list[Callable[[StateTransition], None]] = []

    # -- registration ----------------------------------------------------

    def register_rule(self, rule: Rule) -> None:
        self._rules[rule.domain] = rule

    def has_rule(self, domain: str) -> bool:
        return domain in self._rules

    def subscribe(self, callback: Callable[[StateTransition], None]) -> None:
        self._subscribers.append(callback)

    def register_entity(
        self,
        domain: str,
        entity_id: str,
        initial_state: str,
        at_min: float,
        cause_id: str,
        facts: Sequence[Fact] = (),
    ) -> EntityState:
        """Assert an entity's existence at revision 0 with its initial facts.

        Registration is not a transition: it establishes the baseline the
        first derivation may move away from.
        """
        now = _time(at_min)
        domain_facts = self._facts.setdefault(domain, {})
        domain_states = self._states.setdefault(domain, {})
        if entity_id in domain_states:
            return domain_states[entity_id]
        entity_facts = domain_facts.setdefault(entity_id, {})
        for fact in facts:
            self._validate_fact(fact, domain=domain, entity_id=entity_id)
            entity_facts[fact.channel] = fact
        state = EntityState(domain, entity_id, initial_state, 0, now, cause_id)
        domain_states[entity_id] = state
        return state

    # -- the single pathway ----------------------------------------------

    def ingest_fact(self, fact: Fact) -> EntityState | None:
        """Assert a fact, re-derive the entity, emit any transition."""
        self._validate_fact(fact)
        entity_facts = self._facts.setdefault(fact.domain, {}).setdefault(
            fact.entity_id, {}
        )
        if entity_facts.get(fact.channel) == fact:
            return None
        entity_facts[fact.channel] = fact
        return self.derive(fact.domain, fact.entity_id, fact.at_min)

    def ingest(
        self,
        domain: str,
        entity_id: str,
        channel: str,
        value: object,
        at_min: float,
        source_id: str,
    ) -> EntityState | None:
        return self.ingest_fact(
            Fact(domain, entity_id, channel, value, _time(at_min), source_id)
        )

    def derive(
        self, domain: str, entity_id: str, now_min: float
    ) -> EntityState | None:
        """Re-run the domain rule for one entity; emit on change."""
        now = _time(now_min, "now_min")
        rule = self._rules.get(domain)
        if rule is None:
            raise KeyError(f"no rule registered for domain {domain!r}")
        entity_states = self._states.get(domain, {})
        prior = entity_states.get(entity_id)
        if prior is None and entity_id not in self._facts.get(domain, {}):
            raise KeyError(entity_id)
        facts = self._facts.get(domain, {}).get(entity_id, {})
        outcome = rule.evaluate(entity_id, facts, prior, now)
        if outcome is None:
            return None
        if prior is not None and not outcome.bump_revision:
            if outcome.state == prior.state and outcome.cause_id == prior.cause_id:
                return None
        revision = 1 if prior is None else prior.revision + 1
        state = EntityState(
            domain, entity_id, outcome.state, revision, now, outcome.cause_id
        )
        self._states.setdefault(domain, {})[entity_id] = state
        self._record(
            domain, entity_id,
            prior.state if prior else None, outcome.state,
            revision, now, outcome.cause_id,
        )
        return state

    # -- adapter path for stores not yet rule-driven ----------------------

    def emit_transition(
        self,
        domain: str,
        entity_id: str,
        previous_state: str | None,
        new_state: str,
        at_min: float,
        cause_id: str,
        revision: int = 0,
    ) -> StateTransition:
        """Record a transition produced by an adapter store.

        The store keeps its own record semantics; the kernel keeps the
        uniform transition stream so every state change shares one shape
        and one clock.
        """
        now = _time(at_min)
        self._states.setdefault(domain, {})[entity_id] = EntityState(
            domain, entity_id, new_state, revision, now, cause_id
        )
        return self._record(
            domain, entity_id, previous_state, new_state, revision, now, cause_id
        )

    # -- reads ------------------------------------------------------------

    def state(self, domain: str, entity_id: str) -> EntityState:
        return self._states[domain][entity_id]

    def state_or_none(
        self, domain: str, entity_id: str
    ) -> EntityState | None:
        return self._states.get(domain, {}).get(entity_id)

    def facts(self, domain: str, entity_id: str) -> Mapping[str, Fact]:
        return self._facts.get(domain, {}).get(entity_id, {})

    def transitions(
        self, domain: str | None = None, entity_id: str | None = None
    ) -> tuple[StateTransition, ...]:
        return tuple(
            item
            for item in self._transitions
            if (domain is None or item.domain == domain)
            and (entity_id is None or item.entity_id == entity_id)
        )

    def remove(self, domain: str, entity_id: str) -> None:
        self._facts.get(domain, {}).pop(entity_id, None)
        self._states.get(domain, {}).pop(entity_id, None)

    # -- internals ---------------------------------------------------------

    def __deepcopy__(self, memo):
        """Snapshots must not clone live wiring.

        Subscribers are bound methods of the owning engine: copying them
        pulls the engine's lock-holding object graph into the frame
        snapshot and crashes on ``_thread.RLock``. The copy keeps rules,
        facts, states and the transition log; its subscriber list restarts
        empty because the live engine rewires delivery on its own kernel,
        never on the copy.
        """
        clone = type(self)()
        memo[id(self)] = clone
        clone._rules = deepcopy(self._rules, memo)
        clone._facts = deepcopy(self._facts, memo)
        clone._states = deepcopy(self._states, memo)
        clone._transitions = deepcopy(self._transitions, memo)
        return clone

    def _record(
        self,
        domain: str,
        entity_id: str,
        previous_state: str | None,
        new_state: str,
        revision: int,
        at_min: float,
        cause_id: str,
    ) -> StateTransition:
        transition = StateTransition(
            sequence=len(self._transitions),
            domain=domain,
            entity_id=entity_id,
            previous_state=previous_state,
            state=new_state,
            revision=revision,
            at_min=at_min,
            cause_id=cause_id,
        )
        self._transitions.append(transition)
        for subscriber in self._subscribers:
            subscriber(transition)
        return transition

    @staticmethod
    def _validate_fact(
        fact: Fact, *, domain: str | None = None, entity_id: str | None = None
    ) -> None:
        if not isinstance(fact, Fact):
            raise TypeError("expected Fact")
        if domain is not None and fact.domain != domain:
            raise ValueError("fact domain does not match entity domain")
        if entity_id is not None and fact.entity_id != entity_id:
            raise ValueError("fact entity_id does not match")
        _time(fact.at_min)


__all__ = [
    "Derivation",
    "EntityState",
    "Fact",
    "Rule",
    "StateKernel",
    "StateTransition",
]

# Domains converged onto the unified stream so far:
#   surveillance  — fact-driven rule (probing/tracking/detected/undetected)
#   contact       — lifecycle adapter (created/pending/confirmed/lost/...)
#   intent        — operator lifecycle adapter (active/cancelled/expired)
#   threat        — ThreatGate hysteresis adapter (normal/evasive/recovering)
#   uav           — canonical three-state adapter (search/tracking/returning)
#   evasion       — episode adapter (confirmed/cleared)
#   scheduler     — trigger adapter (light_trigger/heavy_trigger cycles)
#   blue_plan     — red-commander plan adapter (installed/superseded/ended)
