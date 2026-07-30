/**
 * 色值常量 + 双层热力颜色映射。
 */

// 搜索区优先级色
export const PRIORITY_COLORS = {
  high: "#F87171",
  medium: "#FBBF24",
  low: "#60A5FA",
};

// UAV 状态色
export const UAV_STATUS_COLORS = {
  searching: "#22C55E",
  tracking: "#EF4444",
  returning: "#F97316",
  refueling: "#3B82F6",
  idle: "#9CA3AF",
  transit: "#60A5FA",
};

// 标记点按年龄着色
export function markerColor(ageMinutes) {
  if (ageMinutes < 15) return { fill: "#F97316", alpha: 1.0 };
  if (ageMinutes < 45) return { fill: "#FBBF24", alpha: 0.8 };
  return { fill: "#9A3412", alpha: 0.5 };  // 45-60 min
}

/**
 * 信息素 I + 信息价值 V → HSL
 *
 * H = 120 × (1 - V)    → 0°(红,高价值) ~ 120°(绿,低价值)
 * S = V × 100%
 * L = 由 I 映射:  ≥0.7→85%, ≥0.2→60%, >0→25%, 0→10%
 */
export function infoValueToHSL(I, V) {
  const h = 120 * (1 - V);
  const s = V * 100;
  let l;
  if (I >= 0.7) l = 85;
  else if (I >= 0.2) l = 60;
  else if (I > 0) l = 25;
  else l = 10;
  return { h, s, l };
}

export function hslToString({ h, s, l }) {
  return `hsl(${h}, ${s}%, ${l}%)`;
}

/** 态势分类 */
export function infoCategory(I) {
  if (I > 0.7) return "white";
  if (I >= 0.2) return "gray";
  return "black";
}

/** UAV 最大续航 (km) */
export const MAX_RANGE_KM = 4800;
