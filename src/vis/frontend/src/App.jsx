import { useEffect, useMemo, useState } from "react";
import { Grid3X3, History, PanelBottom, PanelRight, Radio } from "lucide-react";

import BottomDrawer from "./components/BottomDrawer";
import CanvasMap from "./components/CanvasMap";
import PlaybackBar from "./components/PlaybackBar";
import RightSidebar from "./components/RightSidebar";
import useReplay from "./hooks/useReplay";
import useWebSocket from "./hooks/useWebSocket";


export default function App() {
  const [mode, setMode] = useState("live");
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showGrid, setShowGrid] = useState(true);
  const [liveEvents, setLiveEvents] = useState([]);
  const [lastLlmCycle, setLastLlmCycle] = useState(null);
  const live = useWebSocket(mode === "live");
  const replay = useReplay(mode === "replay");
  const frame = mode === "live" ? live.frame : replay.frame;

  useEffect(() => {
    if (mode !== "live" || !live.frame) return;
    setLiveEvents((current) => {
      const incoming = live.frame.events || [];
      const keys = new Set(current.map((event) => `${event.time}|${event.type}|${JSON.stringify(event.data)}`));
      const merged = [...current];
      for (const event of incoming) {
        const key = `${event.time}|${event.type}|${JSON.stringify(event.data)}`;
        if (!keys.has(key)) merged.push(event);
      }
      return merged.slice(-300);
    });
    if (live.frame.llm_cycle) setLastLlmCycle(live.frame.llm_cycle);
  }, [live.frame, mode]);

  const replayEvents = useMemo(() => {
    if (mode !== "replay") return [];
    const unique = new Map();
    replay.frames.slice(0, replay.index + 1).forEach((item) => {
      (item.events || []).forEach((event) => {
        const key = `${event.time}|${event.type}|${JSON.stringify(event.data)}`;
        unique.set(key, event);
      });
    });
    return [...unique.values()];
  }, [mode, replay.frames, replay.index]);

  const replayLlmCycle = useMemo(() => {
    if (mode !== "replay") return null;
    for (let index = replay.index; index >= 0; index -= 1) {
      if (replay.frames[index]?.llm_cycle) return replay.frames[index].llm_cycle;
    }
    return null;
  }, [mode, replay.frames, replay.index]);
  const displayedLlmCycle = mode === "replay" ? replayLlmCycle : lastLlmCycle;

  useEffect(() => {
    if (selectedUavId && frame && !(frame.uavs || []).some((uav) => uav.id === selectedUavId)) {
      setSelectedUavId(null);
    }
  }, [frame, selectedUavId]);

  const connectionLabel = {
    idle: "待机",
    connecting: "连接中",
    connected: "实时连接",
    reconnecting: "正在重连",
    error: "数据错误",
  }[live.status] || live.status;

  return (
    <main className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      <header className="top-bar">
        <div className="product-mark" aria-label="UAV 海上侦察任务控制台">
          <span className="mark-index">MC</span>
          <span>海上侦察任务控制台</span>
        </div>
        <div className="mode-switch" aria-label="数据模式">
          <button className={mode === "live" ? "active" : ""} onClick={() => setMode("live")}>
            <Radio size={15} />直播
          </button>
          <button className={mode === "replay" ? "active" : ""} onClick={() => setMode("replay")}>
            <History size={15} />回放
          </button>
        </div>
        {mode === "replay" && (
          <select
            className="file-select"
            value={replay.selectedFile}
            onChange={(event) => replay.load(event.target.value)}
            aria-label="选择回放文件"
          >
            <option value="">选择任务记录</option>
            {replay.files.map((file) => <option key={file} value={file}>{file}</option>)}
          </select>
        )}
        <span className={`connection-state ${mode === "live" ? live.status : replay.loading ? "connecting" : "connected"}`}>
          <span className="connection-dot" />
          {mode === "live" ? connectionLabel : replay.error || (replay.loading ? "载入中" : `${replay.frames.length} 帧`)}
        </span>
        <div className="top-actions">
          <button className={showGrid ? "icon-btn active" : "icon-btn"} onClick={() => setShowGrid((value) => !value)} title="网格" aria-label="切换网格">
            <Grid3X3 size={17} />
          </button>
          <button className={drawerVisible ? "icon-btn active" : "icon-btn"} onClick={() => setDrawerVisible((value) => !value)} title="任务详情" aria-label="切换任务详情面板" aria-pressed={drawerVisible}>
            <PanelBottom size={17} />
          </button>
          <button className="icon-btn mobile-only" onClick={() => setSidebarOpen((value) => !value)} title="编队状态" aria-label="切换编队状态面板">
            <PanelRight size={17} />
          </button>
        </div>
      </header>

      <CanvasMap
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
        showGrid={showGrid}
      />
      <RightSidebar
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        lastLlmCycle={displayedLlmCycle}
      />
      <BottomDrawer
        frame={frame}
        events={mode === "live" ? liveEvents : replayEvents}
        llmCycle={displayedLlmCycle}
        visible={drawerVisible}
        onToggle={() => setDrawerVisible((value) => !value)}
      />
      <PlaybackBar
        visible={mode === "replay"}
        isPlaying={replay.isPlaying}
        onPlayPause={() => replay.setIsPlaying((value) => !value)}
        frameIndex={replay.index}
        totalFrames={replay.frames.length}
        onSeek={replay.seek}
        playSpeed={replay.speed}
        onSpeedChange={replay.setSpeed}
        frame={frame}
        markers={replay.markers}
      />
    </main>
  );
}
