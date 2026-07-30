# GOAL: UAV 海上侦察动态任务重分配 — 完整系统实现目标

> **目标模式（Goal Mode）**：Codex 需自主完成从文献调研、算法复现、代码实现到可视化交付的全流程。
> 禁止使用任何规则（rule-based）或 mock 替代 LLM 决策。所有 LLM 调用必须走真实 API（DeepSeek V4 Pro）。

---

## 一、系统总体目标

构建一个 **基于 LLM 的 UAV 编队海上侦察动态任务重分配系统**，包含：

1. **环境引擎**：300km×300km 海域，30×30 网格（cell=10km），含基地、障碍物（雷云、岛屿）、敌方舰船
2. **UAV 物理模型**：10 架彩虹/翼龙级固定翼 UAV，服从 **Dubins 曲线** 运动学约束
3. **三大任务模式**：区域覆盖搜索（SAR 雷达）、航路规划（不启用传感器，避障）、目标跟踪监视（光电传感器）
4. **LLM 调度核心**：DeepSeek V4 Pro 作为 Decision Maker，负责动态区域划分；Hungarian 算法负责 UAV↔区域配对
5. **Web 可视化**：Canvas 2D 渲染，支持直播（WebSocket）和回放（JSONL）两种模式
6. **真实 API 调用**：所有 LLM 决策必须通过 DeepSeek API，禁止规则替代

---

## 二、动态任务区域划分与 UAV 调度 — 核心决策机制

> 这是整个系统最核心的创新点。传统调度器用确定性规则分配任务，本系统用 **LLM 作为全局态势理解与策略生成的决策器**，确定性算法（连通分量、Hungarian 配对、输出校验）负责优化计算，形成 "AI 决策 + 运筹学执行" 的混合架构。

### 2.1 五层决策架构

```
仿真世界 (30×30 信息场)
  │
  ▼
┌─────────────────────────────────────────────┐
│ 第 1 层：信息场 (InfoField)                  │
│   30×30 网格，每 cell 两个数学量：           │
│   · 信息素 I(c,r)：指数衰减，半衰期 30/15min │
│   · 价值 V(c,r) = α(1-I) + β×S + γ×A       │
│   态势分类：I>0.7→白, 0.2≤I≤0.7→灰, <0.2→黑 │
│   作用：为上层提供"哪里最值得搜索"的数值依据  │
└──────────────┬──────────────────────────────┘
               │ get_value_matrix()
               ▼
┌─────────────────────────────────────────────┐
│ 第 2 层：候选区域提取 (CandidateExtractor)   │
│   确定性算法，将 900 个 cell 的连续价值场    │
│   转化为 LLM 可理解的离散候选区域列表：       │
│   · BFS 连通分量分析 (高价值 cell 聚类)      │
│   · 按总价值 ∑V 降序排序                    │
│   · Top-K 截断 (K = min(可用UAV×2, 10))     │
│   · 矩形拟合 (迭代 stack：膨胀→切分→细分)    │
│   · 碎片检测 (跟踪区挖洞残留 <12 格)          │
│   输出：候选区域列表 + 碎片提醒               │
└──────────────┬──────────────────────────────┘
               │ candidates + fragments
               ▼
┌─────────────────────────────────────────────┐
│ 第 3 层：LLM 决策器 (LLMClient)              │
│   ★ 唯一调用 LLM 的环节 ★                   │
│   · PromptBuilder 组装系统+用户 Prompt       │
│   · System Prompt：角色+7条约束+输出格式     │
│   · User Prompt：候选区+跟踪区+上轮状态+     │
│     碎片提醒+UAV状态+Reviewer长期记忆        │
│   · DeepSeek V4 Pro → 输出 JSON 区域方案     │
│   · OutputValidator：7 条规则校验            │
│   · 失败 → 错误回注 → 重试 (最多 2 次)       │
│   · LLM 只输出"哪些矩形区域"，不做UAV配对    │
│   输出：经过校验的区域划分方案                │
└──────────────┬──────────────────────────────┘
               │ search_regions[]
               ▼
┌─────────────────────────────────────────────┐
│ 第 4 层：Hungarian 配对                      │
│   确定性算法，求解二分图最小代价匹配：        │
│   · 代价矩阵 C[i][j] = EuclideanDist(        │
│       UAV[i].position, Region[j].bbox.center) │
│   · scipy.linear_sum_assignment 最优解       │
│   · 无 scipy → 贪心回退 (sorted by cost)     │
│   · |UAV|>|Regions| → 多余UAV回基地待命      │
│   · |Regions|>|UAV| → 低优先级区回候选池     │
│   输出：(uav_id, region_id) 配对列表         │
└──────────────┬──────────────────────────────┘
               │ assignments
               ▼
┌─────────────────────────────────────────────┐
│ 第 5 层：触发管理器 (TriggerManager)         │
│   决定"何时"触发重分配：                     │
│   · 事件驱动 + 分级响应 + 周期兜底           │
│   · 轻量触发 → 只走 Hungarian，不调 LLM     │
│   · 重量触发 → 走完整 5 层管线              │
│   · 5分钟内 ≥3事件 → 合批为一次重量触发     │
│   作用：避免每步都调 LLM（贵+慢）             │
└─────────────────────────────────────────────┘
```

### 2.2 第 1 层详解：信息场数学模型

信息场是驱动一切决策的底层数值基础。LLM 之所以能做出合理划分，是因为信息场已经将"哪里值得搜索"量化为可比较的数值。

**2.2.1 信息素指数衰减**

```
I(c, r, t) = I₀ · e^(-λ · Δt)

其中:
  I₀ = 1.0                         扫描完成时的初始信息素
  λ  = ln(2) / T_half               衰减常数
  T_half = 30 min (搜索) / 15 min (跟踪)
  Δt = t_current - t_last_scanned

UAV 每步扫描所在 cell → I 重置为 1.0, last_scan_time 更新为当前时间
全局每步衰减 → 所有被扫描过的 cell 按各自半衰期衰减
```

**态势三分法**（将连续信息素映射为 LLM 可理解的三档）：

| 态势 | 条件 | Δt 等价 | 含义 |
|------|------|---------|------|
| **白态势** | I > 0.7 | Δt < 15 min | 刚扫过，信息新鲜，无需重复搜索 |
| **灰态势** | 0.2 ≤ I ≤ 0.7 | 15 ≤ Δt < 70 min | 信息开始陈旧，可考虑再次搜索 |
| **黑态势** | I < 0.2 | Δt > 70 min | 长期未扫描或从未扫描，最高优先级 |

**2.2.2 信息价值（复合指标）**

每个 cell 的"搜索价值"由三个分量加权合成：

```
V(c,r) = α · (1 - I)     信息缺口：越久没扫 → V 越高
       + β · S(c,r)       战略价值场：附近有历史标记点 → V 升高
       + γ · A(c,r)       时效性：标记点越新 → V 越高

α=1.0, β=0.8, γ=0.5 (configs/grid.yaml 可调)

S(c,r) -- 战略价值场:
  标记点高斯扩散: σ=1.5 格 (15km), 多标记点取 max
  标记点年龄>60min → 线性衰减至 0

A(c,r) -- 时效性场:
  标记点高斯扩散 × exp(-age / 45min)
  标记点越新越紧急 — 目标刚丢时信息价值最高
```

**关键洞察**：V(c,r) 在"刚丢目标处"最高（高信息缺口 + 高战略价值 + 高时效性），这会引导 CandidateExtractor 将附近区域提取为高优先级候选区，进而使 LLM 优先在此划搜索区 —— 目标丢失后的快速重搜索就是这样自动发生的。

### 2.3 第 2 层详解：候选区域提取算法

`CandidateExtractor.extract()` 六步流程：

```
输入: V[c][r] (价值矩阵), I[c][r] (信息素), T[] (跟踪区), S[] (上一轮搜索区)

Step 1 - 占位掩码
  将 T[] 中所有跟踪区的 bbox 对应的 cell 标记为 "已占用"
  被占用的 cell 不参与候选提取 → 避免 LLM 在跟踪区上划搜索区

Step 2 - 连通域聚类
  对满足 V ≥ candidate_value_threshold 且未被占用的 cell
  做 BFS 四连通洪水填充 → C₁, C₂, ..., Cₙ
  每个簇记录: cells列表, total_value(∑V), avg_info(mean(I))

Step 3 - 按总价值降序
  clusters.sort(key=total_value, reverse=True)

Step 4 - Top-K 截断
  K = min(available_uavs × 2, 10)
  保留前 K 个簇（给 LLM 留选择余地，但不超出感知容量）

Step 5 - 矩形拟合 (迭代 stack，非递归)
  对每个簇:
    ┌─ 计算最小外接矩形 bbox
    ├─ 面积 < fragment_threshold_cells → 膨胀 2 格
    ├─ 长宽比 > aspect_ratio_max → 沿长轴切分为两半，重新入栈
    ├─ 面积 > search_max_cells → 细分为 n×n 子块，重新入栈
    └─ 否则 → 发射为候选矩形
  每个发射的候选矩形重新计算 own_bbox 内的 ∑V 和 mean(I)
  过滤掉与跟踪区重叠的候选矩形
  最终再次截断到 K 个

Step 6 - 碎片检测
  对 (上一轮搜索区, 当前跟踪区) 的每对重叠者:
    bbox_difference(S_prev.bbox, T_curr.bbox) → 剩余碎片 strip
    面积 < fragment_threshold_cells → 标记为 "需合并碎片"
    在 LLM Prompt 中以 "碎片提醒" 呈现

输出: CandidateResult { candidate_regions, fragment_alerts }
```

**为什么需要矩形拟合？** 连通分量得到的是不规则形状的 cell 簇，但 UAV 只能扫矩形区域（轴对齐矩形），且必须满足面积 20-40 格、长宽比 ≤2:1 的约束。矩形拟合将不规则簇转化为符合约束的候选矩形，使得 LLM 可以直接在候选矩形上做选择/调整，而非从 900 个 cell 原始数据中推理。

### 2.4 第 3 层详解：LLM 决策管线

这是"AI 决策"的核心。LLM 不做 UAV 配对（那是 Hungarian 的活），**只负责输出搜索区的矩形划分方案**。

**2.4.1 System Prompt（固定角色+约束）**

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

**2.4.2 User Prompt（每轮动态组装）— PromptBuilder**

```
【长期记忆】（来自 Reviewer，首次为空）
过去2小时内，共搜索约40%海域，发现目标群3个(11艘)。目标群#1、#3持续跟踪中...

【候选搜索区域】（按信息价值降序，来自 CandidateExtractor）
1. bbox(18,14,24,19) 面积30格 平均信息0.08(黑) 总价值42.3
2. bbox(5,22,11,28) 面积24格 平均信息0.15(黑) 总价值38.7
3. bbox(0,0,6,6) 面积36格 平均信息0.65(灰) 总价值22.1
...（最多 10 个）

【跟踪中区域】（由规则维护，不参与重新划分）
T1: bbox(12,8,16,12) UAV=UAV-3
T2: bbox(22,18,26,22) UAV=UAV-7

【上一轮搜索区状态】（来自 InfoValueTable，含老化程度）
S1: bbox(18,14,24,19) 信息0.82(白) 状态=completed UAV=UAV-1
S2: bbox(0,0,6,6) 信息0.45(灰) 状态=active UAV=UAV-2

【碎片提醒】（来自 CandidateExtractor.fragment_alerts）
- 区域S1被T1挖除后产生8格碎片，需合并

【UAV 可用状态】（来自 StateManager）
现可用UAV: 2架
  UAV-1: searching, 油量72%, 区域=S1
  UAV-3: tracking, 油量45%, 区域=T1
  UAV-5: returning, 油量8%, 区域=none
本周期可用总数: 2架

请输出本周期任务区域划分方案。
```

**2.4.3 LLM 调用与校验-重试闭环**

```
┌─ LLM API 调用 ──────────────────────────────┐
│  client = OpenAI(api_key, base_url)          │
│  response = client.chat.completions.create(  │
│    model="deepseek-v4-pro",                  │
│    temperature=0.3,                          │
│    messages=[system_prompt, user_prompt]     │
│  )                                           │
│  raw_json = response.choices[0].message      │
└──────────────────────────────────────────────┘
                    │
                    ▼
          ┌─ _parse_json() ─┐
          │ 1. 直接解析 JSON │
          │ 2. 从```json```块提取│
          │ 3. 失败 → 返回 None│
          └────────┬─────────┘
                   │ 成功
                   ▼
          ┌─ OutputValidator ───────────────┐
          │ 1. bbox 坐标范围 [0,29]         │
          │ 2. 面积: 20 ≤ w×h ≤ 40         │
          │ 3. 长宽比: max/max ≤ 2.0       │
          │ 4. 搜索区互不重叠               │
          │ 5. 不与跟踪区重叠               │
          │ 6. 总数: search+track ≤ 10     │
          │ 7. 稳定性: 与上轮同ID IoU≥0.7  │
          └────────┬────────────────────────┘
                   │
         ┌──── is_valid? ────┐
         │ 是                │ 否 (attempt ≤ max_retries)
         ▼                   ▼
    提交方案     错误信息追加到 user_prompt
                 重新调用 LLM (最多 2 次)
                     │
                     ▼
              仍失败 → 返回空方案 (空 search_regions[])
```

**2.4.4 LLM 输出示例**

```json
{
  "search_regions": [
    {
      "id": "S1",
      "bbox": [18, 14, 24, 19],
      "priority": "high",
      "reason": "含标记点MK3，信息价值0.82，黑态势，覆盖30格"
    },
    {
      "id": "S2",
      "bbox": [5, 22, 11, 28],
      "priority": "high",
      "reason": "NE象限大面积黑态势未搜索，24格"
    },
    {
      "id": "S3",
      "bbox": [0, 0, 6, 6],
      "priority": "medium",
      "reason": "合并S1被T1挖除的碎片，灰态势36格"
    }
  ],
  "notes": "共覆盖约90格。S2为新划分NE象限区域。S3合并S1碎片避免浪费。需3架UAV。"
}
```

### 2.5 第 4 层详解：Hungarian 配对

LLM 输出的是"需要搜索哪些区域"，Hungarian 解决的是"谁去搜索哪个区域"。

**2.5.1 代价矩阵构建**

```
C[i][j] = EuclideanDistance(
  UAV[i].current_position,      -- UAV 当前位置 (GridCoord)
  Region[j].bbox.center()       -- 区域中心点 (cx=(c0+c1)/2, ry=(r0+r1)/2)
)

矩阵维数: max(n_uavs, n_regions) × max(n_uavs, n_regions)
填充项: 多余行/列用 ∞ 填充
```

**2.5.2 最优求解**

```
优先: scipy.optimize.linear_sum_assignment(C) → O(n³) 全局最优
降级: 贪心算法 (无 scipy 环境)
  1. 生成所有 (cost, uav_idx, region_idx) 三元组
  2. 按 cost 升序排列
  3. 遍历: 如 UAV 未分配且 Region 未被占用 → 配对
  4. 产生局部最优解（非全局最优，但 O(n² log n)）
```

**2.5.3 边界处理**

| 情况 | 处理方式 |
|------|---------|
| \|UAV\| > \|Regions\| | 多余 UAV → 状态 idle，回基地待命 |
| \|Regions\| > \|UAV\| | 低优先级区域 → 回候选池，下轮重新考虑 |
| UAV 在 transit 中 | 已分配区域的 UAV 不参与本轮 Hungarian |

**2.5.4 地区域 ID 连续性**

```
新区域与上一轮区域 IoU ≥ stability_iou_threshold (默认 0.7)
  → 复用上一轮的区域 ID
  → 稳定：UAV 不会因 ID 变化而重新分配

IoU = intersection_area / union_area
  例: S1_prev(bbox=[18,14,24,19]) 与 S1_curr(bbox=[18,15,24,19])
      IoU = (6×4) / (6×5+6×4-6×4) = 24/30 = 0.8 ≥ 0.7 → 保持 ID "S1"
```

这样即使 LLM 每次重新划分，只要变化不大，UAV 就继续在原区域工作，避免频繁切换。

### 2.6 第 5 层详解：触发机制

此层决定了"何时调 LLM"。LLM 调用昂贵且慢（约 2-5 秒），不能每步都调。

**2.6.1 事件分类**

| 事件 | 来源 | 类型 | 含义 |
|------|------|:--:|------|
| `uav_returned` | UAV 油尽，自动返航 | **重量** | 一架 UAV 离岗，需要重新分配 |
| `target_found` | UAV 同 cell 发现船舶 | **重量** | 新目标出现，创建跟踪区，重划搜索区 |
| `target_lost` | 跟踪 UAV 油尽/目标脱离 | **重量** | 跟踪区释放，创建标记点 |
| `search_complete` | UAV 完成搜索区覆盖 | 轻量 | 仅需将完成 UAV 重新配对 |
| `uav_refueled` | UAV 加油完毕 | 轻量 | 仅需将新空闲 UAV 分配任务 |

**2.6.2 判定逻辑 (TriggerManager.check)**

```
1. 收集 5 分钟内的 pending events
2. 分类: heavy_events (uav_returned/target_found/target_lost)
         light_events (search_complete/uav_refueled)
3. 判定:
   if heavy_count > 0 or total_count ≥ 3:
     → HEAVY TRIGGER (事件驱动)
   elif light_count > 0:
     → LIGHT TRIGGER (无需 LLM)
   else:
     → 检查周期定时

4. 周期兜底:
   if current_time - last_heavy_time ≥ heavy_cycle_min (30 min):
     → HEAVY TRIGGER (周期驱动)
```

**2.6.3 轻量 vs 重量流程对比**

| | 轻量触发 | 重量触发 |
|---|---------|---------|
| 触发条件 | UAV 完成搜索/加油完成 | 发现/丢失目标、UAV 返航、30min 周期、多事件合批 |
| CandidateExtractor | ✅ 提取候选 | ✅ 提取候选 |
| LLM 调用 | ❌ **不调用** | ✅ 完整管线 |
| Hungarian 配对 | ✅ 仅对 idle UAV 配对 | ✅ 全局配对 |
| 典型延迟 | < 1ms | 2-5 秒 |
| 频率（典型 8h 仿真） | ~20-40 次 | ~16 次周期 + ~5-15 次事件驱动 |

### 2.7 跟踪区管理（规则驱动，不经过 LLM）

跟踪区是系统中 **唯一不经过 LLM 的区域类型**，由确定性规则管理。

**生命周期**：

```
发现目标 (UAV.position == Ship.position)
  │
  ├→ 为每个目标群创建 1 个跟踪区
  │   bbox = 目标中心 ± 2 格 → 4×4 = 16 格 (40×40 km)
  │   从重叠搜索区中挖除跟踪区占用的 cell
  │   重叠搜索区 < 12 格 → 标记为碎片（下轮 HEAVY TRIGGER 时 LLM 合并）
  │
  ├→ 跟踪中
  │   目标 zigzag 逃逸 → bbox 中心跟随平移 (update_track_region_center)
  │   跟踪 UAV 携带扫描周边 cell → 跟踪扫描衰减 (half_life=15min, 比搜索快)
  │
  ├→ UAV 油尽返航
  │   上报目标最后位置 → 创建 MarkPoint
  │   标记点: id, position, created_time, source_uav_id
  │   标记点生成高斯场 → 提升周边 cell 价值 → 下次 CandidateExtractor 优先提取
  │   原跟踪区释放 (release_track_region)
  │
  └→ 目标完全丢失（无 UAV 可接手）
      跟踪区释放 → 原 bbox 位置 cell 价值高 (1-I ≈ 1)
      → CandidateExtractor 将其纳入候选搜索区
      → 下次 HEAVY TRIGGER → LLM 重划搜索区覆盖此处
```

### 2.8 Reviewer 长期记忆

```
触发周期: 每 15 分钟（仿真时间），独立于 Decision Maker
输入: 仿真开始以来的事件流摘要
输出: ≤200 字自然语言（每次覆盖上一轮，非累积）

示例输出:
"过去2小时内，共搜索约45%海域，发现目标群3个共11艘。
目标群#1、#3持续跟踪中，目标群#2于47分钟前丢失。
UAV-2、UAV-7已各跟踪超90分钟需关注油量。
NE象限大面积黑态未搜索，建议下轮优先分配UAV巡查。"
```

Reviewer 的记忆注入到下一次 Decision Maker 的 User Prompt 中，使 LLM 具备 **跨周期的任务级情境感知**。

### 2.9 区域生命周期全景

```
  CandidateExtractor               LLM                    Hungarian
  提取候选区域                 输出区域方案               UAV↔区域配对
       │                          │                         │
       ▼                          ▼                         ▼
  ┌─────────┐   LLM决策    ┌──────────┐   Hungarian  ┌──────────┐
  │ candidate │ ──────────→ │  search   │ ──────────→ │  UAV 已   │
  │  region  │             │  region   │   配对完成   │  分配    │
  └─────────┘             └──────────┘             └──────────┘
                                                         │
                              ┌──────────────────────────┤
                              │                          │
                         UAV 搜索                     发现目标
                         区域完成                         │
                              │                    ┌──────┴──────┐
                              ▼                    │  track region │
                         ┌──────────┐              │  (规则创建)    │
                         │ completed │              └──────┬──────┘
                         │  触发轻量 │                     │
                         │  Hungarian│              UAV跟踪/丢失目标
                         └──────────┘                     │
                              │                    ┌──────┴──────┐
                         UAV 重新分配              │  mark point  │
                         到新区域                  │  (标记点)    │
                                                   └──────┬──────┘
                                                          │
                                                    高斯场提升周边价值
                                                          │
                                                    下次 CandidateExtractor
                                                    优先提取为候选区域 ──→ 循环
```

### 2.10 可视化中的决策机制展示

**搜索区 (Layer 3)**：
- 矩形框颜色 = 优先级（红=高/黄=中/蓝=低）
- 框内显示：`S1 67%`（区域 ID + 完成百分比）
- 半透明填充 = 优先级色 10% opacity
- SAR 侧视方向箭头

**跟踪区 (Layer 4)**：
- 红色虚线框（区别于搜索区实线）
- 不显示完成百分比（跟踪区不参与 LLM 划分）

**配对动画 (Layer 8)**：
- UAV 被分配到新区域时，从 UAV 当前位置到区域中心画一条淡蓝色虚线
- 虚线以 3 秒渐隐动画展示

**标记点 (Layer 5)**：
- 脉动圆点，颜色随年龄变化（橙→黄→棕）
- 浮窗显示 `MK3 | 42min ago | by UAV-2`

**LLM 日志 (BottomDrawer Tab 3)**：
- 最近一次 LLM 交互的完整记录
- System Prompt / User Prompt / LLM Response / Validation Result
- 可折叠显示，支持复制

**事件时间线 (BottomDrawer Tab 1)**：
- 每个 Heavy Trigger 标注为 ⚡ 紫色闪电图标
- 每个 Light Trigger 标注为 🔄 灰色刷新图标
- 每个目标发现标注为 🎯 红色靶心

---

## 三、三大任务模式 — 算法细节与期望效果

> 每架 UAV 根据当前分配的 mission 处于三种任务模式之一：**区域覆盖搜索**（启用 SAR 雷达）、**航路规划**（不启用传感器，仅飞行）、**目标跟踪监视**（启用光电传感器）。所有模式下的航路都必须满足 **Dubins 曲线** 运动学约束。

### 3.0 基础：固定翼 UAV Dubins 曲线运动学

所有路径规划的共同约束条件。固定翼 UAV 不同于多旋翼——不能悬停、不能原地转弯、所有转弯弧线有最小半径约束。

**3.0.1 数学模型**

```
状态: (x, y, χ) — 2D 位置 + 航向角 (heading)
控制: u ∈ {L, S, R} — 左转 / 直飞 / 右转

转弯半径约束:
  R_min = v² / (g × tan(φ_max))
  其中:
    v = 160 km/h = 44.4 m/s (巡航速度)
    φ_max = 30° (最大滚转角，彩虹固定翼典型值)
    g = 9.81 m/s²
  → R_min ≈ 348m → 取整为 0.035 格 (≈350m)
  工程简化: R_min = 1 格 (10km)，代表战术转弯半径

运动方程 (连续时间):
  ẋ = v × cos(χ)
  ẏ = v × sin(χ)
  χ̇ = v / R × u    (u = -1 左转, 0 直飞, +1 右转)
  |χ̇| ≤ v / R_min  (航向角速率约束)
```

**3.0.2 Dubins 路径类型**

```
LSL: 左弧(L) + 直线(S) + 左弧(L)
LSR: 左弧(L) + 直线(S) + 右弧(R)
RSL: 右弧(R) + 直线(S) + 左弧(L)
RSR: 右弧(R) + 直线(S) + 右弧(R)
LRL: 左弧(L) + 右弧(R) + 左弧(L)   (CCC 类)
RLR: 右弧(R) + 左弧(L) + 右弧(R)   (CCC 类)

给定起点 pose (x₀, y₀, χ₀) 和终点 pose (x₁, y₁, χ₁)，R_min:
  遍历 6 种类型 → 计算每条路径的弧长 + 直线长
  → 选择总长度最短者 → 按固定时间步长 dt 离散化为航路点序列
```

**3.0.3 期望效果**

| 场景 | 输入 | 输出 |
|------|------|------|
| U 型掉头 | start:(5,0,0°), end:(5,10,180°) | LSL 路径：左弧270°→直线→左弧90°，约 3-4 个航路点 |
| 90° 转弯 | start:(0,0,0°), end:(10,10,90°) | RSR 路径：右弧90°→直线→右弧0°，约 2-3 个航路点 |
| 蛇形掉头 (搜索区边界) | start:(0,0,90°), end:(5,0,270°) | LSL 路径：平滑 U 型转弯 |

**3.0.4 交付物**

- `src/env/dubins.py`：`class DubinsPath`，包含：
  - `compute(start_pose, end_pose, R_min) → (path_type, total_length, waypoints[])`
  - `waypoints` 为 `list[(x, y, χ)]`，步长为 dt=1min 的飞行距离
  - 单元测试：10 组已知位姿对，验证最短路径长度

---

### 3.1 区域覆盖搜索 — SAR 雷达蛇形扫描

UAV 被分配到搜索区后，需在该矩形区域上执行系统性的覆盖扫描，同时操作 SAR 雷达获取对海图像。这是本系统 **最复杂的航路规划问题**——必须同时满足飞行运动学和雷达成像两个领域的约束。

**3.1.1 SAR 侧视条带成像原理**

```
SAR 雷达安装在 UAV 侧面（非正下方），以固定俯角照射海面。

侧视几何 (side-looking stripmap):
         UAV 飞行方向 →
         ─────────────────────          ← 飞行轨迹 (nadir line)
              /│
             / │ 俯角 (depression angle) θ_d
            /  │
           /   │
  ┌──────/────┼──────┐  ← near range (离轨迹最近可成像点)
  │    /      │      │
  │   /  成像条带    │  ← swath width S_w
  │  /    (swath)   │
  │ /              │
  └────────────────┘  ← far range (离轨迹最远可成像点)

  条带不包含飞行轨迹正下方！这是一个最常见的误解。
  S_w = z × (tan(θ₂) - tan(θ₁))
  其中 θ₁ = θ_d - Θ_{3dB}/2, θ₂ = θ_d + Θ_{3dB}/2
  z = 飞行高度 (典型 5000m AGL)
  Θ_{3dB} = 天线 3dB 波束宽度
```

**3.1.2 覆盖扫描的数学约束**

```
SAR 成像运动补偿约束（每个扫描行上）:
  ┌─────────────────────────────────────────┐
  │ 1. 高度恒定:  z(n+1) = z(n)            │  保持固定 swath width
  │ 2. 速度恒定:  v(n+1) = v(n)            │  SAR 方位向成像要求
  │ 3. 加速度零:  a(n+1) = a(n) = 0        │  频率域处理算法前提
  │ 4. 直线飞行:  χ(n+1) = χ(n)            │  条带不能弯曲
  │ 5. 条带不跨 nadir: 整个 swath 在飞行   │
  │    轨迹的同一侧（左视或右视）           │
  └─────────────────────────────────────────┘

相邻条带无缝拼接约束:
  x(n) = x(n-1) + z(n-1)·tan(θ₂) - z(n)·tan(θ₁)
  
  含义: 第 n 条带的 near-range 边界 = 第 n-1 条带的 far-range 边界
        保证相邻两次扫描之间没有遗漏的间隙
```

**3.1.3 蛇形扫描路径生成算法**

```
输入:
  bbox:      搜索区矩形 [c0, r0, c1, r1]
  start_pose: UAV 当前位置+航向 (x, y, χ)
  direction:  飞行方向偏好 ("horizontal" | "vertical")
  swath_width: SAR 条带宽度 (grid cells)
  R_min:      最小转弯半径

输出:
  waypoints[]: 完整搜索航路点序列
  swaths[]:   每条扫描行的起止坐标 (用于可视化渲染 footprint)

算法流程:
  1. 确定扫描方向:
     - 如果 bbox 宽度 > 高度: 沿水平方向扫描 (蛇形上下摆动)
     - 否则: 沿垂直方向扫描 (蛇形左右摆动)
     - 偏好: 沿 bbox 长轴扫描可减少掉头次数

  2. 计算扫描行数量:
     N_sweeps = ceil(bbox_height / swath_width)
     每条扫描行 = 沿扫描方向的完整遍历

  3. 生成扫描行起止点:
     for i in range(N_sweeps):
       row = bbox.row_start + i × swath_width + swath_width/2
       if i % 2 == 0:
         swaths[i] = ((bbox.col_start, row) → (bbox.col_end, row))  // 右向
       else:
         swaths[i] = ((bbox.col_end, row) → (bbox.col_start, row))  // 左向

  4. 用 Dubins 连接扫描行:
     ├─ 第一条扫描行: 从 start_pose 到 swaths[0].start
     │   → Dubins(start_pose, pose_of(swaths[0].start))
     │
     ├─ 相邻扫描行之间的掉头 (U-turn):
     │   对于从 swaths[i].end 到 swaths[i+1].start 的过渡:
     │     如果方向相同但行不同: U 型掉头
     │       → Dubins(end_pose_of_i, start_pose_of_i+1)
     │     自动选择最短 Dubins 路径
     │
     └─ 每条扫描行本身: 直线飞行（无 Dubins 弧线）
        扫描行上的航路点 = 等距采样 (dt × v 步长)

  5. SAR 方向切换:
     - 奇数行: 左视 (swath 在飞行轨迹左侧)
     - 偶数行: 右视 (swath 在飞行轨迹右侧)
     - 掉头时自动切换方向（因为飞行方向反转）
     - 也可始终一侧（side-looking consistency），取决于实际 SAR 安装

  6. 离散化:
     整个路径以 dt=1min 步长离散化为航路点序列
     扫描行上: 直线航路点（等距）
     掉头段: Dubins 弧线上的航路点（沿弧等距采样）
```

**3.1.4 SAR 传感器检测模型**

```
对每个被扫描的 cell，基于 SNR 计算检测概率:

SNR(cell) = (P_sar × G_t × G_r × λ³ × σ₀ × c × τ_p × PRF × sin²(θ_d))
            / ((4π)⁴ × z³ × k × T₀ × NF × B_r × L_tot × v)

符号说明:
  P_sar = 雷达发射功率 (典型 1 kW)
  G_t, G_r = 发射/接收天线增益
  λ = 波长 (X-band ≈ 0.03 m)
  σ₀ = 海面后向散射系数 (取决于海况)
  z = 飞行高度 (SNR 随 z³ 恶化)
  v = 飞行速度 (SNR 随 1/v 改善)

检测概率:
  P_det(cell) = 1 / (1 + exp(-k × (SNR - SNR_threshold)))
  简化为: 在被扫描 cell 上的 P_det ≈ 0.9 (SAR 高信噪比条件下)
  未被扫描的 cell: P_det = 0

扫描效果:
  UAV 在 cell (c,r) 上方飞行且 SAR 指向该 cell
  → cell 被标记为 "已扫描"
  → info[c,r] = 1.0, last_scan_time = current_time
```

**3.1.5 期望效果**

| 场景 | 输入 | 预期行为 |
|------|------|---------|
| 6×5 bbox 搜索 | bbox=[0,0,6,5], swath_width=2 | 3 条扫描行，2 次 Dubins 掉头，总路径 ≈ 直线30格+弧线 |
| 方形区域 | bbox=[0,0,6,6], swath_width=2 | 3 条扫描行，蛇形遍历；转弯只发生在 bbox 外部 |
| 长条形区域 | bbox=[0,0,10,3], swath_width=2 | 2 条扫描行，沿长轴扫描，1 次长弧形掉头 |
| SAR 左视 | swath 始终在左侧 | 所有扫描行同侧成像，交替行需在 bbox 外做更大的掉头 |

**可视化中的 SAR footprint**:
- 每个被 UAV 当前扫描的 cell 上叠加半透明蓝色矩形（SAR swath footprint）
- 已扫描过的 cell 在热力图上显示为绿色/白色（高信息素）
- 搜索区的矩形框上显示小箭头指示当前 SAR 侧视方向（←左视 / 右视→）
- 搜索区框内文字: `S1 67%` (区域ID + 完成百分比)

**3.1.6 交付物**

- `src/utils/coverage_planner.py`：`class CoveragePlanner`
  - `plan(bbox, start_pose, swath_width, R_min) → CoveragePath`
  - `CoveragePath`: swaths[] + waypoints[] + total_length
- `src/env/sar_sensor.py`：`class SARSensor`
  - `compute_swath_footprint(uav_position, heading, look_direction) → list[GridCoord]`
  - `is_cell_in_swath(cell, uav_pose) → bool`
  - `compute_snr(altitude, speed) → float`
- 单元测试：
  - 验证 6×5 bbox 的蛇形覆盖：所有 bbox 内 cell 至少被一条扫描行覆盖
  - 验证相邻条带无缝拼接（行间无间隙 cell）
  - 验证每条扫描行的航路点满足直线约束

---

### 3.2 航路规划 — 避障飞行（不启用传感器）

UAV 从当前位置飞往目标搜索区的途中（或从搜索区返回基地），**不启用任何传感器**，仅以最快/最安全的方式到达目的地。途中可能遇到雷云和岛屿等障碍物，需要生成绕行路径。

**3.2.1 障碍物模型**

```
Thunderstorm (雷云 — 动态障碍物):
  center:     GridCoord 或 (col, row)  中心位置
  radius:     float                    影响半径 (grid cells, 3-8)
  move_vector:(dc, dr)                 每步移动量 (可选, 默认静止)
  lifetime:   float                    剩余存在时间 (分钟, -1=永久)
  
  危险等级:
    雷云内部:  不可飞越 (P_fly = 0)
    雷云边缘 (< radius + 2): 高风险 (P_fly = 0.3, UAV 颤振)
    雷云外部 (≥ radius + 2): 安全

Island (岛屿 — 静态障碍物):
  vertices:   list[GridCoord]          多边形顶点 (顺时针或逆时针)
  内部:       不可飞越 (地形障碍)
  边缘 (< 1 格): 不可飞越 (安全余量)
  
  生成方式:
    随机生成不规则多边形:
      1. 取中心点 (cx, cy)
      2. 生成 5-10 个顶点，ri = R_base + random(-Δ, Δ), θi = 2π×i/n + random(-ε, ε)
      3. 顶点坐标 = (cx + ri×cos(θi), cy + ri×sin(θi))
      4. clamp 到 [0, 29]
    岛屿面积: 3-15 格 (30-150 km² 实际面积)

占用栅格生成:
  obstacle_grid_mask() → 30×30 bool numpy array
  mask[c,r] = True 该 cell 不可通行
```

**3.2.2 避障路径规划算法**

使用 **RRT\* + Dubins** 两步法：

```
Step 1: RRT* 在 2D 空间中搜索几何路径

  配置空间: 30×30 grid, 含障碍物占用掩码
  采样策略: 目标偏置 (10%的概率直接采样目标点)
  代价函数: path_length（欧几里得距离）
  近邻半径: γ_RRT* = min(γ₀×√(log(n)/n), 5 cells)
  
  算法:
    1. tree ← {start_point}
    2. for i in range(MAX_ITERATIONS = 500):
    3.   x_rand ← sample(goal_bias=0.1)
    4.   x_nearest ← nearest(tree, x_rand)
    5.   x_new ← steer(x_nearest, x_rand, step=2 cells)
    6.   if not collision(x_nearest → x_new, obstacle_mask):
    7.     x_near ← near(tree, x_new, γ_RRT*)
    8.     x_parent ← min_cost_parent(x_new, x_near)
    9.     tree.add(x_new, parent=x_parent)
   10.     rewire(tree, x_new, x_near)  ← RRT* 特有：优化已有节点的父节点
   11. 提取最优路径: 从 goal 回溯到 start
   
  输出: geometric_path = [(x₀,y₀), (x₁,y₁), ..., (xₙ,yₙ)]

Step 2: 用 Dubins 曲线平滑几何路径

  对 geometric_path 的每对相邻点 (pᵢ, pᵢ₊₁):
    计算从 pᵢ 到 pᵢ₊₁ 的方向向量 → 得到期望航向 χ_desired
    调用 Dubins(pose(pᵢ, χ_current), pose(pᵢ₊₁, χ_desired), R_min)
    将 Dubins 弧线上的航路点追加到最终路径
    
  检查: Dubins 弧线不应穿越障碍物
    如果穿越 → 在该点插入中间航路点 (偏离障碍物方向 + 2 cells) → 重新 Dubins

输出: smooth_path = [(x₀,y₀,χ₀), (x₁,y₁,χ₁), ..., (xₘ,yₘ,χₘ)]
```

**3.2.3 碰撞检测**

```
collision(segment, obstacle_mask) → bool:
  检查从点A到点B的直线段是否穿越任何被障碍物占用的cell
  
  实现: Bresenham 直线光栅化
    for each cell (c,r) on Bresenham line from A to B:
      if obstacle_mask[c, r]:
        return True  ← 碰撞！
    return False

  对于 Dubins 弧线段:
    将弧线以 1° 步长采样为点序列
    检查每个采样点所在的 cell 是否被占用
```

**3.2.4 航路规划触发时机**

| 场景 | 说明 |
|------|------|
| **transit → search** | UAV 分配了新搜索区，从当前位置飞往搜索区起点。需避障。 |
| **search → return** | UAV 油量不足，从当前搜索位置飞回基地。需避障。 |
| **tracking → return** | 跟踪 UAV 油量不足，从盘旋轨道脱离并飞回基地。需避障。 |
| **idle → 待命** | UAV 在基地待命，无需规划。 |

**3.2.5 期望效果**

| 场景 | 预期行为 |
|------|---------|
| 基地到搜索区间无障碍 | 直线飞行（Dubins 最短路径 = 直线，弧长为 0） |
| 基地到搜索区间有雷云 | 路径自动绕行雷云边缘（至少保持 2 格安全距离） |
| 路径上同时有雷云+岛屿 | 路径从两个障碍物之间的缝隙穿过（如缝隙 < 2格则绕更大的弯） |
| 雷云随时间移动 | 每个 dt 更新雷云位置，若新位置与已有航路冲突 → 重新规划避障路径 |
| 多 UAV 同时 transit | 各 UAV 独立规划，不考虑互相碰撞（后续可升级为多机协同避障） |

**可视化中的障碍物**:
- **雷云**: 暗红半透明圆 (#EF4444, opacity 30%) + 边缘脉动动画 + ⚡闪电图标
- **岛屿**: 棕色不规则多边形 (#92400E) + 边缘白色海岸线
- 避障路径: 用淡蓝色虚线连接 UAV 与目的地，显示规划的避障路线

**3.2.6 交付物**

- `src/env/obstacle.py`：`class Thunderstorm`, `class Island`, `obstacle_grid_mask()`
- `src/utils/obstacle_avoider.py`：`class ObstacleAvoider`
  - `plan_path(start_pose, goal_pose, obstacle_mask, R_min) → list[(x,y,χ)]`
  - `is_path_safe(waypoints, obstacle_mask) → bool`
- 单元测试：
  - 单雷云场景：验证路径中所有 cell 距离雷云中心 ≥ radius + 2
  - 雷云+岛屿组合：验证路径不穿越任何障碍物 cell
  - 无障碍场景：验证路径 = 最短 Dubins 路径（不引入多余绕行）

---

### 3.3 目标跟踪监视 — 光电传感器 Standoff 盘旋

UAV 发现舰船目标后，切换为跟踪模式，**开启光电传感器（EO/IR）**，绕目标做持续盘旋监视。与 SAR 搜索不同——SAR 需要严格的直线飞行约束，而 EO 跟踪需要 UAV 维持在目标周围的圆形轨道上。

**3.3.1 光电传感器模型**

```
EO/IR 传感器 (Electro-Optical / Infrared):
  工作模式:  凝视 (staring) — 持续指向目标
  视场角:    FOV ≈ 3°-10° (窄视场，高分辨率识别)
  作用距离:  最大 15-20 km (取决于天气/能见度)
  安装方式:  机腹转塔 (可 360° 旋转)
  
  EO 传感器无需特殊的飞行轨迹约束（不同于 SAR 的直线约束）
  只需 UAV 保持在目标周围的有效观测半径内即可

检测模型:
  当 UAV 在跟踪盘旋轨道上时:
    EO 始终指向轨道中心（目标所在区域）
    P_det_continuous ≈ 0.95 (持续跟踪条件下，检测概率很高)
    
  从搜索到跟踪的切换:
    UAV 首次与目标处于同一 cell → 目标被发现
    → UAV 切换到 tracking 模式
    → 启动 EO 传感器
    → 开始 Standoff 盘旋
```

**3.3.2 Lyapunov Guidance Vector Field (LGVF) 算法**

基于 Liu et al. (2023) 的 LGVF 方法，这是目前固定翼 Standoff 跟踪的 SOTA。

```
问题描述:
  固定翼 UAV 需绕移动目标做半径为 R_d 的圆形盘旋
  UAV 状态: (x, y, χ) — 位置 + 航向
  目标状态: (x_t, y_t) — 位置（可通过 EO 传感器观测）
  
  目标轨道: 以目标为中心的圆, 半径 = standoff_radius (R_d)

LGVF 方法核心思想:
  不直接计算航路点，而是计算一个"引导向量场"
  该场的流线收敛到目标圆形轨道
  UAV 只需跟随该场的梯度方向即可自动收敛到轨道上

Step 1 — 相对坐标变换:
  x_rel = x - x_t    (UAV 相对于目标的位置)
  y_rel = y - y_t
  r = sqrt(x_rel² + y_rel²)   (UAV 到目标中心的距离)

Step 2 — 定义 Lyapunov 函数:
  V = (r² - R_d²)² / 2
  当 UAV 在轨道上时 r = R_d, V = 0
  V > 0 当 r ≠ R_d

Step 3 — 期望航向角:
  χ_d = arctan2(-(r² - R_d²)×x_rel - 2×R_d×r×y_rel,
                 -(r² - R_d²)×y_rel + 2×R_d×r×x_rel)

Step 4 — 航向角速率控制:
  χ̇_cmd = k × (χ_d - χ)    比例控制器，使 UAV 航向收敛到期望方向
  
  输入约束满足:
    确保 |χ̇_cmd| ≤ v / R_min  (不超过最大转弯速率)
    如果超过 → clamp 到 ±v/R_min

Step 5 — 离散化:
  以 dt 为步长:
    χ(t+dt) = χ(t) + χ̇_cmd × dt      航向更新
    x(t+dt) = x(t) + v × cos(χ) × dt  位置更新
    y(t+dt) = y(t) + v × sin(χ) × dt

收敛性质:
  · 初始位置在轨道外 → UAV 以平滑曲线趋近轨道（无超调）
  · 初始位置在轨道内 → UAV 以螺旋线扩展到轨道
  · 初始位置恰好在轨道上 → UAV 保持在轨道上
  · 目标静止 → 最终轨迹为精确的圆形
  · 目标移动 → 轨道中心随目标平移，UAV 自动跟随
```

**3.3.3 目标运动下的 Standoff 跟踪**

```
当目标以速度 v_target 移动时:

  轨道中心 = 目标实时位置 (x_t(t), y_t(t))

  如果 v_target << v_uav (绝大多数情况):
    LGVF 引导场自动将 UAV 拉向移动后的轨道中心
    因为 Lyapunov 函数是基于"当前 UAV-目标相对位置"定义的
    
    实际效果:
      - 目标不动 → UAV 飞精确的圆
      - 目标匀速直行 → UAV 飞近似摆线（cycloid-like），轨道中心跟随平移
      - 目标 zigzag → UAV 轨道有平滑的左右摆动，但始终保持围绕目标

  工程处理:
    每个 dt 步:
      1. 获取当前目标位置 (从仿真环境)
      2. 重新计算 x_rel, y_rel, r
      3. 重新计算 χ_d 和 χ̇_cmd
      4. 更新 UAV 状态
```

**3.3.4 多 UAV 协同 Standoff 跟踪 — 时序相位分离**

当多架 UAV 同时跟踪同一目标群时，需要它们在圆形轨道上均匀分布。

```
Phase Coordinator 算法:

  输入:
    N 架 UAV: 每架有当前轨道相位角 φᵢ (φᵢ = arctan2(y_relᵢ, x_relᵢ))
    目标相位: φᵢ_desired = 2π × i / N  (等间距)

  方法: 通过微调各 UAV 的空速来实现相位收敛
  
  对每架 UAV i:
    phase_error = φᵢ_desired - φᵢ (归一化到 [-π, π])
    v_cmd = v_nominal × (1 + k_phase × phase_error / π)
    v_cmd = clamp(v_cmd, v_min=0.8×v_nominal, v_max=1.2×v_nominal)
    
    相位落后的 UAV → 稍加速
    相位超前的 UAV → 稍减速

  效果:
    两架 UAV → 相位差 ≈ 180° (相向)
    三架 UAV → 相位差 ≈ 120° (等边三角形分布)
    四架 UAV → 相位差 ≈ 90° (正方形分布)
    
  收敛时间: 约 5-15 分钟 (取决于初始相位差和速度调整范围)
```

**3.3.5 从搜索到跟踪的平滑过渡**

```
当 UAV 在搜索飞行中首次检测到目标时:

  当前状态: 直线飞行 (蛇形扫描行上)
    → 不能立即切换到圆形轨道 (违反 Dubins 约束)
    → 需要一段"接入路径"

  接入路径算法:
    1. 计算从 UAV 当前 pose 到轨道上最近切入点的 Dubins 路径
       切点 = (x_t + R_d×cos(φ_entry), y_t + R_d×sin(φ_entry), χ_entry)
       选择使 Dubins 路径最短的 φ_entry
    2. Dubins 弧线 + 直飞 → 到达切入点
    3. 从切入点开始 → 切换到 LGVF 引导模式 (沿圆形轨道继续)

  整个过渡过程中 UAV 满足:
    · 航向连续变化 (无跳变)
    · |χ̇| ≤ v / R_min (转弯不超限)
    · 平滑地从直线过渡到圆形
```

**3.3.6 期望效果**

| 场景 | 预期行为 |
|------|---------|
| 搜索中发现静止目标 | UAV 从直线 Dubins 接入圆形轨道，约 2-3 分钟后稳定在精确圆上 |
| 搜索中发现移动目标 | UAV 完成接入后，LGVF 自动跟随目标平移，轨道平滑漂移 |
| 目标 zigzag 逃逸 | UAV 轨道有左右摆动，但始终保持 standoff distance R_d ± 20% |
| 两 UAV 协同跟踪 | 两机在圆上 180° 相对，交替覆盖目标的前后方向 |
| UAV 油尽脱离跟踪 | 从当前轨道位置 Dubins 过渡到返航路径，同时创建目标标记点 |
| 跟踪 UAV 交接 | 新 UAV 接入的同时旧 UAV 脱离，交接期间轨道上至少有 1 架 UAV |

**可视化中的 Standoff 跟踪**:
- **EO 视场**: 从 UAV 指向目标的一个小扇形区域（黄色半透明），表示光电传感器 FOV
- **盘旋轨道**: 以目标为中心的一个淡色细圆环（不填充），虚线或细实线
- **UAV 跟踪状态**: 红色三角 + 红色油量环（区别于搜索的绿色）
- **相位分布**: 多 UAV 在圆上的位置均匀分布

**3.3.7 交付物**

- `src/utils/track_orbit.py`：重写为 `class LGVFTracker`
  - `compute_guidance(uav_pose, target_position, R_d, v_nominal) → (χ̇_cmd, v_cmd)`
  - `compute_waypoints(uav_pose, target_position, R_d, dt, n_steps) → list[(x,y,χ)]`
  - 内部验证: 确保 |χ̇_cmd| ≤ v/R_min
- `src/utils/phase_coordinator.py`：`class PhaseCoordinator`
  - `compute_phase_offsets(uavs_on_orbit[]) → list[phase_error]`
  - `adjust_airspeeds(phase_errors[], v_nominal) → list[v_cmd]`
- `src/env/eo_sensor.py`：`class EOSensor`
  - `compute_fov(uav_position, heading, target_position) → FOV_cone`
  - `is_target_visible(uav_position, target_position, max_range) → bool`
- 单元测试：
  - 静止目标: 验证 UAV 轨迹在 10 分钟内收敛到 |r - R_d| < 0.5 格的圆
  - 移动目标 (匀速): 验证 UAV 保持 |r - R_d| < 1 格
  - 两 UAV 协同: 验证 10 分钟内相位差收敛到 180° ± 15°

---

## 四、可视化增强

### 4.1 渲染层扩展

当前 `src/vis/frontend/src/renderer/layers.js` 已有 9 层渲染，需新增/修改：

| 层 | 内容 | 要求 |
|---|------|------|
| Layer 0 | 背景 | 改为深色海洋底图（可加经纬度网格） |
| Layer 2.5 | **障碍物** | 雷云（半透明暗红圆+闪电图标+动画）、岛屿（棕色多边形） |
| Layer 3 | 搜索区 | 已有，需标记 SAR 侧视方向（箭头指示左视/右视） |
| Layer 6 | 船舶+轨迹 | 已有，需增强为带历史轨迹尾迹（渐变色） |
| Layer 7 | UAV+基地 | 已有三角指示，需改为**飞机图标+航向角旋转**+Dubins轨迹线 |
| Layer 10 | **传感器 footprint** | SAR 侧视条带（半透明矩形覆盖区域）、EO 视场（扇形圆锥） |

### 4.2 UI 完善

当前 `RightSidebar`、`BottomDrawer`、`PlaybackBar` 为占位符，需完整实现：

**RightSidebar**：
- 仿真概览（时间、周期、覆盖率、态势统计）
- UAV 卡片列表（状态指示灯、油量环形进度条、当前任务描述、点击可选中）
- LLM 决策摘要（最近一次 Heavy Trigger 的关键输出）

**BottomDrawer**（4 个 Tab）：
- **时间线**：带时间戳的事件流，彩色图标区分事件类型（发现目标=红色靶心、返航=橙色箭头、加油=蓝色加油站、LLM决策=紫色闪电）
- **区域详情**：当前搜索/跟踪区域的表格（ID、bbox、面积、信息素均值、价值、分配UAV、完成率）
- **LLM 日志**：最近一次 LLM 交互的 System Prompt / User Prompt / Response / Validation Results（可折叠）
- **参数**：从 `/api/config` 获取并分组织显示

**PlaybackBar**：
- ▶/⏸ 播放暂停、⏮⏭ 逐帧跳转
- 时间轴滑块（标注关键事件位置为小圆点）
- 速度选择：0.5x / 1x / 2x / 5x / 10x
- 当前帧号 / 总帧数 + 仿真时间显示
- 键盘快捷键：Space=播放/暂停, ←→=前后帧, 0-9=直接跳转百分比

**直播模式接入**：
- `src/vis/frontend/src/hooks/useWebSocket.js`：WebSocket 连接管理
  - 自动重连（指数退避 1s→2s→4s→max 30s）
  - 心跳 ping/pong（每 25s）
  - 连接状态指示灯

**回放模式接入**：
- `src/vis/frontend/src/hooks/useReplay.js`：JSONL 文件加载与帧步进

### 4.3 视觉效果要求

- **配色方案**：深色科技风（已部分实现 `#0D1117` 背景），色值统一使用 CSS 变量
- **动画**：UAV 位置变化平滑插值、雷云脉动、标记点脉动（已部分实现）、传感器 footprint 淡入淡出
- **字体**：PingFang SC / Microsoft YaHei，等宽数字用 tabular-nums
- **响应式**：Canvas 自适应用户窗口大小（已实现 ResizeObserver）

---

> **注：LLM 集成的完整细节已在第二章（动态任务区域划分与 UAV 调度）中详细阐述，此处不再重复。核心要点：使用 DeepSeek V4 Pro，禁止 Mock，校验-重试闭环最多 2 次。**

---

## 五、实现阶段划分

### Phase 1：运动模型升级（Dubins + 传感器）
- [ ] `src/env/dubins.py`：Dubins 路径求解器
- [ ] `src/env/sar_sensor.py`：SAR 侧视条带模型
- [ ] `src/env/eo_sensor.py`：EO 光电传感器模型
- [ ] `src/env/obstacle.py`：障碍物模型（雷云 + 岛屿）
- [ ] 修改 `src/env/uav_entity.py`：集成 Dubins 转弯约束 + 传感器状态
- [ ] 修改 `src/env/ship.py`：增强 zigzag 逃逸模型

### Phase 2：路径规划算法升级
- [ ] `src/utils/coverage_planner.py`：基于 Dubins 的蛇形扫描路径
- [ ] `src/utils/obstacle_avoider.py`：RRT* + Dubins 避障规划
- [ ] 重写 `src/utils/track_orbit.py`：LGVF Standoff 跟踪
- [ ] `src/utils/phase_coordinator.py`：多 UAV 等相位协调
- [ ] 修改 `main.py`：集成新的路径规划器

### Phase 3：可视化增强
- [ ] `src/vis/frontend/src/renderer/layers.js`：新增障碍物层、传感器 footprint 层
- [ ] 修改 UAV 渲染：飞机图标 + 航向旋转 + Dubins 轨迹
- [ ] `src/vis/frontend/src/components/RightSidebar.jsx`：完整实现
- [ ] `src/vis/frontend/src/components/BottomDrawer.jsx`：完整实现
- [ ] `src/vis/frontend/src/components/PlaybackBar.jsx`：完整实现
- [ ] `src/vis/frontend/src/hooks/useWebSocket.js`：WebSocket 连接管理
- [ ] `src/vis/frontend/src/hooks/useReplay.js`：回放帧管理
- [ ] `src/vis/frontend/src/App.jsx`：模式切换 + 数据流集成

### Phase 4：LLM 集成验证
- [ ] 确认 `configs/.env` 中 API Key 可用
- [ ] 确认 `configs/llm_params.yaml` 中 provider/model 配置正确
- [ ] 端到端测试：运行仿真 → LLM 决策 → 重分配 → 可视化直播
- [ ] 生成 JSONL 文件 → 回放模式验证

### Phase 5：环境与障碍物集成
- [ ] 在 `main.py` 初始化中增加障碍物生成
- [ ] 在 `frame_builder.py` 中增加障碍物数据序列化
- [ ] 在 `app.state` 中增加障碍物引用
- [ ] 端到端验证

---

## 六、关键约束

1. **Dubins 无处不在**：所有航路规划（transit、coverage、tracking、return）都必须基于 Dubins 曲线
2. **SAR 侧视约束**：覆盖扫描的条带必须在飞行轨迹一侧，不能是正下方
3. **真实 LLM**：禁止用 mock 替代真实 API（mock 仅作为降级兜底）
4. **直播+回放**：Web 界面必须支持两种模式无缝切换
5. **代码结构**：遵循已建立的 `src/` 目录布局（`src/schedule/`, `src/env/`, `src/utils/`, `src/vis/`）
6. **Git 工作流**：在 `branch1` 上开发，定期 commit

---

## 七、验证标准 — 按功能模块细化

验收不是"跑通了就行"，而是每个模块都有**可观察、可量化**的预期效果。以下按模块列出验收标准。

### 7.1 Dubins 路径验证

| # | 测试场景 | 输入 | 预期效果 | 验证方式 |
|---|---------|------|---------|---------|
| D1 | 直线飞行 | start:(0,0,0°), end:(10,0,0°), R=1 | 最短路径=直线，弧长为 0，航向不变 | 单元测试：total_length = 10.0 |
| D2 | 90° 右转 | start:(0,0,0°), end:(10,10,90°), R=1 | 路径类型 RSR，先右弧→直线→右弧 | 单元测试：每个 waypoint 满足 |χ̇| ≤ v/R |
| D3 | U 型掉头 | start:(0,0,0°), end:(0,10,180°), R=1 | LSL 或 RSR，弧+直线+弧形成平滑 U 型 | 可视化：Canvas 上渲染的路径无尖角 |
| D4 | 极端航向差 | start:(0,0,0°), end:(0,10,179°), R=1 | 自动选择 6 种类型中最短者 | 单元测试：验证 path_type ∈ {LSL,LSR,RSL,RSR,LRL,RLR} |

**可视化验收**：在 Canvas 上显示起点（绿点+方向箭头）和终点（红点+方向箭头），Dubins 路径用粗黄线渲染，弧线段和直线段用不同颜色区分（弧线=蓝，直线=白）。

### 7.2 区域覆盖搜索验证

| # | 测试场景 | 输入 | 预期效果 | 验证方式 |
|---|---------|------|---------|---------|
| C1 | 方形区域蛇形覆盖 | bbox=[0,0,6,6], swath=2 | 3 条扫描行，间隔恰好=条带宽度，无间隙 | 单元测试：所有 bbox 内 cell 被至少 1 条扫描行覆盖 |
| C2 | 长条形区域 | bbox=[0,0,10,3], swath=2 | 沿长轴 10 格方向扫描，2 次掉头 | 可视化：搜索区被蓝色半透明条带完整覆盖 |
| C3 | 相邻条带拼接 | 两相邻条带 i 和 i+1 | 条带 i 的 far-range 边界 = 条带 i+1 的 near-range 边界 | 单元测试：相邻条带间无"未覆盖 cell" |
| C4 | Dubins 掉头 | 一个扫描行结束→下一个开始 | 掉头路径在 bbox 外部完成（不在扫描区上方转弯） | 可视化：掉头弧线渲染在搜索区矩形框外 |
| C5 | SAR 侧视方向 | 奇数行左视，偶数行右视 | 条带始终在飞行轨迹的同一侧 | 可视化：搜索区框上有小箭头指示当前侧视方向 |
| C6 | 扫描行约束 | 每个扫描行上的航路点 | 高度恒定、速度恒定、直线飞行 | 单元测试：行内所有 waypoint 共线且等距 |
| C7 | 信息场更新 | UAV 扫描过的 cell | info[c,r]=1.0（黄色→绿渐变），未扫描 cell 保持暗色 | 可视化：热力图实时变化，扫描过的行变亮 |

**可视化验收**：启动仿真，1 架 UAV 分配到一个 6×5 的搜索区。观察 Canvas：
1. UAV 三角沿蛇形路径飞行，蓝色半透明条带（SAR footprint）覆盖飞行轨迹一侧
2. 被扫过的 cell 从暗色→灰色→绿色（信息素上升），形成清晰的条带图案
3. UAV 到达搜索区边界时，执行平滑 Dubins 掉头（弧线在 bbox 外）
4. 搜索区框内文字 `S1 XX%` 随时间递增

### 7.3 航路规划（避障）验证

| # | 测试场景 | 输入 | 预期效果 | 验证方式 |
|---|---------|------|---------|---------|
| O1 | 无障碍直线 | start→goal 无障碍 | 路径 = 直接 Dubins 最短路径，无绕行 | 单元测试：路径长度 = Dubins 理论最短长度 |
| O2 | 单雷云绕行 | start 和 goal 之间有一个雷云 (r=4) | 路径绕过雷云边缘，所有路径点距雷云中心 > r+2 | 单元测试：`min(dist(path_cells, storm_center)) ≥ r+2` |
| O3 | 雷云+岛屿 | 两个不同类型障碍物 | 路径从两者之间的缝隙穿过或绕外侧 | 可视化：可清楚看到路径不穿越任何障碍物 |
| O4 | 雷云移动 | 雷云每步移动 (dc,dr) | 若新位置与已有航路冲突→重新规划 | 可视化：雷云在 Canvas 上缓慢移动+脉动 |
| O5 | Dubins 平滑 | 避障后的几何路径 | 所有拐角为 Dubins 弧线（无尖角） | 可视化：路径为平滑曲线，无折线段 |
| O6 | 安全距离保持 | 任何航路点 | 距最近障碍物 cell ≥ 1 格（安全余量） | 单元测试：遍历所有 waypoint 验证 |

**可视化验收**：在 Canvas 上渲染：
1. 多个暗红半透明圆（雷云）和棕色多边形（岛屿）
2. UAV 从基地出发，淡蓝色虚线显示规划路径，明显绕开障碍物
3. 雷云有边缘脉动动画（表示危险区域）
4. 路径上从未出现 UAV 穿越障碍物内部的情况

### 7.4 目标跟踪监视验证

| # | 测试场景 | 输入 | 预期效果 | 验证方式 |
|---|---------|------|---------|---------|
| T1 | 静止目标收敛 | 目标不动，UAV 初始在轨道外 | 10 分钟内 |r-R_d| < 0.5 格 | 单元测试：最后 20 步的平均 |r-R_d| |
| T2 | 匀速移动目标 | 目标以 2 节 (≈0.02 格/min) 移动 | UAV 保持 |r-R_d| < 1 格，轨道中心跟随平移 | 可视化：轨道随目标平滑漂移 |
| T3 | Zigzag 目标 | 目标 zigzag 逃逸 | UAV 轨道有左右摆动但始终围绕目标 | 可视化：跟踪圆环始终以目标为中心 |
| T4 | 平滑接入 | UAV 从直线搜索切换到跟踪 | Dubins 过渡，无航向跳变 | 可视化：搜索→跟踪的过渡段无"急转弯" |
| T5 | 双 UAV 协同 | 2 架 UAV 跟踪同一目标 | 10 分钟内相位差 → 180°± 15° | 可视化：两 UAV 在圆上对称分布 |
| T6 | EO FOV 显示 | 跟踪中 UAV | 从 UAV 指向目标的黄色扇形视场 | 可视化：FOV 扇形覆盖目标所在 cell |
| T7 | 标记点创建 | UAV 跟踪中油尽返航 | 目标最后位置创建标记点 | 可视化：脉动圆点出现在目标最后位置 |

**可视化验收**：发现目标后：
1. UAV 三角颜色从绿（搜索）变红（跟踪）
2. 以目标为中心的淡色虚线圆环（盘旋轨道）出现
3. 黄色扇形（EO FOV）从 UAV 指向目标
4. 如有多 UAV 跟踪，它们在圆上的位置对称分布
5. 目标船在 zigzag 移动，轨道圆环跟随平移
6. UAV 油尽脱离时，目标位置留下脉动标记点

### 7.5 LLM 决策管线验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| L1 | 首次 Heavy Trigger | LLM 返回至少 1 个搜索区，JSON 格式合规 | 日志输出 LLM response |
| L2 | 校验失败重试 | 故意构造不合法 JSON → LLM 收到错误回注 → 重新输出 | 日志显示 retry count ≥ 1 |
| L3 | 区域 ID 连续 | S1 上轮 bbox 与本轮 IoU≥0.7 → 保持 ID "S1" | 日志显示 region ID 未改变 |
| L4 | 碎片合并 | 跟踪区从 S1 挖走后产生 <12 格碎片 → LLM Prompt 含碎片提醒 | 日志显示 fragment_alerts 内容 |
| L5 | 数量约束 | search+track ≤ 10 | Validator 输出 errors=[] |
| L6 | 不重叠约束 | LLM 输出的任意两搜索区 bbox 交集为空 | 单元测试遍历所有区域对 |

### 7.6 可视化整体验收 — 直播模式

**启动流程**：
```bash
# 终端1
python main.py           # 仿真 + WebSocket 服务
# 终端2
cd src/vis/frontend && npm run dev   # Vite dev server
```

**观察清单**：
1. 浏览器打开 `http://localhost:5173`，Canvas 无黑屏、无闪烁
2. 左上角显示模式切换按钮（`直播 | 回放`），默认 = 直播
3. Canvas 上热力图从全黑逐渐出现扫描条带（UAV 在执行搜索）
4. 右侧面板显示实时统计：仿真时间递增、覆盖率增大、UAV 状态更新
5. 鼠标悬停任意 cell，显示浮窗 `Cell(col,row) | 信息素:X.XX | 价值:X.XX | 态势`
6. 点击 UAV 三角，右侧面板高亮该 UAV 卡片
7. 约 30 分钟后（仿真时间）出现第一次 Heavy Trigger，Canvas 上搜索区矩形框出现/更新
8. 底部抽屉打开（时间线 Tab），可看到事件流实时追加

### 7.7 可视化整体验收 — 回放模式

**操作流程**：
1. 仿真运行结束后（`outputs/simulation_*.jsonl` 已生成）
2. 在界面上切换到"回放"模式
3. 文件列表自动加载（调用 `/api/replay/list`）
4. 选择文件 → 加载全部帧
5. **逐项验证**：
   - ▶ 点击播放 → Canvas 按帧率播放，时间轴滑块移动
   - ⏸ 点击暂停 → Canvas 停止在当前帧
   - 拖拽滑块 → Canvas 跳转到对应帧
   - 选择 2x 速度 → 播放速度翻倍
   - 按 Space → 暂停/播放切换
   - 按 ←/→ → 前后帧步进
   - 按数字 5 → 跳转到 50% 位置
6. 播放条显示 `帧 XXX / YYY | 仿真时间 HH:MM | 速度 Nx`
7. 时间轴上标注关键事件点（小圆点）：红色=发现目标、紫色=LLM决策、橙色=UAV返航

### 7.8 端到端仿真验证

| # | 指标 | 目标值 | 验证方式 |
|---|------|-------|---------|
| E1 | 仿真完整性 | 完整运行 480 步（8 小时），无崩溃 | 日志输出"仿真结束" |
| E2 | JSONL 完整性 | outputs/*.jsonl 行数 ≥ 480 | `wc -l outputs/*.jsonl` |
| E3 | Heavy Trigger 数量 | ≥ 10 次（≈16 次周期 + 事件触发） | 搜索终端日志"Trigger: heavy" |
| E4 | LLM 调用成功率 | ≥ 90%（重试后） | 日志中 `mock` 出现次数 ≤ 10% Heavy Trigger |
| E5 | 覆盖率演变 | 0h: 0% → 4h: ~50-70% → 8h: ~80-95% | 右侧面板覆盖率曲线 |
| E6 | 发现船舶数 | 所有船舶在仿真结束时被标记为 `detected=True` | 日志输出船舶发现状态 |
| E7 | 区域动态变化 | 搜索区矩形在 8h 内至少有 5 次可观测的位置/大小变化 | 回放模式 10x 速度观察 |
| E8 | UAV 全生命周期 | 每架 UAV 经历 idle→transit→searching→returning→refueling 循环 ≥ 3 次 | 日志或回放观察 |
| E9 | 跟踪区创建/释放 | 每个目标群至少创建 1 个跟踪区，标记点 ≥ 2 个 | 回放观察红色虚线框+脉动点 |
| E10 | 无阻塞 | WebSocket 连接在仿真全程保持，前端帧更新率 ≥ 15 fps | 浏览器开发者工具 Network WS 面板 |
