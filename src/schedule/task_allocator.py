from src.schedule.config_loader import AppConfig
from src.schedule.state_manager import StateManager
from src.schedule.info_value_table import InfoValueTable
from src.schedule.candidate_extractor import CandidateExtractor, CandidateResult
from src.schedule.llm_client import LLMClient
from src.schedule.llm_reviewer import LLMReviewer
from src.schedule.hungarian import hungarian_pair
from src.schedule.trigger_manager import TriggerManager
from src.schedule.datatypes import Region, BBox
import math
import numpy as np
from src.control.heuristic.navigation import AStarNavigator, PathNotFoundError
from src.utils.search_route_planner import SearchRouteRequest, plan_search_route

from src.mission.contracts import (
    AssignmentBatch,
    ContactSnapshot,
    FeasibleEdge,
    Intent,
    IntentStatus,
    MissionSnapshot,
    TaskCandidate,
    TaskRecord,
    UavResource,
)
from src.mission.mission_scheduler import MissionScheduler
from src.mission.task_catalog import TaskCatalog
from src.mission.strategy_memory import StrategyMemoryStore


class TaskAllocator:
    """Main orchestrator connecting all scheduling components."""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm_gateway=None,
        strategy_memory_store: StrategyMemoryStore | None = None,
    ):
        self.config = config
        self.sm = StateManager(config)
        self.ivt = InfoValueTable(self.sm)
        self.extractor = CandidateExtractor()
        self.llm_client = LLMClient(config, gateway=llm_gateway)
        self.trigger_manager = TriggerManager(self.sm)
        self.task_catalog = TaskCatalog()
        self.strategy_memory_store = strategy_memory_store or StrategyMemoryStore()
        self.reviewer = LLMReviewer(
            config,
            self.llm_client,
            strategy_memory_store=self.strategy_memory_store,
        )
        self.memory_version = "baseline"
        self.mission_scheduler = MissionScheduler(
            llm_gateway=self.llm_client.gateway,
            reassignment_cooldown_min=config.mission.scheduling.reassignment_cooldown_min,
            max_tasks_in_prompt=config.mission.scheduling.max_tasks_in_prompt,
            allow_probe_preempt_search=config.mission.scheduling.allow_probe_preempt_search,
            allow_intent_preempt_search=config.mission.scheduling.allow_intent_preempt_search,
            strategy_memory_store=self.strategy_memory_store,
        )
        self._mission_snapshot_counter = 0
        heuristic = config.control.heuristic
        self._mission_navigator = AStarNavigator(
            xy_resolution=heuristic.astar_xy_resolution_cells,
            heading_bins=heuristic.astar_heading_bins,
            candidate_limit=heuristic.astar_candidate_limit,
            primitive_length=heuristic.astar_primitive_length_cells,
            sample_step=heuristic.path_sample_step_cells,
        )
        self._mission_route_cache: dict[tuple, float | None] = {}
        self._mission_route_metrics_cache: dict[
            tuple, tuple[float, float, tuple[float, float]] | None
        ] = {}
        self._last_mission_snapshot: MissionSnapshot | None = None

    def build_mission_snapshot(
        self,
        now_min: float | None = None,
        *,
        contacts: tuple[ContactSnapshot, ...] | None = None,
        intents: tuple[Intent, ...] = (),
        intent_statuses: tuple[IntentStatus, ...] = (),
        active_tasks: tuple[TaskRecord, ...] = (),
        memory_version: str | None = None,
        reviewer_summary: str | None = None,
    ) -> MissionSnapshot:
        """Publish a complete scheduler snapshot without applying a decision."""
        now = self.sm.current_time if now_min is None else float(now_min)
        selected_memory_version = (
            self.memory_version if memory_version is None else str(memory_version)
        )
        if not math.isfinite(now) or now < 0:
            raise ValueError("now_min must be finite and non-negative")
        published_contacts = tuple(
            self.sm.contacts.list_snapshots() if contacts is None else contacts
        )
        published_intents = tuple(intents)
        candidates = self.task_catalog.build(
            self.sm, published_contacts, published_intents, now,
        )
        resources = self._mission_resources()
        available = tuple(sorted(uav.id for uav in self.sm.get_available_uavs()))
        active_records = tuple(active_tasks)
        active_by_id = {record.task_id: record for record in active_records}
        preemptible = tuple(sorted(
            resource.uav_id
            for resource in resources
            if self.config.mission.scheduling.allow_probe_preempt_search
            and self._ordinary_search_resource(resource, active_by_id)
        ))
        planning_map_version = int(self.sm.obstacle_version)
        edges = self._mission_edges(
            candidates,
            resources,
            published_contacts,
            planning_map_version,
            active_tasks=active_records,
        )
        self._mission_snapshot_counter += 1
        snapshot_id = f"mission:{now:g}:{self._mission_snapshot_counter}"
        snapshot = MissionSnapshot(
            snapshot_id=snapshot_id,
            sim_time_min=now,
            candidates=tuple(candidates),
            available_uav_ids=available,
            preemptible_uav_ids=preemptible,
            uav_generations=tuple(
                (resource.uav_id, resource.generation) for resource in resources
            ),
            resources=resources,
            feasible_edges=edges,
            active_tasks=active_records,
            contacts=published_contacts,
            intents=published_intents,
            intent_statuses=tuple(intent_statuses),
            memory_version=selected_memory_version,
            planning_map_version=planning_map_version,
            reviewer_summary=(
                self.llm_client._reviewer_memory
                if reviewer_summary is None else reviewer_summary
            ),
        )
        self._last_mission_snapshot = snapshot
        return snapshot

    def set_strategy_memory_version(self, version: str) -> None:
        """Pin the memory manifest used by all snapshots in this episode."""
        if not isinstance(version, str) or not version.strip():
            raise ValueError("memory version must be a non-empty string")
        self.memory_version = version.strip()
        self.sm.memory_version = self.memory_version

    @property
    def last_mission_snapshot(self) -> MissionSnapshot | None:
        """Return the most recent immutable snapshot offered to the scheduler."""
        return self._last_mission_snapshot

    def uses_legacy_scheduler(self) -> bool:
        """Detect explicit test/compatibility overrides of the old path."""
        step_function = getattr(self.step, "__func__", None)
        decide_function = getattr(self.llm_client.decide, "__func__", None)
        return step_function is not TaskAllocator.step or decide_function is not LLMClient.decide

    def decide_mission(self, now_min: float | None = None, **kwargs):
        """Run T11 selection on a fresh snapshot; T12 owns application."""
        snapshot = self.build_mission_snapshot(now_min, **kwargs)
        return self.mission_scheduler.decide(snapshot)

    def mission_step(
        self,
        current_time: float,
        *,
        active_tasks: tuple[TaskRecord, ...] = (),
        intents: tuple[Intent, ...] = (),
        intent_statuses: tuple[IntentStatus, ...] = (),
    ) -> tuple[dict, object | None]:
        """Run one unified scheduling decision without mutating mission state."""
        self.sm.step(current_time)
        new_memory = self.reviewer.step(current_time, self.sm)
        if new_memory:
            self.llm_client.set_reviewer_memory(new_memory)

        decision = self.trigger_manager.check(current_time)
        if decision.trigger_type == "none":
            return {"trigger_type": "none", "action": None}, None
        if decision.trigger_type == "light":
            return self._handle_light_mission_trigger(
                current_time, decision, active_tasks,
            )

        snapshot = self.build_mission_snapshot(
            current_time,
            active_tasks=active_tasks,
            intents=intents,
            intent_statuses=intent_statuses,
        )
        batch = self.mission_scheduler.decide(snapshot)
        interaction = {
            "call_id": self.mission_scheduler.last_selection_call_id,
            "success": self.mission_scheduler.last_selection_success,
            "errors": list(self.mission_scheduler.last_selection_errors),
        }
        self.trigger_manager.mark_triggered("heavy", current_time)
        self.sm.cycle += 1
        self.sm.add_event("mission_decision", {
            "cycle": self.sm.cycle,
            "success": bool(batch is not None),
            "assignments": len(batch.assignments) if batch is not None else 0,
            "snapshot_id": snapshot.snapshot_id,
        })
        result = {
            "trigger_type": "heavy",
            "action": (
                "mission_selection_approved"
                if batch is not None
                else "mission_selection_unavailable"
            ),
            "search_regions": [
                {"id": region.id, "bbox": list(region.bbox)}
                for region in self.sm.get_active_search_regions()
            ],
            "pairs": [],
            "notes": "",
            "llm_cycle": interaction,
            "snapshot_id": snapshot.snapshot_id,
        }
        return result, batch

    def _handle_light_mission_trigger(
        self,
        current_time: float,
        decision,
        active_tasks: tuple[TaskRecord, ...],
    ) -> tuple[dict, AssignmentBatch | None]:
        """Re-pair only approved work; light events cannot create work."""
        del decision
        snapshot = self.build_mission_snapshot(
            current_time,
            active_tasks=active_tasks,
        )
        approved_ids = tuple(
            task.task_id
            for task in snapshot.active_tasks
            if task.status == "approved" and task.assigned_uav_id is None
        )
        assignments = self.mission_scheduler.pair_approved_tasks(
            snapshot,
            task_ids=approved_ids,
        )
        self.trigger_manager.mark_triggered("light", current_time)
        if not assignments:
            return {
                "trigger_type": "light",
                "action": "approved_tasks_deferred",
                "pairs": [],
                "snapshot_id": snapshot.snapshot_id,
            }, None
        batch = AssignmentBatch(
            snapshot.snapshot_id,
            assignments,
            "light-approved-pairing",
        )
        self.sm.add_event("mission_assignment_approved", {
            "snapshot_id": snapshot.snapshot_id,
            "selection_call_id": batch.selection_call_id,
            "task_ids": [assignment.task_id for assignment in assignments],
        })
        return {
            "trigger_type": "light",
            "action": "approved_task_pairing",
            "pairs": [
                (assignment.uav_id, assignment.task_id)
                for assignment in assignments
            ],
            "snapshot_id": snapshot.snapshot_id,
        }, batch

    def _mission_resources(self) -> tuple[UavResource, ...]:
        speed = (
            self.config.uav.cruise_speed_kmh
            / self.config.grid.cell_size_km
            / 60.0
        )
        total_range = (
            self.config.uav.sortie_endurance_h
            * self.config.uav.cruise_speed_kmh
            / self.config.grid.cell_size_km
        )
        resources = []
        for uav in self.sm.get_all_uavs():
            resources.append(UavResource(
                uav_id=uav.id,
                position_cells=(float(uav.position.col), float(uav.position.row)),
                heading_rad=math.radians(float(uav.heading_deg)),
                speed_cells_min=speed,
                remaining_range_cells=max(
                    0.0, total_range * float(uav.fuel_remaining_pct)
                ),
                operation=str(uav.operation_mode or uav.status),
                current_task_id=uav.assigned_region_id,
                generation=int(uav.controller_generation),
                last_reassigned_at_min=float(
                    getattr(uav, "last_reassigned_at_min", 0.0)
                ),
            ))
        return tuple(sorted(resources, key=lambda resource: resource.uav_id))

    def _ordinary_search_resource(
        self,
        resource: UavResource,
        active_tasks: dict[str, TaskRecord],
    ) -> bool:
        if (
            resource.current_task_id is None
            or str(resource.operation).lower() not in {
                "coverage", "search", "searching", "transit",
            }
        ):
            return False
        state = self.sm.get_uav(resource.uav_id)
        if state is not None and (
            state.control_mode != "heuristic"
            or state.control_owner != "heuristic"
        ):
            return False
        record = active_tasks.get(resource.current_task_id)
        return record is None or (
            record.kind == "search"
            and record.status in {"approved", "executing"}
            and record.assigned_uav_id == resource.uav_id
        )

    def _mission_edges(
        self,
        candidates: tuple[TaskCandidate, ...],
        resources: tuple[UavResource, ...],
        contacts: tuple[ContactSnapshot, ...],
        planning_map_version: int,
        *,
        active_tasks: tuple[TaskRecord, ...] = (),
    ) -> tuple[FeasibleEdge, ...]:
        contact_positions = {
            contact.contact_id: contact.estimated_position_cells
            for contact in contacts
        }
        bases = self.sm.get_base_positions()
        reserve = float(self.config.control.safety.reserve_range_cells)
        active_candidates = {
            record.task_id: self._active_task_candidate(record, resources)
            for record in active_tasks
            if record.status in {"approved", "executing"}
        }
        tasks_by_id = {candidate.task_id: candidate for candidate in candidates}
        tasks_by_id.update(active_candidates)
        edges = []
        for task in tasks_by_id.values():
            if task.contact_id is not None:
                target = contact_positions.get(task.contact_id)
            elif task.bbox is not None:
                target = (
                    (task.bbox[0] + task.bbox[2]) / 2.0,
                    (task.bbox[1] + task.bbox[3]) / 2.0,
                )
            else:
                target = None
            if target is None:
                continue
            for resource in resources:
                if task.feasible_uav_ids and resource.uav_id not in task.feasible_uav_ids:
                    continue
                route_metrics = self._mission_route_metrics(
                    resource, task, target, planning_map_version,
                )
                if route_metrics is None:
                    continue
                transit_distance, mission_distance, endpoint = route_metrics
                transit = transit_distance / max(resource.speed_cells_min, 1e-6)
                return_range = min(
                    (self._return_route_distance(
                        endpoint,
                        resource,
                        tuple(map(float, base)),
                        planning_map_version,
                    )
                     for base in bases),
                    default=None,
                )
                if return_range is None or (
                    transit_distance + mission_distance + return_range + reserve
                    > resource.remaining_range_cells + 1e-9
                ):
                    continue
                route_cache_key = (
                    f"{planning_map_version}:{resource.uav_id}:{resource.generation}:"
                    f"h={resource.heading_rad:.6f}:"
                    f"{resource.position_cells[0]:.3f},{resource.position_cells[1]:.3f}:"
                    f"{target[0]:.3f},{target[1]:.3f}:{task.task_id}"
                )
                edges.append(FeasibleEdge(
                    task_id=task.task_id,
                    uav_id=resource.uav_id,
                    transit_time_min=transit,
                    mission_range_cells=mission_distance,
                    return_range_cells=return_range,
                    reserve_range_cells=reserve,
                    route_cache_key=route_cache_key,
                ))
        return tuple(sorted(
            edges,
            key=lambda edge: (edge.task_id, edge.transit_time_min, edge.uav_id),
        ))

    @staticmethod
    def _active_task_candidate(
        record: TaskRecord,
        resources: tuple[UavResource, ...],
    ) -> TaskCandidate:
        feasible = (
            (record.assigned_uav_id,)
            if record.assigned_uav_id is not None
            else tuple(resource.uav_id for resource in resources)
        )
        return TaskCandidate(
            task_id=record.task_id,
            kind=record.kind,
            bbox=record.bbox,
            contact_id=record.contact_id,
            intent_ids=record.intent_ids,
            feasible_uav_ids=feasible,
            eligible_since_min=record.created_at_min,
            priority="high" if record.kind != "search" else "medium",
            estimated_duration_min=1.0,
            utility=0.0,
            expected_information_gain=0.0,
        )

    def _mission_route_metrics(
        self,
        resource: UavResource,
        task: TaskCandidate,
        target: tuple[float, float],
        planning_map_version: int,
    ) -> tuple[float, float, tuple[float, float]] | None:
        key = (
            "mission-metrics",
            planning_map_version,
            resource.uav_id,
            resource.generation,
            tuple(round(value, 6) for value in resource.position_cells),
            round(float(resource.heading_rad), 6),
            task.kind,
            task.task_id,
            tuple(task.bbox)
            if task.bbox is not None
            else tuple(round(value, 6) for value in target),
            np.isfinite(self.sm.info_field.last_scan_time).tobytes()
            if task.kind == "search"
            else None,
        )
        cache = self._mission_route_metrics_cache
        if key in cache:
            return cache[key]

        start = (*resource.position_cells, float(resource.heading_rad))
        try:
            if task.kind == "search":
                path_plan = plan_search_route(
                    SearchRouteRequest(
                        uav_id=resource.uav_id,
                        start_pose=start,
                        bbox=tuple(task.bbox),
                        swath_width=(
                            self.config.sensor.sar.swath_km
                            / self.config.grid.cell_size_km
                        ),
                        r_min=1.0,
                        obstacle_mask=np.asarray(
                            self.sm.obstacle_mask, dtype=bool
                        ).copy(),
                        unscanned_mask=~np.isfinite(
                            self.sm.info_field.last_scan_time
                        ),
                        allow_revisit=False,
                        seed=17,
                    )
                )
                path = tuple(path_plan.path)
                if not path or not path_plan.scanned_swath_count:
                    result = None
                else:
                    transit_end = min(
                        max(0, int(path_plan.transit_end_index)),
                        len(path) - 1,
                    )
                    result = (
                        self._path_length(path[: transit_end + 1]),
                        self._path_length(path[transit_end:]),
                        tuple(path[-1][:2]),
                    )
            else:
                radius = (
                    self.config.mission.contact.baseline_standoff_cells
                    if task.kind == "probe"
                    else self.config.mission.contact.near_standoff_cells
                )
                path = self._mission_navigator.plan_to_standoff(
                    start,
                    target,
                    radius,
                    self.sm.obstacle_mask,
                    1.0,
                    planning_map_version,
                )
                if not path:
                    result = None
                else:
                    result = (
                        self._path_length(path),
                        max(0.0, task.estimated_duration_min)
                        * max(resource.speed_cells_min, 0.0),
                        tuple(path[-1][:2]),
                    )
        except (PathNotFoundError, RuntimeError, TypeError, ValueError):
            result = None
        cache[key] = result
        return result

    def _mission_route_distance(
        self,
        resource: UavResource,
        task: TaskCandidate,
        target: tuple[float, float],
        planning_map_version: int,
    ) -> float | None:
        metrics = self._mission_route_metrics(
            resource, task, target, planning_map_version,
        )
        return None if metrics is None else metrics[0]

    def _return_route_distance(
        self,
        target: tuple[float, float],
        resource: UavResource,
        base: tuple[float, float],
        planning_map_version: int,
    ) -> float | None:
        key = (
            "return", planning_map_version, resource.uav_id, resource.generation,
            tuple(round(value, 6) for value in target),
            tuple(round(value, 6) for value in base),
        )
        if key in self._mission_route_cache:
            return self._mission_route_cache[key]
        heading = math.atan2(base[1] - target[1], base[0] - target[0])
        try:
            path = self._mission_navigator.plan_grid(
                (*target, heading), {base}, self.sm.obstacle_mask, 1.0,
                planning_map_version,
            )
        except (PathNotFoundError, TypeError, ValueError):
            distance = None
        else:
            distance = self._path_length(path)
        self._mission_route_cache[key] = distance
        return distance

    @staticmethod
    def _path_length(path) -> float:
        return sum(
            math.dist(start[:2], end[:2])
            for start, end in zip(path, path[1:])
        )

    def retire_search_track_conflicts(
        self,
    ) -> list[tuple[Region, str | None]]:
        retired = self.sm.retire_search_regions_overlapping_tracks()
        for region, assigned_uav_id in retired:
            self.ivt.remove_row(region.id)
            self.sm.add_event("search_region_retired_for_tracking", {
                "region_id": region.id,
                "assigned_uav_id": assigned_uav_id,
            })
        return retired

    def step(self, current_time: float) -> dict:
        """Advance one frame and return a summary of actions taken."""
        self.sm.step(current_time)

        # Reviewer update: periodically generate long-term memory
        new_memory = self.reviewer.step(current_time, self.sm)
        if new_memory:
            self.llm_client.set_reviewer_memory(new_memory)

        # Check triggers
        decision = self.trigger_manager.check(current_time)

        if decision.trigger_type == "none":
            return {"trigger_type": "none", "action": None}

        if decision.trigger_type == "light":
            return self._handle_light_trigger(current_time, decision)

        if decision.trigger_type == "heavy":
            return self._handle_heavy_trigger(current_time, decision)

        return {"trigger_type": "none", "action": None}

    # ------------------------------------------------------------------
    # Light trigger: Hungarian pairing only
    # ------------------------------------------------------------------

    def _handle_light_trigger(self, current_time: float, decision) -> dict:
        """Pair idle UAVs only with regions already approved by the LLM."""
        self.retire_search_track_conflicts()
        eligible_uavs = self.sm.get_available_uavs()
        if not eligible_uavs:
            return {"trigger_type": "light", "action": "no_idle_uavs"}

        unassigned = [
            {"id": region.id, "bbox": region.bbox}
            for region in self.sm.get_active_search_regions()
            if region.assigned_uav_id is None
        ]

        if not unassigned:
            candidates = self.extractor.extract(self.sm)
            if candidates.candidate_regions:
                # Leaving refuelled/completed airframes idle until the next
                # periodic window wastes the bounded information lifetime.
                # Escalate only when new legal work exists; the real LongCat
                # decision remains the authority for the new partition.
                return self._handle_heavy_trigger(
                    current_time,
                    decision,
                    candidate_result=candidates,
                )
            return {"trigger_type": "light", "action": "no_eligible_regions"}

        # Hungarian pairing
        pairs = hungarian_pair(
            [{"id": u.id, "position": u.position} for u in eligible_uavs],
            unassigned,
        )

        # Update UAV statuses with assignments
        for uav_id, region_id in pairs:
            uav = self.sm.get_uav(uav_id)
            if uav:
                self.sm.update_uav_status(
                    uav_id, "transit", uav.position,
                    assigned_region_id=region_id,
                )
            for region in self.sm.get_search_regions():
                if region.id == region_id:
                    region.assigned_uav_id = uav_id
                    break

        self.trigger_manager.mark_triggered("light", current_time)
        return {
            "trigger_type": "light",
            "action": "hungarian_pairing",
            "pairs": pairs,
        }

    # ------------------------------------------------------------------
    # Heavy trigger: full LLM pipeline
    # ------------------------------------------------------------------

    def _handle_heavy_trigger(
        self,
        current_time: float,
        decision,
        candidate_result: CandidateResult | None = None,
    ) -> dict:
        """Heavy trigger: retain executing work and ask the LLM for additions."""
        self.retire_search_track_conflicts()
        # Step 1: Update info-value table
        self.ivt.update_all()

        # Step 2: Extract candidate regions
        candidate_result = candidate_result or self.extractor.extract(self.sm)

        retained_regions = list(self.sm.get_active_search_regions())
        pending_regions = sum(
            region.assigned_uav_id is None for region in retained_regions
        )
        remaining_slots = max(
            0,
            self.config.uav.count_max
            - len(self.sm.get_track_regions())
            - len(retained_regions),
        )
        if self.sm.lifecycle_mode:
            required_search_regions = min(
                remaining_slots,
                len(candidate_result.candidate_regions),
            )
        else:
            required_search_regions = min(
                max(0, len(self.sm.get_available_uavs()) - pending_regions),
                remaining_slots,
                len(candidate_result.candidate_regions),
            )

        # Step 3-5: LLM decision (with validation retries built in)
        llm_output = self.llm_client.decide(
            self.sm,
            self.ivt,
            candidate_result,
            required_search_regions=required_search_regions,
        )
        interaction = self.llm_client.last_interaction or {}

        # A failed external decision is fail-closed: the last real, validated
        # plan keeps executing and no synthetic region is introduced.
        if not interaction.get("success"):
            pairs = self._pair_available_regions(retained_regions)
            return self._finish_heavy_trigger(
                current_time,
                retained_regions,
                pairs,
                "llm_failed_plan_preserved",
                llm_output.get("notes", ""),
                interaction,
            )

        # Step 6: Create Region objects with ID continuity
        new_regions = []
        candidate_target_by_bbox = {
            tuple(candidate["bbox"]): candidate.get("target_group_id")
            for candidate in candidate_result.candidate_regions
            if candidate.get("target_group_id")
        }
        prev_regions = self.sm.get_previous_search_regions()
        prev_by_id = {r.id: r for r in prev_regions}
        assigned_ids: set[str] = {region.id for region in retained_regions}

        for sr in llm_output.get("search_regions", []):
            bbox = BBox(*sr["bbox"])

            # Start with the ID the LLM assigned (or auto-generate)
            matched_id = sr.get("id", f"S{len(new_regions) + 1}")

            # ID continuity: if IoU with a previous region is high
            # enough, reuse that region's ID
            if matched_id not in assigned_ids:
                for prev_id, prev_r in prev_by_id.items():
                    if prev_id not in assigned_ids:
                        iou = self._iou(bbox, prev_r.bbox)
                        if iou >= self.config.grid.stability_iou_threshold:
                            matched_id = prev_id
                            break

            if matched_id in assigned_ids:
                suffix = 1
                while f"S{suffix}" in assigned_ids:
                    suffix += 1
                matched_id = f"S{suffix}"
            assigned_ids.add(matched_id)

            region = Region(
                id=matched_id,
                bbox=bbox,
                type="search",
                priority=(
                    "high" if tuple(bbox) in candidate_target_by_bbox
                    else sr.get("priority", "medium")
                ),
                info_value=0.0,  # calculated later by InfoValueTable
                target_group_id=candidate_target_by_bbox.get(tuple(bbox)),
            )
            new_regions.append(region)

        combined_regions = [*retained_regions, *new_regions]
        self.sm.set_search_regions(combined_regions)

        # Update IVT: add rows for new regions, remove stale ones
        for r in new_regions:
            self.ivt.add_row(r.id, r.bbox, "search")
        new_ids = {r.id for r in combined_regions}
        for row in list(self.ivt.get_rows()):
            if row.type == "search" and row.region_id not in new_ids:
                self.ivt.remove_row(row.region_id)

        # Step 7: Hungarian pairing of idle UAVs to unassigned regions
        pairs = self._pair_available_regions(combined_regions)
        return self._finish_heavy_trigger(
            current_time,
            combined_regions,
            pairs,
            "llm_reallocation",
            llm_output.get("notes", ""),
            interaction,
        )

    def _pair_available_regions(self, regions: list[Region]) -> list[tuple[str, str]]:
        eligible_uavs = self.sm.get_available_uavs()
        unassigned = [
            {"id": region.id, "bbox": region.bbox}
            for region in regions
            if region.assigned_uav_id is None
        ]
        pairs = hungarian_pair(
            [{"id": uav.id, "position": uav.position} for uav in eligible_uavs],
            unassigned,
        )
        by_id = {region.id: region for region in regions}
        for uav_id, region_id in pairs:
            uav = self.sm.get_uav(uav_id)
            if uav is None or region_id not in by_id:
                continue
            self.sm.update_uav_status(
                uav_id,
                "transit",
                uav.position,
                assigned_region_id=region_id,
            )
            by_id[region_id].assigned_uav_id = uav_id
            row = self.ivt.get_row(region_id)
            if row is not None:
                row.assigned_uav_id = uav_id
        return pairs

    def _finish_heavy_trigger(
        self,
        current_time: float,
        regions: list[Region],
        pairs: list[tuple[str, str]],
        action: str,
        notes: str,
        interaction: dict,
    ) -> dict:
        self.trigger_manager.mark_triggered("heavy", current_time)
        self.sm.cycle += 1
        self.sm.add_event("llm_decision", {
            "cycle": self.sm.cycle,
            "success": bool(interaction.get("success")),
            "regions": len(regions),
        })
        return {
            "trigger_type": "heavy",
            "action": action,
            "search_regions": [
                {"id": region.id, "bbox": list(region.bbox)}
                for region in regions
            ],
            "pairs": pairs,
            "notes": notes,
            "llm_cycle": interaction,
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _iou(a: BBox, b: BBox) -> float:
        """Intersection-over-Union of two bounding boxes."""
        if a.col_end <= b.col_start or b.col_end <= a.col_start:
            return 0.0
        if a.row_end <= b.row_start or b.row_end <= a.row_start:
            return 0.0
        inter_w = min(a.col_end, b.col_end) - max(a.col_start, b.col_start)
        inter_h = min(a.row_end, b.row_end) - max(a.row_start, b.row_start)
        inter = inter_w * inter_h
        area_a = (a.col_end - a.col_start) * (a.row_end - a.row_start)
        area_b = (b.col_end - b.col_start) * (b.row_end - b.row_start)
        union = area_a + area_b - inter
        return inter / union if union > 0 else 0.0
