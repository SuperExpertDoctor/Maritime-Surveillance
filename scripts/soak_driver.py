#!/usr/bin/env python3
"""Adversarial soak driver: injects blue-side vessel churn and operator focus
areas into a live ``main.py`` run on a wall-clock schedule, and auto-retries
``paused_model`` states so the run can proceed unattended.

Records every API request/response, command result, journal tail, and periodic
scenario snapshot to ``outputs/soak_driver_<ts>.jsonl`` for post-run review.
"""
from __future__ import annotations

import argparse
import json
import random
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Driver:
    def __init__(self, args) -> None:
        self.base = args.base.rstrip("/")
        self.deadline = time.monotonic() + args.duration_sec
        self.intent_interval = args.intent_interval_sec
        self.churn_interval = args.churn_interval_sec
        self.snapshot_interval = args.snapshot_interval_sec
        self.poll_interval = args.poll_interval_sec
        self.grid = args.grid
        self.rng = random.Random(args.seed)
        self.log_path = Path(args.log)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor = 0
        self.episode_id: str | None = None
        self.last_sim_time: float | None = None
        self.runtime_status = "unknown"
        self.focus_intent_id: str | None = None
        self.focus_revision: int | None = None
        self.action_seq = 0
        self.paused_retries = 0
        self.log("driver_start", {"args": vars(args)})

    # ---------- io ----------
    def log(self, kind: str, payload: dict) -> None:
        record = {
            "ts": utc_now(),
            "monotonic": round(time.monotonic(), 3),
            "kind": kind,
            "sim_time_min": self.last_sim_time,
            "runtime_status": self.runtime_status,
            **payload,
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

    def api(self, method: str, path: str, body: dict | None = None,
            timeout: float = 15.0) -> tuple[int, dict]:
        req = urllib.request.Request(
            self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except Exception:
                return exc.code, {"error_code": "unparsed", "message": ""}
        except Exception as exc:  # noqa: BLE001 - record and keep driving
            return 0, {"error_code": "transport_error", "message": str(exc)}

    def call(self, action: str, method: str, path: str,
             body: dict | None = None) -> tuple[int, dict]:
        code, payload = self.api(method, path, body)
        self.log("api", {"action": action, "method": method, "path": path,
                         "body": body, "status_code": code, "response": payload})
        return code, payload

    # ---------- command helpers ----------
    def wait_command(self, command_id: str, *, kind: str,
                     timeout: float = 120.0) -> dict:
        path = (f"/api/vessel-commands/{command_id}" if kind == "vessel"
                else f"/api/intent-commands/{command_id}")
        deadline = time.monotonic() + timeout
        last: dict = {}
        while time.monotonic() < deadline:
            code, payload = self.api("GET", path)
            if code == 200 and payload.get("status") not in (None, "queued"):
                self.log("command_result", {"cmd_kind": kind,
                                          "command_id": command_id,
                                          "result": payload})
                return payload
            last = payload
            time.sleep(1.0)
        self.log("command_timeout", {"cmd_kind": kind, "command_id": command_id,
                                     "last": last})
        return last or {"status": "timeout"}

    def refresh_episode(self) -> bool:
        code, payload = self.api("GET", "/api/intents")
        if code == 200 and payload.get("episode_id"):
            self.episode_id = payload["episode_id"]
            return True
        return False

    def poll_journal(self) -> None:
        path = f"/api/runtime/logs?after={self.cursor}"
        if self.episode_id:
            path += f"&episode_id={self.episode_id}"
        code, payload = self.api("GET", path)
        if code != 200:
            return
        items = payload.get("entries") or []
        for item in items:
            self.cursor = max(self.cursor, int(item.get("id", 0)))
            sim = item.get("sim_time_min")
            if isinstance(sim, (int, float)):
                self.last_sim_time = float(sim)
            self.log("journal", {"entry": item})
            if item.get("source") == "runtime":
                self.runtime_status = str(item.get("status"))
            if item.get("source") == "algorithm" and item.get("status") in (
                "mission_model_paused", "mission_model_retry_succeeded",
                "mission_model_retry_failed",
            ):
                status = item["status"]
                self.runtime_status = (
                    "paused_model" if status != "mission_model_retry_succeeded"
                    else "running"
                )

    # ---------- runtime control ----------
    def probe_retry(self, *, quiet: bool = False) -> None:
        """Use the retry endpoint itself as the paused/finished probe."""
        if not self.episode_id:
            return
        command_id = f"soak-retry-{uuid.uuid4().hex[:12]}"
        code, payload = self.api(
            "POST", "/api/runtime/retry",
            {"episode_id": self.episode_id, "command_id": command_id},
        )
        err = payload.get("error_code")
        interesting = code == 202 or err not in {"runtime_not_paused"}
        if not quiet or interesting:
            self.log("api", {"action": "runtime_retry_probe", "method": "POST",
                             "path": "/api/runtime/retry",
                             "body": {"command_id": command_id},
                             "status_code": code, "response": payload})
        if code == 202:
            result = self.wait_command(command_id, kind="intent")
            if result.get("status") == "applied":
                self.runtime_status = "running"
                self.paused_retries = 0
            else:
                self.paused_retries += 1
                self.runtime_status = "paused_model"
        elif err == "episode_finished":
            self.runtime_status = "finished"
        elif err == "runtime_not_paused":
            if self.runtime_status == "paused_model":
                self.runtime_status = "running"
            elif self.runtime_status in {"unknown", "finished"}:
                self.runtime_status = "running"
        elif code == 409 and err in {"replay_read_only"}:
            self.runtime_status = "finished"

    # ---------- operator focus area ----------
    def create_focus_intent(self, index: int) -> None:
        if not self.episode_id:
            self.log("focus_skipped", {"reason": "no episode_id"})
            return
        # Retire the previous driver-created focus area first.
        code, payload = self.api("GET", "/api/intents")
        if code == 200:
            for intent in payload.get("intents", []):
                if (intent.get("label", "").startswith("auto-focus-")
                        and intent.get("lifecycle") in {None, "active"}):
                    del_id = f"soak-intent-del-{uuid.uuid4().hex[:10]}"
                    dcode, dres = self.call(
                        "cancel_prior_focus", "DELETE",
                        f"/api/intents/{intent['intent_id']}",
                        {"episode_id": self.episode_id, "command_id": del_id,
                         "expected_revision": intent.get("revision", 1)},
                    )
                    if dcode == 202:
                        self.wait_command(del_id, kind="intent", timeout=60)
        side = self.grid  # cells per side of the square
        limit = 30 - side
        x0 = self.rng.randint(5, limit)  # keep off the mainland band (x<5)
        y0 = self.rng.randint(0, limit)
        command_id = f"soak-intent-{uuid.uuid4().hex[:10]}"
        body = {
            "episode_id": self.episode_id,
            "command_id": command_id,
            "label": f"auto-focus-{index}",
            "bbox": [x0, y0, x0 + side, y0 + side],
            "mode": "search_priority",
            "priority": "high",
            "weight": 1.0,
            "valid_duration_min": 480,
            "revisit_interval_min": None,
        }
        code, payload = self.call("create_focus", "POST", "/api/intents", body)
        if code == 202:
            result = self.wait_command(command_id, kind="intent")
            intent = result.get("intent") or {}
            if result.get("status") == "applied":
                self.focus_intent_id = intent.get("intent_id")
                self.focus_revision = intent.get("revision")

    # ---------- vessel churn ----------
    def scenario_vessels(self) -> list[dict]:
        code, payload = self.api("GET", "/api/scenario/vessels")
        if code == 200:
            return list(payload.get("vessels", []))
        self.log("vessels_fetch_failed", {"status_code": code, "response": payload})
        return []

    def churn(self, index: int) -> None:
        vessels = self.scenario_vessels()
        self.log("churn_begin", {"index": index, "inventory": vessels})
        if not self.episode_id:
            self.log("churn_skipped", {"reason": "no episode_id"})
            return
        by_class: dict[str, list[dict]] = {"type_i": [], "type_ii": []}
        for vessel in vessels:
            by_class.setdefault(vessel.get("vessel_class", ""), []).append(vessel)
        deletions: list[dict] = []
        for cls, items in by_class.items():
            if cls not in {"type_i", "type_ii"} or not items:
                continue
            count = max(1, len(items) // 2)
            deletions.extend(self.rng.sample(items, count))
        self.rng.shuffle(deletions)
        deleted_classes: list[str] = []
        for vessel in deletions:
            command_id = f"soak-del-{uuid.uuid4().hex[:10]}"
            code, payload = self.call(
                "delete_vessel", "DELETE",
                f"/api/vessels/{vessel['scenario_entity_id']}",
                {"episode_id": self.episode_id, "command_id": command_id,
                 "expected_revision": vessel.get("revision", 1)},
            )
            if code == 202:
                result = self.wait_command(command_id, kind="vessel")
                if result.get("status") == "applied":
                    deleted_classes.append(vessel["vessel_class"])
        # Re-place: recreate the same class mix at random water positions.
        for cls in deleted_classes:
            placed = False
            for attempt in range(8):
                position = [
                    round(self.rng.uniform(5.5, 28.5), 2),
                    round(self.rng.uniform(1.5, 28.5), 2),
                ]
                command_id = f"soak-new-{uuid.uuid4().hex[:10]}"
                code, payload = self.call(
                    "create_vessel", "POST", "/api/vessels",
                    {"episode_id": self.episode_id, "command_id": command_id,
                     "vessel_class": cls, "position_cells": position},
                )
                if code != 202:
                    break
                result = self.wait_command(command_id, kind="vessel")
                if result.get("status") == "applied":
                    placed = True
                    break
                if result.get("error_code") not in {
                    "invalid_position", "vessel_spacing_conflict",
                }:
                    break
            if not placed:
                self.log("create_vessel_failed", {"vessel_class": cls})
        # Toggle AIS on a random half of the controllable type II fleet.
        current = [v for v in self.scenario_vessels()
                   if v.get("vessel_class") == "type_ii" and v.get("ais_controllable")]
        for vessel in current:
            if self.rng.random() >= 0.5:
                continue
            command_id = f"soak-ais-{uuid.uuid4().hex[:10]}"
            code, payload = self.call(
                "toggle_ais", "PATCH",
                f"/api/vessels/{vessel['scenario_entity_id']}/ais",
                {"episode_id": self.episode_id, "command_id": command_id,
                 "expected_revision": vessel.get("revision", 1),
                 "ais_enabled": not vessel.get("ais_enabled")},
            )
            if code == 202:
                self.wait_command(command_id, kind="vessel")
        self.log("churn_end", {"index": index,
                               "inventory": self.scenario_vessels()})

    # ---------- snapshots ----------
    def snapshot(self) -> None:
        code, calls = self.api("GET", "/api/model-calls?limit=100")
        model_summary = {}
        if code == 200:
            for call in calls.get("calls", []):
                key = (call.get("role"), call.get("success"))
                model_summary[str(key)] = model_summary.get(str(key), 0) + 1
        code, intents = self.api("GET", "/api/intents")
        self.log("snapshot", {
            "vessels": self.scenario_vessels(),
            "intents": intents if code == 200 else {"error": intents},
            "model_calls_recent": model_summary,
        })

    # ---------- main loop ----------
    def run(self) -> None:
        wait_deadline = time.monotonic() + 120
        while not self.refresh_episode():
            if time.monotonic() > wait_deadline:
                self.log("abort", {"reason": "episode_id never became available"})
                return
            time.sleep(2)
        self.log("episode", {"episode_id": self.episode_id})
        self.snapshot()
        next_intent = time.monotonic() + self.intent_interval
        next_churn = time.monotonic() + self.churn_interval
        next_snap = time.monotonic() + self.snapshot_interval
        focus_index = 0
        churn_index = 0
        while time.monotonic() < self.deadline:
            self.poll_journal()
            now = time.monotonic()
            if self.runtime_status == "finished":
                self.log("finished_detected", {})
                break
            if self.runtime_status == "paused_model":
                self.probe_retry()
            elif now - getattr(self, "_last_probe", 0) >= 60:
                self._last_probe = now
                self.probe_retry(quiet=True)
            if now >= next_intent:
                focus_index += 1
                try:
                    self.create_focus_intent(focus_index)
                except Exception as exc:  # noqa: BLE001
                    self.log("focus_error", {"error": str(exc)})
                next_intent = now + self.intent_interval
            if now >= next_churn:
                churn_index += 1
                try:
                    self.churn(churn_index)
                except Exception as exc:  # noqa: BLE001
                    self.log("churn_error", {"error": str(exc)})
                next_churn = now + self.churn_interval
            if now >= next_snap:
                try:
                    self.snapshot()
                except Exception as exc:  # noqa: BLE001
                    self.log("snapshot_error", {"error": str(exc)})
                next_snap = now + self.snapshot_interval
            time.sleep(self.poll_interval)
        self.log("driver_end", {"episode_id": self.episode_id})
        self.snapshot()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://localhost:8765")
    parser.add_argument("--duration-sec", type=float, default=6 * 3600)
    parser.add_argument("--intent-interval-sec", type=float, default=3600)
    parser.add_argument("--churn-interval-sec", type=float, default=5400)
    parser.add_argument("--snapshot-interval-sec", type=float, default=300)
    parser.add_argument("--poll-interval-sec", type=float, default=15)
    parser.add_argument("--grid", type=int, default=5)
    parser.add_argument("--seed", type=int, default=20260930)
    parser.add_argument("--log", default="outputs/soak_driver.jsonl")
    args = parser.parse_args()
    Driver(args).run()


if __name__ == "__main__":
    main()
