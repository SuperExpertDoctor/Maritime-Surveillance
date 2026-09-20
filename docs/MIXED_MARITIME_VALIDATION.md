# Mixed Maritime Validation

> 术语已按 2026-09-16 统一：分类使用 I 类船舶/II 类船舶，运行时值使用 `type_i`/`type_ii`。

状态：需求对齐实现已接入；fixture 已验证，真实 LongCat 评估仍需由具备 API 配置的
操作者显式运行。

本文件记录当前混杂海上任务方案。旧版 GOAL/GOAL2 验收结果仍保留在
[VALIDATION.md](VALIDATION.md)，但不作为当前方案的身份研判或策略自演进证据。

## 需求对齐闭环

当前闭环为：观测事实 -> `EvidenceStore`/`InfoFieldDelta` -> 完整候选池与公平窗口
-> LLM 任务选择 -> 确定性 UAV 匹配 -> SAR/EO/被动新观测。被动单机事实只发布方位；
同一采样时刻同一辐射源和 burst 的两架不同 UAV 才发布真实位置。位置使用强度 1.0
点证据并生成 `investigation:<emitter>` 候选，避免与同组方位证据重复增益。

每个报告都必须显式携带 `transport`、fixture/live 标记、指标 numerator/denominator、
N/A 原因、规划延迟、边界违规、候选公平性、trace 断链和跨版本决策计数。fixture
报告不能声明真实模型质量达标。

## 自动化验证

测试默认使用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，避免环境插件改变结果。完整回归：

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest -q
```

fixture transport 只在测试目录注入，并明确标记为 fixture；生产 CLI 没有 mock 成功
开关。前端验证：

```bash
cd src/vis/frontend
npm run build
npx playwright test tests/mixed-maritime.spec.js
```

浏览器测试覆盖实时地图与传感器图层、480 帧回放、任务区域/模型日志/AIS/参数抽屉、
船舶编辑窗口和命令 queued 到 applied。fixture 运行由 Playwright 隔离后端和临时回放
目录；不能用 fixture 结果替代真实 API 验证。

## V01-V18

| 编号 | 自动化证据 | 状态 |
| --- | --- | --- |
| V01 | `tests/mission/test_ship_population.py`, `tests/mission/test_end_to_end.py` | 自动化回归 |
| V02 | `tests/mission/test_ais_generation.py`, `tests/mission/test_ship_population.py` | 自动化回归 |
| V03 | `tests/mission/test_red_commander.py`, `tests/mission/test_ais_discrimination.py` | 自动化回归 |
| V04 | `tests/mission/test_red_commander.py`, `tests/mission/test_failure_paths.py` | 自动化回归 |
| V05 | `tests/mission/test_ship_navigation.py`, `tests/utils/test_search_route_planner.py` | 自动化回归 |
| V06 | `tests/mission/test_ais_generation.py`, `tests/mission/test_visibility.py` | 自动化回归 |
| V07 | `tests/mission/test_contact_store.py`, `tests/mission/test_contact_assessor.py` | 自动化回归 |
| V08 | `tests/mission/test_contact_assessor.py`, `tests/mission/test_end_to_end.py` | 自动化回归 |
| V09 | `tests/mission/test_contact_release.py`, `tests/mission/test_end_to_end.py` | 自动化回归 |
| V10 | `tests/mission/test_probe_session.py`, `tests/mission/test_failure_paths.py` | 自动化回归 |
| V11 | `tests/mission/test_mission_scheduler.py`, `tests/mission/test_simulation_flow.py` | 自动化回归 |
| V12 | `tests/mission/test_contact_release.py`, `tests/mission/test_simulation_flow.py` | 自动化回归 |
| V13 | `tests/env/test_intent_api.py`, `tests/env/test_mixed_frame.py`, `src/vis/frontend/tests/mixed-maritime.spec.js` | 自动化回归 |
| V14 | `tests/mission/test_intent_store.py`, `tests/mission/test_intent_candidates.py` | 自动化回归 |
| V15 | `tests/mission/test_llm_gateway.py`, `tests/mission/test_failure_paths.py` | 自动化回归 |
| V16 | `tests/mission/test_end_to_end.py`, `tests/mission/test_outcome_evaluator.py` | 自动化回归 |
| V17 | `tests/mission/test_strategy_memory.py`, `tests/mission/test_strategy_validation.py`, `tests/mission/test_evaluation_cli.py` | 单元通过；live 未执行 |
| V18 | 全量 Python 回归与前端 build/Playwright | Python `1199 passed`；build 通过；Playwright `7 passed` |
| V19 | 被动/证据/版本/接力对齐 | `tests/mission/test_maritime_acceptance.py`、`tests/sensor/`、`tests/mission/test_information_update.py` |

端到端 fixture 场景包括 `mixed-ais`、`all-type_i`、`silent-target`、
`disguised-target`、`island-confounder`、`no-resources`、`intent-overlap` 和
`model-failure`。这些场景验证管线行为，不证明真实模型分类准确率。

## Fixture 与 Live Smoke

fixture 批量验收（不代表真实模型质量）：

```bash
python scripts/evaluate_mixed_maritime.py --config configs \
  --seeds 101,102,103,104,105 --repeat 3 \
  --output /tmp/maritime-alignment-fixture.json --transport fixture
```

本轮按上述配置完成了 120 个 120 步回合：120/120 完成，操作失败 0；边界违规、候选
公平性缺失、trace 断链和跨版本决策均为 0。规划延迟 p50/p95/max 为
0.051/0.094/5.297 秒；非故障样本超过 2 秒为 0。该报告保存在
`/tmp/maritime-alignment-fixture-final.json`，其中无资格的发现、分类和接力指标保持
`N/A`，连续观察率按真实分母计算，不把 fixture 结果包装成模型质量达标。

真实模型批量验收必须显式使用 `--transport live`，失败和 timeout 作为
`operational_failure` 保留，不能删除样本或以 fixture 结果替代。

生产 smoke 使用默认 LongCat-2.0 四角色绑定，不注入 fixture：

```bash
python scripts/evaluate_mixed_maritime.py --scenario mixed-ais \
  --seed 42 --steps 120 --live \
  --output-dir outputs/evaluations/mixed-ais-live
python scripts/evaluate_mixed_maritime.py --scenario all-type_i \
  --seed 43 --steps 120 --live \
  --output-dir outputs/evaluations/all-type_i-live
python scripts/evaluate_mixed_maritime.py --scenario disguised-target \
  --seed 44 --steps 120 --live \
  --output-dir outputs/evaluations/disguised-target-live
python scripts/evaluate_mixed_maritime.py --scenario island-confounder \
  --seed 45 --steps 120 --live \
  --output-dir outputs/evaluations/island-confounder-live
```

每个输出目录包含不可覆盖的 `manifest.json`，记录 episode、seed、schema、运行状态、
`episode_outcome` 和每个角色的调用计数/失败计数；不写入 API key、Authorization、
原始 prompt 或物理真值。模型故障会使回合无效，不会生成合成成功结果。

## Strategy Memory

无 `--live` 时只显示预算，不调用模型：

```bash
python scripts/validate_strategy_memory.py --memory-id M0001 \
  --phase validation --output-dir outputs/evaluations/memory-M0001-validation
```

真实成对验证默认运行 60 个 validation 和 30 个 holdout 回合，双方使用相同外生
seed 但分别调用对方模型，报告保存到 `outputs/strategy_memory/manifest.json`：

```bash
python scripts/validate_strategy_memory.py --memory-id M0001 \
  --phase validation --live \
  --output-dir outputs/evaluations/memory-M0001-validation
python scripts/validate_strategy_memory.py --memory-id M0001 \
  --phase holdout --live \
  --output-dir outputs/evaluations/memory-M0001-holdout
python scripts/validate_strategy_memory.py --activate M0001 --report-id REPORT_ID
python scripts/validate_strategy_memory.py --rollback baseline
```

未完成真实 validation/holdout，或任一阶段存在无效配对、分类指标缺失、误放目标增加、
终判覆盖率下降、score 未提升或效率未改善时，不得激活记忆，也不得声称策略提升。
