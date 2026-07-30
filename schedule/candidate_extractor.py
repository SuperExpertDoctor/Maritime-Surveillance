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

        # Step 4: Top-K selection
        available = len(sm.get_available_uavs())
        K = min(available * 2, 10)
        K = max(K, 5)
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
                fitted["total_value"] = cluster["total_value"]
                fitted["avg_info"] = cluster["avg_info"]
                candidates.append(fitted)

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

        Handles three cases:
        1. Tiny area  -- expand the bbox.
        2. High aspect ratio -- split along the longer axis.
        3. Over-sized bbox -- subdivide into a regular grid.

        Returns a list of candidate dicts, each with ``bbox`` and ``cell_count``.
        """
        if not cells:
            return []

        cs = [cell.col for cell in cells]
        rs = [cell.row for cell in cells]
        c_min, c_max = min(cs), max(cs) + 1
        r_min, r_max = min(rs), max(rs) + 1

        w, h = c_max - c_min, r_max - r_min
        area = w * h

        # --- tiny-area expansion ---
        if area < gc.fragment_threshold_cells:
            expand = 2
            c_min = max(0, c_min - expand)
            r_min = max(0, r_min - expand)
            c_max = min(cols, c_max + expand)
            r_max = min(rows, r_max + expand)
            w, h = c_max - c_min, r_max - r_min

        # --- aspect-ratio split ---
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect > gc.aspect_ratio_max:
            if w > h:
                mid = (c_min + c_max) // 2
                return [
                    {
                        "bbox": BBox(c_min, r_min, mid, r_max),
                        "cell_count": (mid - c_min) * (r_max - r_min),
                    },
                    {
                        "bbox": BBox(mid, r_min, c_max, r_max),
                        "cell_count": (c_max - mid) * (r_max - r_min),
                    },
                ]
            else:
                mid = (r_min + r_max) // 2
                return [
                    {
                        "bbox": BBox(c_min, r_min, c_max, mid),
                        "cell_count": (c_max - c_min) * (mid - r_min),
                    },
                    {
                        "bbox": BBox(c_min, mid, c_max, r_max),
                        "cell_count": (c_max - c_min) * (r_max - mid),
                    },
                ]

        # --- size-based subdivision ---
        area = (c_max - c_min) * (r_max - r_min)
        if area > gc.search_max_cells:
            n = max(1, round(math.sqrt(area / gc.search_max_cells)))
            piece_w = (c_max - c_min) / n
            piece_h = (r_max - r_min) / n
            results = []
            for i in range(n):
                for j in range(n):
                    sub_c0 = int(c_min + i * piece_w)
                    sub_c1 = int(c_min + (i + 1) * piece_w)
                    sub_r0 = int(r_min + j * piece_h)
                    sub_r1 = int(r_min + (j + 1) * piece_h)
                    if sub_c1 > sub_c0 and sub_r1 > sub_r0:
                        results.append(
                            {
                                "bbox": BBox(sub_c0, sub_r0, sub_c1, sub_r1),
                                "cell_count": (sub_c1 - sub_c0)
                                * (sub_r1 - sub_r0),
                            }
                        )
            return results

        # --- normal case: single bbox ---
        return [
            {
                "bbox": BBox(c_min, r_min, c_max, r_max),
                "cell_count": area,
            }
        ]

    # ------------------------------------------------------------------
    # Fragment detection
    # ------------------------------------------------------------------

    def _detect_fragments(
        self, sm: StateManager, occupied: np.ndarray, gc
    ) -> list[dict]:
        """Detect fragments left after track regions carve into previous
        search regions."""
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
                        if area < gc.fragment_threshold_cells:
                            fragments.append(
                                {
                                    "bbox": rem_bbox,
                                    "area": area,
                                    "reason": f"区域{prev.id}被{track.id}挖除后产生{area}格碎片",
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
