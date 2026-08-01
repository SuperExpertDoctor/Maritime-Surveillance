export const PRIORITY_COLORS = {
  high: "#DC2626",
  medium: "#D97706",
  low: "#2563EB",
};

export const UAV_STATUS_COLORS = {
  searching: "#0F766E",
  tracking: "#BE123C",
  returning: "#C2410C",
  refueling: "#0369A1",
  holding: "#A16207",
  idle: "#475569",
  transit: "#1D4ED8",
};

export function markerColor(ageMinutes) {
  if (ageMinutes < 15) return { fill: "#EA580C", alpha: 1 };
  if (ageMinutes < 45) return { fill: "#CA8A04", alpha: 0.86 };
  return { fill: "#854D0E", alpha: 0.64 };
}
