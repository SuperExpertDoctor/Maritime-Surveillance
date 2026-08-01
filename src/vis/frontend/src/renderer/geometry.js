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
  const offsetX = chartX + taskInset;
  const offsetY = chartY + (chartHeight - taskSize) / 2;
  const mapBounds = { x: chartX, y: chartY, width: chartWidth, height: chartHeight };
  const legendBounds = legendWidth
    ? { x: chartX + chartWidth + 10, y: chartY, width: legendWidth, height: chartHeight }
    : null;

  return {
    cellSize: taskSize / GRID_CELLS,
    offsetX,
    offsetY,
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

export function pixelToCoord(px, py, cellSize, offsetX, offsetY) {
  const col = Math.floor((px - offsetX) / cellSize);
  const row = Math.floor((py - offsetY) / cellSize);
  if (col < 0 || col >= 30 || row < 0 || row >= 30) return null;
  return { col, row };
}
