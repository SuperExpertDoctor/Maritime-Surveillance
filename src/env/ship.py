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
        old_rate = self._yaw_rate_rad_per_min
        error = _wrap_pi(desired_heading - old_heading)
        commanded_rate = _clamp(
            self.heading_control_gain_per_min * error,
            -self.max_turn_rate_rad_per_min,
            self.max_turn_rate_rad_per_min,
        )
        response = 1.0 - math.exp(-dt_min / self.yaw_time_constant_min)
        new_rate = old_rate + response * (commanded_rate - old_rate)
        new_rate = _clamp(
            new_rate,
            -self.max_turn_rate_rad_per_min,
            self.max_turn_rate_rad_per_min,
        )
        delta = 0.5 * (old_rate + new_rate) * dt_min
        if delta * error > 0.0 and abs(delta) > abs(error):
            delta = error
            new_rate = 0.0
        self.heading_rad = _wrap_pi(old_heading + delta)
        self._yaw_rate_rad_per_min = new_rate
        return _wrap_pi(old_heading + delta / 2.0)

    @staticmethod
    def _is_safe_segment(start, end, islands: Iterable[Island]) -> bool:
        return not any(island.intersects_segment(start, end) for island in islands)

    def step(
        self,
        dt_min: float,
        islands: Iterable[Island] = (),
    ) -> None:
        """Advance along this vessel's own normal route."""
        if self.departed or dt_min <= 0.0:
            return
        route = self.normal_route
        distance_budget = self.speed_cells_per_min * dt_min
        while self._route_index < len(route):
            target = route[self._route_index]
            distance = math.dist(self.float_position, target[:2])
            if distance > max(distance_budget, 1e-6):
                break
            self._col, self._row = target[:2]
            distance_budget = max(0.0, distance_budget - distance)
            self._route_index += 1
        if self._route_index >= len(route):
            self.departed = True
        elif distance_budget > 0.0:
            target = route[self._route_index]
            desired_heading = math.atan2(target[1] - self._row, target[0] - self._col)
            motion_heading = self._integrate_yaw(desired_heading, dt_min)
            turn_fraction = abs(self._yaw_rate_rad_per_min) / max(
                self.max_turn_rate_rad_per_min, 1e-9
            )
            distance = distance_budget * (
                1.0 - self.turn_speed_loss_fraction * turn_fraction
            )
            candidate = (
                self._col + distance * math.cos(motion_heading),
                self._row + distance * math.sin(motion_heading),
            )
            if self._is_safe_segment(self.float_position, candidate, islands):
                self._col, self._row = candidate
        self.trail.append(self.float_position)
        if len(self.trail) > 120:
            self.trail.pop(0)


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
        route: tuple[Pose, ...] | None = None
        start: tuple[float, float] | None = None
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
            start = candidate
            route = tuple(planned)
            break
        if route is None or start is None:
            raise PopulationPlacementError(ship_index, 200)

        ship_id = f"Ship-{ship_index + 1}"
        identity = "target" if ship_index in target_slots else "civilian"
        ais_mode: Literal["civilian", "silent"] = "civilian"
        if identity == "target" and ais_rng.random() >= config.ship.target_ais_on_probability:
            ais_mode = "silent"
        ships.append(
            Ship(
                ship_id,
                GridCoord(int(start[0]), int(start[1])),
                config.ship.speed_kn,
                cell_size_km=config.grid.cell_size_km,
                truth_identity=identity,
                ais_mode=ais_mode,
                normal_route=route,
                ais_position_noise_cells=config.ship.ais_position_noise_cells,
                max_turn_rate_deg_min=config.ship.max_turn_rate_deg_min,
                yaw_time_constant_min=config.ship.yaw_time_constant_min,
                heading_control_gain_per_min=config.ship.heading_control_gain_per_min,
                turn_speed_loss_fraction=config.ship.turn_speed_loss_fraction,
            )
        )
    return ships


__all__ = [
    "PopulationPlacementError",
    "Ship",
    "ShipTruth",
    "ShipType",
    "create_ship_population",
]
