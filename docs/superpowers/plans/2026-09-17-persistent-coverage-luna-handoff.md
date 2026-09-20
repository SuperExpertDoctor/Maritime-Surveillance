# Luna max 实施入口：持续覆盖与右侧实时指标

这是实施指令，不是“再写一份计划”的任务。当前交付只有设计文档；生产实现尚未开始。按下面顺序在真实代码中实施并验证。

## 必读文件

1. [设计与约束](../specs/2026-09-17-persistent-coverage-design.md)：尤其 §3 指标、§5 扫描、§6 完成验收、§7 保底、§8 模型失败。
2. [T01–T16 实施计划](2026-09-17-persistent-coverage-implementation-plan.md)：文件、签名、关键算法、测试命令。
3. [C01–C24 与 G01–G06 验证矩阵](../../validation/2026-09-17-persistent-coverage-validation.md)：不可自行降低门槛。
4. `outputs/diagnostics/coverage_audit_20260917/report.md`：原始问题证据，若outputs不在当前checkout，可用计划中的小基线，不必重新下载大日志。

## 可直接作为启动指令的文本

> 请实施 `docs/superpowers/plans/2026-09-17-persistent-coverage-implementation-plan.md` 中 T01–T16，遵循对应设计和验证矩阵。先检查工作树/AGENTS和HEAD差异，保留用户改动。逐任务执行：确认输入接口→写行为失败测试→最小实现→运行对应测试→检查与相邻任务接口→记录结果；全部功能完成后执行一次完整回归和规定长时验证。不要只做右侧面板，不要用front-end info_matrix反推新指标，不要把完成路径等同完成覆盖，不要用规则代理替代真实LLM生产决策。新指标必须来自真实SAR足迹，固定目标海域分母，支持30/60/120分钟，并随当前实时或回放frame更新。既有物理、安全、ownership、真值隔离和红方模型暂停契约不可绕过。离线fixture可用于确定性测试，真实模型运行必须标明transport与授权范围；模型不可用时记录BLOCKED，不伪造线上通过。每项证据写入 `docs/validation/persistent-coverage-execution.md`。出现计划与代码不一致，先核对设计目标并做最小适配，在执行记录中写出具体差异；不能无声删除任务或降低指标。

## 执行节奏

- 使用 `executing-plans` 串行推进，默认不启动多代理。T01–T03做指标底座；T04–T08修复真正扫描与任务结束；T09–T13调度/可靠性；T14界面；T15–T16系统验收。
- 每次上下文不足时，记录：已完成task、git SHA、未提交文件、最后测试命令/结果、下一失败断言、未完成gate。恢复后接着做，不从头改设计。
- 单项测试失败先查根因；保持真实失败样例。若拟改变公开schema、传感器参数、资源保底语义或验收数字，记录为设计变更并说明原因，不能当成普通实现细节自行绕过。
- UI优先权不得高于数据真实性：即使面板做完，后端还有漏扫/静默故障/真实模型未测，最终报告必须分别说明。

## 关键易错点

| 容易错的实现 | 正确要求 |
|---|---|
| 用coverage_pct显示“持续覆盖” | 新coverage_metrics独立SAR滑窗；旧值只叫累计观测覆盖 |
| 统计最近60帧 | 使用sim_time_min的最近60分钟，暂停不走时钟 |
| 用info>0或last_scan混合SAR/EO | 独立last_sar_scan_min，EO不能续SAR窗口 |
| 随天气缩小主分母 | fixed主域不变；dynamic另列 |
| 对已分配bbox涂满信息矩阵 | 只记录传感器真实有效足迹 |
| SAR全部right、扫幅默认2 | 使用各条带方向，生产扫幅来自实际配置1.5 |
| 走完路径就写100 | CoverageService检查本次generation的全部required格 |
| failed UAV仍transit且占区 | quarantine+释放绑定+failed排除调度 |
| 先筛40个几何边再谈公平 | 先预留普通覆盖候选，统一窗口后算边 |
| 后端自动补选4个区域满足保底 | 模型选择，validator校验可行下界，不绕过模型 |
| mock位姿/直接刷覆盖来过长时测试 | fixture只替代模型边界，运动/安全/sensor使用真实实现 |
| 重放缺指标就填0 | null和清楚的缺失提示 |
| 线上额度不足改用fixture继续算通过 | live BLOCKED；fixture结果另列 |

## 最终交付必须包含

- 实际代码和所有新增回归测试。
- 每个task的完成状态与验证证据；文档没有把尚未执行命令称为通过。
- 每seed的open-water/mixed长程覆盖指标、原始日志/CSV/独立复算结果。
- 右侧面板实时刷新、回放切换、暂停/缺失、桌面/移动截图。
- 本地功能/离线长时/live三种状态分开；未过门槛给出具体阻塞原因和下一步，不用“基本完成”掩盖。
