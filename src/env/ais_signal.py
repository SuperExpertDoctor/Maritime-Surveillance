"""AIS signal model used by the simulated maritime targets."""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.env.ship import Ship


@dataclass(frozen=True)
class AISSignal:
    mmsi: str
    reported_position: tuple[float, float]
    reported_speed_kn: float
    reported_heading_deg: float
    ship_name: str
    ship_type: str
    timestamp: float

    def to_dict(self) -> dict:
        return {
            "mmsi": self.mmsi,
            "reported_position": list(self.reported_position),
            "reported_speed_kn": self.reported_speed_kn,
            "reported_heading_deg": self.reported_heading_deg,
            "ship_name": self.ship_name,
            "ship_type": self.ship_type,
            "timestamp": self.timestamp,
        }


def _stable_number(value: str) -> int:
    # Population IDs are unique generation ordinals. A character checksum
    # collides even within the first hundred vessels (e.g. Ship-18/Ship-90).
    if value.startswith("Ship-") and value[5:].isdigit():
        return int(value[5:])
    return sum((index + 1) * ord(char) for index, char in enumerate(value))


def generate_ais_signal(ship: "Ship", timestamp: float) -> AISSignal | None:
    """Generate a public AIS report without consulting hidden vessel identity."""
    mode = getattr(ship, "ais_mode", "civilian")
    if mode == "silent":
        return None
    serial = _stable_number(ship.id)
    phase = serial * 0.017 + float(timestamp) * 0.11
    noise = float(getattr(ship, "ais_position_noise_cells", 0.05))
    reported = (
        ship.float_position[0] + noise * math.cos(phase),
        ship.float_position[1] + noise * math.sin(phase),
    )
    return AISSignal(
        mmsi=f"{100000000 + serial % 899999999:09d}",
        reported_position=reported,
        reported_speed_kn=ship.speed_kn,
        reported_heading_deg=math.degrees(ship.heading_rad) % 360.0,
        ship_name=f"MV-{serial % 1000:03d}",
        ship_type="Cargo",
        timestamp=float(timestamp),
    )


__all__ = ["AISSignal", "generate_ais_signal"]
