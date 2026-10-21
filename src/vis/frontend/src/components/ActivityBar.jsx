import { Bot, LayoutDashboard, SplitSquareHorizontal } from "lucide-react";

// 固定最左侧图标栏（VSCode ActivityBar 风格）：数据栏 / 对话栏 / 任务分配栏。
export default function ActivityBar({ activePanel, onSelect }) {
  const items = [
    { id: "data", label: "数据栏", icon: <LayoutDashboard size={18} /> },
    { id: "agent", label: "对话栏", icon: <Bot size={18} /> },
    { id: "assignment", label: "任务分配栏", icon: <SplitSquareHorizontal size={18} /> },
  ];
  return (
    <nav className="activity-bar" aria-label="功能栏">
      {items.map((item) => (
        <button
          key={item.id}
          type="button"
          className={`activity-item ${activePanel === item.id ? "active" : ""}`}
          onClick={() => onSelect?.(activePanel === item.id ? null : item.id)}
          aria-pressed={activePanel === item.id}
          aria-label={item.label}
          title={item.label}
        >
          {item.icon}
          <span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}
