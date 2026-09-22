import { useEffect, useMemo, useRef, useState } from "react";
import { Eye, EyeOff, Focus, Grid3X3, History, PanelBottom, PanelRight, Radio, Route, Wind } from "lucide-react";

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
  const [showScenario, setShowScenario] = useState(false);
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
  const commandContext = useRef(null);
  const activeCommand = useRef(null);
  const commandAbort = useRef(null);
  const live = useWebSocket(mode === "live");
  const replay = useReplay(mode === "replay");
  const mp4Export = useMp4Export(replay, mapExporterRef);
  const frame = mode === "live" ? live.frame : replay.frame;
  const readOnly = mode === "replay";
  const editingAllowed = mode === "live" && live.status === "connected" && Boolean(frame?.episode_id && frame?.vessel_mutation_allowed);
  const contextKey = `${mode}|${frame?.episode_id || ""}`;
  commandContext.current = contextKey;
  const waitingForFrame = vesselCommandStatus?.status === "applied"
    && vesselCommandStatus.vesselId && vesselCommandStatus.revision != null
    && !(frame?.scenario_vessels || []).some((vessel) =>
      vessel.scenario_entity_id === vesselCommandStatus.vesselId
      && vessel.revision >= vesselCommandStatus.revision);
  const vesselCommandBusy = ["queued", "unknown"].includes(vesselCommandStatus?.status) || Boolean(waitingForFrame);

  useEffect(() => {
    setSelectionMode(false);
    setSelectedBBox(null);
    setSelectedContactId(null);
    setVesselPlacement(null);
    setSelectedScenarioVesselId(null);
    setVesselCommandStatus(null);
    activeCommand.current = null;
    return () => {
      commandAbort.current?.abort();
      activeCommand.current = null;
    };
  }, [contextKey]);

  useEffect(() => {
    if (vesselCommandStatus?.vesselId && !waitingForFrame) {
      setVesselCommandStatus((current) => current?.vesselId ? { ...current, vesselId: null } : current);
    }
  }, [waitingForFrame, vesselCommandStatus?.vesselId]);

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
    return replay.markers.map((marker) => marker.event);
  }, [mode, replay.markers]);

  const replayLlmCycle = useMemo(() => {
    if (mode !== "replay") return null;
    return replay.frame?.llm_cycle || null;
  }, [mode, replay.frame]);
  const displayedLlmCycle = mode === "replay" ? replayLlmCycle : lastLlmCycle;

  const replayConnectionStatus = replay.targetLoadingIndex != null || replay.loading
    ? "connecting"
    : replay.error ? "error" : "connected";
  const replayConnectionLabel = replay.targetLoadingIndex != null
    ? "载入目标帧"
    : replay.error || (replay.loading ? "载入中" : `${replay.frames.length} 帧`);

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

  const markCommandUnknown = (id, error) => {
    setVesselCommandStatus({
      status: "unknown", commandId: id,
      message: "船舶命令结果未知，正在重新查询，请勿重复提交",
      errorCode: error.message,
    });
  };

  const pollVesselCommand = async (id, isCurrent, signal) => {
    while (isCurrent()) {
      try {
        const response = await fetch(`/api/vessel-commands/${encodeURIComponent(id)}`, { signal });
        if (!response.ok) throw new Error(`command_${response.status}`);
        const result = await response.json();
        if (!isCurrent()) return null;
        if (["applied", "rejected"].includes(result.status)) return result;
        if (result.status !== "queued") throw new Error("invalid_command_status");
        setVesselCommandStatus((current) => current?.status === "queued" ? current : {
          status: "queued", message: "船舶命令排队中", commandId: id,
        });
      } catch (error) {
        if (!isCurrent()) return null;
        markCommandUnknown(id, error);
      }
      // LLM steps can take tens of seconds; queued is not a failed command.
      await new Promise((resolve) => {
        const finish = () => {
          window.clearTimeout(timer);
          signal.removeEventListener("abort", finish);
          resolve();
        };
        const timer = window.setTimeout(finish, 500);
        signal.addEventListener("abort", finish, { once: true });
        if (signal.aborted) finish();
      });
    }
    return null;
  };

  const submitVesselCommand = async ({ url, method, body }, successMessage, onApplied) => {
    if (activeCommand.current) return;
    const id = body.command_id;
    const context = commandContext.current;
    const controller = new AbortController();
    commandAbort.current = controller;
    activeCommand.current = id;
    const isCurrent = () => !controller.signal.aborted
      && commandContext.current === context && activeCommand.current === id;
    setVesselCommandStatus({ status: "queued", message: "船舶命令排队中", commandId: id });
    try {
      let result;
      try {
        const response = await fetch(url, {
          method,
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        result = await response.json();
        if (!isCurrent()) return;
        if (!response.ok) {
          setVesselCommandStatus({
            status: "rejected", message: "船舶命令被拒绝", commandId: id,
            errorCode: result.error_code || `command_${response.status}`,
          });
          return;
        }
      } catch (error) {
        if (!isCurrent()) return;
        // A lost POST response does not establish whether the server accepted it.
        markCommandUnknown(id, error);
      }
      if (!isCurrent()) return;
      const applied = ["applied", "rejected"].includes(result?.status)
        ? result : await pollVesselCommand(result?.command_id || id, isCurrent, controller.signal);
      if (!isCurrent() || !applied) return;
      setVesselCommandStatus({
        status: applied.status,
        message: applied.status === "applied" ? successMessage : "船舶命令被拒绝",
        commandId: applied.command_id || id,
        errorCode: applied.error_code,
        vesselId: method === "DELETE" ? null : applied.vessel_id,
        revision: applied.revision,
      });
      if (applied.status === "applied") onApplied?.(applied);
    } finally {
      if (isCurrent()) activeCommand.current = null;
    }
  };

  const handlePlaceVessel = async (position, selectedType = vesselPlacement) => {
    if (!editingAllowed || vesselCommandBusy || !selectedType || !frame?.episode_id) return;
    const id = commandId();
    await submitVesselCommand({
      url: "/api/vessels",
      method: "POST",
      body: {
        episode_id: frame.episode_id,
        command_id: id,
        vessel_class: selectedType,
        position_cells: position,
      },
    }, "船舶已加入场景", () => setVesselPlacement(null));
  };

  const handleDeleteVessel = async () => {
    if (!editingAllowed || vesselCommandBusy || !selectedScenarioVesselId || !frame?.episode_id) return;
    const vessel = (frame.scenario_vessels || []).find(
      (item) => item.scenario_entity_id === selectedScenarioVesselId,
    );
    if (!vessel) return;
    const id = commandId();
    await submitVesselCommand({
      url: `/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}`,
      method: "DELETE",
      body: {
        episode_id: frame.episode_id,
        command_id: id,
        expected_revision: vessel.revision,
      },
    }, "船舶已删除", () => setSelectedScenarioVesselId(null));
  };

  const handleSetVesselAis = async (enabled) => {
    if (!editingAllowed || vesselCommandBusy || !selectedScenarioVesselId || !frame?.episode_id) return;
    const vessel = (frame.scenario_vessels || []).find(
      (item) => item.scenario_entity_id === selectedScenarioVesselId,
    );
    if (!vessel?.ais_controllable || vessel.ais_enabled === enabled) return;
    const id = commandId();
    await submitVesselCommand({
      url: `/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}/ais`,
      method: "PATCH",
      body: {
        episode_id: frame.episode_id,
        command_id: id,
        expected_revision: vessel.revision,
        ais_enabled: enabled,
      },
    }, enabled ? "AIS 已开启" : "AIS 已关闭");
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
        <span className={`connection-state ${mode === "live" ? live.status : replayConnectionStatus}`}>
          <span className="connection-dot" />
          {mode === "live" ? connectionLabel : replayConnectionLabel}
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
          <button
            className={showScenario ? "icon-btn active" : "icon-btn"}
            onClick={() => setShowScenario((value) => !value)}
            title={showScenario ? "隐藏场景真值" : "显示场景真值"}
            aria-label="切换场景真值图层"
            aria-pressed={showScenario}
          >
            {showScenario ? <Eye size={17} /> : <EyeOff size={17} />}
          </button>
          <button className={drawerVisible ? "icon-btn active" : "icon-btn"} onClick={() => setDrawerVisible((value) => !value)} title="任务详情" aria-label="切换任务详情面板" aria-pressed={drawerVisible}>
            <PanelBottom size={17} />
          </button>
          <button
            className={selectionMode ? "icon-btn active" : "icon-btn"}
            onClick={() => { setVesselPlacement(null); setSelectionMode((value) => !value); }}
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
        showScenario={showScenario || Boolean(vesselPlacement)}
        trailMode={trailMode}
        selectionMode={selectionMode}
        onSelectionCommit={(bbox) => { setSelectedBBox(bbox); setSidebarOpen(true); }}
        onSelectContact={setSelectedContactId}
        selectedContactId={selectedContactId}
        editingAllowed={editingAllowed && !vesselCommandBusy}
        placementMode={Boolean(vesselPlacement && editingAllowed && !vesselCommandBusy)}
        onPlaceVessel={handlePlaceVessel}
        onDropVessel={(vesselClass, position) => {
          if (editingAllowed && !vesselCommandBusy) {
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
        connectionStatus={mode === "live" ? live.status : replayConnectionStatus}
        selection={selectedBBox}
        onClearSelection={() => setSelectedBBox(null)}
        selectedContactId={selectedContactId}
        onSelectContact={setSelectedContactId}
        editingAllowed={editingAllowed}
        vesselPlacement={vesselPlacement}
        onSelectVesselType={(vesselClass) => { setSelectionMode(false); setVesselPlacement(vesselClass); }}
        onCancelVesselPlacement={() => setVesselPlacement(null)}
        selectedScenarioVesselId={selectedScenarioVesselId}
        onSelectScenarioVessel={setSelectedScenarioVesselId}
        onDeleteVessel={handleDeleteVessel}
        onSetVesselAis={handleSetVesselAis}
        vesselCommandStatus={vesselCommandStatus}
        vesselCommandBusy={vesselCommandBusy}
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
        loadedFrames={replay.loadedFrameCount}
        targetLoadingIndex={replay.targetLoadingIndex}
        onExportMp4={mp4Export.exportMp4}
        exportAvailable={mp4Export.available}
        exporting={mp4Export.exporting}
        exportProgress={mp4Export.progress}
        exportError={mp4Export.error}
      />
    </main>
  );
}
