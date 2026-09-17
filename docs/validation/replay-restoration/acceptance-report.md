# Replay Restoration Acceptance Report

日期：2026-09-17  
实现分支：`feature/replay-visual-restoration`  
验证提交：`fb43889d8f154de62cdf420fea21a1029009dd90`  
目标合并分支：`branch1`

## 结论

基础回放、真实控制路线、SAR/EO 状态、任务生命周期和前端回放均已通过离线验收。T11 的 AIS、无源协同、信息闭环、意图、接触调查、动态船舶/红方链路也有真实引擎专项产物。T12 的天气重规划、基地恢复、V07 分类与接力由真实控制器和 `SafetyEnvelope` 执行，不是仅写状态标签。

正式 LongCat/live 模型未执行：当前环境没有 `LONGCAT_API_KEY`。因此本报告不把 fixture 结果记为正式模型效果，I14 跨 episode 正式策略记忆验证保持未完成。

## 运行证据

| 产物 | 结果 | 关键事实 |
| --- | --- | --- |
| `t15-v06-20-20260917` | 20/20，审计 0 | 启动与真实路线帧 |
| `t15-v06-120-20260917` | 120/120，审计 0 | SAR、EO、搜索完成 |
| `t15-v06-480-20260917` | 480/480，审计 0 | SAR/EO、返航、搜索完成 |
| `t15-v06-seed101-480-20260917` | 480/480，审计 0 | 第二 seed 长跑 |
| `t12-v07-480-20260917` | 480/480，审计 0 | t24 分类、t26 交接、t27 EO 锁 |
| `t15-features-42-20260917/*` | 11 个子场景各 120/120 | T11-T13 专项均 finished，审计 0 |

所有路径均在 `outputs/validation/replay-restoration/` 下。V07 关键事件的同一 `handoff_id` 顺序为：`handoff_required` t26 → `handoff_assignment_committed` t26 → `handoff_eo_lock_acquired` t27。V07 的 fixture 只提供合法 II 类 assessor 响应，样本仍由真实 EO 传感器采集；接班机通过真实 coordinator 任务和控制执行获得 EO 锁。

## 浏览器验收

专用输入目录为 `outputs/validation/replay-restoration/t14-browser-20260917`，`sources.json` 记录 V01/V03/V06/V07 JSONL 来源和 SHA-256。命令结果：

- `npm run build`：通过。
- 原有静态 acceptance：`13 passed`。
- 专用 `playwright.replay.config.js`：`7 passed`。
- 截图目录：`t14-browser-20260917/screenshots-final/`。
- 导出：`screenshots-final/v07-seed42-replay.mp4`，MP4 可解析，26.888 秒，29,617,626 字节。

事件分镜截图包括 assignment、assessment、return、handoff、probe baseline/near/tracking、V06 SAR、V07 最终帧以及 t120/t480。测试等待目标帧、字体和图片加载，检查 canvas 像素、marker 去重、未加载 seek、文件切换和 1440/1280/768/390 布局溢出；截图已人工抽查。

## 三类效果结论

| 结论栏 | 状态 | 说明 |
| --- | --- | --- |
| 基础效果 | 通过 | B01-B09 的真实引擎/回放/控制链路和基础图层均有测试与产物。 |
| 改进功能接入 | 通过 | I01-I13 有入口、状态、下游消费者和专项证据；I11 的 required→assignment→EO lock 已在 V07 长跑中出现。 |
| 正式模型效果 | 未执行 | 无 `LONGCAT_API_KEY`，没有用 fixture 冒充 live；在线识别精度和 I14 合法历史注入待有真实数据后单独验证。 |

25 项逐项结论见 [feature-integration-matrix.md](feature-integration-matrix.md)，每行保留入口、正反例、专项测试和产物路径。执行过程与失败修复见 [execution-log.md](execution-log.md)。

## 性能与审计

同一固定 900x700 frame 集合的 500 次浏览器构帧样本：baseline 重测 P50/P95 为 `0.9/29.4 ms`，当前为 `1.0/29.0 ms`。这是当前环境的对照记录，不据此声称性能提升。V06 seed42、V06 seed101、V07 的 `--check-log` 均返回 `frames=480`、`issues=[]`。

全量 Python 回归为 `1383 passed, 1 warning in 518.33s`。warning 是 `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1` 下未注册的既有 `pytest.mark.timeout`，不影响测试结果。

## 重放说明

```bash
cd /home/shuixia/users/houguoqiang/projects/Maritime-Surveillance

# 运行一个新 fixture，output-dir 必须使用新的 run-id
python scripts/validate_replay_restoration.py \
  --scenario V07 --seed 42 --steps 480 --transport fixture \
  --output-dir outputs/validation/replay-restoration/<new-run-id>

# 只读审计已有 frames，不改变产物
python scripts/validate_replay_restoration.py --check-log \
  outputs/validation/replay-restoration/t12-v07-480-20260917/frames.jsonl

# 使用真实回放输入启动浏览器验收
cd src/vis/frontend
REPLAY_ACCEPTANCE_DIR="/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/replay-visual-restoration/outputs/validation/replay-restoration/t14-browser-20260917" \
REPLAY_SCREENSHOT_DIR="/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/replay-visual-restoration/outputs/validation/replay-restoration/t14-browser-20260917/screenshots-final" \
  npx playwright test --config playwright.replay.config.js
```

在回放中可从 V07 的 `00:24:00` 查看分类、`00:26:00` 查看返航/交接要求、`00:27:00` 查看接班 EO 锁；V06 的 `01:05:00` 查看 SAR 扫描。侧栏和事件详情使用同一 production frame/event 数据，回放模式不允许写入意图或船舶命令。
