from schedule.config_loader import ConfigLoader, AppConfig
from schedule.state_manager import StateManager
from schedule.info_value_table import InfoValueTable
from schedule.candidate_extractor import CandidateExtractor, CandidateResult
from schedule.llm_client import LLMClient
from schedule.llm_reviewer import LLMReviewer
from schedule.hungarian import hungarian_pair
from schedule.trigger_manager import TriggerManager
from schedule.output_validator import validate
from schedule.datatypes import Region, BBox, GridCoord


class TaskAllocator:
    """Main orchestrator connecting all scheduling components."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.sm = StateManager(config)
        self.ivt = InfoValueTable(self.sm)
        self.extractor = CandidateExtractor()
        self.llm_client = LLMClient(config)
        self.reviewer = LLMReviewer(config)
        self.trigger_manager = TriggerManager(self.sm)

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
        """Lightweight trigger: direct Hungarian pairing of idle UAVs
        with unassigned candidate regions."""
        idle_uavs = self.sm.get_available_uavs()
        if not idle_uavs:
            return {"trigger_type": "light", "action": "no_idle_uavs"}

        # Extract candidate regions and assign temporary IDs
        candidate_result = self.extractor.extract(self.sm)
        candidates_with_id = []
        for i, c in enumerate(candidate_result.candidate_regions):
            enriched = dict(c)
            enriched["id"] = f"C{i + 1}"
            candidates_with_id.append(enriched)

        # Filter out candidates whose bbox already matches an existing
        # search region
        existing_bboxes = {r.bbox for r in self.sm.get_search_regions()}
        unassigned = [c for c in candidates_with_id
                      if c["bbox"] not in existing_bboxes]

        if not unassigned:
            return {"trigger_type": "light",
                    "action": "no_unassigned_regions"}

        # Hungarian pairing
        pairs = hungarian_pair(
            [{"id": u.id, "position": u.position} for u in idle_uavs],
            unassigned,
        )

        # Update UAV statuses with assignments
        candidate_by_id = {c["id"]: c for c in candidates_with_id}
        for uav_id, region_id in pairs:
            candidate = candidate_by_id.get(region_id)
            if candidate:
                uav = self.sm.get_uav(uav_id)
                if uav:
                    self.sm.update_uav_status(
                        uav_id, "transit", uav.position,
                        assigned_region_id=region_id,
                    )

        self.trigger_manager.mark_triggered("light", current_time)
        return {
            "trigger_type": "light",
            "action": "hungarian_pairing",
            "pairs": pairs,
        }

    # ------------------------------------------------------------------
    # Heavy trigger: full LLM pipeline
    # ------------------------------------------------------------------

    def _handle_heavy_trigger(self, current_time: float, decision) -> dict:
        """Heavy trigger: full LLM pipeline for global reallocation."""
        # Step 1: Update info-value table
        self.ivt.update_all()

        # Step 2: Extract candidate regions
        candidate_result = self.extractor.extract(self.sm)

        # Step 3-5: LLM decision (with validation retries built in)
        llm_output = self.llm_client.decide(self.sm, self.ivt, candidate_result)

        # Step 6: Create Region objects with ID continuity
        new_regions = []
        prev_regions = self.sm.get_previous_search_regions()
        prev_by_id = {r.id: r for r in prev_regions}
        assigned_ids: set[str] = set()

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

            assigned_ids.add(matched_id)

            region = Region(
                id=matched_id,
                bbox=bbox,
                type="search",
                priority=sr.get("priority", "medium"),
                info_value=0.0,  # calculated later by InfoValueTable
            )
            new_regions.append(region)

        self.sm.set_search_regions(new_regions)

        # Update IVT: add rows for new regions, remove stale ones
        for r in new_regions:
            self.ivt.add_row(r.id, r.bbox, "search")
        new_ids = {r.id for r in new_regions}
        for row in list(self.ivt.get_rows()):
            if row.type == "search" and row.region_id not in new_ids:
                self.ivt.remove_row(row.region_id)

        # Step 7: Hungarian pairing of idle UAVs to unassigned regions
        idle_uavs = self.sm.get_available_uavs()
        unassigned = [
            {"id": r.id, "bbox": r.bbox}
            for r in new_regions
            if r.assigned_uav_id is None
        ]
        pairs = hungarian_pair(
            [{"id": u.id, "position": u.position} for u in idle_uavs],
            unassigned,
        )

        for uav_id, region_id in pairs:
            uav = self.sm.get_uav(uav_id)
            if uav:
                self.sm.update_uav_status(
                    uav_id, "transit", uav.position,
                    assigned_region_id=region_id,
                )
            for r in new_regions:
                if r.id == region_id:
                    r.assigned_uav_id = uav_id
                    break

        self.trigger_manager.mark_triggered("heavy", current_time)
        self.sm.cycle += 1

        return {
            "trigger_type": "heavy",
            "action": "llm_reallocation",
            "search_regions": [{"id": r.id, "bbox": list(r.bbox)}
                               for r in new_regions],
            "pairs": pairs,
            "notes": llm_output.get("notes", ""),
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
