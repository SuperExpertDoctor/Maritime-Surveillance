import { useEffect, useMemo, useRef, useState } from "react";

// 任务分配工作流：UAV 节点列与三种任务节点列交替排布，每生效一份计划
// （或发生 UAV 返航）向右延申一对列。连线颜色即 UAV 工作状态：
// 蓝=区域覆盖、橙=目标跟踪、灰=返航基地。节点可拖拽，点选 UAV 高亮整条状态链。
const TASKS = [
  ["cov", "区域覆盖"],
  ["trk", "目标跟踪"],
  ["rtb", "返航基地"],
];
const ST_LBL = { cov: "区域覆盖", trk: "目标跟踪", rtb: "返航基地" };
const COLW = 150;
const TOP = 46;
const UGAP = 46;
const TGAP = 80;
const NODE_H = 22;

const TRG_LABEL = {
  event: "事件触发",
  periodic: "周期性触发",
  retry: "失败重试",
  initial: "首次部署",
  human: "人工消息",
};

// kind / task_id / 区域表 → 三态
function classify(kind, taskId, searchIds, trackIds) {
  const k = String(kind || "").toLowerCase();
  if (["probe", "investigate", "track", "reacquire"].includes(k)) return "trk";
  if (["search", "coverage", "partition", "zone"].includes(k)) return "cov";
  if (["rtb", "return", "returning", "base"].includes(k)) return "rtb";
  const id = String(taskId || "");
  if (trackIds.has(id)) return "trk";
  if (searchIds.has(id)) return "cov";
  const p = id.split(/[:\-]/)[0].toLowerCase();
  if (["q", "t", "probe", "track", "investigate"].includes(p)) return "trk";
  if (["rtb", "return", "base"].includes(p)) return "rtb";
  return "cov";
}

function buildRounds({ plans, decisions, logs, frame, assignments, mode }) {
  const uavIds = (frame?.uavs || [])
    .map((u) => u.id)
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  const searchIds = new Set((frame?.search_regions || []).map((r) => r.id));
  const trackIds = new Set((frame?.track_regions || []).map((r) => r.id));
  const liveKind = new Map((assignments || []).map((r) => [r.task_id, r.kind]));

  // 每轮的配对变化 {uav_id: state}
  const rounds = [];
  const livePlans = (plans || []).filter(
    (p) => ["approved", "auto_approved"].includes(p.status) && p.kind !== "legacy-plan/v1",
  );
  if (mode !== "replay" && livePlans.length) {
    const sorted = [...livePlans].sort(
      (a, b) => (a.decided_at_min || a.created_at_min || 0) - (b.decided_at_min || b.created_at_min || 0),
    );
    sorted.forEach((p, i) => {
      const picks = {};
      (p.summary?.assignments || []).forEach((a) => {
        picks[a.uav_id] = classify(a.kind, a.task_id, searchIds, trackIds);
      });
      rounds.push({
        id: `R${i + 1}`,
        key: p.plan_id,
        trg: TRG_LABEL[p.trigger] || p.trigger || "调度决策",
        evt: p.trigger === "event",
        at: p.decided_at_min || p.created_at_min || 0,
        picks,
      });
    });
  } else {
    const committed = (decisions || []).filter(
      (e) => e.data?.status === "committed" && (e.data?.assignments || []).length,
    );
    committed.forEach((e, i) => {
      const picks = {};
      e.data.assignments.forEach((a) => {
        picks[a.uav_id] = classify(liveKind.get(a.task_id), a.task_id, searchIds, trackIds);
      });
      const src = e.data?.trigger_source;
      rounds.push({
        id: `R${i + 1}`,
        key: e.event_id || `d${i}`,
        trg: TRG_LABEL[src] || src || "调度决策",
        evt: src === "event",
        at: Number(e.data?.time_min ?? e.time ?? 0),
        picks,
      });
    });
  }

  // UAV 返航事件：穿插进轮次序列（每次返航也是一次任务方法变化）
  const returns = (logs || [])
    .filter((l) => l.status === "uav_returned" && l.uav_id)
    .map((l) => ({ at: Number(l.sim_time_min ?? 0), uav: l.uav_id }));
  const merged = [];
  const pendingReturns = [...returns].sort((a, b) => a.at - b.at);
  let seq = 0;
  rounds.forEach((r) => {
    while (pendingReturns.length && pendingReturns[0].at <= r.at) {
      const ev = pendingReturns.shift();
      merged.push({
        id: `R${++seq}`,
        key: `ret-${ev.uav}-${ev.at}`,
        trg: "事件触发·UAV返航",
        evt: true,
        at: ev.at,
        picks: { [ev.uav]: "rtb" },
      });
    }
    r.id = `R${++seq}`;
    merged.push(r);
  });
  pendingReturns.forEach((ev) => {
    merged.push({
      id: `R${++seq}`,
      key: `ret-${ev.uav}-${ev.at}`,
      trg: "事件触发·UAV返航",
      evt: true,
      at: ev.at,
      picks: { [ev.uav]: "rtb" },
    });
  });

  // 逐轮累计每架 UAV 的状态快照（未变更的沿用上一轮）
  const state = Object.fromEntries(uavIds.map((id) => [id, "cov"]));
  const snapshots = [];
  merged.forEach((r) => {
    const prev = { ...state };
    let changed = false;
    Object.entries(r.picks).forEach(([uav, st]) => {
      if (state[uav] !== undefined && state[uav] !== st) changed = true;
      if (uavIds.includes(uav)) state[uav] = st;
      else if (uav) { uavIds.push(uav); uavIds.sort((a, b) => a.localeCompare(b, undefined, { numeric: true })); state[uav] = st; changed = true; }
    });
    snapshots.push({ ...r, changed: changed || snapshots.length === 0, state: { ...state }, prev });
  });

  // 末列：当前实时状态（仅直播）
  let current = null;
  if (mode !== "replay" && uavIds.length) {
    const cur = {};
    uavIds.forEach((id) => {
      const u = (frame?.uavs || []).find((x) => x.id === id);
      if (u && ["returning", "refueling", "holding"].includes(u.status)) cur[id] = "rtb";
      else cur[id] = snapshots.length ? state[id] : "cov";
    });
    current = { id: "当前", state: cur };
  }
  return { uavIds, snapshots, current };
}

export default function WorkflowPanel({ plans = [], decisions = [], logs = [], frame, assignments = [], mode = "live" }) {
  const { uavIds, snapshots, current } = useMemo(
    () => buildRounds({ plans, decisions, logs, frame, assignments, mode }),
    [plans, decisions, logs, frame, assignments, mode],
  );
  const [offsets, setOffsets] = useState({});
  const [picked, setPicked] = useState(null);
  const [dragKey, setDragKey] = useState(null);
  const innerRef = useRef(null);
  const scrollRef = useRef(null);

  const cols = useMemo(() => {
    const list = [];
    snapshots.forEach((r) => {
      list.push({ type: "uav", r });
      list.push({ type: "task", r });
    });
    if (current) list.push({ type: "uav", r: current, last: true });
    return list;
  }, [snapshots, current]);

  const rowH = Math.max(uavIds.length * UGAP, 3 * TGAP) + TOP + 30;
  const width = cols.length * COLW + 60;

  // 节点基础坐标（不随拖拽变化）；拖拽位移叠加在 offsets 上
  const nodes = useMemo(() => {
    const list = [];
    cols.forEach((c, ci) => {
      const x = 30 + ci * COLW;
      if (c.type === "uav") {
        uavIds.forEach((u, k) => {
          list.push({ key: `${c.r.id}|${u}`, uav: u, col: ci, x: x + 44, y: TOP + k * UGAP, kind: "uav", st: c.r.state[u] || "cov" });
        });
      } else {
        TASKS.forEach(([cls], k) => {
          list.push({ key: `${c.r.id}|t|${cls}`, uav: null, col: ci, x: x + 30, y: TOP + (uavIds.length * UGAP) / 2 - TGAP + k * TGAP - NODE_H / 2 + 12, kind: "task", st: cls });
        });
      }
    });
    return list;
  }, [cols, uavIds]);

  const posOf = (n) => {
    const o = offsets[n.key] || { dx: 0, dy: 0 };
    return { x: n.x + o.dx, y: n.y + o.dy };
  };

  // 连线：uav(r)→task(r,pick)；task(r)→uav(r+1)。被选中链的节点集合顺便算出。
  const { links, chainNodes, chainLinks } = useMemo(() => {
    const links = [];
    const chainNodes = new Set();
    const chainLinks = new Set();
    const uavCol = (i) => nodes.find((n) => n.col === i && n.kind === "uav");
    cols.forEach((c, ci) => {
      if (c.type !== "task") return;
      const prev = nodes.filter((n) => n.col === ci - 1 && n.kind === "uav");
      const next = nodes.filter((n) => n.col === ci + 1 && n.kind === "uav");
      prev.forEach((un) => {
        const st = c.r.state[un.uav] || "cov";
        const tn = nodes.find((n) => n.col === ci && n.st === st);
        if (tn) links.push({ a: un, b: tn, uav: un.uav, st });
      });
      next.forEach((un) => {
        const st = c.r.state[un.uav] || "cov";
        const tn = nodes.find((n) => n.col === ci && n.st === st);
        if (tn) links.push({ a: tn, b: un, uav: un.uav, st });
      });
    });
    links.forEach((l, i) => {
      if (picked && l.uav === picked) {
        chainLinks.add(i);
        chainNodes.add(l.a.key);
        chainNodes.add(l.b.key);
      }
    });
    return { links, chainNodes, chainLinks };
  }, [cols, nodes, picked]);

  // 新轮出现时自动滚到最右（最新）
  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollLeft = el.scrollWidth;
  }, [snapshots.length]);

  const startDrag = (n) => (e) => {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    const o = offsets[n.key] || { dx: 0, dy: 0 };
    setDragKey({ key: n.key, sx: e.clientX - o.dx, sy: e.clientY - o.dy });
  };
  const moveDrag = (n) => (e) => {
    if (!dragKey || dragKey.key !== n.key) return;
    setOffsets((cur) => ({ ...cur, [n.key]: { dx: e.clientX - dragKey.sx, dy: e.clientY - dragKey.sy } }));
  };
  const endDrag = (n) => (e) => {
    if (dragKey?.key === n.key) setDragKey(null);
  };

  if (!uavIds.length) {
    return <div className="tab-empty">暂无 UAV 数据，等待首个仿真帧</div>;
  }
  if (!snapshots.length) {
    return <div className="tab-empty">尚无生效的任务分配方案；计划生效后此树将逐轮向右延申</div>;
  }

  return (
    <div className={`wf-panel${picked ? " dim" : ""}`} onClick={() => picked && setPicked(null)}>
      <div className="wf-legend">
        <b>任务分配工作流</b>
        <span><i className="wf-dot cov" />区域覆盖</span>
        <span><i className="wf-dot trk" />目标跟踪</span>
        <span><i className="wf-dot rtb" />返航基地</span>
        <small>每次方案变化新增一对列 · 右为最新 · 点选 UAV 高亮其状态链 · 节点可拖拽</small>
      </div>
      <div className="wf-scroll" ref={scrollRef}>
        <div className="wf-inner" ref={innerRef} style={{ width, height: rowH }}>
          {/* 列背景与级头 */}
          {cols.map((c, ci) => (
            <div key={`z${ci}`} className={`wf-col ${c.type}${c.r.changed ? " changed" : ""}`}
              style={{ left: ci * COLW + 14, width: COLW }}>
              <div className="wf-col-head">
                {c.last ? "当前" : c.r.id}
                <br />
                {!c.last && <span className={`wf-trg ${c.r.evt ? "evt" : "cyc"}`}>{c.r.trg}</span>}
              </div>
            </div>
          ))}
          {/* 连线 */}
          <svg className="wf-edges" width={width} height={rowH}>
            {links.map((l, i) => {
              const a = posOf(l.a);
              const b = posOf(l.b);
              const aw = l.a.kind === "task" ? 112 : 74;
              const bw = l.b.kind === "task" ? 112 : 74;
              const ax = a.x + aw / 2, ay = a.y + NODE_H / 2;
              const bx = b.x + bw / 2, by = b.y + NODE_H / 2;
              const mx = (ax + bx) / 2;
              return (
                <path key={i} className={`wf-edge ${l.st}${chainLinks.has(i) ? " chain" : ""}`}
                  d={`M${ax},${ay} C${mx},${ay} ${mx},${by} ${bx},${by}`} />
              );
            })}
          </svg>
          {/* 节点 */}
          {nodes.map((n) =>
            n.kind === "uav" ? (
              <button key={n.key} type="button"
                className={`wf-unode${chainNodes.has(n.key) ? " hot-u" : ""}`}
                style={{ left: posOf(n).x, top: posOf(n).y }}
                onPointerDown={startDrag(n)} onPointerMove={moveDrag(n)} onPointerUp={endDrag(n)}
                onClick={(e) => { e.stopPropagation(); setPicked(picked === n.uav ? null : n.uav); }}
                title={`${n.uav} · ${ST_LBL[n.st]}`}>
                {n.uav}
              </button>
            ) : (
              <div key={n.key}
                className={`wf-tnode ${n.st}${chainNodes.has(n.key) ? " hot-t" : ""}`}
                style={{ left: posOf(n).x, top: posOf(n).y }}
                onPointerDown={startDrag(n)} onPointerMove={moveDrag(n)} onPointerUp={endDrag(n)}>
                <i className="wf-dot" />{ST_LBL[n.st]}
              </div>
            ),
          )}
        </div>
      </div>
    </div>
  );
}
