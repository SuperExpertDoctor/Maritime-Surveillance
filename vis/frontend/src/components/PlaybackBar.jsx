export default function PlaybackBar({ visible }) {
  if (!visible) return null;
  return (
    <div className="playback-bar">
      <span style={{ color: "var(--text-secondary)", fontSize: 13 }}>
        回放控制占位
      </span>
    </div>
  );
}
