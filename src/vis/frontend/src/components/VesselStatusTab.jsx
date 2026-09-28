import { Trash2 } from "lucide-react";

const number = (value, digits = 1) => value != null && value !== "" && Number.isFinite(Number(value))
  ? Number(value).toFixed(digits) : "-";

export default function VesselStatusTab({ vessels = [], mode, editingAllowed, commandBusy,
  commandStatus, onSetAis, onDelete }) {
  return (
    <div className="vessel-table-wrap">
      <table className="vessel-status-table" aria-label="船舶状态">
        <thead><tr>
          <th>船舶</th><th>类型</th><th>AIS</th><th>运行机动参数</th>
          <th>调整原因</th><th>当前位置</th><th>操作</th>
        </tr></thead>
        <tbody>{vessels.map((vessel) => {
          const id = vessel.scenario_entity_id;
          const typeII = vessel.vessel_class === "type_ii";
          const locked = mode !== "live" || !editingAllowed || commandBusy;
          const rowStatus = commandStatus?.requestedVesselId === id ? commandStatus : null;
          const params = vessel.motion_parameters;
          return (
            <tr key={id}>
              <td className="vessel-id">{id}</td>
              <td>{typeII ? "II 类" : "I 类"}</td>
              <td><label className="vessel-ais-cell">
                <input type="checkbox" role="switch" aria-label={`${id} AIS`}
                  checked={typeII ? Boolean(vessel.ais_enabled) : true}
                  disabled={!typeII || !vessel.ais_controllable || locked}
                  onChange={(event) => onSetAis?.(id, event.target.checked)} />
                <span>{vessel.ais_enabled || !typeII ? "开启" : "关闭"}{!typeII && " · 固定"}</span>
              </label></td>
              <td className="vessel-motion">
                <span>{number(vessel.speed_kn)} kn · {number(vessel.heading_deg, 0)}°</span>
                {typeII && <small>{params
                  ? `偏航 ${number(params.heading_offset_deg, 0)}° · 折返 ${number(params.zigzag_heading_deg, 0)}° / ${number(params.zigzag_period_min)} min · 相位 ${number(params.phase_deg, 0)}°`
                  : "无当前机动指令"}</small>}
              </td>
              <td className="vessel-reason">{typeII && params
                ? vessel.motion_reason_content
                  ? <details><summary aria-label={`展开 ${id} 调整原因`}>{vessel.motion_reason_content}</summary>
                    <p>{vessel.motion_reason_content}</p></details>
                  : "模型未返回调整原因"
                : "—"}</td>
              <td className="vessel-position">{Array.isArray(vessel.position)
                ? vessel.position.map((coordinate) => number(coordinate)).join(", ") : "-"}</td>
              <td className="vessel-actions">
                <button type="button" className="icon-btn" title={`删除 ${id}`}
                  aria-label={`删除 ${id}`} disabled={locked} onClick={() => onDelete?.(id)}>
                  <Trash2 size={15} />
                </button>
                {rowStatus && <small className={`vessel-row-feedback ${rowStatus.status}`} role="status">
                  {rowStatus.message}
                  {rowStatus.errorCode && ` · ${rowStatus.errorCode}`}
                </small>}
              </td>
            </tr>
          );
        })}</tbody>
      </table>
      {commandStatus?.requestedVesselId
        && !vessels.some((vessel) => vessel.scenario_entity_id === commandStatus.requestedVesselId)
        && <p className="vessel-row-feedback" role="status">{commandStatus.message}</p>}
      {!vessels.length && <p className="vessel-empty">当前无船舶</p>}
    </div>
  );
}
