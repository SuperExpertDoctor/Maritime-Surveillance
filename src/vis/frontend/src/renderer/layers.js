import { coordToPixel } from "./geometry";
import { markerColor, UAV_STATUS_COLORS } from "./colors";
import { uavDisplayState } from "./displayState";
import { layoutLabels } from "./labelLayout";

const FONT = '"Fira Code", "Microsoft YaHei", monospace';
const GROUP_COLORS = ["#0891B2", "#D97706", "#65A30D"];

function gridCenter(col, row, cellSize, ox, oy) {
  return { x: ox + (col + 0.5) * cellSize, y: oy + (row + 0.5) * cellSize };
}

function clamp(value, minimum, maximum) {
  return Math.max(minimum, Math.min(maximum, value));
}

function normalizeBases(bases, basePosition) {
  const source = bases?.length
    ? bases
    : basePosition
      ? [{ position: basePosition, number: 1, occupancy: 0, capacity: 3, busy: false }]
      : [];
  return source;
}

function buildBaseCenters(bases, mapBounds) {
  return (bases || []).map((base, index) => {
    const row = Number(base.position?.[1] ?? 10 + index * 10);
    return {
      x: mapBounds.x + mapBounds.width * (0.052 + Math.min(index, 1) * 0.038),
      y: mapBounds.y + mapBounds.height * clamp((row + 0.5) / 30, 0.14, 0.86),
    };
  });
}

function baseCenterForUav(uav, baseCenters) {
  if (!baseCenters?.length) return null;
  const number = Number(String(uav.id || "").match(/\d+/)?.[0] || 1);
  return baseCenters[(number - 1) % baseCenters.length];
}

function groundedUavCenter(uav, baseCenters, cellSize) {
  const baseCenter = baseCenterForUav(uav, baseCenters);
  if (!baseCenter) return null;
  const number = Number(String(uav.id || "").match(/\d+/)?.[0] || 1);
  const slot = Math.floor((number - 1) / baseCenters.length) % 5;
  const angle = -Math.PI / 2 + slot * Math.PI * 0.4;
  const radius = Math.max(3, cellSize * 0.34);
  return {
    x: baseCenter.x + Math.cos(angle) * radius,
    y: baseCenter.y + Math.sin(angle) * radius,
  };
}

export function resolveUavDisplayCenter(uav, cellSize, ox, oy, baseCenters) {
  const taskCenter = gridCenter(uav.position[0], uav.position[1], cellSize, ox, oy);
  const baseCenter = groundedUavCenter(uav, baseCenters, cellSize);
  if (["idle", "refueling"].includes(uav.status) || !baseCenter) {
    return baseCenter || taskCenter;
  }

  const progress = Number(uav.transit_progress);
  if (uav.status === "transit" && Number.isFinite(progress)) {
    const departure = clamp(progress, 0, 1);
    return {
      x: baseCenter.x + (taskCenter.x - baseCenter.x) * departure,
      y: baseCenter.y + (taskCenter.y - baseCenter.y) * departure,
    };
  }

  // Historical replay files do not carry transit_progress.  Keep their
  // first assigned frame at the visible base instead of drawing it at the
  // task-grid coordinate that happens to represent the same base location.
  const home = uav.home_base_grid;
  const atHome = home?.length >= 2
    && Math.hypot(Number(uav.position[0]) - Number(home[0]), Number(uav.position[1]) - Number(home[1])) < 1e-4;
  return atHome ? baseCenter : taskCenter;
}

function text(ctx, value, x, y, color = "#0F172A", size = 10, weight = 500) {
  ctx.font = `${weight} ${size}px ${FONT}`;
  ctx.fillStyle = color;
  ctx.fillText(value, x, y);
}

function drawMapImage(ctx, image, bounds) {
  if (!image?.complete || !image.naturalWidth || !image.naturalHeight) return;
  ctx.save();
  ctx.beginPath();
  ctx.rect(bounds.x, bounds.y, bounds.width, bounds.height);
  ctx.clip();
  ctx.globalAlpha = 0.96;
  ctx.drawImage(image, 0, 0, image.naturalWidth, image.naturalHeight, bounds.x, bounds.y, bounds.width, bounds.height);
  ctx.restore();
}

export function drawBackground(ctx, width, height, cellSize, ox, oy, mapBounds, assets) {
  ctx.fillStyle = "#FFFFFF";
  ctx.fillRect(0, 0, width, height);
  ctx.fillStyle = "#075AA6";
  ctx.fillRect(mapBounds.x, mapBounds.y, mapBounds.width, mapBounds.height);
  drawMapImage(ctx, assets?.background, mapBounds);
  ctx.strokeStyle = "#0B3857";
  ctx.lineWidth = 2.25;
  ctx.strokeRect(mapBounds.x - 1, mapBounds.y - 1, mapBounds.width + 2, mapBounds.height + 2);
  ctx.save();
  ctx.strokeStyle = "rgba(15, 23, 42, .94)";
  ctx.lineWidth = 1.7;
  ctx.setLineDash([6, 4]);
  ctx.strokeRect(ox - 1, oy - 1, 30 * cellSize + 2, 30 * cellSize + 2);
  ctx.restore();
  ctx.save();
  ctx.fillStyle = "rgba(255, 255, 255, .88)";
  ctx.fillRect(ox + 5, oy + 5, Math.min(30 * cellSize - 10, Math.max(158, cellSize * 8.7)), Math.max(15, cellSize * 0.7));
  text(ctx, "SIM GRID / 300 x 300 KM", ox + 9, oy + Math.max(16, cellSize * 0.62), "#0B3857", Math.max(7, cellSize * 0.27), 700);
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

/** Render only the authoritative search mask; legacy frames have no inferred mask. */
export function drawSearchDomain(ctx, domain, cellSize, ox, oy) {
  if (!domain) return;
  ctx.save();
  for (const [col, row] of domain.excluded_cells || []) {
    const x = ox + col * cellSize;
    const y = oy + row * cellSize;
    ctx.fillStyle = "rgba(100, 116, 139, .75)";
    ctx.fillRect(x, y, cellSize, cellSize);
    ctx.strokeStyle = "rgba(226, 232, 240, .8)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, y + cellSize);
    ctx.lineTo(x + cellSize, y);
    ctx.stroke();
  }
  const cells = domain.searchable_cells || [];
  const included = new Set(cells.map(([col, row]) => `${col},${row}`));
  ctx.strokeStyle = "#0F766E";
  ctx.lineWidth = 2;
  ctx.setLineDash([]);
  ctx.beginPath();
  for (const [col, row] of cells) {
    const edges = [
      [col - 1, row, col, row, col, row + 1],
      [col + 1, row, col + 1, row, col + 1, row + 1],
      [col, row - 1, col, row, col + 1, row],
      [col, row + 1, col, row + 1, col + 1, row + 1],
    ];
    for (const [neighborCol, neighborRow, x0, y0, x1, y1] of edges) {
      if (included.has(`${neighborCol},${neighborRow}`)) continue;
      ctx.moveTo(ox + x0 * cellSize, oy + y0 * cellSize);
      ctx.lineTo(ox + x1 * cellSize, oy + y1 * cellSize);
    }
  }
  ctx.stroke();
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
  const labels = [];
  const labelSources = new Map();
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
      const id = `search:${region.id}`;
      labels.push({
        id,
        anchor: { x: point.x + cellSize / 2, y: point.y + cellSize / 2 },
        width: labelWidth,
        height: fontSize + 6,
        priority: assigned ? 0 : 2,
      });
      labelSources.set(id, { color, fontSize, text: label });
    }
  }
  const placed = layoutLabels(labels, {
    x: ox,
    y: oy,
    width: 30 * cellSize,
    height: 30 * cellSize,
  });
  for (const label of placed) {
    if (label.hidden) continue;
    const source = labelSources.get(label.id);
    if (!source) continue;
    ctx.save();
    ctx.strokeStyle = `${source.color}99`;
    ctx.lineWidth = 0.8;
    drawLabelLeader(ctx, label.anchor, label);
    ctx.stroke();
    ctx.fillStyle = "rgba(255, 255, 255, .9)";
    ctx.fillRect(label.x, label.y, label.width, label.height);
    text(ctx, source.text, label.x + 4, label.y + source.fontSize + 2, source.color, source.fontSize, 700);
    ctx.restore();
  }
  return placed;
}

export function drawIntents(ctx, intents, statuses, cellSize, ox, oy) {
  const statusById = new Map((statuses || []).map((status) => [status.intent_id, status]));
  for (const intent of intents || []) {
    if (!Array.isArray(intent.bbox) || intent.bbox.length !== 4) continue;
    const [c0, r0, c1, r1] = intent.bbox;
    if (!(c1 > c0 && r1 > r0)) continue;
    const status = statusById.get(intent.intent_id);
    const lifecycle = intent.lifecycle || "active";
    const color = lifecycle === "expired"
      ? "#64748B"
      : lifecycle === "cancelled" ? "#94A3B8" : "#7C3AED";
    const point = coordToPixel(c0, r0, cellSize, ox, oy);
    const width = (c1 - c0) * cellSize;
    const height = (r1 - r0) * cellSize;
    ctx.save();
    ctx.fillStyle = lifecycle === "active" ? "rgba(124, 58, 237, .10)" : "rgba(100, 116, 139, .07)";
    ctx.fillRect(point.x, point.y, width, height);
    ctx.strokeStyle = color;
    ctx.lineWidth = lifecycle === "active" ? 1.8 : 1;
    ctx.setLineDash(lifecycle === "active" ? [5, 3] : [2, 4]);
    ctx.strokeRect(point.x + 1, point.y + 1, Math.max(0, width - 2), Math.max(0, height - 2));
    ctx.restore();
    const coverage = status ? Math.round((status.coverage_ratio || 0) * 100) : null;
    const label = `${intent.intent_id}${coverage == null ? "" : ` ${coverage}%`}`;
    text(ctx, label, point.x + 4, point.y + Math.max(11, cellSize * 0.48), color, Math.max(7, cellSize * 0.27), 700);
  }
}

export function drawTrackRegions(ctx, regions, contacts, cellSize, ox, oy) {
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
    const group = (contacts || []).filter((contact) => {
      const id = contact.contact_id || contact.group_id;
      return id === region.target_group_id && contact.state !== "departed";
    });
    if (group.length) {
      const centerCol = group.reduce((sum, contact) => sum + (contact.estimated_position || contact.position)[0], 0) / group.length;
      const centerRow = group.reduce((sum, contact) => sum + (contact.estimated_position || contact.position)[1], 0) / group.length;
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

function contactColor(contact) {
  if (contact?.state === "lost" || contact?.state === "departed") return "#64748B";
  if (contact?.vessel_class === "type_ii") return "#BE123C";
  if (contact?.vessel_class === "type_i") return "#0F766E";
  return "#B45309";
}

function drawContactVessel(ctx, center, size, color, heading, selected) {
  ctx.save();
  ctx.translate(center.x, center.y);
  if (heading != null) ctx.rotate(heading);
  ctx.fillStyle = color;
  ctx.strokeStyle = "#FFFFFF";
  ctx.lineWidth = selected ? 1.2 : 0.8;
  ctx.beginPath();
  if (heading == null) {
    ctx.moveTo(0, -size * 1.08);
    ctx.lineTo(size * 0.72, 0);
    ctx.lineTo(0, size * 1.08);
    ctx.lineTo(-size * 0.72, 0);
  } else {
    ctx.moveTo(size * 1.25, 0);
    ctx.lineTo(-size * 0.62, -size * 0.72);
    ctx.lineTo(-size * 0.36, 0);
    ctx.lineTo(-size * 0.62, size * 0.72);
  }
  ctx.closePath();
  ctx.fill();
  ctx.stroke();
  ctx.restore();
}

export function drawContacts(ctx, contacts, cellSize, ox, oy, selectedId, phase = 0) {
  for (const contact of contacts || []) {
    const position = contact.estimated_position;
    if (!Array.isArray(position) || position.length < 2) continue;
    const color = contactColor(contact);
    const center = gridCenter(Number(position[0]), Number(position[1]), cellSize, ox, oy);
    const radius = Math.max(4, cellSize * (contact.contact_id === selectedId ? 0.34 : 0.25));
    const samples = (contact.samples || []).filter((sample) => Array.isArray(sample.position));
    if (samples.length > 1) {
      ctx.save();
      ctx.strokeStyle = `${color}66`;
      ctx.lineWidth = 1;
      ctx.beginPath();
      samples.forEach((sample, index) => {
        const point = gridCenter(sample.position[0], sample.position[1], cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.restore();
    }
    const aisSample = [...samples].reverse().find((sample) => sample.source === "ais");
    if (aisSample && contact.state !== "departed") {
      const aisPoint = gridCenter(aisSample.position[0], aisSample.position[1], cellSize, ox, oy);
      ctx.save();
      ctx.strokeStyle = "rgba(100, 116, 139, .62)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(center.x, center.y);
      ctx.lineTo(aisPoint.x, aisPoint.y);
      ctx.stroke();
      ctx.restore();
    }
    ctx.save();
    ctx.globalAlpha = contact.state === "lost" ? 0.5 : 1;
    ctx.fillStyle = `${color}22`;
    ctx.strokeStyle = color;
    ctx.lineWidth = contact.contact_id === selectedId ? 2 : 1.2;
    ctx.beginPath();
    ctx.arc(center.x, center.y, radius + 3, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    if (contact.contact_id === selectedId) {
      ctx.globalAlpha = 0.78;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.arc(center.x, center.y, radius + 7 + Math.sin(phase / 10) * 1.5, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = color;
    const velocity = contact.estimated_velocity;
    const speed = Array.isArray(velocity) && velocity.length >= 2
      ? Math.hypot(Number(velocity[0]), Number(velocity[1]))
      : 0;
    const heading = speed > 1e-3
      ? Math.atan2(Number(velocity[1]), Number(velocity[0]))
      : null;
    drawContactVessel(
      ctx,
      center,
      radius,
      color,
      Number.isFinite(heading) ? heading : null,
      contact.contact_id === selectedId,
    );
    ctx.restore();
  }
}

export function drawPassiveEvidence(ctx, evidence, cellSize, ox, oy, phase = 0, detectionRange = 10) {
  for (const item of evidence || []) {
    if (item.kind === "passive_bearing") {
      const origin = item.observer_position || item.origin;
      if (!Array.isArray(origin) || origin.length < 2) continue;
      const center = gridCenter(Number(origin[0]), Number(origin[1]), cellSize, ox, oy);
      // The public evidence contract is bearing-only. Use the configured
      // receiver envelope as a rendering bound instead of a measured range.
      const configuredRange = Number(detectionRange);
      const radius = (Number.isFinite(configuredRange) && configuredRange > 0 ? configuredRange : 10) * cellSize;
      const bearing = Number(item.bearing_deg || 0) * Math.PI / 180;
      const spread = Math.max(0.04, Number(item.bearing_std_deg || 3) * 2 * Math.PI / 180);
      ctx.save();
      ctx.fillStyle = "rgba(14, 116, 144, .09)";
      ctx.strokeStyle = "rgba(14, 116, 144, .64)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 4]);
      ctx.beginPath();
      ctx.moveTo(center.x, center.y);
      ctx.arc(center.x, center.y, radius, bearing - spread, bearing + spread);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = "#0E7490";
      ctx.beginPath();
      ctx.arc(center.x, center.y, Math.max(2, cellSize * 0.1), 0, Math.PI * 2);
      ctx.fill();
      ctx.restore();
    } else if (item.kind === "passive_position") {
      const position = item.position;
      if (!Array.isArray(position) || position.length < 2) continue;
      const center = gridCenter(Number(position[0]), Number(position[1]), cellSize, ox, oy);
      const size = Math.max(4, cellSize * 0.25);
      const pulse = 1 + 0.12 * Math.sin(phase / 8);
      ctx.save();
      ctx.translate(center.x, center.y);
      ctx.rotate(Math.PI / 4);
      ctx.fillStyle = "rgba(5, 150, 105, .18)";
      ctx.strokeStyle = "#047857";
      ctx.lineWidth = 1.5;
      ctx.fillRect(-size * pulse, -size * pulse, size * 2 * pulse, size * 2 * pulse);
      ctx.strokeRect(-size * pulse, -size * pulse, size * 2 * pulse, size * 2 * pulse);
      ctx.restore();
    }
  }
}

export function drawScenarioVessels(ctx, vessels, cellSize, ox, oy, selectedId) {
  for (const vessel of vessels || []) {
    if (!Array.isArray(vessel.position) || vessel.position.length < 2) continue;
    const center = gridCenter(Number(vessel.position[0]), Number(vessel.position[1]), cellSize, ox, oy);
    const selected = vessel.scenario_entity_id === selectedId;
    const color = vessel.vessel_class === "type_ii" ? "#B45309" : "#0369A1";
    const radius = Math.max(4, cellSize * (selected ? 0.34 : 0.27));
    ctx.save();
    ctx.fillStyle = `${color}20`;
    ctx.strokeStyle = color;
    ctx.lineWidth = selected ? 2 : 1.2;
    ctx.beginPath();
    ctx.arc(center.x, center.y, radius + (selected ? 5 : 3), 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(center.x, center.y - radius);
    ctx.lineTo(center.x + radius, center.y + radius);
    ctx.lineTo(center.x - radius, center.y + radius);
    ctx.closePath();
    ctx.fill();
    ctx.restore();
  }
}

export function drawPaths(ctx, uavs, cellSize, ox, oy, selectedId, baseCenters) {
  for (const uav of uavs || []) {
    const mission = uav.mission_route || uav.planned_path || [];
    const planned = uav.planned_path || [];
    const baseGrid = uav.home_base_grid;
    const isSelected = uav.id === selectedId;

    // Departing UAV: the visual base star lives on the mainland (map
    // coords) while planned_path[0] lives on the ocean grid.  Draw a
    // departure leg bridging the two coordinate spaces so the route
    // reads as originating from the visible red-star marker.
    const departBase = baseCenters?.length
      ? baseCenters[(Number(String(uav.id || "").match(/\d+/)?.[0] || 1) - 1) % baseCenters.length]
      : null;
    const isDeparting = (
      departBase
      && planned.length >= 1
      && uav.status !== "idle"
      && uav.status !== "refueling"
      && uav.status !== "returning"
      && uav.status !== "holding"
    );
    // Avoid drawing the departure leg when the UAV has already
    // travelled well beyond the first few waypoints (the connector
    // would then bisect the screen).
    let showDepartLeg = false;
    let departGridPt = null;
    if (isDeparting && planned.length > 0) {
      const firstPose = planned[0];
      departGridPt = gridCenter(firstPose[0], firstPose[1], cellSize, ox, oy);
      // The departure leg is meaningful when the UAV's home base
      // grid is close to planned_path[0] (i.e. a fresh sortie, not
      // a mid-mission replan).
      if (baseGrid && baseGrid.length >= 2) {
        const homePt = gridCenter(baseGrid[0], baseGrid[1], cellSize, ox, oy);
        showDepartLeg = Math.hypot(homePt.x - departGridPt.x, homePt.y - departGridPt.y) < cellSize * 4;
      }
    }

    // ── Full mission route (dashed, dim) ──────────────────────────
    if (mission.length >= 2 && uav.status !== "idle" && uav.status !== "refueling") {
      ctx.save();
      ctx.strokeStyle = isSelected ? "rgba(37, 99, 235, .48)" : "rgba(100, 116, 139, .28)";
      ctx.lineWidth = isSelected ? 1.4 : 0.9;
      ctx.setLineDash([4, 5]);
      ctx.beginPath();
      // Prepend departure leg from the mainland base star
      if (showDepartLeg && departGridPt) {
        ctx.moveTo(departBase.x, departBase.y);
        ctx.lineTo(departGridPt.x, departGridPt.y);
      }
      mission.forEach((pose, index) => {
        const pt = gridCenter(pose[0], pose[1], cellSize, ox, oy);
        if (index === 0 && showDepartLeg) {
          // Already connected from base star — skip duplicate moveTo
        } else if (index === 0) {
          ctx.moveTo(pt.x, pt.y);
        } else {
          ctx.lineTo(pt.x, pt.y);
        }
      });
      // Returning / holding: draw the final leg back to base
      if (baseGrid && baseGrid.length >= 2
          && (uav.status === "returning" || uav.status === "holding")) {
        const basePt = gridCenter(baseGrid[0], baseGrid[1], cellSize, ox, oy);
        ctx.lineTo(basePt.x, basePt.y);
      }
      ctx.stroke();
      ctx.restore();
    }

    // ── Remaining planned path (solid, prominent) ─────────────────
    if (planned.length >= 2 && uav.status !== "idle" && uav.status !== "refueling") {
      ctx.save();
      ctx.strokeStyle = isSelected ? "rgba(29, 78, 216, .88)" : "rgba(37, 99, 235, .44)";
      ctx.lineWidth = isSelected ? 2.2 : 1.3;
      ctx.setLineDash([]);
      ctx.beginPath();
      if (showDepartLeg && departGridPt) {
        ctx.moveTo(departBase.x, departBase.y);
        ctx.lineTo(departGridPt.x, departGridPt.y);
      }
      planned.forEach((pose, index) => {
        const pt = gridCenter(pose[0], pose[1], cellSize, ox, oy);
        if (index === 0 && showDepartLeg) {
          // connected from base star
        } else if (index === 0) {
          ctx.moveTo(pt.x, pt.y);
        } else {
          ctx.lineTo(pt.x, pt.y);
        }
      });
      ctx.stroke();
      ctx.restore();
    }

    // ── Standoff orbit ring (tracking) ────────────────────────────
    if (uav.status === "tracking" && uav.target_group_id) {
      ctx.save();
      ctx.strokeStyle = isSelected ? "rgba(190, 18, 60, .72)" : "rgba(190, 18, 60, .34)";
      ctx.lineWidth = isSelected ? 1.4 : 0.8;
      ctx.setLineDash([3, 4]);
      const lastPt = planned.length
        ? gridCenter(planned[planned.length - 1][0], planned[planned.length - 1][1], cellSize, ox, oy)
        : uav.position?.length >= 2
          ? gridCenter(uav.position[0], uav.position[1], cellSize, ox, oy)
          : null;
      if (lastPt) {
        ctx.beginPath();
        ctx.arc(lastPt.x, lastPt.y, 1.8 * cellSize, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.restore();
    }

    // ── Storm-avoidance detour path (cyan, prominent) ────────────
    const avoidancePath = uav.avoidance_path || [];
    if (avoidancePath.length >= 2) {
      ctx.save();
      ctx.strokeStyle = "rgba(8, 145, 178, .92)";
      ctx.lineWidth = 1.6;
      ctx.setLineDash([5, 3]);
      ctx.beginPath();
      avoidancePath.forEach((pose, index) => {
        const pt = gridCenter(pose[0], pose[1], cellSize, ox, oy);
        if (index === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
      });
      ctx.stroke();
      ctx.restore();
    }
  }
}

export function drawUavTrails(ctx, uavs, cellSize, ox, oy, selectedId, trailMode) {
  for (const uav of uavs || []) {
    const trail = uav.trail || [];
    if (trail.length < 2) continue;
    const color = UAV_STATUS_COLORS[uav.status] || "#475569";
    ctx.save();
    ctx.lineCap = "round";

    // ── Mode: full ── uniform thin line over the entire trail ─────
    if (trailMode === "full") {
      ctx.strokeStyle = color;
      ctx.globalAlpha = uav.id === selectedId ? 0.9 : 0.62;
      ctx.lineWidth = uav.id === selectedId ? 2.2 : 1.35;
      ctx.beginPath();
      trail.forEach(([col, row], index) => {
        const point = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
      });
      ctx.stroke();
      ctx.restore();
      continue;
    }

    // ── Mode: comet ── filled tapered shape, wide at UAV, point at tail
    if (trailMode === "comet") {
      const maxHalfWidth = cellSize * (uav.id === selectedId ? 0.52 : 0.30);
      const maxAlpha = uav.id === selectedId ? 0.48 : 0.26;
      // Build polygon vertices from tail to head along left edge,
      // then back along right edge.
      const left = [];
      const right = [];
      for (let i = 0; i < trail.length; i += 1) {
        const t = i / Math.max(1, trail.length - 1); // 0→tail  1→head
        const halfW = Math.max(0.2, maxHalfWidth * t * t); // quadratic taper
        let dx = 0, dy = 0;
        if (i < trail.length - 1) {
          dx = trail[i + 1][0] - trail[i][0];
          dy = trail[i + 1][1] - trail[i][1];
        } else if (i > 0) {
          dx = trail[i][0] - trail[i - 1][0];
          dy = trail[i][1] - trail[i - 1][1];
        }
        const len = Math.hypot(dx, dy) || 1;
        const px = -dy / len * halfW;
        const py = dx / len * halfW;
        const pt = gridCenter(trail[i][0], trail[i][1], cellSize, ox, oy);
        left.push({ x: pt.x + px, y: pt.y + py, t });
        right.push({ x: pt.x - px, y: pt.y - py, t });
      }
      // Draw filled polygon
      const headAlpha = maxAlpha;
      ctx.globalAlpha = headAlpha;
      ctx.fillStyle = color;
      ctx.beginPath();
      for (let i = 0; i < left.length; i += 1) {
        if (i === 0) ctx.moveTo(left[i].x, left[i].y);
        else ctx.lineTo(left[i].x, left[i].y);
      }
      for (let i = right.length - 1; i >= 0; i -= 1) {
        ctx.lineTo(right[i].x, right[i].y);
      }
      ctx.closePath();
      ctx.fill();
      // Thin centerline on top for definition
      ctx.globalAlpha = headAlpha * 1.3;
      ctx.strokeStyle = color;
      ctx.lineWidth = Math.max(0.5, cellSize * (uav.id === selectedId ? 0.12 : 0.07));
      ctx.beginPath();
      trail.forEach(([col, row], index) => {
        const pt = gridCenter(col, row, cellSize, ox, oy);
        if (index === 0) ctx.moveTo(pt.x, pt.y);
        else ctx.lineTo(pt.x, pt.y);
      });
      ctx.stroke();
      // Glow head dot
      if (trail.length) {
        const head = trail[trail.length - 1];
        const h = gridCenter(head[0], head[1], cellSize, ox, oy);
        ctx.globalAlpha = headAlpha * 1.5;
        ctx.fillStyle = "#FFFFFF";
        ctx.beginPath();
        ctx.arc(h.x, h.y, maxHalfWidth * 0.85, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.restore();
      continue;
    }

    // ── Mode: tail (default) ── gradient-width line, last 72 points
    const start = Math.max(1, trail.length - 72);
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

function fallbackSarBeam(uav) {
  const [x, y] = uav.position || [];
  if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
  const heading = (Number(uav.heading_deg) || 0) * Math.PI / 180;
  const rightLooking = uav.sar_look_direction !== "left";
  const side = rightLooking
    ? [-Math.sin(heading), Math.cos(heading)]
    : [Math.sin(heading), -Math.cos(heading)];
  const forward = [Math.cos(heading), Math.sin(heading)];
  const point = (along, cross) => [
    x + forward[0] * along + side[0] * cross,
    y + forward[1] * along + side[1] * cross,
  ];
  return {
    polygon: [
      [x, y],                // UAV (beam apex)
      point(-2.5, 0.25),     // behind-near
      point(-2.5, 2.25),     // behind-far
      point(2.5, 2.25),      // ahead-far
      point(2.5, 0.25),      // ahead-near
    ],
  };
}

function sensorPhase(id) {
  return [...String(id || "UAV")].reduce((sum, char) => sum + char.charCodeAt(0), 0) * 0.17;
}

function drawSarBeam(ctx, uav, cellSize, ox, oy, phase) {
  const beam = uav.sar_beam?.polygon?.length >= 4 ? uav.sar_beam : fallbackSarBeam(uav);
  if (!beam?.polygon) return;
  const points = beam.polygon.map(([col, row]) => gridCenter(col, row, cellSize, ox, oy));
  const apertureTrack = (uav.sar_aperture_track || [])
    .filter((point) => Array.isArray(point) && point.length >= 2)
    .map(([col, row]) => ({ col: Number(col), row: Number(row) }));
  ctx.save();
  ctx.beginPath();
  ctx.rect(ox, oy, 30 * cellSize, 30 * cellSize);
  ctx.clip();

  // The long, offset polygon is the coherent strip accumulated from a
  // stable straight flight segment.  It disappears at a connector/turn,
  // rather than pretending that a turning aircraft produces a SAR image.
  if (apertureTrack.length >= 2) {
    const start = apertureTrack[0];
    const end = apertureTrack[apertureTrack.length - 1];
    const heading = Number(beam.heading) || 0;
    const sideSign = beam.look_direction === "left" ? -1 : 1;
    const side = {
      x: -Math.sin(heading) * sideSign,
      y: Math.cos(heading) * sideSign,
    };
    const near = Number(beam.near_range || 0.25);
    const far = Number(beam.far_range || 2.25);
    const stripPoint = (point, range) => gridCenter(
      point.col + side.x * range,
      point.row + side.y * range,
      cellSize,
      ox,
      oy,
    );
    const strip = [
      stripPoint(start, near),
      stripPoint(end, near),
      stripPoint(end, far),
      stripPoint(start, far),
    ];
    ctx.beginPath();
    strip.forEach((point, index) => {
      if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
    });
    ctx.closePath();
    ctx.fillStyle = "rgba(8, 145, 178, .10)";
    ctx.fill();
    ctx.strokeStyle = "rgba(14, 116, 144, .54)";
    ctx.lineWidth = 1;
    ctx.stroke();

    ctx.beginPath();
    apertureTrack.forEach((point, index) => {
      const pixel = gridCenter(point.col, point.row, cellSize, ox, oy);
      if (index === 0) ctx.moveTo(pixel.x, pixel.y); else ctx.lineTo(pixel.x, pixel.y);
    });
    ctx.strokeStyle = "rgba(8, 145, 178, .96)";
    ctx.lineWidth = Math.max(1.5, cellSize * 0.10);
    ctx.setLineDash([]);
    ctx.stroke();

    const pulse = 0.12 + 0.76 * (0.5 + 0.5 * Math.sin(phase * 0.055 + sensorPhase(uav.id)));
    const pulseIndex = Math.min(
      apertureTrack.length - 1,
      Math.max(0, Math.round((apertureTrack.length - 1) * pulse)),
    );
    const pulsePoint = apertureTrack[pulseIndex];
    const pulseNear = stripPoint(pulsePoint, near);
    const pulseFar = stripPoint(pulsePoint, far);
    ctx.beginPath();
    ctx.moveTo(pulseNear.x, pulseNear.y);
    ctx.lineTo(pulseFar.x, pulseFar.y);
    ctx.strokeStyle = "rgba(165, 243, 252, .96)";
    ctx.lineWidth = Math.max(1, cellSize * 0.075);
    ctx.stroke();
  }

  // Fan-shaped instantaneous side-looking beam.  The polygon fans out
  // from the UAV (apex) to the near-range edge and outward.  During
  // U-turn connectors between scan legs the beam stays visible but dims
  // so the UAV reads as continuously in motion.  During initial transit
  // to a search area the radar is powered and rendered at medium visibility.
  const imaging = uav.sar_imaging;
  const standby = !imaging && uav.sar_standby;
  ctx.beginPath();
  points.forEach((point, index) => {
    if (index === 0) ctx.moveTo(point.x, point.y); else ctx.lineTo(point.x, point.y);
  });
  ctx.closePath();
  if (imaging) {
    ctx.fillStyle = "rgba(8, 145, 178, .18)";
    ctx.strokeStyle = "rgba(14, 116, 144, .92)";
    ctx.lineWidth = 1.25;
  } else if (standby) {
    ctx.fillStyle = "rgba(8, 145, 178, .10)";
    ctx.strokeStyle = "rgba(14, 116, 144, .58)";
    ctx.lineWidth = 1.05;
  } else {
    ctx.fillStyle = "rgba(8, 145, 178, .05)";
    ctx.strokeStyle = "rgba(14, 116, 144, .28)";
    ctx.lineWidth = 0.8;
  }
  ctx.fill();
  ctx.stroke();

  // Near-range edge (closest to flight track) — dashed reference line
  if (imaging) {
    ctx.strokeStyle = "rgba(14, 116, 144, .6)";
  } else if (standby) {
    ctx.strokeStyle = "rgba(14, 116, 144, .36)";
  } else {
    ctx.strokeStyle = "rgba(14, 116, 144, .18)";
  }
  ctx.setLineDash([Math.max(2, cellSize * 0.14), Math.max(2, cellSize * 0.12)]);
  ctx.beginPath();
  ctx.moveTo(points[1].x, points[1].y);
  ctx.lineTo(points[points.length - 1].x, points[points.length - 1].y);
  ctx.stroke();
  ctx.restore();
}

function drawEoBeam(ctx, uav, cellSize, ox, oy, phase) {
  const fov = uav.eo_fov;
  if (!fov?.origin) return;
  const origin = gridCenter(fov.origin[0], fov.origin[1], cellSize, ox, oy);
  const heading = Number.isFinite(Number(fov.heading))
    ? Number(fov.heading)
    : (Number(uav.heading_deg) || 0) * Math.PI / 180;
  const halfAngle = Number(fov.half_angle) || Math.PI / 45;
  const radius = Math.max(cellSize * 0.25, Number(fov.max_range || 0.25) * cellSize);
  ctx.save();
  ctx.beginPath();
  ctx.rect(ox, oy, 30 * cellSize, 30 * cellSize);
  ctx.clip();
  ctx.beginPath();
  ctx.moveTo(origin.x, origin.y);
  ctx.arc(origin.x, origin.y, radius, heading - halfAngle, heading + halfAngle);
  ctx.closePath();
  ctx.fillStyle = "rgba(245, 158, 11, .15)";
  ctx.strokeStyle = "rgba(180, 83, 9, .9)";
  ctx.lineWidth = 1.2;
  ctx.fill();
  ctx.stroke();

  const sweepAngle = heading + halfAngle * 0.88 * Math.sin(phase * 0.07 + sensorPhase(uav.id));
  ctx.strokeStyle = "rgba(253, 230, 138, .98)";
  ctx.lineWidth = Math.max(1, cellSize * 0.07);
  ctx.beginPath();
  ctx.moveTo(origin.x, origin.y);
  ctx.lineTo(
    origin.x + Math.cos(sweepAngle) * radius,
    origin.y + Math.sin(sweepAngle) * radius,
  );
  ctx.stroke();

  ctx.strokeStyle = "rgba(217, 119, 6, .34)";
  ctx.lineWidth = 0.8;
  for (const ratio of [0.55, 0.8]) {
    ctx.beginPath();
    ctx.arc(origin.x, origin.y, radius * ratio, heading - halfAngle, heading + halfAngle);
    ctx.stroke();
  }
  ctx.restore();
}

export function drawSensorFootprints(ctx, uavs, cellSize, ox, oy, phase = 0) {
  for (const uav of uavs || []) {
    const hasSarBeam = !!(uav.sar_beam || uav.sar_imaging || uav.sensor_mode === "sar");
    if (hasSarBeam) {
      drawSarBeam(ctx, uav, cellSize, ox, oy, phase);
    }
    if (uav.sar_imaging) {
      ctx.fillStyle = "rgba(6, 182, 212, .16)";
      for (const [col, row] of uav.sar_footprint || []) {
        const point = coordToPixel(col, row, cellSize, ox, oy);
        ctx.fillRect(point.x + 1, point.y + 1, cellSize - 2, cellSize - 2);
      }
    }
    if (uav.sensor_mode === "eo") drawEoBeam(ctx, uav, cellSize, ox, oy, phase);
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
    ? { x: 32, y: 74, width: 1472, height: 842 }
    : { x: 400, y: 78, width: 224, height: 1290 };
  const modelWidth = carrier
    ? Math.max(22, cellSizeForShip(size, true))
    : Math.max(6.5, size * 0.82);
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

function drawClassificationSymbol(ctx, ship, center, size, classification) {
  if (ship.departed) return;
  ctx.save();
  ctx.lineWidth = 1;
  if (classification === "type_ii") {
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
  } else if (classification === "type_i") {
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
  const radius = cellSize * 3;
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
    const classification = ship.vessel_class || ship.assessment?.vessel_class || "unknown";
    const color = classification === "type_ii"
      ? "#E11D48"
      : classification === "type_i" ? "#0369A1" : "#CA8A04";
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
    drawClassificationSymbol(ctx, ship, center, size, classification);
    if (ship.ais?.reported_position && !ship.departed) {
      const report = gridCenter(ship.ais.reported_position[0], ship.ais.reported_position[1], cellSize, ox, oy);
      ctx.save();
      ctx.strokeStyle = classification === "type_ii"
        ? "rgba(251, 113, 133, .72)" : "rgba(148, 163, 184, .52)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 3]);
      ctx.beginPath();
      ctx.moveTo(center.x, center.y);
      ctx.lineTo(report.x, report.y);
      ctx.stroke();
      ctx.restore();
    }
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

export function drawBases(ctx, bases, baseCenters, cellSize, phase) {
  for (const [index, base] of (bases || []).entries()) {
    const center = baseCenters[index];
    if (!center) continue;
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
  }
}

export function drawUavs(ctx, uavs, cellSize, ox, oy, selectedId, assets, baseCenters) {
  for (const uav of uavs || []) {
    const center = resolveUavDisplayCenter(uav, cellSize, ox, oy, baseCenters);
    const color = UAV_STATUS_COLORS[uav.status] || "#94A3B8";
    const size = Math.max(5, cellSize * (uav.id === selectedId ? 0.42 : 0.32));
    ctx.save();
    ctx.globalAlpha = uav.status === "refueling" ? 0.34 : 1;
    const renderedModel = drawSprite(
      ctx,
      assets?.uav,
      { x: 74, y: 205, width: 1388, height: 526 },
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
    if (uav.avoidance_level > 0) {
      const level = Number(uav.avoidance_level);
      const levelColor = level >= 3 ? "#F87171" : level === 2 ? "#FBBF24" : "#67E8F9";
      text(ctx, `L${level}`, center.x + size + 3, center.y + size + 8, levelColor, Math.max(7, cellSize * 0.25), 700);
    }
  }
}

function contactLabel(contact) {
  if (contact.state === "lost") return `${contact.contact_id} 暂时丢失`;
  if (contact.state === "departed") return `${contact.contact_id} 已离场`;
  if (contact.vessel_class === "type_ii") return `${contact.contact_id} II 类`;
  if (contact.vessel_class === "type_i") return `${contact.contact_id} I 类`;
  return `${contact.contact_id} 待核查`;
}

function legacyShipLabel(ship) {
  const label = ship.ship_type === "carrier" ? "航母" : "水面目标";
  return `${ship.id} ${label}`;
}

function addLabel(ctx, labels, styles, id, anchor, value, priority, color, selected) {
  const fontSize = 9;
  ctx.font = `600 ${fontSize}px ${FONT}`;
  labels.push({
    id,
    anchor,
    text: value,
    width: ctx.measureText(value).width + 10,
    height: 17,
    priority,
  });
  styles.set(id, { color, selected, fontSize });
}

function drawLabelLeader(ctx, anchor, label) {
  const endX = clamp(anchor.x, label.x, label.x + label.width);
  const endY = clamp(anchor.y, label.y, label.y + label.height);
  ctx.beginPath();
  ctx.moveTo(anchor.x, anchor.y);
  ctx.lineTo(endX, endY);
  ctx.stroke();
}

/** Draw all object text after symbols have been placed in screen space. */
export function drawLabels(
  ctx,
  frame,
  cellSize,
  ox,
  oy,
  bounds,
  selectedUavId,
  selectedContactId,
  selectedScenarioVesselId,
  baseCenters,
  showScenario = false,
) {
  const labels = [];
  const styles = new Map();
  const uavs = frame?.uavs || [];
  const contacts = Array.isArray(frame?.contacts) && frame.contacts.length
    ? frame.contacts
    : [];
  const legacyShips = contacts.length ? [] : (frame?.ships || []).filter((ship) => ship?.is_detected);

  for (const uav of uavs) {
    const center = resolveUavDisplayCenter(uav, cellSize, ox, oy, baseCenters);
    const display = uavDisplayState(uav);
    const selected = uav.id === selectedUavId;
    addLabel(
      ctx,
      labels,
      styles,
      `uav:${uav.id}`,
      center,
      `${uav.id.replace("UAV-", "U")} · ${display.label}`,
      selected ? "selected" : "uav",
      UAV_STATUS_COLORS[uav.status] || "#334155",
      selected,
    );
  }

  for (const contact of contacts) {
    const position = contact.estimated_position;
    if (!Array.isArray(position) || position.length < 2) continue;
    const center = gridCenter(Number(position[0]), Number(position[1]), cellSize, ox, oy);
    const classified = contact.vessel_class === "type_i" || contact.vessel_class === "type_ii";
    const selected = contact.contact_id === selectedContactId;
    addLabel(
      ctx,
      labels,
      styles,
      `contact:${contact.contact_id}`,
      center,
      contactLabel(contact),
      selected ? "selected" : classified ? "classified" : "contact",
      contactColor(contact),
      selected,
    );
  }

  for (const ship of legacyShips) {
    const position = ship.position || ship.estimated_position;
    if (!Array.isArray(position) || position.length < 2) continue;
    addLabel(
      ctx,
      labels,
      styles,
      `ship:${ship.id}`,
      gridCenter(Number(position[0]), Number(position[1]), cellSize, ox, oy),
      legacyShipLabel(ship),
      "contact",
      "#334155",
      false,
    );
  }

  if (showScenario) {
    for (const vessel of frame?.scenario_vessels || []) {
      const position = vessel.position;
      if (!Array.isArray(position) || position.length < 2) continue;
      const selected = vessel.scenario_entity_id === selectedScenarioVesselId;
      addLabel(
        ctx,
        labels,
        styles,
        `scenario:${vessel.scenario_entity_id}`,
        gridCenter(Number(position[0]), Number(position[1]), cellSize, ox, oy),
        `场景 · ${vessel.scenario_entity_id}`,
        selected ? "selected" : "scenario",
        vessel.vessel_class === "type_ii" ? "#B45309" : "#0369A1",
        selected,
      );
    }
  }

  for (const [index, base] of (frame?.bases || []).entries()) {
    const center = baseCenters?.[index];
    if (!center) continue;
    addLabel(
      ctx,
      labels,
      styles,
      `base:${base.id || index}`,
      center,
      `B${base.number || index + 1} ${base.occupancy || 0}/${base.capacity || 3}`,
      "uav",
      "#991B1B",
      false,
    );
  }

  const placed = layoutLabels(labels, bounds);
  for (const label of placed) {
    if (label.hidden) continue;
    const source = labels.find((item) => item.id === label.id);
    const style = styles.get(label.id);
    if (!source || !style) continue;
    ctx.save();
    if (style.selected) {
      ctx.strokeStyle = `${style.color}B8`;
      ctx.lineWidth = 1;
      drawLabelLeader(ctx, label.anchor, label);
      ctx.stroke();
    }
    ctx.fillStyle = style.selected ? "rgba(255, 255, 255, .98)" : "rgba(255, 255, 255, .88)";
    ctx.strokeStyle = style.selected ? style.color : "rgba(100, 116, 139, .44)";
    ctx.lineWidth = style.selected ? 1.2 : 0.7;
    ctx.fillRect(label.x, label.y, label.width, label.height);
    ctx.strokeRect(label.x, label.y, label.width, label.height);
    text(ctx, source.text, label.x + 5, label.y + 12, style.color, style.fontSize, style.selected ? 700 : 600);
    ctx.restore();
  }
  return placed;
}

export function drawTransparencyLegend(ctx, bounds) {
  if (!bounds || bounds.width < 128) return;
  const swatches = [
    { color: "#D97706", label: "TASK CELLS" },
    { color: "#0F766E", label: "FRESH SAR" },
    { color: "#0891B2", label: "SAR APERTURE / SWATH", shape: "strip" },
    { color: "#D97706", label: "EO / IR FOV", shape: "cone" },
    { color: "#DC2626", label: "NO-FLY STORM" },
    { color: "#0E7490", label: "SHIP RADAR" },
    { color: "#2563EB", label: "UAV TRANSIT" },
    { color: "#BE123C", label: "TARGET CONTACT" },
    { color: "#DC2626", label: "BASE STAR" },
    { color: "#334155", label: "SIM GRID BORDER" },
  ];
  const horizontalInset = 8;
  const width = Math.max(128, bounds.width - horizontalInset * 2);
  const availableHeight = bounds.height;
  const itemHeight = clamp(Math.floor((availableHeight - 32) / swatches.length), 13, 17);
  const swatchSize = clamp(Math.floor(itemHeight * 0.58), 7, 10);
  const titleSize = clamp(Math.floor(width * 0.055), 7, 8);
  const labelSize = clamp(Math.floor(width * 0.045), 6, 7);
  const height = 24 + swatches.length * itemHeight + 8;
  if (height > availableHeight) return;
  const x = bounds.x + horizontalInset;
  const y = bounds.y + 10;
  ctx.fillStyle = "rgba(255, 255, 255, .94)";
  ctx.strokeStyle = "rgba(71, 85, 105, .72)";
  ctx.lineWidth = 1;
  ctx.fillRect(x, y, width, height);
  ctx.strokeRect(x, y, width, height);
  text(ctx, "MAP LEGEND", x + 8, y + 13, "#334155", titleSize, 700);
  swatches.forEach((swatch, index) => {
    const itemX = x + 9;
    const itemY = y + 22 + index * itemHeight;
    ctx.fillStyle = swatch.color;
    ctx.strokeStyle = "#64748B";
    if (swatch.shape === "strip") {
      ctx.fillRect(itemX, itemY + swatchSize * 0.2, swatchSize, swatchSize * 0.6);
      ctx.strokeRect(itemX, itemY + swatchSize * 0.2, swatchSize, swatchSize * 0.6);
    } else if (swatch.shape === "cone") {
      ctx.beginPath();
      ctx.moveTo(itemX, itemY + swatchSize);
      ctx.lineTo(itemX + swatchSize, itemY + swatchSize * 0.15);
      ctx.lineTo(itemX + swatchSize, itemY + swatchSize);
      ctx.closePath();
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.fillRect(itemX, itemY, swatchSize, swatchSize);
      ctx.strokeRect(itemX, itemY, swatchSize, swatchSize);
    }
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
    mapBounds,
    legendBounds,
    trailMode = "tail",
    selectedContactId,
    showScenario = false,
    selectedScenarioVesselId,
  } = options;
  const width = ctx.canvas.clientWidth || ctx.canvas.width;
  const height = ctx.canvas.clientHeight || ctx.canvas.height;
  const bases = normalizeBases(frame?.bases, frame?.base_position);
  const fallbackBounds = {
    x: offsetX,
    y: offsetY,
    width: 30 * cellSize,
    height: 30 * cellSize,
  };
  const resolvedMapBounds = mapBounds || fallbackBounds;
  const baseCenters = buildBaseCenters(bases, resolvedMapBounds);
  drawBackground(ctx, width, height, cellSize, offsetX, offsetY, resolvedMapBounds, assets);
  if (frame) {
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawTransparencyOverlay(ctx, frame.info_matrix, cellSize, offsetX, offsetY);
    drawOceanTexture(ctx, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawSearchDomain(ctx, frame.search_domain, cellSize, offsetX, offsetY);
    drawObstacles(ctx, frame.obstacles, cellSize, offsetX, offsetY, frameCount);
    drawIntents(ctx, frame.intents, frame.intent_statuses, cellSize, offsetX, offsetY);
    drawSearchRegions(ctx, frame.search_regions, frame.uavs, cellSize, offsetX, offsetY);
    const contacts = Array.isArray(frame.contacts) && frame.contacts.length
      ? frame.contacts : frame.ships;
    drawTrackRegions(ctx, frame.track_regions, contacts, cellSize, offsetX, offsetY);
    drawPassiveEvidence(ctx, frame.evidence, cellSize, offsetX, offsetY, frameCount, frame.passive_detection_range_cells);
    drawUavTrails(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId, trailMode);
    drawPaths(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId, baseCenters);
    drawSensorFootprints(ctx, frame.uavs, cellSize, offsetX, offsetY, frameCount);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    if (Array.isArray(frame.contacts) && frame.contacts.length) {
      drawContacts(ctx, frame.contacts, cellSize, offsetX, offsetY, selectedContactId, frameCount);
    } else {
      drawShips(ctx, frame.ships, cellSize, offsetX, offsetY, assets);
    }
    if (showScenario) {
      drawScenarioVessels(ctx, frame.scenario_vessels, cellSize, offsetX, offsetY, selectedScenarioVesselId);
    }
    drawUavs(ctx, frame.uavs, cellSize, offsetX, offsetY, selectedUavId, assets, baseCenters);
    drawBases(ctx, bases, baseCenters, cellSize, frameCount);
    drawLabels(
      ctx,
      frame,
      cellSize,
      offsetX,
      offsetY,
      resolvedMapBounds,
      selectedUavId,
      selectedContactId,
      selectedScenarioVesselId,
      baseCenters,
      showScenario,
    );
    drawTransparencyLegend(ctx, legendBounds);
    const domain = frame.search_domain;
    const gridLabel = domain
      ? `SIM GRID / ${domain.cols * domain.cell_size_km} x ${domain.rows * domain.cell_size_km} KM`
      : "SIM GRID / 300 x 300 KM";
    ctx.save();
    ctx.fillStyle = "rgba(255, 255, 255, .94)";
    ctx.fillRect(offsetX + 5, offsetY + 5, Math.min(30 * cellSize - 10, 280), 32);
    text(ctx, gridLabel, offsetX + 9, offsetY + 17, "#0B3857", 9, 700);
    const domainLabel = domain
      ? domain.excluded_cells?.length
        ? `SEARCH DOMAIN / ${domain.area_km2} KM² · HATCH = EXCLUDED`
        : `FULL TASK AREA / ${domain.area_km2} KM²`
      : "SEARCH DOMAIN / DATA MISSING";
    text(ctx, domainLabel, offsetX + 9, offsetY + 30, "#0F766E", 8, 700);
    ctx.restore();
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY, width, height);
}
