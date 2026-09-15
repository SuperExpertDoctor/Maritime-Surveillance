import { useEffect, useMemo, useRef, useState } from "react";
import { Focus, Grid3X3, History, PanelBottom, PanelRight, Radio, Route, Wind } from "lucide-react";

import BottomDrawer from "./components/BottomDrawer";
import CanvasMap from "./components/CanvasMap";
import PlaybackBar from "./components/PlaybackBar";
import RightSidebar from "./components/RightSidebar";
import useReplay from "./hooks/useReplay";
import useMp4Export from "./hooks/useMp4Export";
import useWebSocket from "./hooks/useWebSocket";


export default function App() {
  const [mode, setMode] = useState("live");
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [showGrid, setShowGrid] = useState(true);
  const [trailMode, setTrailMode] = useState("tail");
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedBBox, setSelectedBBox] = useState(null);
  const [selectedContactId, setSelectedContactId] = useState(null);
  const [vesselPlacement, setVesselPlacement] = useState(null);
  const [selectedScenarioVesselId, setSelectedScenarioVesselId] = useState(null);
  const [vesselCommandStatus, setVesselCommandStatus] = useState(null);
  const [liveEvents, setLiveEvents] = useState([]);
  const [lastLlmCycle, setLastLlmCycle] = useState(null);
  const mapExporterRef = useRef(null);
  const live = useWebSocket(mode === "live");
  const replay = useReplay(mode === "replay");
  const mp4Export = useMp4Export(replay, mapExporterRef);
  const frame = mode === "live" ? live.frame : replay.frame;
  const readOnly = mode === "replay";
  const editingAllowed = mode === "live" && Boolean(frame?.editing_allowed);

  useEffect(() => {
    setSelectionMode(false);
    setSelectedBBox(null);
    setSelectedContactId(null);
    setVesselPlacement(null);
    setSelectedScenarioVesselId(null);
    setVesselCommandStatus(null);
  }, [mode]);

  useEffect(() => {
    if (!editingAllowed) {
      setVesselPlacement(null);
      setSelectedScenarioVesselId(null);
    }
  }, [editingAllowed]);

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
    if (selectedContactId && frame && !(frame.contacts || []).some((contact) => contact.contact_id === selectedContactId)) {
      setSelectedContactId(null);
    }
  }, [frame, selectedContactId, selectedUavId]);

  const commandId = () => globalThis.crypto?.randomUUID?.()
    || `vessel-${Date.now()}-${Math.random().toString(16).slice(2)}`;

  const pollVesselCommand = async (id) => {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, attempt ? 150 : 0));
      const response = await fetch(`/api/vessel-commands/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error(`command_${response.status}`);
      const result = await response.json();
      if (result.status !== "queued") return result;
    }
    throw new Error("command_timeout");
  };

  const handlePlaceVessel = async (position, selectedType = vesselPlacement) => {
    if (!editingAllowed || !selectedType || !frame?.episode_id) return;
    const id = commandId();
    setVesselCommandStatus({ status: "queued", message: "船舶命令排队中", commandId: id });
    try {
      const response = await fetch("/api/vessels", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          episode_id: frame.episode_id,
          command_id: id,
          vessel_class: selectedType,
          position_cells: position,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error_code || "vessel_create_failed");
      const applied = await pollVesselCommand(result.command_id || id);
      setVesselCommandStatus({
        status: applied.status,
        message: applied.status === "applied" ? "船舶已加入场景" : "船舶命令被拒绝",
        commandId: applied.command_id || id,
        errorCode: applied.error_code,
      });
      setVesselPlacement(null);
    } catch (error) {
      setVesselCommandStatus({ status: "rejected", message: "船舶命令失败", errorCode: error.message });
    }
  };

  const handleDeleteVessel = async () => {
    if (!editingAllowed || !selectedScenarioVesselId || !frame?.episode_id) return;
    const vessel = (frame.scenario_vessels || []).find(
      (item) => item.scenario_entity_id === selectedScenarioVesselId,
    );
    if (!vessel) return;
    const id = commandId();
    setVesselCommandStatus({ status: "queued", message: "删除命令排队中", commandId: id });
    try {
      const response = await fetch(`/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}`, {
        method: "DELETE",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          episode_id: frame.episode_id,
          command_id: id,
          expected_revision: vessel.revision,
        }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error_code || "vessel_delete_failed");
      const applied = await pollVesselCommand(result.command_id || id);
      setVesselCommandStatus({
        status: applied.status,
        message: applied.status === "applied" ? "船舶已删除" : "删除命令被拒绝",
        commandId: applied.command_id || id,
        errorCode: applied.error_code,
      });
      setSelectedScenarioVesselId(null);
    } catch (error) {
      setVesselCommandStatus({ status: "rejected", message: "删除命令失败", errorCode: error.message });
    }
  };

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
          <div className="trail-mode-switch" role="group" aria-label="UAV轨迹显示模式">
            <button
              className={trailMode === "full" ? "active" : ""}
              onClick={() => setTrailMode("full")}
              title="完整 UAV 轨迹"
              aria-label="完整 UAV 轨迹"
              aria-pressed={trailMode === "full"}
            >
              <Route size={16} />
            </button>
            <button
              className={trailMode === "tail" ? "active" : ""}
              onClick={() => setTrailMode("tail")}
              title="渐变长尾 UAV 轨迹"
              aria-label="渐变长尾 UAV 轨迹"
              aria-pressed={trailMode === "tail"}
            >
              <Wind size={16} />
            </button>
            <button
              className={trailMode === "comet" ? "active" : ""}
              onClick={() => setTrailMode("comet")}
              title="彗星拖尾 UAV 轨迹"
              aria-label="彗星拖尾 UAV 轨迹"
              aria-pressed={trailMode === "comet"}
            >
              <span style={{ fontSize: 13, lineHeight: 1 }}>☄</span>
            </button>
          </div>
          <button
            className="trail-mode-compact"
            onClick={() => setTrailMode((value) => {
              if (value === "full") return "tail";
              if (value === "tail") return "comet";
              return "full";
            })}
            title={`UAV 轨迹: ${trailMode === "full" ? "完整" : trailMode === "tail" ? "渐变长尾" : "彗星拖尾"} — 点击切换`}
            aria-label="切换 UAV 轨迹显示模式"
          >
            {trailMode === "full" ? <Route size={17} /> : trailMode === "tail" ? <Wind size={17} /> : <span style={{ fontSize: 14 }}>☄</span>}
          </button>
          <button className={showGrid ? "icon-btn active" : "icon-btn"} onClick={() => setShowGrid((value) => !value)} title="网格" aria-label="切换网格">
            <Grid3X3 size={17} />
          </button>
          <button className={drawerVisible ? "icon-btn active" : "icon-btn"} onClick={() => setDrawerVisible((value) => !value)} title="任务详情" aria-label="切换任务详情面板" aria-pressed={drawerVisible}>
            <PanelBottom size={17} />
          </button>
          <button
            className={selectionMode ? "icon-btn active" : "icon-btn"}
            onClick={() => setSelectionMode((value) => !value)}
            title={readOnly ? "回放只读" : "框选重点区"}
            aria-label="框选重点区"
            aria-pressed={selectionMode}
            disabled={readOnly}
          >
            <Focus size={17} />
          </button>
          <button className="icon-btn mobile-only" onClick={() => setSidebarOpen((value) => !value)} title="编队状态" aria-label="切换编队状态面板">
            <PanelRight size={17} />
          </button>
        </div>
      </header>

      <CanvasMap
        ref={mapExporterRef}
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
        showGrid={showGrid}
        trailMode={trailMode}
        selectionMode={selectionMode}
        onSelectionCommit={(bbox) => { setSelectedBBox(bbox); setSidebarOpen(true); }}
        onSelectContact={setSelectedContactId}
        selectedContactId={selectedContactId}
        placementMode={Boolean(vesselPlacement && editingAllowed)}
        onPlaceVessel={handlePlaceVessel}
        onDropVessel={(vesselClass, position) => {
          if (editingAllowed) {
            setVesselPlacement(vesselClass);
            handlePlaceVessel(position, vesselClass);
          }
        }}
        selectedScenarioVesselId={selectedScenarioVesselId}
        onSelectScenarioVessel={setSelectedScenarioVesselId}
      />
      <RightSidebar
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        lastLlmCycle={displayedLlmCycle}
        readOnly={readOnly}
        selection={selectedBBox}
        onClearSelection={() => setSelectedBBox(null)}
        selectedContactId={selectedContactId}
        onSelectContact={setSelectedContactId}
        editingAllowed={editingAllowed}
        vesselPlacement={vesselPlacement}
        onSelectVesselType={setVesselPlacement}
        onCancelVesselPlacement={() => setVesselPlacement(null)}
        selectedScenarioVesselId={selectedScenarioVesselId}
        onSelectScenarioVessel={setSelectedScenarioVesselId}
        onDeleteVessel={handleDeleteVessel}
        vesselCommandStatus={vesselCommandStatus}
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
        totalFrames={replay.total || replay.frames.length}
        onSeek={replay.seek}
        playSpeed={replay.speed}
        onSpeedChange={replay.setSpeed}
        frame={frame}
        markers={replay.markers}
        onExportMp4={mp4Export.exportMp4}
        exportAvailable={mp4Export.available}
        exporting={mp4Export.exporting}
        exportProgress={mp4Export.progress}
        exportError={mp4Export.error}
      />
    </main>
  );
}
