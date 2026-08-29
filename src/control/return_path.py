"""Deprecated compatibility wrapper for endpoint-only return paths."""

import warnings

from src.control.heuristic.return_to_base import legacy_return_endpoints
from src.schedule.datatypes import GridCoord


def return_to_base(current: GridCoord, base_position: GridCoord) -> list[GridCoord]:
    """Preserve the legacy two-endpoint return shape."""
    warnings.warn(
        "return_to_base() is deprecated; use RecoveryPlanner.evaluate()",
        DeprecationWarning,
        stacklevel=2,
    )
    endpoints = legacy_return_endpoints(
        (current.col, current.row),
        (base_position.col, base_position.row),
    )
    return [GridCoord(int(col), int(row)) for col, row in endpoints]
