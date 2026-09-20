# 持续覆盖验证矩阵与工程门槛

日期：2026-09-17。状态：**测试设计，所有新实现测试尚未执行**。本文件定义未来验收；不可引用它声称实现已通过。

上游：[设计](../superpowers/specs/2026-09-17-persistent-coverage-design.md)、[实施计划](../superpowers/plans/2026-09-17-persistent-coverage-implementation-plan.md)。

## 1. 统一约定

主指标：固定海域 A 上的实际 SAR 扫描并集，窗口 `(t-W,t]`；W∈{30,60,120}；主显示W=60。EO/历史信息值/计划足迹不计入。窗口未满时可显示但不加入稳态统计；暂停不推进仿真时间；未知不同于0。

正式报告同时给 fixed 和 dynamic，但以下新工程门槛全部使用 **fixed**，不能拿动态较小分母的数冒充。机队≥10、传感器参数与配置真实一致。每seed单列指标；不得仅以跨seed平均掩盖某次失效。

代码基线、transport、全部配置、seed、测量来源必须记录。fixture 验证闭环和软件性能；live 验证真实模型的选择质量和服务可用性，二者结论分开。

## 2. 契约用例：全部必须通过

| 编号 | 场景 / 输入 | 必须断言 | 任务 / 测试 |
|---|---|---|---|
| C01 | 4格域，SAR t0:A、t1:B/C、t59:B，EO t60:D | C60(60)=50%，C60(119)=0%，不是累计75/100% | T01/T02 coverage_oracle/metrics |
| C02 | 多UAV重复扫B、相同事件重复广播 | 面积只计一次，统计不依赖广播次数 | T02/T03 |
| C03 | t=60±1e-9、dt=.25/1/2、不连续时刻 | 左开右闭，按照时间而非数组最后60帧计算 | T02/T15 |
| C04 | 固定4格、天气从可行4格变2格 | fixed分母4不变，dynamic独立变化；超时格仍属A | T02/T03 |
| C05 | 空固定域、空动态域、非法shape/NaN/inf | counts0/null明确；非法输入拒绝；JSON无非有限数 | T02 |
| C06 | SAR后同格EO/AIS/ESM持续更新 | SAR到期仍掉出窗口，原I/V可更新 | T03 |
| C07 | 重复build_frame、include_matrices=False、episode reset | getter纯；新字段存在；新episode清零 | T03 |
| C08 | 旧replay无字段、未知schema、随机seek | 无假0，无跨帧累加，不从I反推 | T03/T14 |
| C09 | 配置15km/10km，反向/竖向/重规划扫描 | factory=1.5 cells，左右视与每条实际扫描线一致 | T04 |
| C10 | 请求SAR但安全层mask、实际误差>2°或横偏>0.20格 | 不成像、不刷新覆盖；日志真实误差 | T04/T05 |
| C11 | 空海区三bbox×两dt、初始逆向、转场→转弯→扫描 | 真实运动覆盖所有责任格，无teleport/穿障碍 | T05 |
| C12 | 静止进度/align停滞、map版本连续变化 | 最多2次重规划后失败；变版本不无限重置 | T06 |
| C13 | 长60min正常转场；天气更新但剩余路线安全 | 不误判停滞；route ready/revision正确 | T06 |
| C14 | 24格任务仅11格扫描且路线走完 | 45.833%且blocked，绝不是complete100 | T07 |
| C15 | 末格同tick成像、旧generation延迟事件、相同ID新航次 | 先采集再验收；旧事件不碰新任务；不借旧扫描完成 | T07 |
| C16 | 第37min NoSafeRecoveryPath后故障 | task/region/contact/probe/lease/base占用释放；failed不可重派 | T08 |
| C17 | 全域fresh/due/assigned/weather/geometry混合 | 五类责任互斥且并集=A；未入prompt不是geometry不可达 | T09 |
| C18 | 候选增删/天气变化/1–19格碎片/重复bbox | 地理身份与等待年龄不重置；残片可服务或明确暂缓 | T09 |
| C19 | >40 urgent、1000普通、部分无边、120几何预算 | 普通保留≥8、有老格；公平先于边筛；预算后继续队列 | T10 |
| C20 | 10机/4保底、可行边集中1机、受保护9机、抢占 | 下界按最大匹配得出；不凑假边、不抢保护任务、不自动补选；最老代表不能漏选 | T11 |
| C21 | 29.5/30s边界、Prompt压力、模拟错误输出 | 共用deadline、压缩不漏必需字段、过期结果不可安装、计时真实 | T12 |
| C22 | 2次timeout→第3次、HTTP402、paused重复100次、retry | 正确暂停/重试/幂等；无规则兜底；clock/fuel/coverage不推进 | T13 |
| C23 | 推送8→12%、30/60/120切换、0/null、暂停/断连/重连 | 无reload动态刷新，值与当前帧一致，主窗口切换面积同步 | T14 |
| C24 | 篡改SAR事件/footprint/汇总、提前结束、配置不匹配 | 独立oracle抓错；短跑不通过长跑门槛；不生成伪受控对照 | T15 |

额外回归：任务ownership、返航安全、probe识别、handoff、被动传感器、动态船舶编辑、Intent、地图拖拽、回放和前端console。C表不是削减原有测试的理由。

## 3. 系统门禁

### G01：确定性单元和模块集成

- 全部C01–C24具备对应测试，`python -m pytest tests -q` 无新失败。
- build_frame/raw扫描日志/独立oracle同一窗口counts精确一致、pct误差≤1e-9；serialized值不提前round。
- fixed域含所有原始可搜索海域，不能通过过滤难点来缩小；no_searchable_area只有真实空域时合法。
- 只在感知/模型边界使用mock；至少T05与G03的飞行路径、传感器足迹和覆盖计算为真实实现。

### G02：局部物理覆盖与故障回收

- 三类bbox（6×4、4×6、4×5）、水平/竖直/逆向、dt=.25/1，单任务在240min上限内有效扫描required全部格。
- 正常场景无长期align停滞；必须观察过真实turn/scan/非空footprint，而不是仅状态字符串。
- 所有search_complete任务按当次任务独立足迹复核覆盖率100%；无法完成的必须blocked且欠账保留。
- 注入故障场景failed机不被算成可用，不再占区；其它健康机可在之后模型批准的任务中接手。
- obstacle avoidance、R_min、速度、燃油/返航保护不退化。

### G03：开放海域长时覆盖能力（fixture）

**场景固定：** coverage-open-water；当前10 UAV、2基地、160km/h、15km SAR；0船、0岛、0雷暴；除此之外不改物理或基地规则。seeds=42,43,44,45,46；每seed运行720个仿真分钟。

每seed全部满足：

| 指标 | 门槛 |
|---|---:|
| t=480固定域累计SAR覆盖 | ≥80% |
| t=720固定域累计SAR覆盖 | ≥95% |
| t=240–720，C60时间均值 | ≥10% |
| 同区间C60的时间加权P5 | ≥5% |
| 同区间连续C60=0时长 | 0 min |
| 未解释丢失责任格 | 0 |
| 固定域曾扫但持续720min无再服务的格 | 单独报告，不得藏入未扫描集合 |
| emergency_failure或不可恢复controller_fault | 0 |

这些是工程目标。若当前平台/规划证明做不到，需要保留失败、分析路径效率/转场/资源分配并修复；不能在实施中默默改80→50或95→60。720min只是初轮全域覆盖与重访烟测；不等于证明无限时域的保证。

### G04：混合任务与天气（fixture）

coverage-mixed-weather，当前默认船舶/天气/传感器/10机配置，seeds=42–46、720min；禁止切到全coverage无目标替代这个门禁。

每seed：

- t480固定域累计SAR≥65%，t720≥85%。
- t240–720 C60时间均值≥7.5%，时间加权P5≥3%。
- 无静默永久占区、无被忽略的model pause、无成功任务漏扫；所有deferred天气/几何责任保留并可解释。
- 没有外部模型失败的fixture中不应paused_model；注入失败场景另外作为C22，不混入正常性能样本。
- 受控paired baseline在同配置同seed同fixture模型下，C60时间均值不得下降；至少4/5 seeds相对提升≥20%。若baseline=0，使用提升≥2个百分点代替相对比；配置不一致或baseline无法测量，该配对项标BLOCKED，绝对门槛独立报告。
- 旧场景classification/discovery/handoff仍通过既有契约；有非零分母的既有比率不比同配置paired baseline低超过2个百分点。若冲突，优化分配而非取消覆盖保底；分母为0标N/A并补相应具备非零分母的原有场景测试。

历史参考检查（不是受控算法门槛）：seed42 t177原始旧日志固定C60=53/671≈7.8987%、累计SAR=102/671≈15.2012%，故障新日志分别1/671≈0.1490%、11/671≈1.6393%。新混合seed42须达到这两个旧参照值或更高，且报告模型/船舶差异；达不到则明确尚未恢复该参照表现。禁止用旧动态8.20%当fixed7.90%直接对照。

### G05：真实帧的浏览器显示

- `npm run build`和全套Playwright通过；test:acceptance脚本只跑一文件，不能代替完整 `npx playwright test`。
- live生产frame至少连续推送3个不同仿真时刻，面板不reload而更新；浏览器收到帧到DOM更新P95≤500ms（本地、前台tab、固定测试机器，性能截图保留），不能将后台限速当应用失败。
- 新log seek t60/120/177/240/480/720数值与独立CSV相同；三窗口、面积、unseen/overdue一致。
- paused/断连明确标识，旧日志不显示伪0；重置不沿用上一episode指标。
- 1440×900和390×844截图无溢出遮挡；键盘切换可用；无console错误。

### G06：真实模型运行与可用性

在有真实模型运行授权且provider可用时，以G04相同配置、seeds及720min运行。参数、模型版本、调用量、token与各阶段时延保留。

- 必须满足G04绝对覆盖门槛以及C22的正确错误处理；不能用fixture数据填live缺口。
- 决策最终成功率≥95%，无超过总预算的结果被安装；成功决策P95≤配置30s，完整端到端准备耗时另报，不能宣称满足旧2s指标。
- 任一quota/auth/网络阻塞导致无法完成该seed，整组在线覆盖结论为BLOCKED，保留已完成时段；不得丢掉故障seed只平均成功seed。
- 正常live仍出现大量timeout/不满足覆盖目标为FAIL或外部依赖BLOCKED（明确证据），继续定位；正确暂停只是错误处理通过，不等于持续服务通过。

## 4. 统计与异常判定细节

1. 全部simulation samples按episode分组、sim_time排序；同一时刻内容相同的重复帧去重，内容冲突标失败（只runtime状态不同允许作为独立终态，不重复计面积）。
2. 采样均值用于复核旧审计；正式门槛的time mean=在相邻合法tick间以左端C值保持的时间积分/有效时长；门槛区间240–720严格切边。
3. weighted P5：将各区间值由小到大排列、累计区间时长达到总时长5%时的值；非独立样本不做虚假p值检验。
4. 缺tick不能隐式当作未扫描/全新鲜；如果有覆盖完整区间的原始扫描事件可复算则恢复，否则该区间unknown，相关长程门槛BLOCKED，不能缩短分母时间造好成绩。
5. 记录有效SAR机时来自真正成像，而不是status=searching。failed/returning/转弯不能算有效成像。
6. 搜索覆盖面积按格中心footprint栅格化估计，面积单位km²；不能声称网格整块都被连续空间的SAR波束完全成像。
7. 测量指标与调度策略分开：改变主显示窗口不改变已批准任务；前端按钮不会偷偷修改policy.primary_window_min。

## 5. 实际执行记录模板

真正实施时写 `docs/validation/persistent-coverage-execution.md`，每次更新带日期和git SHA：

```text
Task / Gate:
Command and working directory:
Exit code:
Transport: fixture | live | none
Config hash / seeds / actual simulation end:
Raw artifacts:
Metric measurement: native | legacy_reconstructed
Result: PASS | FAIL | BLOCKED | NOT_RUN
Evidence and remaining issue:
```

未执行就是NOT_RUN；环境缺依赖、模型缺额度就是BLOCKED；断言或性能不满足就是FAIL。最终报告同时列出功能实施是否完成、离线验证是否完成、在线验证是否完成，三个结果不能压成一个“全部完成”。
