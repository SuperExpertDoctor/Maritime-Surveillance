# Validation Record

Date: 2026-07-31

## Automated Checks

- Python regression: 84 tests passed.
- Frontend production build: Vite 8.2.0 build passed.
- Dependency audit: 0 vulnerabilities.
- Playwright browser acceptance: passed with local Chrome.
- Viewports inspected: 1440x900, 1280x720, 768x1024, and 390x844.
- Browser console/page errors: none.

The regression suite covers Dubins paths, continuous collision checks,
executable SAR scan patterns, moving-obstacle replanning, sensor phase rules,
candidate geometry, searchable-cell coverage, prompt constraints, and model
output validation. The browser suite covers live WebSocket rendering, nonblank
Canvas pixels, 480-frame replay, transport controls, keyboard navigation,
drawers, and responsive overflow.

## Eight-Hour Physical Baseline

Artifact: `outputs/simulation_20260731_061343.jsonl`

Log: `outputs/longcat-physical-acceptance-20260731_061342.log`

| Metric | Result | Target | Status |
|---|---:|---:|---|
| Frames | 480 (1-480 min) | 480 | Pass |
| Heavy triggers | 22 | >=10 | Pass |
| Real model | LongCat-2.0 | User-selected LongCat-2.0 | Pass |
| LLM success rate | 22/22 (100%, all first attempt) | >=90% | Pass |
| Coverage at 4h | 65.7% | about 50-70% | Pass |
| Coverage at 8h | 81.9% (494/603 searchable cells) | about 80-95% | Pass |
| Ships detected | 10/10 | all | Pass |
| Region changes | 13 | >=5 | Pass |
| Track regions created | 3/3 groups | every group | Pass |
| Markers | 3 | >=2 | Pass |
| Per-UAV lifecycle cycles | minimum 0; four UAVs completed one | >=3 each | Not met |

No mock or rule-generated model response was used. Every Heavy decision was a
real LongCat request, and every accepted response passed geometric, occupancy,
obstacle, scan-pattern, and region-count validation.

Coverage is unique coverage over currently searchable sea cells. Boundary
turn-clearance cells and no-fly obstacle cells are excluded from the
denominator; scanned obstacle cells cannot inflate the result.

## E8 Constraint Conflict

The base remains on land at grid coordinate `(15,28)`. With the specified
160 km/h cruise speed, real Dubins return paths, a 12-minute base refuel, and
20-40-cell search regions, forcing three returns per UAV inside 480 minutes
conflicts with the coverage target:

- a 60-minute forced-rotation trial reached only 27.0% coverage at 4 hours;
- a near-shore-first 75-minute trial reached 36.0% at 4 hours and 41.0% at
  5 hours, with only 6/10 ships detected at that point;
- the physically productive baseline reaches 65.7% at 4 hours and 81.9% at
  8 hours, but cannot complete three base turnarounds per airframe.

Those forced trials were stopped once they could no longer meet E5; they are
diagnostic artifacts, not acceptance results. E8 requires a requirement-level
change: a longer mission horizon, fewer mandatory cycles, or additional
land-based forward operating sites. The implementation does not fake refuels,
accelerate return legs, move the base offshore, or count incomplete cycles.
