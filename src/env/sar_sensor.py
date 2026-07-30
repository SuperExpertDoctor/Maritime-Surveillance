"""Side-looking stripmap SAR geometry and a compact SNR model."""
from __future__ import annotations

import math
from typing import Iterable, Sequence

from src.schedule.datatypes import GridCoord


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


__all__ = ["SARSensor"]
