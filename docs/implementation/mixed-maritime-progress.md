# Mixed Maritime Implementation Progress

日期：2026-09-15。分支：`feature/mixed-maritime-llm`。

## Task Status

| Tasks | 状态 | 说明 |
| --- | --- | --- |
| T01-T08 | 已完成 | 船舶生成、AIS/接触、红方轨迹、证据研判和基础状态契约已接入 |
| T09-T11 | 已完成 | probe、意图候选、统一任务选择与可行配对已接入 |
| T12-T13 | 已完成 | 主循环原子安装、故障暂停、意图命令队列和 HTTP API 已接入 |
| T14 | 已完成 | mission-frame/v2、接触证据面板、人工框选和旧帧回放兼容已接入 |
| T15 | 已完成 | 分域 episode 日志、实际执行时间指标和终判覆盖率已接入 |
| T16 | 已完成 | 候选策略记忆、白名单条件、模型前置隐私校验和受限 Prompt 注入已接入 |
| T17 | 已完成 | paired validation/holdout、报告状态机、激活/回滚及 live CLI 已接入 |
| T18 | 自动化收尾完成 | 八场景 fixture、V01-V18 追踪文档、旧文档迁移、完整后端回归和前端验收已完成；真实 live 仍待具备 LongCat key 的环境 |

## Evidence

- T12 专项：35 passed。
- T11-T13 及 frame/evaluator 回归：132 passed。
- 策略记忆与 LLM Reviewer 回归：48 passed。
- 策略验证 CLI：7 passed。
- 八场景端到端 fixture：4 passed。
- 完整后端回归：`1125 passed`。
- 新前端 Playwright：`3 passed`。
- 前端 `npm run build`：通过。
- 接触清除合并回归：`73 passed`；修复了旧 TRACK 任务在 `civilian_released` 后重新绑定已清除 alias 的问题。

上述测试不代表真实 LongCat 分类准确率。真实 smoke 和 90 回合策略验证必须使用
默认网关、真实 API 调用和独立输出目录；在当前记录中尚未执行，也没有激活任何策略
记忆。

## Remaining Checks

1. 在具备 `LONGCAT_API_KEY` 的环境运行至少四个 live smoke；当前环境该变量缺失，不能用 `OPENAI_API_KEY` 代替 LongCat 配置。
2. 若要评估自演进，执行 60+30 paired live 回合；只有两阶段门槛均通过才允许激活。
3. 现有 acceptance suite 需要完整 FastAPI/WebSocket 仿真服务；本次已完成独立 mixed-maritime Playwright，未把仅有 Vite 的开发端口冒充后端验收。

实现没有自动部署、合并分支或覆盖用户审阅记录。
