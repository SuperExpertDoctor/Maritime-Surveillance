"""Water-only vessel reference planning and shared, inertial motion rollout.

Coordinates are continuous grid cells; speeds are knots and time is minutes.
The private execution recipe on a route keeps yaw/speed state and timed controls,
so Ship.advance integrates commands rather than copying geometric waypoints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import TYPE_CHECKING, Literal

import numpy as np

from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.env.dubins import Pose
from src.env.obstacle import Island
from src.mission.contracts import RedMotionParameters

if TYPE_CHECKING:
    from src.env.ship import Ship


def wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2 * math.pi) - math.pi


def clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


@dataclass(frozen=True)
class MotionState:
    pose: Pose
    speed_kn: float
    yaw_rate: float


@dataclass(frozen=True)
class MotionDynamics:
    cell_size_km: float
    max_turn_rate: float
    yaw_time_constant: float
    heading_gain: float
    turn_speed_loss: float
    max_acceleration: float

    def roll(self, state: MotionState, heading: float, speed_kn: float, dt: float) -> MotionState:
        """Pure version of vessel yaw inertia, with bounded acceleration."""
        x, y, old_heading = state.pose
        error = wrap_pi(heading - old_heading)
        commanded_rate = clamp(self.heading_gain * error, self.max_turn_rate)
        response = 1 - math.exp(-dt / self.yaw_time_constant)
        rate = clamp(state.yaw_rate + response * (commanded_rate - state.yaw_rate),
                     self.max_turn_rate)
        delta = .5 * (state.yaw_rate + rate) * dt
        if delta * error > 0 and abs(delta) > abs(error):
            delta, rate = error, 0.
        target_speed = max(0., speed_kn) * (
            1 - self.turn_speed_loss * abs(rate) / self.max_turn_rate)
        speed = max(0., state.speed_kn + clamp(target_speed - state.speed_kn,
                                              self.max_acceleration * dt))
        if speed < 1e-10:
            speed = 0.
        distance = .5 * (state.speed_kn + speed) * 1.852 / 60 / self.cell_size_km * dt
        middle = old_heading + delta / 2
        return MotionState((x + distance * math.cos(middle),
                            y + distance * math.sin(middle),
                            wrap_pi(old_heading + delta)), speed, rate)


@dataclass(frozen=True)
class MotionCommand:
    duration_min: float
    heading_rad: float
    speed_kn: float


@dataclass(frozen=True)
class ShipRoute:
    poses: tuple[Pose, ...]
    generated_at_min: float
    map_version: int
    status: Literal["ready", "blocked"]
    blocked_reason: str | None
    reference_deviation_cells: float
    _commands: tuple[MotionCommand, ...] = field(default=(), repr=False, compare=False)
    _initial_state: MotionState | None = field(default=None, repr=False, compare=False)
    _navigator: ShipNavigator | None = field(default=None, repr=False, compare=False)
    _land_mask: np.ndarray | None = field(default=None, repr=False, compare=False)
    _island_bounds: tuple[tuple[float, float, float, float], ...] = field(default=(), repr=False, compare=False)
    _generation: int | None = field(default=None, repr=False, compare=False)


class ShipNavigator:
    """Bind vessel dynamics, then plan using only the supplied land chart.

    Call install at the actual command installation time. Repeated plan calls
    retain that epoch; a changed command is installed on its first plan call.
    """

    def __init__(self, ship: Ship, astar: AStarNavigator | None = None, *,
                 horizon_min: float = 8., integration_dt_min: float = .1,
                 clearance_cells: float = .1, map_version: int = 0) -> None:
        self.ship = ship
        self.astar = astar if astar is not None else AStarNavigator()
        self.horizon_min = horizon_min
        self.integration_dt_min = integration_dt_min
        self.clearance_cells = clearance_cells
        self.map_version = max(ship._map_version, map_version)
        self.land_mask = ship.land_mask
        self.island_bounds: tuple[tuple[float, float, float, float], ...] = ()

    @property
    def map_version(self) -> int:
        return self.ship._map_version

    @map_version.setter
    def map_version(self, version: int) -> None:
        # Keep the existing version interface, shared by every vessel planner.
        self.ship._map_version = version

    def set_islands(self, obstacles) -> None:
        self.island_bounds = tuple(item.bounds for item in obstacles if isinstance(item, Island))

    def _exit_gate(self, mask):
        endpoint = self.ship.normal_route[-1]
        x, y = endpoint[:2]
        cols, rows = mask.shape
        if not (0 <= x < cols and 0 <= y < rows) or mask[math.floor(x), math.floor(y)]:
            return None
        for axis, coordinate, size in ((0, x, cols), (1, y, rows)):
            if coordinate == 0:
                return axis, -1, 0., endpoint[1 - axis]
            if coordinate == size - 1:
                return axis, 1, float(size), endpoint[1 - axis]
        return None

    def has_exited(self, pose, mask) -> bool:
        gate = self._exit_gate(mask)
        if gate is None:
            return False
        axis, sign, boundary, cross = gate
        outside = pose[axis] >= boundary if sign == 1 else pose[axis] < boundary
        return outside and abs(pose[1 - axis] - cross) < .5

    @staticmethod
    def _intersects_box(start, end, bounds) -> bool:
        """Closed segment/rectangle intersection (no sampling blind spots)."""
        t0, t1 = 0., 1.
        for axis in range(2):
            delta = end[axis] - start[axis]
            low, high = bounds[axis], bounds[axis + 2]
            if abs(delta) < 1e-14:
                if start[axis] < low or start[axis] > high:
                    return False
            else:
                a, b = (low - start[axis]) / delta, (high - start[axis]) / delta
                t0, t1 = max(t0, min(a, b)), min(t1, max(a, b))
                if t0 > t1:
                    return False
        return True

    def segment_is_safe(self, start, end, land_mask: np.ndarray) -> bool:
        cols, rows = land_mask.shape
        if any(not (0 <= p[0] < cols and 0 <= p[1] < rows) and not self.has_exited(p, land_mask)
               for p in (start, end)):
            return False
        if self.has_exited(end, land_mask) and not self.has_exited(start, land_mask):
            axis, _, boundary, cross = self._exit_gate(land_mask)
            ratio = (boundary - start[axis]) / (end[axis] - start[axis])
            crossing = start[1 - axis] + ratio * (end[1 - axis] - start[1 - axis])
            if abs(crossing - cross) >= .5:
                return False
        margin = self.clearance_cells
        for x0, y0, x1, y1 in self.island_bounds:
            if self._intersects_box(start, end, (x0 - margin, y0 - margin, x1 + margin, y1 + margin)):
                return False
        c0 = max(0, math.floor(min(start[0], end[0]) - margin) - 1)
        c1 = min(cols, math.floor(max(start[0], end[0]) + margin) + 1)
        r0 = max(0, math.floor(min(start[1], end[1]) - margin) - 1)
        r1 = min(rows, math.floor(max(start[1], end[1]) + margin) + 1)
        for c, r in np.argwhere(land_mask[c0:c1, r0:r1]):
            c, r = c + c0, r + r0
            if self._intersects_box(start, end, (c - margin, r - margin,
                                                 c + 1 + margin, r + 1 + margin)):
                return False
        return True

    def _inflated_mask(self, mask: np.ndarray) -> np.ndarray:
        # A* uses whole cells: round outward. Rollout checks the exact clearance.
        radius = math.ceil(self.clearance_cells)
        mask = mask.copy()
        for x0, y0, x1, y1 in self.island_bounds:
            mask[max(0, math.floor(x0)):min(mask.shape[0], math.floor(x1) + 1),
                 max(0, math.floor(y0)):min(mask.shape[1], math.floor(y1) + 1)] = True
        padded = np.pad(mask, radius)
        result = mask.copy()
        for dc in range(2 * radius + 1):
            for dr in range(2 * radius + 1):
                result |= padded[dc:dc + mask.shape[0], dr:dr + mask.shape[1]]
        return result

    def install(self, params: RedMotionParameters | None, now_min: float) -> None:
        # Installations (including equal commands/normal mode) supersede every
        # earlier recipe for this vessel, even from another planning navigator.
        self.ship._navigation_generation += 1
        self.ship._navigation_params = params
        self.ship._navigation_installed_at_min = now_min

    def reference_heading(self, params: RedMotionParameters | None,
                          normal_tangent_rad: float, now_min: float) -> float:
        if params is None:
            return normal_tangent_rad
        return normal_tangent_rad + math.radians(params.heading_offset_deg) + math.radians(
            params.zigzag_heading_deg) * math.sin(math.radians(params.phase_deg)
                + 2 * math.pi * (now_min - self.ship._navigation_installed_at_min) / params.zigzag_period_min)

    def plan(self, pose: Pose, params: RedMotionParameters | None,
             normal_tangent_rad: float, now_min: float, land_mask: np.ndarray) -> ShipRoute:
        # Once a chart is bound to the vessel, an external planner's old input
        # cannot replace it. Unbound legacy callers may still supply a chart.
        if self.ship._land_mask is not None:
            land_mask = self.ship.land_mask
        self.land_mask = np.asarray(land_mask, dtype=bool)
        if params != self.ship._navigation_params:
            self.install(params, now_min)
        self.ship._navigation_generation += 1
        initial = MotionState(pose, self.ship.speed_kn, self.ship._yaw_rate_rad_per_min)
        states, commands = [initial], []
        normal_indices = [self.ship._route_index]
        for state, command, normal_index in self._reference_steps(
                initial, params, normal_tangent_rad, now_min, land_mask, self.horizon_min,
                normal_indices[-1]):
            states.append(state)
            commands.append(command)
            normal_indices.append(normal_index)
        mask = np.array(land_mask, dtype=bool, copy=True)
        mask.setflags(write=False)
        repaired = self._repair(states, commands, normal_indices, mask, params,
                                normal_tangent_rad, now_min)
        if repaired is None:
            return self.braking_route(initial, now_min, mask, "no dynamically safe route")
        # Include any downstream reference extension in the deviation baseline.
        reference = tuple(s.pose for s in states)
        states, commands = repaired
        if not self.can_stop(states[-1], mask):
            return self.braking_route(initial, now_min, mask, "insufficient stopping reserve")
        deviation = 0.
        if tuple(s.pose for s in states) != reference:
            deviation = max(min(math.dist(s.pose[:2], p[:2]) for p in reference) for s in states)
        return ShipRoute(tuple(s.pose for s in states), now_min, self.map_version,
                         "ready", None, deviation, tuple(commands), initial, self, mask,
                         self.island_bounds, self.ship._navigation_generation)

    def _reference_steps(self, state, params, tangent, now_min, mask, duration_min,
                         normal_index):
        elapsed = 0.
        while elapsed < duration_min - 1e-10 and not self.has_exited(state.pose, mask):
            dt = min(self.integration_dt_min, duration_min - elapsed)
            heading = self.reference_heading(params, tangent, now_min + elapsed)
            if params is None:
                heading, normal_index = self._normal_guidance(state.pose, normal_index, mask)
            speed = self.ship.normal_speed_kn if params is None else params.speed_kn
            command = MotionCommand(dt, heading, speed)
            state = self.ship.motion_dynamics.roll(state, heading, speed, dt)
            elapsed += dt
            yield state, command, normal_index

    def _normal_guidance(self, pose, index, mask):
        route = self.ship.normal_route
        if len(route) < 2:
            return route[0][2], index
        index = min(index, len(route) - 1)
        while index < len(route) - 1:
            a, b = route[index - 1], route[index]
            if (pose[0] - b[0]) * (b[0] - a[0]) + (pose[1] - b[1]) * (b[1] - a[1]) < 0:
                break
            index += 1
        dynamics = self.ship.motion_dynamics
        lookahead = max(.15, self.ship.normal_speed_kn * 1.852 / 60 / dynamics.cell_size_km
                        * (dynamics.yaw_time_constant + 1 / dynamics.heading_gain))
        target_index = index
        while target_index < len(route) - 1 and math.dist(pose[:2], route[target_index][:2]) < lookahead:
            target_index += 1
        target = route[target_index][:2]
        gate = self._exit_gate(mask)
        if target_index == len(route) - 1 and gate is not None and math.dist(pose[:2], target) < lookahead:
            axis, sign, boundary, cross = gate
            target = (boundary + sign, cross) if axis == 0 else (cross, boundary + sign)
        return math.atan2(target[1] - pose[1], target[0] - pose[0]), index

    def _repair(self, states, commands, normal_indices, mask, params, tangent, now_min, depth=0):
        collision = next((i for i in range(len(commands)) if not self.segment_is_safe(
            states[i].pose, states[i + 1].pose, mask)), None)
        dynamics = self.ship.motion_dynamics
        speed = max(s.speed_kn for s in states) * 1.852 / 60 / dynamics.cell_size_km
        radius = max(.1, speed * (1 / dynamics.max_turn_rate
                                  + dynamics.yaw_time_constant + 1 / dynamics.heading_gain))
        # One shared extension budget per plan, never renewed by recursive repairs.
        # At 18 knots / 10 km cells this covers 6.67 downstream cells. Slow,
        # looping or very long blocked references therefore terminate explicitly.
        elapsed = sum(c.duration_min for c in commands)
        extension = iter(()) if depth else self._reference_steps(
            states[-1], params, tangent, now_min + elapsed, mask, 120., normal_indices[-1])
        if collision is None and depth == 0 and (mask.any() or self.island_bounds):
            # Detect land while a turn can still start outside the inflated A*
            # cells. A short nominal horizon alone loses that approach on replan.
            approach_time = (3 * radius + math.ceil(self.clearance_cells)
                             + self.clearance_cells) / max(speed, 1e-6)
            preview = []
            previous = states[-1]
            while elapsed < approach_time - 1e-10:
                item = next(extension, None)
                if item is None:
                    break
                state, command, normal_index = item
                preview.append(item)
                elapsed += command.duration_min
                if not self.segment_is_safe(previous.pose, state.pose, mask):
                    collision = len(commands) + len(preview) - 1
                    states.extend(s for s, _, _ in preview)
                    commands.extend(c for _, c, _ in preview)
                    normal_indices.extend(index for _, _, index in preview)
                    break
                previous = state
        if collision is None:
            return states, commands  # Discard clear preview; preserve nominal horizon.
        if depth >= 8:
            return None
        anchor = collision
        # Include the approach needed for inertial yaw, keeping the earlier prefix.
        while anchor > 0 and math.dist(states[anchor].pose[:2], states[collision].pose[:2]) < 3 * radius:
            anchor -= 1
        inflated = self._inflated_mask(mask)
        # Only downstream reference points are A* goals, never a snapped safe cell.
        # A rejoin need not be two turn radii beyond the collision: that distance
        # can exceed the entire horizon with default yaw inertia. A* accounts for
        # curvature from the clear approach; physical rollout decides reachability.
        candidates = [j for j in range(collision + 1, len(states))
                      if self.segment_is_safe(states[j].pose, states[j].pose, inflated)]
        if not candidates:
            for state, command, normal_index in extension:
                states.append(state)
                commands.append(command)
                normal_indices.append(normal_index)
                if self.segment_is_safe(state.pose, state.pose, inflated):
                    candidates.append(len(states) - 1)
                    if math.dist(state.pose[:2], states[collision].pose[:2]) >= 2 * radius:
                        break
        if not candidates:
            return None
        # A small deterministic set limits repeat searches in disconnected charts.
        # Prefer the original roomy rejoin when available, without making that
        # distance a prerequisite. Short horizons try their furthest goal first.
        roomy = [j for j in candidates if math.dist(
            states[j].pose[:2], states[collision].pose[:2]) >= 2 * radius]
        choices = roomy or candidates
        candidates = sorted(set((choices[0], choices[len(choices) // 2], choices[-1])),
                            reverse=not bool(roomy))
        for j in candidates:
            try:
                geometry = self.astar.plan_grid(states[anchor].pose, {states[j].pose[:2]},
                                                inflated, r_min=radius,
                                                planning_map_version=self.map_version)
            except PathNotFoundError:
                continue
            detour = self._follow_geometry(states[anchor], geometry, commands[anchor].speed_kn, mask)
            if detour is None:
                continue
            detour_states, detour_commands = detour
            stitched_states = states[:anchor] + detour_states
            stitched_commands = commands[:anchor] + detour_commands
            # Prediction owns its cursor. Keep the approach's progress during
            # the detour, then resume from the selected downstream reference.
            normal_index = normal_indices[j]
            stitched_indices = (normal_indices[:anchor]
                                + [normal_indices[anchor]] * (len(detour_states) - 1)
                                + [normal_index])
            state = stitched_states[-1]
            elapsed = sum(c.duration_min for c in stitched_commands)
            for command in commands[j:]:
                heading = self.reference_heading(params, tangent, now_min + elapsed)
                if params is None:
                    heading, normal_index = self._normal_guidance(state.pose, normal_index, mask)
                command = MotionCommand(command.duration_min, heading, command.speed_kn)
                state = dynamics.roll(state, command.heading_rad, command.speed_kn, command.duration_min)
                stitched_states.append(state)
                stitched_commands.append(command)
                stitched_indices.append(normal_index)
                elapsed += command.duration_min
            repaired = self._repair(stitched_states, stitched_commands, stitched_indices,
                                    mask, params, tangent, now_min, depth + 1)
            if repaired is not None:
                return repaired
        return None

    def can_stop(self, state: MotionState, mask: np.ndarray) -> bool:
        """Check the full braking tail, even when it exceeds the lookahead horizon."""
        heading = state.pose[2]
        while state.speed_kn > 1e-10:
            if self.has_exited(state.pose, mask):
                return True
            candidate = self.ship.motion_dynamics.roll(state, heading, 0., self.integration_dt_min)
            if not self.segment_is_safe(state.pose, candidate.pose, mask):
                return False
            state = candidate
        return True

    def _follow_geometry(self, initial, geometry, speed_kn, mask):
        if len(geometry) < 2:
            return None
        state, states, commands = initial, [initial], []
        dynamics = self.ship.motion_dynamics
        speed = max(speed_kn, initial.speed_kn) * 1.852 / 60 / dynamics.cell_size_km
        lookahead = max(.25, speed * (dynamics.yaw_time_constant + 1 / dynamics.heading_gain))
        length = sum(math.dist(a[:2], b[:2]) for a, b in zip(geometry, geometry[1:]))
        limit = math.ceil((length / max(speed, 1e-6) * 3 + 10) / self.integration_dt_min)
        index = 0
        for _ in range(limit):
            # Progress is local and monotonic, avoiding jumps across concave bends.
            while index < len(geometry) - 2 and math.dist(state.pose[:2], geometry[index + 1][:2]) <= math.dist(state.pose[:2], geometry[index][:2]):
                index += 1
            target_index = index + 1
            while target_index < len(geometry) - 1 and math.dist(state.pose[:2], geometry[target_index][:2]) < lookahead:
                target_index += 1
            target = geometry[target_index]
            heading = math.atan2(target[1] - state.pose[1], target[0] - state.pose[0])
            command = MotionCommand(self.integration_dt_min, heading, speed_kn)
            candidate = dynamics.roll(state, heading, speed_kn, command.duration_min)
            if not self.segment_is_safe(state.pose, candidate.pose, mask):
                return None
            state = candidate
            states.append(state)
            commands.append(command)
            if math.dist(state.pose[:2], geometry[-1][:2]) < max(.12, speed * self.integration_dt_min):
                return states, commands
        return None

    def braking_route(self, initial: MotionState, now_min: float, mask: np.ndarray,
                      reason: str, duration_min: float = 0.) -> ShipRoute:
        state, states, commands = initial, [initial], []
        duration = max(duration_min, self.horizon_min,
                       initial.speed_kn / self.ship.motion_dynamics.max_acceleration)
        elapsed = 0.
        while elapsed < duration - 1e-10:
            dt = min(self.integration_dt_min, duration - elapsed)
            command = MotionCommand(dt, initial.pose[2], 0.)
            candidate = self.ship.motion_dynamics.roll(state, command.heading_rad, 0., dt)
            if not self.segment_is_safe(state.pose, candidate.pose, mask):
                reason += "; insufficient clearance for continued braking"
                break
            state = candidate
            states.append(state)
            commands.append(command)
            elapsed += dt
        return ShipRoute(tuple(s.pose for s in states), now_min, self.map_version,
                         "blocked", reason, 0., tuple(commands), initial, self, mask,
                         self.island_bounds, self.ship._navigation_generation)


__all__ = ["ShipNavigator", "ShipRoute"]
