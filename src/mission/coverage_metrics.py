"""Authoritative rolling coverage metrics from actual SAR scan cells."""

import math

import numpy as np


class CoverageMetrics:
    """Track last actual SAR scan time for each cell in one episode."""

    def __init__(
        self,
        *,
        episode_id: str,
        fixed_mask: np.ndarray,
        cell_size_km: float,
        windows_min: tuple[int, ...] = (30, 60, 120),
        primary_window_min: int = 60,
    ) -> None:
        if not isinstance(episode_id, str):
            raise ValueError("episode_id: expected string")
        fixed = np.asarray(fixed_mask, dtype=bool)
        if fixed.ndim != 2:
            raise ValueError("fixed_mask: expected two-dimensional mask")
        if isinstance(cell_size_km, bool) or not isinstance(cell_size_km, (int, float)):
            raise ValueError("cell_size_km: expected finite positive number")
        if not math.isfinite(cell_size_km) or cell_size_km <= 0:
            raise ValueError("cell_size_km: expected finite positive number")
        if not isinstance(windows_min, tuple) or not windows_min:
            raise ValueError("windows_min: expected non-empty tuple of integers")
        if any(isinstance(window, bool) or not isinstance(window, int) or window <= 0
               for window in windows_min):
            raise ValueError("windows_min: expected positive integers")
        if windows_min != (30, 60, 120):
            raise ValueError("windows_min must be exactly (30, 60, 120)")
        if isinstance(primary_window_min, bool) or not isinstance(primary_window_min, int):
            raise ValueError("primary_window_min: expected integer")
        if primary_window_min not in windows_min:
            raise ValueError("primary_window_min must be in windows_min")

        cell_size_km = float(cell_size_km)
        area_per_cell = cell_size_km * cell_size_km
        fixed_count = int(np.count_nonzero(fixed))
        fixed_area = fixed_count * area_per_cell
        if not math.isfinite(area_per_cell) or not math.isfinite(fixed_area):
            raise ValueError("cell_size_km produces non-finite coverage area")

        self.episode_id = episode_id
        self._fixed = fixed.copy()
        self._area_per_cell = area_per_cell
        self._fixed_area = fixed_area
        self._windows_min = windows_min
        self._primary_window_min = primary_window_min
        self._last_sar = np.full(fixed.shape, -np.inf, dtype=float)
        self._last_record_min: float | None = None

    @property
    def fixed_mask(self) -> np.ndarray:
        """Return a detached copy of the episode's static SAR denominator."""
        return self._fixed.copy()

    @staticmethod
    def _time(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name}: expected finite non-negative number")
        value = float(value)
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{name}: expected finite non-negative number")
        return value

    def record_sar(self, cells: tuple[tuple[int, int], ...], *, at_min: float) -> None:
        at_min = self._time(at_min, "at_min")
        if self._last_record_min is not None and at_min < self._last_record_min:
            raise ValueError("at_min cannot be earlier than the last recorded SAR scan")
        if not isinstance(cells, tuple):
            raise ValueError("cells: expected tuple of coordinate pairs")

        validated: list[tuple[int, int]] = []
        cols, rows = self._fixed.shape
        for cell in cells:
            if not isinstance(cell, tuple) or len(cell) != 2:
                raise ValueError("cells: expected tuple of coordinate pairs")
            col, row = cell
            if (isinstance(col, bool) or not isinstance(col, int)
                    or isinstance(row, bool) or not isinstance(row, int)):
                raise ValueError("cells: expected integer coordinates")
            if not 0 <= col < cols or not 0 <= row < rows:
                raise ValueError("cells: coordinate outside map")
            validated.append((col, row))

        for col, row in validated:
            if self._fixed[col, row]:
                self._last_sar[col, row] = at_min
        self._last_record_min = at_min

    def last_scan_matrix(self) -> np.ndarray:
        """Return a detached copy of the per-cell SAR scan timestamps."""
        return self._last_sar.copy()

    def snapshot(self, *, now_min: float, feasible_mask: np.ndarray) -> dict:
        now_min = self._time(now_min, "now_min")
        if self._last_record_min is not None and now_min < self._last_record_min:
            raise ValueError("now_min cannot be earlier than the last recorded SAR scan")
        feasible = np.asarray(feasible_mask, dtype=bool)
        if feasible.shape != self._fixed.shape:
            raise ValueError("feasible_mask: shape must match fixed_mask")

        fixed_count = int(np.count_nonzero(self._fixed))
        current = self._fixed & feasible
        current_count = int(np.count_nonzero(current))
        valid = self._fixed & np.isfinite(self._last_sar) & (self._last_sar <= now_min)
        ever_count = int(np.count_nonzero(valid))
        fixed_pct = None if fixed_count == 0 else 100.0 * ever_count / fixed_count
        unseen_pct = None if fixed_pct is None else 100.0 - fixed_pct

        windows = []
        primary_pct = None
        for window_min in self._windows_min:
            recent = valid & (self._last_sar > now_min - window_min)
            count = int(np.count_nonzero(recent))
            dynamic_count = int(np.count_nonzero(recent & current))
            coverage_pct = None if fixed_count == 0 else 100.0 * count / fixed_count
            dynamic_pct = (
                None if current_count == 0 else 100.0 * dynamic_count / current_count
            )
            windows.append({
                "minutes": window_min,
                "covered_cells": count,
                "covered_area_km2": float(count * self._area_per_cell),
                "coverage_pct": coverage_pct,
                "currently_searchable_coverage_pct": dynamic_pct,
                "window_complete": now_min >= window_min,
            })
            if window_min == self._primary_window_min:
                primary_pct = coverage_pct

        return {
            "schema_version": "persistent-coverage/v1",
            "episode_id": self.episode_id,
            "as_of_min": now_min,
            "status": "no_searchable_area" if fixed_count == 0 else "ok",
            "source": "sar",
            "denominator": "fixed_searchable_sea",
            "primary_window_min": self._primary_window_min,
            "fixed_searchable_cells": fixed_count,
            "fixed_searchable_area_km2": float(self._fixed_area),
            "currently_searchable_cells": current_count,
            "weather_blocked_cells": fixed_count - current_count,
            "ever_scanned_cells": ever_count,
            "cumulative_pct": fixed_pct,
            "unseen_pct": unseen_pct,
            "overdue_seen_pct": None if fixed_pct is None else fixed_pct - primary_pct,
            "windows": windows,
        }
