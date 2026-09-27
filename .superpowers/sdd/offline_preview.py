"""Diagnostic preview only: real main/UI loop, deterministic offline model fixture."""
from functools import partial
from pathlib import Path
import sys
import tempfile
import json
import os

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import main as live
from scripts.evaluate_mixed_maritime import _FixtureGateway


class RecordingFixtureGateway(_FixtureGateway):
    def __init__(self, audit_path):
        super().__init__()
        self.audit_path = audit_path

    def set_context(self, episode_id, memory_version, sim_time_min):
        self.context = dict(episode_id=episode_id, memory_version=memory_version,
                            sim_time_min=sim_time_min)

    def request_json(self, **kwargs):
        result = super().request_json(**kwargs)
        call = self.call_log[-1]
        call.update(getattr(self, "context", {}))
        call["attempts"][0]["messages"] = [
            {"role": "system", "content": kwargs.get("system_prompt", "")},
            {"role": "user", "content": json.dumps(kwargs["user_payload"])},
        ]
        with self.audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**getattr(self, "context", {}),
                "role": kwargs["role"], "user_payload": kwargs["user_payload"],
                "call_id": result.call_id, "success": result.success}) + "\n")
        return result


if __name__ == "__main__":
    output = Path(tempfile.mkdtemp(prefix="maritime-live5h-offline-"))
    print(f"OFFLINE fixture preview, not provider-backed acceptance: {output}", flush=True)
    live.SimulationEngine = partial(live.SimulationEngine, seed=42,
        llm_gateway=RecordingFixtureGateway(output / "requests.jsonl"))
    live.main(
        steps=100000, port=int(os.environ.get("PREVIEW_BACKEND_PORT", "18766")),
        step_delay=1.0, probe_llm=False,
        wall_seconds=900.0, hold_server=True,
        memory_root=output / "memory", run_report_dir=str(output / "report"),
    )
