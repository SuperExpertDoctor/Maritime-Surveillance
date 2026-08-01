import { coordToPixel } from "./geometry";
import { markerColor, UAV_STATUS_COLORS } from "./colors";

const FONT = '"Fira Code", "Microsoft YaHei", monospace';
const GROUP_COLORS = ["#0891B2", "#D97706", "#65A30D"];

function gridCenter(col, row, cellSize, ox, oy) {
  return { x: ox + (col + 0.5) * cellSize, y: oy + (row + 0.5) * cellSize };
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function normalizeBases(bases, basePosition) {
  if (bases?.length) return bases;
  return basePosition
    ? [{ position: basePosition, number: 1, occupancy: 0, capacity: 3, busy: false }]
    : [];
}

function text(ctx, value, x, y, color = "#0F172A", size = 10, weight = 500) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.fillStyle = color;
  ctx.fillText(value, x, y);
}

function drawMapImage(ctx, image, cellSize, ox, oy) {
  if (!image?.complete || !image.naturalWidth || !image.naturalHeight) return;
  const size = 30 * cellSize;
  ctx.save();
  ctx.beginPath();
  ctx.rect(ox, oy, size, size);
  ctx.clip();
  ctx.globalAlpha = 0.93;
  ctx.drawImage(image, 0, 0, image.naturalWidth, image.naturalHeight, ox, oy, size, size);
  ctx.restore();
}

export function drawBackground(ctx, width, height, cellSize, ox, oy, assets) {
  ctx.fillStyle = "#FFFFFF";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#075AA6";
  ctx.fillRect(ox, oy, 30 * cellSize, 30 * cellSize);
  drawMapImage(ctx, assets?.background, cellSize, ox, oy);
  ctx.strokeStyle = "#0B3857";
  ctx.lineWidth = 2.25;
  ctx.strokeRect(ox - 1, oy - 1, 30 * cellSize + 2, 30 * cellSize + 2);
  ctx.save();
  ctx.fillStyle = "rgba(255, 255, 255, .88)";
  ctx.fillRect(ox + 5, oy + 5, Math.max(158, cellSize * 8.7), Math.max(15, cellSize * 0.7));
  text(ctx, "TASK AREA / 300 x 300 KM", ox + 9, oy + Math.max(16, cellSize * 0.62), "#0B3857", Math.max(7, cellSize * 0.27), 700);
  ctx.restore();

  ctx.save();
  ctx.fillStyle = "#526E7A";
  ctx.font = `600 ${Math.max(7, Math.min(9, cellSize * 0.3))}px ${FONT}`;
  ctx.textAlign = "center";
  for (const index of [0, 5, 10, 15, 20, 25, 29]) {
    const x = ox + (index + 0.5) * cellSize;
    ctx.fillText(String(index).padStart(2, "0"), x, oy - Math.max(4, cellSize * 0.25));
  }
  ctx.textAlign = "right";
  for (const index of [0, 5, 10, 15, 20, 25, 29]) {
    const y = oy + (index + 0.5) * cellSize + 3;
    ctx.fillText(String(index).padStart(2, "0"), ox - Math.max(4, cellSize * 0.25), y);
  }
  ctx.restore();
}

export function drawHeatmap(ctx, info, values, cellSize, ox, oy) {
  for (let col = 0; col < 30; col += 1) {
    for (let row = 0; row < 30; row += 1) {
      const freshness = Number(info?.[col]?.[row] || 0);
      const value = Number(values?.[col]?.[row] || 0);
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      if (freshness > 0.7) ctx.fillStyle = `rgba(13, 148, 136, ${0.12 + freshness * 0.2})`;
      else if (freshness >= 0.2) ctx.fillStyle = `rgba(217, 119, 6, ${0.08 + freshness * 0.14})`;
      else ctx.fillStyle = `rgba(37, 99, 235, ${0.018 + value * 0.045})`;
      ctx.fillRect(x + 0.5, y + 0.5, Math.max(0, cellSize - 1), Math.max(0, cellSize - 1));
    }
  }
}

export function drawTransparencyOverlay(ctx, info, cellSize, ox, oy) {
  for (let col = 0; col < 30; col += 1) {
    for (let row = 0; row < 30; row += 1) {
      const freshness = clamp(Number(info?.[col]?.[row] || 0), 0, 1);
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      ctx.fillStyle = `rgba(15, 23, 42, ${0.1 - freshness * 0.08})`;
      ctx.fillRect(x, y, cellSize, cellSize);
    }
  }
}

export function drawOceanTexture(ctx, cellSize, ox, oy) {
  const size = 30 * cellSize;
  ctx.save();
  ctx.beginPath();
  ctx.rect(ox, oy, size, size);
  ctx.clip();
  ctx.strokeStyle = "rgba(8, 145, 178, .16)";
  ctx.lineWidth = 1;
  for (let row = 2; row < 30; row += 4) {
    ctx.beginPath();
    for (let col = 0; col <= 30; col += 1) {
      const x = ox + col * cellSize;
      const y = oy + (row + Math.sin((col + row) * 0.55) * 0.12) * cellSize;
      if (col === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  ctx.restore();
}

export function drawGridLines(ctx, cellSize, ox, oy, showGrid) {
  if (!showGrid) return;
  for (let index = 0; index <= 30; index += 1) {
    const major = index % 5 === 0;
    ctx.strokeStyle = major ? "rgba(30, 64, 88, .36)" : "rgba(71, 85, 105, .19)";
    ctx.lineWidth = major ? 0.9 : 0.5;
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
      ctx.fillStyle = "rgba(239, 68, 68, .22)";
      ctx.strokeStyle = `rgba(185, 28, 28, ${pulse + 0.25})`;
      ctx.lineWidth = 1.5;
      ctx.fillRect(x, y, size, size);
      ctx.strokeRect(x, y, size, size);
      ctx.save();
      ctx.strokeStyle = "#FFFFFF";
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
      ctx.strokeStyle = "rgba(217, 119, 6, .88)";
      ctx.lineWidth = 1;
      ctx.strokeRect(x - cellSize, y - cellSize, size + 2 * cellSize, size + 2 * cellSize);
      ctx.restore();
      text(ctx, "STORM", x + 3, y + Math.max(10, cellSize * 0.45), "#7F1D1D", Math.max(7, cellSize * 0.28), 700);
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
      ctx.fillStyle = "#A16207";
      ctx.strokeStyle = "#713F12";
      ctx.lineWidth = 1.5;
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

function drawMissionEnvelope(ctx, regions, cellSize, ox, oy) {
  const active = (regions || []).filter((region) => region?.bbox?.length === 4);
  if (!active.length) return;
  const [minCol, minRow, maxCol, maxRow] = active.reduce((bounds, region) => [
    Math.min(bounds[0], region.bbox[0]),
    Math.min(bounds[1], region.bbox[1]),
    Math.max(bounds[2], region.bbox[2]),
    Math.max(bounds[3], region.bbox[3]),
  ], [30, 30, 0, 0]);
  const margin = Math.max(2, cellSize * 0.28);
  const x = ox + minCol * cellSize - margin;
  const y = oy + minRow * cellSize - margin;
  const width = (maxCol - minCol) * cellSize + margin * 2;
  const height = (maxRow - minRow) * cellSize + margin * 2;
  ctx.save();
  ctx.strokeStyle = "rgba(3, 105, 161, .72)";
  ctx.lineWidth = 1.5;
  ctx.setLineDash([Math.max(7, cellSize * 0.48), Math.max(4, cellSize * 0.3)]);
  ctx.strokeRect(x, y, width, height);
  ctx.restore();
}

function taskCells(region) {
  if (Array.isArray(region.cells) && region.cells.length) return region.cells;
  const [c0, r0, c1, r1] = region.bbox;
  const seed = [...String(region.id || "S")].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  const cells = [];
  for (let col = c0; col < c1; col += 1) {
    for (let row = r0; row < r1; row += 1) {
      const edgeDistance = Math.min(col - c0, c1 - 1 - col, row - r0, r1 - 1 - row);
      const carveEdge = edgeDistance === 0 && (col * 13 + row * 7 + seed) % 5 === 0;
      if (!carveEdge) cells.push([col, row]);
    }
  }
  return cells;
}

export function drawSearchRegions(ctx, regions, uavs, cellSize, ox, oy) {
  for (const region of regions || []) {
    const color = "#F59E0B";
    const cells = taskCells(region);
    const assigned = Boolean(region.assigned_uav_id);
    ctx.fillStyle = `${color}${assigned ? "70" : "52"}`;
    for (const [col, row] of cells) {
      const point = coordToPixel(col, row, cellSize, ox, oy);
      ctx.fillRect(point.x + 1, point.y + 1, Math.max(1, cellSize - 2), Math.max(1, cellSize - 2));
    }
    const uav = (uavs || []).find((item) => item.id === region.assigned_uav_id);
    const arrow = uav?.sar_look_direction === "left" ? "<" : ">";
    const fontSize = Math.max(8, Math.min(10, cellSize * 0.34));
    const fullLabel = `${region.id} ${Math.round(region.completion_pct || 0)}% ${arrow}`;
    ctx.font = `700 ${fontSize}px ${FONT}`;
    const labelCell = cells[0];
    if (labelCell) {
      const point = coordToPixel(labelCell[0], labelCell[1], cellSize, ox, oy);
      const label = cellSize >= 14 ? fullLabel : region.id;
      const labelWidth = ctx.measureText(label).width + 8;
      ctx.fillStyle = "rgba(255, 255, 255, .9)";
      ctx.fillRect(point.x + 2, point.y + 2, labelWidth, fontSize + 6);
      text(ctx, label, point.x + 6, point.y + fontSize + 4, color, fontSize, 700);
    }
  }
}

export function drawTrackRegions(ctx, regions, ships, cellSize, ox, oy) {
  for (const region of regions || []) {
    const [c0, r0, c1, r1] = region.bbox;
    const { x, y } = coordToPixel(c0, r0, cellSize, ox, oy);
    ctx.fillStyle = "rgba(190, 18, 60, .06)";
    ctx.fillRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize);
    ctx.strokeStyle = "#BE123C";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([5, 4]);
    ctx.strokeRect(x, y, (c1 - c0) * cellSize, (r1 - r0) * cellSize);
    ctx.setLineDash([]);
    const group = (ships || []).filter((ship) => ship.group_id === region.target_group_id && !ship.departed);
    if (group.length) {
      const centerCol = group.reduce((sum, ship) => sum + ship.position[0], 0) / group.length;
      const centerRow = group.reduce((sum, ship) => sum + ship.position[1], 0) / group.length;
      const center = gridCenter(centerCol, centerRow, cellSize, ox, oy);
      ctx.strokeStyle = "rgba(190, 18, 60, .72)";
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
      ctx.strokeStyle = uav.id === selectedId ? "rgba(29, 78, 216, .92)" : "rgba(37, 99, 235, .38)";
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
      ctx.strokeStyle = "rgba(8, 145, 178, .92)";
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

function drawUavTrails(ctx, uavs, cellSize, ox, oy, selectedId) {
  for (const uav of uavs || []) {
    const trail = uav.trail || [];
    if (trail.length < 2) continue;
    const color = UAV_STATUS_COLORS[uav.status] || "#475569";
    const start = Math.max(1, trail.length - 72);
    ctx.save();
    ctx.lineCap = "round";
    for (let index = start; index < trail.length; index += 1) {
      const previous = gridCenter(trail[index - 1][0], trail[index - 1][1], cellSize, ox, oy);
      const current = gridCenter(trail[index][0], trail[index][1], cellSize, ox, oy);
      const progress = (index - start + 1) / Math.max(1, trail.length - start);
      ctx.strokeStyle = color;
      ctx.globalAlpha = 0.1 + progress * (uav.id === selectedId ? 0.82 : 0.52);
      ctx.lineWidth = (uav.id === selectedId ? 2.25 : 1.45) * (0.62 + progress * 0.38);
      ctx.beginPath();
      ctx.moveTo(previous.x, previous.y);
      ctx.lineTo(current.x, current.y);
      ctx.stroke();
    }
    ctx.restore();
  }
}

export function drawSensorFootprints(ctx, uavs, cellSize, ox, oy) {
  for (const uav of uavs || []) {
    if (uav.sensor_mode === "sar") {
      ctx.fillStyle = "rgba(8, 145, 178, .18)";
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
      ctx.fillStyle = "rgba(202, 138, 4, .18)";
      ctx.strokeStyle = "rgba(161, 98, 7, .82)";
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
    text(ctx, marker.id, center.x + 7, center.y - 7, "#0F172A", 10);
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

function drawSprite(ctx, image, source, center, width, rotation) {
  if (!image?.complete || !image.naturalWidth || !image.naturalHeight) return false;
  const height = width * source.height / source.width;
  ctx.save();
  ctx.translate(center.x, center.y);
  ctx.rotate(rotation);
  ctx.drawImage(
    image,
    source.x,
    source.y,
    source.width,
    source.height,
    -width / 2,
    -height / 2,
    width,
    height,
  );
  ctx.restore();
  return true;
}

function drawShipHull(ctx, ship, center, size, color, assets) {
  const carrier = ship.ship_type === "carrier";
  const model = carrier ? assets?.carrier : assets?.destroyer;
  const source = carrier
    ? { x: 196, y: 244, width: 1072, height: 540 }
    : { x: 48, y: 64, width: 1420, height: 908 };
  const modelWidth = Math.max(carrier ? 20 : 17, cellSizeForShip(size, carrier));
  if (drawSprite(
    ctx,
    model,
    source,
    center,
    modelWidth,
    // The carrier image has its bow to the left; the destroyer image has
    // its bow at the top. Canvas heading zero points to the right.
    (Number(ship.heading_deg) || 0) * Math.PI / 180 + (carrier ? Math.PI : Math.PI / 2),
  )) return;
  ctx.fillStyle = color;
  if (carrier) {
    ctx.fillRect(center.x - size * 1.15, center.y - size * 0.52, size * 2.3, size * 1.04);
    ctx.fillStyle = "#FFFFFF";
    ctx.fillRect(center.x - size * 0.15, center.y - size * 0.42, size * 0.34, size * 0.84);
    ctx.strokeStyle = "#334155";
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

function cellSizeForShip(size, carrier) {
  return size * (carrier ? 4.6 : 4.15);
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
    ctx.fillStyle = "#0369A1";
    ctx.beginPath();
    ctx.moveTo(center.x + size + 2, center.y - 2);
    ctx.lineTo(center.x + size + 9, center.y - 2);
    ctx.lineTo(center.x + size + 6, center.y + 4);
    ctx.closePath();
    ctx.fill();
  }
  ctx.restore();
}

function drawShipRadar(ctx, ship, center, cellSize) {
  if (ship.departed) return;
  const radius = Math.max(cellSize * 1.6, Number(ship.radar_range_cells || 3) * cellSize);
  const heading = (Number(ship.heading_deg) || 0) * Math.PI / 180;
  ctx.save();
  ctx.fillStyle = "rgba(6, 182, 212, .035)";
  ctx.strokeStyle = "rgba(14, 116, 144, .68)";
  ctx.lineWidth = 1;
  ctx.setLineDash([3, 4]);
  ctx.beginPath();
  ctx.arc(center.x, center.y, radius, 0, Math.PI * 2);
  ctx.fill();
  ctx.stroke();
  ctx.setLineDash([]);
  ctx.fillStyle = "rgba(34, 211, 238, .10)";
  ctx.beginPath();
  ctx.moveTo(center.x, center.y);
  ctx.arc(center.x, center.y, radius, heading - Math.PI / 5, heading + Math.PI / 5);
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

export function drawShips(ctx, ships, cellSize, ox, oy, assets) {
  const observedShips = (ships || []).filter((ship) => ship?.is_detected);
  drawGroupRings(ctx, observedShips, cellSize, ox, oy);
  for (const ship of observedShips) {
    const military = ship.is_military === true || ship.discrimination === "military";
    const color = military ? "#E11D48" : ship.is_military === false ? "#0369A1" : ship.is_detected ? "#CA8A04" : "#475569";
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
    drawShipRadar(ctx, ship, center, cellSize);
    ctx.save();
    ctx.globalAlpha = ship.departed ? 0.36 : 1;
    drawShipHull(ctx, ship, center, size, color, assets);
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
    const state = ship.departed
      ? "DEPARTED"
      : ship.is_evasive
        ? "EVADE"
        : ship.ship_type === "carrier" ? "CV" : "DDG";
    const stateColor = ship.departed ? "#64748B" : ship.is_evasive ? "#BE123C" : military ? "#BE123C" : "#334155";
    text(ctx, state, center.x + size + 3, center.y + 3, stateColor, Math.max(7, cellSize * 0.26), 700);
  }
}

function drawBaseStar(ctx, center, outerRadius, innerRadius) {
  ctx.beginPath();
  for (let point = 0; point < 10; point += 1) {
    const radius = point % 2 === 0 ? outerRadius : innerRadius;
    const angle = -Math.PI / 2 + point * Math.PI / 5;
    const x = center.x + Math.cos(angle) * radius;
    const y = center.y + Math.sin(angle) * radius;
    if (point === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }
  ctx.closePath();
}

export function drawBases(ctx, bases, cellSize, ox, oy, phase) {
  for (const [index, base] of (bases || []).entries()) {
    const center = gridCenter(base.position[0], base.position[1], cellSize, ox, oy);
    const color = "#DC2626";
    const outerRadius = Math.max(8, cellSize * 0.48);
    const innerRadius = outerRadius * 0.46;
    ctx.save();
    if (base.busy) {
      ctx.strokeStyle = `rgba(220, 38, 38, ${0.32 + Math.sin(phase / 16) * 0.12})`;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(center.x, center.y, outerRadius + 4, 0, Math.PI * 2);
      ctx.stroke();
    }
    drawBaseStar(ctx, center, outerRadius, innerRadius);
    ctx.fillStyle = color;
    ctx.fill();
    ctx.strokeStyle = "#7F1D1D";
    ctx.lineWidth = 1.3;
    ctx.stroke();
    ctx.restore();
    const baseLabel = `B${base.number || index + 1}`;
    const labelX = center.x + outerRadius + 4;
    const labelY = center.y + 2;
    const fontSize = Math.max(7, cellSize * 0.25);
    text(ctx, baseLabel, labelX, labelY, color, fontSize, 700);
    text(ctx, `${base.occupancy || 0}/${base.capacity || 3}`, labelX, labelY + fontSize + 3, "#7F1D1D", Math.max(6, cellSize * 0.21), 600);
  }
}

export function drawUavs(ctx, uavs, cellSize, ox, oy, selectedId, assets) {
  for (const uav of uavs || []) {
    const center = gridCenter(uav.position[0], uav.position[1], cellSize, ox, oy);
    const color = UAV_STATUS_COLORS[uav.status] || "#94A3B8";
    const size = Math.max(5, cellSize * (uav.id === selectedId ? 0.42 : 0.32));
    ctx.save();
    ctx.globalAlpha = uav.status === "refueling" ? 0.34 : 1;
    const renderedModel = drawSprite(
      ctx,
      assets?.uav,
      { x: 36, y: 180, width: 1444, height: 632 },
      center,
      Math.max(20, cellSize * (uav.id === selectedId ? 1.75 : 1.45)),
      // The Rainbow UAV cutout has its nose at the top of the source image.
      (Number(uav.heading_deg) || 0) * Math.PI / 180 + Math.PI / 2,
    );
    if (!renderedModel) {
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
    }
    ctx.restore();
    if (uav.id === selectedId) {
      ctx.strokeStyle = "#0F172A";
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
    text(ctx, uav.id.replace("UAV-", "U"), center.x + size + 3, center.y - size - 1, "#0F172A", 9, 700);
    if (uav.avoidance_level > 0) {
      const level = Number(uav.avoidance_level);
      const levelColor = level >= 3 ? "#F87171" : level === 2 ? "#FBBF24" : "#67E8F9";
      text(ctx, `L${level}`, center.x + size + 3, center.y + size + 8, levelColor, Math.max(7, cellSize * 0.25), 700);
    }
  }
}

export function drawTransparencyLegend(ctx, cellSize, ox, oy) {
  const canvasWidth = ctx.canvas.clientWidth || ctx.canvas.width;
  const canvasHeight = ctx.canvas.clientHeight || ctx.canvas.height;
  const mapRight = ox + 30 * cellSize;
  const sideSpace = canvasWidth - mapRight;
  const horizontalInset = 10;
  const usableWidth = Math.floor(sideSpace - horizontalInset * 2);
  // The legend belongs only in the right-side whitespace, never over the map.
  if (usableWidth < 128) return;
  const width = clamp(usableWidth, 128, 160);
  const availableHeight = Math.max(0, canvasHeight - oy - 20);
  const itemHeight = clamp(Math.floor((availableHeight - 32) / 8), 13, 17);
  const swatchSize = clamp(Math.floor(itemHeight * 0.58), 7, 10);
  const titleSize = clamp(Math.floor(width * 0.055), 7, 8);
  const labelSize = clamp(Math.floor(width * 0.045), 6, 7);
  const height = 24 + 8 * itemHeight + 8;
  if (height > availableHeight) return;
  const x = mapRight + horizontalInset;
  const y = Math.max(8, Math.min(oy + 10, canvasHeight - height - 8));
  ctx.fillStyle = "rgba(255, 255, 255, .94)";
  ctx.strokeStyle = "rgba(71, 85, 105, .72)";
  ctx.lineWidth = 1;
  ctx.fillRect(x, y, width, height);
  ctx.strokeRect(x, y, width, height);
  text(ctx, "MAP LEGEND", x + 8, y + 13, "#334155", titleSize, 700);
  const swatches = [
    { color: "#D97706", label: "TASK CELLS" },
    { color: "#0F766E", label: "FRESH SAR" },
    { color: "#DC2626", label: "NO-FLY STORM" },
    { color: "#0E7490", label: "SHIP RADAR" },
    { color: "#2563EB", label: "UAV TRANSIT" },
    { color: "#BE123C", label: "TARGET CONTACT" },
    { color: "#DC2626", label: "BASE STAR" },
    { color: "#334155", label: "TASK BORDER" },
  ];
  swatches.forEach((swatch, index) => {
    const itemX = x + 9;
    const itemY = y + 22 + index * itemHeight;
    ctx.fillStyle = swatch.color;
    ctx.fillRect(itemX, itemY, swatchSize, swatchSize);
    ctx.strokeStyle = "#64748B";
    ctx.strokeRect(itemX, itemY, swatchSize, swatchSize);
    text(ctx, swatch.label, itemX + swatchSize + 4, itemY + swatchSize - 1, "#475569", labelSize, 600);
  });
}

export function drawHoverTooltip(ctx, hover, cellSize, ox, oy, width, height) {
  if (!hover) return;
  const point = coordToPixel(hover.col, hover.row, cellSize, ox, oy);
  ctx.strokeStyle = "#0F172A";
  ctx.lineWidth = 1.5;
  ctx.strokeRect(point.x, point.y, cellSize, cellSize);
  const tipWidth = 188;
  const tipHeight = 70;
  const x = Math.min(width - tipWidth - 8, point.x + cellSize + 8);
  const y = Math.max(8, Math.min(height - tipHeight - 8, point.y));
  ctx.fillStyle = "rgba(255, 255, 255, .97)";
  ctx.strokeStyle = "#64748B";
  ctx.fillRect(x, y, tipWidth, tipHeight);
  ctx.strokeRect(x, y, tipWidth, tipHeight);
  text(ctx, `CELL ${String(hover.col).padStart(2, "0")} / ${String(hover.row).padStart(2, "0")}`, x + 9, y + 17, "#0F172A", 11);
  text(ctx, `INFO ${hover.I.toFixed(2)}   VALUE ${hover.V.toFixed(2)}`, x + 9, y + 35, "#475569", 10);
  text(ctx, `SHADE ${(0.1 - hover.I * 0.08).toFixed(2)}`, x + 9, y + 52, "#475569", 10);
  const state = hover.category === "white" ? "FRESH" : hover.category === "gray" ? "AGING" : "UNSCANNED";
  const stateColor = hover.category === "white" ? "#2DD4BF" : hover.category === "gray" ? "#FACC15" : "#F87171";
  text(ctx, state, x + 112, y + 52, stateColor, 10, 700);
}

export function renderFrame(ctx, frame, options = {}) {
  const {
    cellSize,
    offsetX,
    offsetY,
    showGrid,
    hoverInfo,
    selectedUavId,
    frameCount = 0,
    assets,
  } = options;
  const width = ctx.canvas.clientWidth || ctx.canvas.width;
  const height = ctx.canvas.clientHeight || ctx.canvas.height;
  const bases = normalizeBases(frame?.bases, frame?.base_position);
  drawBackground(ctx, width, height, cellSize, offsetX, offsetY, assets);
  if (frame) {
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawTransparencyOverlay(ctx, frame.info_matrix, cellSize, offsetX, offsetY);
    drawOceanTexture(ctx, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawObstacles(ctx, frame.obstacles, cellSize, offsetX, offsetY, frameCount);
    drawMissionEnvelope(ctx, [...(frame.search_regions || []), ...(frame.track_regions || [])], cellSize, offsetX, offsetY);
    drawSearchRegions(ctx, frame.search_regions, frame.uavs, cellSize, offsetX, offsetY);
    drawTrackRegions(ctx, frame.track_regions, frame.ships, cellSize, offsetX, offsetY);
    drawUavTrails(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId);
    drawPaths(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId);
    drawSensorFootprints(ctx, frame.uavs, cellSize, offsetX, offsetY);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    drawShips(ctx, frame.ships, cellSize, offsetX, offsetY, assets);
    drawUavs(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId, assets);
    drawBases(ctx, bases, cellSize, offsetX, offsetY, frameCount);
    drawTransparencyLegend(ctx, cellSize, offsetX, offsetY);
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY, width, height);
}
