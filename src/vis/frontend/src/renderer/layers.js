import { coordToPixel } from "./geometry";
import { markerColor, PRIORITY_COLORS, UAV_STATUS_COLORS } from "./colors";

const FONT = '"Fira Code", "Microsoft YaHei", monospace';
const BASE_COLORS = ["#3B82F6", "#10B981", "#F59E0B"];
const GROUP_COLORS = ["#67E8F9", "#FBBF24", "#A3E635"];

function gridCenter(col, row, cellSize, ox, oy) {
  return { x: ox + (col + 0.5) * cellSize, y: oy + (row + 0.5) * cellSize };
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function normalizeBases(bases, basePosition, supportBases) {
  if (bases?.length) return bases;
  return [
    ...(basePosition ? [{ position: basePosition, number: 1, occupancy: 0, capacity: 3, busy: false }] : []),
    ...((supportBases || []).map((position, index) => ({
      position, number: index + 2, occupancy: 0, capacity: 3, busy: false,
    }))),
  ];
}

function text(ctx, value, x, y, color = "#F8FAFC", size = 10, weight = 500) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.fillStyle = color;
  ctx.fillText(value, x, y);
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

export function drawTransparencyOverlay(ctx, info, cellSize, ox, oy) {
  for (let col = 0; col < 30; col += 1) {
    for (let row = 0; row < 30; row += 1) {
      const freshness = clamp(Number(info?.[col]?.[row] || 0), 0, 1);
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      ctx.fillStyle = `rgba(0, 0, 0, ${1 - freshness * 0.9})`;
      ctx.fillRect(x, y, cellSize, cellSize);
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
      const size = Math.max(1, obstacle.size || obstacle.radius * 2 || 1) * cellSize;
      const center = coordToPixel(obstacle.center[0], obstacle.center[1], cellSize, ox, oy);
      const x = center.x - size / 2;
      const y = center.y - size / 2;
      const pulse = 0.56 + Math.sin(phase / 24) * 0.12;
      ctx.fillStyle = "rgba(239, 68, 68, .4)";
      ctx.strokeStyle = `rgba(248, 113, 113, ${pulse})`;
      ctx.lineWidth = 1.5;
      ctx.fillRect(x, y, size, size);
      ctx.strokeRect(x, y, size, size);
      ctx.save();
      ctx.strokeStyle = "#FDE2E2";
      ctx.lineWidth = Math.max(1, cellSize * 0.08);
      ctx.beginPath();
      ctx.moveTo(center.x + size * 0.07, center.y - size * 0.28);
      ctx.lineTo(center.x - size * 0.11, center.y - size * 0.02);
      ctx.lineTo(center.x + size * 0.06, center.y - size * 0.02);
      ctx.lineTo(center.x - size * 0.09, center.y + size * 0.29);
      ctx.stroke();
      ctx.restore();
      ctx.save();
      ctx.setLineDash([3, 3]);
      ctx.strokeStyle = "rgba(250, 204, 21, .7)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x - cellSize, y - cellSize, size + 2 * cellSize, size + 2 * cellSize);
      ctx.restore();
      text(ctx, "STORM", x + 3, y + Math.max(10, cellSize * 0.45), "#FECACA", Math.max(7, cellSize * 0.28), 700);
    } else {
      const vertices = obstacle.vertices || [];
      if (!vertices.length) continue;
      ctx.beginPath();
      vertices.forEach(([col, row], index) => {
        const point = coordToPixel(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.closePath();
      ctx.save();
      ctx.globalAlpha = 0.7;
      ctx.fillStyle = "#92400E";
      ctx.strokeStyle = "#FFFFFF";
      ctx.lineWidth = 1.2;
      ctx.fill();
      ctx.stroke();
      ctx.restore();
      const [col, row] = vertices.reduce(
        (sum, vertex) => [sum[0] + vertex[0] / vertices.length, sum[1] + vertex[1] / vertices.length],
        [0, 0],
      );
      const label = obstacle.label || obstacle.id || "ISLAND";
      const point = coordToPixel(col, row, cellSize, ox, oy);
      text(ctx, label, point.x + 3, point.y + 3 + Math.max(7, cellSize * 0.25), "#FFFFFF", Math.max(7, cellSize * 0.25), 700);
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
    ctx.fillStyle = `${color}12`;
    ctx.fillRect(x, y, width, height);
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(x, y, width, height);
    const uav = (uavs || []).find((item) => item.id === region.assigned_uav_id);
    const arrow = uav?.sar_look_direction === "left" ? "<" : ">";
    const label = `${region.id}  ${Math.round(region.completion_pct || 0)}%  ${arrow}`;
    ctx.font = `600 ${Math.max(9, Math.min(12, cellSize * 0.42))}px ${FONT}`;
    const labelWidth = ctx.measureText(label).width + 8;
    ctx.fillStyle = "rgba(5, 10, 14, .88)";
    ctx.fillRect(x + 2, y + 2, labelWidth, 18);
    text(ctx, label, x + 6, y + 15, color, Math.max(9, Math.min(12, cellSize * 0.42)), 600);
  }
}

export function drawTrackRegions(ctx, regions, ships, cellSize, ox, oy) {
  for (const region of regions || []) {
    const [c0, r0, c1, r1] = region.bbox;
    const { x, y } = coordToPixel(c0, r0, cellSize, ox, oy);
    ctx.fillStyle = "rgba(251, 113, 133, .07)";
    ctx.fillRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize);
    ctx.strokeStyle = "#FB7185";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize);
    ctx.setLineDash([]);
    const group = (ships || []).filter((ship) => ship.group_id === region.target_group_id && !ship.departed);
    if (group.length) {
      const centerCol = group.reduce((sum, ship) => sum + ship.position[0], 0) / group.length;
      const centerRow = group.reduce((sum, ship) => sum + ship.position[1], 0) / group.length;
      const center = gridCenter(centerCol, centerRow, cellSize, ox, oy);
      ctx.strokeStyle = "rgba(251, 113, 133, .6)";
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.arc(center.x, center.y, 1.8 * cellSize, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
}

export function drawPaths(ctx, uavs, cellSize, ox, oy, selectedId) {
  for (const uav of uavs || []) {
    const path = uav.planned_path || [];
    if (path.length >= 2 && uav.status !== "idle") {
      ctx.strokeStyle = uav.id === selectedId ? "rgba(125, 211, 252, .88)" : "rgba(96, 165, 250, .26)";
      ctx.lineWidth = uav.id === selectedId ? 1.8 : 1;
      ctx.setLineDash(uav.id === selectedId ? [] : [3, 4]);
      ctx.beginPath();
      path.forEach((pose, index) => {
        const point = gridCenter(pose[0], pose[1], cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.setLineDash([]);
    }
    const avoidancePath = uav.avoidance_path || [];
    if (avoidancePath.length >= 2) {
      ctx.save();
      ctx.strokeStyle = "rgba(34, 211, 238, .9)";
      ctx.lineWidth = 1.6;
      ctx.setLineDash([5, 3]);
      ctx.beginPath();
      avoidancePath.forEach((pose, index) => {
        const point = gridCenter(pose[0], pose[1], cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.restore();
    }
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
      ctx.closePath();
      ctx.fillStyle = "rgba(250, 204, 21, .24)";
      ctx.strokeStyle = "rgba(250, 204, 21, .75)";
      ctx.fill();
      ctx.stroke();
    }
  }
}

export function drawMarkers(ctx, markers, cellSize, ox, oy, time, phase) {
  for (const marker of markers || []) {
    const age = time - marker.created_time_min;
    if (age > 60) continue;
    const center = gridCenter(marker.position[0], marker.position[1], cellSize, ox, oy);
    const color = markerColor(age);
    ctx.save();
    ctx.globalAlpha = color.alpha;
    ctx.fillStyle = color.fill;
    ctx.beginPath();
    ctx.arc(center.x, center.y, 4 + Math.sin(phase / 12) * 1.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
    text(ctx, marker.id, center.x + 7, center.y - 7, "#F8FAFC", 10);
  }
}

function drawGroupRings(ctx, ships, cellSize, ox, oy) {
  const groups = new Map();
  for (const ship of ships || []) {
    if (ship.departed) continue;
    const members = groups.get(ship.group_id) || [];
    members.push(ship);
    groups.set(ship.group_id, members);
  }
  for (const [groupId, members] of groups) {
    if (!members.length) continue;
    const col = members.reduce((sum, ship) => sum + ship.position[0], 0) / members.length;
    const row = members.reduce((sum, ship) => sum + ship.position[1], 0) / members.length;
    const center = gridCenter(col, row, cellSize, ox, oy);
    ctx.save();
    ctx.setLineDash([2, 3]);
    const groupIndex = Math.max(0, Number(String(groupId).replace(/\D/g, "")) - 1) % GROUP_COLORS.length;
    const color = GROUP_COLORS[groupIndex];
    ctx.strokeStyle = `${color}90`;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.arc(center.x, center.y, Math.max(9, cellSize * 1.22), 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
    text(ctx, groupId, center.x + 5, center.y - Math.max(9, cellSize * 1.28), color, Math.max(7, cellSize * 0.25), 700);
  }
}

function drawShipHull(ctx, ship, center, size, color) {
  ctx.fillStyle = color;
  if (ship.ship_type === "carrier") {
    ctx.fillRect(center.x - size * 1.15, center.y - size * 0.52, size * 2.3, size * 1.04);
    ctx.fillStyle = "#0F172A";
    ctx.fillRect(center.x - size * 0.15, center.y - size * 0.42, size * 0.34, size * 0.84);
    ctx.strokeStyle = "#E2E8F0";
    ctx.lineWidth = 0.8;
    ctx.strokeRect(center.x - size * 1.15, center.y - size * 0.52, size * 2.3, size * 1.04);
    return;
  }
  ctx.beginPath();
  ctx.moveTo(center.x + size, center.y);
  ctx.lineTo(center.x - size * 0.7, center.y - size * 0.58);
  ctx.lineTo(center.x - size * 0.34, center.y);
  ctx.lineTo(center.x - size * 0.7, center.y + size * 0.58);
  ctx.closePath();
  ctx.fill();
}

function drawClassificationSymbol(ctx, ship, center, size, military) {
  if (ship.departed) return;
  ctx.save();
  ctx.lineWidth = 1;
  if (military) {
    const x = center.x + size + 5;
    const y = center.y - size - 1;
    ctx.strokeStyle = "#F87171";
    ctx.beginPath();
    ctx.arc(x, y - 2, 1.3, 0, Math.PI * 2);
    ctx.moveTo(x, y - 0.5);
    ctx.lineTo(x, y + 5);
    ctx.moveTo(x - 4, y + 2);
    ctx.lineTo(x + 4, y + 2);
    ctx.moveTo(x - 4, y + 2);
    ctx.quadraticCurveTo(x - 2, y + 6, x, y + 6);
    ctx.quadraticCurveTo(x + 2, y + 6, x + 4, y + 2);
    ctx.stroke();
  } else if (ship.is_military === false) {
    ctx.fillStyle = "#E0F2FE";
    ctx.beginPath();
    ctx.moveTo(center.x + size + 2, center.y - 2);
    ctx.lineTo(center.x + size + 9, center.y - 2);
    ctx.lineTo(center.x + size + 6, center.y + 4);
    ctx.closePath();
    ctx.fill();
  }
  ctx.restore();
}

export function drawShips(ctx, ships, cellSize, ox, oy) {
  drawGroupRings(ctx, ships, cellSize, ox, oy);
  for (const ship of ships || []) {
    const military = ship.is_military === true || ship.discrimination === "military";
    const color = military ? "#FB7185" : ship.is_military === false ? "#E0F2FE" : ship.is_detected ? "#FBBF24" : "#94A3B8";
    const size = Math.max(4, cellSize * (ship.ship_type === "carrier" ? 0.38 : 0.28));
    if (ship.trail?.length > 1) {
      ctx.save();
      ctx.globalAlpha = ship.departed ? 0.22 : 1;
      ctx.strokeStyle = `${color}55`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ship.trail.forEach(([col, row], index) => {
        const point = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.restore();
    }
    const rawCenter = gridCenter(ship.position[0], ship.position[1], cellSize, ox, oy);
    const center = {
      x: clamp(rawCenter.x, ox + size + 2, ox + 30 * cellSize - size - 2),
      y: clamp(rawCenter.y, oy + size + 2, oy + 30 * cellSize - size - 2),
    };
    ctx.save();
    ctx.globalAlpha = ship.departed ? 0.36 : 1;
    drawShipHull(ctx, ship, center, size, color);
    if (ship.is_detected && !ship.departed) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.arc(center.x, center.y, size + 3, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.restore();
    drawClassificationSymbol(ctx, ship, center, size, military);
    if (ship.ais?.reported_position && !ship.departed) {
      const report = gridCenter(ship.ais.reported_position[0], ship.ais.reported_position[1], cellSize, ox, oy);
      ctx.save();
      ctx.strokeStyle = military ? "rgba(251, 113, 133, .72)" : "rgba(148, 163, 184, .52)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(center.x, center.y);
      ctx.lineTo(report.x, report.y);
      ctx.stroke();
      ctx.restore();
    }
    const state = ship.departed ? "DEPARTED" : military ? "M" : ship.is_military === false ? "C" : "?";
    const stateColor = ship.departed ? "#94A3B8" : military ? "#F87171" : "#CBD5E1";
    text(ctx, state, center.x + size + 3, center.y + 3, stateColor, Math.max(7, cellSize * 0.26), 700);
  }
}

export function drawBases(ctx, bases, cellSize, ox, oy, phase) {
  for (const [index, base] of (bases || []).entries()) {
    const center = gridCenter(base.position[0], base.position[1], cellSize, ox, oy);
    const color = BASE_COLORS[index % BASE_COLORS.length];
    const size = Math.max(12, cellSize * 0.66);
    ctx.save();
    ctx.fillStyle = "#091116";
    ctx.strokeStyle = base.busy ? `rgba(248, 113, 113, ${0.65 + Math.sin(phase / 16) * 0.2})` : color;
    ctx.lineWidth = base.busy ? 2 : 1.3;
    ctx.fillRect(center.x - size / 2, center.y - size / 2, size, size);
    ctx.strokeRect(center.x - size / 2, center.y - size / 2, size, size);
    ctx.restore();
    text(ctx, `B${base.number || index + 1}`, center.x - size / 2 + 2, center.y - 1, color, Math.max(7, cellSize * 0.25), 700);
    text(ctx, `${base.occupancy || 0}/${base.capacity || 3}`, center.x - size / 2 + 2, center.y + Math.max(8, cellSize * 0.3), "#CBD5E1", Math.max(6, cellSize * 0.21), 600);
  }
}

export function drawUavs(ctx, uavs, cellSize, ox, oy, selectedId) {
  for (const uav of uavs || []) {
    const center = gridCenter(uav.position[0], uav.position[1], cellSize, ox, oy);
    const color = UAV_STATUS_COLORS[uav.status] || "#94A3B8";
    const size = Math.max(5, cellSize * (uav.id === selectedId ? 0.42 : 0.32));
    ctx.save();
    ctx.globalAlpha = uav.status === "refueling" ? 0.34 : 1;
    ctx.translate(center.x, center.y);
    ctx.rotate((uav.heading_deg || 0) * Math.PI / 180);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(size * 1.25, 0);
    ctx.lineTo(-size * 0.3, -size * 0.22);
    ctx.lineTo(-size * 0.85, -size);
    ctx.lineTo(-size * 0.55, -size * 0.12);
    ctx.lineTo(-size, 0);
    ctx.lineTo(-size * 0.55, size * 0.12);
    ctx.lineTo(-size * 0.85, size);
    ctx.lineTo(-size * 0.3, size * 0.22);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
    if (uav.id === selectedId) {
      ctx.strokeStyle = "#E0F2FE";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(center.x, center.y, size + 5, 0, Math.PI * 2);
      ctx.stroke();
    }
    if (uav.status === "refueling") {
      const progress = clamp(1 - Number(uav.time_to_available_min || 0) / 15, 0, 1);
      ctx.strokeStyle = "#38BDF8";
      ctx.lineWidth = 1.4;
      ctx.beginPath();
      ctx.arc(center.x, center.y, size + 3, -Math.PI / 2, -Math.PI / 2 + progress * Math.PI * 2);
      ctx.stroke();
    }
    text(ctx, uav.id.replace("UAV-", "U"), center.x + size + 3, center.y - size - 1, "#F8FAFC", 9, 600);
    if (uav.avoidance_level > 0) {
      const level = Number(uav.avoidance_level);
      const levelColor = level >= 3 ? "#F87171" : level === 2 ? "#FBBF24" : "#67E8F9";
      text(ctx, `L${level}`, center.x + size + 3, center.y + size + 8, levelColor, Math.max(7, cellSize * 0.25), 700);
    }
  }
}

export function drawTransparencyLegend(ctx, cellSize, ox, oy) {
  const width = Math.max(174, cellSize * 7.4);
  const height = 48;
  const canvasWidth = ctx.canvas.clientWidth || ctx.canvas.width;
  const canvasHeight = ctx.canvas.clientHeight || ctx.canvas.height;
  const x = Math.max(8, canvasWidth - width - 12);
  const y = Math.max(8, canvasHeight - height - 38);
  ctx.fillStyle = "rgba(5, 10, 14, .86)";
  ctx.strokeStyle = "rgba(100, 116, 139, .7)";
  ctx.lineWidth = 1;
  ctx.fillRect(x, y, width, height);
  ctx.strokeRect(x, y, width, height);
  text(ctx, "SCAN TRANSPARENCY", x + 7, y + 13, "#CBD5E1", 8, 700);
  const swatches = [
    { color: "#050505", label: "BLACK 0.0" },
    { color: "#737373", label: "GRAY 0.5" },
    { color: "#F8FAFC", label: "WHITE 1.0" },
  ];
  swatches.forEach((swatch, index) => {
    const itemX = x + 7 + index * ((width - 14) / 3);
    ctx.fillStyle = swatch.color;
    ctx.fillRect(itemX, y + 21, 10, 10);
    ctx.strokeStyle = "#64748B";
    ctx.strokeRect(itemX, y + 21, 10, 10);
    text(ctx, swatch.label, itemX + 13, y + 30, "#94A3B8", 7, 600);
  });
}

export function drawHoverTooltip(ctx, hover, cellSize, ox, oy, width, height) {
  if (!hover) return;
  const point = coordToPixel(hover.col, hover.row, cellSize, ox, oy);
  ctx.strokeStyle = "#E2E8F0";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(point.x, point.y, cellSize, cellSize);
  const tipWidth = 188;
  const tipHeight = 70;
  const x = Math.min(width - tipWidth - 8, point.x + cellSize + 8);
  const y = Math.max(8, Math.min(height - tipHeight - 8, point.y));
  ctx.fillStyle = "rgba(8, 15, 20, .96)";
  ctx.strokeStyle = "#334155";
  ctx.fillRect(x, y, tipWidth, tipHeight);
  ctx.strokeRect(x, y, tipWidth, tipHeight);
  text(ctx, `CELL ${String(hover.col).padStart(2, "0")} / ${String(hover.row).padStart(2, "0")}`, x + 9, y + 17, "#F8FAFC", 11);
  text(ctx, `INFO ${hover.I.toFixed(2)}   VALUE ${hover.V.toFixed(2)}`, x + 9, y + 35, "#94A3B8", 10);
  text(ctx, `OPACITY ${(1 - hover.I * 0.9).toFixed(2)}`, x + 9, y + 52, "#94A3B8", 10);
  const state = hover.category === "white" ? "FRESH" : hover.category === "gray" ? "AGING" : "UNSCANNED";
  const stateColor = hover.category === "white" ? "#2DD4BF" : hover.category === "gray" ? "#FACC15" : "#F87171";
  text(ctx, state, x + 112, y + 52, stateColor, 10, 700);
}

export function renderFrame(ctx, frame, options = {}) {
  const { cellSize, offsetX, offsetY, showGrid, hoverInfo, selectedUavId, frameCount = 0 } = options;
  const width = ctx.canvas.clientWidth || ctx.canvas.width;
  const height = ctx.canvas.clientHeight || ctx.canvas.height;
  drawBackground(ctx, width, height, cellSize, offsetX, offsetY);
  if (frame) {
    const bases = normalizeBases(frame.bases, frame.base_position, frame.support_base_positions);
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawTransparencyOverlay(ctx, frame.info_matrix, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawObstacles(ctx, frame.obstacles, cellSize, offsetX, offsetY, frameCount);
    drawSearchRegions(ctx, frame.search_regions, frame.uavs, cellSize, offsetX, offsetY);
    drawTrackRegions(ctx, frame.track_regions, frame.ships, cellSize, offsetX, offsetY);
    drawPaths(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId);
    drawSensorFootprints(ctx, frame.uavs, cellSize, offsetX, offsetY);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    drawBases(ctx, bases, cellSize, offsetX, offsetY, frameCount);
    drawShips(ctx, frame.ships, cellSize, offsetX, offsetY);
    drawUavs(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId);
    drawTransparencyLegend(ctx, cellSize, offsetX, offsetY);
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY, width, height);
}
