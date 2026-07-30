import { useState } from "react";
import CanvasMap from "./components/CanvasMap";
import RightSidebar from "./components/RightSidebar";
import BottomDrawer from "./components/BottomDrawer";
import PlaybackBar from "./components/PlaybackBar";

export default function App() {
  const [mode, setMode] = useState("live");   // "live" | "replay"
  const [frame, setFrame] = useState(null);
  const [selectedUavId, setSelectedUavId] = useState(null);
  const [drawerVisible, setDrawerVisible] = useState(false);

  return (
    <div className={`app-layout ${mode === "replay" ? "replay-active" : ""}`}>
      <CanvasMap
        frame={frame}
        selectedUavId={selectedUavId}
        onSelectUav={setSelectedUavId}
      />
      <RightSidebar frame={frame} />
      <BottomDrawer frame={frame} visible={drawerVisible} />
      <PlaybackBar visible={mode === "replay"} />
    </div>
  );
}
