# Validation Record

Date: 2026-08-01

## Automated Checks

- Python regression: 93 tests passed.
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

## Eight-Hour Acceptance Run

Artifact: `outputs/simulation_20260801_002817.jsonl`

Model: `LongCat-2.0` through the real configured LongCat API.

| Metric | Result | Target | Status |
|---|---:|---:|---|
| Frames | 480 (1-480 min) | 480 | Pass |
| Heavy triggers | 30 | >=10 | Pass |
| Real model | LongCat-2.0 | User-selected LongCat-2.0 | Pass |
| LLM success rate | 30/30 (100%) | >=90% | Pass |
| Coverage at 4h | 58.8% | about 50-70% | Pass |
| Coverage at 8h | 81.1% (481/593 searchable cells) | about 80-95% | Pass |
| Ships detected | 10/10 | all | Pass |
| Region changes | 9 | >=5 | Pass |
| Track regions created | 3/3 groups | every group | Pass |
| Markers | 3 | >=2 | Pass |
| Per-UAV lifecycle cycles | minimum 3; all 10 complete 3 | >=3 each | Pass |
| Browser replay | Playwright passed, 4 viewports | nonblank/responsive | Pass |
| Dependency audit | 0 vulnerabilities | none high | Pass |

No mock or rule-generated model response was used. Every Heavy decision was a
real LongCat request, and every accepted response passed geometric, occupancy,
obstacle, scan-pattern, and region-count validation.

Coverage is unique coverage over currently searchable sea cells. Boundary,
designated land recovery cells, and no-fly obstacle cells are excluded from
the denominator; scanned obstacle cells cannot inflate the result.

## Land-Based Lifecycle Rotation

The primary base remains on land at `(15,28)`. The acceptance run uses the
configured coastal forward arming and refueling points, each designated as a
land cell. Once coverage passes 50%, the engine preplans real LongCat-approved
nearby search regions, flies collision-free Dubins returns to the shortest
reachable land site, refuels for the configured three-minute turnaround, and
then resumes the assigned SAR sortie.

No mock response, rule-generated region, artificial state transition, or
offshore base was used. A capacity-full empty plan is treated as a valid model
decision, so the client does not misclassify it as an LLM failure.
