# 分区滚动覆盖：八项任务修改报告

日期：2026-09-20。分支：`feat/zone-aware-rolling-coverage`。起点：`02c6e6c399697aec6fcab4e43b87341050ebe876`。

状态：八项实现及确定性离线验收完成；未合并、未推送，真实模型和长期覆盖收益未验证。

## 本轮交付

在现有程序生成几何、模型选择任务、确定性配对链路上增加分区覆盖摘要和硬约束，不重写控制器，不训练模型。用户确认由本轮统一承担全部八项；合并和推送不在本轮已执行范围。

| 原任务 | 实现位置与结果 |
|---|---|
| 1 分区与摘要 | `src/mission/coverage_zones.py`：默认九区；固定海域、天气可搜索格、未扫、超期、在途并集和有效缺口；末行末列吸收余数，无固定海域格的区省略。 |
| 2 搜索预算与配额 | `coverage_policy.py`：`ceil(available × fraction)`，默认比例随缺口从 0.4 到 1.0；单次配额优先匹配，数量受可行槽位约束，不可行原因显式返回。 |
| 3 数据接口 | `contracts.py`：分区约束、不可行明细、快照摘要；新字段默认空，摘要不持有调用方的可变字典引用。 |
| 4 候选分散 | `CoveragePolicy.select_window`：按区轮转，跳过重叠继续寻找；无 zones 的旧路径保留。 |
| 5 调度接线 | `task_allocator.py` 和 `config.py`／`configs/mission.yaml`：窗口、摘要、比例、配额、可行边和快照接通；无 metrics 时不生成新摘要和约束。 |
| 6 硬约束与载荷 | `mission_scheduler.py`：普通搜索必须恰好达到预算，包括零预算；可行区配额和必选任务校验；摘要进入最终模型载荷，保留 100 KiB 限制。 |
| 7 提示词与说明 | `mission_scheduler.txt` 和 README 第 2 章：描述真实任务选择架构、预算、分区与降级；保留几何、航程、代际和抢占约束。 |
| 8 验收与回归 | `tests/mission/test_zone_rolling_acceptance.py`，以及原配置、覆盖、调度测试：真实十机接线、首步任务应用、完成事件重规划、过期重算、在途扣除、不可行降级与离线模型验证。 |

## 对原计划的修正

1. **重复匹配与分区串用**：配额阶段已匹配的任务不再在全局阶段重复扩增槽位；每区保留自己的候选和必选代表。一个任务连两架无人机只能计一个槽位，两区不同候选不会混用。另对全部 512 个三任务三无人机二分图与独立穷举结果比较匹配数量。
2. **必选任务冲突**：优先使用配额匹配出的代表作为全局必选集合；存在配额时不额外强塞一个不属于该可行组合的“最旧任务”，避免配额已占满预算后再多要求一个任务。没有配额时仍保留最早可匹配代表。
3. **候选轮转提前终止**：某一轮全部候选重叠时继续扫描后续候选；区内优先完整包含的矩形，避免跨区矩形遮挡本可建立的配额。保留原覆盖排名作为该优先级内的顺序。普通搜索预留不少于可用机数（受窗口容量限制），避免默认八个预留阻断十机九区首轮需求。
4. **错误测试几何与数量**：使用 30×30 地图内九区的合法矩形，增加第十个独立搜索；候选的可用机列表与边一致。十搜索通过，六核查加四搜索被拒绝；真实接线使用十机，零预算、多选、方向搜索不计数均独立测试。
5. **可行与不可行语义**：数量不足时返回实际匹配槽位及全局原因；全局不可行豁免精确数量和全局必选，但各可行区仍须满足自身配额。不可行区域放在 `zone_infeasible`，不要求模型虚构任务。阈值采用设计规定的 `>=`。
6. **完成事件缺失的实际接线**：`SimulationEngine._close_mission_task` 原来只写审计日志；日志不会自动进入 TriggerManager。本轮在记录真正变化时通知已有 `mission_task_released` heavy 事件，复用原去重和周期机制，不新增触发类型。
7. **离线模拟模型不能吞掉错误**：两个 fixture gateway 按新预算先满足搜索和配额，再考虑其他任务；构建过程允许暂时未填满，最终必须返回真实校验结果，不能把未达配额伪装成成功。零预算不再追加普通搜索。
8. **契约与测试兼容**：保留 `healthy_count` 关键字供旧调用方使用，但它不再参与新预算；空分区字段在模型载荷中省略。更新旧下限测试和配置字段清单；日志测试用 `caplog` 检查实际日志，避免 pytest 已捕获日志而 stderr 为空导致误报。
9. **旧核查场景的前置条件**：全域未扫时禁止用核查挤占首轮搜索，因此 V07 离线核查／交接场景显式初始化合成的新鲜 SAR，并发布 `validation_fixture_prepared` 说明；live 场景不做该处理。信息版本集成测试也显式设置已覆盖前提，保留原调查分配与提交断言。离线模型仅在搜索预算以外安排紧急任务，并保留零空闲机时的合法交接抢占路径；未降低分类、交接、EO 锁定等断言。

## 验收口径

- 首轮：十架空闲 UAV、全域缺口和合法几何／航路下，真实 allocator 产生十个搜索预算与九区配额；真实引擎首步应用十个普通搜索，各自分配不同 UAV。
- 在途：仅已批准／执行、已配对且 UAV 可工作的搜索类任务参与扣除；重叠 bbox 按格并集计算。普通搜索计数不包含方向搜索与调查，但它们的在途扫描区域可参与覆盖去重。
- 滚动：向真实 CoverageMetrics 输入 SAR 格，经过真实完成回调、事件触发、mission_step、模型校验和配对生成新快照；缺口下降后预算降低，到覆盖窗口边界时恢复。该测试是组件事件链验收，不冒充完整飞行架次。
- 安全：旧航路、代际、抢占、活动任务重叠等校验继续执行；没有通过修改安全阈值让测试通过。
- 模型：所有本轮功能验收采用确定性离线 gateway；未调用真实 LLM，未读取或复制 `.env`，没有声称真实模型稳定遵守新策略。

## 测试环境与证据

Git 的 HTTPS 端点不可连接时，通过 GitHub 官方 API 确认 main SHA，再从官方 codeload 下载同一提交。归档内 415 个文件逐一按 Git blob SHA 核验后补入对象缓存，随后成功建立工作分支；未修改全局 Git 配置或网络设置。

初始 Python 缺少 fastapi、uvicorn 和 openai：完整收集曾报两个导入错误，排除这两个文件后的基线尝试在 454 项通过、8 项缺 openai 失败后按预设 maxfail 停止，因此不存在“本地原版全绿”的结论。

用户批准后建立 `.venv-zone-tests`，复用现有科学计算包并仅在该环境安装缺失依赖。当前环境：pytest 8.3.4、NumPy 2.1.3、SciPy 1.15.3、PyYAML 6.0.2、fastapi 0.141.1、uvicorn 0.53.0、openai 3.16.2。原 Python 未修改。

| 最终检查 | 实际结果 | 证据文件（本地工作区 outputs） |
|---|---|---|
| `tests/mission tests/regressions tests/schedule` 完整回归 | **1189 passed，0 failed，2 warnings**，603.42 秒 | `zone-rolling-final-regression.xml` |
| 新增真实天气全阻挡补充用例 | **1 passed**；在完整回归收集完成后新增，单独运行，不混入上行计数 | `zone-rolling-weather.xml` |
| `tests/control/heuristic` | **126 passed** | `zone-rolling-heuristic.xml` |
| `tests/env/test_simulation_integration.py`，显式离线 gateway | **30 passed** | `zone-rolling-engine-offline.xml` |
| 新功能、配置、contracts、覆盖窗口、调度器和载荷专项 | **153 passed**，是主回归的子集，不重复累计 | `zone-rolling-final-focused.xml` |
| `git diff --check origin/main` | 通过 | 本地分支差异检查 |

主回归首轮为 1178 passed、4 failed：配置字段断言与三个核查／调查工作流前提不再符合新规则。修正后重新运行完整套件得到上表结果；首轮失败记录 `zone-rolling-regression.xml` 仍保留，不用局部重测冒充完整通过。

旧引擎套件直接运行曾有 29 项因未配置 API 密钥而在构造阶段终止，另有 127 项通过（其中 126 项为控制测试）。随后使用 `outputs/zone_rolling_offline_pytest.py` 显式注入 fixture gateway，完整的 30 项引擎测试通过。该插件不进入产品代码，不改变显式传入的 gateway，也不读取凭据。

本地证据目录：`C:/Users/98466/Documents/Codex/2026-09-16/https-github-com-superexpertdoctor-maritime-surveillance/outputs/`。测试环境由仓库内 `.venv-zone-tests` 提供。源码提交按可审查的功能分组：`d2f17d4`（分区／接口／预算）、`7099f89`（接线／校验／事件）、`ad10ab0`（测试与离线场景）、`3fae0c1`（提示词／README）、`0274652`（天气降级补充测试）；报告单独提交。

复现主回归：

```powershell
./.venv-zone-tests/Scripts/python.exe -m pytest tests/mission tests/regressions tests/schedule -q
```

复现额外旧引擎套件（在本地仓库目录运行，插件位于上述 outputs）：

```powershell
./.venv-zone-tests/Scripts/python.exe -c "import sys,pytest; sys.path.insert(0,'../../outputs'); raise SystemExit(pytest.main(['tests/env/test_simulation_integration.py','-p','zone_rolling_offline_pytest','-q']))"
```

## 尚未执行

真实模型端到端调用、长时间海域覆盖质量评估、主干合并和远端推送均未执行。本轮验证功能契约与确定性集成，不宣称长期覆盖率改善或真实模型成功率。
