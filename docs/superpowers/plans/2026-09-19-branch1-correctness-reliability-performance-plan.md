# branch1 正确性、可靠性与性能修复实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Every task ends with a focused test gate;不得把尚未执行的命令写成通过。

**Goal:** 修复 branch1 审计清单中 46 项确认问题、18 项部分成立问题，并为已修复的 4/15/43 建立防回归证据，使覆盖、调度、控制、回放和被动定位共享一致的状态与错误契约。

**Architecture:** 先建立跨模块契约（坐标、候选面积、异常分类、不可变快照、任务状态），再修复覆盖/跟踪/调度状态机；随后收敛录制与 WebSocket 的队列语义，最后处理缓存和热点。所有静默降级改成可审计事件或明确失败；旧兼容路径在有测试依赖时保留适配器，但不得继续参与默认生产路径。

**Tech Stack:** Python 3.13、pytest/pytest-asyncio、FastAPI/Starlette WebSocket、NumPy、JSONL、现有 TypeScript 前端测试。

## Global Constraints

- 搜索网格统一使用 (cols, rows)；所有 bbox 均为半开区间 [col_start, col_end) × [row_start, row_end)。
- SAR 覆盖只由真实 SAR footprint 记账；航路几何、覆盖账本、fresh mask 使用同一 footprint 投影。
- 搜索候选最小面积固定为 search_min_cells=20，由配置读取；候选生成、提示词和 validator 共享同一值。
- 控制错误区分 InvalidControlCommand、UnsafeControlState、ControlInputClipped、ProbeValidationError 和规划失败；只有前两类按连续计数触发撤销。
- last_selection_success、route status、任务保存/恢复、录制 durability 都必须在成功和失败路径写入明确状态。
- 回放接口单次最多 300 帧；请求体流式限长；半行 JSON 返回可识别的 partial/422 结果。
- 4、15、43 的现有修复必须保留并建立回归测试。
- 覆盖中途遇新障碍采用 fail-closed：受影响未飞 scan leg 标为缺口，route status=unavailable，释放绑定并发出审计事件；不把绕飞路径当作已覆盖。
- conflict_detector 的同一 cell 同时占用视为冲突；horizon 使用两条路径最大长度；显式 direction 必须进入 feasibility 判定。
- 真实模型不可用时记录 BLOCKED；fixture 不能刷位姿、覆盖或控制结果。

---

### Task 1: 建立契约基线、测试夹具和执行记录

**Files:**
- Create: tests/regressions/test_branch1_contracts.py
- Create: tests/regressions/test_branch1_contract_matrix.py
- Create: docs/validation/2026-09-19-branch1-execution.md
- Modify: src/schedule/config_loader.py, src/schedule/datatypes.py

**Interfaces:**
- Produces grid_bbox_from_center(center: GridCoord, radius: int, resolution: tuple[int, int]) -> BBox and search_min_cells(config) -> int.

- [ ] Step 1: 写失败测试

    def test_bbox_uses_cols_then_rows_for_rectangular_grid():
        assert grid_bbox_from_center((35, 25), 2, (40, 30)) == BBox(33, 23, 37, 27)

    def test_search_min_cells_is_shared_by_config_and_validator():
        assert search_min_cells(ConfigLoader.load()) == 20

- [ ] Step 2: 运行失败测试

    pytest tests/regressions/test_branch1_contracts.py -q

Expected: 当前 helper 不存在或 bbox 末端列/行互换导致 FAIL。

- [ ] Step 3: 实现最小契约

把网格尺寸和 search_min_cells 暴露为只读配置值；禁止模块内硬编码 19/20/10。为每个审计编号建立 issue_ids -> test names -> source files 表。

- [ ] Step 4: 运行通过并记录

    pytest tests/regressions/test_branch1_contracts.py tests/regressions/test_branch1_contract_matrix.py -q

把命令、SHA、失败原因和通过证据写入执行记录。

- [ ] Step 5: Commit

    git add tests/regressions docs/validation/2026-09-19-branch1-execution.md src/schedule/config_loader.py src/schedule/datatypes.py
    git commit -m "test: establish branch1 correctness contracts"

### Task 2: 修复覆盖几何、bbox、候选面积和 fragment 账本（1, 2, 3, 16-19, 34, 38, 56, 62, 63, 65）

**Files:**
- Modify: src/utils/coverage_planner.py, src/control/heuristic/coverage.py, src/schedule/state_manager.py, src/schedule/candidate_extractor.py, src/schedule/prompt_builder.py, src/schedule/output_validator.py
- Modify: src/control/heuristic/conflict_detector.py, src/schedule/task_allocator.py
- Test: tests/utils/test_coverage_planner.py, tests/control/heuristic/test_coverage.py, tests/control/heuristic/test_coverage_sensor_geometry.py, tests/schedule/test_candidate_extractor.py, tests/utils/test_conflict_detector.py

**Interfaces:**
- CoveragePlan exposes scan_footprints and required_cells; CoverageController.route_snapshot() returns status plus uncovered_cells and never publishes a usable route when unavailable.
- extract_pool(...) -> CandidatePool retains fragment_alerts and rejects candidates whose unique due cells are fewer than search_min_cells.

- [ ] Step 1: 写失败测试

    def test_swath_1_5_does_not_claim_half_cell_gap_covered(): ...
    def test_rectangular_track_bbox_overlap_and_completion_are_correct(): ...
    def test_one_cell_fragment_is_rejected_and_alert_is_retained(): ...
    def test_new_obstacle_on_unflown_scan_leg_fails_closed(): ...
    def test_replan_changes_orientation_and_preserves_unaffected_task(): ...
    def test_feasibility_honors_explicit_direction(): ...
    def test_fragment_due_cells_are_subtracted_from_served_cells(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/utils/test_coverage_planner.py tests/control/heuristic/test_coverage.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/schedule/test_candidate_extractor.py tests/utils/test_conflict_detector.py -q

Expected: 至少覆盖留缝、障碍物契约、矩形 bbox 和 1-cell candidate 失败。

- [ ] Step 3: 实现

1. 将规划带宽、真实 footprint 宽度和账本投影分开；covered_cells 只接受 footprint 与 cell 的实际交集，增加相邻 scan leg gap assertion。
2. 所有 bbox clamp 使用 resolution[0] 限制列、resolution[1] 限制行；half=2 改成显式 window_size=5，边界裁剪后的实际尺寸可审计。
3. 提前应用 search_min_cells；fragment 生成按 due_cells - served_cells 去重后再入池，fragment_alerts 写入 CandidatePool 和 frame/audit。
4. 初次规划持久化 direction；换向重规划必须选择不同方向，否则进入 unavailable。
5. 新障碍影响未飞 scan leg 时 fail-closed，清空 route snapshot，释放绑定；冲突异常保留 planning_failed 状态并记录异常类型。
6. is_region_feasible(region, direction=...) 与 plan(direction=...) 共享同一方向参数。

- [ ] Step 4: 运行通过及性质校验

    pytest tests/utils/test_coverage_planner.py tests/control/heuristic/test_coverage.py tests/control/heuristic/test_coverage_closed_loop.py tests/control/heuristic/test_coverage_sensor_geometry.py tests/schedule/test_candidate_extractor.py tests/utils/test_conflict_detector.py -q
    pytest tests/env/test_simulation_integration.py -k "weather or obstacle or coverage" -q

另运行 40×30 网格 property test：任意 bbox 满足 0<=col_start<=col_end<=40 和 0<=row_start<=row_end<=30，账本覆盖集合是 footprint 投影的子集。

- [ ] Step 5: Commit

    git add src/utils/coverage_planner.py src/control/heuristic/coverage.py src/schedule/state_manager.py src/schedule/candidate_extractor.py src/schedule/prompt_builder.py src/schedule/output_validator.py src/control/heuristic/conflict_detector.py src/schedule/task_allocator.py tests
    git commit -m "fix: make coverage geometry and candidate accounting consistent"

### Task 3: 统一任务生命周期、目标交接、触发去重和 AIS 历史（6-11, 15, 23）

**Files:**
- Modify: src/control/common/coordinator.py, src/env/simulation.py, src/schedule/trigger_manager.py, src/mission/mission_scheduler.py, src/mission/contact_store.py
- Test: tests/control/test_coordinator.py, tests/env/test_contact_lifecycle.py, tests/mission/test_information_loop.py, tests/mission/test_contact_release.py, tests/mission/test_feature_sensing_integration.py

**Interfaces:**
- ControlOutcome 区分 clipped、masked、invalid、unsafe；TaskFlow.save_coverage_task/restore_coverage_task/clear_saved_coverage 成对使用。
- TriggerManager.check() 保留 (event_type,uav_id) 去重直到 expires_at，不在每次 check 清空未过期状态。

- [ ] Step 1: 写失败测试

    def test_clipping_does_not_increment_invalid_command_counter(): ...
    def test_unsafe_control_state_is_classified_and_recovered_separately(): ...
    def test_target_expiry_releases_tracking_before_control_tick(): ...
    def test_saved_coverage_round_trip_restores_generation_and_route(): ...
    def test_tracking_timeout_is_reached_after_start_timestamp_is_written(): ...
    def test_light_trigger_respects_five_minute_dedup_window(): ...
    def test_ais_history_removes_expired_mmsi_keys(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/control/test_coordinator.py tests/env/test_contact_lifecycle.py tests/mission/test_information_loop.py tests/mission/test_contact_release.py tests/mission/test_feature_sensing_integration.py -q

- [ ] Step 3: 实现

安全裁剪/传感器屏蔽只记录 safety_adjustment；仅真正非法命令累加 invalid_command_streak。控制回路前先 expire contacts、清理 target binding，再生成 ActionMask。所有 coverage 中断路径 save，恢复成功 restore，失败 clear。首次 tracking assignment 写 _tracking_started_at，完成/释放删除。_last_light_time 用于节流；去重字典按时间淘汰。AIS 删除船时同步删除 _ais_history[mmsi]。保留已有 target-loss 回归并扩大到控制 tick。

- [ ] Step 4: 运行通过

    pytest tests/control tests/env/test_contact_lifecycle.py tests/mission/test_information_loop.py tests/mission/test_contact_release.py tests/mission/test_feature_sensing_integration.py -q

验收日志必须同时出现：一次 clipping 不返航；unsafe 有明确 unsafe_control_state；目标消失后进入 HOLDING/request_assignment；coverage save/restore 有相同 generation。

- [ ] Step 5: Commit

    git add src/control/common/coordinator.py src/env/simulation.py src/schedule/trigger_manager.py src/mission/mission_scheduler.py src/mission/contact_store.py tests
    git commit -m "fix: make task lifecycle and control fault semantics explicit"

### Task 4: 收敛 LLM 选择后置校验、prompt 语义与旧调度路径（7, 12-14, 19, 24, 41, 47, 58-61, 64, 66）

**Files:**
- Modify: src/mission/mission_scheduler.py, src/mission/llm_gateway.py, src/schedule/prompt_builder.py, src/schedule/task_allocator.py, src/schedule/hungarian.py, src/schedule/llm_client.py, src/schedule/output_validator.py
- Modify: src/mission/prompts/mission_scheduler.txt
- Test: tests/mission/test_mission_scheduler.py, tests/mission/test_feature_episode_integration.py, tests/schedule/test_task_allocator.py, tests/schedule/test_hungarian.py

**Interfaces:**
- pair_selected_tasks() 返回 PairingResult(assignments, errors, is_valid)；selection_interaction() 只能以该结果最终状态写 success/is_valid。
- 调度路径由显式 scheduler_mode 配置选择，不再比较 __func__；缺少 scipy 时抛出/记录 AssignmentBackendUnavailable，不得静默贪心。

- [ ] Step 1: 写失败测试

    def test_pairing_failure_rewrites_selection_success_false(): ...
    def test_prompt_uses_allocator_availability_and_configured_region_limit(): ...
    def test_relay_candidate_value_has_no_fake_1000_bonus(): ...
    def test_pairing_exception_is_logged_with_exception_type_and_context(): ...
    def test_light_snapshot_contains_intents_and_statuses(): ...
    def test_scheduler_mode_does_not_change_when_method_is_wrapped(): ...
    def test_missing_scipy_is_blocked_instead_of_greedy_fallback(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/mission/test_mission_scheduler.py tests/mission/test_feature_episode_integration.py tests/schedule/test_task_allocator.py tests/schedule/test_hungarian.py -q

- [ ] Step 3: 实现

后置 pairing/route feasibility/ownership 任一失败都调用统一 _finalize_selection(success=False, errors=...)；纠正消息只对 LLM schema 错误重试，后置错误生成 selection_post_validation_failed。prompt 复用 allocator 的 available set、uav.count 和真实排序键；删除接力 +1000。合并 _compute_iou/_parse_json 重复实现。旧 search- 前缀转为一次性兼容解析并统计 warning，默认生产只走 search:c...；删除未调用旧 completion 路径前先迁移测试到统一 finalize。light/heavy 使用同一 snapshot builder，传入 intents/statuses。

- [ ] Step 4: 运行通过

    pytest tests/mission tests/schedule -q

检查失败选择 JSON：success=false、is_valid=false、errors 非空；确认生产日志不存在旧前缀估价分支调用。

- [ ] Step 5: Commit

    git add src/mission src/schedule tests
    git commit -m "fix: unify scheduler validation and prompt semantics"

### Task 5: 修复控制 probe、返航、冲突和异常类型契约（20-22, 40, 47, 55, 56）

**Files:**
- Modify: src/control/heuristic/probe.py, src/control/common/coordinator.py, src/control/heuristic/safety.py, src/control/heuristic/return_to_base.py, src/utils/obstacle_avoider.py, src/control/heuristic/conflict_detector.py, src/vis/backend/server.py
- Test: tests/control/heuristic/test_probe.py, tests/control/test_safety.py, tests/control/test_route_snapshot.py, tests/utils/test_obstacle_avoider.py, tests/env/test_server_runtime.py

- [ ] Step 1: 写失败测试

    def test_probe_validation_errors_are_control_errors_with_reason(): ...
    def test_no_safe_candidate_is_not_reported_as_invalid_command(): ...
    def test_return_to_base_uses_pose_tolerance(): ...
    def test_conflict_detector_checks_same_cell_and_longer_horizon(): ...
    def test_server_uses_isinstance_for_command_conflict(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/control/heuristic/test_probe.py tests/control/test_safety.py tests/control/test_route_snapshot.py tests/utils/test_obstacle_avoider.py tests/env/test_server_runtime.py -q

- [ ] Step 3: 实现

定义 ProbeValidationError、UnsafeControlState 的统一基类/错误码；coordinator 按类型映射 recovery policy。返航基地使用已有 _poses_match 容差。冲突检测从 offset=0、horizon=max(len)，显式方向传入 feasibility。RRT 使用空间索引/批量候选，Dubins 仅对 shortlist 计算，anchor 设上限并记录耗时；超时返回规划失败而非静默淘汰。server 所有异常用 isinstance。

- [ ] Step 4: 运行通过

    pytest tests/control tests/utils/test_obstacle_avoider.py tests/env/test_server_runtime.py -q

- [ ] Step 5: Commit

    git add src/control src/utils/obstacle_avoider.py src/vis/backend/server.py tests
    git commit -m "fix: classify control and route planning failures precisely"

### Task 6: 深拷贝契约与 SAR/passive 真值隔离（4, 25, 42-44, 54, 57, 65）

**Files:**
- Modify: src/control/common/contracts.py, src/vis/backend/frame_builder.py, src/sensor/passive.py, src/mission/replay_adapter.py
- Test: tests/control/test_contracts.py, tests/env/test_coverage_frame.py, tests/mission/test_maritime_acceptance.py, tests/sensor/test_passive.py, tests/vis/test_replay_adapter.py

- [ ] Step 1: 写失败测试

    def test_snapshot_detaches_ndarray_and_custom_mutable_payload(): ...
    def test_frame_publisher_can_deepcopy_state_without_mappingproxy_error(): ...
    def test_zero_heading_is_preserved(): ...
    def test_nested_coverage_progress_is_json_serializable(): ...
    def test_event_window_does_not_duplicate_boundary_event(): ...
    def test_replay_mode_overwrites_live_mode(): ...
    def test_passive_position_contains_noise_and_never_uses_truth_directly(): ...
    def test_scenario_vessels_non_list_is_rejected_with_422(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/control/test_contracts.py tests/env/test_coverage_frame.py tests/mission/test_maritime_acceptance.py tests/sensor/test_passive.py tests/vis/test_replay_adapter.py -q

- [ ] Step 3: 实现

递归 snapshot：mapping/list/tuple/set/ndarray/dataclass/custom mutable 均复制为 JSON-safe immutable 值；保留已修复 _FrozenMapping。frame builder 使用 is None 选择 heading，递归 normalize coverage progress，事件窗口使用半开区间 (last, now] 并保存 event id。replay adapter 强制 mode=replay。passive resolver 只发布带配置噪声的 observation，真值仅留在仿真内部，双测向也必须经过误差模型和关联。scenario_vessels 做类型校验。

- [ ] Step 4: 运行通过

    pytest tests/control/test_contracts.py tests/env/test_coverage_frame.py tests/mission/test_maritime_acceptance.py tests/sensor/test_passive.py tests/vis/test_replay_adapter.py -q

确认 4/15/43 对应的现有测试仍通过。

- [ ] Step 5: Commit

    git add src/control/common/contracts.py src/vis/backend/frame_builder.py src/sensor/passive.py src/mission/replay_adapter.py tests
    git commit -m "fix: enforce detached snapshots and truthful sensor frames"

### Task 7: FramePublisher 可靠落盘与实时广播语义（5, 26, 31, 32, 49, 51, 52）

**Files:**
- Modify: src/vis/backend/frame_publisher.py, src/vis/backend/frame_builder.py, src/vis/backend/server.py
- Test: tests/env/test_frame_publisher.py, tests/env/test_server_runtime.py

- [ ] Step 1: 写失败测试

    def test_recording_exception_sets_failure_and_does_not_report_flush_success(): ...
    def test_flush_waits_for_queue_join_even_when_queue_empty_race_occurs(): ...
    def test_final_live_frame_is_broadcast_after_previous_future_finishes(): ...
    def test_broadcast_future_exception_is_observed_and_logged(): ...
    def test_matrices_use_sim_time_threshold_crossing_not_float_modulo(): ...
    def test_llm_cycle_is_present_only_on_decision_frames(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/env/test_frame_publisher.py tests/env/test_server_runtime.py -q

- [ ] Step 3: 实现

使用 Queue.join()/accepted sequence counter 表示 replay durability；record loop 捕获序列化和 logger 异常，设置 record_error 并使 flush 返回 False。live loop 不丢最后一帧：等待上一 future 或合并最新帧；读取 future exception。矩阵使用 last_matrix_time 与 sim_time >= next_matrix_time。frame builder 仅在真实 decision frame 写 llm_cycle，普通帧写 null/摘要。

- [ ] Step 4: 运行通过

    pytest tests/env/test_frame_publisher.py tests/env/test_server_runtime.py tests/env/test_replay_acceptance_server.py -q

注入 logger 异常后 flush=False；正常 close 后 record_count 等于接受帧数；最终 live 帧可见。

- [ ] Step 5: Commit

    git add src/vis/backend/frame_publisher.py src/vis/backend/frame_builder.py src/vis/backend/server.py tests
    git commit -m "fix: make frame persistence and live delivery loss-aware"

### Task 8: Replay/API/WebSocket 并发、限长和半行处理（27-30, 46-53）

**Files:**
- Modify: src/vis/backend/server.py, src/mission/replay_adapter.py
- Test: tests/env/test_server_runtime.py, tests/env/test_replay_acceptance_server.py, tests/vis/test_replay_adapter.py

- [ ] Step 1: 写失败测试

    async def test_replay_total_uses_incremental_index_without_o_n_rescan(): ...
    async def test_mp4_upload_rejected_while_streaming_past_limit(): ...
    async def test_broadcast_uses_snapshot_of_clients_and_keeps_new_client(): ...
    async def test_websocket_initial_frame_is_serialized_before_registration(): ...
    async def test_idle_timeout_logs_close_code_1001(): ...
    async def test_replay_truncated_jsonl_returns_partial_result_and_error_metadata(): ...

- [ ] Step 2: 运行失败测试

    pytest tests/env/test_server_runtime.py tests/env/test_replay_acceptance_server.py tests/vis/test_replay_adapter.py -q

- [ ] Step 3: 实现

按文件 inode/size/offset 维护增量 JSONL index，避免每次 mtime 变化全扫；请求仍强制 1<=limit<=300。上传用 request.stream() 累加并超过 250MB 立即返回 413。broadcast 在 await 前复制 clients tuple，逐客户端隔离异常。WebSocket 初始 frame 先构建，再注册/发送并用发送锁串行化；timeout 记录 disconnect code/reason。半行 JSON 返回已解析帧、truncated=true 和 next_offset，完整损坏行返回 422。_live_done 改为公开 wait_live_idle()，有消费者。

- [ ] Step 4: 运行通过

    pytest tests/env/test_server_runtime.py tests/env/test_replay_acceptance_server.py tests/vis/test_replay_adapter.py -q

用临时 1MB/251MB body、并发连接增删和正在写入的 JSONL 做集成验证；记录 RSS 不随轮询次数二次增长。

- [ ] Step 5: Commit

    git add src/vis/backend/server.py src/mission/replay_adapter.py tests
    git commit -m "fix: harden replay and websocket concurrency"

### Task 9: 缓存、热点和旧路径性能治理（33-40, 57）

**Files:**
- Modify: src/schedule/candidate_extractor.py, src/schedule/state_manager.py, src/schedule/task_allocator.py, src/vis/backend/frame_builder.py, src/utils/obstacle_avoider.py, src/control/common/coordinator.py
- Test: tests/performance/test_branch1_hotspots.py

- [ ] Step 1: 写基准与失败阈值

    def test_rank_search_candidates_vectorized_budget(benchmark): ...
    def test_info_value_grid_cache_invalidates_on_planning_map_version(): ...
    def test_route_caches_are_bounded_and_evicted_on_version_change(): ...
    def test_task_cells_are_reused_when_generation_unchanged(): ...
    def test_mission_snapshot_light_path_skips_full_rebuild(): ...

固定 374,866-cell fixture；阈值相对基线至少降低 50%，缓存条目有上限（route 512、metrics 512、geometry 2048，可配置）。

- [ ] Step 2: 运行基准确认失败

    pytest tests/performance/test_branch1_hotspots.py --benchmark-only -q

- [ ] Step 3: 实现

NumPy 向量化候选排序；info/value 按 (planning_map_version,bbox) 缓存；route metrics key 只含版本/hash 而非整张 isfinite().tobytes()；版本变化清理/淘汰缓存；fragment 枚举用 connected components/剪枝；frame builder 缓存 immutable task_cells；light trigger 只重配对；RRT 使用空间索引和受限 anchor。删除 _return_route_distance 的重复 allow_zero 分支。

- [ ] Step 4: 运行性能和正确性

    pytest tests/performance/test_branch1_hotspots.py tests/schedule tests/env/test_coverage_frame.py -q
    python scripts/evaluate_goal.py --scenario branch1-hotspots --repeat 10

输出 p50/p95、缓存峰值、快照墙钟；任何优化若改变任务选择/覆盖集合则回滚该优化。

- [ ] Step 5: Commit

    git add src/schedule src/vis/backend/frame_builder.py src/utils/obstacle_avoider.py src/control/common/coordinator.py tests/performance
    git commit -m "perf: bound planning caches and remove repeated grid work"

### Task 10: 全量验证、兼容审计和交付门禁

**Files:**
- Modify: docs/validation/2026-09-19-branch1-execution.md
- Create: scripts/validate_branch1_audit.py
- Test: all existing tests plus tests/regressions/

- [ ] Step 1: 静态审计

    python scripts/validate_branch1_audit.py

脚本检查裸 except/通用 except 是否带错误码和日志；or heading、精确浮点基地比较、setdefault("mode")、__class__.__name__、Queue.empty()、% 5 == 0、+1000、硬编码 search area 等模式为零（允许带注释的兼容测试夹具）。

- [ ] Step 2: 分层测试

    pytest tests/regressions tests/control tests/env tests/mission tests/schedule tests/utils tests/vis -q
    npm --prefix src/vis/frontend test -- --runInBand

- [ ] Step 3: 场景验收

运行矩形网格、障碍 fail-closed、天气重规划、目标消失、录制 logger 失败、WebSocket 并发、被动定位噪声、replay 半行和 30/60/120 分钟三档场景。每个场景输出 JSONL/CSV、seed、HEAD SHA、命令和结果；live/fixture/replay 分开标记，额度不足时标 BLOCKED。

- [ ] Step 4: 回归门槛

必须满足：
1. 全量 pytest 与前端测试通过；
2. 审计 1/2/3/5/6/7/8/9/10/11/16/17/18/20/21/22/23/25/26/27/28/29/30/31/32/34-42/44-52/54/59/60/62-64/67 各有通过测试或静态证据；
3. 4/15/43 的现有修复回归通过；
4. 覆盖 fail-closed、天气重规划保留 unaffected task，且不可把返航任务当作覆盖完成；
5. 性能基准达到 Task 9 阈值，缓存有界，录制 flush durability 语义成立。

- [ ] Step 5: 交付审核包

    git status --short
    git log --oneline -12

在执行记录末尾写入未解决项（只能是明确 BLOCKED）、测试摘要、性能表、兼容路径说明和待 reviewer 决策；不得直接合并或发布。

## 覆盖性自检

- 已修复/验证：1-3、5-11、16-18、20-25、26-32、34-42、44-52、54、59-60、62-64、67。
- 已纳入兼容/设计取舍测试：12-14、19、27、30、40-41、49、55-58、61、65-66。
- 已明确保留并回归验证：4、15、43。
- 已明确悬置决策：新障碍 fail-closed；weather replan 必须改变受影响 route、保留 unaffected task；conflict 同格冲突、最长 horizon、显式方向生效。

