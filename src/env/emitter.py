"""Environment-only, reproducible radiation burst timelines."""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
import sys

from src.mission.config import EmitterConfig


@dataclass(frozen=True)
class EmissionInterval:
    ship_id: str
    burst_id: str
    start_min: float
    end_min: float


@dataclass(frozen=True)
class EmitterState:
    active: bool
    next_transition_min: float
    burst_id: str | None
    started_at_min: float | None


class RadarEmitter:
    """A continuous-time on/off process with deterministic RNG state."""

    def __init__(self, ship_id: str, *, seed: int, config: EmitterConfig) -> None:
        if not ship_id:
            raise ValueError("ship_id is required")
        self.ship_id = ship_id
        self.config = config
        self._rng = random.Random(seed)
        self._last_advanced_min = 0.0
        self._active = False
        self._active_start_min: float | None = None
        self._active_burst_id: str | None = None
        self._burst_number = 0
        self._next_transition_min = self._sample_silent_wait(0.0)
        self._intervals: list[EmissionInterval] = []

    def _sample_silent_wait(self, start_min: float) -> float:
        u = max(self._rng.random(), sys.float_info.min)
        return start_min - self.config.mean_silent_interval_min * math.log(u)

    def _sample_duration(self) -> float:
        low, high = self.config.burst_duration_min
        return self._rng.uniform(low, high)

    @property
    def state(self) -> EmitterState:
        return EmitterState(
            active=self._active,
            next_transition_min=self._next_transition_min,
            burst_id=self._active_burst_id,
            started_at_min=self._active_start_min,
        )

    @property
    def intervals(self) -> tuple[EmissionInterval, ...]:
        return tuple(self._intervals)

    def advance(self, end_min: float) -> tuple[EmissionInterval, ...]:
        """Advance to an absolute time and return intervals closed by it."""
        if not math.isfinite(end_min) or end_min < self._last_advanced_min:
            raise ValueError("emitter time must be finite and monotonic")
        emitted_before = len(self._intervals)
        while self._next_transition_min <= end_min + 1e-12:
            transition = self._next_transition_min
            if not self._active:
                self._active = True
                self._active_start_min = transition
                self._burst_number += 1
                self._active_burst_id = f"{self.ship_id}:burst-{self._burst_number}"
                self._next_transition_min = transition + self._sample_duration()
            else:
                assert self._active_start_min is not None
                assert self._active_burst_id is not None
                self._intervals.append(
                    EmissionInterval(
                        ship_id=self.ship_id,
                        burst_id=self._active_burst_id,
                        start_min=self._active_start_min,
                        end_min=transition,
                    )
                )
                self._active = False
                self._active_start_min = None
                self._active_burst_id = None
                self._next_transition_min = self._sample_silent_wait(transition)
        self._last_advanced_min = end_min
        return tuple(self._intervals[emitted_before:])

    def current_burst_at(self, at_min: float) -> EmitterState | None:
        if not math.isfinite(at_min) or at_min < 0.0:
            return None
        if self._active and self._active_start_min is not None and at_min >= self._active_start_min:
            return self.state
        for interval in reversed(self._intervals):
            if interval.start_min <= at_min < interval.end_min:
                return EmitterState(
                    active=True,
                    next_transition_min=interval.end_min,
                    burst_id=interval.burst_id,
                    started_at_min=interval.start_min,
                )
        return None

    def is_active(self, at_min: float) -> bool:
        return self.current_burst_at(at_min) is not None


__all__ = ["EmissionInterval", "EmitterState", "RadarEmitter"]
