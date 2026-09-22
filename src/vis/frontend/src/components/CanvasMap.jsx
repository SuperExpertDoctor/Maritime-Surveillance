import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import { RadioTower } from "lucide-react";

import { computeLayout, dragToBBox, pixelToCoord } from "../renderer/geometry";
import { renderFrame } from "../renderer/layers";

const MAP_ASSET_SOURCES = {
  background: "/assets/background.png",
  uav: "/assets/rainbow-uav.png?v=20260801",
  carrier: "/assets/carrier.png?v=20260801",
  destroyer: "/assets/destroyer.png?v=20260801",
};

function loadMapAsset(source) {
  return new Promise((resolve) => {
    const image = new Image();
    image.decoding = "async";
    image.onload = () => resolve(image);
    image.onerror = () => resolve(null);
    image.src = source;
  });
}

const CanvasMap = forwardRef(function CanvasMap({
  frame,
  selectedUavId,
  onSelectUav,
  showGrid = false,
  showScenario = false,
  trailMode = "tail",
  selectionMode = false,
  onSelectionCommit,
  onSelectContact,
  selectedContactId,
  placementMode = false,
  editingAllowed = false,
  onPlaceVessel,
  onDropVessel,
  selectedScenarioVesselId,
  onSelectScenarioVessel,
}, ref) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const layoutRef = useRef({ cellSize: 20, offsetX: 0, offsetY: 0 });
  const hoverRef = useRef(null);
  const [hovered, setHovered] = useState(false);
  const [hoverVersion, setHoverVersion] = useState(0);
  const prevFrameRef = useRef(null);
  const targetFrameRef = useRef(null);
  const frameReceivedRef = useRef(0);
  const [sizeVersion, setSizeVersion] = useState(0);
  const [mapAssets, setMapAssets] = useState({});
  const exportingRef = useRef(false);
  const selectionRef = useRef(null);
  const [selection, setSelection] = useState(null);

  useEffect(() => {
    let disposed = false;
    Promise.all(Object.entries(MAP_ASSET_SOURCES).map(async ([key, source]) => [
      key,
      await loadMapAsset(source),
    ])).then((entries) => {
      if (!disposed) setMapAssets(Object.fromEntries(entries.filter(([, image]) => image)));
    });
    return () => { disposed = true; };
  }, []);

  const updateSize = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;

    const width = Math.max(1, container.clientWidth);
    const height = Math.max(1, container.clientHeight);
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.round(width * dpr);
    canvas.height = Math.round(height * dpr);
    canvas.style.width = `${width}px`;
    canvas.style.height = `${height}px`;
    canvas.getContext("2d").setTransform(dpr, 0, 0, dpr, 0, 0);
    layoutRef.current = computeLayout(width, height);
    setSizeVersion((version) => version + 1);
  }, []);

  useEffect(() => {
    updateSize();
    const observer = new ResizeObserver(updateSize);
    if (containerRef.current) observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, [updateSize]);

  useEffect(() => {
    if (!frame) return;
    prevFrameRef.current = targetFrameRef.current;
    targetFrameRef.current = frame;
    frameReceivedRef.current = performance.now();
  }, [frame]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return undefined;
    const context = canvas.getContext("2d");
    if (!targetFrameRef.current) return undefined;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let animationFrame = null;
    let phase = 0;
    const INTERP_MS = 180;

    const lerpAngle = (a, b, k) => {
      if (a == null || b == null) return b ?? a ?? 0;
      let d = ((b - a) % 360 + 540) % 360 - 180;
      return a + d * k;
    };

    const render = () => {
      if (exportingRef.current) return;
      const prev = prevFrameRef.current;
      const target = targetFrameRef.current;
      let displayFrame = target;

      if (prev && target) {
        const elapsed = performance.now() - frameReceivedRef.current;
        const raw = Math.min(1, elapsed / INTERP_MS);
        if (raw < 1) {
          const t = 1 - (1 - raw) ** 2; // ease-out quad

          const prevUavMap = new Map();
          for (const u of prev.uavs || []) prevUavMap.set(u.id, u);
          const prevShipMap = new Map();
          for (const s of prev.ships || []) prevShipMap.set(s.id, s);

          const uavs = (target.uavs || []).map((u) => {
            const prevU = prevUavMap.get(u.id);
            if (!prevU) return u;
            return {
              ...u,
              position: [
                prevU.position[0] + (u.position[0] - prevU.position[0]) * t,
                prevU.position[1] + (u.position[1] - prevU.position[1]) * t,
              ],
              heading_deg: lerpAngle(prevU.heading_deg, u.heading_deg, t),
            };
          });

          const ships = (target.ships || []).map((s) => {
            const prevS = prevShipMap.get(s.id);
            if (!prevS) return s;
            return {
              ...s,
              position: [
                prevS.position[0] + (s.position[0] - prevS.position[0]) * t,
                prevS.position[1] + (s.position[1] - prevS.position[1]) * t,
              ],
              heading_deg: lerpAngle(prevS.heading_deg, s.heading_deg, t),
            };
          });

          displayFrame = { ...target, uavs, ships };
        }
      }

      const { cellSize, offsetX, offsetY, mapBounds, legendBounds } = layoutRef.current;
      context.save();
      renderFrame(context, displayFrame, {
        cellSize,
        offsetX,
        offsetY,
        mapBounds,
        legendBounds,
        showGrid,
        showScenario,
        trailMode,
        selectedContactId,
        selectedScenarioVesselId,
        hoverInfo: hoverRef.current,
        selectedUavId,
        frameCount: phase,
        assets: mapAssets,
      });
      context.restore();
      phase += 1;
      const elapsed = performance.now() - frameReceivedRef.current;
      if (!reducedMotion && prev && target && elapsed < INTERP_MS) {
        animationFrame = window.requestAnimationFrame(render);
      }
    };

    render();
    return () => {
      if (animationFrame) window.cancelAnimationFrame(animationFrame);
    };
  }, [frame, hoverVersion, mapAssets, selectedContactId, selectedScenarioVesselId, selectedUavId, showGrid, showScenario, sizeVersion, trailMode]);

  useImperativeHandle(ref, () => ({
    async recordReplay(frames, { fps = 20, onProgress } = {}) {
      const canvas = canvasRef.current;
      if (!canvas?.captureStream || !window.MediaRecorder) {
        throw new Error("This browser cannot record the mission map");
      }
      if (!frames?.length) throw new Error("No replay frames available");
      const mimeType = ["video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"]
        .find((candidate) => MediaRecorder.isTypeSupported(candidate));
      if (!mimeType) throw new Error("This browser does not support WebM recording");

      const context = canvas.getContext("2d");
      const stream = canvas.captureStream(fps);
      const chunks = [];
      const recorder = new MediaRecorder(stream, { mimeType, videoBitsPerSecond: 7_000_000 });
      const finished = new Promise((resolve, reject) => {
        recorder.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data); };
        recorder.onstop = () => resolve(new Blob(chunks, { type: mimeType }));
        recorder.onerror = () => reject(new Error("Mission map recording failed"));
      });

      exportingRef.current = true;
      recorder.start();
      try {
        for (let index = 0; index < frames.length; index += 1) {
          const { cellSize, offsetX, offsetY, mapBounds, legendBounds } = layoutRef.current;
          context.save();
          renderFrame(context, frames[index], {
            cellSize, offsetX, offsetY, mapBounds, legendBounds,
            showGrid, showScenario, trailMode, hoverInfo: null, selectedUavId,
            selectedContactId,
            selectedScenarioVesselId,
            frameCount: index, assets: mapAssets,
          });
          context.restore();
          onProgress?.((index + 1) / frames.length);
          await new Promise((resolve) => window.setTimeout(resolve, 1000 / fps));
        }
      } finally {
        exportingRef.current = false;
        recorder.stop();
        stream.getTracks().forEach((track) => track.stop());
      }
      return finished;
    },
  }), [mapAssets, selectedContactId, selectedScenarioVesselId, selectedUavId, showGrid, showScenario, trailMode]);

  const handleMouseMove = useCallback((event) => {
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    const rect = canvas.getBoundingClientRect();
    const { cellSize, offsetX, offsetY } = layoutRef.current;
    const coord = pixelToCoord(
      event.clientX - rect.left,
      event.clientY - rect.top,
      cellSize,
      offsetX,
      offsetY,
    );

    const excluded = coord && frame.search_domain?.excluded_cells?.some(
      ([col, row]) => col === coord.col && row === coord.row,
    );
    if (coord && !excluded && frame.info_matrix && frame.value_matrix) {
      const info = Number(frame.info_matrix?.[coord.col]?.[coord.row] || 0);
      const value = Number(frame.value_matrix?.[coord.col]?.[coord.row] || 0);
      const category = info >= 0.7 ? "white" : info >= 0.2 ? "gray" : "black";
      hoverRef.current = { col: coord.col, row: coord.row, I: info, V: value, category };
      setHovered(true);
      setHoverVersion((version) => version + 1);
    } else {
      hoverRef.current = null;
      setHovered(false);
      setHoverVersion((version) => version + 1);
    }
  }, [frame]);

  const pointerPosition = useCallback((event) => {
    const canvas = canvasRef.current;
    if (!canvas) return null;
    const rect = canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }, []);

  const isInsideTask = useCallback((point) => {
    const bounds = layoutRef.current.taskBounds;
    return Boolean(bounds && point
      && point.x >= bounds.x && point.x <= bounds.x + bounds.width
      && point.y >= bounds.y && point.y <= bounds.y + bounds.height);
  }, []);

  const cancelSelection = useCallback(() => {
    const current = selectionRef.current;
    if (current?.pointerId != null && canvasRef.current?.hasPointerCapture(current.pointerId)) {
      canvasRef.current.releasePointerCapture(current.pointerId);
    }
    selectionRef.current = null;
    setSelection(null);
  }, []);

  useEffect(() => {
    if (!selectionMode) cancelSelection();
  }, [cancelSelection, selectionMode]);

  useEffect(() => {
    const onKeyDown = (event) => {
      if (event.key === "Escape") cancelSelection();
    };
    const onBlur = () => cancelSelection();
    window.addEventListener("keydown", onKeyDown);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      window.removeEventListener("blur", onBlur);
    };
  }, [cancelSelection]);

  const handlePointerDown = useCallback((event) => {
    if (!selectionMode || event.button !== 0) return;
    const point = pointerPosition(event);
    if (!isInsideTask(point)) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    selectionRef.current = { pointerId: event.pointerId, start: point, end: point };
    setSelection({ start: point, end: point });
  }, [isInsideTask, pointerPosition, selectionMode]);

  const handlePointerMove = useCallback((event) => {
    handleMouseMove(event);
    const current = selectionRef.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const point = pointerPosition(event);
    current.end = point;
    setSelection({ start: current.start, end: point });
  }, [handleMouseMove, pointerPosition]);

  const handlePointerUp = useCallback((event) => {
    const current = selectionRef.current;
    if (!current || current.pointerId !== event.pointerId) return;
    const point = pointerPosition(event);
    current.end = point;
    const bbox = dragToBBox(current.start, point, layoutRef.current);
    cancelSelection();
    if (bbox) onSelectionCommit?.(bbox);
  }, [cancelSelection, onSelectionCommit, pointerPosition]);

  const handleMouseLeave = useCallback(() => {
    hoverRef.current = null;
    setHovered(false);
    setHoverVersion((version) => version + 1);
  }, []);

  const handleClick = useCallback((event) => {
    if (selectionMode) return;
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    const point = pointerPosition(event);
    if (!point) return;
    const { x: mouseX, y: mouseY } = point;
    const { cellSize, offsetX, offsetY } = layoutRef.current;

    if (placementMode) {
      const coord = pixelToCoord(mouseX, mouseY, cellSize, offsetX, offsetY);
      if (coord) onPlaceVessel?.([coord.col + 0.5, coord.row + 0.5]);
      return;
    }

    for (const uav of frame.uavs) {
      const [col, row] = uav.position;
      const centerX = offsetX + (col + 0.5) * cellSize;
      const centerY = offsetY + (row + 0.5) * cellSize;
      if (Math.hypot(mouseX - centerX, mouseY - centerY) < Math.max(9, cellSize * 0.55)) {
        onSelectUav?.(uav.id === selectedUavId ? null : uav.id);
        return;
      }
    }
    for (const contact of frame.contacts || []) {
      const position = contact.estimated_position;
      if (!Array.isArray(position)) continue;
      const centerX = offsetX + (Number(position[0]) + 0.5) * cellSize;
      const centerY = offsetY + (Number(position[1]) + 0.5) * cellSize;
      if (Math.hypot(mouseX - centerX, mouseY - centerY) < Math.max(10, cellSize * 0.65)) {
        onSelectContact?.(contact.contact_id);
        return;
      }
    }
    for (const vessel of showScenario ? frame.scenario_vessels || [] : []) {
      const position = vessel.position;
      if (!Array.isArray(position)) continue;
      const centerX = offsetX + (Number(position[0]) + 0.5) * cellSize;
      const centerY = offsetY + (Number(position[1]) + 0.5) * cellSize;
      if (Math.hypot(mouseX - centerX, mouseY - centerY) < Math.max(10, cellSize * 0.7)) {
        onSelectScenarioVessel?.(
          vessel.scenario_entity_id === selectedScenarioVesselId ? null : vessel.scenario_entity_id,
        );
        return;
      }
    }
  }, [frame, onPlaceVessel, onSelectContact, onSelectScenarioVessel, onSelectUav, placementMode, pointerPosition, selectedScenarioVesselId, selectedUavId, selectionMode, showScenario]);

  const handleDragOver = useCallback((event) => {
    if (!editingAllowed || !event.dataTransfer.types.includes("application/x-vessel-class")) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
  }, [editingAllowed]);

  const handleDrop = useCallback((event) => {
    event.preventDefault();
    const vesselClass = event.dataTransfer.getData("application/x-vessel-class");
    if (!editingAllowed || !["type_i", "type_ii"].includes(vesselClass)) return;
    const point = pointerPosition(event);
    const { cellSize, offsetX, offsetY } = layoutRef.current;
    const coord = point && pixelToCoord(point.x, point.y, cellSize, offsetX, offsetY);
    if (!coord) return;
    onDropVessel?.(vesselClass, [coord.col + 0.5, coord.row + 0.5]);
  }, [editingAllowed, onDropVessel, pointerPosition]);

  const selectionBox = selection?.start && selection?.end
    ? {
      left: Math.min(selection.start.x, selection.end.x),
      top: Math.min(selection.start.y, selection.end.y),
      width: Math.abs(selection.start.x - selection.end.x),
      height: Math.abs(selection.start.y - selection.end.y),
    }
    : null;

  return (
    <div className="canvas-area" ref={containerRef}>
      <canvas
        ref={canvasRef}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerCancel={cancelSelection}
        onMouseLeave={handleMouseLeave}
        onClick={handleClick}
        onDragOver={handleDragOver}
        onDrop={handleDrop}
        style={{ cursor: placementMode || selectionMode ? "crosshair" : hovered ? "crosshair" : "default", touchAction: "none" }}
        aria-label="Operational map"
      />
      {selectionBox && (
        <div
          className="selection-rect"
          style={selectionBox}
          aria-label="当前重点区框选"
        />
      )}
      {!frame && (
        <div className="map-empty" role="status">
          <RadioTower size={22} />
          <strong>WAITING FOR MISSION DATA</strong>
          <span>Live telemetry or replay frames will appear here.</span>
        </div>
      )}
      {frame && <div className="search-domain-note" aria-label="搜索域说明">
        {frame.search_domain
          ? frame.search_domain.excluded_cells?.length
            ? `搜索域 ${frame.search_domain.area_km2} km² · 灰色斜线为排除区（不计入搜索覆盖）`
            : `全任务区域 ${frame.search_domain.area_km2} km² · 全部网格计入侦察覆盖`
          : "搜索域数据缺失 · 不推断排除区"}
      </div>}
      <div className="map-scale" aria-hidden="true"><i />20 KM</div>
    </div>
  );
});

export default CanvasMap;
