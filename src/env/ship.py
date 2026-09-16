"""Independent maritime vessels and deterministic population generation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import hashlib
import math
import random
from typing import TYPE_CHECKING, Iterable, Literal

import numpy as np

from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.env.dubins import Pose
from src.env.obstacle import Island
from src.env.ship_navigation import MotionDynamics, MotionState, ShipNavigator, ShipRoute
from src.env.emitter import RadarEmitter
from src.mission.contracts import VesselClass, ship_rng_manifest
from src.schedule.config_loader import allocate_population
from src.schedule.datatypes import GridCoord

if TYPE_CHECKING:
    from src.schedule.config_loader import AppConfig


@dataclass(frozen=True)
class ShipTruth:
    """Environment/evaluation-only vessel truth."""

    ship_id: str
    vessel_class: VesselClass
    ais_enabled: bool
    normal_route: tuple[Pose, ...]
    activity_schedule: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        if self.vessel_class not in ("unknown", "type_i", "type_ii"):
            raise ValueError("invalid vessel_class")
        if type(self.ais_enabled) is not bool:
            raise TypeError("ais_enabled must be bool")
        if self.vessel_class == "type_i" and not self.ais_enabled:
            raise ValueError("type_i_ais_required")
        object.__setattr__(self, "normal_route", tuple(self.normal_route))
        object.__setattr__(self, "activity_schedule", tuple(self.activity_schedule))

def _replace_truth(truth: ShipTruth, **changes) -> ShipTruth:
    return ShipTruth(
        changes.get("ship_id", truth.ship_id),
        changes.get("vessel_class", truth.vessel_class),
        changes.get("ais_enabled", truth.ais_enabled),
        changes.get("normal_route", truth.normal_route),
        changes.get("activity_schedule", truth.activity_schedule),
    )


class PopulationPlacementError(RuntimeError):
    """Raised when a vessel cannot be placed with a reachable exit route."""

    def __init__(self, ship_index: int, attempt_count: int) -> None:
        self.ship_index = int(ship_index)
        self.attempt_count = int(attempt_count)
        super().__init__(
            f"unable to place ship {self.ship_index} after "
            f"{self.attempt_count} attempts"
        )


class ShipType(str, Enum):
    """Public vessel label; environment truth never changes this value."""

    CARGO = "cargo"


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


class Ship:
    """One independently placed vessel following its own normal route."""

    def __init__(
        self,
        ship_id: str,
        initial_position: GridCoord,
        speed_kn: float,
        cell_size_km: float = 10.0,
        *,
        normal_route: tuple[Pose, ...] = (),
        ais_position_noise_cells: float = 0.05,
        base_heading: float | None = None,
        max_turn_rate_deg_min: float = 12.0,
        yaw_time_constant_min: float = 2.5,
        heading_control_gain_per_min: float = 0.35,
        turn_speed_loss_fraction: float = 0.12,
        max_acceleration_kn_per_min: float = 2.0,
        land_mask: np.ndarray | None = None,
        navigator: AStarNavigator | None = None,
        navigation_horizon_min: float = 8.0,
        integration_dt_min: float = .1,
        navigation_clearance_cells: float = .1,
        radar_emitter=None,
        vessel_class: VesselClass = "type_i",
        ais_enabled: bool = True,
        activity_schedule: tuple[tuple[float, float], ...] = (),
        patrol_route: tuple[Pose, ...] = (),
    ) -> None:
        route = tuple(normal_route)
        heading = (
            float(route[0][2])
            if route
            else (0.0 if base_heading is None else float(base_heading))
        )
        if not route:
            route = ((float(initial_position.col), float(initial_position.row), heading),)
        if type(ais_enabled) is not bool:
            raise TypeError("ais_enabled must be bool")
        if vessel_class not in ("unknown", "type_i", "type_ii"):
            raise ValueError("invalid vessel_class")
        if vessel_class == "type_i" and not ais_enabled:
            raise ValueError("type_i_ais_required")
        self.truth = ShipTruth(
            ship_id, vessel_class, ais_enabled, route,
            tuple(activity_schedule),
        )
        self.id = ship_id
        self.ship_id = ship_id
        self._col, self._row = float(route[0][0]), float(route[0][1])
        self.speed_kn = float(speed_kn)
        self.normal_speed_kn = self.speed_kn
        self.cell_size_km = float(cell_size_km)
        self.speed_cells_per_min = self.speed_kn * 1.852 / 60.0 / cell_size_km
        self.ais_position_noise_cells = float(ais_position_noise_cells)
        self.ship_type = ShipType.CARGO
        self.max_turn_rate_rad_per_min = math.radians(max_turn_rate_deg_min)
        self.yaw_time_constant_min = max(float(yaw_time_constant_min), 1e-3)
        self.heading_control_gain_per_min = max(float(heading_control_gain_per_min), 1e-3)
        self.turn_speed_loss_fraction = _clamp(float(turn_speed_loss_fraction), 0.0, 0.5)
        self._base_heading = heading
        self.heading_rad = heading
        self._yaw_rate_rad_per_min = 0.0
        self._route_index = 1 if len(route) > 1 else len(route)
        self.patrol_route = tuple(patrol_route) if patrol_route else route
        self.closed_route = bool(patrol_route)
        self.survey_route: tuple[Pose, ...] = ()
        self._active_activity = "transit"
        self.ais_signal = None
        self.radar_emitter = radar_emitter
        self.estimated_position: tuple[float, float] | None = None
        self.departed = False
        self._detected = False
        self._being_tracked = False
        self.trail: list[tuple[float, float]] = []
        self.motion_dynamics = MotionDynamics(
            self.cell_size_km, self.max_turn_rate_rad_per_min,
            self.yaw_time_constant_min, self.heading_control_gain_per_min,
            self.turn_speed_loss_fraction, max_acceleration_kn_per_min)
        self._land_mask = None if land_mask is None else np.array(land_mask, dtype=bool, copy=True)
        self._map_version = 0
        self._navigation_params = None
        self._navigation_installed_at_min = 0.
        self._navigation_options = (navigation_horizon_min, integration_dt_min, navigation_clearance_cells)
        self._navigator = (None if navigator is None else ShipNavigator(
            self, navigator, horizon_min=navigation_horizon_min,
            integration_dt_min=integration_dt_min, clearance_cells=navigation_clearance_cells))
        self._motion_time_min = 0.
        self._navigation_generation = 0
        self.navigation_status = "ready"
        self.blocked_reason: str | None = None

    @property
    def navigator(self) -> ShipNavigator:
        if self._navigator is None:
            horizon, dt, clearance = self._navigation_options
            self._navigator = ShipNavigator(self, horizon_min=horizon,
                                            integration_dt_min=dt, clearance_cells=clearance)
        return self._navigator

    @property
    def land_mask(self) -> np.ndarray:
        # Standalone legacy vessels have an empty default chart until bound.
        return np.zeros((30, 30), bool) if self._land_mask is None else self._land_mask

    @land_mask.setter
    def land_mask(self, mask: np.ndarray) -> None:
        self._land_mask = np.array(mask, dtype=bool, copy=True)
        self._map_version += 1
        if self._navigator is not None:
            self._navigator.land_mask = self._land_mask

    @property
    def contact_id(self) -> str:
        return self.id

    @property
    def group_id(self) -> str:
        """Read-only alias for legacy frame consumers; ships have no groups."""
        return self.contact_id

    @property
    def vessel_class(self) -> VesselClass:
        return self.truth.vessel_class

    @property
    def ais_enabled(self) -> bool:
        return self.truth.ais_enabled

    def set_ais_enabled(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise TypeError("ais_enabled must be bool")
        if self.vessel_class == "type_i" and not enabled:
            raise ValueError("type_i_ais_required")
        self.truth = replace(self.truth, ais_enabled=enabled)

    @property
    def activity(self) -> Literal["unknown"]:
        # Activity is an environment truth and is deliberately not serialized
        # into blue-side observations. Runtime activity state is added later.
        return "unknown"

    def activity_state_at(self, at_min: float) -> Literal["transit", "survey"]:
        """Return hidden environment activity for evaluator-side use only."""
        if self.vessel_class != "type_ii":
            return "transit"
        for start_min, duration_min in self.truth.activity_schedule:
            if float(start_min) <= float(at_min) < float(start_min) + float(duration_min):
                return "survey"
        return "transit"

    def _set_activity_for_time(self, at_min: float) -> None:
        activity = self.activity_state_at(at_min)
        if activity == self._active_activity:
            return
        self._active_activity = activity
        self._route_index = 1

    def active_route(self) -> tuple[Pose, ...]:
        if self._active_activity == "survey" and self.survey_route:
            return self.survey_route
        return self.patrol_route if self.closed_route else self.normal_route

    @property
    def normal_route(self) -> tuple[Pose, ...]:
        return self.truth.normal_route

    @property
    def pose(self) -> Pose:
        return self._col, self._row, self.heading_rad

    @property
    def position(self) -> GridCoord:
        return GridCoord(int(round(self._col)), int(round(self._row)))

    @position.setter
    def position(self, value: GridCoord) -> None:
        self._col, self._row = float(value.col), float(value.row)

    @property
    def float_position(self) -> tuple[float, float]:
        return self._col, self._row

    @property
    def detected(self) -> bool:
        return self._detected

    @property
    def base_heading(self) -> float:
        return self._base_heading

    @property
    def is_evading(self) -> bool:
        return self._navigation_params is not None

    @property
    def turn_rate_deg_min(self) -> float:
        return math.degrees(self._yaw_rate_rad_per_min)

    @property
    def surface_search_radar_range_cells(self) -> float:
        return 3.0

    def mark_detected(self) -> None:
        self._detected = True

    def set_tracked(self, tracked: bool) -> None:
        # Compatibility bookkeeping only; normal motion never reads this state.
        self._being_tracked = bool(tracked)

    def set_ais_signal(self, signal) -> None:
        self.ais_signal = signal

    def _integrate_yaw(self, desired_heading: float, dt_min: float) -> float:
        old_heading = self.heading_rad
        state = self.motion_dynamics.roll(self._motion_state(), desired_heading,
                                          self.speed_kn, dt_min)
        self.heading_rad = state.pose[2]
        self._yaw_rate_rad_per_min = state.yaw_rate
        return _wrap_pi(old_heading + _wrap_pi(self.heading_rad - old_heading) / 2)

    def _motion_state(self) -> MotionState:
        return MotionState(self.pose, self.speed_kn, self._yaw_rate_rad_per_min)

    def normal_tangent_rad(self) -> float:
        """Direction of the current normal-route segment, never current yaw."""
        route = self.active_route()
        if self.closed_route and self._route_index >= len(route) - 1:
            self._route_index = 1
        while self._route_index < len(route) - 1:
            a, b = route[self._route_index - 1], route[self._route_index]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if (self._col - b[0]) * dx + (self._row - b[1]) * dy < 0:
                break
            self._route_index += 1
        if self.closed_route and self._route_index >= len(route) - 1:
            self._route_index = 1
        if len(route) < 2:
            return route[0][2]
        a, b = route[self._route_index - 1], route[self._route_index]
        return math.atan2(b[1] - a[1], b[0] - a[0])

    def advance(self, route: ShipRoute, dt_min: float) -> tuple[Pose, ...]:
        """Execute timed controls and return the actual start and substep poses."""
        if self.departed or dt_min <= 0:
            return (self.pose,)
        # A separately constructed planning navigator must not override a chart
        # or obstacle context subsequently bound to the executing vessel.
        navigator = self._navigator
        if navigator is None:
            navigator = (route._navigator if route._navigator is not None
                         and route._navigator.ship is self else self.navigator)
        mask = self._land_mask if self._land_mask is not None else navigator.land_mask
        if (route._initial_state != self._motion_state() or route._navigator is None
                or route.generated_at_min < self._motion_time_min - 1e-9
                or route._generation != self._navigation_generation
                or route._navigator.ship is not self
                or route.map_version != route._navigator.map_version
                or route._island_bounds != route._navigator.island_bounds
                or not np.array_equal(route._land_mask, route._navigator.land_mask)
                or route.map_version != navigator.map_version
                or route._island_bounds != navigator.island_bounds
                or not np.array_equal(route._land_mask, mask)):
            route = navigator.braking_route(self._motion_state(), self._motion_time_min,
                                            mask, "stale or unvalidated route/map", dt_min)
        self._motion_time_min = max(self._motion_time_min, route.generated_at_min)
        self.navigation_status, self.blocked_reason = route.status, route.blocked_reason
        actual = [self.pose]
        remaining = dt_min
        commands = enumerate(route._commands, start=1)
        braking = False
        while remaining > 1e-10 and not self.departed:
            try:
                pose_index, command = next(commands)
            except StopIteration:
                if braking:
                    break  # Explicitly blocked: even braking has no safe continuation.
                reason = route.blocked_reason or "route horizon exhausted"
                route = navigator.braking_route(
                    self._motion_state(), self._motion_time_min, mask,
                    reason, remaining)
                self.navigation_status, self.blocked_reason = route.status, route.blocked_reason
                commands, braking = enumerate(route._commands, start=1), True
                continue
            dt = (command.duration_min if remaining >= command.duration_min - 1e-10
                  else remaining)
            state = self.motion_dynamics.roll(self._motion_state(), command.heading_rad,
                                              command.speed_kn, dt)
            if not navigator.segment_is_safe(self.pose, state.pose, mask):
                if braking:
                    # A fractional braking substep can differ from its planned
                    # chord. Do not retry an unsafe continuation indefinitely.
                    self.navigation_status = "blocked"
                    self.blocked_reason = "insufficient clearance for continued braking"
                    break
                route = navigator.braking_route(
                    self._motion_state(), self._motion_time_min, mask,
                    "execution segment blocked", remaining)
                self.navigation_status, self.blocked_reason = route.status, route.blocked_reason
                commands, braking = enumerate(route._commands, start=1), True
                continue
            self._col, self._row, self.heading_rad = state.pose
            self.speed_kn = state.speed_kn
            self.speed_cells_per_min = state.speed_kn * 1.852 / 60 / self.cell_size_km
            self._yaw_rate_rad_per_min = state.yaw_rate
            # Only a fully executed, collision-checked command reaches the
            # pose whose cursor was validated. A partial step cannot claim a
            # downstream rejoin, and replacement braking has no such metadata.
            if route._normal_indices and dt == command.duration_min:
                self._route_index = max(self._route_index, route._normal_indices[pose_index])
            actual.append(self.pose)
            remaining -= dt
            self._motion_time_min += dt
            if navigator.has_exited(self.pose, mask):
                self.departed = True
        self.normal_tangent_rad()
        self.trail.append(self.float_position)
        self.trail[:] = self.trail[-120:]
        return tuple(actual)

    @staticmethod
    def _is_safe_segment(start, end, islands: Iterable[Island]) -> bool:
        return not any(island.intersects_segment(start, end) for island in islands)

    def step(
        self,
        dt_min: float,
        islands: Iterable[Island] = (),
        *,
        current_time: float | None = None,
    ) -> tuple[Pose, ...]:
        """Advance along this vessel's own normal route."""
        if self.departed or dt_min <= 0.0:
            return (self.pose,)
        if current_time is not None:
            self._set_activity_for_time(float(current_time))
        self.navigator.set_islands(islands)
        route = self.navigator.plan(self.pose, self._navigation_params, self.normal_tangent_rad(),
                                    self._motion_time_min, self.land_mask)
        return self.advance(route, dt_min)


def _boundary_water_cells(mask: np.ndarray) -> list[tuple[float, float]]:
    cols, rows = mask.shape
    cells = {
        (float(col), float(row))
        for col in range(cols)
        for row in range(rows)
        if (col in (0, cols - 1) or row in (0, rows - 1)) and not mask[col, row]
    }
    return sorted(cells)


def create_ship_population(
    config: "AppConfig",
    seed: int,
    land_mask: np.ndarray,
    navigator: AStarNavigator,
) -> list[Ship]:
    """Create exactly the configured number of independently routed vessels."""
    mask = np.asarray(land_mask, dtype=bool)
    if mask.ndim != 2:
        raise ValueError("land_mask must be a two-dimensional grid")
    population = config.ship.population
    count = population.total_count
    type_ii_count = population.allocate()["type_ii"]

    manifest = ship_rng_manifest(seed)
    slots = list(range(count))
    class_rng = random.Random(manifest["ship_class"])
    class_rng.shuffle(slots)
    type_ii_slots = set(slots[:type_ii_count])
    ais_rng = random.Random(manifest["ship_ais_enabled"])
    activity_seed = int.from_bytes(
        hashlib.sha256(f"{int(seed)}:ship_activity".encode("ascii")).digest()[:8],
        "big",
    )
    activity_rng = random.Random(activity_seed)
    placement_rng = random.Random(f"{int(seed)}:ship-placement")
    motion_rng = random.Random(f"{int(seed)}:ship-motion")

    cols, rows = mask.shape
    spawn_cells = [
        (float(col), float(row))
        for col in range(1, max(1, cols - 1))
        for row in range(1, max(1, rows - 1))
        if not mask[col, row]
    ]
    exits = _boundary_water_cells(mask)
    ships: list[Ship] = []
    turn_rate = math.radians(config.ship.max_turn_rate_deg_min)
    speed_cells = config.ship.speed_kn * 1.852 / 60.0 / config.grid.cell_size_km
    r_min = max(0.1, speed_cells / max(turn_rate, 1e-9))

    for ship_index in range(count):
        ship_id = f"Ship-{ship_index + 1}"
        vessel_class = "type_ii" if ship_index in type_ii_slots else "type_i"
        ais_enabled = (
            True
            if vessel_class == "type_i"
            else ais_rng.random() < config.ship.type_ii_ais_on_probability
        )
        for _attempt in range(200):
            if not spawn_cells or not exits:
                continue
            candidate = placement_rng.choice(spawn_cells)
            if any(math.dist(candidate, ship.float_position) < 1.0 for ship in ships):
                continue
            exit_position = placement_rng.choice(exits)
            heading = motion_rng.uniform(-math.pi, math.pi)
            try:
                planned = navigator.plan_grid(
                    (candidate[0], candidate[1], heading),
                    {exit_position},
                    mask,
                    r_min=r_min,
                )
            except PathNotFoundError:
                continue
            ship = Ship(
                ship_id,
                GridCoord(int(candidate[0]), int(candidate[1])),
                config.ship.speed_kn,
                cell_size_km=config.grid.cell_size_km,
                normal_route=tuple(planned),
                ais_position_noise_cells=config.ship.ais_position_noise_cells,
                max_turn_rate_deg_min=config.ship.max_turn_rate_deg_min,
                yaw_time_constant_min=config.ship.yaw_time_constant_min,
                heading_control_gain_per_min=config.ship.heading_control_gain_per_min,
                turn_speed_loss_fraction=config.ship.turn_speed_loss_fraction,
                max_acceleration_kn_per_min=config.ship.max_acceleration_kn_per_min,
                land_mask=mask,
                navigator=navigator,
                navigation_horizon_min=config.ship.navigation_horizon_min,
                integration_dt_min=config.ship.integration_dt_min,
                navigation_clearance_cells=config.ship.navigation_clearance_cells,
                vessel_class=vessel_class,
                ais_enabled=ais_enabled,
                patrol_route=tuple(planned) + tuple(reversed(planned[:-1])),
            )
            if vessel_class == "type_ii":
                start_low, start_high = config.mission.activity.schedule_start_min
                duration_low, duration_high = config.mission.activity.schedule_duration_min
                schedule = (
                    (
                        activity_rng.uniform(start_low, start_high),
                        activity_rng.uniform(duration_low, duration_high),
                    ),
                )
                ship.truth = _replace_truth(ship.truth, activity_schedule=schedule)
                regulated = config.mission.activity.regulated_bboxes
                if regulated:
                    ship.survey_route = ship.navigator.plan_survey_lawnmower(
                        tuple(regulated[0]),
                        config.mission.activity.survey_track_spacing_cells,
                    )
                ship.radar_emitter = RadarEmitter(
                    ship.id,
                    seed=activity_seed ^ (ship_index + 1),
                    config=config.sensor.emitter,
                )
            # Grid-cell water alone does not guarantee a valid continuous pose.
            # Use the same clearance and stopping dynamics as actual execution.
            if (not ship.navigator.segment_is_safe(ship.pose, ship.pose, mask)
                    or not ship.navigator.can_stop(ship._motion_state(), mask)
                    or not all(ship.navigator.segment_is_safe(a, b, mask)
                               for a, b in zip(ship.normal_route, ship.normal_route[1:]))
                    or not all(ship.navigator.segment_is_safe(a, b, mask)
                               for a, b in zip(ship.patrol_route, ship.patrol_route[1:]))):
                continue
            break
        else:
            raise PopulationPlacementError(ship_index, 200)
        ships.append(ship)
    return ships


__all__ = [
    "PopulationPlacementError",
    "Ship",
    "ShipTruth",
    "ShipType",
    "create_ship_population",
]
