# UAV Maritime Surveillance Scheduler

基于 LLM（LongCat）的 UAV 编队海上侦察动态任务调度系统。在 300km×300km 海域中，10 架固定翼 UAV 执行区域覆盖搜索（SAR）与目标跟踪监视（EO/IR），LLM 作为全局决策器动态划分搜索区域，Hungarian 算法负责 UAV 与区域的最优配对。

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
     │  障碍物更新   │     │  舰船机动     │        │  UAV 状态推进 │
     │ (雷云移动/消散)│     │ (zigzag/编队) │        │ (Dubins+LGVF)│
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
   任务区域划分   UAV↔区域配对   路径规划
   (5层决策架构)  (Hungarian)   (Dubins)
```

每仿真步（1 分钟）执行一次上述循环。核心调度器不每步调用 LLM——仅在**事件驱动**（目标发现/丢失/驶离、UAV返航、雷云变化）或**周期兜底**（30 分钟）时触发重分配。轻量事件（搜索完成、加油完成）仅走 Hungarian 重新配对，不调用 LLM。

### 数据流总览

```
信息场 I(c,r)  ──→  价值场 V(c,r)  ──→  候选区域提取  ──→  LLM 区域划分  ──→  Hungarian 配对  ──→  UAV 路径规划
    │                    │                   │                   │                   │                   │
 30×30 网格         V=α(1-I)+βS+γA      BFS聚类+矩形拟合     LongCat API        二分图最优匹配      Dubins+Coverage
 指数衰减模型        战略+时效性加权      Top-K截断输出       校验-重试闭环       scipy/greedy       蛇形扫描/LGVF
```

---

## 二、核心：LLM-based 任务区域划分与 UAV 调度

> 这是整个系统最核心的模块。传统调度器用确定性规则分配任务，本系统用 **LLM 作为全局态势理解与策略生成的决策器**，确定性算法（信息场、候选提取、Hungarian 配对、输出校验）负责数值计算与约束保障，形成 **"AI 决策 + 运筹学执行"** 的混合架构。

### 2.1 五层决策架构

```
仿真世界 (30×30 信息场)
  │
  ▼
┌─────────────────────────────────────────────────┐
│ 第 1 层：信息场 (InfoField)                      │
│   30×30 网格，每 cell 维护两个数学量：            │
│   信息素 I(c,r) — 指数衰减，半衰期 30/15 min     │
│   价值   V(c,r) = α(1-I) + β×S + γ×A            │
│   态势三分：I>0.7→白, 0.2≤I≤0.7→灰, <0.2→黑      │
│   作用：为上层提供"哪里最值得搜索"的数值依据       │
└──────────────────────┬──────────────────────────┘
                       │ get_value_matrix()
                       ▼
┌─────────────────────────────────────────────────┐
│ 第 2 层：候选区域提取 (CandidateExtractor)        │
│   将 900 个 cell 的连续价值场转化为 LLM 可理解    │
│   的离散候选区域列表（确定性算法，不调 LLM）：      │
│   · BFS 四连通聚类 (V ≥ threshold 且未占用的 cell) │
│   · 按总价值 ∑V 降序排序                         │
│   · Top-K 截断 (K = min(available_uavs×2, 10))   │
│   · 矩形拟合 (迭代 stack：膨胀→切分→细分)         │
│   · 碎片检测 (跟踪区挖洞残留 < 12 格)              │
│   输出：候选区域列表 + 碎片提醒                   │
└──────────────────────┬──────────────────────────┘
                       │ candidates + fragments
                       ▼
┌─────────────────────────────────────────────────┐
│ 第 3 层：LLM 决策器 (LLMClient)                   │
│   ★ 唯一调用 LLM 的环节 ★                        │
│   · PromptBuilder 组装 System + User Prompt     │
│   · System Prompt：角色定义 + 7 条约束 + 输出格式  │
│     - 矩形性、尺寸 20-40格、长宽比≤2:1            │
│     - 不重叠、不覆盖跟踪区、数量≤可用UAV           │
│     - 稳定性(IoU≥0.7)、碎片合并、优先级            │
│   · User Prompt：候选区 + 跟踪区 + 上轮状态 +     │
│     碎片提醒 + UAV 状态 + Reviewer 长期记忆       │
│   · LongCat API → 输出 JSON 区域方案              │
│   · OutputValidator：7 条规则校验                 │
│   · 失败 → 错误回注到下一轮 Prompt → 重试 (≤2次)  │
│   · LLM 只输出"哪些矩形区域"，不做 UAV 配对       │
│   输出：经过校验的区域划分方案                     │
└──────────────────────┬──────────────────────────┘
                       │ search_regions[]
                       ▼
┌─────────────────────────────────────────────────┐
│ 第 4 层：Hungarian 配对                          │
│   求解二分图最小代价匹配（确定性算法）：           │
│   · 代价矩阵 C[i][j] = EuclideanDist(UAV[i],     │
│       Region[j].bbox.center)                    │
│   · scipy.linear_sum_assignment 最优解           │
│   · 无 scipy → 贪心回退 (按 cost 升序)           │
│   · |UAV| > |Regions| → 多余 UAV 回基地待命      │
│   · |Regions| > |UAV| → 低优先级区回候选池       │
│   输出：(uav_id, region_id) 配对列表              │
└──────────────────────┬──────────────────────────┘
                       │ assignments
                       ▼
┌─────────────────────────────────────────────────┐
│ 第 5 层：触发管理器 (TriggerManager)              │
│   决定"何时"触发重分配（见第四章）：               │
│   · 事件驱动 + 分级响应 (heavy/light)             │
│   · 周期兜底 (30 min heavy trigger)              │
│   · 5min 内 ≥3 事件 → 合批为一次 heavy 触发       │
│   · 同 UAV 同类型事件去重                         │
└─────────────────────────────────────────────────┘
```

### 2.2 信息场数学模型

信息场是所有决策的数值基础。LLM 之所以能做出合理划分，是因为信息场已将"哪里值得搜索"量化为可比较的数值。

**信息素指数衰减**：
```
I(c, r, t) = I₀ · e^(-λ · Δt)

其中:
  I₀ = 1.0                      扫描完成时的初始信息素
  λ  = ln(2) / T_half            衰减常数
  T_half = 30 min (搜索) / 15 min (跟踪)
  Δt = t_current - t_last_scanned
```

**信息价值（复合指标）**：
```
V(c,r) = α · (1-I)          信息缺口：越久没扫 → V 越高
       + β · S(c,r)           战略价值场：附近有标记点 → V 升高
       + γ · A(c,r)           时效性场：标记点越新 → V 越高

α=1.0, β=0.8, γ=0.5 (configs/grid.yaml 可调)
```

关键洞察：V(c,r) 在"刚丢失目标处"最高（高信息缺口 + 高战略价值 + 高时效性），引导 CandidateExtractor 优先提取该区域，LLM 优先在此划搜索区——目标丢失后的快速重搜索自动发生。

### 2.3 LLM 校验-重试闭环

```
┌─ LLM API 调用 ──────────────────────────┐
│  LongCat ChatCompletions API             │
│  model: LongCat-2.0                      │
│  temperature: 0.3                        │
│  response_format: json_object            │
└──────────────────────────────────────────┘
                │
                ▼
      ┌─ _parse_json() ─────┐
      │ 1. 直接解析 JSON     │
      │ 2. 从 ```json``` 提取 │
      │ 3. 失败 → 返回 None  │
      └────────┬─────────────┘
               │
               ▼
      ┌─ OutputValidator ────────────────┐
      │ 1. bbox 坐标 [0,29]              │
      │ 2. 面积 20–40 格                 │
      │ 3. 长宽比 ≤ 2.0                  │
      │ 4. 搜索区互不重叠                 │
      │ 5. 不与跟踪区/障碍物重叠          │
      │ 6. 总数: search+track ≤ 10       │
      │ 7. 稳定性: 同ID IoU ≥ 0.7        │
      │ 8. 区域ID在候选bbox中            │
      │ 9. 数量 ≥ required_search_regions │
      └────────┬─────────────────────────┘
               │
     ┌──── is_valid? ────┐
     │ 是                 │ 否 (attempt ≤ max_retries)
     ▼                    ▼
  提交方案         错误信息追加到 user_prompt
                   重新调用 LLM (最多 2 次)
                       │
                       ▼
                仍失败 → 返回空方案 (fail-closed)
```

### 2.4 Prompt 结构

**System Prompt（固定角色+约束）**：
```
你是 UAV 编队任务调度决策器。任务：300×300km 海域对海侦察与目标跟踪。

【区域划分约束】
1. 矩形性：输出轴对齐矩形，坐标系 (col,row)，范围 [0,29]×[0,29]
2. 尺寸范围：搜索区 20–40 格，长宽比 ≤ 2:1
3. 数量上限：区域数 ≤ 可用 UAV 数（最多 10）
4. 不重叠：区域之间互不相交
5. 稳定性：与上一轮同区域偏差 ≤ 30% 面积
6. 碎片合并：< 12 格的碎片须并入相邻区域
7. 优先级：高价值黑态区 > 灰态区 > 白态区

【输出格式】严格 JSON，无额外文字：
{
  "search_regions": [
    {"id": "S1", "bbox": [c0,r0,c1,r1], "priority": "high|medium|low", "reason": "说明"}
  ],
  "notes": "简要说明本周期方案"
}
```

**User Prompt（每轮动态组装）**：
```
【长期记忆】（来自 Reviewer，首次为空）
过去2h内，共搜索约40%海域，发现目标群3个(11艘)…

【候选搜索区域】（来自 CandidateExtractor，按价值降序，最多10个）
1. bbox(18,14,24,19) 面积30格 平均信息0.08(黑) 总价值42.3
2. bbox(5,22,11,28) 面积24格 平均信息0.15(黑) 总价值38.7
…

【跟踪中区域】（不参与重划）
T1: bbox(12,8,16,12) UAV=UAV-3

【上一轮搜索区状态】
S1: bbox(…) 信息0.82(白) 状态=completed UAV=UAV-1

【碎片提醒】
区域S1被T1挖除后产生8格碎片，需合并

【UAV 可用状态】
现可用UAV: 2架  UAV-1: searching, 油量72%  …

请输出本周期任务区域划分方案。
```

### 2.5 Reviewer 长期记忆

```
触发周期: 每 15 分钟仿真时间
输入: 仿真开始以来的事件流摘要
输出: ≤200 字自然语言，覆盖上一轮

示例:
"过去2小时内，共搜索约45%海域，发现目标群3个共11艘。
目标群#1、#3持续跟踪中，目标群#2于47分钟前丢失。
UAV-2、UAV-7已各跟踪超90分钟需关注油量。
NE象限大面积黑态未搜索，建议下轮优先分配UAV巡查。"
```

Reviewer 记忆注入到下一次 Decision Maker 的 User Prompt，使 LLM 具备**跨周期的任务级情境感知**。

### 2.6 区域 ID 连续性

```
新区域与上一轮同ID区域 IoU ≥ stability_iou_threshold (默认 0.7)
  → 复用上一轮的区域 ID
  → 稳定性：UAV 不会因 ID 变化而重新分配

IoU = intersection_area / union_area
```

即使 LLM 每次重新划分区域，只要变化不大，UAV 就在原区域继续工作，避免频繁切换。

---

## 三、三大任务模式

每架 UAV 根据当前分配的 mission 处于三种任务模式之一。所有航路必须满足 **Dubins 曲线** 运动学约束。

### 3.1 固定翼 Dubins 运动学

```
状态: (x, y, χ) — 2D 位置 + 航向角
转弯半径: R_min = 1 cell (10km)，代表战术转弯半径

Dubins 路径类型 (6种):
  LSL, LSR, RSL, RSR, LRL, RLR
  (L=左弧, S=直线, R=右弧)

给定起点 pose 和终点 pose → 遍历 6 种类型 → 选最短路径
```

### 3.2 区域覆盖搜索 — SAR 蛇形扫描

UAV 在分配的搜索矩形区域上执行蛇形扫描，SAR 雷达安装在侧面（非正下方）。

**核心约束**：
- 条带不包含飞行轨迹正下方（侧视成像）
- 扫描行必须是直线（SAR 方位向成像要求）
- 相邻条带无缝拼接（near-range = 上一条带的 far-range）
- 高度恒定、速度恒定、加速度零（运动补偿）

**SAR 传感器参数**：
- 条带宽度：20km (2 cells)
- 检测概率：P_det = 0.90
- 虚警率：0.01

**扫描行间的掉头**通过 Dubins 路径连接，在搜索区外部完成。

### 3.3 航路规划 — 避障飞行

UAV 从当前位置飞往目标搜索区/基地途中（不启用传感器），使用 **RRT\* + Dubins** 两步法避障：

```
Step 1: RRT* 在 2D 空间中搜索几何路径（目标偏置采样 10%）
Step 2: 用 Dubins 曲线平滑几何路径的每个拐角
Step 3: Bresenham 碰撞检测确保路径安全
```

### 3.4 目标跟踪监视 — EO/IR Standoff 盘旋

UAV 绕目标做圆形盘旋，采用 **LGVF (Lyapunov Guidance Vector Field)** 算法：

```
不直接计算航路点，而是定义引导向量场
该场的流线收敛到目标圆形轨道 (R_d = 1.8 cells)
UAV 跟随场的梯度 → 自动收敛到轨道上

Lyapunov 函数: V = (r² - R_d²)² / 2
  当 UAV 在轨道上时 r = R_d, V = 0

目标移动 → 轨道中心实时跟随平移
多 UAV 协同 → Phase Coordinator 通过微调空速实现等相位分布
```

**EO/IR 传感器**：
- 视场角：30°
- 最大作用距离：25km (2.5 cells)
- 检测概率：0.70

---

## 四、事件触发机制

### 4.1 事件分类

| 分类 | 事件 | 响应 |
|------|------|:--:|
| **Heavy** | `target_found`, `target_lost`, `target_departed`, `civilian_released`, `target_military`, `uav_returned`, `lifecycle_completed`, `storm_spawned`, `storm_dissipated` | LLM 全管线 |
| **Light** | `search_complete`, `uav_refueled`, `base_capacity_full`, `uav_fuel_low_warning` | 仅 Hungarian 配对 |
| **周期** | 每 30 min（仿真时间） | Heavy trigger |

### 4.2 触发逻辑

```
TriggerManager.check():
  1. 收集 pending 事件
  2. 分类: heavy_events, light_events
  3. 判定:
     if heavy_count > 0 or total ≥ 3 → HEAVY (事件驱动)
     elif light_count > 0 → LIGHT (无需 LLM)
     else → 检查周期定时
  4. 周期兜底:
     if current_time - last_heavy_time ≥ 30min → HEAVY
```

---

## 五、GOAL2 新增功能

### 5.1 多基地模型

- 基地数量：1–3 个（`configs/environment.yaml` 可配）
- 初始化时随机生成于陆地/岸线位置
- 基地间距 ≥ 5 cells
- 每个基地最多同时维护 3 架 UAV
- UAV 返航时自动选择最近可用基地
- 基地满容时 UAV 进入 holding 状态

### 5.2 岛屿与雷云

- **岛屿**：静态正方形 (1–3 cells)，船舶需绕行，UAV 可飞越
- **雷云**：动态正方形 (1–4 cells)，UAV 不可穿越，SAR 可穿透
- 雷云位置动态变化，生命周期结束自动消散并补充新雷云

### 5.3 舰艇编队与 AIS 判别

- 最大 5 个目标，最大 3 个编队 (Group)
- 舰艇类型：航母（必伴随 ≥2 艘驱逐舰）、驱逐舰
- 目标驶离任务区域后 UAV 放弃跟踪，恢复区域搜索
- **AIS 军民判别**：
  - 无 AIS 信号 → 军舰
  - AIS 位置与 EO/IR 推算位置偏差 > 2 cells → 军舰（虚假 AIS）
  - 位置一致 → 民船 → 放弃跟踪

### 5.4 雷云规避跟踪

三级响应机制：轨道调整 (Level 1) → 轨道偏移 (Level 2) → 紧急规避 (Level 3)

### 5.5 态势透明度可视化

Canvas 叠加半透明覆盖层：opacity 与信息素成反比，随扫描/衰减实时变化。

### 5.6 燃油预警

UAV 油量降至 25% 时触发 `uav_fuel_low_warning` 事件，调度器提前准备接班 UAV，避免跟踪/搜索因燃油耗尽而中断。

---

## 六、运行

```powershell
# 完整启动 (后端 + 前端)
.\scripts\console.ps1 start

# 仅仿真 (无 Web 服务)
python main.py --steps 480 --no-server --step-delay 0

# 使用自定义 Python 环境
.\scripts\console.ps1 start -PythonPath C:\path\to\python.exe
```

仿真完成后在 `outputs/simulation_*.jsonl` 输出每帧 JSON。

---

## 七、验证

```powershell
python -m pytest -q
cd src/vis/frontend
npm run build
npm run test:acceptance
```

验收标准详见 [docs/GOAL.md](docs/GOAL.md) § 七（V1 基线）、[docs/GOAL2.md](docs/GOAL2.md) § 十（GOAL2 增量）。

---

## 八、核心模块

| 模块 | 文件 | 功能 |
|------|------|------|
| **Dubins 路径** | `src/env/dubins.py` | 六种 Dubins 路径族求解器 |
| **SAR 传感器** | `src/env/sar_sensor.py` | 侧视条带成像模型 |
| **EO/IR 传感器** | `src/env/eo_sensor.py` | 光电跟踪模型 |
| **UAV 实体** | `src/env/uav_entity.py` | 连续位姿固定翼 UAV，集成 Dubins + LGVF |
| **舰船模型** | `src/env/ship.py` | Zigzag 逃逸 + 编队 + 舰型 + AIS |
| **障碍物** | `src/env/obstacle.py` | 正方形岛屿 + 动态雷云 |
| **基地** | `src/env/base_station.py` | 多基地 + 容量约束 + refueling |
| **AIS 判别** | `src/utils/ais_discriminator.py` | AIS 信号对比 + 军民分类 |
| **覆盖规划** | `src/utils/coverage_planner.py` | Dubins 蛇形 SAR 扫描路径 |
| **避障规划** | `src/utils/obstacle_avoider.py` | RRT* + Dubins 避障 |
| **跟踪轨道** | `src/utils/track_orbit.py` | LGVF Standoff 跟踪 |
| **相位协调** | `src/utils/phase_coordinator.py` | 多 UAV 等相位分布 |
| **信息场** | `src/schedule/info_field.py` | 信息素指数衰减 + 价值场 |
| **信息价值表** | `src/schedule/info_value_table.py` | 区域级信息统计 |
| **候选提取** | `src/schedule/candidate_extractor.py` | BFS 聚类 + 矩形拟合 |
| **Prompt 构建** | `src/schedule/prompt_builder.py` | System + User Prompt 组装 |
| **LLM 客户端** | `src/schedule/llm_client.py` | LongCat API + 校验-重试 |
| **输出校验** | `src/schedule/output_validator.py` | 7-9 条规则验证 |
| **Hungarian** | `src/schedule/hungarian.py` | 最小代价二分图匹配 |
| **触发管理** | `src/schedule/trigger_manager.py` | 事件驱动 + 周期 + 去重 |
| **任务分配** | `src/schedule/task_allocator.py` | 五层决策架构编排器 |
| **状态管理** | `src/schedule/state_manager.py` | 调度状态 + 区域/标记点 |
| **仿真引擎** | `src/env/simulation.py` | 环境 + UAV + 舰船 + 调度集成 |
| **WebSocket** | `src/vis/backend` | 直播推送 + JSONL 回放 |
| **Canvas 渲染** | `src/vis/frontend` | 9+ 层 Canvas 2D 可视化 |
#   M a r i t i m e - S u r v e i l l a n c e  
 