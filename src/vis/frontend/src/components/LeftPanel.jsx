import { CircleX } from "lucide-react";
import AssignmentPanel from "./AssignmentPanel";
import ConversationPanel from "./ConversationPanel";
import FleetPanel from "./FleetPanel";

const TITLES = {
  data: { eyebrow: "MISSION STATE", title: "编队态势" },
  agent: { eyebrow: "PI AGENT", title: "代理对话" },
  assignment: { eyebrow: "ASSIGNMENT", title: "任务分配" },
};

// 左侧折叠面板宿主：数据栏 / 对话栏 / 任务分配栏，由 ActivityBar 切换。
export default function LeftPanel({ panel, onClose, fleet, agent, onPreviewPlan }) {
  if (!panel) return null;
  const title = TITLES[panel] || TITLES.data;
  return (
    <aside className="left-panel open" aria-label={title.title}>
      <div className="sidebar-header">
        <div><span className="eyebrow">{title.eyebrow}</span><strong>{title.title}</strong></div>
        <button className="icon-btn" onClick={onClose} aria-label="关闭面板" title="关闭"><CircleX size={17} /></button>
      </div>
      <div className="left-panel-body">
        {panel === "data" && <FleetPanel {...fleet} />}
        {panel === "agent" && (
          <ConversationPanel
            agent={agent}
            readOnly={fleet.readOnly}
            onPreviewPlan={onPreviewPlan}
          />
        )}
        {panel === "assignment" && (
          <AssignmentPanel assignments={agent.assignments} plans={agent.plans} />
        )}
      </div>
    </aside>
  );
}
