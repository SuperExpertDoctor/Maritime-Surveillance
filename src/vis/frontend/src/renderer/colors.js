export const PRIORITY_COLORS = {
  high: "#F87171",
  medium: "#FBBF24",
  low: "#60A5FA",
};

export const UAV_STATUS_COLORS = {
  searching: "#2DD4BF",
  tracking: "#FB7185",
  returning: "#F59E0B",
  refueling: "#38BDF8",
  holding: "#EAB308",
  idle: "#94A3B8",
  transit: "#60A5FA",
};

export function markerColor(ageMinutes) {
  if (ageMinutes < 15) return { fill: "#FB923C", alpha: 1 };
  if (ageMinutes < 45) return { fill: "#FACC15", alpha: 0.82 };
  return { fill: "#A16207", alpha: 0.58 };
}
