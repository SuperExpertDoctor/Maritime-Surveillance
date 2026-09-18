"""Authoritative scheduling state and information-field facade."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from typing import Iterable, Mapping, Optional
import math

import numpy as np

from src.schedule.config_loader import AppConfig
from src.schedule.datatypes import BBox, GridCoord, Marker, Region, TargetReport, UAVState
from src.mission.information_update import InformationUpdatePolicy, ScanRefresh
from src.mission.coverage_metrics import CoverageMetrics
from src.mission.coverage_service import CoverageService
from src.mission.contact_store import ContactStore
from src.mission.contracts import (
    PassiveBearingObservation,
    PassivePosition,
    ProbeSession,
    VisualDetection,
)
from src.control.common.contracts import UavRouteSnapshot


_OPERATION_BY_STATUS = {
    "idle": "idle",
    "transit": "transit",
    "searching": "coverage",
    "tracking": "track",
    "returning": "return",
    "holding": "holding",
    "refueling": "idle",
    "failed": "idle",
}


class StateManager:
    def __init__(self, config: AppConfig):
        self.config = config
        self.episode_id = ""
        self.current_time = 0.0
        self.cycle = 0
        self.runtime_status = "running"
        self.blocked_role = None
        self.memory_version = "baseline"
        self.vessel_mutation_allowed = True
        self.editing_allowed = True
        self.initial_vessel_count = config.ship.population.total_count
        self.actual_vessel_count = 0
        self._vessel_inventory: tuple[dict, ...] = ()
        self.lifecycle_mode = False
        self.information_policy = InformationUpdatePolicy(config)
        self.coverage_metrics: CoverageMetrics | None = None
        self.coverage_service: CoverageService | None = None
        self._coverage_task_generations: dict[str, int] = {}
        self._coverage_task_uavs: dict[str, str] = {}
        self._last_information_delta = None
        self._uavs = [
            UAVState(
                id=f"UAV-{index + 1}",
                status="idle",
                position=GridCoord(*config.environment.base_position),
            )
            for index in range(config.uav.count)
        ]
        self._search_regions: list[Region] = []
        self._track_regions: list[Region] = []
        self._track_region_counter = 0
        self._previous_search_regions: list[Region] = []
        self._markers: list[Marker] = []
        self._marker_counter = 0
        self._events: list[dict] = []
        self._intent_events: list[dict] = []
        self._published_intents: tuple = ()
        self._published_intent_statuses: tuple = ()
        self._known_target_groups: set[str] = set()
        self.contacts = ContactStore(
            config.mission.contact, cell_size_km=config.grid.cell_size_km,
            ais_uncertainty_cells=config.ship.ais_position_noise_cells,
        )
        # Older controller tests inject named observations. Production sensors
        # ingest typed measurements directly and always use generated contact IDs.
        self._legacy_contact_ids: dict[str, str] = {}
        self._legacy_sample_counter = 0
        self._contact_event_cursor = 0
        self._probe_sessions: dict[str, ProbeSession] = {}
        self._control_routes: dict[str, UavRouteSnapshot] = {}
        self._passive_positions: dict[str, PassivePosition] = {}
        self._passive_observations: dict[str, PassiveBearingObservation] = {}
        self.obstacles: list = []
        self.obstacle_mask = np.zeros(config.grid.resolution, dtype=bool)
        self.obstacle_version = 0
        self.land_mask = np.zeros(config.grid.resolution, dtype=bool)
        self._base_positions: tuple[tuple[int, int], ...] = (config.environment.base_position,)

    def step(self, current_time: float) -> None:
        self.current_time = current_time
        self._last_information_delta = self.information_policy.advance_time(current_time)
        values = self.get_value_matrix()
        scan_times = self.get_last_scan_matrix()
        for region in self._search_regions:
            b = region.bbox
            generation = self._coverage_task_generations.get(region.id)
            if generation is None and self.coverage_service is not None:
                generation = self.coverage_service.latest_generation(region.id)
            task_progress = None
            if self.coverage_service is not None and generation is not None:
                task_progress = self.coverage_service.progress(
                    region.id,
                    generation,
                    uav_id=self._coverage_task_uavs.get(region.id),
                )
            if task_progress is not None:
                required = len(task_progress.required_cells)
                scanned = len(task_progress.scanned_cells)
                region.completion_pct = (
                    100.0 * scanned / required if required else 0.0
                )
                region.completion_basis = "task_sar"
            else:
                scan_patch = scan_times[
                    b.col_start:b.col_end, b.row_start:b.row_end
                ]
                region.completion_pct = (
                    float(np.isfinite(scan_patch).mean() * 100)
                    if scan_patch.size else 0.0
                )
                region.completion_basis = "legacy_observation"
            region.avg_info = self.get_avg_info_in_bbox(b)
            patch = values[b.col_start:b.col_end, b.row_start:b.row_end]
            region.info_value = float(patch.mean()) if patch.size else 0.0

    # Operator vessel read model ------------------------------------
    def publish_vessel_inventory(self, items: Iterable[Mapping]) -> None:
        """Publish a detached vessel inventory for operator-facing frames."""
        self._vessel_inventory = tuple(deepcopy(dict(item)) for item in items)

    def get_vessel_inventory(self) -> tuple[dict, ...]:
        """Return a detached snapshot; callers cannot mutate authoritative state."""
        return deepcopy(self._vessel_inventory)

    # UAV management -------------------------------------------------
    def get_all_uavs(self) -> list[UAVState]:
        return self._uavs

    def get_uav(self, uav_id: str) -> Optional[UAVState]:
        return next((uav for uav in self._uavs if uav.id == uav_id), None)

    def is_uav_operational(self, uav_id: str) -> bool:
        """Return whether a UAV may participate in new mission work."""
        uav = self.get_uav(uav_id)
        return bool(
            uav is not None
            and getattr(uav, "operational_status", "available") != "failed"
            and getattr(uav, "status", "idle") != "failed"
        )

    def get_available_uavs(self) -> list[UAVState]:
        return [
            uav
            for uav in self._uavs
            if self.is_uav_operational(uav.id)
            and uav.control_mode == "heuristic"
            and uav.control_owner == "system"
            and uav.status in {"idle", "holding"}
            and uav.operation_mode in {"idle", "holding"}
        ]

    def update_uav_control(
        self,
        uav_id: str,
        control_mode: str,
        control_owner: str,
        operation_mode: str,
        controller_generation: int,
        safety_intervened: bool,
    ) -> None:
        uav = self.get_uav(uav_id)
        if uav is None:
            return
        uav.control_mode = control_mode
        uav.control_owner = control_owner
        uav.operation_mode = operation_mode
        uav.controller_generation = controller_generation
        uav.safety_intervened = safety_intervened

    def update_uav_status(
        self,
        uav_id: str,
        status: str,
        position: GridCoord,
        assigned_region_id: Optional[str] = None,
        fuel_remaining_pct: Optional[float] = None,
        target_group_id: Optional[str] = None,
        heading_deg: Optional[float] = None,
        sensor_mode: Optional[str] = None,
    ) -> None:
        uav = self.get_uav(uav_id)
        if uav is None:
            return
        uav.status = status
        if status in _OPERATION_BY_STATUS:
            uav.operation_mode = _OPERATION_BY_STATUS[status]
        uav.position = position
        if assigned_region_id is not None:
            uav.assigned_region_id = assigned_region_id
        if fuel_remaining_pct is not None:
            uav.fuel_remaining_pct = fuel_remaining_pct
        if target_group_id is not None:
            uav.target_group_id = target_group_id
        if heading_deg is not None:
            uav.heading_deg = heading_deg
        if sensor_mode is not None:
            uav.sensor_mode = sensor_mode

    def clear_uav_assignment(self, uav_id: str) -> None:
        uav = self.get_uav(uav_id)
        if uav:
            uav.assigned_region_id = None
            uav.target_group_id = None

    def mark_uav_reassigned(self, uav_id: str, current_time: float) -> None:
        """Record the last scheduler task switch used by cooldown checks."""
        if isinstance(current_time, bool) or not isinstance(current_time, (int, float)):
            raise TypeError("current_time must be a finite non-negative number")
        current_time = float(current_time)
        if not math.isfinite(current_time) or current_time < 0.0:
            raise ValueError("current_time must be a finite non-negative number")
        uav = self.get_uav(uav_id)
        if uav is None:
            raise KeyError(f"unknown UAV: {uav_id}")
        uav.last_reassigned_at_min = current_time

    def set_probe_session(self, probe: ProbeSession) -> None:
        """Publish a session already advanced by the simulation thread."""
        if not isinstance(probe, ProbeSession):
            raise TypeError("probe must be a ProbeSession")
        previous = self._probe_sessions.get(probe.probe_id)
        if previous is not None and (
            previous.contact_id != probe.contact_id
            or previous.uav_id != probe.uav_id
        ):
            raise ValueError("probe_id_owner_conflict")
        self._probe_sessions[probe.probe_id] = probe

    def get_probe_session(self, probe_id: str) -> ProbeSession | None:
        return self._probe_sessions.get(probe_id)

    def get_probe_sessions(self) -> tuple[ProbeSession, ...]:
        """Return an immutable, deterministic view for the simulation thread."""
        return tuple(
            self._probe_sessions[probe_id]
            for probe_id in sorted(self._probe_sessions)
        )

    def clear_probe_session(self, probe_id: str) -> None:
        self._probe_sessions.pop(probe_id, None)

    def set_control_route(self, uav_id: str, snapshot: UavRouteSnapshot) -> None:
        """Publish the newest immutable route envelope for one UAV."""
        if self.get_uav(uav_id) is None:
            raise KeyError(f"unknown UAV: {uav_id}")
        if not isinstance(snapshot, UavRouteSnapshot):
            raise TypeError("snapshot must be a UavRouteSnapshot")
        if self.episode_id and snapshot.episode_id != self.episode_id:
            raise ValueError("route snapshot episode mismatch")
        if not self.episode_id:
            self.episode_id = snapshot.episode_id
        previous = self._control_routes.get(uav_id)
        if previous is not None:
            if previous.episode_id != snapshot.episode_id:
                raise ValueError("route snapshot episode regressed")
            if snapshot.generation < previous.generation:
                raise ValueError("route snapshot generation regressed")
            if snapshot.generation == previous.generation:
                previous_revision = previous.route.route_revision
                incoming_revision = snapshot.route.route_revision
                if (
                    previous.route.status == "cleared"
                    and snapshot.route.status != "cleared"
                    and incoming_revision <= previous_revision
                ):
                    raise ValueError("cleared route cannot be revived")
                if (
                    snapshot.route.status != "cleared"
                    and incoming_revision < previous_revision
                ):
                    raise ValueError("route snapshot revision regressed")
                if incoming_revision < previous_revision:
                    snapshot = UavRouteSnapshot(
                        snapshot.episode_id,
                        snapshot.generation,
                        replace(
                            snapshot.route,
                            route_revision=previous_revision,
                        ),
                    )
        self._control_routes[uav_id] = snapshot

    def get_control_route(self, uav_id: str) -> UavRouteSnapshot | None:
        return self._control_routes.get(uav_id)

    def clear_control_routes(self) -> None:
        self._control_routes.clear()

    # Environment ----------------------------------------------------
    def set_environment_obstacles(self, obstacles: list, mask) -> None:
        self.obstacles = list(obstacles)
        normalized = np.array(mask, dtype=np.bool_, copy=True)
        if not np.array_equal(self.obstacle_mask, normalized):
            self.obstacle_version += 1
        self.obstacle_mask = normalized

    def set_land_mask(self, mask) -> None:
        """Publish the reset-specific mainland cells to all schedulers."""
        normalized = np.asarray(mask, dtype=bool)
        if normalized.shape != self.obstacle_mask.shape:
            raise ValueError("land mask must match the grid resolution")
        self.land_mask = normalized

    def set_base_positions(self, positions) -> None:
        """Publish reset-specific land bases to scheduling and coverage code."""
        normalized = tuple((int(position[0]), int(position[1])) for position in positions)
        if not normalized:
            raise ValueError("at least one base position is required")
        self._base_positions = normalized
        for uav in self._uavs:
            if uav.status == "idle":
                uav.position = GridCoord(*normalized[0])

    def get_base_positions(self) -> tuple[tuple[int, int], ...]:
        return self._base_positions

    # Region management ----------------------------------------------
    def set_search_regions(self, regions: list[Region]) -> None:
        self._previous_search_regions = list(self._search_regions)
        self._search_regions = regions

    def get_search_regions(self) -> list[Region]:
        return self._search_regions

    def get_active_search_regions(self) -> list[Region]:
        return [region for region in self._search_regions if region.status == "active"]

    def get_previous_search_regions(self) -> list[Region]:
        return self._previous_search_regions

    def get_track_regions(self) -> list[Region]:
        return self._track_regions

    def retire_search_regions_overlapping_tracks(
        self,
    ) -> list[tuple[Region, Optional[str]]]:
        """Remove search work that no longer has exclusive airspace."""
        if not self._track_regions or not self._search_regions:
            return []

        retired: list[tuple[Region, Optional[str]]] = []
        retained: list[Region] = []
        for region in self._search_regions:
            overlaps_track = any(
                self._bboxes_overlap(region.bbox, track.bbox)
                for track in self._track_regions
            )
            if not overlaps_track:
                retained.append(region)
                continue

            assigned_uav_id = region.assigned_uav_id
            retired.append((region, assigned_uav_id))
            region.status = "stale"
            region.assigned_uav_id = None
            if assigned_uav_id:
                uav = self.get_uav(assigned_uav_id)
                if uav and uav.assigned_region_id == region.id:
                    uav.assigned_region_id = None

        if not retired:
            return []

        previous_by_id = {
            region.id: region for region in self._previous_search_regions
        }
        previous_by_id.update({region.id: region for region, _ in retired})
        self._previous_search_regions = list(previous_by_id.values())
        self._search_regions = retained
        return retired

    @staticmethod
    def _bboxes_overlap(a: BBox, b: BBox) -> bool:
        return not (
            a.col_end <= b.col_start
            or b.col_end <= a.col_start
            or a.row_end <= b.row_start
            or b.row_end <= a.row_start
        )

    def get_track_region_for_group(self, target_group_id: str) -> Optional[Region]:
        canonical = self.resolve_contact_id(target_group_id)
        return next(
            (region for region in self._track_regions
             if region.target_group_id is not None
             and self.resolve_contact_id(region.target_group_id) == canonical),
            None,
        )

    def is_target_group_known(self, target_group_id: str) -> bool:
        return target_group_id in self._known_target_groups

    def record_target_observation(
        self,
        group_id: str,
        position: GridCoord,
        source_uav_id: str,
        observed_at: Optional[float] = None,
    ) -> TargetReport:
        """Compatibility adapter for callers with an already named sensor fix."""
        timestamp = self.current_time if observed_at is None else float(observed_at)
        uav = self.get_uav(source_uav_id)
        observer = tuple(uav.position) if uav else tuple(position)
        self._legacy_sample_counter += 1
        cid = self.contacts.ingest_visual(VisualDetection(
            f"legacy:{self._legacy_sample_counter}", timestamp, "eo", source_uav_id,
            tuple(position), None, 0.05, observer, math.dist(observer, position), "unknown"))
        self._legacy_contact_ids[group_id] = cid
        self._known_target_groups.add(group_id)
        return self.get_target_report(group_id)

    def resolve_contact_id(self, contact_id: str) -> str:
        return self.contacts.resolve(self._legacy_contact_ids.get(contact_id, contact_id))

    @property
    def merged_contact_aliases(self) -> dict[str, str]:
        aliases = self.contacts.aliases
        merged_ids = set(aliases.values())
        aliases.update((name, self.resolve_contact_id(name)) for name in self._legacy_contact_ids
                       if self.resolve_contact_id(name) in merged_ids)
        return aliases

    def get_target_report(self, contact_id: str) -> Optional[TargetReport]:
        try:
            contact = self.contacts.snapshot(self.resolve_contact_id(contact_id))
        except KeyError:
            return None
        if not contact.samples:
            return None
        latest = contact.samples[-1]
        return TargetReport(
            contact_id=contact_id if contact_id in self._legacy_contact_ids else contact.contact_id,
            position=GridCoord(*(int(round(v)) for v in contact.estimated_position_cells)),
            observed_at=contact.last_seen_min, source_uav_id=latest.source_id,
            velocity_cells_per_min=contact.estimated_velocity_cells_min or (0.0, 0.0),
            observation_count=len(contact.samples))

    def get_target_reports(self) -> list[TargetReport]:
        merged_ids = set(self.contacts.aliases.values())
        legacy_names = {self.resolve_contact_id(name): name for name in self._legacy_contact_ids
                        if self.resolve_contact_id(name) not in merged_ids}
        reports = [self.get_target_report(legacy_names.get(c.contact_id, c.contact_id))
                   for c in self.contacts.list_snapshots()]
        return sorted((r for r in reports if r is not None), key=lambda r: r.contact_id)

    def clear_target_report(self, contact_id: str) -> None:
        if self.get_target_report(contact_id) is not None:
            self.contacts.release(self.resolve_contact_id(contact_id), self.current_time, "released")

    def release_contact_reservation(
        self, contact_id: str, uav_id: str, now_min: float, reason: str,
    ) -> None:
        """Release only this UAV's reservation, including legacy/merged IDs."""
        try:
            contact = self.contacts.snapshot(self.resolve_contact_id(contact_id))
        except KeyError:
            return  # Legacy operation bindings need not have a contact history.
        if contact.assigned_uav_id == uav_id:
            self.contacts.release(contact.contact_id, now_min, reason)

    def contact_position(self, contact_id: str, now_min: float) -> tuple[float, float] | None:
        """Finite prediction only; reading never refreshes observation evidence."""
        try:
            c = self.contacts.snapshot(self.resolve_contact_id(contact_id))
        except KeyError:
            return None
        elapsed = max(0.0, now_min - c.last_seen_min)
        if c.state in ("lost", "departed") or elapsed > self.config.mission.contact.stale_after_min:
            return None
        velocity = c.estimated_velocity_cells_min or (0.0, 0.0)
        return tuple(p + v * elapsed for p, v in zip(c.estimated_position_cells, velocity))

    def publish_contact_events(self) -> tuple[dict, ...]:
        events = self.contacts.events[self._contact_event_cursor:]
        self._contact_event_cursor += len(events)
        published = []
        cancelled = {(e["contact_id"], e["uav_id"]) for e in events
                     if e["type"] == "duplicate_task_cancelled"}
        for event in events:
            if event["type"] == "contact_merged":
                for cancellation in self._merge_contact_bindings(event):
                    key = (cancellation["contact_id"], cancellation["uav_id"])
                    if key not in cancelled:
                        published.append(cancellation)
                        cancelled.add(key)
            published.append(event)
        for event in published:
            self.add_event(event["type"], {k: v for k, v in event.items() if k != "type"})
        return tuple(published)

    def _merge_contact_bindings(self, event: dict) -> list[dict]:
        """Keep the surviving track geometry and canonicalize scheduler IDs."""
        cid = self.resolve_contact_id(event["contact_id"])
        if self.contacts.snapshot(cid).state == "cleared":
            # The engine will release all bindings through type_i_released.
            return []
        owner = event["assigned_uav_id"]
        tracks = [region for region in self._track_regions
                  if region.target_group_id is not None
                  and self.resolve_contact_id(region.target_group_id) == cid]
        # Legacy operation bindings can predate ContactStore reservations.
        # Prefer the AIS track just as ContactStore prefers the AIS owner.
        if owner is None:
            assigned = sorted((region for region in tracks if region.assigned_uav_id),
                              key=lambda region: region.target_group_id != cid)
            if assigned:
                owner = assigned[0].assigned_uav_id
                self.contacts.reserve(cid, owner, None)
                event["assigned_uav_id"] = owner
        retained = next((region for region in tracks if region.assigned_uav_id == owner), None)
        cancellations = []
        for region in tracks:
            if region is retained:
                region.target_group_id = cid
            else:
                self.release_track_region(region.id, create_marker=False)
        for uav in self._uavs:
            if uav.target_group_id and self.resolve_contact_id(uav.target_group_id) == cid:
                if owner is not None and uav.id != owner:
                    cancellations.append({
                        "type": "duplicate_task_cancelled", "contact_id": cid,
                        "alias_contact_id": event["alias_contact_id"],
                        "uav_id": uav.id, "probe_id": None,
                    })
                    self.clear_uav_assignment(uav.id)
                else:
                    uav.target_group_id = cid
                    if retained is not None:
                        uav.assigned_region_id = retained.id
        for name in self._legacy_contact_ids:
            self._legacy_contact_ids[name] = self.resolve_contact_id(name)
        self._known_target_groups.add(cid)
        return cancellations

    def create_track_region(self, target_group_id: str, center: GridCoord) -> Region:
        existing = self.get_track_region_for_group(target_group_id)
        if existing is not None:
            return existing
        self._track_region_counter += 1
        col, row = center
        half = 2
        region = Region(
            id=f"T{self._track_region_counter}",
            bbox=BBox(
                max(0, col - half),
                max(0, row - half),
                min(self.config.grid.resolution[1], col + half),
                min(self.config.grid.resolution[0], row + half),
            ),
            type="track",
            priority="high",
            created_cycle=self.cycle,
            target_group_id=target_group_id,
        )
        self._track_regions.append(region)
        self._known_target_groups.add(target_group_id)
        return region

    def update_track_region_center(self, region_id: str, new_center: GridCoord) -> None:
        for region in self._track_regions:
            if region.id != region_id:
                continue
            col, row = new_center
            region.bbox = BBox(
                max(0, col - 2),
                max(0, row - 2),
                min(self.config.grid.resolution[1], col + 2),
                min(self.config.grid.resolution[0], row + 2),
            )
            return

    def release_track_region(
        self,
        region_id: str,
        source_uav_id: str = "",
        *,
        create_marker: bool = True,
    ) -> None:
        for region in list(self._track_regions):
            if region.id != region_id:
                continue
            center = GridCoord(
                (region.bbox.col_start + region.bbox.col_end) // 2,
                (region.bbox.row_start + region.bbox.row_end) // 2,
            )
            if create_marker:
                self._marker_counter += 1
                marker = Marker(
                    id=f"MK{self._marker_counter}",
                    position=center,
                    created_time=self.current_time,
                    source_uav_id=source_uav_id,
                )
                self._markers.append(marker)
            self._track_regions.remove(region)
            return

    def get_active_markers(self) -> list[Marker]:
        return self._markers

    # Events ---------------------------------------------------------
    def add_event(self, event_type: str, data: dict) -> None:
        self._events.append({"type": event_type, "time": self.current_time, "data": data})

    def record_intent_event(self, result) -> None:
        """Retain a bounded, JSON-ready view of command application results."""
        intent = getattr(result, "intent", None)
        intent_data = asdict(intent) if is_dataclass(intent) else intent
        self._intent_events.append({
            "command_id": result.command_id,
            "status": result.status,
            "intent": intent_data,
            "error_code": result.error_code,
        })
        del self._intent_events[:-100]

    def get_intent_events(self) -> list[dict]:
        return deepcopy(self._intent_events)

    def publish_intent_snapshot(self, intents, statuses) -> None:
        self._published_intents = tuple(deepcopy(tuple(intents)))
        self._published_intent_statuses = tuple(deepcopy(tuple(statuses)))

    def get_published_intent_snapshot(self) -> tuple[tuple, tuple]:
        return (
            deepcopy(self._published_intents),
            deepcopy(self._published_intent_statuses),
        )

    def get_recent_events(self, since_time: float) -> list[dict]:
        return [event for event in self._events if event["time"] >= since_time]

    # Information field facade -------------------------------------
    def configure_coverage_metrics(self, fixed_mask, episode_id: str) -> None:
        """Initialize episode-scoped SAR coverage after the map is complete."""
        coverage = self.config.mission.coverage
        self.coverage_metrics = CoverageMetrics(
            episode_id=episode_id,
            fixed_mask=fixed_mask,
            cell_size_km=self.config.grid.cell_size_km,
            windows_min=coverage.windows_min,
            primary_window_min=coverage.primary_window_min,
        )

    def configure_coverage_service(self, fixed_mask) -> None:
        """Initialize task-level SAR completion accounting for this episode."""
        self.coverage_service = CoverageService(fixed_mask)
        self._coverage_task_generations.clear()
        self._coverage_task_uavs.clear()

    def register_coverage_task_generation(
        self,
        task_id: str,
        generation: int,
        uav_id: str | None = None,
    ) -> None:
        """Publish the committed generation used by region progress read models."""
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id must be a non-empty string")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise ValueError("generation must be a non-negative integer")
        if uav_id is not None and (not isinstance(uav_id, str) or not uav_id):
            raise ValueError("uav_id must be a non-empty string")
        self._coverage_task_generations[task_id] = generation
        if uav_id is not None:
            self._coverage_task_uavs[task_id] = uav_id

    def coverage_task_generation(self, task_id: str) -> int | None:
        return self._coverage_task_generations.get(task_id)

    def get_persistent_coverage_stats(self) -> dict | None:
        """Return a detached point-in-time snapshot without advancing state."""
        if self.coverage_metrics is None:
            return None
        return self.coverage_metrics.snapshot(
            now_min=self.current_time,
            feasible_mask=self.get_searchable_mask(),
        )

    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False):
        return self.information_policy.apply_batch(
            [ScanRefresh(tuple(bbox), "track" if is_track else "search")],
            current_time,
        )

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False):
        cols, rows = self.config.grid.resolution
        if not (0 <= coord.col < cols and 0 <= coord.row < rows):
            return None
        return self.information_policy.apply_batch(
            [ScanRefresh((coord.col, coord.row, coord.col + 1, coord.row + 1),
                         "track" if is_track else "search")],
            current_time,
        )

    def apply_information_facts(self, facts, current_time: float):
        return self.information_policy.apply_batch(facts, current_time)

    def register_passive_position(self, position: PassivePosition) -> None:
        """Retain the latest conditional position for task generation."""
        if not isinstance(position, PassivePosition):
            raise TypeError("position must be PassivePosition")
        self._passive_positions[position.emitter_track_id] = position
        self.contacts.ingest_passive_position(
            position,
            association_radius_cells=(
                self.config.sensor.passive.position_association_radius_cells
            ),
            radiation_window_min=self.config.mission.activity.radiation_window_min,
            min_distinct_bursts=self.config.mission.activity.min_distinct_bursts,
        )

    def drain_radiation_activity_evidence(self):
        """Drain contact-derived activity facts for the current information batch."""
        return self.contacts.drain_radiation_activity_evidence()

    def passive_position_contact_id(self, emitter_track_id: str) -> str | None:
        """Return the observation-only contact associated with a signal track."""
        return self.contacts.emitter_contacts.get(emitter_track_id)

    def passive_burst_count(self, contact_id: str, now_min: float | None = None) -> int:
        now = self.current_time if now_min is None else float(now_min)
        return self.contacts.passive_burst_count(
            contact_id,
            now,
            window_min=self.config.mission.activity.radiation_window_min,
        )

    def record_passive_observations(
        self, observations: tuple[PassiveBearingObservation, ...] | list[PassiveBearingObservation]
    ) -> None:
        for observation in observations:
            if not isinstance(observation, PassiveBearingObservation):
                raise TypeError("observations must contain PassiveBearingObservation")
            self._passive_observations[observation.observation_id] = observation
        if len(self._passive_observations) > 2000:
            keep = sorted(
                self._passive_observations.values(),
                key=lambda item: (item.observed_at_min, item.observation_id),
            )[-2000:]
            self._passive_observations = {
                item.observation_id: item for item in keep
            }

    def get_passive_observations(self, now_min: float | None = None):
        now = self.current_time if now_min is None else float(now_min)
        return tuple(
            self._passive_observations[key]
            for key in sorted(self._passive_observations)
            if self._passive_observations[key].observed_at_min <= now
        )

    def get_passive_positions(self, now_min: float | None = None) -> tuple[PassivePosition, ...]:
        """Return active position releases without exposing environment truth."""
        now = self.current_time if now_min is None else float(now_min)
        active_sources = {
            record.source_id
            for record in self.information_policy.evidence_store.active_records(now)
            if record.kind == "passive_position"
        }
        return tuple(
            self._passive_positions[source_id]
            for source_id in sorted(self._passive_positions)
            if source_id in active_sources
        )

    @property
    def information_version(self) -> int:
        return self.information_policy.version

    @property
    def last_information_delta(self):
        return self._last_information_delta

    @property
    def ais_updates(self):
        return self.information_policy.ais_updates

    def freeze_information_snapshot(self, current_time: float | None = None):
        return self.information_policy.freeze_information_snapshot(
            self.current_time if current_time is None else current_time
        )

    def get_info_matrix(self):
        return self.information_policy.info_matrix(self.current_time)

    def get_last_scan_matrix(self):
        return self.information_policy.last_scan_time

    def get_value_matrix(self):
        return self.information_policy.value_matrix(self.current_time)

    def get_searchable_mask(self) -> np.ndarray:
        """Return cells that can be searched under the operational rules."""
        searchable = ~np.asarray(self.obstacle_mask, dtype=bool).copy()
        searchable &= ~self.land_mask
        if searchable.size:
            searchable[0, :] = False
            searchable[-1, :] = False
            searchable[:, 0] = False
            searchable[:, -1] = False
            for col, row in self._base_positions:
                searchable[col, row] = False
        return searchable

    def get_coverage_stats(self) -> dict[str, float | int]:
        """Measure unique coverage over searchable sea cells only."""
        searchable = self.get_searchable_mask()
        scanned = np.isfinite(self.get_last_scan_matrix()) & searchable
        searchable_cells = int(searchable.sum())
        scanned_cells = int(scanned.sum())
        coverage_pct = (
            scanned_cells / searchable_cells * 100.0
            if searchable_cells
            else 0.0
        )
        return {
            "scanned_searchable_cells": scanned_cells,
            "searchable_cells": searchable_cells,
            "coverage_pct": coverage_pct,
        }

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        c0, r0, c1, r1 = bbox
        patch = self.get_info_matrix()[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size else 0.0

    def get_avg_value_in_bbox(self, bbox: BBox) -> float:
        c0, r0, c1, r1 = bbox
        patch = self.get_value_matrix()[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size else 0.0


__all__ = ["StateManager"]
