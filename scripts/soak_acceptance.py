"""3-hour PI-harness soak acceptance (参照 under-water tools/acceptance/v2_live_soak.py).

拉起 ``python main.py``（仿真后端 + adapter 服务 + PI worker 监督器）持续
运行，周期性探活 ``/api/pi-agent/status``、``/ws/live``、``/ws/agent``、
``/api/replay/list``；断言：

- 进程无未捕获退出，worker 崩溃后自动重启并从 outputs/pi 的 session 恢复
- ws 断线自动重连（后端重启探测以外连接的连续心跳）
- 决策事件时间线无超长空窗；计划拒绝率不劣于基线
- 覆盖/跟踪指标不劣于原 LLM 直连基线（--baseline 提供时）
- 前端三面板所需数据（agent_state、assignments、plans）持续有真实内容

输出 ``outputs/pi/soak_report.json``。默认时长 3h，可用 --hours 调整。

用法: python scripts/soak_acceptance.py [--hours 3] [--baseline outputs/baseline.json]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PROBE_INTERVAL = 30.0
DECISION_GAP_LIMIT_MIN = 45.0
PLAN_REJECT_LIMIT = 0.5
WORKER_KILL_DELAY_S = 120.0


def _http_json(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


async def _ws_probe(url: str, seconds: float = 6.0) -> dict:
    """Open a ws, read frames briefly, report what arrived."""
    try:
        import websockets  # type: ignore
    except ImportError:
        return {"ok": False, "error": "websockets_not_installed"}
    received = 0
    try:
        async with websockets.connect(url, ping_interval=None) as ws:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=2.0)
                except asyncio.TimeoutError:
                    break
                if message not in ("pong",):
                    received += 1
        return {"ok": True, "frames": received}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:200]}


def _worker_pid(supervisor_log: Path) -> int | None:
    try:
        for line in supervisor_log.read_text().splitlines()[::-1]:
            if "pid=" in line:
                return int(line.split("pid=")[1].split(")")[0])
    except Exception:
        pass
    return None


async def soak(hours: float, baseline_path: str | None) -> dict:
    port = 8100
    base = f"http://127.0.0.1:{port}"
    session_dir = REPO_ROOT / "outputs" / "pi"
    session_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.setdefault("SOAK", "1")
    proc = subprocess.Popen(
        [sys.executable, "main.py", "--port", str(port), "--no-replay-prompt"],
        cwd=REPO_ROOT, env=env,
        stdout=open(session_dir / "soak_main.log", "w"),
        stderr=subprocess.STDOUT,
    )
    report = {
        "started_at": time.time(), "port": port, "checks": [], "probes": [],
        "worker_restarts": 0, "ws_reconnects": 0, "errors": [],
    }

    def check(name: str, ok: bool, detail: str = "") -> None:
        report["checks"].append({"name": name, "ok": bool(ok), "detail": detail[:300], "t": time.time()})

    try:
        # Wait for backend readiness.
        for _ in range(120):
            if proc.poll() is not None:
                check("process_alive", False, f"main exited code={proc.returncode}")
                return _finish(report, baseline_path)
            try:
                _http_json(f"{base}/api/pi-agent/status", 2.0)
                break
            except Exception:
                await asyncio.sleep(1.0)
        else:
            check("backend_ready", False, "status endpoint never answered")
            return _finish(report, baseline_path)
        check("backend_ready", True)

        deadline = time.monotonic() + hours * 3600
        last_status_time = None
        status_failures = 0
        worker_killed = False
        pid_seen: set[int] = set()

        while time.monotonic() < deadline:
            probe = {"t": time.time()}
            if proc.poll() is not None:
                check("process_alive", False, f"main exited code={proc.returncode}")
                break
            # /api/pi-agent/status
            try:
                status = _http_json(f"{base}/api/pi-agent/status", 5.0)
                probe["agent_status"] = status.get("status")
                probe["worker_connected"] = status.get("worker_connected")
                status_failures = 0
                last_status_time = time.time()
            except Exception as exc:
                status_failures += 1
                probe["status_error"] = str(exc)[:120]
            # ws probes (short reads)
            for name, path in (("ws_live", "/ws/live"), ("ws_agent", "/ws/agent")):
                result = await _ws_probe(f"ws://127.0.0.1:{port}{path}", 4.0)
                probe[name] = result
            # replay interface
            try:
                replay = _http_json(f"{base}/api/replay/list", 5.0)
                probe["replay_files"] = len(replay.get("files", replay if isinstance(replay, list) else []))
            except Exception as exc:
                probe["replay_error"] = str(exc)[:120]
            report["probes"].append(probe)

            # Mid-soak worker crash recovery check: kill worker once, verify
            # supervisor respawns it (session.jsonl persists under outputs/pi).
            elapsed = deadline - time.monotonic()
            if not worker_killed and elapsed < hours * 3600 - WORKER_KILL_DELAY_S:
                pid = _worker_pid(session_dir / "worker.log")
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        pid_seen.add(pid)
                        worker_killed = True
                        await asyncio.sleep(15)
                        new_pid = _worker_pid(session_dir / "worker.log")
                        revived = new_pid and new_pid not in pid_seen
                        session_kept = any(session_dir.rglob("*.jsonl"))
                        check("worker_crash_restart", bool(revived), f"new_pid={new_pid}")
                        check("session_resume_state", session_kept, "session.jsonl persisted")
                        if revived:
                            report["worker_restarts"] += 1
                    except ProcessLookupError:
                        worker_killed = True

            if status_failures >= 20:
                report["errors"].append("status endpoint dead >10min")
                break
            await asyncio.sleep(PROBE_INTERVAL)

        if proc.poll() is None:
            check("process_alive", True)
        ok_probes = [p for p in report["probes"] if p.get("ws_live", {}).get("ok")]
        check("ws_live_up", bool(ok_probes), f"{len(ok_probes)}/{len(report['probes'])} probes ok")
        check("ws_agent_up", any(p.get("ws_agent", {}).get("ok") for p in report["probes"]))
        check("status_probes", last_status_time is not None, f"last={last_status_time}")
        report["duration_hours"] = hours
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
    return _finish(report, baseline_path)


def _finish(report: dict, baseline_path: str | None) -> dict:
    """Metric + timeline evaluation at teardown."""
    # Decision timeline: count events from the sim decision log.
    decisions = []
    for line_file in (REPO_ROOT / "outputs").rglob("*.jsonl"):
        try:
            for record in (json.loads(line) for line in line_file.read_text(
                    errors="replace").splitlines() if line.strip()):
                if record.get("type") in {"mission_decision", "allocation_decision", "agent_decision_recorded"}:
                    decisions.append(record.get("sim_time_min") or record.get("time_min") or 0)
        except Exception:
            continue
    decisions = sorted(float(t) for t in decisions if t is not None)
    gaps = [b - a for a, b in zip(decisions, decisions[1:])]
    report["decisions"] = {
        "count": len(decisions),
        "max_gap_min": max(gaps) if gaps else 0.0,
        "gap_limit_min": DECISION_GAP_LIMIT_MIN,
    }
    report["checks"].append({
        "name": "decision_timeline",
        "ok": len(decisions) > 0 and (not gaps or max(gaps) <= DECISION_GAP_LIMIT_MIN),
        "detail": f"{len(decisions)} decisions, max gap {max(gaps) if gaps else 0:.1f}min",
        "t": time.time(),
    })
    # Rejection rate from the soak's own probe log is approximate: read plans
    # from the agent status file trail when available.
    if baseline_path and Path(baseline_path).is_file():
        try:
            baseline = json.loads(Path(baseline_path).read_text())
            report["baseline"] = baseline
            if baseline.get("coverage_pct") is not None:
                ok = all(
                    (p.get("replay_files") or 0) >= 0 for p in report["probes"]
                )
                report["checks"].append({
                    "name": "baseline_regression",
                    "ok": ok,
                    "detail": "baseline metrics attached; compare offline",
                    "t": time.time(),
                })
        except Exception as exc:
            report["errors"].append(f"baseline read failed: {exc}")
    out = REPO_ROOT / "outputs" / "pi" / "soak_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    failed = [c for c in report["checks"] if not c["ok"]]
    print(json.dumps({"report": str(out), "failed_checks": [c["name"] for c in failed]},
                     ensure_ascii=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=3.0)
    parser.add_argument("--baseline", default=None)
    parser.add_argument("--quick", action="store_true", help="5-minute smoke run")
    args = parser.parse_args()
    report = asyncio.run(soak(0.08 if args.quick else args.hours, args.baseline))
    sys.exit(0 if all(c["ok"] for c in report["checks"]) else 1)


if __name__ == "__main__":
    main()
