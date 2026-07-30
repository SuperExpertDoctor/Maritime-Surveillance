import { useRef, useState, useEffect, useCallback } from "react";
import { renderFrame } from "../renderer/layers";
import { computeLayout, pixelToCoord } from "../renderer/geometry";

export default function CanvasMap({
  frame,
  selectedUavId,
  onSelectUav,
  showGrid = false,
  mode = "live",
}) {
  const canvasRef = useRef(null);
  const containerRef = useRef(null);
  const layoutRef = useRef({ cellSize: 20, offsetX: 0, offsetY: 0 });
  const hoverRef = useRef(null);
  const [hovered, setHovered] = useState(false);
  const frameCountRef = useRef(0);
  const rafRef = useRef(null);

  // 响应式尺寸
  const updateSize = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const w = container.clientWidth;
    const h = container.clientHeight;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    layoutRef.current = computeLayout(w, h);
  }, []);

  // ResizeObserver
  useEffect(() => {
    updateSize();
    const obs = new ResizeObserver(updateSize);
    if (containerRef.current) obs.observe(containerRef.current);
    return () => obs.disconnect();
  }, [updateSize]);

  // 主渲染循环
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");

    const render = () => {
      const { cellSize, offsetX, offsetY } = layoutRef.current;
      ctx.save();
      renderFrame(ctx, frame, {
        cellSize,
        offsetX,
        offsetY,
        showGrid,
        hoverInfo: hoverRef.current,
        selectedUavId,
        frameCount: frameCountRef.current,
      });
      ctx.restore();
      frameCountRef.current++;
      rafRef.current = requestAnimationFrame(render);
    };

    rafRef.current = requestAnimationFrame(render);
    return () => {
      if (rafRef.current) cancelAnimationFrame(rafRef.current);
    };
  }, [frame, showGrid, selectedUavId]);

  // 鼠标 hover → cell tooltip
  const handleMouseMove = useCallback((e) => {
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const { cellSize, offsetX, offsetY } = layoutRef.current;
    const coord = pixelToCoord(mx, my, cellSize, offsetX, offsetY);
    if (coord && frame.info_matrix && frame.value_matrix) {
      const I = (frame.info_matrix[coord.col] || [])[coord.row] || 0;
      const V = (frame.value_matrix[coord.col] || [])[coord.row] || 0;
      let cat = "black";
      if (I >= 0.7) cat = "white";
      else if (I >= 0.2) cat = "gray";
      hoverRef.current = { col: coord.col, row: coord.row, I, V, category: cat };
      setHovered(true);
    } else {
      hoverRef.current = null;
      setHovered(false);
    }
  }, [frame]);

  // 点击 UAV 选中
  const handleClick = useCallback((e) => {
    if (!frame || !frame.uavs) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const { cellSize, offsetX, offsetY } = layoutRef.current;

    for (const u of frame.uavs) {
      const [col, row] = u.position;
      const cx = offsetX + col * cellSize + cellSize / 2;
      const cy = offsetY + row * cellSize + cellSize / 2;
      const dist = Math.sqrt((mx - cx) ** 2 + (my - cy) ** 2);
      if (dist < cellSize * 0.5) {
        onSelectUav?.(u.id === selectedUavId ? null : u.id);
        return;
      }
    }
  }, [frame, selectedUavId, onSelectUav]);

  return (
    <div className="canvas-area" ref={containerRef}>
      <canvas
        ref={canvasRef}
        onMouseMove={handleMouseMove}
        onClick={handleClick}
        style={{ cursor: hovered ? "crosshair" : "default" }}
      />
    </div>
  );
}
