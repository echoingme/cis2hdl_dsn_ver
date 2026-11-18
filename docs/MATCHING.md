# MATCHING（匹配系统设计与分析）

> 文档元信息
> - 编号：CORE-MATCH；版本：v4.0（2026-09-01 mentor 交接改造）
> - 角色：匹配系统专项（背景与根因 + v2.0 设计 + 六类路由 + 当前指标 + 非数值匹配边界 + 真实工程格式证据）
> - 当前基线：R1 504 + R2 48 + R3 185 + R4 6 + GND 675 + R6 321；精确转换 1500（86.26%）；hdl 符号转换 89.02%（含 R2 conf≥0.60）
> - 关联：ARCHITECTURE.md §14（路由定义）/ STATUS.md §5（匹配指标）
> - 交接说明：本文档供接手开发人员阅读
> - 历史注记：本文档由历史文档整合而来，历史原件见 docs/archive/（含 temp files/非数值匹配能力分析.md、phaseXI_P0_fix_evidence.md、00-core-pre-consolidation/）

---

## Part 0 文档说明

### 0.1 文档定位

本文档为匹配系统专项：背景与根因分析、两阶段匹配 v2.0 设计、六类结果路由 R1-R6、当前匹配结果与指标（TG0C0D2）、非数值匹配能力与边界、真实工程格式证据。历史整合来源见 docs/archive/。

### 0.2 当前口径

- 六类路由：R1 精确（conf≥0.99）/ R2 近似（0.90≤conf<0.99，项目决策）/ R3 mock / R4 非电气 / R5 GND / R6 连接符；
- 精确转换 1500（86.26%）= R1 + GND + 连接符（R1 504 + GND 675 + R6 321）；
- hdl 符号转换 89.02%（含 R2 conf≥0.60）；
- APPROX_THRESHOLD = 0.90（QA 修复：OR 分支曾绕过阈值）；
- crystal 特判：conf<0.99 → R3；J/LB/L/UD 全 R3 mock（项目决策）。

---

## Part I 匹配问题背景

### 1. 新旧系统对比总览

| 维度 | 旧系统（v0.9.0 MultiScorer） | 新系统（v2.0 两阶段） |
|---|---|---|
| 类型约束 | SOFT WEIGHT（类型只是权重之一） | HARD GATE（类型假设优先，Phase 1 定类型） |
| 评分 | 六维加权（MultiScorer） | 分阶段、可追溯（phase1_prior × phase2_within） |
| 值匹配 | ValueMatcher 无类型检查 | PassiveMatcher 类型感知确定性规则 |
| 兜底 | FallbackMatcher 分数通胀 | 路由兜底（R3 mock 1:1 复刻） |
| 结果 | 类型正确性严重下降 | 精确转换 76.97%，hdl 符号 96%+ |

背景结论：MultiScorer 六维权重的结构性缺陷导致类型正确性严重下降，v2.0 需要重做匹配架构（保留两阶段但修正 Phase 1 锁死类型问题）。

### 2. 根因分析

#### 2.1 架构层面：类型约束从 HARD GATE 退化为 SOFT WEIGHT

旧系统把"类型一致性"从硬性门槛降为权重之一：只要其他维度得分高，类型不符也能拿到高分（如电容 → resistor conf=1.0）。

#### 2.2 MultiScorer 六维权重的结构性缺陷

- 六维（value/footprint/pin_count/prefix/type/name）权重固定，无法表达"某维缺失时该维不应贡献分数"；
- 缺失维按中性分处理 → 信息量少的元件分数被稀释/通胀；
- 维度间相关性未建模（值 + 封装强相关时双重计分）。

#### 2.3 ValueMatcher 无类型检查的致命缺陷

```python
# ValueMatcher.match() — 当前逻辑（旧）
# 只做字符串归一化比较，不检查类型
```

1mF 电容 → resistor（conf=1.0，VALUE 策略）：值"1mF"与某电阻值归一化后相等，但类型完全不同。

#### 2.4 FallbackMatcher 的分数通胀

无候选命中时 Fallback 给一个"看起来合理"的分数（conf=0.5），导致 NEEDS_REVIEW 与真匹配无法区分。

#### 2.5 PST Value 列和 JEDEC_TYPE 列的来源

- PST（pstxnet/pstchip）的 Value 列来自 OrCAD 导出；JEDEC_TYPE 来自 pstchip 的 JEDEC_TYPE 字段；
- 旧系统把这些当匹配特征，但 DSN 时代已删除（DSN 无 JEDEC；由 value+footprint+引脚数替代）。

#### 2.6 "Best match via MultiScorer: X" 的含义

旧报告行表示 MultiScorer 综合六维评分后的最高分候选，但"最高分"不等于"类型正确"。

### 3. 具体案例深度分析

| 案例 | 元件 | 期望 | 旧结果 | 错误类型 |
|---|---|---|---|---|
| C11 | 1mF 电容 | capacitor | resistor（conf=1.0, VALUE） | 类型错误 |
| C21/C282/C394 | 22UF 电容 | capacitor | inductor（conf=0.7） | 类型错误 |
| D21 | 二极管 | DIODE | resistor（conf=1.0, VALUE） | 类型错误 |
| M1/M2/M3-M6 | MARK | mark | rtxm169/ch347（conf=0.5） | 误匹配 |
| R117/R92/R2/R42 | 电阻 | resistor | capacitor（conf=0.6-0.7） | 类型错误 |
| R228/R229 | 9.1NH 电感 | inductor | resistor（conf=0.7） | 类型错误 |
| C284 | 22UF 电容 | capacitor | uc2843（conf=0.8, 像芯片名） | 值误解析 |
| D9 | 空值二极管 | DIODE | conf=0.5 FALLBACK | 分数通胀 |
| IC3 | AMS1117-1.5 稳压器 | regulator | 匹配问题（值无法归一化） | 值误解析 |

共同根因：类型假设缺失 + 值归一化破坏性 + 分数通胀。

### 4. 根本原因总结

- 旧系统（v0.8.2 output_final 类型正确性 100%）：有类型硬门 + 人工规则，虽覆盖窄但类型不出错；
- 新系统（v1.0 output_phaseX_test）：类型正确性严重下降（MultiScorer 权重设计缺陷）；
- 结论：匹配架构必须"类型先行 + 确定性规则 + 可追溯 conf"，而非黑盒加权。

---

## Part II 匹配系统 v2.0 设计

### 5. 两阶段匹配架构（修正版）

```
MatcherPipeline.run_batch(sources, db)
  → Phase 1: TypeHypothesisGenerator     # 类型假设（位号前缀 + 值特征 + footprint）
  → Phase 1.5: CandidatePoolBuilder      # 候选池（hdl_lib_only=True）
  → Phase 2: PassiveMatcher / ActiveMatcher
  → final_conf = phase1_prior × phase2_within
  → 阈值: NEEDS_REVIEW=0.40, STOP_SEARCH=0.75（R2 用 0.90）
```

关键设计（避免 Phase 1 锁死类型）：

- Phase 1 产出类型假设的概率分布而非单点（prefix → [(type, prior_conf)]，按 prior_conf 降序）；
- Top-3 候选生成：同类型候选按分数取 Top-3 进 Phase 2，避免一次锁死；
- 被动元件（C/R/L）不用加权评分，用确定性规则（值+封装精确匹配）。

被动元件为什么不能用加权评分：C/R/L 值域高度重叠（1K 电阻 vs 1K 电容值相同），只有类型门 + 封装门才能区分。

### 6. 类型映射表 type_gate.yaml（动态可学习）

```yaml
# 格式: prefix → [(type, prior_conf), ...]（列表按 prior_conf 降序）
R:    [[resistor, 1.0], [resistor_net, 0.9], [resistor_array, 0.8]]
C:    [[capacitor, 1.0], [capacitor_array, 0.9], [capacitor_polarized, 0.8]]
L:    [[inductor, 1.0]]
FB:   [[ferrite_bead, 1.0]]      # hdl_lib 只有 fb，需改（见 §15）
LB:   [[ferrite_bead, 0.75]]     # 同上
D:    [[diode, 1.0], [led, 0.8]]
Q:    [[transistor, 1.0]]
U:    [[ic, 0.6], [regulator, 0.5]]     # U 前缀类型需值特征辅助
J:    [[connector, 0.9]]
X/Y:  [[crystal, 0.85], [oscillator, 0.75]]
CS:   [[capacitor, 1.0]]         # 2026-08-21 QA 补（CS 前缀曾漏，占 R3 59%）
```

值特征辅助：MHz/kHz → crystal/oscillator 提升 prior；CRYSTAL → crystal；电源 IC 型号（AMS1117/MP1584）→ regulator 提升。

### 7. PrefixAffinityCalculator 与 conf 值重新定义

- PrefixAffinityCalculator 新角色：Phase 1 类型假设的 prefix 权重计算器（不再做最终评分）；
- conf 重新定义（分阶段、可追溯）：final_conf = phase1_prior × phase2_within，每阶段可单独审计；Phase 2 内部：PASSIVE_EXACT = 1.0、PASSIVE_VALUE_NEAR = 0.7、PASSIVE_VALUE_ONLY = 0.8、PASSIVE_SIZE_ONLY = 0.6、ACTIVE 评分 = 多维乘积。

### 8. CSV/HTML 报告增强（Top-3 示例）

- 报告输出每个元件的 Top-3 候选（类型 + conf + 差异原因），不再只给"最佳匹配"；
- 路由统计：R1_exact / R2_approx / R3_mock / R4_non_electrical / R5_gnd / R6_connectors 计数。

---

## Part III 六类结果路由 R1-R6

### 9. 路由定义与下游处理

| 路由 | 判定条件 | 下游处理 | symbol.css 来源 |
|---|---|---|---|
| R1 精确 | final_conf ≥ 0.99（值+封装+引脚数全等） | hdl_lib 符号 + 继承 DSN 缺失信息 | hdl_lib（继承） |
| R2 近似 | 0.90 ≤ conf < 0.99 | hdl_lib 符号 + 继承 + 差异记录 | hdl_lib（继承） |
| R3 mock | conf < 0.90 且电气类 | 1:1 几何复刻 → TG0C0D2_VB_lib | mock（复刻） |
| R4 非电气 | test point / mark / 机械符号 | 忽略为实例；有几何则复刻为页面图形 | 无（图形） |
| R5 GND | shape=GND_SYMBOL 或 source symbol GROUND/POWER | hdl_lib GND 元件化放置 | hdl_lib GND |
| R6 连接符 | ONSHEET2/OFFSHEET2/BISHEET source symbol | 连接符复刻（按坐标/网名/目标页） | 页面级（IOPORT/网标） |

R1 100% 置信度判定标准（项目决策 ①）——全部满足：

1. 类型假设唯一且 phase1_prior = 1.0（位号前缀 + footprint 强约束）；
2. 值精确匹配（normalize_value 归一化后相等）；
3. 封装精确匹配（大小写归一化后相等）；
4. 引脚数一致（pin_count 相等）；
5. PassiveMatcher 命中 Level 1（PASSIVE_EXACT）或 ActiveMatcher conf=1.0。

### 10. R2 语义（项目决策）

- 仅 conf≥0.90 且值精确、封装近似的元件用 hdl_lib 符号；
- VALUE_NEAR 0.70 / VALUE_ONLY 0.80 / SIZE_ONLY 0.60 全部移 R3 mock；
- crystal（X/Y 前缀）：要求完全匹配（conf≥0.99），否则 R3（项目决策）；
- R2 41 个全 conf≥0.90（QA 复验 0 个 <0.90）。

QA 审查 4 项修复（2026-08-21）：

1. refdes 丢失：ComponentDef 未携带 DSN refdes → source_builder extra_data["dsn_refdes"] 补齐；
2. CD-RS 前缀：type_gate 漏 CD/RS → R2 误入 → 补 type_gate；
3. C63 R2：C63 0.77 误入 R2 → APPROX_THRESHOLD 0.75→0.90 后归 R3；
4. 空 target=0：无值元件 target_hdl_lib 空 → 路由判定补 NEEDS_REVIEW 排除。

---

## Part IV 当前匹配结果与指标（TG0C0D2）

### 11. 路由统计（2026-08-21 定稿）

```
R1_exact: 504    （conf≥0.99，hdl_lib 符号）
R2_approx: 41    （conf≥0.90，hdl_lib 符号 + 差异记录）
R3_mock: 284     （1:1 几何复刻，TG0C0D2_VB_lib cell；内容探测后 R3 减少至 191 cell 唯一）
R4_non_electrical: 6   （ID-BOARD，hdl_lib 无对应）
R5_gnd: 675      （原位放置）
R6_connectors: 24（6 off-page + 18 port）
```

注：R3 路由 284 指路由计数；mock 库实际 cell 191（多实例共用 cell 去重后）。

### 12. 精确转换 1500（86.26%）

- 精确转换 1500（86.26%）= R1 + GND + 连接符（504 + 675 + 321）；
- hdl 符号转换 89.02%（含 R2 conf≥0.60）；
- EXP 对拍 2253/2253=100%（R-PINNET：mismatch 0 / missing 0；pin_connections 2430）；
- EXP 辅助：EXP 辅助键名对齐（L1-L2 键名对齐/补键/修正/清理）是引脚网络 100% 的关键路径。

### 13. NEEDS_REVIEW / 低置信度分布

- conf<0.45 属源数据/库限制，非算法缺陷（132 个，Phase XII 记录）；
- R3 mock 284 个中大部分是 hdl_lib 无对应（3A BUCK/LDO/SLIC/MOSFET/LED/连接器 J/排针/磁珠 L/µH 电感）——真实工程库覆盖后自动转 R1/R2；
- L/LB 匹配 conf 仅 0.675（L1~L5）、0.495（LB）、0.386（L1708）——全部 <0.90 R2 阈值 → R3 mock（详见 §15）。

---

## Part V 非数值匹配能力与边界

### 14. 字母封装 / 型号误解析（normalize_value 破坏性）

#### 14.1 extract_pkg_size() 只认 4 位数字封装

| 输入 | 输出 | 判定 |
|---|---|---|
| HSC0402-HDTB / SC0603-TD / 0402C-S | 0402 / 0603 / 0402 | 数字封装可提取 |
| SOD323 / SMA / DO-214AC / LL-34 | 原样 / SOT（部分） | 字母封装无法归一化 |
| SOD2513X90N | 2513 | 误提取为尺寸 |

结论：字母封装（SOD323/SMA/DO-214AC/LL-34/SOT23）无法与数字封装（0402/0603）归一化比较；二极管库 ptf 的 PACKAGE_TYPE 全是字母 → 尺寸匹配必然失败。

#### 14.2 normalize_value() 只归一化数值（R/C/L 值）

| 输入 | 输出 | 判定 |
|---|---|---|
| 10UF / 100NF / 4.7K / 60UH | 10U / 100N / 4.7K / 60U | 数值（但 U/H 不区分单位） |
| DZ_L / DZ3 / LED / LB / FILTER | 原样大写 | 非数值不归一化 |
| 1N4148W-7-F | 1N | 严重误解析（把型号当数值） |
| BAS21-03W | 原样 | 保持原样 |

结论：非数值标称值（二极管型号、芯片型号、功能名）无法与 HDL 库 ptf 的 VALUE 列正确归一化比较；1N4148W-7-F 被误解析为 1N —— normalize_value 对带数字前缀的型号存在破坏性误匹配。

#### 14.3 D* 值缺失

- D1~D21（15 个二极管）值 DZ_L/DZ3/空 → DIODE/DIODE 0.38（值非数值 + 封装字母）；
- T1~T32（变压器）60UH/LC_J/200UH → PQ2016 0.35（库无对应变体）；
- LB1~LB15（磁珠）LB → FB 0.30（值无特征）；
- Z1/Z2 FILTER → 0.24（值非数值）。

### 15. L/LB 匹配遗留（hdl_lib inductor 仅 nH 级 vs 设计 µH 级）

深因（handoff-20260901 §14）：

1. hdl_lib 的 inductor ptf_rows 都是 nH 级（22NH/1.8NH/4.7NH...），而设计 L1=2.2uH（µH 级电源电感）——值域天然不匹配，走 mock 是合理的；
2. ferrite_bead 不存在于 hdl_lib（只有 fb），但 type_gate.yaml 要求 LB → ferrite_bead——候选池为空 → LB 匹配必然失败；
3. 直接测 PassiveMatcher：L1 能匹配 inductor（conf=0.7、value 2.2uH、PASSIVE_VALUE_NEAR）、LB1 能匹配 fb（conf=0.6）——但 phase1_prior_conf=0.0（type hypothesis 的 prior 是 0）导致最终 conf 低。

修复建议（未实施，不阻塞 Cadence 加载）：

```yaml
# type_gate.yaml
FB: [[fb, 1.0]]                    # 用实际存在的 fb
LB: [[fb, 0.75], [inductor, 0.50]] # fb 优先 + inductor 兜底
```

- L 元件（µH 级）若 hdl_lib inductor 只有 nH 级，保持 mock 是合理行为（或补充 µH 电感库）；
- 此修复可减少 L/LB mock 数量。

---

## Part VI 真实工程格式证据（8367/04p4/HG5015 实测）

来源：archive/temp files/phaseXI_P0_fix_evidence.md。所有片段均来自真实文件。

### 16. 电源符号 CSV 块（8367 page1.csv L360-376）

```
%"GND_POWER"
"1","(-5600,4275)","0","hdl_lib","I27";
;
CDS_LMAN_SYM_OUTLINE"-50,0,50,-50"
CDS_LIB"hdl_lib"
HDL_POWER"GND_POWER"
BODY_TYPE"PLUMBING";
"GND"2;
```

```
%"VCC_CIRCLE"
"1","(-6450,5375)","0","hdl_lib","I28";
;
HDL_POWER"VCC_12"
CDS_LIB"hdl_lib"
SIZE"1B"
BODY_TYPE"PLUMBING"
CDS_LMAN_SYM_OUTLINE"-75,75,75,-75";
"G<SIZE-1..0> \B"1;
```

要点：无 VALUE / PART_NAME / LOCATION；必含 HDL_POWER（=电源网名，无 \g）+ BODY_TYPE"PLUMBING"；VCC_CIRCLE 另有 SIZE"1B"；单引脚行 "<引脚名>"<网ID>;。

### 17. UN$ 自动网名规则

8367 实测：

```
格式: UN$<页号>$<CELL>$<I<k>>$<引脚号>
样例: UN$1$CAPACITOR$I12$1  → con 内部名 unnamed_1_capacitor_i12_1
     UN$1$DC_DC$I1$EN      → unnamed_1_dc_dc_i1_en
```

- 页号 = 物理页码；CELL = 元件符号名（大写）；I<k> = 页内实例编号（与 cpc/csv 的 I<k> 一致）；引脚号 = 连接该网的那个引脚；
- con 内部名用下划线替代 $（unnamed_<page>_<cell>_i<k>_<pin>，scope=0）；
- 电源网全局名 = 裸名小写（scope=2）+ 每页 pageN_<name>（scope=0）+ alias；
- 当前状态：con 中无数字开头网名（旧 21n109399 样式已修复）；UN$ 规则来源为 8367/04p4 实测（HG5015 用 EDIF 内部名 $47N777 样式，DSN 时代以网名直通为主）。

### 18. con / cpc 格式证据

- con：("N3" "unnamed_1_capacitor_i12_1" -1 -1 0 )（scope=0）；电源网 ("N12" "vcc_12" -1 -1 2 )（scope=2）；
- cpc：电源符号用 #ISCELL（普通元件 #CELL）；实例名 pageN_i<k> 与 csv I<k> 一致；
- CSA：FORCEADD GND_POWER..1 + FORCEPROP 3 LASTPIN (x y) SIG_NAME GND_POWER\g + FORCEPROP 1 LAST HDL_POWER GND_POWER + BODY_TYPE PLUMBING。

---

## Part VII 附录

### 19. weights.yaml 潜在缺陷

- 旧 MultiScorer 权重（weights.yaml）在新架构已废弃；保留仅作历史参考；
- 潜在缺陷：固定权重无法表达缺失维、维度相关性、类型硬门。

### 20. 信息缺口

| 缺口 | 影响 | 状态 |
|---|---|---|
| hdl_lib inductor 值域只有 nH 级 | L/LB 匹配 conf 低 → R3 mock | 待真实库接入 |
| ferrite_bead 库缺失（只有 fb） | LB 候选池空 | type_gate 待修（§15） |
| 字母封装归一化 | 二极管/变压器匹配 conf 低 | 待算法增强（低优先级） |
| normalize_value 型号误解析 | 带数字前缀型号（1N4148）误匹配 | 待算法增强（低优先级） |
| PH_ 前缀假说 | Cadence 吞 L 首字母 | 未被最终证实（零长度修复后待实测确认） |

---

*MATCHING（v4.0）完。原始文档归档于 docs/archive/（00-core-pre-consolidation/、legacy-pluginver-docs/、temp files/）。最新匹配数字以 STATUS.md §5 为准。*
