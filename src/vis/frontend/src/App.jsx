import { useState, useEffect, useRef, useCallback } from "react";
import CanvasMap from "./components/CanvasMap";
import RightSidebar from "./components/RightSidebar";
import BottomDrawer from "./components/BottomDrawer";
import PlaybackBar from "./components/PlaybackBar";

// ── WebSocket 直播 Hook ──
function useLiveSocket(setFrame, enabled) {
  const wsRef = useRef(null);
  const retryRef = useRef(0);
  const heartbeatRef = useRef(null);

  useEffect(() => {
    if (!enabled) {
      // 断开连接
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
      if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
      return;
    }

    let cancelled = false;

    function connect() {
      if (cancelled) return;
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws/live`);
      wsRef.current = ws;

      ws.onopen = () => {
        retryRef.current = 0;
        // 心跳：每 25 秒 ping
        heartbeatRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) ws.send("ping");
        }, 25000);
      };

      ws.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data && data.frame_id != null) {
            setFrame(data);
          }
        } catch {}
      };

      ws.onclose = () => {
        if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
        if (!cancelled) {
          // 指数退避重连: 1s → 2s → 4s → ... → 最大 30s
          const delay = Math.min(1000 * Math.pow(2, retryRef.current), 30000);
          retryRef.current++;
          setTimeout(connect, delay);
        }
      };

      ws.onerror = () => { ws.close(); };
    }

    connect();

    return () => {
      cancelled = true;
      if (heartbeatRef.current) { clearInterval(heartbeatRef.current); heartbeatRef.current = null; }
      if (wsRef.current) { wsRef.current.close(); wsRef.current = null; }
    };
  }, [enabled, setFrame]);
}

// ── Replay 文件列表获取 ──
async function fetchReplayFiles() {
  try {
    const resp = await fetch("/api/replay/list");
    const data = await resp.json();
    return data.files || [];
  } catch {
    return [];
  }
}

async function fetchReplayFrames(filename) {
  try {
    const resp = await fetch(`/api/replay?file=${encodeURIComponent(filename)}`);
    const text = await resp.text();
    return text.trim().split("\n").map((l) => JSON.parse(l));
  } catch {
    return [];
  }
}

export default function App() {
  const [mode, setMode] = useState("live");   // "live" | "replay"
  const [frame, setFrame] = useState(null);
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);
  const [replayFiles, setReplayFiles] = useState([]);
  const [selectedFile, setSelectedFile] = useState("");
  const [statusText, setStatusText] = useState("未连接");

  // 直播 WebSocket
  useLiveSocket(setFrame, mode === "live");

  // 回放帧数据
  const framesRef = useRef([]);
  const [frameIndex, setFrameIndex] = useState(0);
  const [isPlaying, setIsPlaying] = useState(false);
  const [playSpeed, setPlaySpeed] = useState(1);
  const playbackTimer = useRef(null);

  // 直播模式下帧更新时显示状态
  useEffect(() => {
    if (mode === "live" && frame) {
      setStatusText(`帧 #${frame.frame_id} | t=${frame.sim_time_min?.toFixed(0)}min`);
    }
  }, [frame, mode]);

  // 切换模式
  const switchMode = useCallback((newMode) => {
    setMode(newMode);
    if (newMode === "live") {
      setFrame(null);
      setStatusText("连接中...");
      if (playbackTimer.current) { clearInterval(playbackTimer.current); playbackTimer.current = null; }
      setIsPlaying(false);
    } else {
      setStatusText("");
      setFrame(null);
      fetchReplayFiles().then(setReplayFiles);
    }
  }, []);

  // 加载回放文件
  const loadReplayFile = useCallback(async (filename) => {
    if (!filename) return;
    setSelectedFile(filename);
    setStatusText("加载中...");
    const frames = await fetchReplayFrames(filename);
    framesRef.current = frames;
    setFrameIndex(0);
    setIsPlaying(false);
    if (frames.length > 0) {
      setFrame(frames[0]);
      setStatusText(`${frames.length} 帧已加载`);
    } else {
      setStatusText("文件为空");
    }
  }, []);

  // 回放定时器
  useEffect(() => {
    if (mode !== "replay" || !isPlaying) {
      if (playbackTimer.current) { clearInterval(playbackTimer.current); playbackTimer.current = null; }
      return;
    }
    const intervalMs = 1000 / playSpeed;
    playbackTimer.current = setInterval(() => {
      setFrameIndex((prev) => {
        const next = prev + 1;
        if (next >= framesRef.current.length) {
          setIsPlaying(false);
          return prev;
        }
        setFrame(framesRef.current[next]);
        return next;
      });
    }, intervalMs);
    return () => {
      if (playbackTimer.current) { clearInterval(playbackTimer.current); playbackTimer.current = null; }
    };
  }, [mode, isPlaying, playSpeed]);

  // 回放进度变更
  const onSeek = useCallback((index) => {
    setFrameIndex(index);
    if (framesRef.current[index]) {
      setFrame(framesRef.current[index]);
    }
  }, []);

  // 键盘快捷键
  useEffect(() => {
    const onKey = (e) => {
      if (mode !== "replay") return;
      if (e.key === " ") { e.preventDefault(); setIsPlaying((p) => !p); }
      if (e.key === "ArrowLeft") { e.preventDefault(); onSeek(Math.max(0, frameIndex - 1)); }
      if (e.key === "ArrowRight") { e.preventDefault(); onSeek(Math.min(framesRef.current.length - 1, frameIndex + 1)); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [mode, frameIndex, onSeek]);

  return (
    <div className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      {/* ── 顶部模式切换栏 ── */}
      <div className="top-bar">
        <div className="mode-switch">
          <button
            className={`mode-btn ${mode === "live" ? "active" : ""}`}
            onClick={() => switchMode("live")}
          >
            ● 直播
          </button>
          <button
            className={`mode-btn ${mode === "replay" ? "active" : ""}`}
            onClick={() => switchMode("replay")}
          >
            ▶ 回放
          </button>
        </div>

        {mode === "replay" && (
          <select
            className="file-select"
            value={selectedFile}
            onChange={(e) => loadReplayFile(e.target.value)}
          >
            <option value="">-- 选择回放文件 --</option>
            {replayFiles.map((f) => (
              <option key={f} value={f}>{f}</option>
            ))}
          </select>
        )}

        <span className="status-text">{statusText}</span>
      </div>

      <CanvasMap
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
      />
      <RightSidebar
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
      />
      <BottomDrawer
        frame={frame}
        visible={drawerVisible}
        onToggle={() => setDrawerVisible((v) => !v)}
      />
      <PlaybackBar
        visible={mode === "replay"}
        isPlaying={isPlaying}
        onPlayPause={() => setIsPlaying((p) => !p)}
        frameIndex={frameIndex}
        totalFrames={framesRef.current.length}
        onSeek={onSeek}
        playSpeed={playSpeed}
        onSpeedChange={setPlaySpeed}
        frame={frame}
      />
    </div>
  );
}
