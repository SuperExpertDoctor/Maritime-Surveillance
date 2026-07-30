/**
 * 网格坐标 ↔ Canvas 像素坐标映射。
 *
 * cellSize  = floor(min(canvasW, canvasH) / 32)
 * offsetX   = (canvasW - 30 * cellSize) / 2
 * offsetY   = (canvasH - 30 * cellSize) / 2
 */

export function computeLayout(canvasW, canvasH) {
  const cellSize = Math.floor(Math.min(canvasW, canvasH) / 32);
  const offsetX = (canvasW - 30 * cellSize) / 2;
  const offsetY = (canvasH - 30 * cellSize) / 2;
  return { cellSize, offsetX, offsetY };
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
