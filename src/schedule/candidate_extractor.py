import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from src.schedule.datatypes import BBox, GridCoord
from src.schedule.state_manager import StateManager
from src.utils.coverage_planner import CoveragePlanner


@dataclass
class CandidateResult:
    candidate_regions: list[dict] = field(default_factory=list)
    fragment_alerts: list[dict] = field(default_factory=list)


class CandidateExtractor:
    def __init__(self):
        self.coverage_planner = CoveragePlanner(sample_step=0.25)

    def extract(self, sm: StateManager) -> CandidateResult:
        gc = sm.config.grid
        cols, rows = gc.resolution
        V = sm.get_value_matrix()
        I = sm.get_info_matrix()
        seen = np.isfinite(sm.info_field.last_scan_time)
        searchable = sm.get_searchable_mask()
        searchable_cells = int(searchable.sum())
        unique_coverage = (
            int((seen & searchable).sum()) / searchable_cells
            if searchable_cells
            else 0.0
        )
        exploration_mode = unique_coverage < 0.80

        # Step 1: track-region occupancy mask
        occupied = np.zeros((cols, rows), dtype=bool)
        occupied |= getattr(sm, "obstacle_mask", occupied)
        occupied |= getattr(sm, "land_mask", occupied)
        for col, row in sm.get_base_positions():
            occupied[col, row] = True
        # A one-cell flight margin lets a radius-1 Dubins U-turn bulge
        # outside every candidate rectangle without leaving the map.
        occupied[0, :] = True
        occupied[cols - 1, :] = True
        occupied[:, 0] = True
        occupied[:, rows - 1] = True
        for tr in sm.get_track_regions():
            b = tr.bbox
            occupied[b.col_start:b.col_end, b.row_start:b.row_end] = True
        active_search = sm.get_active_search_regions()
        # Completed regions remain visible in history but are no longer an
        # active airspace reservation.  Once their information decays, they
        # must be eligible for a fresh SAR revisit.
        for region in active_search:
            b = region.bbox
            occupied[b.col_start:b.col_end, b.row_start:b.row_end] = True

        # Step 2: high-value cell clustering (connected components)
        threshold = gc.candidate_value_threshold
        high_value_mask = (V >= threshold) & ~occupied
        clusters = self._connected_components(high_value_mask, V, I)

        # Step 3: sort by total value descending
        clusters.sort(key=lambda c: c["total_value"], reverse=True)

        # Step 4: Top-K selection (on clusters, not final candidates)
        pending_regions = sum(
            region.assigned_uav_id is None for region in active_search
        )
        available = max(0, len(sm.get_available_uavs()) - pending_regions)
        # During the lifecycle rotation all UAVs may be returning at once.
        # Keep legal, LLM-approved recovery sorties ready before they land so
        # refueled airframes can launch immediately instead of waiting for
        # the next 30-minute planning window.
        has_handoff_report = any(
            sm.get_track_region_for_group(report.group_id) is None
            for report in sm.get_target_reports()
        )
        K = 10 if sm.lifecycle_mode else min(max(available * 2, int(has_handoff_report)), 10)
        clusters = clusters[:max(K * 4, K)]

        # Step 5: rectangle fitting and track-region overlap filtering
        track_bboxes = [tr.bbox for tr in sm.get_track_regions()]
        candidates = []
        for cluster in clusters:
            fitted_list = self._fit_rectangle(cluster["cells"], gc, cols, rows)
            for fitted in fitted_list:
                # Exclude candidates whose bbox overlaps any track region
                if any(
                    self._bboxes_overlap(fitted["bbox"], tb)
                    for tb in track_bboxes
                ):
                    continue
                # Bug 2 fix: recompute total_value / avg_info from the
                # sub-candidate's own bbox, not the whole cluster.
                bbox = fitted["bbox"]
                if not gc.search_min_cells <= fitted["cell_count"] <= gc.search_max_cells:
                    continue
                if np.any(occupied[
                    bbox.col_start:bbox.col_end,
                    bbox.row_start:bbox.row_end,
                ]):
                    continue
                if not self._has_turning_clearance(bbox, sm.obstacle_mask):
                    continue
                swath_width = (
                    sm.config.sensor.sar.swath_km
                    / sm.config.grid.cell_size_km
                )
                if not self.coverage_planner.is_region_feasible(
                    bbox,
                    swath_width,
                    1.0,
                    sm.obstacle_mask,
                ):
                    continue
                patch_V = V[bbox.col_start:bbox.col_end,
                            bbox.row_start:bbox.row_end]
                patch_I = I[bbox.col_start:bbox.col_end,
                            bbox.row_start:bbox.row_end]
                fitted["total_value"] = float(np.sum(patch_V))
                fitted["avg_info"] = float(np.mean(patch_I))
                fitted["unseen_count"] = int((~seen[
                    bbox.col_start:bbox.col_end,
                    bbox.row_start:bbox.row_end,
                ]).sum())
                if (
                    exploration_mode
                    and fitted["unseen_count"] / fitted["cell_count"] < 0.25
                ):
                    continue
                candidates.append(fitted)

        # Connected-component bounding boxes lose navigable pockets when a
        # concave sea component wraps around an obstacle. Enumerate compact,
        # fully feasible windows so those pockets remain schedulable.
        candidates.extend(self._fit_feasible_windows(
            sm,
            occupied,
            V,
            I,
            seen,
            K,
            exploration_mode,
        ))

        # A returning tracker leaves a sensor-derived report, not a live ship
        # position.  Offer a compact high-priority search box around its
        # short-horizon projection so the LLM can explicitly plan hand-off.
        candidates.extend(self._handoff_candidates(sm, occupied, V, I, seen))

        # Cap final candidates at K and keep them mutually disjoint so a model
        # can safely copy the supplied candidate list as its additions.
        base_positions = sm.get_base_positions()
        ready_positions = [
            (uav.position.col, uav.position.row)
            for uav in sm.get_available_uavs()
        ] or list(base_positions)
        def candidate_key(item):
            bbox = item["bbox"]
            area = (bbox.col_end - bbox.col_start) * (bbox.row_end - bbox.row_start)
            unseen_count = item.get("unseen_count", 0)
            unseen_density = unseen_count / max(area, 1)
            center = (
                (bbox.col_start + bbox.col_end) / 2,
                (bbox.row_start + bbox.row_end) / 2,
            )
            distance = min(
                math.dist(center, ready_position)
                for ready_position in ready_positions
            )
            if item.get("target_group_id"):
                return (-2, distance, -item["total_value"])
            if sm.lifecycle_mode:
                return (-unseen_density, distance,
                        abs(area - gc.search_min_cells),
                        -unseen_count, -item["total_value"])
            if exploration_mode:
                # Initial sea cells are equally unknown.  Prefer legal work
                # close to a coastal launch point in that tie so the first
                # sortie spends its limited early window on SAR imaging
                # rather than a long deadhead transit across the map.
                return (-unseen_density, -unseen_count, distance,
                        abs(area - 24), -item["total_value"])
            return (-item["total_value"], distance)

        candidates.sort(key=candidate_key)
        if sm.lifecycle_mode:
            candidates = self._order_lifecycle_candidates(candidates, sm, base_positions)
        selected = []
        seen_bboxes = set()
        for candidate in candidates:
            bbox = candidate["bbox"]
            if self._distance_to_bases(bbox, base_positions) < (
                sm.config.environment.base_task_min_distance_cells
            ):
                continue
            if sm.lifecycle_mode:
                center = (
                    (bbox.col_start + bbox.col_end) / 2,
                    (bbox.row_start + bbox.row_end) / 2,
                )
                if min(
                    math.dist(center, base_position)
                    for base_position in base_positions
                ) > sm.config.uav.lifecycle_candidate_max_distance_cells:
                    continue
            bbox_key = tuple(bbox)
            if bbox_key in seen_bboxes:
                continue
            seen_bboxes.add(bbox_key)
            if any(self._bboxes_overlap(bbox, item["bbox"]) for item in selected):
                continue
            candidate.pop("unseen_count", None)
            selected.append(candidate)
            if len(selected) >= K:
                break
        candidates = selected

        # Step 6: fragment detection
        fragments = self._detect_fragments(sm, occupied, gc)

        return CandidateResult(
            candidate_regions=candidates,
            fragment_alerts=fragments,
        )

    def _handoff_candidates(
        self,
        sm: StateManager,
        occupied: np.ndarray,
        values: np.ndarray,
        info: np.ndarray,
        seen: np.ndarray,
    ) -> list[dict]:
        """Build legal successor search regions from observed target reports."""
        active_groups = {
            region.target_group_id
            for region in sm.get_track_regions()
            if region.target_group_id
        }
        gc = sm.config.grid
        cols, rows = gc.resolution
        side = int(math.ceil(math.sqrt(gc.search_min_cells)))
        side = max(1, min(side, gc.search_max_cells))
        half = side // 2
        swath_width = sm.config.sensor.sar.swath_km / gc.cell_size_km
        candidates = []

        for report in sm.get_target_reports():
            if report.group_id in active_groups:
                continue
            elapsed = max(0.0, sm.current_time - report.observed_at)
            horizon = min(12.0, elapsed + 5.0)
            predicted = (
                report.position.col + report.velocity_cells_per_min[0] * horizon,
                report.position.row + report.velocity_cells_per_min[1] * horizon,
            )
            seed_col, seed_row = int(round(predicted[0])), int(round(predicted[1]))
            # Try a nearby deterministic ring when the direct projection
            # intersects land, an island, or a thunderstorm safety area.
            offsets = [(0, 0), (side, 0), (-side, 0), (0, side), (0, -side),
                       (side, side), (-side, side), (side, -side), (-side, -side)]
            for dc, dr in offsets:
                center_col = min(cols - half - 1, max(half + 1, seed_col + dc))
                center_row = min(rows - half - 1, max(half + 1, seed_row + dr))
                bbox = BBox(
                    center_col - half,
                    center_row - half,
                    center_col - half + side,
                    center_row - half + side,
                )
                if not gc.search_min_cells <= side * side <= gc.search_max_cells:
                    continue
                if np.any(occupied[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]):
                    continue
                if not self._has_turning_clearance(bbox, sm.obstacle_mask):
                    continue
                if self._distance_to_bases(bbox, sm.get_base_positions()) < (
                    sm.config.environment.base_task_min_distance_cells
                ):
                    continue
                if not self.coverage_planner.is_region_feasible(
                    bbox, swath_width, 1.0, sm.obstacle_mask,
                ):
                    continue
                patch_value = values[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]
                patch_info = info[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]
                candidates.append({
                    "bbox": bbox,
                    "cell_count": side * side,
                    # The priority comes from the observed contact, not from
                    # an unobserved target or a fabricated information value.
                    "total_value": float(patch_value.sum()) + 1000.0,
                    "avg_info": float(patch_info.mean()),
                    "unseen_count": int((~seen[bbox.col_start:bbox.col_end, bbox.row_start:bbox.row_end]).sum()),
                    "target_group_id": report.group_id,
                    "observed_position": [report.position.col, report.position.row],
                    "observed_at": report.observed_at,
                })
                break
        return candidates

    def _order_lifecycle_candidates(
        self,
        candidates: list[dict],
        sm: StateManager,
        base_positions: tuple,
    ) -> list[dict]:
        """Put one short, non-overlapping sortie near each UAV first.

        LongCat still chooses only from the candidate list.  This merely
        avoids offering a left-coast airframe a right-coast recovery sortie
        when a legal local rectangle is available.
        """
        max_distance = sm.config.uav.lifecycle_candidate_max_distance_cells
        positions = [
            (uav.position.col, uav.position.row)
            for uav in sm.get_all_uavs()
        ]
        ordered: list[dict] = []
        used_bboxes: set[tuple] = set()

        def center(item: dict) -> tuple[float, float]:
            bbox = item["bbox"]
            return (
                (bbox.col_start + bbox.col_end) / 2,
                (bbox.row_start + bbox.row_end) / 2,
            )

        def nearby_base(item: dict) -> bool:
            point = center(item)
            return min(math.dist(point, base) for base in base_positions) <= max_distance

        for position in positions:
            options = []
            for item in candidates:
                bbox = item["bbox"]
                bbox_key = tuple(bbox)
                if bbox_key in used_bboxes or not nearby_base(item):
                    continue
                if any(self._bboxes_overlap(bbox, picked["bbox"]) for picked in ordered):
                    continue
                distance = math.dist(center(item), position)
                if distance > max_distance:
                    continue
                density = item.get("unseen_count", 0) / max(item["cell_count"], 1)
                options.append((
                    -density,
                    distance,
                    abs(item["cell_count"] - sm.config.grid.search_min_cells),
                    -item.get("unseen_count", 0),
                    -item["total_value"],
                    item,
                ))
            if not options:
                continue
            _, _, _, _, _, chosen = min(options, key=lambda item: item[:-1])
            ordered.append(chosen)
            used_bboxes.add(tuple(chosen["bbox"]))

        # Preserve the general lifecycle priority for any unused capacity.
        ordered_keys = {tuple(item["bbox"]) for item in ordered}
        remaining = [
            item for item in candidates
            if tuple(item["bbox"]) not in ordered_keys
        ]
        remaining.sort(key=lambda item: (
            abs(item["cell_count"] - sm.config.grid.search_min_cells),
            min(math.dist(center(item), base) for base in base_positions),
            -item.get("unseen_count", 0),
            -item["total_value"],
        ))
        return [
            *ordered,
            *remaining,
        ]

    def _fit_feasible_windows(
        self,
        sm: StateManager,
        occupied: np.ndarray,
        values: np.ndarray,
        info: np.ndarray,
        seen: np.ndarray,
        limit: int,
        exploration_mode: bool,
    ) -> list[dict]:
        """Find spatially diverse feasible windows inside irregular free sea."""
        if limit <= 0:
            return []
        gc = sm.config.grid
        cols, rows = gc.resolution
        base_positions = sm.get_base_positions()
        raw = []
        for width in range(1, gc.search_max_cells + 1):
            for height in range(1, gc.search_max_cells + 1):
                area = width * height
                if not gc.search_min_cells <= area <= gc.search_max_cells:
                    continue
                if max(width, height) / min(width, height) > gc.aspect_ratio_max:
                    continue
                for c0 in range(1, cols - width):
                    for r0 in range(1, rows - height):
                        c1, r1 = c0 + width, r0 + height
                        if occupied[c0:c1, r0:r1].any():
                            continue
                        unseen_count = int((~seen[c0:c1, r0:r1]).sum())
                        if exploration_mode and unseen_count / area < 0.25:
                            continue
                        bbox = BBox(c0, r0, c1, r1)
                        if not self._has_turning_clearance(bbox, sm.obstacle_mask):
                            continue
                        distance = self._distance_to_bases(bbox, base_positions)
                        if distance < sm.config.environment.base_task_min_distance_cells:
                            continue
                        raw.append({
                            "bbox": bbox,
                            "cell_count": area,
                            "unseen_count": unseen_count,
                            "total_value": float(values[c0:c1, r0:r1].sum()),
                            "avg_info": float(info[c0:c1, r0:r1].mean()),
                            "distance": distance,
                        })

        if sm.lifecycle_mode:
            raw.sort(key=lambda item: (
                -(item["unseen_count"] / item["cell_count"]),
                item["distance"],
                abs(item["cell_count"] - gc.search_min_cells),
                -item["unseen_count"],
                -item["total_value"],
            ))
            raw = self._order_lifecycle_candidates(raw, sm, base_positions)
        else:
            raw.sort(key=lambda item: (
                -(item["unseen_count"] / item["cell_count"]),
                -item["unseen_count"],
                abs(item["cell_count"] - 24),
                -item["total_value"],
                item["distance"],
            ))
        swath_width = sm.config.sensor.sar.swath_km / gc.cell_size_km
        selected = []
        for item in raw:
            bbox = item["bbox"]
            if (
                sm.lifecycle_mode
                and item["distance"]
                > sm.config.uav.lifecycle_candidate_max_distance_cells
            ):
                continue
            if any(self._bboxes_overlap(bbox, other["bbox"]) for other in selected):
                continue
            if not self.coverage_planner.is_region_feasible(
                bbox,
                swath_width,
                1.0,
                sm.obstacle_mask,
            ):
                continue
            item.pop("distance")
            selected.append(item)
            if len(selected) >= max(limit * 4, 20):
                break
        return selected

    @staticmethod
    def _has_turning_clearance(
        bbox: BBox,
        obstacle_mask: np.ndarray,
        margin_cells: int = 2,
    ) -> bool:
        """Reserve space for the radius-1 Dubins turns between SAR swaths."""
        c0 = max(0, bbox.col_start - margin_cells)
        r0 = max(0, bbox.row_start - margin_cells)
        c1 = min(obstacle_mask.shape[0], bbox.col_end + margin_cells)
        r1 = min(obstacle_mask.shape[1], bbox.row_end + margin_cells)
        return not bool(obstacle_mask[c0:c1, r0:r1].any())

    # ------------------------------------------------------------------
    # Connected-component analysis
    # ------------------------------------------------------------------

    def _connected_components(
        self, mask: np.ndarray, V: np.ndarray, I: np.ndarray
    ) -> list[dict]:
        """Flood-fill connected-component extraction on the high-value mask."""
        cols, rows = mask.shape
        visited = np.zeros_like(mask, dtype=bool)
        clusters = []

        for c in range(cols):
            for r in range(rows):
                if mask[c, r] and not visited[c, r]:
                    cells, total_value, total_info = self._flood_fill(
                        mask, V, I, visited, c, r, cols, rows
                    )
                    avg_info = total_info / len(cells) if cells else 0.0
                    clusters.append(
                        {
                            "cells": cells,
                            "total_value": total_value,
                            "avg_info": avg_info,
                        }
                    )

        return clusters

    def _flood_fill(
        self,
        mask: np.ndarray,
        V: np.ndarray,
        I: np.ndarray,
        visited: np.ndarray,
        start_c: int,
        start_r: int,
        cols: int,
        rows: int,
    ):
        """BFS flood-fill returning cells, total value, and total info."""
        q = deque([(start_c, start_r)])
        visited[start_c, start_r] = True
        cells = []
        total_value = 0.0
        total_info = 0.0

        while q:
            c, r = q.popleft()
            cells.append(GridCoord(c, r))
            total_value += float(V[c, r])
            total_info += float(I[c, r])
            for dc, dr in [(1, 0), (-1, 0), (0, 1), (0, -1)]:
                nc, nr = c + dc, r + dr
                if 0 <= nc < cols and 0 <= nr < rows:
                    if mask[nc, nr] and not visited[nc, nr]:
                        visited[nc, nr] = True
                        q.append((nc, nr))

        return cells, total_value, total_info

    # ------------------------------------------------------------------
    # Rectangle fitting
    # ------------------------------------------------------------------

    def _fit_rectangle(
        self,
        cells: list[GridCoord],
        gc,
        cols: int,
        rows: int,
    ) -> list[dict]:
        """Fit bounding rectangle(s) to a cluster of cells.

        Uses an iterative stack so that every sub-piece is checked
        against all three constraints (tiny-area expansion, aspect-ratio
        split, size-based subdivision) -- fixing Issue 3 (non-recursive).
        """
        if not cells:
            return []

        cs = [cell.col for cell in cells]
        rs = [cell.row for cell in cells]
        c_min, c_max = min(cs), max(cs) + 1
        r_min, r_max = min(rs), max(rs) + 1

        # Stack of (c0, r0, c1, r1) bbox tuples to process
        stack: list[tuple[int, int, int, int]] = [
            (c_min, r_min, c_max, r_max)
        ]
        results: list[dict] = []

        while stack:
            c0, r0, c1, r1 = stack.pop()
            w, h = c1 - c0, r1 - r0
            area = w * h

            # --- tiny-area expansion ---
            if area < gc.fragment_threshold_cells:
                expand = 2
                c0 = max(0, c0 - expand)
                r0 = max(0, r0 - expand)
                c1 = min(cols, c1 + expand)
                r1 = min(rows, r1 + expand)
                w, h = c1 - c0, r1 - r0
                area = w * h

            # --- aspect-ratio split (push halves back to stack) ---
            aspect = max(w, h) / max(min(w, h), 1)
            if aspect > gc.aspect_ratio_max:
                if w > h:
                    mid = (c0 + c1) // 2
                    stack.append((c0, r0, mid, r1))
                    stack.append((mid, r0, c1, r1))
                else:
                    mid = (r0 + r1) // 2
                    stack.append((c0, r0, c1, mid))
                    stack.append((c0, mid, c1, r1))
                continue

            # --- size-based subdivision (push pieces back to stack) ---
            if area > gc.search_max_cells:
                n_cols, n_rows = self._partition_counts(w, h, gc)
                col_edges = [c0 + round(i * w / n_cols) for i in range(n_cols + 1)]
                row_edges = [r0 + round(i * h / n_rows) for i in range(n_rows + 1)]
                for i in range(n_cols):
                    for j in range(n_rows):
                        sub_c0, sub_c1 = col_edges[i], col_edges[i + 1]
                        sub_r0, sub_r1 = row_edges[j], row_edges[j + 1]
                        if sub_c1 > sub_c0 and sub_r1 > sub_r0:
                            stack.append((sub_c0, sub_r0, sub_c1, sub_r1))
                continue

            # --- base case: acceptable bbox ---
            results.append(
                {
                    "bbox": BBox(c0, r0, c1, r1),
                    "cell_count": area,
                }
            )

        return results

    @staticmethod
    def _partition_counts(width: int, height: int, gc) -> tuple[int, int]:
        """Choose the densest grid whose every tile satisfies region limits."""
        choices: list[tuple[float, int, int, int]] = []
        for n_cols in range(1, width + 1):
            widths = (width // n_cols, math.ceil(width / n_cols))
            if widths[0] <= 0:
                continue
            for n_rows in range(1, height + 1):
                heights = (height // n_rows, math.ceil(height / n_rows))
                if heights[0] <= 0:
                    continue
                shapes = [(w, h) for w in widths for h in heights]
                if any(
                    w * h < gc.search_min_cells
                    or w * h > gc.search_max_cells
                    or max(w, h) / min(w, h) > gc.aspect_ratio_max
                    for w, h in shapes
                ):
                    continue
                mean_area = width * height / (n_cols * n_rows)
                # Around 24 cells typically needs two scan lines with the
                # configured two-cell SAR swath, preserving the validated
                # coverage throughput of the normal exploration phase.
                choices.append((-abs(mean_area - 24.0), n_cols * n_rows, n_cols, n_rows))
        if not choices:
            n = max(1, math.ceil(math.sqrt(width * height / gc.search_max_cells)))
            return n, n
        _, _, n_cols, n_rows = max(choices)
        return n_cols, n_rows

    # ------------------------------------------------------------------
    # Fragment detection
    # ------------------------------------------------------------------

    def _detect_fragments(
        self, sm: StateManager, occupied: np.ndarray, gc
    ) -> list[dict]:
        """Detect fragments left after track regions carve into previous
        search regions.

        ``occupied`` is used as a safety filter: any fragment whose bbox
        overlaps a currently-occupied cell is skipped (Bug 4 fix).
        """
        fragments = []
        prev_regions = sm.get_previous_search_regions()
        track_regions = sm.get_track_regions()

        for prev in prev_regions:
            for track in track_regions:
                if self._bboxes_overlap(prev.bbox, track.bbox):
                    remaining = self._bbox_difference(prev.bbox, track.bbox)
                    for rem_bbox in remaining:
                        area = (rem_bbox.col_end - rem_bbox.col_start) * (
                            rem_bbox.row_end - rem_bbox.row_start
                        )
                        if area >= gc.fragment_threshold_cells:
                            continue
                        # Safety: skip if any cell in the fragment is
                        # currently occupied (Bug 4 fix).
                        if np.any(
                            occupied[
                                rem_bbox.col_start:rem_bbox.col_end,
                                rem_bbox.row_start:rem_bbox.row_end,
                            ]
                        ):
                            continue
                        fragments.append(
                            {
                                "bbox": rem_bbox,
                                "area": area,
                                "reason": (
                                    f"区域{prev.id}被{track.id}"
                                    f"挖除后产生{area}格碎片"
                                ),
                                "parent_region_id": prev.id,
                            }
                        )

        return fragments

    # ------------------------------------------------------------------
    # BBox utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _distance_to_bases(bbox: BBox, base_positions: tuple) -> float:
        def distance(base_position) -> float:
            col, row = base_position
            dx = max(bbox.col_start - col, 0, col - bbox.col_end)
            dy = max(bbox.row_start - row, 0, row - bbox.row_end)
            return math.hypot(dx, dy)

        return min(distance(base) for base in base_positions)

    @staticmethod
    def _bboxes_overlap(a: BBox, b: BBox) -> bool:
        if a.col_end <= b.col_start or b.col_end <= a.col_start:
            return False
        if a.row_end <= b.row_start or b.row_end <= a.row_start:
            return False
        return True

    @staticmethod
    def _bbox_difference(a: BBox, b: BBox) -> list[BBox]:
        """Return the rectangular pieces of ``a - b``.

        Simplified: assumes *b* sits entirely inside *a* and returns the
        four surrounding strips (left, right, top, bottom).
        """
        pieces = []
        # left
        if a.col_start < b.col_start:
            pieces.append(
                BBox(a.col_start, a.row_start, b.col_start, a.row_end)
            )
        # right
        if b.col_end < a.col_end:
            pieces.append(
                BBox(b.col_end, a.row_start, a.col_end, a.row_end)
            )
        # top
        if a.row_start < b.row_start:
            pieces.append(
                BBox(
                    max(a.col_start, b.col_start),
                    a.row_start,
                    min(a.col_end, b.col_end),
                    b.row_start,
                )
            )
        # bottom
        if b.row_end < a.row_end:
            pieces.append(
                BBox(
                    max(a.col_start, b.col_start),
                    b.row_end,
                    min(a.col_end, b.col_end),
                    a.row_end,
                )
            )
        return pieces
