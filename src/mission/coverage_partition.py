"""Stable rectangular partitions of currently unreserved searchable space."""
from __future__ import annotations

import math

import numpy as np


def partition_search_mask(
    mask: np.ndarray,
    slots: int,
    *,
    min_area: int = 1,
    max_area: int | None = None,
) -> tuple[tuple[int, int, int, int], ...]:
    """Cover free space with about one compact rectangle per search aircraft.

    Every free cell must end up inside exactly one partition: when obstacles
    fragment the mask into more components than there are aircraft, residual
    pieces are merged into their nearest neighbouring partition instead of
    being dropped. A merged partition may enclose a few blocked cells — the
    route planner skips them — and only cells that cannot be merged without
    overlapping another partition fall back to the ordinary candidate pool.
    """
    remaining = np.asarray(mask, dtype=bool).copy()
    if remaining.ndim != 2 or slots < 0:
        raise ValueError('expected a 2D mask and non-negative slot count')
    if min_area < 1 or (max_area is not None and max_area < min_area):
        raise ValueError('invalid partition area bounds')
    if slots == 0:
        return ()
    boxes = []
    while remaining.any():
        heights = np.zeros(remaining.shape[0], dtype=int)
        best = None
        best_key = None
        for row in range(remaining.shape[1]):
            heights = np.where(remaining[:, row], heights + 1, 0)
            stack = []
            for col in range(len(heights) + 1):
                height = int(heights[col]) if col < len(heights) else 0
                start = col
                while stack and stack[-1][1] > height:
                    left, h = stack.pop()
                    box = (left, row + 1 - h, col, row + 1)
                    width = col - left
                    key = (width * h, min(width, h), tuple(-v for v in box))
                    if best_key is None or key > best_key:
                        best, best_key = box, key
                    start = left
                if height and (not stack or stack[-1][1] < height):
                    stack.append((start, height))
        assert best is not None
        boxes.append(best)
        x0, y0, x1, y1 = best
        remaining[x0:x1, y0:y1] = False
    boxes.sort(key=lambda b: (-_area(b), b))
    if max_area is not None:
        bounded = []
        for box in boxes:
            width, height = box[2] - box[0], box[3] - box[1]
            count = max(
                math.ceil(_area(box) / max_area),
                math.ceil(max(width, height) / (2 * min(width, height))),
            )
            bounded.extend(_split(box, count))
        boxes = [box for box in bounded if min_area <= _area(box) <= max_area]
        boxes.sort(key=lambda b: (-_area(b), b))
        if len(boxes) >= slots:
            return _merge_to_slots(boxes, slots)
        while len(boxes) < slots:
            choices = [i for i, box in enumerate(boxes) if _area(box) >= 2 * min_area]
            if not choices:
                break
            index = max(choices, key=lambda i: (_area(boxes[i]), -i))
            parts = _split(boxes[index], 2)
            if len(parts) != 2 or min(_area(part) for part in parts) < min_area:
                break
            boxes[index:index + 1] = parts
        return tuple(sorted(boxes))
    if len(boxes) > slots:
        return _merge_to_slots(boxes, slots)
    counts = [1] * len(boxes)
    while sum(counts) < slots:
        choices = [i for i, b in enumerate(boxes) if counts[i] < _area(b)]
        if not choices:
            break
        index = max(choices, key=lambda i: (_area(boxes[i]) / (counts[i] + 1), -i))
        counts[index] += 1
    result = []
    for box, count in zip(boxes, counts):
        result.extend(_split(box, count))
    return tuple(sorted(result))


def _area(box):
    x0, y0, x1, y1 = box
    return (x1 - x0) * (y1 - y0)


def _intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _union_rect(a, b):
    return (
        min(a[0], b[0]), min(a[1], b[1]),
        max(a[2], b[2]), max(a[3], b[3]),
    )


def _merge_to_slots(boxes, slots):
    """Fold surplus partitions into their nearest neighbour.

    The union rectangle keeps every free cell inside a partition while
    preserving disjointness; a fragment that cannot join any neighbour
    without overlapping a third partition is dropped back to the
    ordinary candidate pool — strictly better than before, where every
    surplus component was dropped unconditionally.
    """
    boxes = [tuple(box) for box in boxes]
    while len(boxes) > slots:
        index = min(
            range(len(boxes)),
            key=lambda i: (_area(boxes[i]), boxes[i]),
        )
        target = None
        target_key = None
        for other, box in enumerate(boxes):
            if other == index:
                continue
            union = _union_rect(boxes[index], box)
            if any(
                _intersects(union, boxes[j])
                for j in range(len(boxes))
                if j not in (index, other)
            ):
                continue
            key = (_area(union) - _area(box) - _area(boxes[index]), -other)
            if target_key is None or key < target_key:
                target, target_key = other, key
        if target is None:
            boxes.pop(index)
            continue
        merged = _union_rect(boxes[index], boxes[target])
        boxes = [
            box for j, box in enumerate(boxes) if j not in (index, target)
        ] + [merged]
    return tuple(sorted(boxes))


def _split(box, count):
    if count == 1:
        return [box]
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    first_count = count // 2
    # A proportional guillotine cut balances workload without moving any
    # boundary belonging to an already executing task.
    options = []
    for axis, length, other in ((0, width, height), (1, height, width)):
        for offset in range(1, length):
            area = offset * other
            if area < first_count or (length - offset) * other < count - first_count:
                continue
            imbalance = abs(area / (width * height) - first_count / count)
            options.append((imbalance, -length, axis, offset))
    if not options:
        return [box]
    _, _, axis, offset = min(options)
    if axis == 0:
        first, second = (x0, y0, x0 + offset, y1), (x0 + offset, y0, x1, y1)
    else:
        first, second = (x0, y0, x1, y0 + offset), (x0, y0 + offset, x1, y1)
    return _split(first, first_count) + _split(second, count - first_count)
