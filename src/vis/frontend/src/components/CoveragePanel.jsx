import { Radar } from "lucide-react";
import { useEffect, useState } from "react";

const WINDOW_OPTIONS = [30, 60, 120];
const COVERAGE_SCHEMA = "persistent-coverage/v1";

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
  } else if (windowData.window_complete === false) {
    statusMessages.push("窗口积累中");
  }

  if (frame?.runtime_status === "paused_model") {
    statusMessages.push(`模型暂停 · 指标停留在${simulationTimeText(metrics?.as_of_min)}`);
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

      <div className="coverage-meta">
        <span>{simulationTimeText(metrics?.as_of_min)}</span>
        {supported && windowData && (
          <span>{windowData.window_complete === false ? "窗口积累中" : "窗口已完整"}</span>
        )}
      </div>
      {statusText && <p className="coverage-status" role="status">{statusText}</p>}
    </section>
  );
}
