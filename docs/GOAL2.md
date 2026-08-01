# GOAL2: 对海侦察任务 — 多基地、多目标编队、AIS 判别与动态雷云规避

> **目标模式（Goal Mode）**：Codex 需在 V1 系统基础上，自主完成环境模型升级、新功能实现、可视化增强与端到端验证。
> V1 基线见 [GOAL.md](GOAL.md)，八小时验收基线见 [VALIDATION.md](VALIDATION.md)。

---

## 一、GOAL2 总体目标

在 V1 的对海侦察系统基础上，进行以下核心升级：

1. **多基地模型**：基地数量可配置（1–3 个），每次环境初始化时随机生成于陆地/岸线位置，每个基地最多同时维护 3 架 UAV 的加油服务
2. **岛屿与雷云模型**：岛屿为静态正方形障碍物，雷云为动态正方形障碍物；尺寸符合真实海洋环境；岛屿不阻碍 UAV 飞行但船舶需绕行；雷云 UAV 不可穿越但 SAR 可穿透
3. **目标编队模型**：最大 5 个目标、最大 3 个 Group；舰艇类型含航母（必伴随 >1 艘驱逐舰）和驱逐舰；目标驶离任务区域后 UAV 放弃跟踪并恢复区域覆盖搜索
4. **AIS 信号军民判别**：UAV 跟踪目标后捕获 AIS 信号，通过对比 AIS 位置与光电雷达推算的真实位置区分军舰与民船
5. **态势透明度可视化**：连续透明度按阈值分为"黑→灰→白"三阶段，以颜色透明度在任务区域中呈现
6. **雷云规避跟踪**：UAV 在跟踪目标途中遇到雷云时，需避免进入雷云同时保持对目标的持续监视

---

## 二、多基地模型 — Base Station Redesign

> V1 中基地模型为固定单基地 + 10 个海岸支援点。GOAL2 改为可配置数量的陆地基地，每个基地有容量约束。

### 2.1 基地数量与初始化

```
基地数量: N_base ∈ {1, 2, 3}（configs/environment.yaml 可配置）

初始化规则:
  1. 任务区域: 300km×300km 海域（30×30 grid）
  2. 基地必须位于陆地/岸线 cell（grid 边缘区域，row=0 或 row=29 或 col=0 或 col=29）
  3. 每次 environment.reset() → 随机生成 N_base 个基地位置
  4. 基地之间最小距离: ≥ 5 cells（50km），避免重叠
  5. 主基地编号 Base-1，其余为 Base-2、Base-3

配置示例 (configs/environment.yaml):
  base_count: 2                 # 基地数量 (1-3)
  base_min_distance_cells: 5    # 基地间最小距离
  base_land_margin: 1           # 基地必须在距边界 margin 内的 cell（陆地/岸线）
```

### 2.2 基地容量约束

```
每个基地同时最多维护 3 架 UAV（加油/检修）:

class BaseStation:
    capacity: int = 3           # 最大同时加油 UAV 数
    _refueling_queue: dict      # uav_id → time_remaining_min
    _hangar: list[str]          # 当前在基地待命的 UAV ID

    def can_accept(self) -> bool:
        """是否有空余加油位"""
        return len(self._refueling_queue) < self.capacity

    def land_uav(self, uav_id: str) -> bool:
        """尝试降落并开始加油。返回 False 表示基地已满。"""
        if not self.can_accept():
            return False
        self._refueling_queue[uav_id] = self.refuel_time_min
        return True
```

### 2.3 UAV 返航基地选择

```
当 UAV 油量不足需要返航时:

  1. 计算 UAV 到每个基地的 Dubins 路径距离
  2. 过滤: 目标基地 can_accept() == True（有空位）
  3. 过滤: 路径距离 < UAV 当前油量可飞距离（含安全余量 2%）
  4. 选择: 距离最近的可用基地
  5. 若无可用基地 → UAV 进入 holding 状态，在最近基地附近盘旋等待
     （每 step 重新评估是否有基地空出）

UAV 生命周期新增状态:
  idle → transit → searching → returning → refueling → idle
                                          ↘ holding    (基地满，盘旋等待)
```

### 2.4 基地可视化

```
 Canvas 渲染:
   - 基地方形图标（区别于 V1 的三角），颜色按基地编号区分
     Base-1: 蓝色 #3B82F6
     Base-2: 绿色 #10B981
     Base-3: 橙色 #F59E0B
   - 基地旁数字标注: "B1 2/3"（编号 + 当前占用/容量）
   - UAV 在基地加油时，UAV 图标淡出（opacity 30%），加油进度环显示
   - 基地 busy（满容量）时边框变红色闪烁
```

---

## 三、岛屿与雷云模型

> 岛屿和雷云均为**正方形区域**，尺寸符合真实海洋环境。岛屿为静态障碍物（初始化时固定），雷云为动态障碍物（位置随时间变化）。两者对 UAV 飞行的影响不同。

### 3.1 真实海洋尺度参考

```
岛屿（真实海洋参考）:
  小型岛礁:  5–15 km²   → 1×1 或 2×2 cells（边长 10–20 km）
  中型岛屿:  15–50 km²  → 2×2 或 3×3 cells（边长 20–30 km）
  大型岛屿:  50–120 km² → 3×3 或 4×4 cells（边长 30–40 km）
  默认: 1×1 至 3×3 正方形，初始化时随机

雷云（真实海洋参考）:
  单体雷暴:  直径 5–15 km   → 1×1 至 2×2 cells
  雷暴群:    直径 15–40 km  → 2×2 至 4×4 cells
  飑线:      宽 10–30 km，长 50–200 km → 1-3 × 5-20 cells（矩形）
  默认: 1×1 至 3×3 正方形，动态移动
```

### 3.2 岛屿模型（静态正方形）

```
class Island:
    center:     GridCoord           # 正方形中心 cell
    size:       int                 # 边长 (cells)，1-3
    vertices:   list[GridCoord]     # 正方形四角（从 center 和 size 推导）
    label:      str                 # "岛屿-1", "岛屿-2", ...

    初始化:
      center = 随机位置（确保完全在任务区域内，不超出 grid 边界）
      size = random.randint(1, 3)
      验证: 岛屿不与基地 cell 重叠，岛屿之间不重叠（最小间距 ≥ 1 cell）

    岛屿数量: 2-6 个（config 可配），初始化时固定

对 UAV 的影响:
  - UAV 飞行高度（5000m AGL）远高于岛屿地形 → 不受影响
  - 不阻碍区域覆盖搜索（SAR 扫描可覆盖岛屿上方）
  - 航路规划时不视为障碍物

对船舶的影响:
  - 船舶必须绕行岛屿（不可穿越）
  - 检测: Bresenham 线段与岛屿正方形是否相交
  - 绕行: 船舶遇岛屿时调整航向（base_heading 偏转 ± 避障角度）
```

### 3.3 雷云模型（动态正方形）

```
class Thunderstorm:
    center:      GridCoord          # 当前中心 cell（浮点数，支持亚格子移动）
    size:        int                # 边长 (cells)，1-4
    move_vector: tuple[float,float] # 每步移动向量 (dc, dr)，单位 cells/min
    lifetime:    float              # 剩余存在时间 (min)，-1=永久
    intensity:   float              # 0.0-1.0，雷云强度（影响危险半径）

    初始化:
      center = 随机位置
      size = random.randint(1, 4)
      move_vector = (random.uniform(-0.05, 0.05), random.uniform(-0.05, 0.05))
      intensity = random.uniform(0.3, 1.0)

    动态更新 (每 step dt):
      center.col += move_vector.dc × dt
      center.row += move_vector.dr × dt
      if center 超出边界 → 反弹（move_vector 对应分量取反）
      if lifetime > 0:
        lifetime -= dt
        if lifetime <= 0 → 标记为 dissipated（消散）

    雷云数量: 3-8 个（config 可配），动态变化（可消散/新生）

对 UAV 的影响:
  - UAV **不可穿越**雷云内部
  - 危险区域: 雷云正方形 + 安全余量 1 cell
  - SAR 雷达可穿透雷云（X-band 雷达对降水/云层有穿透能力）
    但 SNR 会降低: SNR_eff = SNR × (1 - 0.3 × intensity)
  - EO/IR 传感器在雷云中完全失效（可见光/红外无法穿透厚云）
```

### 3.4 障碍物可视化

```
岛屿:
  - 棕色填充正方形 (#92400E)
  - 白色边框表示海岸线
  - 标签 "岛屿-N" 居中显示
  - 半透明效果 (opacity 70%)

雷云:
  - 暗红填充正方形 (#EF4444, opacity 40%)
  - 边缘有脉动/闪烁动画（危险区域暗示）
  - ⚡ 闪电图标居中或偏上
  - 雷云移动时平滑插值动画
  - 消散时渐隐动画（2-3 秒 fade out）
```

---

## 四、目标编队模型 — Ship Group & Type System

> V1 中船舶为统一的 zigzag 逃逸模型。GOAL2 引入舰艇类型、编队概念和任务区域边界约束。

### 4.1 目标数量与编队

```
配置 (configs/ship.yaml):
  target_max: 5              # 目标船舶最大数量
  group_max: 3               # 最大编队数量
  target_min: 3              # 目标船舶最小数量（新增）

初始化逻辑:
  1. 随机决定 target_count ∈ [target_min, target_max]
  2. 随机决定 group_count ∈ [1, min(group_max, target_count)]
  3. 将 target_count 个目标分配到 group_count 个编队中
     - 至少 1 个 Group 包含 ≥ 2 艘船
     - Group 内船舶在相邻 cell 中（中心 ± 1-2 cells）
     - Group 内所有船舶共享相同的基准航向 base_heading
```

### 4.2 舰艇类型

```
class ShipType(Enum):
    AIRCRAFT_CARRIER = "carrier"   # 航空母舰
    DESTROYER = "destroyer"        # 驱逐舰（含护卫舰、巡洋舰等水面作战舰）

航母约束:
  - 系统中最多出现 1 艘航母（config 可配: carrier_max = 1）
  - 航母出现时，同一 Group 内必须包含 > 1 艘驱逐舰（至少 2 艘）
  - 即含航母的 Group 至少包含 1 航母 + 2 驱逐舰 = 3 艘船
  - 航母速度略低于驱逐舰（carrier_speed_kn = 12-15，驱逐舰 18-22）

驱逐舰:
  - 可以独立成 Group（不需要航母）
  - 驱逐舰 Group 大小: 1-3 艘
  - 驱逐舰 zigzag 幅度可大于航母（驱逐舰更灵活）

class Ship:
    ship_type: ShipType
    group_id: str               # 所属编队 ID
    ais_signal: AISSignal | None  # AIS 信号对象
    is_military: bool           # True=军舰, False=民船（由 AIS 判别后确定）

    # 编队行为:
    # 同 Group 船舶以相同基准航向航行，位置在中心周围呈松散编队
    # Group 中心 = Group 内所有船舶的几何中心
```

### 4.3 目标驶离任务区域

```
任务区域边界: [0, 29] × [0, 29]（30×30 grid）

当目标船舶浮点位置超出边界时:
  1. 判定目标已驶离任务区域
  2. 若该目标正被 UAV 跟踪:
     a. UAV 立即放弃跟踪
     b. 释放跟踪区（release_track_region）
     c. UAV 状态 → idle（或返回搜索区）
     d. 不创建标记点（目标主动驶离，非丢失）
  3. 目标从仿真中移除（或标记为 departed）
  4. 日志记录: "目标 {id} 驶离任务区域，UAV {uav_id} 恢复搜索"

边界检测:
  每 step 检查所有 target.float_position
  if col < -0.5 or col > 29.5 or row < -0.5 or row > 29.5:
    → target.departed = True

船舶边界反弹修改:
  V1 中船舶在边界反弹。GOAL2 中:
  - 未检测到的目标: 到达边界后反弹（保持区域内）
  - 检测到后、已被跟踪的目标: 到达边界后继续驶出（尝试逃脱跟踪）
  - 驱逐舰在航母 Group 中: 不单独驶离，跟随 Group 中心
```

### 4.4 编队可视化

```
 编队中心: 半透明大圆（Group 包围圈），颜色按 Group ID 区分
 航母: 大型船舶图标（区别于驱逐舰），灰色/深色
 驱逐舰: 中型船舶图标，海军灰
 民船（AIS 判别后）: 小型船舶图标，白色/浅蓝，不显示跟踪框
 目标驶离: 在边界处渐隐消失 + "已驶离"文字标注
```

---

## 五、AIS 信号军民判别系统

> 这是 GOAL2 的核心新功能之一。当 UAV 跟踪到目标后，系统自动捕获目标的 AIS 信号，通过对比 AIS 自报位置与 UAV 推算的真实位置，区分军舰与民船。民船不予跟踪，释放 UAV 资源。

### 5.1 AIS 信号模型

```
AIS (Automatic Identification System):
  民用船舶强制广播: 位置、航速、航向、船名、MMSI、船型
  军舰: 可选择性关闭 AIS 或广播虚假位置（战术欺骗）
  民船: 持续广播真实 AIS 信号

class AISSignal:
    mmsi: str                   # 海上移动通信业务标识（9 位数字）
    reported_position: GridCoord  # AIS 自报位置
    reported_speed_kn: float    # AIS 自报航速
    reported_heading: float     # AIS 自报航向
    ship_name: str              # 船名
    ship_type: str              # AIS 船型代码（"Cargo", "Tanker", "Fishing"等）
    timestamp: float            # 信号时间戳（仿真时间）

AIS 信号特性:
  - 更新频率: 每 2-10 秒（取决于航速）→ 仿真中简化为每 1 min
  - 民船 AIS 位置误差: ± 1 cell（GPS 精度 + AIS 报告延迟）
  - 军舰 AIS 特性:
    方案 A（关闭 AIS）: 无 AIS 信号 → 直接判定为军舰
    方案 B（广播虚假位置）: AIS 位置与真实位置偏差 > 2 cells → 判定为军舰
    方案 C（正常广播）: 军舰伪装民船，AIS 位置 = 真实位置 → 需额外判别
```

### 5.2 UAV 推算目标真实位置

```
当 UAV 处于 tracking 模式（EO/IR 传感器开启）:

  UAV 自身位置: (x_uav, y_uav) — 已知（GPS + 惯性导航）
  EO/IR 传感器测量:
    目标方位角: θ_target（相对于 UAV 航向）
    目标距离:   d_target（由 EO/IR 激光测距或被动测距推算）

  推算目标真实位置:
    x_estimated = x_uav + d_target × cos(χ_uav + θ_target)
    y_estimated = y_uav + d_target × sin(χ_uav + θ_target)

  测量误差:
    方位误差: σ_θ = 0.5°（EO/IR 稳定平台）
    距离误差: σ_d = 0.1 km（激光测距）/ 0.5 km（被动测距）
    → 最终位置推算误差: ± 0.3-0.5 cell

  位置推算在每 step 执行（连续跟踪条件下）
```

### 5.3 军民判别逻辑

```
判别流程:

Step 1: 目标被发现 → UAV 切换 tracking → EO/IR 传感器开启

Step 2: 系统自动查询该目标 AIS 信号（仿真中: 从目标 ship 对象获取）

Step 3: 判别:
   if 无 AIS 信号:
     → 该船舶关闭了 AIS → **判定为军舰** → 继续跟踪

   elif 有 AIS 信号:
     distance = EuclideanDist(AIS.reported_position, UAV推算位置)
     if distance > ais_discrepancy_threshold（默认 2 cells = 20km）:
       → AIS 位置与真实位置不符 → **判定为军舰**（广播虚假 AIS）
     else:
       → AIS 位置与真实位置一致 → **判定为民船** → 放弃跟踪

Step 4: 民船处理:
   if 判定为民船:
     a. UAV 停止跟踪该目标
     b. 释放跟踪区
     c. 不创建标记点
     d. UAV 状态 → idle（或返回搜索区）
     e. 日志: "目标 {id} 判定为民船 (AIS: {mmsi}, {ship_name})，放弃跟踪"
     f. 可视化: 目标图标变为白色，持续 30 秒后从跟踪显示中移除

Step 5: 军舰处理:
   if 判定为军舰:
     a. 继续跟踪
     b. 标记 ship.is_military = True
     c. 日志: "目标 {id} 判定为军舰 (AIS偏差={distance} cells)，持续跟踪"

配置 (configs/ship.yaml 新增):
  ais_discrepancy_threshold_cells: 2  # AIS 位置偏差阈值
  ais_update_interval_min: 1          # AIS 信号更新间隔
```

### 5.4 判别时机

```
判别发生在 UAV 首次进入 tracking 模式后的 N 分钟内:
  - N = ais_discrimination_delay_min（默认 2 分钟）
  - 原因: EO/IR 需要时间稳定跟踪 + 多次测量取平均以降低误差
  - 在 discrimination_delay 期间: UAV 正常跟踪，积累位置测量样本
  - 到期后: 取位置估计的中位数与 AIS 位置比较

 若目标在判别完成前就丢失:
   → 标记点记录"未完成判别"
   → 下次重新发现时重新进行判别
```

### 5.5 AIS 判别可视化

```
 BottomDrawer 新增 "AIS" Tab:
   - 当前跟踪目标的 AIS 信息表
   - 列: 目标ID | MMSI | AIS位置 | 推算位置 | 偏差 | 判定结果
   - 判定为军舰的行: 红色高亮
   - 判定为民船的行: 灰色

 Canvas 上:
   - 判定为军舰: 目标上方显示 ⚓ 红色锚图标
   - 判定为民船: 目标上方显示 🚢 灰色商船图标，渐隐后移除
   - AIS 位置 vs 推算位置: 半透明虚线连接两个位置点（判定时短暂显示）
```

---

## 六、态势透明度可视化

> 态势透明度是描述 UAV 编队对任务区域"感知程度"的连续变量。V1 中已有信息素 I(c,r) 的三分法（黑/灰/白态势），GOAL2 将其升级为连续可视化。

### 6.1 数学模型（复用 V1 信息场）

```
态势透明度 T(c,r) = I(c,r)
  （直接复用 V1 信息素值，见 GOAL.md § 2.2.1）

I(c,r) ∈ [0, 1]
  1.0: 完全透明（刚扫描，信息最新）
  0.0: 完全不透明（从未扫描或长期未更新）

阈值分阶段:
  白态势: T > 0.7   → 信息新鲜，高透明度
  灰态势: 0.2 ≤ T ≤ 0.7 → 信息开始陈旧，中等透明度
  黑态势: T < 0.2   → 信息缺失，低透明度（不透明）
```

### 6.2 可视化方案

```
Canvas 热力图叠加层:

  策略: 在任务区域上叠加半透明覆盖层，颜色深度与 I(c,r) 成反比
  
  黑色覆盖层:
    opacity = 1 - T(c,r) × 0.9    // T=1.0 → opacity=0.1（几乎透明）
                                   // T=0.0 → opacity=1.0（完全不透明，黑色遮盖）
  
  颜色映射:
    黑态势 (T < 0.2):  覆盖层 opacity 0.82-1.0 → 深黑遮盖，看不清下方海面
    灰态势 (0.2-0.7):  覆盖层 opacity 0.37-0.82 → 半透明灰遮盖
    白态势 (T > 0.7):   覆盖层 opacity 0.1-0.37 → 几乎透明，海面清晰可见

  实现:
    - 在 Layer 1（热力图）之上新增 Layer 1.5（透明度覆盖层）
    - 每个 cell 渲染一个矩形，fillStyle = `rgba(0,0,0, ${1 - I(c,r) * 0.9})`
    - 相邻 cell 之间无缝拼接（无边框）
    - 更新频率: 每 step（随信息素衰减实时更新）

  平滑过渡:
    - 信息素连续衰减 → 透明度连续变化
    - 不使用离散的颜色跳变
    - UAV 扫描过的条带 → 黑色逐渐消退（opacity 降低），形成"拨云见日"的视觉效果

  Legend（图例）:
    - 右下角渐变色条: 黑 → 灰 → 白
    - 标注: "黑: 未扫描 | 灰: 信息陈旧 | 白: 信息新鲜"
    - 与 V1 的态势分类彩色方框配合显示
```

### 6.3 与 V1 态势标记的关系

```
V1 已有的态势标记（BottomDrawer / 统计面板）保持不变:
  - 右侧面板统计: 黑态 cell 数 / 灰态 cell 数 / 白态 cell 数
  - 候选区域优先级: 黑态 > 灰态 > 白态

GOAL2 新增:
  - Canvas 上的连续透明度覆盖层（上述 Layer 1.5）
  - hover 浮窗增加 "透明度: X.XX | 态势: 黑/灰/白"
```

---

## 七、雷云规避跟踪

> 当 UAV 在目标跟踪（Standoff 盘旋）过程中遇到雷云，需要在**不穿越雷云**的前提下**保持对目标的持续监视**。这是固定翼 UAV 约束下最复杂的决策场景之一。

### 7.1 问题描述

```
场景:
  UAV 正以圆形 Standoff 轨道绕目标盘旋（LGVF 引导）
  目标（船舶）正朝某一方向移动
  雷云（动态）正靠近或位于 UAV 当前轨道与目标之间

约束:
  1. UAV 不可进入雷云正方形 + 1 cell 安全余量
  2. UAV 必须保持目标在 EO/IR 视场内（最大距离 25 km = 2.5 cells）
  3. UAV 飞行满足 Dubins 约束（|χ̇| ≤ v/R_min）
  4. 目标持续移动，轨道中心动态变化
```

### 7.2 规避策略

```
三级响应机制:

Level 1 — 轨道调整（雷云在轨道外围）:
  条件: 雷云边缘距离轨道 > 1 cell
  行为: 
    - 临时增大 standoff_radius R_d（从 1.8 → 2.5-3.0 cells）
    - UAV 以更大的圆绕飞，绕开雷云区域
    - EO/IR 视距仍在作用范围内（≤ 2.5 cells 优先）
    - 雷云移走后逐步恢复原始 R_d
  实现: LGVF 的目标半径参数动态调整

Level 2 — 轨道偏移（雷云部分覆盖轨道）:
  条件: 雷云边缘距离轨道 ≤ 1 cell，但目标仍在清晰视场内
  行为:
    - 暂停圆形盘旋，切换到 Dubins 避障路径
    - 在雷云外侧生成临时航路点序列，UAV 沿该路径飞行
    - 路径约束: 所有航路点到目标距离 ≤ eo_detection_range（2.5 cells）
    - EO/IR 云台持续指向目标（转塔可 360° 旋转）
    - 雷云移走后 → 重新计算 LGVF 轨道切入路径

Level 3 — 紧急规避（雷云直接覆盖目标上空）:
  条件: 雷云中心距离目标 < 雷云 size/2 + 安全余量（即雷云覆盖了目标）
  行为:
    - UAV 在雷云外侧安全区域盘旋等待
    - EO/IR 失效（云层遮挡），目标可能丢失
    - 预测目标位置: 基于最后已知位置 + 航向航速推算
    - 雷云移走后 → 在预测位置附近重新搜索目标
    - 若重新发现 → 恢复跟踪
    - 若目标丢失 → 创建标记点，触发 Heavy Trigger

触发检测:
  每 step 执行:
    1. 计算 UAV 当前位置到雷云正方形边界的距离
    2. 预测 UAV 下一 step 位置（基于当前航向和速度）
    3. 若预测位置进入雷云安全余量范围 → 触发对应 Level 的规避
```

### 7.3 雷云规避与 LGVF 的集成

```
修改 LGVFTracker.compute_guidance():

  新增参数: thunderstorm_zones: list[tuple[GridCoord, int]]
    （雷云中心列表 + 对应 size）

  在引导计算中增加斥力场:
    if min_distance_to_any_storm(uav_position) < storm_avoidance_radius:
      → 在期望航向 χ_d 中加入偏离雷云方向的分量
      → χ_d = α × χ_lgvf + (1-α) × χ_storm_avoidance
      → α 随到雷云距离减小而减小（越近越偏向避障方向）

  安全约束:
    确保 |χ̇_cmd| ≤ v/R_min  （转弯速率不超限）
    若无法同时满足跟踪 + 避障 → 优先避障（Level 3）
```

### 7.4 雷云规避可视化

```
 Canvas 渲染:
   - 雷云危险区域: 暗红正方形外围 + 黄色虚线安全余量圈（+1 cell）
   - UAV 规避路径: 淡蓝色虚线，显示计划绕飞路径
   - 当前危险级别: UAV 上方显示 Level 指示器
     L1: 黄色 ⚠
     L2: 橙色 ⚠⚠
     L3: 红色 🚨

 BottomDrawer 日志:
   "UAV-3 检测到雷云接近 (距离=1.2cells)，Level 2 轨道偏移已激活"
   "UAV-3 雷云威胁解除，恢复正常 Standoff 跟踪"
```

---

## 八、实施阶段划分

### Phase 1：基础模型升级
- [ ] `configs/environment.yaml`：新增 base_count、base_capacity、base_min_distance、base_land_margin
- [ ] `configs/ship.yaml`：新增 target_max、group_max、target_min、carrier_max、ais_* 参数
- [ ] `src/env/base_station.py`：重写为多基地 + 容量约束模型
- [ ] `src/env/obstacle.py`：重写 Island 为正方形模型，重写 Thunderstorm 为动态正方形模型
- [ ] `src/env/ship.py`：新增 ShipType（航母/驱逐舰）、Group 编队逻辑、任务区域驶离检测

### Phase 2：AIS 判别系统
- [ ] `src/env/ais_signal.py`：`class AISSignal` — AIS 信号数据结构与生成逻辑
- [ ] `src/utils/ais_discriminator.py`：`class AISDiscriminator`
  - `estimate_target_position(uav_pose, eo_measurement) → GridCoord`
  - `discriminate(ais_signal, estimated_position) → DiscriminateResult`
  - `class DiscriminateResult`: is_military, confidence, reason
- [ ] 修改 `src/env/uav_entity.py`：集成 AIS 判别触发 + 结果处理
- [ ] 修改 `src/schedule/state_manager.py`：民船判定后释放跟踪区 + 释放 UAV

### Phase 3：雷云规避跟踪
- [ ] 修改 `src/utils/track_orbit.py`（LGVFTracker）：
  - `compute_guidance()` 新增 storm_zones 参数
  - 实现三级响应（轨道调整/偏移/紧急规避）
- [ ] `src/utils/storm_avoider.py`：`class StormAvoider`
  - `detect_threat(uav_pose, storms) → ThreatLevel`
  - `plan_avoidance(uav_pose, target_pos, storms, R_min) → waypoints[]`
- [ ] 修改 `src/env/uav_entity.py`：`_step_tracking()` 中集成雷云检测与规避

### Phase 4：态势透明度可视化
- [ ] 新增 Canvas Layer 1.5：透明度覆盖层渲染
- [ ] `src/vis/frontend/src/renderer/layers.js`：新增 `renderTransparencyOverlay()`
  - 每 cell 渲染 `rgba(0,0,0, opacity)` 矩形
  - opacity = 1 - I(c,r) × 0.9
- [ ] hover 浮窗增加透明度显示
- [ ] 右下角图例（黑→灰→白渐变条）

### Phase 5：多基地与编队可视化
- [ ] 基地渲染：方形图标 + 颜色区分 + 容量标注 + 满容闪烁
- [ ] 编队渲染：Group 包围圈 + 航母/驱逐舰大小区分
- [ ] AIS 判别结果渲染：军舰锚图标 / 民船商船图标
- [ ] 雷云规避状态指示器（Level 1/2/3）
- [ ] 目标驶离动画（渐隐 + 标注）

### Phase 6：端到端验证
- [ ] 多基地随机初始化 + 容量约束场景测试
- [ ] AIS 判别准确率测试（军舰/民船各 20 个以上测试用例）
- [ ] 雷云规避跟踪场景测试（静止目标 + 移动目标 + zigzag 目标）
- [ ] 透明度可视化验收（Canvas 渲染 + 实时更新）
- [ ] 目标驶离任务区域完整流程测试
- [ ] 8 小时完整仿真验收（输出 JSONL + 回放验证）

---

## 九、关键约束

1. **基地容量硬约束**：每个基地最多 3 架 UAV 同时加油，超限的 UAV 必须进入 holding 等待
2. **岛屿与雷云差异化处理**：岛屿 UAV 可飞越、船舶需绕行；雷云 UAV 不可飞越、SAR 可穿透但 EO/IR 不可
3. **真实尺度**：岛屿和雷云尺寸必须基于真实海洋环境数据，不可随意设定
4. **AIS 判别真实逻辑**：基于位置偏差的判别，不可用随机或 mock 替代
5. **固定翼约束不变**：所有新增航路（避雷云、轨道调整）必须基于 Dubins 曲线
6. **编队一致性**：同 Group 船舶行为需保持一致（同航向、松散编队），航母 Group 驱逐舰数量 ≥ 2
7. **透明度连续性**：态势透明度必须连续变化，不可仅使用离散的黑/灰/白三色
8. **向后兼容**：V1 仿真 JSONL 格式保持兼容，新增字段以 optional 方式追加

---

## 十、验证标准

### 10.1 多基地验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| B1 | base_count=1 | 单基地，行为与 V1 一致 | 回归测试 |
| B2 | base_count=2，随机初始化 | 2 个基地位于岸边，间距 ≥ 5 cells | 多次 reset 检查位置分布 |
| B3 | base_count=3 | 3 个基地负载均衡使用 | 统计各基地加油次数 |
| B4 | 基地满容 (3/3) | 第 4 架 UAV 进入 holding 状态 | 单元测试 |
| B5 | 基地空出后 UAV 降落 | holding UAV 自动切换到 refueling | 仿真观察 |

### 10.2 岛屿与雷云验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| I1 | 岛屿正方形生成 | 所有岛屿为正方形，尺寸 1-3 cells | 单元测试 + 可视化 |
| I2 | 船舶绕行岛屿 | 船舶路径不穿越岛屿 cell | Bresenham 碰撞检测 |
| I3 | UAV 飞越岛屿 | UAV 路径可穿越岛屿，不受影响 | 搜索路径穿越岛屿验证 |
| T1 | 雷云正方形 + 动态移动 | 雷云随时间移动，边界反弹 | 可视化观察 |
| T2 | UAV 不可穿越雷云 | 所有航路点距雷云 ≥ 安全余量 | 单元测试 |
| T3 | SAR 穿透雷云 | 雷云覆盖区域的 cell 仍可被扫描 | SNR 计算验证 |
| T4 | 雷云消散 + 新生 | 生命周期结束的雷云消失，新雷云出现 | 长时间仿真观察 |

### 10.3 目标编队验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| G1 | 驱逐舰 Group (2艘) | 同航向，松散编队，间距 1-2 cells | 可视化观察 |
| G2 | 航母 Group (1航母+2驱逐舰) | 驱逐舰围绕航母，编队航行 | 单元测试：驱逐舰数 ≥ 2 |
| G3 | target_count=5, group_count=3 | 5 个目标分配到 3 个 Group | 初始化验证 |
| G4 | 目标驶离任务区域 | UAV 放弃跟踪，目标移除/标记 departed | 单元测试 + 日志 |
| G5 | 未检测目标边界反弹 | 未被跟踪的目标在边界处反弹 | 与 V1 行为对比 |

### 10.4 AIS 判别验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| A1 | 民船 AIS 匹配 | AIS 位置 = 推算位置 → 判定为民船 → 放弃跟踪 | 单元测试 |
| A2 | 军舰 AIS 不匹配 | AIS 位置偏差 > 2 cells → 判定为军舰 → 继续跟踪 | 单元测试 |
| A3 | 军舰关闭 AIS | 无 AIS 信号 → 判定为军舰 → 继续跟踪 | 单元测试 |
| A4 | 判别延迟 | 首次跟踪后 2 分钟执行判别 | 时间戳验证 |
| A5 | 民船释放 UAV | 判定为民船后 UAV 恢复搜索（非 idle） | 状态转换验证 |
| A6 | 判别准确率 | ≥ 95% 判定正确（20 军 + 20 民测试） | 批量测试 |

### 10.5 雷云规避跟踪验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| S1 | Level 1 — 轨道调整 | 增大 R_d 绕开雷云，仍跟踪目标 | 单元测试 + 可视化 |
| S2 | Level 2 — 轨道偏移 | 临时航路绕飞，目标在 EO 视场内 | 路径与目标距离验证 |
| S3 | Level 3 — 紧急规避 | UAV 在安全区域等待，目标丢失后重建标记点 | 单元测试 |
| S4 | 雷云移走后恢复 | UAV 自动恢复正常 Standoff 跟踪 | 可视化观察 |
| S5 | Dubins 约束 | 规避过程中所有航路满足 |χ̇| ≤ v/R | 单元测试 |

### 10.6 态势透明度可视化验证

| # | 测试场景 | 预期效果 | 验证方式 |
|---|---------|---------|---------|
| V1 | 初始态势 | Canvas 全黑覆盖（I=0 全区域） | 截图对比 |
| V2 | UAV 扫描后 | 扫描条带区域黑色消退（I→1），透明化 | Canvas 渲染观察 |
| V3 | 信息素衰减 | 未重扫区域黑色逐渐加深（连续变化） | 快进仿真观察 |
| V4 | 三阶段可读 | 明显分辨黑（未扫）、灰（陈旧）、白（新鲜） | 视觉验收 |
| V5 | 图例显示 | 右下角图例正确标注 | 截图确认 |

### 10.7 端到端验收

| # | 指标 | 目标值 | 验证方式 |
|---|------|-------|---------|
| E1 | 仿真完整性 | 完整运行 480 步（8 小时），无崩溃 | 日志输出 |
| E2 | 基地利用率 | 多基地时各基地加油次数差值 ≤ 30% | 统计分析 |
| E3 | AIS 判别次数 | 每次目标跟踪均触发判别 | 日志统计 |
| E4 | 民船正确释放 | 民船判定后 UAV 100% 释放 | 状态转换日志 |
| E5 | 雷云规避触发 | 雷云靠近时 100% 触发规避 | 仿真观察 |
| E6 | 目标驶离处理 | 目标驶离后跟踪区释放 + UAV 恢复搜索 | 日志验证 |
| E7 | 航母编队约束 | 航母 Group 驱逐舰数始终 ≥ 2 | 初始化 + 全程验证 |
| E8 | JSONL 兼容性 | V1 回放器可读取 GOAL2 的 JSONL | 回放测试 |
