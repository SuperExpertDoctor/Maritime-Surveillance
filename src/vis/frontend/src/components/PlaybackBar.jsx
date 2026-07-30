const SPEEDS = [0.5, 1, 2, 4, 8];

export default function PlaybackBar({
  visible,
  isPlaying,
  onPlayPause,
  frameIndex,
  totalFrames,
  onSeek,
  playSpeed,
  onSpeedChange,
  frame,
}) {
  if (!visible) return null;

  const simTime = frame?.timestamp || "--:--:--";

  return (
    <div className="playback-bar">
      {/* 播放/暂停 */}
      <button className="pb-btn" onClick={onPlayPause} title="空格键播放/暂停">
        {isPlaying ? "⏸" : "▶"}
      </button>

      {/* 上一帧 */}
      <button
        className="pb-btn"
        onClick={() => onSeek(Math.max(0, frameIndex - 1))}
        disabled={totalFrames === 0}
        title="← 上一帧"
      >
        ⏮
      </button>

      {/* 下一帧 */}
      <button
        className="pb-btn"
        onClick={() => onSeek(Math.min(totalFrames - 1, frameIndex + 1))}
        disabled={totalFrames === 0}
        title="→ 下一帧"
      >
        ⏭
      </button>

      {/* 时间线滑块 */}
      <input
        type="range"
        className="pb-slider"
        min={0}
        max={Math.max(0, totalFrames - 1)}
        value={frameIndex}
        onChange={(e) => onSeek(Number(e.target.value))}
        disabled={totalFrames === 0}
      />

      {/* 帧计数 */}
      <span className="pb-frame-count">
        {frameIndex + 1} / {totalFrames || 0}
      </span>

      {/* 仿真时间 */}
      <span className="pb-sim-time">{simTime}</span>

      {/* 速度选择 */}
      <select
        className="pb-speed"
        value={playSpeed}
        onChange={(e) => onSpeedChange(Number(e.target.value))}
      >
        {SPEEDS.map((s) => (
          <option key={s} value={s}>{s}x</option>
        ))}
      </select>
    </div>
  );
}
