const PRIORITY = {
  selected: 4,
  uav: 3,
  classified: 2,
  contact: 1,
  scenario: 0,
};

function priorityValue(priority) {
  if (typeof priority === "number" && Number.isFinite(priority)) return priority;
  return PRIORITY[String(priority || "contact")] ?? 1;
}

function finiteDimension(value, fallback) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : fallback;
}

function anchorPoint(anchor) {
  if (Array.isArray(anchor)) {
    return { x: Number(anchor[0]), y: Number(anchor[1]) };
  }
  return { x: Number(anchor?.x), y: Number(anchor?.y) };
}

function boundsRect(bounds) {
  const x = Number(bounds?.x ?? bounds?.left ?? 0);
  const y = Number(bounds?.y ?? bounds?.top ?? 0);
  const width = Number(bounds?.width ?? (Number(bounds?.right) - x));
  const height = Number(bounds?.height ?? (Number(bounds?.bottom) - y));
  return {
    x: Number.isFinite(x) ? x : 0,
    y: Number.isFinite(y) ? y : 0,
    width: Number.isFinite(width) && width >= 0 ? width : 0,
    height: Number.isFinite(height) && height >= 0 ? height : 0,
  };
}

function withinBounds(rect, bounds) {
  return rect.x >= bounds.x
    && rect.y >= bounds.y
    && rect.x + rect.width <= bounds.x + bounds.width
    && rect.y + rect.height <= bounds.y + bounds.height;
}

function overlaps(left, right) {
  return !(left.x + left.width <= right.x
    || right.x + right.width <= left.x
    || left.y + left.height <= right.y
    || right.y + right.height <= left.y);
}

function candidates(anchor, width, height) {
  return [
    { x: anchor.x + 8, y: anchor.y - height - 8 },
    { x: anchor.x + 8, y: anchor.y + 8 },
    { x: anchor.x - width - 8, y: anchor.y - height - 8 },
    { x: anchor.x - width - 8, y: anchor.y + 8 },
    { x: anchor.x - width / 2, y: anchor.y - height - 12 },
    { x: anchor.x - width / 2, y: anchor.y + 12 },
    { x: anchor.x + 12, y: anchor.y - height / 2 },
    { x: anchor.x - width - 12, y: anchor.y - height / 2 },
  ];
}

/** Place labels in screen space with stable priority and fixed offsets. */
export function layoutLabels(labels = [], bounds = {}) {
  const mapBounds = boundsRect(bounds);
  const placed = [];
  const ordered = labels.map((label, index) => ({ label, index })).sort(
    (left, right) => priorityValue(right.label.priority) - priorityValue(left.label.priority)
      || left.index - right.index
      || String(left.label.id).localeCompare(String(right.label.id)),
  );
  const output = Array(labels.length);

  for (const { label, index } of ordered) {
    const anchor = anchorPoint(label.anchor);
    const width = finiteDimension(label.width, 1);
    const height = finiteDimension(label.height, 1);
    const choices = candidates(anchor, width, height);
    const choice = choices
      .map((point) => ({ ...point, width, height }))
      .find((rect) => withinBounds(rect, mapBounds)
        && !placed.some((other) => overlaps(rect, other)));
    const fallback = choices[0]
      ? { ...choices[0], width, height }
      : { x: mapBounds.x, y: mapBounds.y, width, height };
    const result = choice || fallback;
    output[index] = {
      id: label.id,
      x: result.x,
      y: result.y,
      width,
      height,
      anchor: label.anchor,
      hidden: !choice,
    };
    if (choice) placed.push(choice);
  }
  return output;
}

