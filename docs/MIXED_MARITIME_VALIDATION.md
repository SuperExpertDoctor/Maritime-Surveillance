# Mixed Maritime Validation

状态：实现已接入，真实 LongCat 评估需由具备 API 配置的操作者显式运行。

本文件对应 `feature/mixed-maritime-llm` 的混杂海上任务方案。旧版 GOAL/GOAL2
验收结果仍保留在 [VALIDATION.md](VALIDATION.md)，但不作为本方案的身份研判或
策略自演进证据。

## 自动化验证

测试默认使用 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`，避免环境插件改变结果。完整回归：

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python -m pytest tests/mission tests/control tests/schedule tests/env tests/utils \
  tests/test_runtime_configuration.py -q
```

fixture transport 只在测试目录注入，并明确标记为 fixture；生产 CLI 没有 mock 成功
开关。前端验证：

```bash
cd src/vis/frontend
npm run build
npx playwright test tests/mixed-maritime.spec.js
```

浏览器测试覆盖四方向框选、命令 queued 到 applied、回放只读和几何换算。完整旧版
acceptance 命令仍需在实际 WebSocket/API 环境中运行，不能用 fixture 结果替代。

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
| V18 | 全量 Python 回归与前端 build/Playwright | Python `1125 passed`；build 通过；mixed-maritime Playwright `3 passed` |

端到端 fixture 场景包括 `mixed-ais`、`all-civilian`、`silent-target`、
`disguised-target`、`island-confounder`、`no-resources`、`intent-overlap` 和
`model-failure`。这些场景验证管线行为，不证明真实模型分类准确率。

## Live Smoke

生产 smoke 使用默认 LongCat-2.0 四角色绑定，不注入 fixture：

```bash
python scripts/evaluate_mixed_maritime.py --scenario mixed-ais \
  --seed 42 --steps 120 --live \
  --output-dir outputs/evaluations/mixed-ais-live
python scripts/evaluate_mixed_maritime.py --scenario all-civilian \
  --seed 43 --steps 120 --live \
  --output-dir outputs/evaluations/all-civilian-live
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
