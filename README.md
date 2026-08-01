# UAV Maritime Surveillance Scheduler

End-to-end simulation for dynamic maritime search, obstacle-aware fixed-wing
flight, SAR coverage, EO standoff tracking, and LongCat-driven task-region
allocation.

## Requirements

- Python 3.10+
- Node.js 20+
- `LONGCAT_API_KEY` in the process environment or ignored `configs/.env.local`
- A LongCat account with sufficient API quota

The scheduler is fail-closed. It never substitutes a mock or a rule-generated
region when LongCat is unavailable; the last validated LLM plan continues.
Do not commit API keys.

## Run

Start the persistent local console with one command:

```powershell
.\scripts\console.ps1 start
```

The launcher verifies LongCat-2.0 with a short live request before opening the
backend, keeps the backend alive after the simulation completes, records PID
files and logs under `.runtime/`, then starts Vite. Open `http://127.0.0.1:5173`.

```powershell
.\scripts\console.ps1 status
.\scripts\console.ps1 stop
```

When the project uses a non-default Python environment, select it explicitly:

```powershell
.\scripts\console.ps1 start -PythonPath C:\path\to\python.exe
```

A completed simulation writes one JSON object per frame to `outputs/simulation_*.jsonl`.

Useful CLI options:

```powershell
python main.py --steps 480 --step-delay 0.05
python main.py --steps 35 --no-server --step-delay 0
python main.py --steps 1 --hold-server --step-delay 0
python main.py --skip-llm-probe
```

## Verify

```powershell
python -m pytest -q
cd src/vis/frontend
npm run build
npm run test:acceptance
npm audit --audit-level=high
```

Browser acceptance expects the backend on `8765`, Vite on `5173`, and the
480-frame replay named in `tests/acceptance.spec.js`. See
`docs/VALIDATION.md` for the latest recorded acceptance run.

## Main Modules

- `src/env/dubins.py`: all six Dubins path families
- `src/utils/coverage_planner.py`: side-looking SAR boustrophedon coverage
- `src/utils/obstacle_avoider.py`: curvature-constrained RRT*
- `src/utils/track_orbit.py`: LGVF standoff tracking
- `src/schedule/task_allocator.py`: trigger, LLM, validation, and Hungarian flow
- `src/env/simulation.py`: integrated environment and mission lifecycle
- `src/vis/backend`: WebSocket, replay, config, and JSONL frame service
- `src/vis/frontend`: Canvas renderer and live/replay operations console
