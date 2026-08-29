"""Register applied operation intents in scheduler-owned world state."""

from __future__ import annotations

from src.control.common.contracts import (
    ContactObservation,
    ControlCommand,
    ControlObservation,
    OperationMode,
)
from src.schedule.datatypes import GridCoord, Region
from src.schedule.state_manager import StateManager


class InvalidOperationIntent(ValueError):
    """Raised before state mutation when an applied operation is invalid."""


class OperationRegistry:
    def __init__(self, state_manager: StateManager) -> None:
        self._state_manager = state_manager
        self._track_bindings: dict[str, str] = {}

    def reconcile(
        self,
        uav_id: str,
        previous_command: ControlCommand | None,
        applied_command: ControlCommand,
        observation: ControlObservation,
    ) -> None:
        contact = (
            self._resolve_track_contact(applied_command, observation)
            if applied_command.operation_mode is OperationMode.TRACK
            else None
        )

        previous_was_track = (
            previous_command is not None
            and previous_command.operation_mode is OperationMode.TRACK
        )
        if previous_was_track and applied_command.operation_mode is not OperationMode.TRACK:
            self._release_binding(uav_id)

        if contact is not None:
            self._bind_track(uav_id, contact)

        uav = self._state_manager.get_uav(uav_id)
        if uav is not None:
            uav.sensor_mode = applied_command.sensor_mode.value

    @staticmethod
    def _resolve_track_contact(
        command: ControlCommand,
        observation: ControlObservation,
    ) -> ContactObservation:
        contact_id = command.target_contact_id
        if (
            not contact_id
            or contact_id not in observation.action_mask.target_contact_ids
        ):
            raise InvalidOperationIntent(
                f"TRACK target contact {contact_id!r} is absent from the action mask"
            )
        contact = next(
            (
                item
                for item in observation.contacts
                if item.contact_id == contact_id
            ),
            None,
        )
        if contact is None:
            raise InvalidOperationIntent(
                f"TRACK target contact {contact_id!r} is absent from observations"
            )
        if not contact.group_id:
            raise InvalidOperationIntent(
                f"TRACK target contact {contact_id!r} has no group_id"
            )
        return contact

    def _bind_track(self, uav_id: str, contact: ContactObservation) -> None:
        assert contact.group_id is not None
        existing_region_id = self._track_bindings.get(uav_id)
        region = self._state_manager.get_track_region_for_group(contact.group_id)
        if region is None:
            region = self._state_manager.create_track_region(
                contact.group_id,
                self._grid_position(contact),
            )
        else:
            self._state_manager.update_track_region_center(
                region.id, self._grid_position(contact)
            )

        if existing_region_id is not None and existing_region_id != region.id:
            self._release_binding(uav_id)

        self._track_bindings[uav_id] = region.id
        region.assigned_uav_id = uav_id
        uav = self._state_manager.get_uav(uav_id)
        if uav is not None:
            uav.assigned_region_id = region.id
            uav.target_group_id = contact.group_id

    def _release_binding(self, uav_id: str) -> None:
        region_id = self._track_bindings.pop(uav_id, None)
        if region_id is None:
            return
        other_uavs = sorted(
            bound_uav
            for bound_uav, bound_region_id in self._track_bindings.items()
            if bound_region_id == region_id
        )
        region = self._region(region_id)
        if other_uavs:
            if region is not None:
                region.assigned_uav_id = other_uavs[0]
        else:
            self._state_manager.release_track_region(
                region_id,
                source_uav_id=uav_id,
                create_marker=False,
            )
        uav = self._state_manager.get_uav(uav_id)
        if uav is not None and uav.assigned_region_id == region_id:
            uav.assigned_region_id = None
            uav.target_group_id = None

    def _region(self, region_id: str) -> Region | None:
        return next(
            (
                region
                for region in self._state_manager.get_track_regions()
                if region.id == region_id
            ),
            None,
        )

    @staticmethod
    def _grid_position(contact: ContactObservation) -> GridCoord:
        return GridCoord(
            int(round(contact.estimated_position[0])),
            int(round(contact.estimated_position[1])),
        )


__all__ = ["InvalidOperationIntent", "OperationRegistry"]
