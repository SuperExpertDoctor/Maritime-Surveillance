"""Stable rectangular partitions of currently unreserved searchable space."""
from __future__ import annotations

import numpy as np


def partition_search_mask(mask: np.ndarray, slots: int) -> tuple[tuple[int, int, int, int], ...]:
    """Cover free space with about one compact rectangle per search aircraft.

    Obstacles can require more rectangles than aircraft. In that case retain
    the largest components for this dispatch; smaller residuals stay in the
    ordinary candidate pool for later sorties. Never include a blocked cell.
    """
    remaining = np.asarray(mask, dtype=bool).copy()
    if remaining.ndim != 2 or slots < 0:
        raise ValueError('expected a 2D mask and non-negative slot count')
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
    boxes = boxes[:slots]
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
