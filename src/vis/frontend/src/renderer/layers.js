import { coordToPixel } from "./geometry";
import { markerColor, PRIORITY_COLORS, UAV_STATUS_COLORS } from "./colors";

const FONT = '"Fira Code", "Microsoft YaHei", monospace';

function gridCenter(col, row, cellSize, ox, oy) {
  return { x: ox + (col + 0.5) * cellSize, y: oy + (row + 0.5) * cellSize };
}

export function drawBackground(ctx, width, height, cellSize, ox, oy) {
  ctx.fillStyle = "#050A0E";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#071821";
  ctx.fillRect(ox, oy, 30 * cellSize, 30 * cellSize);
  ctx.strokeStyle = "rgba(103, 232, 249, .08)";
  ctx.lineWidth = 1;
  for (let ring = 5; ring < 30; ring += 5) {
    ctx.strokeRect(ox + ring * cellSize / 2, oy + ring * cellSize / 2, (30 - ring) * cellSize, (30 - ring) * cellSize);
  }
}

export function drawHeatmap(ctx, info, values, cellSize, ox, oy) {
  for (let col = 0; col < 30; col += 1) {
    for (let row = 0; row < 30; row += 1) {
      const freshness = Number(info?.[col]?.[row] || 0);
      const value = Number(values?.[col]?.[row] || 0);
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      if (freshness > 0.7) ctx.fillStyle = `rgba(45, 212, 191, ${0.12 + freshness * 0.26})`;
      else if (freshness >= 0.2) ctx.fillStyle = `rgba(250, 204, 21, ${0.07 + freshness * 0.18})`;
      else ctx.fillStyle = `rgba(248, 113, 113, ${0.015 + value * 0.085})`;
      ctx.fillRect(x + 0.5, y + 0.5, Math.max(0, cellSize - 1), Math.max(0, cellSize - 1));
    }
  }
}

export function drawGridLines(ctx, cellSize, ox, oy, showGrid) {
  if (!showGrid) return;
  ctx.strokeStyle = "rgba(148, 163, 184, .11)";
  ctx.lineWidth = 0.5;
  for (let index = 0; index <= 30; index += 1) {
    ctx.beginPath();
    ctx.moveTo(ox + index * cellSize, oy);
    ctx.lineTo(ox + index * cellSize, oy + 30 * cellSize);
    ctx.moveTo(ox, oy + index * cellSize);
    ctx.lineTo(ox + 30 * cellSize, oy + index * cellSize);
    ctx.stroke();
  }
}

export function drawObstacles(ctx, obstacles, cellSize, ox, oy, phase) {
  for (const obstacle of obstacles || []) {
    if (obstacle.type === "thunderstorm") {
      const center = gridCenter(obstacle.center[0], obstacle.center[1], cellSize, ox, oy);
      const radius = obstacle.radius * cellSize;
      ctx.fillStyle = "rgba(127, 29, 29, .38)";
      ctx.strokeStyle = `rgba(248, 113, 113, ${0.58 + Math.sin(phase / 24) * 0.12})`;
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(center.x, center.y, radius, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
      ctx.strokeStyle = "#FCA5A5";
      ctx.lineWidth = Math.max(1, cellSize * 0.08);
      ctx.beginPath();
      ctx.moveTo(center.x + radius * 0.08, center.y - radius * 0.52);
      ctx.lineTo(center.x - radius * 0.12, center.y - radius * 0.05);
      ctx.lineTo(center.x + radius * 0.14, center.y - radius * 0.05);
      ctx.lineTo(center.x - radius * 0.08, center.y + radius * 0.5);
      ctx.stroke();
    } else {
      const vertices = obstacle.vertices || [];
      if (!vertices.length) continue;
      ctx.beginPath();
      vertices.forEach(([col, row], index) => {
        const point = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.closePath();
      ctx.fillStyle = "#4A3B2A"; ctx.strokeStyle = "#D6C69B"; ctx.lineWidth = 1.2; ctx.fill(); ctx.stroke();
    }
  }
}

export function drawSearchRegions(ctx, regions, uavs, cellSize, ox, oy) {
  for (const region of regions || []) {
    const [c0, r0, c1, r1] = region.bbox;
    const { x, y } = coordToPixel(c0, r0, cellSize, ox, oy);
    const width = (c1 - c0) * cellSize;
    const height = (r1 - r0) * cellSize;
    const color = PRIORITY_COLORS[region.priority] || PRIORITY_COLORS.medium;
    ctx.fillStyle = `${color}12`; ctx.fillRect(x, y, width, height);
    ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.strokeRect(x, y, width, height);
    const uav = (uavs || []).find((item) => item.id === region.assigned_uav_id);
    const arrow = uav?.sar_look_direction === "left" ? "<" : ">";
    const label = `${region.id}  ${Math.round(region.completion_pct || 0)}%  ${arrow}`;
    ctx.font = `600 ${Math.max(9, Math.min(12, cellSize * 0.42))}px ${FONT}`;
    const labelWidth = ctx.measureText(label).width + 8;
    ctx.fillStyle = "rgba(5, 10, 14, .88)"; ctx.fillRect(x + 2, y + 2, labelWidth, 18);
    ctx.fillStyle = color; ctx.fillText(label, x + 6, y + 15);
  }
}

export function drawTrackRegions(ctx, regions, ships, cellSize, ox, oy) {
  for (const region of regions || []) {
    const [c0, r0, c1, r1] = region.bbox;
    const { x, y } = coordToPixel(c0, r0, cellSize, ox, oy);
    ctx.fillStyle = "rgba(251, 113, 133, .07)"; ctx.fillRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize);
    ctx.strokeStyle = "#FB7185"; ctx.lineWidth = 1.5; ctx.setLineDash([5, 4]);
    ctx.strokeRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize); ctx.setLineDash([]);
    const group = (ships || []).filter((ship) => ship.group_id === region.target_group_id);
    if (group.length) {
      const centerCol = group.reduce((sum, ship) => sum + ship.position[0], 0) / group.length;
      const centerRow = group.reduce((sum, ship) => sum + ship.position[1], 0) / group.length;
      const center = gridCenter(centerCol, centerRow, cellSize, ox, oy);
      ctx.strokeStyle = "rgba(251, 113, 133, .6)"; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.arc(center.x, center.y, 1.8 * cellSize, 0, Math.PI * 2); ctx.stroke(); ctx.setLineDash([]);
    }
  }
}

export function drawPaths(ctx, uavs, cellSize, ox, oy, selectedId) {
  for (const uav of uavs || []) {
    const path = uav.planned_path || [];
    if (path.length < 2 || uav.status === "idle") continue;
    ctx.strokeStyle = uav.id === selectedId ? "rgba(125, 211, 252, .88)" : "rgba(96, 165, 250, .26)";
    ctx.lineWidth = uav.id === selectedId ? 1.8 : 1;
    ctx.setLineDash(uav.id === selectedId ? [] : [3, 4]);
    ctx.beginPath();
    path.forEach((pose, index) => {
      const point = gridCenter(pose[0], pose[1], cellSize, ox, oy);
      if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
    });
    ctx.stroke(); ctx.setLineDash([]);
  }
}

export function drawSensorFootprints(ctx, uavs, cellSize, ox, oy) {
  for (const uav of uavs || []) {
    if (uav.sensor_mode === "sar") {
      ctx.fillStyle = "rgba(34, 211, 238, .22)";
      for (const [col, row] of uav.sar_footprint || []) {
        const point = coordToPixel(col, row, cellSize, ox, oy);
        ctx.fillRect(point.x + 1, point.y + 1, cellSize - 2, cellSize - 2);
      }
    }
    if (uav.sensor_mode === "eo" && uav.eo_fov?.polygon) {
      ctx.beginPath();
      uav.eo_fov.polygon.forEach(([col, row], index) => {
        const point = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.closePath(); ctx.fillStyle = "rgba(250, 204, 21, .24)"; ctx.strokeStyle = "rgba(250, 204, 21, .75)"; ctx.fill(); ctx.stroke();
    }
  }
}

export function drawMarkers(ctx, markers, cellSize, ox, oy, time, phase) {
  for (const marker of markers || []) {
    const age = time - marker.created_time_min;
    if (age > 60) continue;
    const center = gridCenter(marker.position[0], marker.position[1], cellSize, ox, oy);
    const color = markerColor(age);
    ctx.globalAlpha = color.alpha; ctx.fillStyle = color.fill; ctx.beginPath(); ctx.arc(center.x, center.y, 4 + Math.sin(phase / 12) * 1.5, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha = 1;
    ctx.font = `10px ${FONT}`; ctx.fillStyle = "#F8FAFC"; ctx.fillText(marker.id, center.x + 7, center.y - 7);
  }
}

export function drawShips(ctx, ships, cellSize, ox, oy) {
  for (const ship of ships || []) {
    const color = ship.is_detected ? "#FB7185" : "#94A3B8";
    if (ship.trail?.length > 1) {
      ctx.strokeStyle = `${color}55`; ctx.lineWidth = 1; ctx.beginPath();
      ship.trail.forEach(([col, row], index) => {
        const point = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      }); ctx.stroke();
    }
    const center = gridCenter(ship.position[0], ship.position[1], cellSize, ox, oy);
    const size = Math.max(4, cellSize * 0.28);
    ctx.fillStyle = color; ctx.beginPath(); ctx.moveTo(center.x + size, center.y); ctx.lineTo(center.x - size * 0.75, center.y - size * 0.55); ctx.lineTo(center.x - size * 0.4, center.y); ctx.lineTo(center.x - size * 0.75, center.y + size * 0.55); ctx.closePath(); ctx.fill();
    if (ship.is_detected) { ctx.strokeStyle = color; ctx.beginPath(); ctx.arc(center.x, center.y, size + 3, 0, Math.PI * 2); ctx.stroke(); }
  }
}

export function drawUavs(ctx, uavs, basePosition, supportBases, cellSize, ox, oy, selectedId) {
  const bases = [
    ...(basePosition ? [{ position: basePosition, label: "B" }] : []),
    ...((supportBases || []).map((position) => ({ position, label: "F" }))),
  ];
  for (const { position, label } of bases) {
    const base = gridCenter(position[0], position[1], cellSize, ox, oy);
    ctx.strokeStyle = "#CBD5E1"; ctx.lineWidth = 1.3; ctx.strokeRect(base.x - 7, base.y - 7, 14, 14);
    ctx.font = `700 10px ${FONT}`; ctx.fillStyle = "#CBD5E1"; ctx.fillText(label, base.x - 3, base.y + 4);
  }
  for (const uav of uavs || []) {
    const center = gridCenter(uav.position[0], uav.position[1], cellSize, ox, oy);
    const color = UAV_STATUS_COLORS[uav.status] || "#94A3B8";
    const size = Math.max(5, cellSize * (uav.id === selectedId ? 0.42 : 0.32));
    ctx.save(); ctx.translate(center.x, center.y); ctx.rotate((uav.heading_deg || 0) * Math.PI / 180);
    ctx.fillStyle = color; ctx.beginPath();
    ctx.moveTo(size * 1.25, 0); ctx.lineTo(-size * 0.3, -size * 0.22); ctx.lineTo(-size * 0.85, -size); ctx.lineTo(-size * 0.55, -size * 0.12); ctx.lineTo(-size, 0); ctx.lineTo(-size * 0.55, size * 0.12); ctx.lineTo(-size * 0.85, size); ctx.lineTo(-size * 0.3, size * 0.22); ctx.closePath(); ctx.fill(); ctx.restore();
    if (uav.id === selectedId) { ctx.strokeStyle = "#E0F2FE"; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(center.x, center.y, size + 5, 0, Math.PI * 2); ctx.stroke(); }
    ctx.font = `600 9px ${FONT}`; ctx.fillStyle = "#F8FAFC"; ctx.fillText(uav.id.replace("UAV-", "U"), center.x + size + 3, center.y - size - 1);
  }
}

export function drawHoverTooltip(ctx, hover, cellSize, ox, oy, width, height) {
  if (!hover) return;
  const point = coordToPixel(hover.col, hover.row, cellSize, ox, oy);
  ctx.strokeStyle = "#E2E8F0"; ctx.lineWidth = 1.5; ctx.strokeRect(point.x, point.y, cellSize, cellSize);
  const tipWidth = 178; const tipHeight = 58;
  const x = Math.min(width - tipWidth - 8, point.x + cellSize + 8);
  const y = Math.max(8, Math.min(height - tipHeight - 8, point.y));
  ctx.fillStyle = "rgba(8, 15, 20, .96)"; ctx.strokeStyle = "#334155"; ctx.fillRect(x, y, tipWidth, tipHeight); ctx.strokeRect(x, y, tipWidth, tipHeight);
  ctx.font = `11px ${FONT}`; ctx.fillStyle = "#F8FAFC"; ctx.fillText(`CELL ${String(hover.col).padStart(2, "0")} / ${String(hover.row).padStart(2, "0")}`, x + 9, y + 17);
  ctx.fillStyle = "#94A3B8"; ctx.fillText(`信息素 ${hover.I.toFixed(2)}   价值 ${hover.V.toFixed(2)}`, x + 9, y + 34);
  ctx.fillStyle = hover.category === "white" ? "#2DD4BF" : hover.category === "gray" ? "#FACC15" : "#F87171"; ctx.fillText(`${hover.category === "white" ? "白" : hover.category === "gray" ? "灰" : "黑"}态势`, x + 9, y + 50);
}

export function renderFrame(ctx, frame, options = {}) {
  const { cellSize, offsetX, offsetY, showGrid, hoverInfo, selectedUavId, frameCount = 0 } = options;
  const width = ctx.canvas.clientWidth || ctx.canvas.width;
  const height = ctx.canvas.clientHeight || ctx.canvas.height;
  drawBackground(ctx, width, height, cellSize, offsetX, offsetY);
  if (frame) {
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawObstacles(ctx, frame.obstacles, cellSize, offsetX, offsetY, frameCount);
    drawSearchRegions(ctx, frame.search_regions, frame.uavs, cellSize, offsetX, offsetY);
    drawTrackRegions(ctx, frame.track_regions, frame.ships, cellSize, offsetX, offsetY);
    drawPaths(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId);
    drawSensorFootprints(ctx, frame.uavs, cellSize, offsetX, offsetY);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    drawShips(ctx, frame.ships, cellSize, offsetX, offsetY);
    drawUavs(ctx, frame.uavs, frame.base_position, frame.support_base_positions, cellSize, offsetX, offsetY, selectedUavId);
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY, width, height);
}
