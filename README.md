# UAV Maritime Surveillance Scheduler

> 术语已按 2026-09-16 统一：分类使用 I 类船舶/II 类船舶，运行时值使用 `type_i`/`type_ii`。

基于 LLM（LongCat）的 UAV 编队海上侦察动态任务调度系统。在 300 km × 300 km 海域中，10 架固定翼 UAV 执行区域覆盖搜索（SAR）与目标跟踪监视（EO/IR）。程序生成合法搜索矩形，LLM 根据分区覆盖缺口选择任务 ID，确定性匹配器分配 UAV。所有单 UAV 命令经过统一的 `ControlCoordinator`，默认使用 heuristic 控制策略。

当前对齐实现还包含混合海上目标、全局卫星 AIS、递进式 EO 核查、被动辐射探测、
人工重点区和受验证策略记忆。当前实现和测试状态见
[混合海上验证记录](docs/MIXED_MARITIME_VALIDATION.md)；参数口径见
[系统参数手册](docs/SYSTEM_PARAMS.md)。本文后面的 GOAL/GOAL2 章节是历史能力说明，
不替代当前方案的 `vessel_class/activity`、观测证据和 live 验证口径。

## 2026-09-15 需求对齐

- 船舶类别与活动状态分离：`unknown/type_i/type_ii` 和
  `unknown/normal/suspected_violation/confirmed_violation`。
- 被动接收器只向蓝方发布含噪方位；只有同一 `sample/source/burst` 中至少两架不同
  UAV 成功探测时，环境边界才释放真实 `PassivePosition`。硬探测范围外不产生观测。
- 中央信息策略以 `EvidenceRecord` 更新 `I/S/A/V`，每个事务只递增一个
  `information_version`，并把 dirty bbox、原因和证据 ID 传给候选池及调度审计。
- 调度使用完整可行候选池和公平 Prompt 窗口；LLM 选择任务 ID，确定性匹配器选择 UAV。
- fixture 评估不会冒充真实模型达标；使用 `--transport live` 的报告才属于真实 API 验证。

快速 fixture 验证：

```bash
LONGCAT_API_KEY=offline-test PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  python scripts/evaluate_mixed_maritime.py --config configs \
  --seeds 101,102,103,104,105 --repeat 3 \
  --output /tmp/maritime-alignment-fixture.json --transport fixture
```

---

## 一、总体算法工作逻辑

```
                        ┌─────────────────────────┐
                        │    仿真时钟 (1 min/step)  │
                        └──────────┬──────────────┘
                                   │
            ┌──────────────────────┼──────────────────────┐
            ▼                      ▼                      ▼
     ┌─────────────┐     ┌─────────────┐        ┌─────────────┐
     │  障碍物更新   │     │  船舶机动     │        │ 控制运行时推进 │
     │ (雷云移动/消散)│     │ (zigzag/编队) │        │ (Coordinator) │
     └─────────────┘     └─────────────┘        └──────┬──────┘
                                                       │
                              ┌────────────────────────┘
                              ▼
                    ┌──────────────────┐
                    │  传感器检测更新    │
                    │ SAR扫描 / EO跟踪  │
                    └────────┬─────────┘
                             │
                    ┌────────▼────────┐
                    │ 事件触发管理器    │
                    │ (TriggerManager) │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
          Heavy          Light           None
       (LLM全管线)    (仅Hungarian)    (跳过)
              │              │
              └──────┬───────┘
                     ▼
            ┌─────────────────┐
            │  任务分配协调器   │
            │ (TaskAllocator)  │
            └────────┬────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   跨区任务选择   UAV↔任务配对   路径规划
   (快照与硬约束) (最小代价匹配) (A* + Dubins)
```

每仿真步（1 分钟）执行一次上述循环。核心调度器按事件或周期兜底触发重规划；任务完成释放资源时，通过现有 `mission_task_released` 事件触发 heavy 决策并刷新覆盖摘要。仅重配已批准任务的 light 路径仍不调用模型。

### 数据流总览

```
信息场与价值场 → 程序生成合法候选 → 分区轮转窗口 → LLM 选择任务 ID → 校验与配对 → UAV 执行
实际 SAR 时间戳 → 分区覆盖摘要 → 自适应搜索预算与分区配额 ────────┘
```

---

## 二、核心：分区滚动覆盖与 UAV 调度

### 2.1 当前任务决策架构

程序生成合法几何 → 按分区轮转形成候选窗口 → LLM 选择任务 ID → 硬约束校验 → 确定性最小代价配对 → UAV 执行。模型不生成或修改 bbox，也不直接指定 UAV；Reviewer、目标评估等角色仍有各自的模型调用。

覆盖路径默认划分 3×3 个区域，每轮从实际 SAR 时间戳统计未扫、超期和在途工作。搜索比例为 `0.4 + 0.6 × gap_pct / 100`，以空闲 UAV 数计算 `ceil(available × fraction)`；忙碌和转场 UAV 不重复计入。候选几何和可行边限制可分配数量，不足时明确报告 `insufficient_available_resources`。

有效缺口达到阈值（默认 0.5）的区域按缺口降序获得一个配额，总配额不超过搜索预算。配额代表必须完整包含于该区，与全局搜索共用一次最大匹配；无法匹配的区域记录在 `zone_infeasible`，不伪造任务。已批准或执行中、已配对且 UAV 可工作的搜索类任务按覆盖格的并集扣除有效缺口，但不会改变实际扫描时间。

当全域未扫、十架空闲 UAV 且几何和航路可行时，首轮为十个普通搜索任务，照顾九区并增加一个不重叠搜索。完成事件和周期 heavy 决策重新计算摘要与配额；无 coverage metrics 的 legacy 路径保留原行为。

### 2.2 信息场数学模型

信息场为候选任务提供价值依据；覆盖新鲜度与搜索预算另外读取实际 SAR 时间戳，不能用信息素数值冒充已扫描面积。

**信息素指数衰减**：

$$I(c, r, t) = I_0 \cdot e^{-\lambda \cdot \Delta t}$$

其中：

| 符号 | 含义 | 值 |
|------|------|-----|
| $I_0$ | 扫描完成时的初始信息素 | 1.0 |
| $\lambda$ | 衰减常数 | $\ln(2) / T_{\text{half}}$ |
| $T_{\text{half}}$ | 半衰期 | 30 min（搜索）/ 15 min（跟踪） |
| $\Delta t$ | 距上次扫描的时间 | $t_{\text{current}} - t_{\text{last\_scanned}}$ |

**信息价值（复合指标）**：

$$V(c,r) = \alpha \cdot (1 - I) + \beta \cdot S(c,r) + \gamma \cdot A(c,r)$$

| 分量 | 含义 | 权重 |
|------|------|:----:|
| $\alpha \cdot (1 - I)$ | 信息缺口：越久没扫，价值越高 | $\alpha = 1.0$ |
| $\beta \cdot S(c,r)$ | 战略价值场：附近有历史标记点则价值升高 | $\beta = 0.8$ |
| $\gamma \cdot A(c,r)$ | 时效性场：标记点越新，价值越高 | $\gamma = 0.5$ |

参数可在 `configs/grid.yaml` 中调整。

**态势三分法**（将连续信息素映射为离散三档）：

| 态势 | 条件 | $\Delta t$ 等价 | 含义 |
|------|------|:--:|------|
| **白态势** | $I > 0.7$ | $\Delta t < 15\text{ min}$ | 刚扫过，信息新鲜，无需重复搜索 |
| **灰态势** | $0.2 \leq I \leq 0.7$ | $15 \leq \Delta t < 70\text{ min}$ | 信息开始陈旧，可考虑再次搜索 |
| **黑态势** | $I < 0.2$ | $\Delta t > 70\text{ min}$ | 长期未扫描或从未扫描，最高优先级 |

> 目标丢失会影响区域价值与候选优先级；模型只能选择程序提供的任务，仍须满足当轮搜索数量、分区配额和可行边约束。

### 2.3 校验与重试

`MissionScheduler` 检查候选可见性、合法边、资源、代际、抢占、接触状态和矩形不重叠，并要求普通搜索（`kind=search` 且有 bbox）数量恰好等于 `required_new_search_count`，少选和多选都拒绝。核查、跟踪、方向搜索和被动调查不能冒充普通搜索。

全局预算不可行时豁免精确数量和全局 must-service；仍需满足各个可行分区的配额及 must-service。校验失败继续使用原 gateway 重试／失败处理，不用提示词绕过硬约束。

### 2.4 Prompt 结构

System Prompt 见 `src/mission/prompts/mission_scheduler.txt`。每轮载荷包含可见候选、可行边、资源、活动任务、目标与意图，以及 `coverage_summary`、`coverage_constraint` 和 Reviewer 摘要，保留 100 KB 载荷预算。

模型返回 `mission-selection/v1` JSON：

```json
{
  "schema_version": "mission-selection/v1",
  "snapshot_id": "当前快照 ID",
  "selected_task_ids": ["可见任务 ID"],
  "preempt_uav_ids": [],
  "defer_reason": null,
  "notes": "不超过 160 字符的说明"
}
```

任务 ID 表示同时执行的分配，不是未来顺序队列；每个任务必须能匹配不同的合法 UAV。

### 2.5 Reviewer 长期记忆

Reviewer 每 15 分钟（仿真时间）独立于 Decision Maker 运行一次，输出 ≤ 200 字自然语言摘要，注入到下一轮 Decision Maker 的 User Prompt 中，使 LLM 具备**跨周期的任务级情境感知**。

示例输出：

> "过去2小时内，共搜索约45%海域，发现目标群3个共11艘。目标群#1、#3持续跟踪中，目标群#2于47分钟前丢失。UAV-2、UAV-7已各跟踪超90分钟需关注油量。NE象限大面积黑态未搜索，建议下轮优先分配UAV巡查。"

### 2.6 任务 ID 与几何

当前 mission 路径使用程序生成的候选任务 ID（普通搜索如 `search:c0:r0:c1:r1`）。模型只选择可见 ID，执行沿用对应几何。旧 `stability_iou_threshold` 和区域 ID 复用属于 legacy 区域规划路径，不是新模型自由修改矩形的许可。

### 2.7 确定性配对

模型选择同时执行的任务后，`MissionScheduler` 在合法航路边上进行精确最小代价匹配，代价采用转场时间，并检查航程、代际、冷却及抢占权限。任务数不能超过可匹配资源，匹配失败不会偷偷丢弃任务或用非法边补齐。

旧 legacy 路径的 Hungarian／贪心实现仍保留；本轮不改变配对后端。

---

## 三、三大任务模式

每架 UAV 根据当前分配的 mission 处于三种任务模式之一。所有航路必须满足 **Dubins 曲线** 运动学约束。

### 3.1 固定翼 Dubins 运动学

$$R_{\min} = 1 \text{ cell} \ (10 \text{ km})，\text{代表战术转弯半径}$$

Dubins 路径共 6 种类型：`LSL`、`LSR`、`RSL`、`RSR`、`LRL`、`RLR`（L = 左弧，S = 直线，R = 右弧）。给定起点位姿 $(x_0, y_0, \chi_0)$ 和终点位姿 $(x_1, y_1, \chi_1)$，遍历 6 种类型，选总长度最短者。

运动方程（连续时间）：

$$\dot{x} = v \cdot \cos(\chi),\quad \dot{y} = v \cdot \sin(\chi),\quad \dot{\chi} = \frac{v}{R} \cdot u \quad (u \in \{-1, 0, +1\})$$

约束：$|\dot{\chi}| \leq v / R_{\min}$

### 3.2 区域覆盖搜索 — SAR 蛇形扫描

UAV 在分配的搜索矩形区域上执行蛇形（boustrophedon）扫描。SAR 雷达安装在侧面（非正下方），以固定俯角照射海面。

**核心约束**：

- 条带不包含飞行轨迹正下方（侧视成像）
- 每条扫描行必须是直线（SAR 方位向成像要求）
- 相邻条带无缝拼接：第 $n$ 条带的 near-range 边界 = 第 $n-1$ 条带的 far-range 边界
- 高度恒定、速度恒定、加速度零（运动补偿）

**SAR 传感器参数**：

| 参数 | 值 |
|------|-----|
| 条带宽度 | 20 km（2 cells） |
| 检测概率 $P_d$ | 0.90 |
| 虚警率 | 0.01 |
| 模式 | stripmap |

扫描行之间的掉头通过 **Dubins 路径**连接，在搜索区外部完成。

### 3.3 航路规划 — 避障飞行

UAV 从当前位置飞往目标搜索区或返回基地的途中（不启用传感器），使用 **Hybrid A* + Dubins** 两步法避障：

1. **Hybrid A*** 在带航向状态的栅格空间中搜索满足最小转弯半径的路径
2. **Dubins 曲线**将栅格转场转换为可飞行的连续轨迹
3. **超覆盖栅格碰撞检测**确认路径不穿越障碍、不切角

### 3.4 目标跟踪监视 — EO/IR Standoff 盘旋

UAV 绕目标做半径为 $R_d = 1.8$ cells 的圆形盘旋，采用 **LGVF (Lyapunov Guidance Vector Field)** 算法。

**核心思想**：不直接计算航路点，而是定义一个引导向量场，该场的流线收敛到以目标为中心的圆形轨道，UAV 跟随场的梯度方向即可自动收敛。

**Lyapunov 函数**：

$$V = \frac{(r^2 - R_d^2)^2}{2}$$

当 UAV 在轨道上时 $r = R_d$，$V = 0$。目标移动时轨道中心实时跟随平移。多 UAV 协同跟踪时，**Phase Coordinator** 通过微调各 UAV 空速实现等相位分布（$180^\circ$ 对称 / $120^\circ$ 三等分）。

**EO/IR 传感器**：

| 参数 | 值 |
|------|-----|
| 视场角 | 30° |
| 最大作用距离 | 25 km（2.5 cells） |
| 检测概率 | 0.70 |

---

### 3.5 控制策略架构

控制代码按四个目录组织：`common/` 放置不可变观测、动作、事件、lease 和安全契约，`heuristic/` 提供当前内置策略和 Hybrid A* 导航，`bc/` 与 `rl/` 只提供可扩展的抽象基类。单 UAV 的运行时顺序固定为“取出事件 → 构建观测 → controller 决策 → 安全校验 → executor 执行”，实体和调度器不会绕过这条命令路径直接推进任务。

`control_mode` 表示配置的策略类型，`control_owner` 表示当前控制权。普通工作 lease 由 heuristic 或 learning controller 持有；返航、holding 和安全故障处理使用显式的 SYSTEM lease。任务事件只会一次性消费 heuristic lease，learning lease 只接收事件观测，不会被普通任务事件替换。新的 BC/RL 模式必须通过 `ControlFactory.register()` 注入 provider；未注册的模式会快速失败，不会静默回退到 heuristic 或伪造动作。

## 四、事件触发机制

### 4.1 事件分类

| 分类 | 事件类型 | 响应方式 |
|:----:|------|:----:|
| **Heavy** | `target_found`, `target_lost`, `target_departed`, `type_i_released`, `type_ii_confirmed`, `uav_returned`, `lifecycle_completed`, `storm_spawned`, `storm_dissipated` | LLM 全管线 |
| **Light** | `search_complete`, `uav_refueled`, `base_capacity_full`, `uav_fuel_low_warning` | 仅 Hungarian 配对 |
| **周期** | 每 30 min（仿真时间） | Heavy trigger |

### 4.2 判定逻辑

```text
TriggerManager.check():
  1. 收集 pending 事件，过滤 5 min 内的近期事件
  2. 分类统计 heavy_count 和 light_count
  3. 判定:
     if heavy_count > 0 or total ≥ 3:
       → HEAVY (事件驱动，LLM 全管线)
     elif light_count > 0:
       → LIGHT (仅 Hungarian 配对)
     else:
       → 检查周期定时
  4. 周期兜底:
     if current_time - last_heavy_time ≥ 30 min:
       → HEAVY
  5. 事件去重: 同 UAV 的同类型事件在 5 min 窗口内自动合并
```

---

## 五、GOAL2 新增功能

### 5.1 多基地模型

- 基地数量：1–3 个（`configs/environment.yaml` 可配）
- 初始化时随机生成于陆地/岸线位置，基地间距 $\geq 5$ cells
- 每个基地容量上限：**3 架 UAV 同时加油维护**
- UAV 返航时自动选择最近的可用基地；满容时 UAV 进入 `holding` 状态盘旋等待

### 5.2 岛屿与雷云

| 属性 | 岛屿 | 雷云 |
|------|:--:|:--:|
| 形状 | 正方形（1–3 cells） | 正方形（1–4 cells） |
| 动态性 | 静态（初始化固定） | 动态（位置随时间变化） |
| 对 UAV | 可飞越 | **不可穿越**（+ 1 cell 安全余量） |
| 对船舶 | 需绕行 | 需绕行 |
| 对 SAR | 无影响 | 可穿透（SNR 降 30% × intensity） |
| 对 EO/IR | 无影响 | 完全失效 |

### 5.3 舰艇编队与 AIS 判别

- **最大 5 个目标**，最大 3 个编队（Group）
- **舰艇类型**：航空母舰（必伴随 $\geq 2$ 艘驱逐舰）、驱逐舰
- **目标驶离任务区域** → UAV 放弃跟踪，恢复区域覆盖搜索

**AIS 军民判别逻辑**：

| 条件 | 判定 | 动作 |
|------|:--:|------|
| 无 AIS 信号 | **II 类船舶** | 继续跟踪 |
| $\text{dist}(\text{AIS位置}, \text{推算位置}) > 2 \text{ cells}$ | **II 类船舶**（虚假 AIS） | 继续跟踪 |
| $\text{dist}(\text{AIS位置}, \text{推算位置}) \leq 2 \text{ cells}$ | **I 类船舶** | 放弃跟踪，释放 UAV |

### 5.4 雷云规避跟踪（三级响应）

| Level | 条件 | 行为 |
|:-----:|------|------|
| **1** | 雷云在轨道外围 | 临时增大 $R_d$，绕开雷云区域 |
| **2** | 雷云部分覆盖轨道 | 暂停盘旋，Dubins 避障路径绕飞，保持目标在 EO 视场内 |
| **3** | 雷云覆盖目标上空 | UAV 在安全区域等待；EO/IR 失效后基于预测位置重建跟踪 |

### 5.5 态势透明度可视化

Canvas 叠加半透明覆盖层，每个 cell 的 opacity 与信息素 $I(c,r)$ 成反比：

$$\text{opacity} = 1 - I(c,r) \times 0.9$$

- $I = 1.0$（白态势）：opacity = 0.1，几乎透明，海面清晰可见
- $I = 0.0$（黑态势）：opacity = 1.0，深黑遮盖，表示信息缺失

### 5.6 燃油预警

UAV 油量降至 **25%** 时触发 `uav_fuel_low_warning` 事件（Light），调度器提前准备接班 UAV，避免跟踪/搜索因燃油耗尽（8% 临界）而中断。

---

## 六、运行

```powershell
# 完整启动（后端 + 前端）
.\scripts\console.ps1 start

# 仅仿真（无 Web 服务）
python main.py --steps 480 --no-server --step-delay 0

# 使用自定义 Python 环境
.\scripts\console.ps1 start -PythonPath C:\path\to\python.exe

# 跳过 LLM 探活（离线调试）
python main.py --skip-llm-probe
```

仿真完成后在 `outputs/simulation_*.jsonl` 输出每帧 JSON（一行一帧）。

---

## 七、验证

```powershell
python -m pytest -q
cd src/vis/frontend
npm run build
npm run test:acceptance
```

验收标准详见：

- [docs/GOAL.md](docs/GOAL.md) § 七 — V1 基线验证（Dubins、SAR、避障、LGVF、LLM 管线、可视化）
- [docs/GOAL2.md](docs/GOAL2.md) § 十 — GOAL2 增量验证（多基地、AIS 判别、雷云规避、透明度可视化）
- [docs/VALIDATION.md](docs/VALIDATION.md) — 最新验收记录

### 回放视觉恢复验收

2026-09-17 的真实引擎 fixture 长跑、专项集成测试、回放 API 和浏览器视觉验收记录在
[回放视觉恢复验收报告](docs/validation/replay-restoration/acceptance-report.md)；25 项能力逐项状态见
[feature integration matrix](docs/validation/replay-restoration/feature-integration-matrix.md)。运行产物放在
`outputs/validation/replay-restoration/<run-id>/`，不得清理 `outputs/` 根目录。

```bash
# 20/120/480 分钟 V06 fixture 与实时帧审计示例
python scripts/validate_replay_restoration.py \
  --scenario V06 --seed 42 --steps 480 --transport fixture \
  --output-dir outputs/validation/replay-restoration/<new-run-id>
python scripts/validate_replay_restoration.py --check-log \
  outputs/validation/replay-restoration/<run-id>/frames.jsonl

# 专用浏览器验收使用已登记来源的输入目录
cd src/vis/frontend
REPLAY_ACCEPTANCE_DIR="/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/replay-visual-restoration/outputs/validation/replay-restoration/t14-browser-20260917" \
REPLAY_SCREENSHOT_DIR="/home/shuixia/users/houguoqiang/projects/Maritime-Surveillance/.worktrees/replay-visual-restoration/outputs/validation/replay-restoration/t14-browser-20260917/screenshots-final" \
  npx playwright test --config playwright.replay.config.js
```

该记录使用 fixture 验证真实引擎和渲染链路；当前环境未提供 `LONGCAT_API_KEY`，所以报告不会把 fixture 结果标记为 live 模型效果。

---

## 八、核心模块

### 环境引擎 (`src/env/`)

| 模块 | 文件 | 职责 |
|------|------|------|
| Dubins 路径 | `dubins.py` | 六种 Dubins 路径族求解器 |
| SAR 传感器 | `sar_sensor.py` | 侧视条带成像 + SNR 检测模型 |
| EO/IR 传感器 | `eo_sensor.py` | 光电跟踪 + FOV 锥计算 |
| UAV 实体 | `uav_entity.py` | 连续位姿固定翼 UAV，集成 Dubins + LGVF + 传感器 |
| 船舶模型 | `ship.py` | Zigzag 逃逸 + 编队 + ShipType（航母/驱逐舰）+ AIS |
| 障碍物 | `obstacle.py` | 正方形岛屿 + 动态雷云 + 碰撞检测 |
| 基地 | `base_station.py` | 多基地 + 容量约束 + 加油队列管理 |
| 仿真引擎 | `simulation.py` | 环境 + UAV + 船舶 + 调度全集成 |

### 工具库 (`src/utils/`)

| 模块 | 文件 | 职责 |
|------|------|------|
| 覆盖规划 | `coverage_planner.py` | Dubins 蛇形 SAR 扫描路径生成 |
| 避障规划 | `control/heuristic/navigation.py` | Hybrid A* + Dubins 避障路径规划 |
| 跟踪轨道 | `track_orbit.py` | LGVF Standoff 跟踪引导 |
| 相位协调 | `phase_coordinator.py` | 多 UAV 等相位空速协调 |
| AIS 判别 | `ais_discriminator.py` | AIS 信号对比 + 军民分类决策 |

### 调度管线 (`src/schedule/`)

| 模块 | 文件 | 职责 |
|------|------|------|
| 信息场 | `info_field.py` | $I(c,r)$ 指数衰减 + 标记点高斯扩散 |
| 信息价值表 | `info_value_table.py` | 区域级信息统计 + 完成率 |
| 候选提取 | `candidate_extractor.py` | BFS 连通聚类 + 矩形拟合 + 碎片检测 |
| Prompt 构建 | `prompt_builder.py` | System + User Prompt 动态组装 |
| LLM 客户端 | `llm_client.py` | LongCat API 调用 + 校验-重试闭环 |
| 输出校验 | `output_validator.py` | 9 条规则验证 LLM 输出 |
| Hungarian | `hungarian.py` | 最小代价二分图最优匹配 |
| 触发管理 | `trigger_manager.py` | 事件驱动 + 周期 + 去重 |
| 任务分配 | `task_allocator.py` | **五层决策架构编排器** |
| 状态管理 | `state_manager.py` | UAV/区域/标记点/事件权威状态 |
| Reviewer | `llm_reviewer.py` | 长期记忆生成 |

### 控制策略 (`src/control/`)

| 目录 | 职责 |
|------|------|
| `common/` | 统一观测、动作、事件、lease、安全 envelope、executor 和 coordinator |
| `heuristic/` | 覆盖、跟踪、返航、holding 以及 Hybrid A* 控制实现 |
| `bc/` | `BCControllerBase` 抽象接口，由外部 provider 提供实现 |
| `rl/` | `RLControllerBase` 抽象接口，由外部 provider 提供实现 |

### 可视化 (`src/vis/`)

| 模块 | 路径 | 职责 |
|------|------|------|
| WebSocket 服务 | `backend/` | 直播推送 + JSONL 回放帧服务 |
| Canvas 渲染 | `frontend/src/renderer/` | 9+ 层 Canvas 2D 渲染 |
| UI 组件 | `frontend/src/components/` | RightSidebar, BottomDrawer, PlaybackBar |

---

## 九、配置

配置文件位于 `configs/` 目录：

| 文件 | 内容 |
|------|------|
| `environment.yaml` | 海域尺寸、基地数量/容量、障碍物参数 |
| `uav.yaml` | UAV 数量、速度、续航、加油时间 |
| `ship.yaml` | 目标数量、编队、航速、舰型、AIS 参数 |
| `sensor.yaml` | SAR / EOIR / Radar 传感器参数 |
| `grid.yaml` | 网格分辨率、信息场衰减参数、候选提取阈值 |
| `llm.yaml` | LLM 重试策略、触发周期 |
| `llm_params.yaml` | LLM Provider / Model / API 绑定 |
| `control.yaml` | 默认控制模式、per-UAV 策略、安全和导航参数 |
