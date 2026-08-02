"""Side-looking stripmap SAR geometry and a compact SNR model."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from src.schedule.datatypes import GridCoord


@dataclass(frozen=True)
class SwathBeam:
    origin: tuple[float, float]
    heading: float
    look_direction: str
    near_range: float
    far_range: float
    along_track: float
    polygon: tuple[tuple[float, float], ...]


class SARSensor:
    def __init__(
        self,
        swath_width_cells: float = 2.0,
        near_range_cells: float = 0.25,
        detection_probability: float = 0.9,
        grid_shape: tuple[int, int] = (30, 30),
    ):
        if swath_width_cells <= 0 or near_range_cells <= 0:
            raise ValueError("SAR ranges must be positive")
        self.swath_width_cells = float(swath_width_cells)
        self.near_range_cells = float(near_range_cells)
        self.detection_probability = float(detection_probability)
        self.grid_shape = grid_shape

    @staticmethod
    def _side_vector(heading: float, look_direction: str) -> tuple[float, float]:
        if look_direction == "right":
            return -math.sin(heading), math.cos(heading)
        if look_direction == "left":
            return math.sin(heading), -math.cos(heading)
        raise ValueError("look_direction must be 'left' or 'right'")

    def compute_swath_footprint(
        self,
        uav_position: Sequence[float],
        heading: float,
        look_direction: str,
        along_track_cells: float = 1.0,
    ) -> list[GridCoord]:
        """Return grid cells inside the instantaneous off-nadir footprint."""
        x, y = float(uav_position[0]), float(uav_position[1])
        side_x, side_y = self._side_vector(heading, look_direction)
        forward_x, forward_y = math.cos(heading), math.sin(heading)
        far = self.near_range_cells + self.swath_width_cells
        cols, rows = self.grid_shape
        cells: list[GridCoord] = []
        padding = int(math.ceil(far + along_track_cells))
        for col in range(max(0, int(math.floor(x)) - padding), min(cols, int(math.floor(x)) + padding + 1)):
            for row in range(max(0, int(math.floor(y)) - padding), min(rows, int(math.floor(y)) + padding + 1)):
                dx, dy = col + 0.5 - x, row + 0.5 - y
                cross = dx * side_x + dy * side_y
                along = dx * forward_x + dy * forward_y
                if self.near_range_cells <= cross < far and abs(along) <= along_track_cells / 2.0:
                    cells.append(GridCoord(col, row))
        return cells

    def compute_swath_beam(
        self,
        uav_position: Sequence[float],
        heading: float,
        look_direction: str,
        along_track_cells: float = 1.0,
    ) -> SwathBeam:
        """Return the continuous side-looking ground beam used by the UI."""
        if along_track_cells <= 0:
            raise ValueError("along_track_cells must be positive")
        x, y = float(uav_position[0]), float(uav_position[1])
        side_x, side_y = self._side_vector(heading, look_direction)
        forward_x, forward_y = math.cos(heading), math.sin(heading)
        near = self.near_range_cells
        far = near + self.swath_width_cells
        half_along = float(along_track_cells) / 2.0

        def point(along: float, cross: float) -> tuple[float, float]:
            return (
                x + forward_x * along + side_x * cross,
                y + forward_y * along + side_y * cross,
            )

        # Fan-shaped beam anchored at the UAV: the apex is the aircraft
        # itself, then the polygon opens to the side into a trapezoidal
        # ground swath — visually a side-looking fan, not a detached strip.
        polygon = (
            (x, y),                     # UAV (beam apex)
            point(-half_along, near),   # behind-near
            point(-half_along, far),    # behind-far
            point(half_along, far),     # ahead-far
            point(half_along, near),    # ahead-near
        )
        return SwathBeam(
            origin=(x, y),
            heading=float(heading),
            look_direction=look_direction,
            near_range=near,
            far_range=far,
            along_track=float(along_track_cells),
            polygon=polygon,
        )

    def is_cell_in_swath(
        self,
        cell: GridCoord | Sequence[int],
        uav_pose: Sequence[float],
        look_direction: str = "right",
    ) -> bool:
        target = GridCoord(int(cell[0]), int(cell[1]))
        return target in self.compute_swath_footprint(
            uav_pose[:2], float(uav_pose[2]), look_direction
        )

    @staticmethod
    def compute_snr(
        altitude_m: float,
        speed_kmh: float,
        transmit_power_w: float = 1000.0,
        antenna_gain_linear: float = 1_000.0,
        wavelength_m: float = 0.03,
        sigma0: float = 0.1,
    ) -> float:
        """Return a relative stripmap SNR in dB.

        The proportional radar equation preserves the required ``z^-3`` and
        ``v^-1`` relationships; a reference normalisation keeps values useful
        for simulation without pretending to know platform-classified losses.
        """
        if altitude_m <= 0 or speed_kmh <= 0:
            raise ValueError("altitude and speed must be positive")
        numerator = transmit_power_w * antenna_gain_linear**2 * wavelength_m**3 * sigma0
        denominator = altitude_m**3 * (speed_kmh / 3.6)
        reference = 1e-10
        return 10.0 * math.log10(max(numerator / denominator / reference, 1e-15))

    @staticmethod
    def probability_from_snr(snr_db: float, threshold_db: float = 8.0, slope: float = 0.7) -> float:
        return 1.0 / (1.0 + math.exp(-slope * (snr_db - threshold_db)))


__all__ = ["SARSensor", "SwathBeam"]
