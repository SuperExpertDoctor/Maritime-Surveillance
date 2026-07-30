import { coordToPixel } from "./geometry";
import {
  PRIORITY_COLORS,
  UAV_STATUS_COLORS,
  markerColor,
  infoValueToHSL,
  hslToString,
  MAX_RANGE_KM,
} from "./colors";

// ── Layer 0: 背景 ──
export function drawBackground(ctx, w, h) {
  ctx.fillStyle = "#0D1117";
  ctx.fillRect(0, 0, w, h);
}

// ── Layer 1: 双层融合热力 ──
export function drawHeatmap(ctx, infoMatrix, valueMatrix, cellSize, ox, oy) {
  for (let col = 0; col < 30; col++) {
    for (let row = 0; row < 30; row++) {
      const I = (infoMatrix && infoMatrix[col] && infoMatrix[col][row] != null)
        ? infoMatrix[col][row] : 0;
      const V = (valueMatrix && valueMatrix[col] && valueMatrix[col][row] != null)
        ? valueMatrix[col][row] : 0;
      const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
      const hsl = infoValueToHSL(I, V);
      ctx.fillStyle = hslToString(hsl);
      ctx.fillRect(x, y, cellSize, cellSize);
    }
  }
}

// ── Layer 2: 网格线 ──
export function drawGridLines(ctx, cellSize, ox, oy, showGrid) {
  if (!showGrid) return;
  ctx.strokeStyle = "rgba(255,255,255,0.06)";
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 30; i++) {
    const px = ox + i * cellSize;
    ctx.beginPath();
    ctx.moveTo(px, oy);
    ctx.lineTo(px, oy + 30 * cellSize);
    ctx.stroke();
    const py = oy + i * cellSize;
    ctx.beginPath();
    ctx.moveTo(ox, py);
    ctx.lineTo(ox + 30 * cellSize, py);
    ctx.stroke();
  }
}

// ── Layer 3: 搜索区矩形 ──
export function drawSearchRegions(ctx, regions, cellSize, ox, oy) {
  if (!regions) return;
  for (const r of regions) {
    const [cs, rs, ce, re] = r.bbox;
    const { x, y } = coordToPixel(cs, rs, cellSize, ox, oy);
    const w = (ce - cs) * cellSize;
    const h = (re - rs) * cellSize;
    const color = PRIORITY_COLORS[r.priority] || PRIORITY_COLORS.medium;

    // 填充
    ctx.fillStyle = color + "18";
    ctx.fillRect(x, y, w, h);

    // 边框
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.strokeRect(x, y, w, h);

    // 标签
    const label = `${r.id} ${r.completion_pct != null ? Math.round(r.completion_pct) + "%" : ""}`;
    ctx.font = "11px sans-serif";
    const textW = ctx.measureText(label).width + 8;
    ctx.fillStyle = "#0D1117";
    ctx.fillRect(x + 2, y + 2, textW, 18);
    ctx.fillStyle = color;
    ctx.fillText(label, x + 6, y + 15);
  }
}

// ── Layer 4: 跟踪区矩形 ──
export function drawTrackRegions(ctx, regions, cellSize, ox, oy) {
  if (!regions) return;
  for (const r of regions) {
    const [cs, rs, ce, re] = r.bbox;
    const { x, y } = coordToPixel(cs, rs, cellSize, ox, oy);
    const w = (ce - cs) * cellSize;
    const h = (re - rs) * cellSize;

    ctx.fillStyle = "rgba(239,68,68,0.06)";
    ctx.fillRect(x, y, w, h);

    ctx.strokeStyle = "#EF4444";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 3]);
    ctx.strokeRect(x, y, w, h);
    ctx.setLineDash([]);

    const label = r.id;
    ctx.font = "11px sans-serif";
    ctx.fillStyle = "#EF4444";
    ctx.fillRect(x + 2, y + 2, ctx.measureText(label).width + 8, 18);
    ctx.fillStyle = "#FFF";
    ctx.fillText(label, x + 6, y + 15);
  }
}

// ── Layer 5: 标记点 ──
export function drawMarkers(ctx, markers, cellSize, ox, oy, currentTimeMin, frameCount) {
  if (!markers) return;
  for (const m of markers) {
    const [col, row] = m.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const age = currentTimeMin - m.created_time_min;
    if (age > 60) continue;

    const { fill, alpha } = markerColor(age);
    const r = 6 + 4 * Math.sin(frameCount / 50);

    ctx.globalAlpha = alpha;
    ctx.fillStyle = fill;
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(r, 3), 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 1;

    // 标签
    ctx.fillStyle = "#FFF";
    ctx.font = "10px sans-serif";
    ctx.fillText(m.id, cx + 10, cy + 4);
  }
}

// ── Layer 6: 船舶 + 轨迹 ──
export function drawShips(ctx, ships, cellSize, ox, oy) {
  if (!ships) return;
  const groupColors = { G1: "#EF4444", G2: "#3B82F6", G3: "#FBBF24" };
  for (const s of ships) {
    const [col, row] = s.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const color = groupColors[s.group_id] || "#9CA3AF";

    // 轨迹尾迹
    if (s.trail && s.trail.length > 1) {
      ctx.strokeStyle = color + "40";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      const first = s.trail[0];
      const fp = coordToPixel(
        Math.round(first[0]), Math.round(first[1]), cellSize, ox, oy
      );
      ctx.moveTo(fp.x + cellSize / 2, fp.y + cellSize / 2);
      for (let i = 1; i < s.trail.length; i++) {
        const pt = s.trail[i];
        const pp = coordToPixel(
          Math.round(pt[0]), Math.round(pt[1]), cellSize, ox, oy
        );
        ctx.lineTo(pp.x + cellSize / 2, pp.y + cellSize / 2);
      }
      ctx.stroke();
    }

    // 船舶三角
    const size = cellSize * 0.35;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy - size);
    ctx.lineTo(cx - size * 0.7, cy + size * 0.5);
    ctx.lineTo(cx + size * 0.7, cy + size * 0.5);
    ctx.closePath();
    ctx.fill();

    // 被跟踪光环
    if (s.is_detected) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, size + 3, 0, Math.PI * 2);
      ctx.stroke();
    }
  }
}

// ── Layer 7: UAV + 基地 ──
export function drawUavs(ctx, uavs, basePos, cellSize, ox, oy, selectedUavId) {
  // 基地
  if (basePos) {
    const [bc, br] = basePos;
    const bp = coordToPixel(bc, br, cellSize, ox, oy);
    const bcx = bp.x + cellSize / 2;
    const bcy = bp.y + cellSize / 2;
    const s = cellSize * 0.45;
    ctx.fillStyle = "#6B7280";
    ctx.beginPath();
    ctx.moveTo(bcx, bcy - s);
    ctx.lineTo(bcx - s, bcy + s * 0.6);
    ctx.lineTo(bcx + s, bcy + s * 0.6);
    ctx.closePath();
    ctx.fill();
    ctx.fillStyle = "#6B7280";
    ctx.font = "10px sans-serif";
    ctx.fillText("基地", bcx - 12, bcy + s + 14);
  }

  if (!uavs) return;
  for (const u of uavs) {
    const [col, row] = u.position;
    const { x, y } = coordToPixel(col, row, cellSize, ox, oy);
    const cx = x + cellSize / 2;
    const cy = y + cellSize / 2;
    const color = UAV_STATUS_COLORS[u.status] || "#9CA3AF";
    const size = cellSize * 0.3;
    const isSelected = u.id === selectedUavId;
    const drawSize = isSelected ? size * 1.4 : size;

    // 油量环形指示
    const rangePct = (u.remaining_range_km || 0) / MAX_RANGE_KM;
    ctx.strokeStyle = color + "40";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(cx, cy, drawSize + 5, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2);
    ctx.stroke();
    if (rangePct > 0) {
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, cy, drawSize + 5, -Math.PI / 2, -Math.PI / 2 + Math.PI * 2 * rangePct);
      ctx.stroke();
    }

    // UAV 三角
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.moveTo(cx, cy - drawSize);
    ctx.lineTo(cx - drawSize * 0.7, cy + drawSize * 0.5);
    ctx.lineTo(cx + drawSize * 0.7, cy + drawSize * 0.5);
    ctx.closePath();
    ctx.fill();

    // 标签
    ctx.fillStyle = "#FFF";
    ctx.font = isSelected ? "bold 11px sans-serif" : "10px sans-serif";
    const label = u.id.replace("UAV-", "U-");
    ctx.fillText(label, cx - ctx.measureText(label).width / 2, cy + drawSize + 14);
  }
}

// ── Layer 8: 配对连线（动画） ──
let pairingAnimations = [];

export function triggerPairing(uavId, regionBbox) {
  pairingAnimations.push({
    uavId,
    regionBbox,
    startTime: performance.now(),
    duration: 3000,
  });
}

export function drawPairingLines(ctx, uavs, cellSize, ox, oy, now) {
  pairingAnimations = pairingAnimations.filter((a) => now - a.startTime < a.duration);
  for (const anim of pairingAnimations) {
    const elapsed = now - anim.startTime;
    const alpha = 1 - elapsed / anim.duration;

    // UAV 位置
    const uav = uavs?.find((u) => u.id === anim.uavId);
    if (!uav) continue;
    const [uc, ur] = uav.position;
    const up = coordToPixel(uc, ur, cellSize, ox, oy);

    // 区域中心
    const [cs, rs, ce, re] = anim.regionBbox;
    const rcx = ox + ((cs + ce) / 2) * cellSize;
    const rcy = oy + ((rs + re) / 2) * cellSize;

    ctx.strokeStyle = `rgba(96,165,250,${alpha})`;
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 4]);
    ctx.beginPath();
    ctx.moveTo(up.x + cellSize / 2, up.y + cellSize / 2);
    ctx.lineTo(rcx, rcy);
    ctx.stroke();
    ctx.setLineDash([]);
  }
}

// ── Layer 9: Hover 交互（绘制浮窗 tooltip） ──
export function drawHoverTooltip(ctx, hoverInfo, cellSize, ox, oy) {
  if (!hoverInfo) return null;
  const { col, row, I, V, category } = hoverInfo;
  const { x, y } = coordToPixel(col, row, cellSize, ox, oy);

  // Cell 高亮边框
  ctx.strokeStyle = "rgba(255,255,255,0.25)";
  ctx.lineWidth = 2;
  ctx.strokeRect(x, y, cellSize, cellSize);

  // Tooltip 定位（避免超出 canvas）
  const tipX = Math.min(x + cellSize + 8, ctx.canvas.width - 170);
  const tipY = Math.min(y, ctx.canvas.height - 70);
  const tipW = 160;
  const tipH = 52;

  ctx.fillStyle = "rgba(22,27,34,0.95)";
  ctx.fillRect(tipX, tipY, tipW, tipH);
  ctx.strokeStyle = "#30363D";
  ctx.lineWidth = 1;
  ctx.strokeRect(tipX, tipY, tipW, tipH);

  ctx.fillStyle = "#E6EDF3";
  ctx.font = "12px sans-serif";
  ctx.fillText(`Cell(${col},${row})`, tipX + 8, tipY + 18);
  ctx.fillText(`信息素: ${(I ?? 0).toFixed(2)}  价值: ${(V ?? 0).toFixed(2)}`, tipX + 8, tipY + 34);
  ctx.fillStyle = category === "black" ? "#EF4444" : category === "gray" ? "#FBBF24" : "#22C55E";
  ctx.fillText(`${category === "black" ? "黑" : category === "gray" ? "灰" : "白"}态势`, tipX + 8, tipY + 48);
}

// ── 主渲染入口 ──
export function renderFrame(ctx, frame, opts = {}) {
  const {
    cellSize, offsetX, offsetY,
    showGrid = false,
    hoverInfo = null,
    selectedUavId = null,
    frameCount = 0,
  } = opts;

  const w = ctx.canvas.width;
  const h = ctx.canvas.height;

  drawBackground(ctx, w, h);
  if (frame) {
    drawHeatmap(ctx, frame.info_matrix, frame.value_matrix, cellSize, offsetX, offsetY);
    drawGridLines(ctx, cellSize, offsetX, offsetY, showGrid);
    drawSearchRegions(ctx, frame.search_regions, cellSize, offsetX, offsetY);
    drawTrackRegions(ctx, frame.track_regions, cellSize, offsetX, offsetY);
    drawMarkers(ctx, frame.markers, cellSize, offsetX, offsetY, frame.sim_time_min, frameCount);
    drawShips(ctx, frame.ships, cellSize, offsetX, offsetY);
    drawUavs(ctx, frame.uavs, frame.base_position, cellSize, offsetX, offsetY, selectedUavId);
    drawPairingLines(ctx, frame.uavs, cellSize, offsetX, offsetY, performance.now());
  }
  drawHoverTooltip(ctx, hoverInfo, cellSize, offsetX, offsetY);
}
