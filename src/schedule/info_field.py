import math
import numpy as np
from src.schedule.datatypes import GridCoord, BBox
from src.schedule.config_loader import AppConfig


class InfoField:
    def __init__(self, config: AppConfig):
        self.config = config
        gc = config.grid
        self.rows, self.cols = gc.resolution  # (30, 30)
        self.info = np.zeros((self.cols, self.rows), dtype=np.float64)
        self.last_scan_time = np.full((self.cols, self.rows), -np.inf, dtype=np.float64)
        self.is_track_scan = np.zeros((self.cols, self.rows), dtype=bool)
        self._markers: list[dict] = []

    def _decay_lambda(self, is_track: bool) -> float:
        gc = self.config.grid
        half_life = gc.track_decay_half_life_min if is_track else gc.decay_half_life_min
        return math.log(2) / half_life

    def update_decay(self, current_time: float) -> None:
        gc = self.config.grid
        # 只对已被扫描过的 cell 做衰减
        scanned_mask = np.isfinite(self.last_scan_time)
        if not np.any(scanned_mask):
            return

        dt = np.maximum(current_time - self.last_scan_time, 0.0)

        # 搜索扫描衰减
        search_mask = scanned_mask & ~self.is_track_scan
        lam_search = self._decay_lambda(False)
        self.info[search_mask] = np.exp(-lam_search * dt[search_mask])

        # 跟踪扫描衰减
        track_mask = scanned_mask & self.is_track_scan
        lam_track = self._decay_lambda(True)
        self.info[track_mask] = np.exp(-lam_track * dt[track_mask])

        # 限制在 [0, 1]
        np.clip(self.info, 0.0, 1.0, out=self.info)

    def scan_cell(self, coord: GridCoord, current_time: float, is_track: bool = False) -> None:
        c, r = coord
        if 0 <= c < self.cols and 0 <= r < self.rows:
            self.info[c, r] = 1.0
            self.last_scan_time[c, r] = current_time
            self.is_track_scan[c, r] = is_track

    def scan_bbox(self, bbox: BBox, current_time: float, is_track: bool = False) -> None:
        c0, r0, c1, r1 = bbox
        c0 = max(0, c0); r0 = max(0, r0)
        c1 = min(self.cols, c1); r1 = min(self.rows, r1)
        self.info[c0:c1, r0:r1] = 1.0
        self.last_scan_time[c0:c1, r0:r1] = current_time
        self.is_track_scan[c0:c1, r0:r1] = is_track

    def add_marker(self, position: GridCoord, current_time: float, marker_id: str) -> None:
        self._markers.append({
            "id": marker_id,
            "position": position,
            "created_time": current_time,
        })

    def remove_marker(self, marker_id: str) -> None:
        self._markers = [m for m in self._markers if m["id"] != marker_id]

    def _strategic_field(self, current_time: float) -> np.ndarray:
        """S(c,r): 标记点高斯衰减场。"""
        gc = self.config.grid
        S = np.zeros((self.cols, self.rows), dtype=np.float64)
        for marker in self._markers:
            age = current_time - marker["created_time"]
            if age > gc.marker_max_age_min:
                continue
            # 时间衰减因子（线性）
            time_factor = max(0.0, 1.0 - age / gc.marker_max_age_min)
            mc, mr = marker["position"]
            for c in range(self.cols):
                for r in range(self.rows):
                    dist = math.sqrt((c - mc) ** 2 + (r - mr) ** 2)
                    gauss = math.exp(-0.5 * (dist / gc.marker_sigma_cells) ** 2)
                    S[c, r] = max(S[c, r], gauss * time_factor)
        return S

    def _timeliness_field(self, current_time: float) -> np.ndarray:
        """A(c,r): 标记点时效性。A(c,r) = exp(-t_marker / time_constant)."""
        gc = self.config.grid
        A = np.zeros((self.cols, self.rows), dtype=np.float64)
        for marker in self._markers:
            age = current_time - marker["created_time"]
            if age < 0:
                continue
            mc, mr = marker["position"]
            for c in range(self.cols):
                for r in range(self.rows):
                    dist = math.sqrt((c - mc) ** 2 + (r - mr) ** 2)
                    gauss = math.exp(-0.5 * (dist / gc.marker_sigma_cells) ** 2)
                    A[c, r] = max(A[c, r], gauss * math.exp(-age / gc.marker_decay_half_life_min))
        return A

    def get_value_matrix(self, current_time: float) -> np.ndarray:
        gc = self.config.grid
        alpha, beta, gamma = gc.value_alpha, gc.value_beta, gc.value_gamma
        info_gap = 1.0 - self.info
        S = self._strategic_field(current_time)
        A = self._timeliness_field(current_time)
        V = alpha * info_gap + beta * S + gamma * A
        return np.clip(V, 0.0, 1.0)

    def get_info_matrix(self) -> np.ndarray:
        return self.info.copy()

    def get_avg_info_in_bbox(self, bbox: BBox) -> float:
        c0, r0, c1, r1 = bbox
        patch = self.info[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size > 0 else 0.0

    def get_avg_value_in_bbox(self, bbox: BBox, current_time: float) -> float:
        c0, r0, c1, r1 = bbox
        V = self.get_value_matrix(current_time)
        patch = V[c0:c1, r0:r1]
        return float(np.mean(patch)) if patch.size > 0 else 0.0

    def classify_cell(self, info: float) -> str:
        gc = self.config.grid
        if info > gc.white_threshold:
            return "white"
        elif info > gc.gray_threshold:
            return "gray"
        else:
            return "black"
