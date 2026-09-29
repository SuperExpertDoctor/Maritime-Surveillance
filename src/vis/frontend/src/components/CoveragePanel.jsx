import { Radar } from "lucide-react";
import { useEffect, useRef, useState } from "react";

const WINDOW_OPTIONS = [30, 60, 120];
const COVERAGE_SCHEMA = "persistent-coverage/v1";
const HEATMAP_CELLS = 30;
const HEATMAP_CELL_SIZE = 6;

function finiteNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function percentText(value) {
  return finiteNumber(value) ? `${value.toFixed(2)}%` : "—";
}

function areaText(value) {
  return finiteNumber(value)
    ? value.toLocaleString("zh-CN", { maximumFractionDigits: 0 })
    : "—";
}

function boundedPercent(value) {
  return Math.max(0, Math.min(100, value));
}

function simulationTimeText(value) {
  return finiteNumber(value) ? `仿真 ${value.toFixed(2)} min` : "仿真时刻 —";
}

function connectionMessage(connectionStatus) {
  if (connectionStatus === "error" || connectionStatus === "reconnecting") {
    return "连接中断，非实时";
  }
  if (connectionStatus === "connecting") return "连接中，等待最新帧";
  return null;
}

function normalizedInformation(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? Math.max(0, Math.min(1, numeric)) : 0;
}

function InformationHeatmap({ info }) {
  const canvasRef = useRef(null);
  const [hovered, setHovered] = useState(null);
  const hasInfo = Array.isArray(info) && info.length === HEATMAP_CELLS
    && info.every((column) => Array.isArray(column) && column.length === HEATMAP_CELLS);

  useEffect(() => {
    if (!hasInfo) return;
    const ctx = canvasRef.current?.getContext("2d");
    if (!ctx) return;
    ctx.fillStyle = "#cbdbd7";
    ctx.fillRect(0, 0, HEATMAP_CELLS * HEATMAP_CELL_SIZE, HEATMAP_CELLS * HEATMAP_CELL_SIZE);
    for (let col = 0; col < HEATMAP_CELLS; col += 1) {
      for (let row = 0; row < HEATMAP_CELLS; row += 1) {
        const value = normalizedInformation(info[col][row]);
        ctx.fillStyle = `rgb(${Math.round(232 - 210 * value)}, ${Math.round(240 - 100 * value)}, ${Math.round(237 - 130 * value)})`;
        ctx.fillRect(col * HEATMAP_CELL_SIZE + 1, row * HEATMAP_CELL_SIZE + 1,
          HEATMAP_CELL_SIZE - 1, HEATMAP_CELL_SIZE - 1);
      }
    }
  }, [hasInfo, info]);

  const handlePointerMove = (event) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const col = Math.min(HEATMAP_CELLS - 1,
      Math.max(0, Math.floor((event.clientX - bounds.left) / bounds.width * HEATMAP_CELLS)));
    const row = Math.min(HEATMAP_CELLS - 1,
      Math.max(0, Math.floor((event.clientY - bounds.top) / bounds.height * HEATMAP_CELLS)));
    setHovered({ col, row });
  };

  return (
    <div className="information-heatmap-section">
      <div className="information-heatmap-heading">
        <strong>信息量可视化</strong>
        <small>{hovered && hasInfo
          ? `${hovered.col + 1}, ${hovered.row + 1} · ${normalizedInformation(info[hovered.col][hovered.row]).toFixed(2)}`
          : "30 × 30"}</small>
      </div>
      {hasInfo ? (
        <canvas
          ref={canvasRef}
          data-testid="information-heatmap"
          width={HEATMAP_CELLS * HEATMAP_CELL_SIZE}
          height={HEATMAP_CELLS * HEATMAP_CELL_SIZE}
          role="img"
          aria-label="信息量热力图，30乘30格，信息量归一化范围为0到1"
          onPointerMove={handlePointerMove}
          onPointerLeave={() => setHovered(null)}
        />
      ) : <p className="information-heatmap-empty">暂无信息量数据</p>}
      <div className="information-heatmap-legend" aria-label="信息量色阶，从0到1">
        <span>0</span><i /><span>1</span>
      </div>
    </div>
  );
}

export default function CoveragePanel({ frame, connectionStatus = "connected", readOnly = false }) {
  const [windowMin, setWindowMin] = useState(60);

  useEffect(() => setWindowMin(60), [frame?.episode_id]);

  const metrics = frame?.coverage_metrics;
  const supported = metrics?.schema_version === COVERAGE_SCHEMA;
  const windowData = supported && Array.isArray(metrics.windows)
    ? metrics.windows.find((item) => item?.minutes === windowMin)
    : null;
  const noSearchableArea = supported && metrics?.status === "no_searchable_area";
  const percent = windowData?.coverage_pct;
  const cumulative = metrics?.cumulative_pct;
  const unseen = metrics?.unseen_pct;
  const overdue = finiteNumber(cumulative) && finiteNumber(percent)
    ? cumulative - percent
    : null;
  const hasCoverage = supported && !noSearchableArea && windowData && finiteNumber(percent);
  const statusMessages = [];

  if (!metrics) {
    statusMessages.push(readOnly ? "该回放未记录持续覆盖指标" : "等待覆盖指标");
  } else if (!supported) {
    statusMessages.push("不支持的指标版本");
  } else if (noSearchableArea) {
    statusMessages.push("无可搜索海域");
  } else if (!windowData) {
    statusMessages.push("该帧缺少持续覆盖窗口数据");
  }

  if (frame?.runtime_status === "paused_model") {
    statusMessages.push(`模型暂停 · 指标停留在${simulationTimeText(metrics?.as_of_min)}`);
  }
  if (frame?.runtime_status === "paused_safety") {
    statusMessages.push(`任务状态异常暂停 · 指标停留在${simulationTimeText(metrics?.as_of_min)}`);
  }
  if (readOnly) {
    statusMessages.push("回放数据");
  } else {
    const disconnected = connectionMessage(connectionStatus);
    if (disconnected) statusMessages.push(disconnected);
  }

  const statusText = statusMessages.filter(Boolean).join(" · ");
  const fixedArea = metrics?.fixed_searchable_area_km2;
  const progressValue = finiteNumber(percent) ? boundedPercent(percent) : null;

  return (
    <section
      className="sidebar-section coverage-panel"
      role="region"
      aria-label="持续搜索覆盖"
      title="窗口内至少一次有效 SAR 扫描；重复不重复计面积；不代表每格持续凝视"
    >
      <div className="section-heading coverage-heading">
        <span><Radar size={15} />持续搜索覆盖</span>
        <small>SAR / FIXED DOMAIN</small>
      </div>

      <div className="coverage-window-switcher" role="group" aria-label="覆盖时间窗口">
        {WINDOW_OPTIONS.map((option) => (
          <button
            type="button"
            key={option}
            className={windowMin === option ? "active" : ""}
            aria-label={`最近 ${option} 分钟`}
            aria-pressed={windowMin === option}
            onClick={() => setWindowMin(option)}
          >
            {option}
          </button>
        ))}
      </div>

      <div className="coverage-primary">
        <div className="coverage-primary-row">
          <span>最近 {windowMin} 分钟</span>
          <strong data-testid="coverage-primary-value">{percentText(percent)}</strong>
        </div>
        {finiteNumber(percent) && (
          <div
            className="coverage-progress"
            role="progressbar"
            aria-label={`最近 ${windowMin} 分钟持续搜索覆盖率`}
            aria-valuemin="0"
            aria-valuemax="100"
            aria-valuenow={progressValue}
          >
            <i style={{ width: `${progressValue}%` }} />
          </div>
        )}
      </div>

      <div className="coverage-area-row">
        <span>已搜索面积</span>
        <strong>{hasCoverage ? `${areaText(windowData.covered_area_km2)} / ${areaText(fixedArea)} km²` : "—"}</strong>
      </div>

      <dl className="coverage-stat-grid">
        <div>
          <dt>从未搜索</dt>
          <dd>{hasCoverage ? percentText(unseen) : "—"}</dd>
        </div>
        <div>
          <dt>超时未重访</dt>
          <dd>{hasCoverage ? percentText(overdue) : "—"}</dd>
        </div>
      </dl>

      <InformationHeatmap info={frame?.info_matrix} />
      {statusText && <p className="coverage-status" role="status">{statusText}</p>}
    </section>
  );
}
