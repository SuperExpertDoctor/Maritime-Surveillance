# LLM 动态任务重分配系统 — 设计文档

> 日期: 2026-07-30 | 状态: Draft

---

## 1. 系统概述

### 1.1 任务背景

针对 300km × 300km 海上区域进行对海侦察与目标跟踪监视。使用 10 架彩虹/翼龙系列固定翼 UAV，每架携带 SAR 雷达、光电、雷帧三种传感器。UAV 从基地起飞，经路径规划飞往指定搜索区域，执行区域覆盖搜索；发现目标后转为盘旋跟踪监视直至油量耗尽返航。

### 1.2 核心目标

实现基于 LLM 的动态任务重分配——将 300km×300km 区域划分为矩形子区域，每架 UAV 负责一个子区域。LLM 周期性审视全局态势，输出区域划分方案。

### 1.3 关键设计原则

- **LLM 只做划分，不做配对**：LLM 输出搜索区域矩形列表；UAV↔区域配对由 Hungarian 算法在确定性层完成
- **跟踪区由规则生成**：不经过 LLM，跟踪区以目标位置为中心自动创建
- **异步触发 + 分级响应**：轻量触发（UAV 完成任务）不调 LLM，直接用 Hungarian；重量触发（周期/关键事件）调 LLM 全局重划分
- **非全覆盖**：区域为严格矩形，区域之间允许有未覆盖间隙

---

## 2. 代码目录结构

```
v3/
├── configs/                 # 所有超参数 (YAML)
│   ├── environment.yaml    # 海域尺寸、基地位置
│   ├── grid.yaml           # 网格分辨率、衰减参数、态势阈值
│   ├── uav.yaml            # UAV 机动属性（参考彩虹固定翼）
│   ├── ship.yaml           # 舰船机动属性（参考宙斯盾驱逐舰）+ zigzag 参数
│   ├── sensor.yaml         # 传感器参数（SAR、EO/IR、Radar/ESM）
│   └── llm.yaml            # LLM 周期、模型、重试次数
│
├── src/
│   ├── sensor/              # 传感器建模
│   │   ├── heading.py      # 传感器朝向控制
│   │   ├── models.py       # SAR / EO/IR / Radar/ESM 传感器模型
│   │   └── __init__.py
│   │
│   ├── control/             # 固定翼控制算法
│   │   ├── waypoint.py     # 航路点计算（当前位置→目标区域）
│   │   ├── scan_pattern.py # 覆盖扫描模式（弓形扫描）
│   │   ├── track_orbit.py  # 目标跟踪盘旋
│   │   ├── return_path.py  # 返航路径规划
│   │   └── __init__.py
│   │
│   ├── schedule/            # 核心调度
│   │   ├── info_field.py       # 信息场：30×30 cell 级信息素 + 衰减 + 价值计算
│   │   ├── info_value_table.py # 信息价值表：区域级聚合，LLM 输入数据源
│   │   ├── candidate_extractor.py  # 关键区域提取：连通域聚类 + 排序 + 碎片检测
│   │   ├── llm_client.py       # LLM 调用封装 (Decision Maker)
│   │   ├── llm_reviewer.py     # 后台 Reviewer：长期记忆凝练 (15min 周期)
│   │   ├── prompt_builder.py   # Prompt 组装（System + User 模板）
│   │   ├── output_validator.py # LLM 输出校验（矩形合法性、面积、重叠、稳定性）
│   │   ├── hungarian.py        # Hungarian 算法：UAV↔区域最小距离配对
│   │   ├── task_allocator.py   # 任务分配引擎：触发器 + 轻/重量决策流程编排
│   │   ├── trigger_manager.py  # 触发管理器：事件监听 + 合批 + 优先级仲裁
│   │   └── state_manager.py    # 全局状态管理（UAV 状态机、区域状态、标记点）
│   │
│   ├── env/                  # 仿真环境
│   │   ├── ship.py          # 舰船实体（zigzag 规避）
│   │   ├── uav_entity.py    # UAV 实体（位置/油量/航路点）
│   │   ├── base_station.py  # 基地（加油管理）
│   │   └── sim_clock.py     # 仿真时钟
│   │
│   ├── utils/               # 通用辅助
│   │   └── __init__.py      # 通用辅助函数
│   │
│   └── vis/                 # 可视化
│       ├── backend/         # FastAPI + WebSocket 服务
│       └── frontend/        # React Canvas 前端
```

---

## 3. 信息场数学模型

### 3.1 网格定义

| 参数 | 值 |
|------|-----|
| 总区域 | 300km × 300km |
| 网格分辨率 | 30 × 30 |
| 单 cell 尺寸 | 10km × 10km |
| 坐标系 | `(col, row)`, col ∈ [0,29], row ∈ [0,29] |

### 3.2 信息素指数衰减

```
I(c, r, t) = I₀ · e^(-λ · Δt)

I₀ = 1.0                           # 搜索完成时的初始信息素
λ  = ln(2) / T_half                 # 衰减常数
T_half = 30 min                     # 半衰期
Δt = t_current - t_last_scanned     # 距上次被扫描的时间

态势分类:
  白态势: I > 0.7    →  Δt < 15 min
  灰态势: 0.2 ≤ I ≤ 0.7 → 15 ≤ Δt < 70 min
  黑态势: I < 0.2    →  Δt > 70 min
```

### 3.3 跟踪区信息衰减

跟踪 UAV 在目标周围盘旋时附带扫描周边 cell，但衰减更快：

```
T_half_track = 15 min    # 跟踪扫描半衰期（搜索扫描的一半）
```

### 3.4 信息价值（复合指标）

每个 cell 的价值：

```
V(c, r) = α · (1 - I(c,r))    # 信息缺口价值
        + β · S(c, r)           # 战略价值场
        + γ · A(c, r)           # 标记点时效性

α = 1.0   (默认)
β = 0.8
γ = 0.5

S(c,r): 标记点高斯衰减场 (σ = 1.5 格 = 15km)，多标记点取 max
       标记点年龄 > 60min → S(c,r) 线性衰减至 0

A(c,r): 标记点时效性 = e^(-t_marker / 45)，无标记点 cell = 0
```

### 3.5 搜索更新

UAV 扫描 cell → 该 cell 信息素立即重置为 1.0，`t_last_scanned` 更新为当前时间。

### 3.6 信息价值表（区域级聚合）

```
每行 = 一个任务区域，区域内部 cell 独立衰减，表级聚合:

  avg_info  = mean(I(c,r))  for (c,r) in bbox
  value     = mean(V(c,r))  for (c,r) in bbox
  updated   = 最后更新时间戳
  status    = active | completed | stale
```

---

## 4. UAV 能力边界

### 4.1 搜索覆盖能力

| 参数 | 值 |
|------|-----|
| 巡航速度 | 160 km/h |
| SAR 条带宽度（条带模式） | 15 km |
| 实际覆盖率（含转弯损耗） | ~1,800 km²/h |
| 续航时间 | ~30 h |
| 加油时间 | ~12 min |

### 4.2 区域尺寸推导

| 类型 | 网格单元数 | 实际面积 | 单次全覆盖耗时 |
|------|:---------:|---------|:-------------:|
| 搜索区 | 20–40 格 | 2,000–4,000 km² | 65–130 min |
| 跟踪区 | 6–16 格 | 600–1,600 km² | —（盘旋不系统覆盖）|

- 最小面积约束（20 格）：避免"扫一下就结束"无意义分区
- 最大面积约束（40 格）：保证 UAV 在规定时间内完成覆盖，维持合理信息量均值

---

## 5. LLM 决策管线

### 5.1 分工原则

| 组件 | 职责 | 是否调 LLM |
|------|------|:---------:|
| Candidate Extractor | 从 900 cell 中提取候选关注区域 | ❌ 确定性 |
| LLM Decision Maker | 输出搜索区域矩形划分 | ✅ |
| Hungarian | UAV ↔ 区域最小距离配对 | ❌ 确定性 |
| LLM Reviewer | 长期任务进展凝练 | ✅ 后台周期 |
| Output Validator | 校验 LLM 输出合法性 | ❌ 确定性 |
| Track Region Manager | 跟踪区创建/平移/释放 | ❌ 规则驱动 |

### 5.2 Candidate Extractor 算法

```
输入: V[c][r] (价值矩阵), I[c][r] (信息素矩阵), T[] (跟踪区), S[] (上一轮搜索区)

步骤:
1. [占位] 将 T[] 的 cell 标记为"已占用"
2. [聚类] 对 V > V_threshold 的 cell 做连通域分析 → C₁, C₂, ...
3. [排序] 按 ∑V(c,r) 降序
4. [Top-K] 取前 K 个簇，K = min(可用UAV数×1.5, 10) - |T|
5. [矩形拟合] 每个 Cᵢ 拟合最小外接矩形
     - 面积 < 12 格 → 与相邻合并
     - 面积 > 40 格 → 按价值梯度切分
     - 长宽比 > 2:1 → 沿长轴切分为两个
6. [碎片检测] 上一轮 Sᵢ 挖掉新 T 后 < 12 格的碎片 → 标记"需合并"

输出: 候选搜索区列表 + 碎片提醒列表
```

### 5.3 Prompt 模板

**System Prompt**（固定）：

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

【输出格式】
严格 JSON，无额外文字。
```

**User Prompt**（每轮组装）：

```
【长期记忆】                                ← Reviewer 注入
【候选搜索区域】(按信息价值降序)               ← CandidateExtractor 输出
【跟踪中区域】                               ← 来自 state_manager
【上一轮搜索区状态】                          ← 来自 info_value_table
【碎片提醒】                                 ← CandidateExtractor 输出
【UAV 可用状态】                             ← 来自 state_manager

请输出本周期任务区域划分方案。
```

### 5.4 LLM 输出格式

```json
{
  "cycle": 12,
  "search_regions": [
    {
      "id": "S1",
      "bbox": [18, 14, 24, 19],
      "priority": "high",
      "reason": "含标记#B，信息价值0.82，黑态势"
    }
  ],
  "notes": "共覆盖约138格，需4架UAV。"
}
```

### 5.5 输出校验规则

```
1. bbox 坐标 ∈ [0,29], col_end > col_start, row_end > row_start
2. 面积: 20 ≤ (w × h) ≤ 40
3. 长宽比: max(w,h) / min(w,h) ≤ 2.0
4. 不重叠: 任意两个 bbox 交集为空
5. 不与跟踪区重叠
6. len(search_regions) + len(track_regions) ≤ 10
7. 稳定性: 与上一轮同 ID 区域 IoU ≥ 0.7

校验失败 → 错误信息回注 Prompt → LLM 重试（最多 2 次）
```

---

## 6. 触发机制

### 6.1 触发事件分类

| 事件 | 类型 | 触发效果 |
|------|:----:|---------|
| UAV 完成搜索任务 | 轻量 | Hungarian 配对，不调 LLM |
| UAV 加油完成 | 轻量 | Hungarian 配对，不调 LLM |
| UAV 油尽返航（上报标记点） | **重量** | 信息价值表更新 → 调 LLM |
| 发现新目标 | **重量** | 创建跟踪区 → 调 LLM |
| 目标丢失 | **重量** | 跟踪区释放 → 调 LLM |
| 30min 周期定时 | **重量** | 全局审视 + 信息价值重算 → 调 LLM |
| 多 UAV 集中释放 (>3 in 5min) | 合批→**重量** | 合并为一次重量触发 |

### 6.2 轻量触发流程

```
UAV 完成搜索
  → 更新 info_value_table (该区域 avg_info=1.0, value=0)
  → UAV 状态 → idle
  → 检查未分配候选区？
      ├─ 有 → Hungarian([idle_uavs], [candidate_regions])
      └─ 无 → UAV 回基地待命
  → 完成（不调 LLM）
```

### 6.3 重量触发流程

```
重量事件
  → Step 1: 更新 info_value_table（全表重算衰减+价值）
  → Step 2: CandidateExtractor 提取关键区域
  → Step 3: Prompt 组装（Reviewer 记忆 + 候选区 + 跟踪区 + 碎片）
  → Step 4: LLM → 输出新区域列表
  → Step 5: 校验（合法/面积/重叠/稳定性）→ 失败则重试 ≤2 次
  → Step 6: 新旧区域 ID 匹配（IoU ≥ 0.7 保持 ID 连续）
  → Step 7: Hungarian 配对（空闲 UAV ↔ 未分配新区域）
  → Step 8: 更新 info_value_table + state_manager
```

---

## 7. Hungarian 配对

### 7.1 代价矩阵

```
Cost[i][j] = EuclideanDistance(UAV[i].position, Region[j].center)

求解: min ΣCost → 配对列表
```

### 7.2 边界处理

- \|UAV\| > \|Regions\|：多余 UAV → 基地待命
- \|Regions\| > \|UAV\|：低优先级区域 → 候选池等待

---

## 8. 跟踪区规则（不经过 LLM）

### 8.1 生命周期

```
发现目标:
  创建 T_new: bbox = 目标位置 ± 2 格 (4×4 = 16 格, 40×40km)
  从重叠搜索区挖除 cell
  搜索区 < 12 格 → 标记碎片，下轮重量触发时合并

跟踪中:
  目标 zigzag 逃逸 → bbox 中心跟随平移
  跟踪 UAV 附带扫描周边 → 衰减半衰期 = 15min
  bbox 移出 300×300 边界 → clamp

UAV 油尽:
  上报目标最后位置 → 创建标记点
  标记点按高斯场(σ=1.5格)提升周边 cell 信息价值 → CandidateExtractor
  下次重量触发时将该区域提升为高优先级搜索候选区
  原跟踪区释放

目标丢失:
  跟踪区释放 → 原 bbox 位置 cell 信息价值 = 0.85
  → CandidateExtractor 将其纳入候选搜索区 → 触发重量重划分
```

### 8.2 跟踪区数量

- 每个目标群一个跟踪区
- 跟踪 UAV 数 = 跟踪区数
- 目标群内多艘舰船共享一个跟踪区

---

## 9. Reviewer 长期记忆

### 9.1 运行方式

- **周期**: 15 分钟（独立后台协程）
- **输入**: 自仿真开始以来的事件流摘要
- **输出**: ≤ 200 字自然语言任务进展（每次覆盖上一轮，非累积）

### 9.2 示例输出

> 过去 2 小时内，共搜索约 40% 海域，发现目标群 3 个(11 艘)。目标群#1、#3 持续跟踪中，目标群#2 于 47 分钟前丢失。UAV-2、UAV-7 已各跟踪超 90 分钟，需关注油量。NE 象限大面积黑态势未搜索，建议下轮优先。

---

## 10. 目标船舶环境

### 10.1 随机初始化

- 船舶数量: ≥ 5，最多组成 3 个目标群（每群 ≥ 1 艘）
- 初始位置: 随机分布于 300×300 海域
- 每次仿真运行环境不同（随机种子控制）

### 10.2 zigzag 逃逸

- 触发: 被 UAV 发现后立即开始
- 策略超参数定义在 `configs/ship.yaml`
- 参考宙斯盾驱逐舰机动性能设定速度/转向参数

---

## 11. 基地模型

- 位置: 定义在 `configs/environment.yaml`
- UAV 初始起飞位置 = 基地
- 油量耗尽返航目的地 = 基地
- 加油时间: ~12 min（可在 `configs/uav.yaml` 调整）
- 加油期间 UAV 不可用

---

## 12. 配置文件清单

### environment.yaml
- `sea_area`: [300, 300] km
- `base_position`: [col, row] 网格坐标

### grid.yaml
- `resolution`: [30, 30]
- `cell_size_km`: 10
- `decay_half_life_min`: 30
- `track_decay_half_life_min`: 15
- `white_threshold`: 0.7
- `gray_threshold`: 0.2

### uav.yaml
- `count_max`: 10
- `cruise_speed_kmh`: 160
- `endurance_h`: 30
- `refuel_time_min`: 12

### sensor.yaml
- `sar.swath_km`: 15
- `sar.detection_range_km`: 80
- `sar.detection_probability`: 0.85
- `eoir.fov_deg`: 30
- `eoir.detection_range_km`: 25
- `eoir.detection_probability`: 0.70
- `radar.detection_range_km`: 100
- `radar.detection_probability`: 0.90
- `general.search_efficiency`: 0.75

### ship.yaml
- `count_min`: 5
- `max_groups`: 3
- `speed_kn`: 18
- `zigzag_amplitude_km`: 5
- `zigzag_period_min`: 10
- `zigzag_phase_random`: true

### llm.yaml
- `model`: deepseek-v4  (或项目实际使用的模型)
- `heavy_cycle_min`: 30
- `reviewer_cycle_min`: 15
- `max_retries`: 2
- `temperature`: 0.3

---

## 13. 设计决策汇总

| # | 决策 | 选择 |
|---|------|------|
| 1 | 网格分辨率 | 30×30 (10km/cell) |
| 2 | 信息衰减模型 | 指数衰减 e^(-λt) |
| 3 | 半衰期 | 30 min (搜索) / 15 min (跟踪) |
| 4 | 白/灰/黑阈值 | 0.7 / 0.2 |
| 5 | LLM 重划分周期 | 30 min |
| 6 | 触发模式 | 事件驱动(轻/重量) + 周期兜底 |
| 7 | 分区形状 | 轴对齐矩形, 长宽比 ≤ 2:1 |
| 8 | 分区尺寸 | 搜索 20-40 格, 跟踪 4×4-6×6 格 |
| 9 | 是否全覆盖 | 否, 允许间隙 |
| 10 | LLM 分工 | LLM 管划分, Hungarian 管配对 |
| 11 | 跟踪区 | 规则驱动, 不经过 LLM |
| 12 | LLM 输入策略 | 方案 C — 关键区域聚焦 |
| 13 | 记忆机制 | 双层（Reviewer 长期 + 最近 N 轮语义观测） |
| 14 | 稳定性约束 | 区域面积偏差 ≤ 30%, IoU ≥ 0.7 |
| 15 | 碎片处理 | < 12 格自动合并 |
| 16 | 区域不重叠 | 跟踪区从搜索区中挖除 |
