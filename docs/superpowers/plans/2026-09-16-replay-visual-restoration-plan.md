# 混合海上任务基础效果恢复与完整功能接入实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: 使用 `executing-plans` 按任务顺序执行；只有用户另行授权并行时才使用 `subagent-driven-development`。步骤用 `- [ ]` 跟踪。
>
> **状态：待用户审阅，禁止现在执行。** 本文是给 Luna max 的可交接计划，不是已完成测试报告。

**Goal:** 在不牺牲旧版搜索、飞行、传感器和回放基础效果的前提下，使当前已实现功能合理进入主链路，并可视化证明新增能力确实工作。

**Architecture:** 保留 LLM 任务选择、确定性合法性与配对、统一控制执行。修复会话/任务事务，补充可验证的资源利用规则，再以只读控制器快照贯通帧和 UI。所有已实现能力通过入口—状态—消费者—可视化—端到端证据矩阵验收。

**Tech Stack:** Python、pytest、dataclasses、现有导航/传感器；React、Canvas、Vite、Playwright；不增加依赖。

**Design:** [完整设计文档](../specs/2026-09-16-replay-visual-restoration-design.md)，尤其§14用户追加要求。两份文档共同批准才开始。

## Global Constraints

- G01：业务实现必须在用户批准本设计与实施方案之后开始。
- G02：唯一运动入口仍是 ControlCoordinator → SafetyEnvelope → UAVDynamicsExecutor；帧、渲染与测试不得旁路推进实体。
- G03：同一 episode 内不同探查会话 ID 不复用；同一探查续派保留 ID，不重置有效证据。
- G04：对外发布的每个 approved/executing 且有 assigned_uav_id 的 TaskRecord，必须对应当前有效控制任务及 generation；return/holding/refueling 不能再占用旧 probe。
- G05：正式任务选择仍由 LLM 完成；确定性模块只做可行性、约束校验及既有配对；失败不得静默代选。
- G06：展示数据只读、有限、按 episode/generation 隔离；读取和重复序列化不得推进路线、消耗随机数或触发规划。
- G07：搜索框、扫掠、SAR 足迹、EO 视场只能来自已提交任务与实际传感器状态；场景真值不得进入调度观测或接触分类。
- G08：原始三份 JSONL 不改写；老格式能回放，缺数据只做有依据的兼容，不补造航线或识别结果。
- G09：新测试必须分清静态渲染、真实引擎离线闭环、真实模型运行三类证据。
- G10：不改物理参数换取验收通过；新增依赖默认零，沿用 pytest、Playwright、React/Canvas。
- G11：每项已有业务能力必须在功能接入矩阵中有归属与通过证据；发现断链需修复，不能以“本次只是可视化”排除。
- G12：演示按“搜索基础 → 新线索 → 信息场/调度变化 → 调查识别 → 跟踪接力/恢复搜索”组织，新增功能不能使搜索、传感器、导航、返航基础功能失效。
- G13：展示“功能已工作”必须有因果链；宣称“性能更好”还必须有同场景/同预算配对实验，不能从更丰富的动画推导性能提升。

## 0. Luna 执行协议与任务依赖

先读设计全文及本节，再读所执行任务，不能只读任务标题。每次仅处理一个任务；每项都有独立失败测试、最小修改、验证与交接记录。不要把16个任务一次性交给一个不可审查的大补丁。

每任务通用完成步骤（各任务都必须执行）：

- [ ] 阅读列出的文件与调用者；记录当前签名与设计差异。
- [ ] 写所列失败测试并运行，记录确切失败原因，排除导入/环境故障假红。
- [ ] 按“修改逻辑”实施，使用现有类型和模式，禁止顺带重构。
- [ ] 运行该任务命令并记录退出码/通过数；执行与本次更改直接有关的现有回归。
- [ ] 检查 diff、更新功能矩阵与执行记录；一个任务一个提交，只加入本任务文件，不 `git add .`。
- [ ] 交接：接口变化、已通过命令、产物路径、未解决项、下游风险。未过门不勾选完成。

如果现有接口改变，先在文档中修正事实与计划引用，再实施等价的小改；若影响已批准行为或测试门槛，停在该项补充设计，不擅自降低要求。

依赖顺序：

```text
T00 基线与功能清单
 → T01 会话编号/事务 → T02 生命周期
 → T03 调度约束 → T04 网关与测试模型适配
 → T05 路线契约 → T06 控制器导出/真实导航 → T07 帧与传感器
 → T08 目标/标签/阶段 → T09 回放/事件/API
 → T10 引擎场景runner
 → T11 感知/信息/意图/动态船舶接入
 → T12 跟踪/安全/基地/接力接入
 → T13 Reviewer/评估/记忆/扩展接口接入
 → T14 浏览器与视觉回归
 → T15 长跑、完整验收及交付
```

T11—T13不是可选审查：已正确接入则提交覆盖证据；有断链则在对应任务修复并新增回归。每个功能单独提交，禁止把“全部已有功能接入”变成没有边界的改造。

### 固定产物

- `docs/validation/replay-restoration/feature-integration-matrix.md`：设计§14的全部ID，逐项实证。
- `docs/validation/replay-restoration/execution-log.md`：每项red/green、HEAD、命令、结论。
- `docs/validation/replay-restoration/acceptance-report.md`：最终报告。
- `outputs/validation/replay-restoration/<run-id>/`：隔离输出，内含manifest、frames.jsonl、metrics.json、events.jsonl、截图、角色日志/错误摘要。不得清空outputs根目录。

## T00：固定基线、逐项核对已有功能

**Files:** 读 `README.md`、设计§14全部源文件；创建上述两个validation文档；新建 `tests/mission/replay_restoration_helpers.py`。

**Consumes:** 原始3份日志与设计assets中的SHA。**Produces:** 完整功能矩阵；测试helper `probe_batch(engine, count=6) -> AssignmentBatch`，供T01/T02复用。

- [ ] 运行 `git status --short`、`git rev-parse HEAD`，核对基线变化，不覆盖用户改动。
- [ ] 为B01—B09、I01—I14、E01、C01逐项填唯一运行入口与消费者；不能仅填模块路径。读 `SimulationEngine.step` 和 `TaskAllocator.mission_step` 实际顺序，核对是否在新版mission路径执行。
- [ ] 保存red基线：原日志统计、现有视觉截图；对原文件流式计算SHA，和baseline-manifest对比。
- [ ] 在固定浏览器与受控相同frame集合上记录改前500次render/构帧P50/P95、原始耗时数组、环境版本和JSON字节；保存到隔离baseline目录。T15必须用同一输入测新实现，不能只保存截图后声称有性能基线。
- [ ] 实施前运行：

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_mission_scheduler.py tests/control/test_simulation_ownership.py tests/env/test_mixed_frame.py tests/vis/test_replay_adapter.py -q
```

预期输出为现有测试结果，不预先承诺全通过；已有失败记录到独立表。offline-test不是有效凭据，测试仍必须注入fake gateway，禁止实际联网。

- [ ] 新helper完整核心代码如下，不在生产路径使用：

```python
from src.mission.contracts import Assignment, AssignmentBatch

def probe_batch(engine, count=6):
    snapshot = engine.allocator.build_mission_snapshot(engine.clock.time)
    generations = dict(snapshot.uav_generations)
    used = set()
    items = []
    for candidate in snapshot.candidates:
        if candidate.kind != "probe":
            continue
        edge = next((edge for edge in snapshot.feasible_edges
                     if edge.task_id == candidate.task_id and edge.uav_id not in used), None)
        if edge is None:
            continue
        used.add(edge.uav_id)
        items.append(Assignment(candidate.task_id, edge.uav_id,
                                generations[edge.uav_id], None))
        if len(items) == count:
            break
    assert len(items) == count, "fixture cannot build the requested independent probe batch"
    return AssignmentBatch(snapshot.snapshot_id, tuple(items), "fixture-probe-batch")
```

**Gate:** 矩阵所有ID有入口定位，未验证项明确；helper不改变引擎。提交 `docs: capture replay restoration baseline and integration inventory`。

## T01：修复批量探查编号、续派与失败回滚

**Files:** 修改 `src/env/simulation.py::apply_assignment_batch`、`src/schedule/state_manager.py`；必要的窄contact事务支持放 `src/mission/contact_store.py`；测试 `tests/mission/test_simulation_flow.py`、`test_contact_store.py`、`tests/schedule/test_state_manager.py`。

**Consumes:** T00 probe_batch。**Produces:** 批内独立session；失败不耗编号；同会话可更新/续派但不被其它任务覆盖。

- [ ] 先写直接复现测试（原文件 `_engine()` 使用离线失败网关，测试这里只提交不step）：

```python
from tests.mission.replay_restoration_helpers import probe_batch

def test_batch_probe_ids_are_unique_and_sessions_match_tasks():
    engine = _engine()
    batch = probe_batch(engine)
    assert engine.apply_assignment_batch(batch)
    tasks = [engine.control_coordinator.active_task(a.uav_id) for a in batch.assignments]
    assert len({task.probe_id for task in tasks}) == 6
    sessions = {p.probe_id: p for p in engine.allocator.sm.get_probe_sessions()}
    assert len(sessions) == 6
    for assignment, task in zip(batch.assignments, tasks):
        probe = sessions[task.probe_id]
        assert (probe.uav_id, probe.contact_id) == (assignment.uav_id, task.target_contact_id)
        contact = engine.allocator.sm.contacts.snapshot(task.target_contact_id)
        assert (contact.assigned_uav_id, contact.active_probe_id) == (assignment.uav_id, task.probe_id)
```

- [ ] `python -m pytest tests/mission/test_simulation_flow.py::test_batch_probe_ids_are_unique_and_sessions_match_tasks -q`。预期修复前FAIL（唯一ID=1），修复后PASS。
- [ ] 按设计§5局部预分配counter；复用旧task仅限task_id/contact/uav都一致且probe存在；commit结束写回一次；删除循环内旧递增。
- [ ] `set_probe_session` 校验已有归属；同归属阶段更新允许，跨归属 `ValueError("probe_id_owner_conflict")`。接触合并显式重绑定，并验证alias关系。
- [ ] 续派分支保留完整旧session，不重新生成started_at/sample集合。检查handoff验证在实际安装之前完成。
- [ ] 用 monkeypatch coordinator 的 assign_tasks_atomically 在安装前抛异常；断言counter、leases、contact reservation/revision/cooldown、sessions、records、成功事件与之前一致。另用第二个非法assignment验证prepare失败，禁止只测第一项失败。
- [ ] 增加：跨批ID不复用；10项；同ID更新；同ID异owner拒绝；续派保留阶段；过期snapshot/地图/generation拒绝；重复提交拒绝。

```bash
python -m pytest tests/mission/test_simulation_flow.py tests/mission/test_contact_store.py tests/schedule/test_state_manager.py tests/mission/test_handoff.py -q
```

**Gate:** 6任务=6独立会话，失败无部分成功。提交 `fix: keep probe sessions unique across atomic assignments`。

## T02：任务释放与控制/资源状态同步

**Files:** `src/env/simulation.py`、`src/schedule/state_manager.py`、`task_allocator.py`、`trigger_manager.py`；按实际入口最小调整 `src/control/heuristic/task_flow.py`。测试新建 `tests/mission/test_mission_task_lifecycle.py`，回归 `test_contact_release.py`、`test_dynamic_vessel_lifecycle.py`。

**Consumes:** T01唯一会话。**Produces:** 设计§6 `_close_mission_task` 幂等入口与无幽灵snapshot。

- [ ] 用T01的批次，触发单机holding，检查该机旧record不再assigned/executing；其他5任务不变。先保存这项失败证据。
- [ ] 在捕获旧active_task后再执行控制权切换；终态仅关闭旧任务，不能关闭已接替的新task。完成/阻塞/取消映射严格按设计表。
- [ ] 断言辅助函数（测试文件内）核心：

```python
def assert_owned_records(engine):
    for record in engine._mission_task_records.values():
        if record.status not in {"approved", "executing"} or record.assigned_uav_id is None:
            continue
        task = engine.control_coordinator.active_task(record.assigned_uav_id)
        assert task is not None
        assert task.task_id == record.task_id
        entity = next(u for u in engine.uavs if u.id == record.assigned_uav_id)
        assert entity.status not in {"holding", "returning", "refueling"}
```

- [ ] 增加generation/contact/session交叉断言；断言helper不能用于生产自我修补。
- [ ] 参数化触发：holding、probe_blocked、approach_timeout、probe_timeout、fuel_return、type_i_released、target_lost、vessel_removed、duplicate_task_cancelled、refuel_reset；每项assert所有权、session清理、record终态、下一snapshot。
- [ ] 搜索抢占保留region与已完成度、assigned=None；正常probe→track不清新track归属；合并只保留canonical合法owner。
- [ ] 释放/加油事件去重；protected实体不能出现在available；generation0 idle仍可用；下一个允许的调度周期感知变化。

```bash
python -m pytest tests/mission/test_mission_task_lifecycle.py tests/mission/test_contact_release.py tests/mission/test_dynamic_vessel_lifecycle.py tests/control/test_simulation_ownership.py tests/control/heuristic/test_task_flow.py tests/schedule/test_trigger_manager.py -q
```

**Gate:** 状态切换后snapshot、entity、coordinator、contact、record一致。提交 `fix: close mission records when control ownership changes`。

## T03：有可行增补任务时拒绝漏派

**Files:** `src/mission/mission_scheduler.py`、`src/mission/prompts/mission_scheduler.txt`；测试 `tests/mission/test_mission_scheduler.py`、`test_prompt_window.py`。

**Consumes:** T02可靠资源快照。**Produces:** 非递归基础校验+增补校验；公开validate与scheduler方法一致；错误码 `underutilized_feasible_work:<task_id>`。

- [ ] 在现有scheduler测试中复用 `_snapshot/_task/_resource/_edge/_selection`，写最小失败测试：

```python
def test_reason_does_not_allow_empty_selection_with_idle_feasible_work():
    snapshot = _snapshot([_task("S1")], [_resource("U1", generation=0)],
                         [_edge("S1", "U1", 1.0)])
    errors = validate_selection(_selection(snapshot, [], defer="no feasible edges"), snapshot)
    assert "underutilized_feasible_work:S1" in errors

def test_partial_selection_must_add_independent_idle_work():
    snapshot = _snapshot([_task("S1"), _task("S2")],
                         [_resource("U1"), _resource("U2")],
                         [_edge("S1", "U1", 1.0), _edge("S2", "U2", 1.0)])
    errors = validate_selection(_selection(snapshot, ["S1"]), snapshot)
    assert "underutilized_feasible_work:S2" in errors
```

- [ ] 将现有几何/代价/资源校验抽成不含增补规则的内部函数；保持原public返回tuple错误兼容。
- [ ] 确定性排序可见未选候选；为S+t执行基础校验及完整匹配；只有可增加真实idle投入且不引入新preemption才报告；找到首个witness立即停止。不能固定当前一次配对后仅看剩余边，要允许本批重匹配。
- [ ] 给方法 `validate_selection(..., *, visible_task_ids: frozenset[str] | None = None)` 增可选关键字；模块级函数同样支持，默认全snapshot候选。`decide`使用本次payload得到的固定visible集合，不从可变last payload读取，避免跨调用污染。
- [ ] 只允许selected_task_ids来自可见候选或已有合法续派记录；新增 `selected_task_not_visible:<id>`，不能选择隐藏候选绕过prompt。
- [ ] 必测：6probe+3search可完整配9；6-only拒绝；资源少于候选允许部分；无边空选合法；protected/cooldown/燃油不强派；重叠区不可增补；重匹配才可加任务；隐藏候选不拒绝可见最大选择；新规则不改变现役任务。
- [ ] 更新以前“给理由便空选合法”的测试为新明确规则，不删除测试或仅改预期掩盖失败。

```bash
python -m pytest tests/mission/test_mission_scheduler.py tests/mission/test_prompt_window.py tests/mission/test_intent_candidates.py -q
```

**Gate:** 合法选择无增补witness，未可见任务不制造无法满足的约束。提交 `fix: validate work conserving mission selections`。

## T04：接通纠错、失败可见性和离线模型边界

**Files:** `src/mission/mission_scheduler.py`、必要时 `llm_gateway.py`、`src/schedule/task_allocator.py`；测试 `test_llm_gateway.py`、`test_failure_paths.py`；修改 `scripts/evaluate_mixed_maritime.py::_FixtureGateway` 的测试模式策略。

**Consumes:** T03错误码和visible集合。**Produces:** 有界纠错轨迹；失败事件 `mission_selection_failed`；离线模型不会因永远只选1项而失效。

- [ ] 真实LLMGateway+ScriptedTransport返回“漏派→完整合法”两次JSON；断言第一次validator错误进入第二次请求、第二次成功提交一次。
- [ ] 持续非法、transport timeout、decision deadline、迟到snapshot：均无新任务；保护现役任务；错误包含来源和原因，不静默固定选择。
- [ ] `mission_selection_failed`数据：snapshot_id、failure_category、error_codes、available_count；不包含凭据。Runtime继续/暂停沿用现有各role策略，不顺带统一红方和蓝方失败语义。
- [ ] 当前 `_FixtureGateway` 只选一个候选。离线模式改为从可见候选构建合法不可增补集合：逐项尝试，错误若**仅**为underutilized可继续扩展，基础错误则不接纳；循环最多候选数轮，最终validate必须空错误。生产gateway不得调用这段代码。
- [ ] fixture assessor仍默认unknown；专门分类场景显式提供有合法sample IDs的回答，不把真实类别硬塞默认gateway。

```bash
python -m pytest tests/mission/test_llm_gateway.py tests/mission/test_failure_paths.py tests/mission/test_evaluation_cli.py tests/mission/test_mission_scheduler.py -q
```

**Gate:** 固定策略仅在显式fixture transport；attempt上限和deadline未放宽。提交 `fix: surface mission validation failures and adapt fixture selections`。

## T05：定义只读路线快照与生命周期

**Files:** `src/control/common/contracts.py`、`base.py`、`coordinator.py`、`src/schedule/state_manager.py`；新测 `tests/control/test_route_snapshot.py`。

**Consumes:** 现有Pose/ControllerBase、episode/generation。**Produces:** 设计§8两个冻结类型，默认route_snapshot方法，state发布/读取。

- [ ] 新类型用dataclass(frozen=True)，构造校验有限pose、0<=next_index<=len、revision>=0、合法status、非负generation；转换nested tuples防外部list共享。
- [ ] 声明完整接口（按这些名称供后续调用）：

```python
# src/control/common/base.py，默认不要求学习控制器实现
# ControlRouteSnapshot 从 contracts 导入。
def route_snapshot(self) -> ControlRouteSnapshot | None:
    return None

# StateManager 的新增公共接口：
# set_control_route(uav_id: str, snapshot: UavRouteSnapshot) -> None
# get_control_route(uav_id: str) -> UavRouteSnapshot | None
# clear_control_routes() -> None
# Coordinator.route_snapshot(uav_id: str) -> UavRouteSnapshot
```

Coordinator默认无控制器时返回显式cleared；有controller但不支持时unavailable；episode来自当前StateManager.episode_id、generation来自当前lease，不由controller自报。

- [ ] state setter拒绝同episode较旧generation；reset清空后可接纳新episode；相同generation只接受非回退revision或显式cleared，cleared不能被旧revision复活。合法新任务通过generation变化开启新生命周期；不用时间戳代替generation。
- [ ] 只读测试：连续两次snapshot相等，controller follower.index/entity position/RNG state不变；试改元组失败；无provider不被强制启用。
- [ ] 明确None只表示“旧调用链未提供缓存”，unavailable/cleared必须是有包络的快照。

```bash
python -m pytest tests/control/test_route_snapshot.py tests/control/test_base_classes.py tests/control/test_coordinator.py tests/control/test_ownership.py -q
```

**Gate:** 获取快照零规划/零运动，过期缓存无法覆盖新任务。提交 `feat: expose immutable controller route snapshots`。

## T06：各控制器导出真实路线并验证调查导航

**Files:** `src/control/heuristic/{base,coverage,probe,tracking,return_to_base}.py`；测试各对应 `tests/control/heuristic/test_*.py`，新 `tests/mission/test_probe_navigation_integration.py`。

**Consumes:** T05类型。**Produces:** coverage/probe/track/return/holding的route_snapshot实现；probe首次接近路线。

- [ ] RouteFollower.next_index转换完整核心示例（置于导出方法，不改变follower）：

```python
def next_route_index(follower):
    if follower is None:
        return 0
    if follower.is_complete:
        return len(follower.poses)
    return min(follower.index + 1, len(follower.poses))
```

- [ ] Coverage导出真实self.route、follower、phase、map_version；记录重规划revision；已有路线规划只发生在start/act现有路径。
- [ ] Probe.start_task利用当前contact observation规划baseline路线；session还没发布时也可计划，不能为了展示调用act。后续phase与contact变化按原逻辑更新revision。缺contact明确unavailable并保持原安全处理。
- [ ] Tracking选择当前实际使用的avoidance follower或approach follower；LGVF稳态标guidance_only。Return/Holding分别检查实际内部属性，不能统一读不存在的 `_route`。
- [ ] 停止/重分配/地图失效导出cleared/pending/unavailable，不能旧route永远ready。
- [ ] 真实导航集成：V04起点/目标不在同一水平线上，推进真实executor；断言距离总体下降、实际航向改变、有限坐标、未穿障、最终进入正确距离。单位测试NavigatorSpy结果不能替代。
- [ ] 若V04揭示反复重规划导致不收敛，先保存trace、规划次数和距离曲线，再单独修复已证实原因；允许保持未失效剩余路线/按位移阈值重规划，但参数变化需写入设计补充，不能拍脑袋加大timeout。

```bash
python -m pytest tests/control/heuristic/test_coverage.py tests/control/heuristic/test_probe.py tests/control/heuristic/test_tracking.py tests/control/heuristic/test_navigation.py tests/mission/test_probe_navigation_integration.py -q
```

**Gate:** 确实可用的路径才ready，传感器与接近阶段不混淆，真实导航测试通过。提交 `feat: publish authoritative heuristic mission routes`。

## T07：路线、阶段、传感器进入直播与JSONL

**Files:** `src/env/simulation.py`、`src/vis/backend/frame_builder.py`、`frame_publisher.py`、必要时server；测试新 `tests/env/test_route_visual_frame.py`，回归 `test_mixed_frame.py`、`test_frame_publisher.py`。

**Consumes:** T05/T06快照。**Produces:** `visual_schema_version`、task_visual及兼容路线字段；首个成功提交帧有权威任务信息。

- [ ] 提交成功/每tick/释放/reset后发布快照到state；live与logger共用build_frame处理，不各写一套。
- [ ] 纯采样函数放frame_builder（或文件过大时同目录小helper），完整核心：

```python
def sample_route_overview(poses, limit):
    if limit < 2:
        raise ValueError("route overview limit must be at least two")
    poses = tuple(poses)
    if len(poses) <= limit:
        return [list(pose) for pose in poses]
    indices = [round(i * (len(poses) - 1) / (limit - 1)) for i in range(limit)]
    return [list(poses[index]) for index in indices]

def remaining_route(position_pose, poses, next_index, limit):
    if limit < 1:
        raise ValueError("remaining route limit must be positive")
    return [list(position_pose), *[list(p) for p in poses[next_index:next_index + limit - 1]]]
```

只对ready且有剩余route调用remaining_route；cleared/unavailable输出空，guidance_only不得变造预计路径。单点重复可去重但测试首点必须当前pose。

- [ ] 测1000点路径index=10：第二点必须route[11]，不能route[-100]；首尾概览保留；realtime/replay点数上限分别断言。
- [ ] 过期缓存、显式空、历史缺字段、无entity、reset都测；旧实体只有在无新缓存时fallback。
- [ ] `transit_progress`无合法边界返回None；修正“无路=100%”。
- [ ] SAR/EO使用真实采集/切换后状态；待机beam与imaging区分，覆盖只来自实际ScanRefresh；构帧不能更新传感器。

```bash
python -m pytest tests/env/test_route_visual_frame.py tests/env/test_mixed_frame.py tests/env/test_frame_publisher.py tests/mission/test_sensor_modes.py tests/env/test_sensors_and_obstacles.py -q
```

**Gate:** 相同引擎状态live/replay语义相同（只允许采样密度不同），render重复不影响执行。提交 `fix: serialize current routes and truthful sensor phases`。

## T08：恢复目标、任务阶段与标签的视觉可读性

**Files:** `src/vis/frontend/src/renderer/layers.js`、新 `displayState.js`、新 `labelLayout.js`；`App.jsx`、`components/CanvasMap.jsx`、`RightSidebar.jsx`、`ContactPanel.jsx`；测试新 `tests/replay-restoration.spec.js`。

**Consumes:** T07 task_visual、原contacts/scenario_vessels。**Produces:** 单一阶段映射、中性观测船形符号、场景层开关、确定性标签布局。

- [ ] 新纯函数接口：`uavDisplayState(uav) -> {label, tone, phase}`；`layoutLabels(labels, bounds) -> [{id,x,y,width,height,anchor,hidden}]`。labels输入含id/anchor/text/width/height/priority；调用处先用canvas测字。
- [ ] 新帧probe+sensor off+baseline且尚未进入观察，显示“接近调查”，不可仅按phase=baseline判已经观察；判定依据增加 `task_visual.observation_started`（baseline依据匹配session.baseline_started_at_min是否非空；进入near时为true，closing为false；在T07就导出并测试）。旧帧沿用status回退。
- [ ] 分类和活动用contact已公开字段；颜色复用现有contactColor；未知船只画中性船形，估计速度近零时不编造航向。
- [ ] contacts存在→观测分支；只有旧ships→旧分支；两者并存不重复观测对象。场景开关独立，只影响render；初始化放置时自动可见并标识场景，退出编辑恢复用户开关值。
- [ ] labels按设计§10优先级与固定8偏移尝试，矩形交叠及边界检测；没有空间隐藏低优先级文字但保留可点击符号。选中标签用受限tooltip/引线保证可读，不能超出画布。
- [ ] 浏览器纯函数失败测试示例：

```javascript
import { test, expect } from '@playwright/test';

test('approaching probe is not displayed as acquired tracking', async ({ page }) => {
  await page.goto('/');
  const state = await page.evaluate(async () => {
    const { uavDisplayState } = await import('/src/renderer/displayState.js');
    return uavDisplayState({
      status: 'tracking', operation_mode: 'probe', sensor_mode: 'off',
      task_visual: { task_type: 'probe', phase: 'baseline', observation_started: false },
    });
  });
  expect(state.label).toBe('接近调查');
});
```

- [ ] 用同一frame绘制快照测试：图层开关前后引擎/API调用计数无变化；contact类别/位置不被scenario覆盖；近邻8个contact、基地多机、地图边缘标签不重叠越界。

```bash
cd src/vis/frontend
npx playwright test tests/replay-restoration.spec.js --grep 'probe|contact|label|scenario'
npm run build
```

**Gate:** 搜索图层与原路线样式保留；新目标符号、阶段与真实任务一致。提交 `fix: make mixed mission targets and phases visually consistent`。

## T09：事件时间轴、真实回放API与历史兼容

**Files:** 新 `src/vis/frontend/src/renderer/replayEvents.js`；`hooks/useReplay.js`、`components/PlaybackBar.jsx`、`App.jsx`、`BottomDrawer.jsx`；`src/vis/backend/replay_adapter.py`、server（仅需要的适配）；测试 `tests/vis/test_replay_adapter.py`、前端新spec。

**Consumes:** T04失败事件、T02任务结束、T07新帧，旧无schema帧。**Produces:** `collectReplayMarkers(frames)`纯函数，新旧事件统一marker和加载状态。

- [ ] 先测同事件在相邻帧出现、data键顺序不同、时间非整数、frame_id不连续，最终一个marker，指向首次出现的数组索引。
- [ ] 新旧事件名字统一分类；包含mission_selection_failed/task_failed等失败事件；不能将空assignment当作“新增任务成功”。
- [ ] seek未加载块：显示“载入目标帧”，不显示最近帧的旧地图配新时间；跨文件generation取消旧请求，markers与last LLM清空。
- [ ] 时间轴读取真实total，不拿首块120当总帧；加载进度独立显示。事件只加载局部时明确未加载范围，必要时通过现有分块预取完成，不新增全量大JSON请求。
- [ ] 后端兼容只做缺省与已有明确转换；历史缺航线保持空；深拷贝不修改源对象。原文件SHA前后相等。
- [ ] 原始三文件本地真实`/api/replay/list`与`/api/replay`验证；CI用来源清楚的小fixture和临时OUTPUT_DIR。读API时不能绕过normalize_replay_frame。

```bash
python -m pytest tests/vis/test_replay_adapter.py tests/env/test_server_runtime.py -q
cd src/vis/frontend
npx playwright test tests/replay-restoration.spec.js --grep 'replay|timeline|legacy'
```

**Gate:** t20同时间比较、seek/切换/末帧、错误事件、只读行为全部通过。提交 `fix: preserve mission events and frame identity during replay`。

## T10：建立真实引擎场景与产物runner

**Files:** 新 `scripts/replay_restoration_scenarios.py`、`scripts/validate_replay_restoration.py`、`tests/mission/test_replay_restoration_runner.py`；复用 `evaluate_mixed_maritime.py` 模型边界与现有logger。

**Consumes:** T01—T09已修链路。**Produces:** 后续所有专项可复现入口，不使用静态视觉夹具冒充引擎。

### 新接口与CLI

`build_scenario(name: str, *, seed: int, transport: str) -> SimulationEngine` 构造受控场景；`capture_frame(engine, *, total_steps: int) -> dict` 使用生产build_frame；`run_scenario(name, *, seed, steps, output_dir, transport) -> dict` 逐步step→frame→assert→写产物。

CLI固定参数：`--scenario`取V01—V09及T11—T13列出的子场景名；`--seed`整数；`--steps`正整数；`--transport fixture|live`默认fixture；`--output-dir`必填；`--check-log PATH`只读审计已有JSONL，与run参数互斥；`--suite feature-integration`用于专项集合，与--scenario互斥。

- [ ] 逐帧必须调用 `engine.step()`，禁止直接改clock/position/coverage/contact classification生成展示。场景初始化可设置合法船位置、测试观测序列、模型脚本、障碍条件；这些须进manifest。
- [ ] 在暂停模型状态时不得连续写480个同时间帧并声称跑完：检测time未推进，写blocked结果并退出非零。
- [ ] manifest字段：git_commit、config_hash、seed、scenario、requested_steps、completed_steps、sim_time、transport、fixture=true/false、role_bindings、各配置覆盖、input_hashes、memory_version、测试版本。记录开始/结束wall time，不把仿真分钟与墙钟混用。
- [ ] 已存在manifest拒绝覆盖；生产记忆输出与测试输出隔离；不调用main.clear_output_cache/_free_port。
- [ ] 几何/状态审计：G03/G04/G06、有限坐标、真实传感器与足迹、route first point、no stale generation、覆盖分母正确。审计失败写具体frame/uav/task/expected/actual并退出1。
- [ ] events.jsonl按canonical key去重；metrics记录实际发生的phase与first_time，不发生记null+reason；同时保留来源role与evidence IDs。
- [ ] 建立fixture/live显式隔离测试，禁止runner导入时启动网络或运行长仿真。

```bash
python -m pytest tests/mission/test_replay_restoration_runner.py -q
python scripts/validate_replay_restoration.py --scenario V01 --seed 42 --steps 2 --transport fixture --output-dir outputs/validation/replay-restoration/t10-v01
python scripts/validate_replay_restoration.py --scenario V03 --seed 42 --steps 20 --transport fixture --output-dir outputs/validation/replay-restoration/t10-v03
```

预期命令退出0且completed_steps=requested_steps；产物具真实位移与任务。提交 `test: add real engine replay restoration scenarios`。

## T11：完整接入感知、信息场、意图与动态船舶

**Files:** 按设计§14的I01—I10、I12对应文件逐项核对；新 `tests/mission/test_feature_sensing_integration.py`、`test_feature_commands_integration.py`；T10场景文件；前端 `ContactPanel.jsx`、`IntentPanel.jsx`、`BottomDrawer.jsx` 仅补缺失的公开信息展示。

**Consumes:** T10真实引擎runner。**Produces:** 每项能力明确的跨模块因果证据，功能矩阵I01—I10/I12通过。

每个子任务分别red/green和提交，不合并成一个无法审查的大改。

| 子任务 | 初始输入/触发 | 必须贯通的链路 | 反例与精确断言 |
| --- | --- | --- | --- |
| T11a AIS | 一个合法AIS源，随后通过命令关闭再打开 | broadcast→contact sample→ais evidence→information_version→candidate→prompt→frame | 关闭后无新AIS sample；历史未删；打开新revision一次；I类确认后新AIS价值过滤仍正确 |
| T11b 无源协同 | 1机、2机同源同刻、2机不同源/不同时刻四组 | passive observation→条件position→investigation→合法派工→实际位移 | 1机只有bearing无position；无效组合不定位；不暴露隐藏class；同一position不重复消费 |
| T11c 信息闭环/公平 | 一个新证据、一次过期、一个SAR refresh，候选数超窗口 | evidence→I/V delta→trigger→同version snapshot→选择→commit→新观测 | 过期按TTL撤销；相同fact幂等；公平窗口无无限饥饿；不接受错版本提交 |
| T11d 接触/调查/活动 | 接触合并、near样本、受控逃逸与岛屿迫转 | canonical接触→probe阶段→assessment/evasion→活动状态→frame | 无样本不研判；合并不误删owner；normal迫转不算逃逸；I/II与activity不混用 |
| T11e 人工重点区 | 真实API create/update/cancel，随后expire | queue→仿真边界apply→intent revision→候选/revisit→状态/UI | 错revision拒绝；无效bbox拒绝；保护probe不被普通intent抢占；回放写请求禁用 |
| T11f 船舶/红方 | runtime允许时add/delete、AIS切换、red合法/非法响应 | command→场景实体→后续采样→观测/任务清理；red→ship动力学 | 错episode拒绝；删除后无幽灵record；red失败遵循paused_model；边界/陆地非法运动拒绝；蓝方prompt无red真值 |

- [ ] 对每项在生产入口装轻量测试spy（仅测试）计数，不monkeypatch生产下游返回“已成功”；对end-to-end断言使用公开结果+事件序列，关键安全属性另查状态。
- [ ] 每个断链的修改写清“缺失调用/错误过滤/错误版本/消费顺序”，沿用已有业务规则；不得重新发明AIS关联或无源算法。
- [ ] 改进展示必须能在事件详情关联 observation/evidence/assignment；UI显示自然语言原因，不显示完整debug snapshot。
- [ ] 子场景名固定：`ais-toggle`、`passive-gates`、`information-loop`、`contact-assessment`、`intent-lifecycle`、`vessel-red-lifecycle`。

```bash
python -m pytest tests/mission/test_feature_sensing_integration.py tests/mission/test_feature_commands_integration.py tests/mission/test_information_loop.py tests/mission/test_visibility.py tests/mission/test_dynamic_vessel_lifecycle.py tests/mission/test_maritime_acceptance.py tests/mission/test_prompt_window.py tests/sensor/test_passive.py tests/env/test_vessel_boundary.py -q
python scripts/validate_replay_restoration.py --scenario information-loop --seed 42 --steps 120 --transport fixture --output-dir outputs/validation/replay-restoration/t11-information
```

**Gate:** 不仅单项传感器通过，还必须看到所产生的信息实际影响候选与合法任务；每个子场景报告包含因果IDs。

## T12：完整接入安全控制、基地、跟踪与接力

**Files:** 设计B02—B06/I11对应控制、simulation、utils；新 `tests/mission/test_feature_control_integration.py`，T10场景文件。

**Consumes:** 真实感知与一致任务。**Produces:** 安全控制与任务连续性证据，矩阵B02—B06/I11通过。

- [ ] 子场景 `weather-replan`：活动搜索路线前方出现动态storm；真实navigation重规划、route_revision变化、executor不穿障；未受影响UAV保留任务。新版与旧冲突检测路径均检查是否实际生效，不能只有事件没有控制动作。
- [ ] 子场景 `base-recovery`：多个UAV低油与基地容量限制；真实返航、队列/holding、land/refuel、reset、再派；没有基地可达要明确失败，不能瞬移加油。
- [ ] 子场景V07：受控真实EO样本之后fixture assessor给出合法II类回答；track开始后触发合法燃油预警/返航；handoff required→assigned→EO lock各有时间戳，成功只在锁定后记。
- [ ] 正常返回和传感器不可用时显示不同状态；track丢失生成对应公开事实，禁用无观测“全知跟踪”。
- [ ] 参数化所有动作经过SafetyEnvelope；BC/RL也不能由旧heuristic conflict hook直接重写状态/路径。原所有权测试保持通过。

```bash
python -m pytest tests/mission/test_feature_control_integration.py tests/mission/test_handoff.py tests/control/test_simulation_ownership.py tests/control/test_safety.py tests/env/test_storm_avoidance.py tests/utils/test_conflict_detector.py tests/env/test_goal2_foundation.py -q
python scripts/validate_replay_restoration.py --scenario V07 --seed 42 --steps 480 --transport fixture --output-dir outputs/validation/replay-restoration/t12-handoff
```

**Gate:** 规避/返航/接力是实际运动结果，非状态标签；各异常有安全终态和恢复策略。每个子场景独立提交。

## T13：Reviewer、指标、策略记忆和扩展接口完整接入

**Files:** `src/schedule/{task_allocator,llm_reviewer}.py`、`src/mission/{outcome_evaluator,episode_logger,strategy_memory}.py`、`scripts/{evaluate_mixed_maritime,validate_strategy_memory}.py`；测试新 `tests/mission/test_feature_episode_integration.py`；必要时补前端episode摘要。

**Consumes:** T11/T12真实事件和T10provenance。**Produces:** B08/I13/I14/E01/C01证据；跨episode流程明确，fixture不污染生产记忆。

- [ ] 确认新版mission_step调用reviewer的周期与摘要传播；模拟合法reviewer响应，下一prompt确实含摘要；失败保留现有安全行为，不阻塞飞行或偷偷切换旧scheduler。
- [ ] evaluator在实际EO、识别、接力与结束时收到事实；取指标分母与采样时间测试，异常episode valid=false；不能因缺帧把没测项写100%。所有frame与episode日志关联episode_id。
- [ ] 在临时目录跑memory propose/save/validate/report/activate/select_for_context/rollback；复用现有test_strategy_validation输入工厂，保留最少3个合格live历史、validation/holdout等既有门槛。单元测试合成数据仅用于验证逻辑，不能作为正式激活证据。
- [ ] 未validated memory不进入生产prompt；fixture episode不能提名；active manifest固定到下一episode，不中途改本episode；已有策略回滚不影响物理参数或身份。
- [ ] 按设计§14.4修改 `main.py`、`scripts/run_simulation.py`：`--memory-version`默认baseline，`--memory-root`默认outputs/strategy_memory；使用现有 `SimulationEngine(..., strategy_memory_store=store, strategy_memory_version=resolved_version)`。active在初始化解析一次，非法显式版本报错；baseline不注入策略。不增加每步热更新。
- [ ] 扩充 `tests/test_runtime_configuration.py`：两个入口参数一致、baseline/active/明确版本行为正确、reset保持既定策略约定、unknown版本失败；开启清理且memory-root在被清理目录内时必须在删除前拒绝。fixture记忆根目录始终临时隔离。
- [ ] 工程扩展E01：注入测试provider验证factory→observation→action→safety→executor完整；无provider时明确报错，不要求训练真实BC/RL模型。
- [ ] 历史兼容C01：旧replay adapter与legacy测试仍通过；正式main必须走mission scheduler而非为修效果回退legacy。
- [ ] 子场景名 `reviewer-episode`、`memory-lifecycle`、`provider-contract`。memory-lifecycle为临时存储专项验证，不伪装480步演示。

```bash
python -m pytest tests/mission/test_feature_episode_integration.py tests/mission/test_strategy_memory.py tests/mission/test_strategy_validation.py tests/mission/test_outcome_evaluator.py tests/mission/test_episode_logger.py tests/control/test_factory.py tests/control/test_base_classes.py tests/vis/test_replay_adapter.py tests/test_runtime_configuration.py -q
```

**Gate:** 正常条件能力可调用并有下游；缺真实历史就明确未完成正式memory验证，不伪造。按reviewer/metrics/memory/provider分别提交。

## T14：真实前端验证、改进演示分镜与截图

**Files:** 前端 `tests/replay-restoration.spec.js`、新 `playwright.replay.config.js`、`hooks/useMp4Export.js`（仅修已证实问题）；新 `scripts/run_replay_acceptance_server.py`；README；T10输出。

**Consumes:** T10—T13真实引擎JSONL。**Produces:** 基础与新增效果截图、真实API测试、事件驱动分镜、可播放导出。

- [ ] 新acceptance server只提供指定临时output目录的真实产物，不循环伪造相同帧；CLI `--output-dir PATH --port N`。端口占用直接失败，不杀他人服务。
- [ ] 新建 `src/vis/frontend/playwright.replay.config.js`：testMatch仅replay-restoration.spec.js，viewport=1440×1000、deviceScaleFactor=1、workers=1、retries=0；backend=18866、frontend=5186，两个webServer都reuseExistingServer=false，Vite使用 `--strictPort`。环境变量 `REPLAY_ACCEPTANCE_DIR`必填，作为新acceptance server的output-dir；Vite设VITE_BACKEND_PORT=18866，baseURL=http://127.0.0.1:5186。启动端口冲突直接报错，不杀进程。
- [ ] 现有静态测试保持原playwright.config.js入口，在报告单列；T08/T09的局部纯函数测试可先使用原配置，T14最终真实回放验收必须使用新配置。
- [ ] 将T10—T13选定真实JSONL复制到 `outputs/validation/replay-restoration/t14-browser`，每文件记录源路径与SHA，文件名保留场景/seed，供真实API枚举；不得生成静态伪帧。
- [ ] 截图前等待目标frame time/ID、images.complete、document.fonts.ready；冻结动画phase或调用生产renderFrame(frameCount=固定值)作稳定截图，不只sleep一个常数。
- [ ] 将T10报告中的实际事件时间定位为分镜，非硬编码“第30分钟必发现”。截图覆盖设计§11.3及§14.3，至少基础搜索、线索升级、重派、调查、分类跟踪、接力、恢复搜索7类。
- [ ] 验证1440×1000的layer数据与图像一致；1280×720/390×844布局不溢出，侧栏/图层开关可操作。
- [ ] 像素回归只能对固定受控fixture；用region/path/FOV的数据断言+图层像素存在+人工审图组合，不能仅判断canvas不是空白。
- [ ] 相同frame直播/回放/导出语义一致；MP4实际生成并可解码至少3个时刻，含一段SAR和一段EO；若浏览器无编码器，标记该环境未完成并换支持环境验证，禁止silent skip算通过。
- [ ] 老日志本地回放SHA对比；新版坏日志依旧忠实展示，不能把“适配后像好了”当作算法修复。

```bash
cd src/vis/frontend
npm run build
npx playwright test tests/acceptance.spec.js tests/mixed-maritime.spec.js
REPLAY_ACCEPTANCE_DIR="$PWD/../../../outputs/validation/replay-restoration/t14-browser" npx playwright test --config playwright.replay.config.js
```

**Gate:** 旧版基础图层存在、新功能因果可读，图层数据准确，全部截图逐张人工核对并写结论。提交 `test: verify foundational and improved replay visuals end to end`。

## T15：长跑、全功能闭环与最终验收

**Files:** 补齐 `docs/validation/replay-restoration/acceptance-report.md`、feature matrix、README运行说明；只修验证暴露的问题，修复回到所属任务red/green门。

**Consumes:** 所有前置任务通过。**Produces:** 20/120/480验证包、正式模型结果、全部能力结论、可复用演示说明。

- [ ] 完整自动回归：

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest tests -q
```

测试若依赖正常插件，应保留既有项目要求并记录环境；不能以禁插件掩盖失败。不得用dummy key运行真实模型验收。

- [ ] 离线长跑（每个目录只首次使用；重复运行另设run-id）：

```bash
python scripts/validate_replay_restoration.py --scenario V06 --seed 42 --steps 20 --transport fixture --output-dir outputs/validation/replay-restoration/offline-42-20
python scripts/validate_replay_restoration.py --scenario V06 --seed 42 --steps 120 --transport fixture --output-dir outputs/validation/replay-restoration/offline-42-120
python scripts/validate_replay_restoration.py --scenario V06 --seed 42 --steps 480 --transport fixture --output-dir outputs/validation/replay-restoration/offline-42-480
python scripts/validate_replay_restoration.py --scenario V06 --seed 101 --steps 480 --transport fixture --output-dir outputs/validation/replay-restoration/offline-101-480
python scripts/validate_replay_restoration.py --suite feature-integration --seed 42 --steps 120 --transport fixture --output-dir outputs/validation/replay-restoration/features-42
```

suite按子场景自身时长执行：普通120、V07 480、存储/provider专项无仿真步；manifest分别写requested/completed，不把CLI steps误用为全部场景硬长度。feature suite涵盖V01—V09中适用离线场景和T11—T13全部子场景，V08检查输入文件而非运行引擎。

- [ ] 用户已批准实施时，正式LLM在现有凭据/预算规则内运行；这里给执行命令而不在文档阶段调用：

```bash
python scripts/validate_replay_restoration.py --scenario V06 --seed 42 --steps 480 --transport live --output-dir outputs/validation/replay-restoration/live-42-480
```

live只能使用默认正式配置的场景，不用仅适用于测试的强制样本/识别；V06允许live，V01/V02等合成专项禁止live并报参数错误。凭据缺失/额度不足/模型失败需保留未完成结论，不能替换fixture后仍叫live。

- [ ] 检查完成步数、实际仿真时间、每个任务ID和session关系、无幽灵record、路径有效率（按ready限定）、真实扫描与coverage、基地产能、接力锁定、资源利用witness、模型错误/重试。
- [ ] 在同机同固定fixture记录性能基线与新版本P50/P95、构帧耗时、JSONL字节；零导航构帧断言与点数预算为硬门，性能P95超20%按设计处理。
- [ ] 最终matrix逐项给测试和产物路径；没有端到端证据不能从待验证改通过。I14正式跨episode若缺合法历史单列未完成，不能宣称全部已有能力完成正式验收。
- [ ] 报告用三个结论栏：基础效果、改进功能接入、正式模型效果。未达标写具体项而非笼统“基本完成”。禁止用旧56.3553%作为新混合任务硬阈值，禁止声称没有对照的性能提升。
- [ ] 生成给用户的演示说明：打开哪个回放、在哪个实际事件时间看哪项基础/改进、对应截图/证据，包含模型失败或无触发时的解释。

**Final Gate:** 全部可执行自动验证通过、完整演示包、功能矩阵全覆盖、正式验证状态透明，用户可按报告重放。提交 `docs: publish replay restoration validation evidence`。

## 需求到任务追踪

| 设计要求 | 实现任务 | 主验证 |
| --- | --- | --- |
| F01/G03 会话唯一/续派 | T01 | 6/10并发、续派、回滚 |
| F02/G04 生命周期 | T02 | holding/return/refuel/merge/delete全状态 |
| F03/G05 可行资源利用 | T03/T04 | 增补witness、bounded retry、visible集合 |
| F04/F08/G06 路线 | T05/T06/T07 | 权威路线、起点连续、无副作用、generation |
| F06/G07 阶段/传感器 | T07/T08 | SAR真实足迹、EO门、接近不是跟踪 |
| F05 目标/标签 | T08 | 中性符号、真值隔离、label bbox |
| F07/G08 历史/事件 | T09 | 新旧事件去重、seek、API、SHA |
| F09 真实导航 | T06/T10 | 非水平真实接近与阶段门 |
| G09/G10 验证来源/回归 | T10/T14/T15 | 真引擎、live隔离、全套回归 |
| B01—B09 基础效果 | T01—T12/T14/T15 | 搜索/路线/SAR/EO/安全/基地/回放 |
| I01—I10/I12 改进链路 | T11 | 感知/信息/动态命令/红方端到端 |
| I11 跟踪接力 | T12 | required→assignment→EO acquired |
| I13/I14 评估/记忆 | T13/T15 | 分母/valid、跨episode门、fixture隔离 |
| E01/C01 扩展/兼容 | T13 | provider契约、legacy仅兼容 |
| G11—G13 完整接入与改进展示 | T00/T11—T15 | 25项矩阵、7类分镜、无证据不宣称提升 |

## 可直接交给 Luna max 的执行提示

```text
请执行 docs/superpowers/plans/2026-09-16-replay-visual-restoration-plan.md。
先确认用户已明确批准对应设计与计划；没有批准则只审阅，不改业务代码。
先读配套设计全文，尤其§14，随后从T00开始顺序执行。当前任务之外不顺带重构。
每项先复现失败，按明确修改逻辑修复，再运行指定验证并保存证据。
用户要求：先确保旧版基础效果，再凸显改进；所有已实现业务能力都必须正确接入主链路。
不得用假航线/假扫描/静态帧补画面，不得正式运行静默改用fixture选择器，不得修改原日志。
每项更新execution-log与25项功能矩阵，说明已验证事实及未完成项。
若失败来自尚未验证的导航/生命周期机制，先定位最小根因，不调整验收阈值掩盖问题。
文档列出的新测试、runner与CLI在实施前不存在；须按所属任务创建后才可执行后续命令。
全部完成以T15门槛为准；只单测通过或只有截图不能宣称恢复。
```


## 文档交接前自检（非实现验收）

2026-09-16已做：设计/计划相互链接与截图链接检查；16项任务顺序与25项能力计数；13条全局约束一致性；文档Python片段语法检查；原三份日志SHA复核。此检查仅说明文档结构与示例语法，不表示新增测试已经存在、业务错误已修复或正式仿真已通过。
