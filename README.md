# UAV Maritime Surveillance Scheduler

基于 LLM（LongCat）的 UAV 编队海上侦察动态任务调度系统。在 300 km × 300 km 海域中，10 架固定翼 UAV 执行区域覆盖搜索（SAR）与目标跟踪监视（EO/IR），LLM 作为全局决策器动态划分搜索区域，Hungarian 算法负责 UAV 与区域的最优配对。

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

每仿真步（1 分钟）执行一次上述循环。核心调度器不每步调用 LLM——仅在**事件驱动**（目标发现/丢失/驶离、UAV 返航、雷云变化）或**周期兜底**（30 分钟）时触发重分配。轻量事件（搜索完成、加油完成）仅走 Hungarian 重新配对，不调用 LLM。

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

> **关键洞察**：$V(c,r)$ 在"刚丢失目标处"最高（高信息缺口 + 高战略价值 + 高时效性），引导 `CandidateExtractor` 优先提取该区域，LLM 优先在此划搜索区——目标丢失后的快速重搜索自动发生。

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

**System Prompt**（固定角色 + 约束）：

```text
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

**User Prompt**（每轮动态组装，由 `PromptBuilder` 生成）：

```text
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

Reviewer 每 15 分钟（仿真时间）独立于 Decision Maker 运行一次，输出 ≤ 200 字自然语言摘要，注入到下一轮 Decision Maker 的 User Prompt 中，使 LLM 具备**跨周期的任务级情境感知**。

示例输出：

> "过去2小时内，共搜索约45%海域，发现目标群3个共11艘。目标群#1、#3持续跟踪中，目标群#2于47分钟前丢失。UAV-2、UAV-7已各跟踪超90分钟需关注油量。NE象限大面积黑态未搜索，建议下轮优先分配UAV巡查。"

### 2.6 区域 ID 连续性

新区域与上一轮同 ID 区域的 IoU（Intersection over Union）满足阈值时复用旧 ID，保证 UAV 不需要因 ID 变化而重新分配：

$$\text{IoU}(A, B) = \frac{|A \cap B|}{|A \cup B|} \geq \text{stability\_iou\_threshold} \quad (\text{默认 } 0.7)$$

### 2.7 Hungarian 配对

LLM 输出"需要搜索哪些区域"，Hungarian 解决"谁去搜索哪个区域"。

代价矩阵构建：

$$C[i][j] = \text{EuclideanDist}\bigl(\text{UAV}[i]\text{.position},\ \text{Region}[j]\text{.bbox.center}\bigr)$$

- 优先使用 `scipy.optimize.linear_sum_assignment` 求全局最优解（$O(n^3)$）
- 无 scipy 时降级为贪心算法（按 cost 升序配对，$O(n^2 \log n)$）
- $|\text{UAV}| > |\text{Regions}|$：多余 UAV 回基地待命
- $|\text{Regions}| > |\text{UAV}|$：低优先级区域回候选池，下轮重新考虑

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

UAV 从当前位置飞往目标搜索区或返回基地的途中（不启用传感器），使用 **RRT\* + Dubins** 两步法避障：

1. **RRT\*** 在 2D 空间中搜索几何路径（目标偏置采样 10%）
2. **Dubins 曲线**平滑几何路径的每个拐角
3. **Bresenham 光栅化**碰撞检测确保路径安全

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

## 四、事件触发机制

### 4.1 事件分类

| 分类 | 事件类型 | 响应方式 |
|:----:|------|:----:|
| **Heavy** | `target_found`, `target_lost`, `target_departed`, `civilian_released`, `target_military`, `uav_returned`, `lifecycle_completed`, `storm_spawned`, `storm_dissipated` | LLM 全管线 |
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
| 无 AIS 信号 | **军舰** | 继续跟踪 |
| $\text{dist}(\text{AIS位置}, \text{推算位置}) > 2 \text{ cells}$ | **军舰**（虚假 AIS） | 继续跟踪 |
| $\text{dist}(\text{AIS位置}, \text{推算位置}) \leq 2 \text{ cells}$ | **民船** | 放弃跟踪，释放 UAV |

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

---

## 八、核心模块

### 环境引擎 (`src/env/`)

| 模块 | 文件 | 职责 |
|------|------|------|
| Dubins 路径 | `dubins.py` | 六种 Dubins 路径族求解器 |
| SAR 传感器 | `sar_sensor.py` | 侧视条带成像 + SNR 检测模型 |
| EO/IR 传感器 | `eo_sensor.py` | 光电跟踪 + FOV 锥计算 |
| UAV 实体 | `uav_entity.py` | 连续位姿固定翼 UAV，集成 Dubins + LGVF + 传感器 |
| 舰船模型 | `ship.py` | Zigzag 逃逸 + 编队 + ShipType（航母/驱逐舰）+ AIS |
| 障碍物 | `obstacle.py` | 正方形岛屿 + 动态雷云 + 碰撞检测 |
| 基地 | `base_station.py` | 多基地 + 容量约束 + 加油队列管理 |
| 仿真引擎 | `simulation.py` | 环境 + UAV + 舰船 + 调度全集成 |

### 工具库 (`src/utils/`)

| 模块 | 文件 | 职责 |
|------|------|------|
| 覆盖规划 | `coverage_planner.py` | Dubins 蛇形 SAR 扫描路径生成 |
| 避障规划 | `obstacle_avoider.py` | RRT\* + Dubins 避障路径规划 |
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
