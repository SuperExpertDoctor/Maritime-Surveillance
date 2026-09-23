"""Seeded blue-vessel arrivals through the existing create-command lifecycle.

Create one OpponentPopulation(config.ship.opponent_population, seed=engine.seed,
start_time=engine.clock.time) per episode. Call tick(engine) on the simulation
thread immediately before engine.apply_pending_vessel_commands(). Recreate on
reset. This module only reports release_queued; vessel_created remains the
engine's authoritative successful-apply event.
"""
from __future__ import annotations

import math
import random
from typing import TYPE_CHECKING

import numpy as np

from src.mission.contracts import VesselCommand
from src.mission.vessel_commands import VesselCommandResult

if TYPE_CHECKING:
    from src.schedule.config_loader import OpponentPopulationConfig


class OpponentPopulation:
    """At most one arrival per due tick; missed intervals never cause a burst.

    max_active counts all non-departed ships, including the initial population
    and manual creations. Pending creates reserve capacity too. A drained own
    command blocks further releases until the existing lifecycle completes it.
    The simulation thread must serialize tick and command application.
    """

    def __init__(self, config: OpponentPopulationConfig, *, seed: int,
                 start_time: float = 0.0):
        self.config = config
        # A separate, namespaced stream never consumes engine/navigation RNG.
        self._rng = random.Random(f"opponent-population:{seed}")
        self._next_release = start_time + self._interval()
        self._sequence = 0
        self._inflight: str | None = None

    def _interval(self) -> float:
        return self._rng.uniform(self.config.interval_min_min,
                                 self.config.interval_max_min)

    def owns_command(self, command_id: str) -> bool:
        """Identify the actual in-flight release, even after the queue drains."""
        return command_id == self._inflight

    def capacity_error(self, ships, vessel_class: str) -> str | None:
        """Shared apply-time limits also govern operator-created vessels."""
        active = [ship for ship in ships if not ship.departed]
        if len(active) >= self.config.max_active:
            return "opponent_capacity_reached"
        limit = getattr(self.config, f"max_active_{vessel_class}", None)
        if limit is not None and sum(
            getattr(ship, "vessel_class", None) == vessel_class for ship in active
        ) >= limit:
            return f"opponent_{vessel_class}_capacity_reached"
        return None

    def tick(self, engine) -> VesselCommandResult | None:
        """Enqueue a due create and return its queued result, or return None."""
        now = engine.clock.time
        if not self.config.enabled or now < self._next_release:
            return None
        self._next_release = now + self._interval()
        if self._inflight is not None:
            result = engine.vessel_commands.get(self._inflight)
            if result is not None and result.status == "queued":
                return None
            self._inflight = None
        active = sum(not ship.departed for ship in engine.ships)
        pending_creates = sum(result.vessel_id is None
                              for result in engine.vessel_commands.pending())
        if active + pending_creates >= self.config.max_active:
            return None
        position = self._position(engine)
        if position is None:
            return None
        vessel_class = ("type_i" if self._rng.random() < self.config.type_i_probability
                        else "type_ii")
        if self.capacity_error(engine.ships, vessel_class):
            vessel_class = "type_ii" if vessel_class == "type_i" else "type_i"
            if self.capacity_error(engine.ships, vessel_class):
                return None
        self._sequence += 1
        command = VesselCommand(
            command_id=f"opponent-release-{self._sequence:08d}",
            episode_id=engine.episode_id, operation="create", vessel_id=None,
            expected_revision=None, vessel_class=vessel_class,
            position_cells=position,
        )
        try:
            result = engine.vessel_commands.enqueue(command)
        except RuntimeError:
            # A full queue is transient; retry on the next randomized interval.
            return None
        self._inflight = command.command_id
        engine.allocator.sm.add_event("opponent_vessel_release_queued", {
            "command_id": command.command_id,
            "episode_id": engine.episode_id,
            "vessel_class": vessel_class,
            "position": list(position),
            "time": now,
        })
        return result

    def _position(self, engine) -> tuple[float, float] | None:
        blocked = np.logical_or(engine.ship_land_mask, engine.obstacle_mask)
        cols, rows = blocked.shape
        # Match create's one-cell boundary margin and sample cell centers.
        cells = np.argwhere(~blocked[1:-1, 1:-1]) + 1
        safe = []
        edge = []
        for cx, cy in cells:
            position = (float(cx) + 0.5, float(cy) + 0.5)
            if any(math.dist(position, uav.float_position) < 5.0
                   for uav in engine.uavs):
                continue
            # Create currently checks spacing against departed vessels as well.
            if any(math.dist(position, ship.float_position) < 1.0
                   for ship in engine.ships):
                continue
            safe.append(position)
            if min(position[0], position[1],
                   cols - position[0], rows - position[1]) <= 3.0:
                edge.append(position)
        candidates = edge or safe
        return self._rng.choice(candidates) if candidates else None
