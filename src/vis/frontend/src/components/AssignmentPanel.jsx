import { Crosshair, LayoutGrid, Plane } from "lucide-react";

const KIND_LABELS = {
  search: "区域覆盖", coverage: "区域覆盖", track: "目标跟踪",
  reacquire: "再捕获", probe: "查证",
};

// 任务分配栏：展示 adapter 下发的 AssignmentView 配对；激活时地图切换为
// assignment 渲染模式（子任务区域标注 UAV ID，跟踪目标方形阴影）。
export default function AssignmentPanel({ assignments, plans }) {
  const committed = plans.filter((plan) => ["approved", "auto_approved"].includes(plan.status));
  return (
    <div className="assignment-panel">
      <section className="sidebar-section">
        <div className="section-heading"><span><LayoutGrid size={15} />当前任务配对</span><small>{assignments.length} ASSIGN</small></div>
        {assignments.length ? (
          <ul className="assignment-list">
            {assignments.map((item) => (
              <li key={`${item.task_id}-${item.uav_id}`}>
                <Plane size={13} />
                <div className="assignment-copy">
                  <strong>{item.uav_id}</strong>
                  <small>{item.label || item.task_id}</small>
                </div>
                <span className="assignment-kind">{KIND_LABELS[item.kind] || item.kind || item.task_id}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="editor-note">当前没有已提交的任务配对；配对视图由适配层在 validate_selection 通过后下发。</p>
        )}
      </section>
      {committed.length > 0 && (
        <section className="sidebar-section">
          <div className="section-heading"><span><Crosshair size={15} />已生效计划</span><small>{committed.length}</small></div>
          <ul className="assignment-list">
            {committed.slice(-8).map((plan) => (
              <li key={plan.plan_id}>
                <div className="assignment-copy">
                  <strong>{plan.status === "auto_approved" ? "自动执行" : "人工批准"}</strong>
                  <small>{plan.decision_reason || (plan.summary?.selected_task_ids || []).join("、") || plan.plan_id}</small>
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
