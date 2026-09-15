"""EO measurement geometry retained after AIS identity rules were removed."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Mapping, Sequence


@dataclass(frozen=True)
class EOMeasurement:
    relative_bearing_rad: float
    distance_cells: float


class AISDiscriminator:
    @staticmethod
    def estimate_target_position(
        uav_pose: Sequence[float],
        eo_measurement: EOMeasurement | Mapping[str, float],
    ) -> tuple[float, float]:
        """Triangulate target coordinates from the EO bearing/range reading."""
        if isinstance(eo_measurement, Mapping):
            bearing = float(eo_measurement["relative_bearing_rad"])
            distance = float(eo_measurement["distance_cells"])
        else:
            bearing = eo_measurement.relative_bearing_rad
            distance = eo_measurement.distance_cells
        heading = float(uav_pose[2])
        return (
            float(uav_pose[0]) + distance * math.cos(heading + bearing),
            float(uav_pose[1]) + distance * math.sin(heading + bearing),
        )

__all__ = ["AISDiscriminator", "EOMeasurement"]
