from dataclasses import dataclass, field
from src.schedule.datatypes import BBox, Region
from src.schedule.config_loader import AppConfig
from src.utils.coverage_planner import CoveragePlanner


_coverage_planner = CoveragePlanner(sample_step=0.25)


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[str] = field(default_factory=list)


def validate(llm_output: dict, config: AppConfig,
             track_regions: list[Region],
             prev_search_regions: list[Region],
             obstacle_mask=None,
             allow_empty: bool = False) -> ValidationResult:
    """校验 LLM 输出的搜索区域划分方案。"""
    errors = []
    gc = config.grid

    regions_data = llm_output.get("search_regions", [])
    if not regions_data:
        if allow_empty:
            return ValidationResult(is_valid=True, errors=[])
        errors.append("search_regions is empty")
        return ValidationResult(is_valid=False, errors=errors)

    bboxes = []
    for i, r in enumerate(regions_data):
        bbox = r.get("bbox", [])
        if len(bbox) != 4:
            errors.append(f"Region {i}: bbox must have 4 elements, got {len(bbox)}")
            continue

        c0, r0, c1, r1 = bbox
        cols, rows = gc.resolution

        # 1. 坐标范围
        if not (0 <= c0 < c1 <= cols and 0 <= r0 < r1 <= rows):
            errors.append(f"Region {i} bbox {bbox}: out of bounds [0,{cols}]x[0,{rows}]")
            continue

        b = BBox(c0, r0, c1, r1)
        w, h = c1 - c0, r1 - r0
        area = w * h

        # 2. 面积约束
        if area < gc.search_min_cells:
            errors.append(f"Region {i} area={area}: below minimum {gc.search_min_cells}")
        if area > gc.search_max_cells:
            errors.append(f"Region {i} area={area}: above maximum {gc.search_max_cells}")

        # 3. 长宽比
        aspect = max(w, h) / max(min(w, h), 1)
        if aspect > gc.aspect_ratio_max:
            errors.append(f"Region {i} aspect={aspect:.2f}: exceeds max {gc.aspect_ratio_max}")

        bboxes.append((i, b))
        if obstacle_mask is not None and obstacle_mask[
            c0:c1,
            r0:r1,
        ].any():
            errors.append(f"Region {i} overlaps a no-fly obstacle")
        elif obstacle_mask is not None:
            swath_width = config.sensor.sar.swath_km / config.grid.cell_size_km
            if not _coverage_planner.is_region_feasible(
                b,
                swath_width,
                1.0,
                obstacle_mask,
            ):
                errors.append(
                    f"Region {i} has no collision-free SAR scan pattern"
                )

    # 4. 不重叠（搜索区之间）
    for i in range(len(bboxes)):
        for j in range(i + 1, len(bboxes)):
            a = bboxes[i][1]
            b = bboxes[j][1]
            if _bboxes_overlap(a, b):
                errors.append(f"Region {bboxes[i][0]} and {bboxes[j][0]} overlap")

    # 5. 不与跟踪区重叠
    for ti, track in enumerate(track_regions):
        for ri, r_bbox in bboxes:
            if _bboxes_overlap(r_bbox, track.bbox):
                errors.append(f"Search region {ri} overlaps track region {track.id}")

    # 6. 数量约束
    total_regions = len(regions_data) + len(track_regions)
    if total_regions > 10:
        errors.append(f"Total regions {total_regions} exceeds UAV max 10")

    # Display IDs in heavy-model output describe additions, not mutations of
    # previous regions. TaskAllocator normalizes continuity by geometric IoU
    # after validation, so reusing an old display ID is not a validation error.

    return ValidationResult(is_valid=len(errors) == 0, errors=errors)


def _bboxes_overlap(a: BBox, b: BBox) -> bool:
    if a.col_end <= b.col_start or b.col_end <= a.col_start:
        return False
    if a.row_end <= b.row_start or b.row_end <= a.row_start:
        return False
    return True


def _compute_iou(a: BBox, b: BBox) -> float:
    if not _bboxes_overlap(a, b):
        return 0.0
    inter_w = min(a.col_end, b.col_end) - max(a.col_start, b.col_start)
    inter_h = min(a.row_end, b.row_end) - max(a.row_start, b.row_start)
    inter_area = inter_w * inter_h
    area_a = (a.col_end - a.col_start) * (a.row_end - a.row_start)
    area_b = (b.col_end - b.col_start) * (b.row_end - b.row_start)
    union_area = area_a + area_b - inter_area
    return inter_area / union_area if union_area > 0 else 0.0
