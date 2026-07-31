"""Three-level fixed-wing thunderstorm threat assessment and Dubins detours."""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import math
from typing import Iterable, Sequence

from src.env.dubins import DubinsPath, Pose


class ThreatLevel(IntEnum):
    CLEAR = 0
    LEVEL_1 = 1
    LEVEL_2 = 2
    LEVEL_3 = 3


@dataclass(frozen=True)
class ThreatAssessment:
    level: ThreatLevel
    storm: object | None = None
    distance_cells: float = math.inf


class StormAvoider:
    def __init__(
        self,
        safety_margin_cells: float = 1.0,
        eo_detection_range_cells: float = 2.5,
    ):
        self.safety_margin_cells = float(safety_margin_cells)
        self.eo_detection_range_cells = float(eo_detection_range_cells)

    def detect_threat(
        self,
        uav_pose: Sequence[float],
        target_position: Sequence[float],
        storms: Iterable,
        speed_cells_min: float = 0.0,
        dt_min: float = 1.0,
        standoff_radius: float = 1.8,
    ) -> ThreatAssessment:
        """Classify the most severe immediate/next-step storm threat."""
        best = ThreatAssessment(ThreatLevel.CLEAR)
        predicted = (
            float(uav_pose[0]) + speed_cells_min * math.cos(float(uav_pose[2])) * dt_min,
            float(uav_pose[1]) + speed_cells_min * math.sin(float(uav_pose[2])) * dt_min,
        )
        for storm in storms:
            if storm.contains(target_position, self.safety_margin_cells):
                return ThreatAssessment(ThreatLevel.LEVEL_3, storm, 0.0)
            distance = storm.distance_to_boundary(uav_pose[:2])
            if storm.contains(predicted, self.safety_margin_cells) or distance <= self.safety_margin_cells:
                candidate = ThreatAssessment(ThreatLevel.LEVEL_2, storm, distance)
            elif distance <= standoff_radius + self.safety_margin_cells:
                candidate = ThreatAssessment(ThreatLevel.LEVEL_1, storm, distance)
            else:
                candidate = ThreatAssessment(ThreatLevel.CLEAR, storm, distance)
            if candidate.level > best.level or (
                candidate.level == best.level and candidate.distance_cells < best.distance_cells
            ):
                best = candidate
        return best

    def plan_avoidance(
        self,
        uav_pose: Sequence[float],
        target_position: Sequence[float],
        storms: Iterable,
        R_min: float,
        *,
        standoff_radius: float = 2.3,
        sample_step: float = 0.15,
    ) -> list[Pose]:
        """Return the shortest safe Dubins detour that retains EO range."""
        storms = list(storms)
        candidates: list[list[Pose]] = []
        for index in range(24):
            phase = 2.0 * math.pi * index / 24.0
            goal = (
                float(target_position[0]) + standoff_radius * math.cos(phase),
                float(target_position[1]) + standoff_radius * math.sin(phase),
                phase + math.pi / 2.0,
            )
            path = DubinsPath.compute(uav_pose, goal, R_min, sample_step).waypoints
            if any(
                storm.contains(point[:2], self.safety_margin_cells)
                for storm in storms
                for point in path
            ):
                continue
            if any(
                math.dist(point[:2], target_position[:2]) > self.eo_detection_range_cells + 1e-6
                for point in path
            ):
                continue
            candidates.append(path)
        if not candidates:
            return []
        return min(
            candidates,
            key=lambda path: sum(math.dist(start[:2], end[:2]) for start, end in zip(path, path[1:])),
        )


__all__ = ["StormAvoider", "ThreatAssessment", "ThreatLevel"]
