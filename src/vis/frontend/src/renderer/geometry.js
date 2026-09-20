/**
 * 网格坐标 ↔ Canvas 像素坐标映射。
 *
 * cellSize  = floor(min(canvasW, canvasH) / 32)
 * offsetX   = (canvasW - 30 * cellSize) / 2
 * offsetY   = (canvasH - 30 * cellSize) / 2
 */

const BACKGROUND_ASPECT_RATIO = 1672 / 938;
const GRID_CELLS = 30;

export function computeLayout(canvasW, canvasH) {
  const inset = canvasW <= 620 ? 12 : 16;
  const legendWidth = canvasW >= 900 ? 168 : 0;
  const availableWidth = Math.max(1, canvasW - inset * 2 - legendWidth - (legendWidth ? 10 : 0));
  const availableHeight = Math.max(1, canvasH - inset * 2);
  const chartWidth = Math.min(availableWidth, availableHeight * BACKGROUND_ASPECT_RATIO);
  const chartHeight = chartWidth / BACKGROUND_ASPECT_RATIO;
  const groupWidth = chartWidth + (legendWidth ? legendWidth + 10 : 0);
  const chartX = Math.max(inset, (canvasW - groupWidth) / 2);
  const chartY = inset;
  const taskInset = Math.max(5, Math.min(12, Math.round(chartHeight * 0.025)));
  const taskSize = Math.max(
    GRID_CELLS,
    Math.floor(Math.min(chartHeight - taskInset * 2, chartWidth - taskInset * 2)),
  );
  // Keep the square surveillance sector in open water, away from the
  // mainland occupying the left edge of the 16:9 chart.
  const offsetX = chartX + chartWidth - taskSize - taskInset;
  const offsetY = chartY + (chartHeight - taskSize) / 2;
  const mapBounds = { x: chartX, y: chartY, width: chartWidth, height: chartHeight };
  const legendBounds = legendWidth
    ? { x: chartX + chartWidth + 10, y: chartY, width: legendWidth, height: chartHeight }
    : null;

  return {
    cellSize: taskSize / GRID_CELLS,
    offsetX,
    offsetY,
    taskBounds: { x: offsetX, y: offsetY, width: taskSize, height: taskSize },
    mapBounds,
    legendBounds,
  };
}

export function coordToPixel(col, row, cellSize, offsetX, offsetY) {
  return {
    x: offsetX + col * cellSize,
    y: offsetY + row * cellSize,
  };
}

export function pixelToCoord(px, py, cellSize, offsetX, offsetY, cols = GRID_CELLS, rows = GRID_CELLS) {
  const col = Math.floor((px - offsetX) / cellSize);
  const row = Math.floor((py - offsetY) / cellSize);
  if (col < 0 || col >= cols || row < 0 || row >= rows) return null;
  return { col, row };
}

/** Convert a CSS-pixel drag into a half-open, clamped grid rectangle. */
export function dragToBBox(start, end, layout, cols = GRID_CELLS, rows = GRID_CELLS) {
  if (!start || !end || !layout || Math.abs(start.x - end.x) < 3 || Math.abs(start.y - end.y) < 3) {
    return null;
  }
  const clamp = (value, maximum) => Math.max(0, Math.min(maximum, value));
  const x0 = (Math.min(start.x, end.x) - layout.offsetX) / layout.cellSize;
  const y0 = (Math.min(start.y, end.y) - layout.offsetY) / layout.cellSize;
  const x1 = (Math.max(start.x, end.x) - layout.offsetX) / layout.cellSize;
  const y1 = (Math.max(start.y, end.y) - layout.offsetY) / layout.cellSize;
  const bbox = [
    clamp(Math.floor(x0), cols),
    clamp(Math.floor(y0), rows),
    clamp(Math.ceil(x1), cols),
    clamp(Math.ceil(y1), rows),
  ];
  return bbox[0] < bbox[2] && bbox[1] < bbox[3] ? bbox : null;
}
