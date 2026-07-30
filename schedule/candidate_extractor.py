import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from schedule.datatypes import BBox, GridCoord
from schedule.state_manager import StateManager


@dataclass
class CandidateResult:
    candidate_regions: list[dict] = field(default_factory=list)
    fragment_alerts: list[dict] = field(default_factory=list)


class CandidateExtractor:
    def extract(self, sm: StateManager) -> CandidateResult:
        gc = sm.config.grid
        cols, rows = gc.resolution
        V = sm.get_value_matrix()
        I = sm.get_info_matrix()

        # Step 1: track-region occupancy mask
        occupied = np.zeros((cols, rows), dtype=bool)
        for tr in sm.get_track_regions():
            b = tr.bbox
            occupied[b.col_start:b.col_end, b.row_start:b.row_end] = True

        # Step 2: high-value cell clustering (connected components)
        threshold = gc.candidate_value_threshold
        high_value_mask = (V >= threshold) & ~occupied
        clusters = self._connected_components(high_value_mask, V, I)

        # Step 3: sort by total value descending
        clusters.sort(key=lambda c: c["total_value"], reverse=True)

        # Step 4: Top-K selection (on clusters, not final candidates)
        available = len(sm.get_available_uavs())
        K = min(available * 2, 10)
        clusters = clusters[:K]

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
                patch_V = V[bbox.col_start:bbox.col_end,
                            bbox.row_start:bbox.row_end]
                patch_I = I[bbox.col_start:bbox.col_end,
                            bbox.row_start:bbox.row_end]
                fitted["total_value"] = float(np.sum(patch_V))
                fitted["avg_info"] = float(np.mean(patch_I))
                candidates.append(fitted)

        # Bug 1 fix: cap final candidates at K (subdivision may inflate count)
        candidates = candidates[:K]

        # Step 6: fragment detection
        fragments = self._detect_fragments(sm, occupied, gc)

        return CandidateResult(
            candidate_regions=candidates,
            fragment_alerts=fragments,
        )

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
                n = max(1, round(math.sqrt(area / gc.search_max_cells)))
                piece_w = (c1 - c0) / n
                piece_h = (r1 - r0) / n
                for i in range(n):
                    for j in range(n):
                        sub_c0 = int(c0 + i * piece_w)
                        sub_c1 = int(c0 + (i + 1) * piece_w)
                        sub_r0 = int(r0 + j * piece_h)
                        sub_r1 = int(r0 + (j + 1) * piece_h)
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
