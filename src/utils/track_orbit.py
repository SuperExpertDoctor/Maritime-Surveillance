"""Lyapunov guidance-vector-field standoff tracking."""
from __future__ import annotations

import math
from typing import Callable, Sequence

from src.env.dubins import DubinsPath, Pose


def _wrap_pi(angle: float) -> float:
    return (angle + math.pi) % (2.0 * math.pi) - math.pi


class LGVFTracker:
    """Continuous-curvature guidance around stationary or moving targets."""

    def __init__(self, R_min: float = 1.0, heading_gain: float = 1.6, convergence_gain: float = 1.0):
        if R_min <= 0:
            raise ValueError("R_min must be positive")
        self.R_min = float(R_min)
        self.heading_gain = float(heading_gain)
        self.convergence_gain = float(convergence_gain)

    def compute_guidance(
        self,
        uav_pose: Sequence[float],
        target_position: Sequence[float],
        R_d: float,
        v_nominal: float,
    ) -> tuple[float, float]:
        """Return bounded heading-rate and airspeed commands.

        Units are grid cells and minutes, so ``v_nominal`` is cells/minute
        and the returned heading rate is radians/minute.
        """
        if R_d <= 0 or v_nominal <= 0:
            raise ValueError("R_d and v_nominal must be positive")
        x_rel = float(uav_pose[0]) - float(target_position[0])
        y_rel = float(uav_pose[1]) - float(target_position[1])
        radius = max(math.hypot(x_rel, y_rel), 1e-9)
        error = radius * radius - R_d * R_d

        # Convergence term is radial and the second term is tangential.  In
        # the downward-y visualiser frame this produces a clockwise orbit.
        vx = -self.convergence_gain * error * x_rel - 2.0 * R_d * radius * y_rel
        vy = -self.convergence_gain * error * y_rel + 2.0 * R_d * radius * x_rel
        desired_heading = math.atan2(vy, vx)
        heading_error = _wrap_pi(desired_heading - float(uav_pose[2]))
        raw_rate = self.heading_gain * heading_error
        max_rate = v_nominal / self.R_min
        heading_rate = max(-max_rate, min(max_rate, raw_rate))
        return heading_rate, v_nominal

    def compute_waypoints(
        self,
        uav_pose: Sequence[float],
        target_position: Sequence[float] | Callable[[int], Sequence[float]],
        R_d: float,
        dt: float,
        n_steps: int,
        v_nominal: float = 0.35,
    ) -> list[Pose]:
        if dt <= 0 or n_steps < 0:
            raise ValueError("dt must be positive and n_steps non-negative")
        pose = tuple(map(float, uav_pose))
        waypoints = [pose]
        for step in range(n_steps):
            target = target_position(step) if callable(target_position) else target_position
            rate, speed = self.compute_guidance(pose, target, R_d, v_nominal)
            new_heading = _wrap_pi(pose[2] + rate * dt)
            # Midpoint heading integration avoids a systematic outward bias.
            mid_heading = _wrap_pi(pose[2] + rate * dt / 2.0)
            pose = (
                pose[0] + speed * math.cos(mid_heading) * dt,
                pose[1] + speed * math.sin(mid_heading) * dt,
                new_heading,
            )
            waypoints.append(pose)
        return waypoints

    def plan_entry(
        self,
        uav_pose: Sequence[float],
        target_position: Sequence[float],
        R_d: float,
        sample_count: int = 36,
        step_size: float = 0.2,
    ):
        """Choose the shortest Dubins connection to a tangent orbit pose."""
        candidates = []
        tx, ty = float(target_position[0]), float(target_position[1])
        for index in range(sample_count):
            phase = 2.0 * math.pi * index / sample_count
            point = (tx + R_d * math.cos(phase), ty + R_d * math.sin(phase))
            tangent_heading = phase + math.pi / 2.0
            path = DubinsPath.compute(uav_pose, (*point, tangent_heading), self.R_min, step_size)
            candidates.append(path)
        return min(candidates, key=lambda path: path.total_length)


__all__ = ["LGVFTracker"]
