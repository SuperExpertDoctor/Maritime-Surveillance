"""Deprecated compatibility wrappers for target-orbit helpers."""

import warnings

from src.control.heuristic.tracking import (
    orbit_waypoint_positions,
    shifted_orbit_positions,
)
from src.schedule.datatypes import GridCoord


def generate_orbit_waypoints(target_position: GridCoord,
                             standoff_cells: float = 3.0,
                             num_points: int = 8) -> list[GridCoord]:
    """Return legacy integer orbit points through the heuristic helper."""
    warnings.warn(
        "generate_orbit_waypoints() is deprecated; use "
        "src.control.heuristic.tracking.orbit_waypoint_positions()",
        DeprecationWarning,
        stacklevel=2,
    )
    positions = orbit_waypoint_positions(
        (target_position.col, target_position.row),
        standoff_cells,
        num_points,
    )
    return [GridCoord(int(col), int(row)) for col, row in positions]


def update_orbit_center(old_waypoints: list[GridCoord],
                        target_displacement: tuple[float, float]) -> list[GridCoord]:
    """Translate legacy integer orbit points through the shared helper."""
    warnings.warn(
        "update_orbit_center() is deprecated; use "
        "src.control.heuristic.tracking.shifted_orbit_positions()",
        DeprecationWarning,
        stacklevel=2,
    )
    positions = shifted_orbit_positions(
        tuple((point.col, point.row) for point in old_waypoints),
        tuple(map(int, target_displacement)),
    )
    return [GridCoord(int(col), int(row)) for col, row in positions]
