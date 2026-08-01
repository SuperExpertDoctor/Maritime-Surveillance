from src.schedule.config_loader import ConfigLoader, AppConfig
from src.schedule.state_manager import StateManager
from src.schedule.info_value_table import InfoValueTable
from src.schedule.candidate_extractor import CandidateExtractor, CandidateResult
from src.schedule.llm_client import LLMClient
from src.schedule.llm_reviewer import LLMReviewer
from src.schedule.hungarian import hungarian_pair
from src.schedule.trigger_manager import TriggerManager
from src.schedule.output_validator import validate
from src.schedule.datatypes import Region, BBox, GridCoord


class TaskAllocator:
    """Main orchestrator connecting all scheduling components."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.sm = StateManager(config)
        self.ivt = InfoValueTable(self.sm)
        self.extractor = CandidateExtractor()
        self.llm_client = LLMClient(config)
        self.reviewer = LLMReviewer(config, self.llm_client)
        self.trigger_manager = TriggerManager(self.sm)

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
        idle_uavs = self.sm.get_available_uavs()
        if not idle_uavs:
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
            [{"id": u.id, "position": u.position} for u in idle_uavs],
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
        idle_uavs = self.sm.get_available_uavs()
        unassigned = [
            {"id": region.id, "bbox": region.bbox}
            for region in regions
            if region.assigned_uav_id is None
        ]
        pairs = hungarian_pair(
            [{"id": uav.id, "position": uav.position} for uav in idle_uavs],
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
