import { useState } from "react";

const TABS = ["时间线", "区域详情", "LLM日志", "参数"];

export default function BottomDrawer({ frame, visible }) {
  const [activeTab, setActiveTab] = useState(0);

  if (!visible) return null;

  return (
    <div className="bottom-drawer" style={{ height: "35vh" }}>
      <div className="drawer-tabs">
        {TABS.map((t, i) => (
          <div
            key={t}
            className={`drawer-tab ${i === activeTab ? "active" : ""}`}
            onClick={() => setActiveTab(i)}
          >
            {t}
          </div>
        ))}
      </div>
      <div className="drawer-content">
        {TABS[activeTab]} 占位内容
      </div>
    </div>
  );
}
