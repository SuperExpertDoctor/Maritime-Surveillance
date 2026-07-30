import { UAV_STATUS_COLORS } from "../renderer/colors";

export default function RightSidebar({ frame, onSelectUav, selectedUavId }) {
  if (!frame) {
    return (
      <div className="sidebar">
        <div className="sidebar-empty">等待数据...</div>
      </div>
    );
  }

  const uavs = frame.uavs || [];
  const searchRegions = frame.search_regions || [];
  const trackRegions = frame.track_regions || [];
  const ships = frame.ships || [];
  const markers = frame.markers || [];
  const detectedShips = ships.filter((s) => s.is_detected);

  // 计算覆盖率
  const infoMat = frame.info_matrix;
  let coverage = 0;
  if (infoMat) {
    let total = 0, scanned = 0;
    for (const row of infoMat) {
      for (const v of row) {
        total++;
        if (v > 0) scanned++;
      }
    }
    coverage = total > 0 ? (scanned / total * 100) : 0;
  }

  return (
    <div className="sidebar">
      {/* ── 仿真概览 ── */}
      <div className="sidebar-section">
        <div className="section-title">仿真概览</div>
        <div className="stat-row">
          <span className="stat-label">时间</span>
          <span className="stat-value">{frame.timestamp || "--:--:--"}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">周期</span>
          <span className="stat-value">{frame.cycle ?? "-"}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">帧 #</span>
          <span className="stat-value">{frame.frame_id ?? "-"}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">覆盖率</span>
          <span className="stat-value">{coverage.toFixed(1)}%</span>
        </div>
      </div>

      {/* ── UAV 状态 ── */}
      <div className="sidebar-section">
        <div className="section-title">
          UAV ({uavs.length})
        </div>
        <div className="uav-list">
          {uavs.map((u) => {
            const color = UAV_STATUS_COLORS[u.status] || "#9CA3AF";
            const isSel = u.id === selectedUavId;
            return (
              <div
                key={u.id}
                className={`uav-row ${isSel ? "selected" : ""}`}
                onClick={() => onSelectUav?.(isSel ? null : u.id)}
              >
                <span className="uav-dot" style={{ background: color }} />
                <span className="uav-id">{u.id.replace("UAV-", "U-")}</span>
                <span className="uav-status" style={{ color }}>{u.status}</span>
                <span className="uav-fuel">
                  {u.remaining_range_km != null
                    ? `${Math.round(u.remaining_range_km)}km`
                    : "-"}
                </span>
              </div>
            );
          })}
        </div>
      </div>

      {/* ── 统计 ── */}
      <div className="sidebar-section">
        <div className="section-title">统计</div>
        <div className="stat-row">
          <span className="stat-label">搜索区域</span>
          <span className="stat-value">{searchRegions.length}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">跟踪区域</span>
          <span className="stat-value">{trackRegions.length}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">船舶</span>
          <span className="stat-value">{detectedShips.length}/{ships.length}</span>
        </div>
        <div className="stat-row">
          <span className="stat-label">标记点</span>
          <span className="stat-value">{markers.length}</span>
        </div>
      </div>
    </div>
  );
}
