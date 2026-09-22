# Legacy Search Scheduling Compatibility 验收报告

## 1. 报告元数据

- 日期：2026-09-22
- 主模式：`software`
- 次模式：`research`（仅用于记录长时仿真和可复现实验结果）
- 项目阶段：搜索调度兼容性阶段验收
- 任务阶段：Task 9，最终回归、长时仿真、JSONL 审计和浏览器验收
- 仓库：`Maritime-Surveillance`
- 验收分支：`fix/legacy-search-scheduling-compatibility`
- 基线提交：`740fde483286cda0b6a2f8a12d54ca305996abcb`
- 已提交实现祖先：`fdd0e1b8504e7d8f4fb3c626860132e52130e1ae`
- 报告状态：最终验证已完成；Git 提交、合并和推送在本报告写入后执行。

本报告只记录本工作区实际执行的结果。fixture 和 live 证据独立统计；不会以 fixture 代替 live 验证。JSONL、截图和可复现本地运行环境位于 Git 忽略路径，未被纳入交付提交。

## 2. 依据与实现范围

- 计划 SHA-256：`1F3BE00F0484DEB9CA7B12CB7849FE6808DE2E68F5DA7D49E875C3336222C1EE`
- 设计 SHA-256：`F5F1C8905D70C14F7F3CCD50EC00692D887EF41126B93FC0AC450C585CFDDE45`

该阶段以前的提交已经建立了权威 search Region 生命周期、pending 保留和原子任务重新分配、JSONL/回放投影以及长时验证入口。本次最终修正完成以下兼容性边界：

- 健康 UAV 数量与当前 idle UAV 数量分离，覆盖下限以健康资源为分母。
- pending 搜索先确定性接手；仅真实剩余覆盖缺口可生成普通新搜索。
- 无真实缺口时跳过 LLM 选择；高优先级、intent 驱动和非搜索工作仍可进入模型路径。
- zone `must_service` 以合法路径、generation 和转场代价匹配，每个候选 Region 只能满足一个 zone 配额。
- 验证器和调度提示都采用覆盖下限语义，避免零剩余预算时误选普通搜索。
- 捕获脚本显式处理 UTF-8 Git 元数据；前端 search label 使用确定性布局、优先保留 pending，并增加 480 帧回放检查。

## 3. 验证命令与结果

| 门禁 | 实际结果 |
|---|---|
| `python -m compileall -q src scripts tests` | PASS |
| 聚焦调度/覆盖回归 | `137 passed, 1 warning in 139.89s` |
| 完整 Python 回归 | `1754 passed, 2 warnings in 1485.81s` |
| `npm run build --prefix src/vis/frontend` | PASS；Vite 8.2.0，1,512 modules |
| 通用前端 Playwright | `3 passed` |
| 专用 legacy 480 帧 Playwright | `1 passed (24.6s)` |
| `git diff --check` | PASS |

完整 Python 的两个 warning 是 Starlette/anyio 弃用提示和未注册的 pytest `timeout` 标记；没有失败断言。浏览器测试使用仓库内 portable Node/npm、锁定依赖和仓库 Chromium；关闭测试浏览器后 Vite 有 WebSocket 断开日志，但 Playwright 进程以成功状态退出。

## 4. 长时仿真与 JSONL 审计

每个目录均由 `scripts/validate_legacy_search_scheduling.py` 生成，再独立以 `--check-log <frames.jsonl>` 复核。全部运行跨过原第 70--100 分钟故障窗口，并完成全部请求步数。

| 目录 | seed / transport | 步数 | runtime | JSONL 审计 | 调度故障 | 模型故障 | 导航故障 | pending / 重分配 | 末端累计覆盖 |
|---|---|---:|---|---|---:|---:|---:|---|---:|
| `fixture-seed42-480-final-v2` | 42 / fixture | 480/480 | running | PASS，0 issues | 0 | 0 | 43 | 1 / 5 | 75.74% |
| `fixture-seed101-480-final-v2` | 101 / fixture | 480/480 | running | PASS，0 issues | 0 | 0 | 37 | 1 / 2 | 74.85% |
| `fixture-seed202-720-final-v2` | 202 / fixture | 720/720 | running | PASS，0 issues | 0 | 0 | 43 | 1 / 7 | 84.97% |
| `live-seed42-480-final-v2` | 42 / LongCat live | 480/480 | running | PASS，0 issues | 0 | 0 | 28 | 0 / 3 | 76.79% |

没有出现 `must_service` 不可满足、`overlapping_active_search` 或调度失败链条。live 运行共有 18 次模型调用、18 次成功、0 次失败；21 条尝试记录的 p50/p95/最大延迟分别为 5.27/37.70/40.43 秒，且第 70--100 分钟无模型失败。

代表性 SHA-256：

- `fixture-seed42-480-final-v2/manifest.json`：`C679274352D389949B8630494CC91631167F9EEAA3E43285B5DB11513699B8F0`
- `fixture-seed42-480-final-v2/audit.json`：`FD9F1ADC6D0F75796CC2AF1A05E6CD8141250EA8C663C6D0C1F5F9ECA4CB3A21`
- `fixture-seed101-480-final-v2/manifest.json`：`087313DBB625143D9A88B2A4CCB22A703F87DA6BB0AA859480E99EBBCBDF52D5`
- `fixture-seed202-720-final-v2/manifest.json`：`90E6175D4021F3AFC3769C39FB427FB5A4E5780A36E49EC7B300BE7EFA049170`
- `live-seed42-480-final-v2/manifest.json`：`4A70E103FE4DE6D00C62C37B8E68A355F8E6A1BBE70DB7BB57F29184807D2347`
- `live-seed42-480-final-v2/audit.json`：`5B9ADBF3293B361BA1C70863D1FBC5A2976134DD7890293AFCE07D8FCA72106E`

## 5. 480 帧浏览器验收与地图审查

专用回放测试验证真实 `frames.jsonl`、精确 480 帧、非空 canvas、minute 79、minute 120 和终点帧。它还验证同一 Region 在 assigned -> pending -> assigned 间保持 ID/bbox，不会由于抢占变成新的搜索区。截图位于：

- `outputs/validation/legacy-search-scheduling/fixture-seed42-480-final-v2/browser-evidence-480-final-v2/assigned-before.png`
- `outputs/validation/legacy-search-scheduling/fixture-seed42-480-final-v2/browser-evidence-480-final-v2/pending-search.png`
- `outputs/validation/legacy-search-scheduling/fixture-seed42-480-final-v2/browser-evidence-480-final-v2/assigned-after.png`
- `outputs/validation/legacy-search-scheduling/fixture-seed42-480-final-v2/browser-evidence-480-final-v2/minute-79.png`
- `outputs/validation/legacy-search-scheduling/fixture-seed42-480-final-v2/browser-evidence-480-final-v2/terminal-frame-480.png`

| 用户关心的能力 | 事实结论 | 证据与边界 |
|---|---|---|
| 整个任务区域的持续扫描覆盖 | 扫描、coverage 和 Region 生命周期机制已实现且可视化；全域持续覆盖未被证明。 | 截图显示 SAR 波束、轨迹、搜索区和覆盖面板。最终累计覆盖仅为 74.85%--84.97%，不能表述为 100% 全域持续覆盖。 |
| 目标的稳定跟踪 | 仿真和回放链路已实现并通过现有 EO/track/handoff 测试。 | 本次 legacy open-water 产物没有目标接触，不能据此证明现实海况下的稳定跟踪。 |
| AIS 信号的准确识别 | 仿真契约、接触融合和界面路径已通过回归。 | 没有带标注的真实 AIS 数据集，因此不能将仿真通过表述为现实 AIS 准确率已验证。 |

新增 label 布局使 pending Region 优先保留、标签保持边界内并避免彼此重叠；末端高密度场景会隐藏低优先级标签，而不是相互遮挡。

## 6. 导航与调度故障分离

调度/模型故障在四组最终运行中均为 0。导航故障分别为 43、37、43、28，代表性类别为 `no_safe_recovery_path`。这些事件没有被归因成调度故障，也没有通过弱化审计规则掩盖；它们是现有导航安全链的独立剩余问题。

因此，本报告的 PASS 仅覆盖旧版搜索区域调度兼容性计划所定义的调度、状态、回放和浏览器门禁；不等同于“整个海事项目已经没有任何 bug”。

## 7. 配置与安全边界

- live API key 只在运行进程环境中使用；未写入本报告、代码差异、提交或输出。
- `configs/.env` 未被 Git 跟踪；`.venv`、`.runtime`、`outputs` 和 `test-results` 均保持在仓库忽略路径。
- 未修改系统、conda、全局 pip/npm、共享目录或服务；没有删除文件。

## 8. 验收结论

| 范围 | 结论 |
|---|---|
| 搜索调度兼容性实现 | PASS |
| Python 单元/集成/完整回归 | PASS |
| fixture 480/480/720 长时仿真 | PASS |
| live LongCat 480/480 长时仿真 | PASS |
| JSONL 状态不变量审计 | PASS |
| 480 帧浏览器回放和通用前端回归 | PASS |
| 调度兼容性阶段 | PASS，具备提交、合并和推送条件 |

已知而未掩盖的范围边界：导航安全恢复仍有独立失败记录；本次没有证明全任务区 100% 持续覆盖、现实目标跟踪稳定性或现实 AIS 识别准确率。后续应将这些作为独立阶段的量化验收目标，而不是修改本阶段调度兼容性结论。
