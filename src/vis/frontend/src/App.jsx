import { useEffect, useMemo, useRef, useState } from "react";
import { Download, Eye, EyeOff, Focus, Grid3X3, History, PanelBottom, PanelRight, Radio, Route, Wind, X } from "lucide-react";

import BottomDrawer from "./components/BottomDrawer";
import CanvasMap from "./components/CanvasMap";
import MenuBar from "./components/MenuBar";
import PlaybackBar from "./components/PlaybackBar";
import RightSidebar from "./components/RightSidebar";
import SettingsDialog from "./components/SettingsDialog";
import useReplay from "./hooks/useReplay";
import useMp4Export from "./hooks/useMp4Export";
import useWebSocket from "./hooks/useWebSocket";
import useRuntimeLogs from "./hooks/useRuntimeLogs";


export default function App() {
  const [mode, setMode] = useState("live");
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(true);
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
  const [lastLlmCycle, setLastLlmCycle] = useState(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [restartTimedOut, setRestartTimedOut] = useState(false);
  const mapExporterRef = useRef(null);
  const commandContext = useRef(null);
  const activeCommand = useRef(null);
  const commandAbort = useRef(null);
  const eventContext = useRef(null);
  const live = useWebSocket(mode === "live");
  const runtime = useRuntimeLogs(mode === "live" ? live.frame?.episode_id : null, live.frame?.reset_generation);
  const replay = useReplay(mode === "replay");
  const mp4Export = useMp4Export(replay, mapExporterRef);
  const frame = mode === "live" ? live.frame : replay.frame;
  const readOnly = mode === "replay";
  const editingAllowed = mode === "live" && live.status === "connected" && Boolean(frame?.episode_id && frame?.vessel_mutation_allowed);
  const contextKey = `${mode}|${frame?.episode_id || ""}|${frame?.reset_generation ?? 0}`;
  commandContext.current = contextKey;
  const confirmedVessel = (frame?.scenario_vessels || []).find((vessel) =>
    vessel.scenario_entity_id === vesselCommandStatus?.vesselId);
  const frameConfirmsCommand = vesselCommandStatus?.context === contextKey
    && Array.isArray(frame?.scenario_vessels)
    && Boolean(vesselCommandStatus?.vesselId)
    && vesselCommandStatus.revision != null
    && (!vesselCommandStatus.requestedVesselId || vesselCommandStatus.requestedVesselId === vesselCommandStatus.vesselId)
    && (vesselCommandStatus.method === "DELETE"
      ? !confirmedVessel
      : confirmedVessel?.revision >= vesselCommandStatus.revision
        && (vesselCommandStatus.method !== "PATCH" || confirmedVessel.ais_enabled === vesselCommandStatus.aisEnabled));
  const waitingForFrame = vesselCommandStatus?.status === "applied"
    && !vesselCommandStatus.confirmed && !frameConfirmsCommand;
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
    if (vesselCommandStatus?.status === "applied" && !vesselCommandStatus.confirmed && frameConfirmsCommand) {
      vesselCommandStatus.onConfirmed?.();
      setVesselCommandStatus((current) => current === vesselCommandStatus ? { ...current, confirmed: true, onConfirmed: null } : current);
    }
  }, [frameConfirmsCommand, vesselCommandStatus]);

  useEffect(() => {
    if (!editingAllowed) {
      setVesselPlacement(null);
      setSelectedScenarioVesselId(null);
    }
  }, [editingAllowed]);

  useEffect(() => {
    if (mode === "replay") setDrawerVisible(true);
  }, [mode]);

  useEffect(() => {
    if (mode !== "live" || !live.frame) return;
    const keyContext = `${live.frame.episode_id}|${live.frame.reset_generation}`;
    const changed = eventContext.current !== keyContext;
    eventContext.current = keyContext;
    if (changed) setLastLlmCycle(null);
    if (live.frame.llm_cycle) setLastLlmCycle(live.frame.llm_cycle);
  }, [live.frame, mode]);

  const decisionEvents = mode === "live" ? runtime.decisions : replay.markers
    .filter((marker) => marker.type === "allocation_decision" && marker.frameIndex <= replay.index)
    .map((marker) => marker.event);
  const runtimeLogs = mode === "live" ? runtime.logs : replay.markers
    .filter((marker) => marker.type === "runtime_log" && marker.frameIndex <= replay.index)
    .map((marker) => ({ ...marker.data, id: marker.key, sim_time_min: marker.time }));

  const replayLlmCycle = useMemo(() => {
    if (mode !== "replay") return null;
    if (!replay.frame) return null;
    for (let i = replay.index; i >= 0; i -= 1) {
      const item = replay.frames[i];
      if (item?.episode_id === replay.frame.episode_id && item?.llm_cycle) return item.llm_cycle;
    }
    return null;
  }, [mode, replay.frame, replay.frames, replay.index]);
  const displayedLlmCycle = mode === "replay" ? replayLlmCycle : frame ? lastLlmCycle : null;

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

  const markCommandUnknown = (id, error, vesselId) => {
    setVesselCommandStatus({
      status: "unknown", commandId: id,
      message: "船舶命令结果未知，正在重新查询，请勿重复提交",
      errorCode: error.message,
      requestedVesselId: vesselId,
    });
  };

  const pollVesselCommand = async (id, isCurrent, signal, vesselId) => {
    while (isCurrent()) {
      try {
        const response = await fetch(`/api/vessel-commands/${encodeURIComponent(id)}`, { signal });
        if (!response.ok) throw new Error(`command_${response.status}`);
        const result = await response.json();
        if (!isCurrent()) return null;
        if (["applied", "rejected"].includes(result.status)) return result;
        if (result.status !== "queued") throw new Error("invalid_command_status");
        setVesselCommandStatus((current) => current?.status === "queued" ? current : {
          status: "queued", message: "船舶命令排队中", commandId: id, requestedVesselId: vesselId,
        });
      } catch (error) {
        if (!isCurrent()) return null;
        markCommandUnknown(id, error, vesselId);
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

  const submitVesselCommand = async ({ url, method, body, vesselId }, successMessage, onConfirmed) => {
    if (activeCommand.current) return;
    const id = body.command_id;
    const context = commandContext.current;
    const controller = new AbortController();
    commandAbort.current = controller;
    activeCommand.current = id;
    const isCurrent = () => !controller.signal.aborted
      && commandContext.current === context && activeCommand.current === id;
    setVesselCommandStatus({ status: "queued", message: "船舶命令排队中", commandId: id, requestedVesselId: vesselId });
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
            requestedVesselId: vesselId,
          });
          return;
        }
      } catch (error) {
        if (!isCurrent()) return;
        // A lost POST response does not establish whether the server accepted it.
        markCommandUnknown(id, error, vesselId);
      }
      if (!isCurrent()) return;
      const applied = ["applied", "rejected"].includes(result?.status)
        ? result : await pollVesselCommand(result?.command_id || id, isCurrent, controller.signal, vesselId);
      if (!isCurrent() || !applied) return;
      setVesselCommandStatus({
        status: applied.status,
        message: applied.status === "applied" ? successMessage : "船舶命令被拒绝",
        commandId: applied.command_id || id,
        errorCode: applied.error_code,
        vesselId: applied.vessel_id,
        revision: applied.revision,
        requestedVesselId: vesselId,
        context,
        method,
        aisEnabled: body.ais_enabled,
        onConfirmed,
        confirmed: false,
      });
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

  const handleDeleteVessel = async (vesselId) => {
    if (!editingAllowed || vesselCommandBusy || !vesselId || !frame?.episode_id) return;
    const vessel = (frame.scenario_vessels || []).find(
      (item) => item.scenario_entity_id === vesselId,
    );
    if (!vessel) return;
    const id = commandId();
    await submitVesselCommand({
      url: `/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}`,
      method: "DELETE",
      vesselId: vessel.scenario_entity_id,
      body: {
        episode_id: frame.episode_id,
        command_id: id,
        expected_revision: vessel.revision,
      },
    }, "船舶已删除", () => setSelectedScenarioVesselId((current) => current === vesselId ? null : current));
  };

  const handleSetVesselAis = async (vesselId, enabled) => {
    if (!editingAllowed || vesselCommandBusy || !vesselId || !frame?.episode_id) return;
    const vessel = (frame.scenario_vessels || []).find(
      (item) => item.scenario_entity_id === vesselId,
    );
    if (!vessel?.ais_controllable || vessel.ais_enabled === enabled) return;
    const id = commandId();
    await submitVesselCommand({
      url: `/api/vessels/${encodeURIComponent(vessel.scenario_entity_id)}/ais`,
      method: "PATCH",
      vesselId: vessel.scenario_entity_id,
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

  // 保存配置 / 点「重启仿真」后：整个后端进程 execv 重启，这里轮询
  // 直到服务恢复再整页刷新，拿到新配置下的全新回合。
  useEffect(() => {
    if (!restarting) return undefined;
    let stopped = false;
    setRestartTimedOut(false);
    const startedAt = Date.now();
    const poll = async () => {
      if (stopped) return;
      try {
        const response = await fetch("/api/settings");
        if (response.ok) {
          window.location.reload();
          return;
        }
      } catch (_) { /* 服务尚未起来 */ }
      if (Date.now() - startedAt > 90000) {
        setRestartTimedOut(true);
        return;
      }
      setTimeout(poll, 1500);
    };
    const timer = setTimeout(poll, 2500);
    return () => { stopped = true; clearTimeout(timer); };
  }, [restarting]);

  const restartSim = async () => {
    if (mode === "replay" || restarting) return;
    setRestarting(true);
    try {
      await fetch("/api/runtime/restart", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (_) { /* 进程重启时连接断开属正常 */ }
  };

  const sendRuntimeCommand = async (operation) => {
    if (!frame?.episode_id) return;
    try {
      await fetch(`/api/runtime/${operation}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          episode_id: frame.episode_id,
          command_id: `${operation}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        }),
      });
    } catch (_) { /* 命令回执会在帧里体现 */ }
  };

  const cycleTrailMode = () => setTrailMode((value) => {
    if (value === "full") return "tail";
    if (value === "tail") return "comet";
    return "full";
  });

  const trailModeLabel = { full: "完整", tail: "渐变长尾", comet: "彗星拖尾" }[trailMode];

  const menus = [
    {
      id: "file", label: "文件(F)", items: [
        { id: "open-settings", label: "算法参数设置…", shortcut: "Ctrl+,", onClick: () => setSettingsOpen(true), disabled: readOnly },
        "sep",
        {
          id: "export-mp4", label: "导出回放 MP4",
          disabled: !(mode === "replay" && replay.selectedFile && mp4Export.available) || mp4Export.exporting,
          onClick: mp4Export.exportMp4,
        },
        "sep",
        { id: "restart", label: "重启仿真", shortcut: "Ctrl+R", onClick: restartSim, disabled: readOnly || restarting },
      ],
    },
    {
      id: "view", label: "视图(V)", items: [
        { id: "grid", label: "网格", checked: showGrid, onClick: () => setShowGrid((v) => !v) },
        { id: "scenario", label: "场景真值图层", checked: showScenario, onClick: () => setShowScenario((v) => !v) },
        { id: "drawer", label: "任务详情面板", checked: drawerVisible, onClick: () => setDrawerVisible((v) => !v) },
        { id: "sidebar", label: "编队状态面板", checked: sidebarOpen, onClick: () => setSidebarOpen((v) => !v) },
        "sep",
        { id: "trail", label: `UAV 轨迹模式：${trailModeLabel}`, onClick: cycleTrailMode },
      ],
    },
    {
      id: "run", label: "运行(R)", items: [
        {
          id: "retry", label: "重试模型决策", onClick: () => sendRuntimeCommand("retry"),
          disabled: readOnly || frame?.runtime_status !== "paused_model",
        },
        {
          id: "abort", label: "结束当前回合", danger: true,
          onClick: () => sendRuntimeCommand("abort"),
          disabled: readOnly || !frame?.episode_id || frame?.runtime_status === "finished",
        },
        "sep",
        { id: "restart", label: "重启仿真", onClick: restartSim, disabled: readOnly || restarting },
      ],
    },
    {
      id: "settings", label: "设置(S)", items: [
        { id: "open-settings", label: "算法参数设置…", shortcut: "Ctrl+,", onClick: () => setSettingsOpen(true), disabled: readOnly },
      ],
    },
    {
      id: "help", label: "帮助(H)", items: [
        { id: "about", label: "关于本系统", onClick: () => setAboutOpen(true) },
      ],
    },
  ];

  return (
    <main className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      <MenuBar menus={menus} />
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
        {mode === "replay" && replay.selectedFile && (
          <button
            className="export-mp4-btn"
            onClick={mp4Export.exportMp4}
            disabled={!mp4Export.available || mp4Export.exporting}
            title={mp4Export.error || (!mp4Export.available ? "MP4 encoder unavailable" : "下载回放 MP4")}
            aria-label={mp4Export.exporting ? `Exporting MP4 ${mp4Export.progress}%` : "下载回放 MP4"}
          >
            <Download size={15} />
            <span>{mp4Export.exporting ? `${mp4Export.progress}%` : "MP4"}</span>
          </button>
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
        vesselCommandStatus={vesselCommandStatus}
        vesselCommandBusy={vesselCommandBusy}
      />
      <BottomDrawer
        key={`${mode}|${frame?.episode_id}|${replay.selectedFile}`}
        mode={mode}
        frame={frame}
        llmCycle={displayedLlmCycle}
        decisions={decisionEvents}
        logs={runtimeLogs}
        logError={mode === "live" ? runtime.error : ""}
        editingAllowed={editingAllowed}
        vesselCommandBusy={vesselCommandBusy}
        vesselCommandStatus={vesselCommandStatus}
        onDeleteVessel={handleDeleteVessel}
        onSetVesselAis={handleSetVesselAis}
        onSelectDecision={mode === "replay" ? (event) => {
          const marker = replay.markers.find((item) => item.event?.event_id === event.event_id);
          if (marker) replay.seek(marker.frameIndex);
        } : undefined}
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
      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onRestarting={() => { setSettingsOpen(false); setRestarting(true); }}
      />
      {aboutOpen && (
        <div className="settings-overlay" role="presentation" onMouseDown={(e) => {
          if (e.target === e.currentTarget) setAboutOpen(false);
        }}>
          <div className="about-dialog" role="dialog" aria-modal="true" aria-label="关于本系统">
            <div className="settings-head">
              <h2>UAV 侦察态势监控</h2>
              <button type="button" className="settings-close" onClick={() => setAboutOpen(false)} aria-label="关闭">
                <X size={16} />
              </button>
            </div>
            <p className="about-text">
              海上多无人机协同侦察仿真系统。支持直播态势、任务回放、人工重点区指定、
              船舶增删与 AIS 切换，以及通过「算法参数设置」在线调整并重启仿真。
            </p>
          </div>
        </div>
      )}
      {restarting && (
        <div className="restart-overlay" role="alert" aria-live="assertive">
          <div className="restart-card">
            <div className="restart-spinner" aria-hidden="true" />
            <strong>{restartTimedOut ? "重启超时" : "正在重启仿真…"}</strong>
            <p>{restartTimedOut
              ? "服务在 90 秒内未恢复，请检查后端进程日志后手动刷新页面。"
              : "配置已保存，仿真进程正在用新配置重启，页面将在服务恢复后自动刷新。"}</p>
            {restartTimedOut && (
              <button type="button" className="btn ghost" onClick={() => window.location.reload()}>
                手动刷新
              </button>
            )}
          </div>
        </div>
      )}
    </main>
  );
}
