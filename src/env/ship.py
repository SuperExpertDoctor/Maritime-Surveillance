"""Independent maritime vessels and deterministic population generation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
import math
import random
from typing import TYPE_CHECKING, Iterable, Literal

import numpy as np

from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.env.dubins import Pose
from src.env.obstacle import Island
from src.env.ship_navigation import MotionDynamics, MotionState, ShipNavigator, ShipRoute
from src.mission.contracts import ship_rng_manifest
from src.schedule.datatypes import GridCoord

if TYPE_CHECKING:
    from src.schedule.config_loader import AppConfig


@dataclass(frozen=True)
class ShipTruth:
    """Environment/evaluation-only vessel truth."""

    ship_id: str
    identity: Literal["target", "civilian"]
    ais_mode: Literal["civilian", "silent"]
    normal_route: tuple[Pose, ...]


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
    """Public vessel label; hidden identity never changes this value."""

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
        truth_identity: Literal["target", "civilian"] = "civilian",
        ais_mode: Literal["civilian", "silent"] = "civilian",
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
    ) -> None:
        route = tuple(normal_route)
        heading = (
            float(route[0][2])
            if route
            else (0.0 if base_heading is None else float(base_heading))
        )
        if not route:
            route = ((float(initial_position.col), float(initial_position.row), heading),)
        self.truth = ShipTruth(ship_id, truth_identity, ais_mode, route)
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
        self.ais_signal = None
        self.is_military: bool | None = None
        self.discrimination = None
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
    def truth_identity(self) -> Literal["target", "civilian"]:
        return self.truth.identity

    @property
    def ais_mode(self) -> Literal["civilian", "silent"]:
        return self.truth.ais_mode

    @ais_mode.setter
    def ais_mode(self, value: Literal["civilian", "silent"]) -> None:
        self.truth = replace(self.truth, ais_mode=value)

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
        return False

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
        route = self.normal_route
        while self._route_index < len(route) - 1:
            a, b = route[self._route_index - 1], route[self._route_index]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if (self._col - b[0]) * dx + (self._row - b[1]) * dy < 0:
                break
            self._route_index += 1
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
        commands = iter(route._commands)
        braking = False
        while remaining > 1e-10 and not self.departed:
            try:
                command = next(commands)
            except StopIteration:
                if braking:
                    break  # Explicitly blocked: even braking has no safe continuation.
                reason = route.blocked_reason or "route horizon exhausted"
                route = navigator.braking_route(
                    self._motion_state(), self._motion_time_min, mask,
                    reason, remaining)
                self.navigation_status, self.blocked_reason = route.status, route.blocked_reason
                commands, braking = iter(route._commands), True
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
                commands, braking = iter(route._commands), True
                continue
            self._col, self._row, self.heading_rad = state.pose
            self.speed_kn = state.speed_kn
            self.speed_cells_per_min = state.speed_kn * 1.852 / 60 / self.cell_size_km
            self._yaw_rate_rad_per_min = state.yaw_rate
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
    ) -> tuple[Pose, ...]:
        """Advance along this vessel's own normal route."""
        if self.departed or dt_min <= 0.0:
            return (self.pose,)
        self.navigator.set_islands(islands)
        route = self.navigator.plan(self.pose, None, self.normal_tangent_rad(),
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
    count = config.ship.initial_ship_count
    target_count = config.ship.target_ship_count

    manifest = ship_rng_manifest(seed)
    slots = list(range(count))
    identity_rng = random.Random(manifest["ship_identity"])
    identity_rng.shuffle(slots)
    target_slots = set(slots[:target_count])
    ais_rng = random.Random(manifest["ship_ais_mode"])
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
        identity = "target" if ship_index in target_slots else "civilian"
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
                truth_identity=identity,
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
            )
            # Grid-cell water alone does not guarantee a valid continuous pose.
            # Use the same clearance and stopping dynamics as actual execution.
            if (not ship.navigator.segment_is_safe(ship.pose, ship.pose, mask)
                    or not ship.navigator.can_stop(ship._motion_state(), mask)
                    or not all(ship.navigator.segment_is_safe(a, b, mask)
                               for a, b in zip(ship.normal_route, ship.normal_route[1:]))):
                continue
            break
        else:
            raise PopulationPlacementError(ship_index, 200)
        if identity == "target" and ais_rng.random() >= config.ship.target_ais_on_probability:
            ship.ais_mode = "silent"
        ships.append(ship)
    return ships


__all__ = [
    "PopulationPlacementError",
    "Ship",
    "ShipTruth",
    "ShipType",
    "create_ship_population",
]
