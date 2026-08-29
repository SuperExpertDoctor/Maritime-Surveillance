"""Atomic ownership leases for UAV control commands."""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from src.control.common.contracts import ControlOwner


@dataclass(frozen=True)
class ControlLease:
    uav_id: str
    owner: ControlOwner
    controller_id: str
    generation: int
    acquired_at_min: float


class ControlOwnershipError(RuntimeError):
    """Raised when a lease does not describe the current UAV ownership."""

    def __init__(
        self,
        uav_id: str,
        expected_generation: int,
        actual_generation: int,
        *,
        current_owner: ControlOwner | None = None,
    ):
        self.uav_id = uav_id
        self.expected_generation = expected_generation
        self.actual_generation = actual_generation
        self.current_generation = actual_generation
        self.current_owner = current_owner
        if current_owner is not None:
            message = (
                f"cannot acquire control for {uav_id}: SYSTEM ownership required; "
                f"current owner {current_owner.value}, generation {actual_generation}"
            )
        else:
            message = (
                f"stale control lease for {uav_id}: "
                f"expected generation {expected_generation}, "
                f"current generation {actual_generation}"
            )
        super().__init__(message)


class ControlOwnership:
    def __init__(self, uav_ids: list[str] | tuple[str, ...]) -> None:
        self._lock = RLock()
        self._leases = {
            uav_id: ControlLease(uav_id, ControlOwner.SYSTEM, "system", 0, 0.0)
            for uav_id in uav_ids
        }

    def acquire(
        self,
        uav_id: str,
        owner: ControlOwner,
        controller_id: str,
        acquired_at_min: float,
    ) -> ControlLease:
        with self._lock:
            current = self._get_current(uav_id)
            if current.owner is not ControlOwner.SYSTEM:
                raise ControlOwnershipError(
                    uav_id,
                    current.generation,
                    current.generation,
                    current_owner=current.owner,
                )
            lease = ControlLease(
                uav_id,
                owner,
                controller_id,
                current.generation + 1,
                acquired_at_min,
            )
            self._leases[uav_id] = lease
            return lease

    def replace(
        self,
        lease: ControlLease,
        owner: ControlOwner,
        controller_id: str,
        acquired_at_min: float,
    ) -> ControlLease:
        with self._lock:
            current = self._get_current(lease.uav_id)
            self._require_current(lease, current)
            replacement = ControlLease(
                lease.uav_id,
                owner,
                controller_id,
                current.generation + 1,
                acquired_at_min,
            )
            self._leases[lease.uav_id] = replacement
            return replacement

    def release_to_system(self, lease: ControlLease, acquired_at_min: float) -> ControlLease:
        with self._lock:
            current = self._get_current(lease.uav_id)
            self._require_current(lease, current)
            released = ControlLease(
                lease.uav_id,
                ControlOwner.SYSTEM,
                "system",
                current.generation + 1,
                acquired_at_min,
            )
            self._leases[lease.uav_id] = released
            return released

    def current(self, uav_id: str) -> ControlLease:
        with self._lock:
            return self._get_current(uav_id)

    def accepts(self, lease: ControlLease) -> bool:
        with self._lock:
            current = self._leases.get(lease.uav_id)
            return current == lease

    def _get_current(self, uav_id: str) -> ControlLease:
        try:
            return self._leases[uav_id]
        except KeyError as exc:
            raise KeyError(f"unknown UAV: {uav_id}") from exc

    @staticmethod
    def _require_current(lease: ControlLease, current: ControlLease) -> None:
        if current != lease:
            raise ControlOwnershipError(lease.uav_id, lease.generation, current.generation)
