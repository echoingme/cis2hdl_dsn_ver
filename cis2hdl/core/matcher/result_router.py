"""六类匹配结果路由（第七章 §7.1/§7.2）。

把 ``MatcherPipeline.run_batch`` 的 MatchResult 列表按 §7.1 表路由为
六类计划：R1 精确（conf≥0.99）/ R2 近似（0.75≤conf<0.99）/
R3 未命中电气件（conf<0.75 → 1:1 几何复刻 mock）/ R4 非电气辅助
（test point/mark/机械——**hdl_lib 有对应元件时优先 R1/R2 命中用
hdl_lib 符号**，仅匹配失败才忽略/复刻）/ R5 GND/电源（独立放置）/
R6 网名连接符（off-page/port → 页面级 IOPORT/网标）。

路由判定信息（R4/R5 标记、refdes）从**配对源 ComponentDef**读取
（``source_builder.to_component_def`` 已写入 ``extra_data``；
run_batch 按输入顺序产出结果，zip 配对安全），置信度从 MatchResult
读取——两者解耦，不依赖 matcher 内部实现。

高内聚：路由判定唯一收敛；低耦合：只读 ``MatchResult``/源 ComponentDef/
``DesignIR``；输出 ``RoutedPlan`` 数据类供下游消费。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

from cis2hdl.core.ir.component import ComponentDef
from cis2hdl.core.ir.design import DesignIR
from cis2hdl.core.ir.match import MatchResult

logger = logging.getLogger(__name__)

#: R1 精确命中阈值（用户裁决 ①：值+封装+引脚数全等 → final_conf≥0.99）。
EXACT_THRESHOLD = 0.99
#: R2 近似命中下限（用户裁决 2026-08-21）：conf>=0.90 才留 R2，
#: 其余（VALUE_NEAR 0.70/VALUE_ONLY 0.80 等）移 R3 生成模拟图标。
APPROX_THRESHOLD = 0.90


@dataclass
class RoutedPlan:
    """六类路由结果计划（数据类，下游按类消费）。"""

    exact: list[tuple[ComponentDef, MatchResult]] = field(default_factory=list)      # R1
    approx: list[tuple[ComponentDef, MatchResult]] = field(default_factory=list)     # R2
    unmatched: list[tuple[ComponentDef, MatchResult]] = field(default_factory=list)  # R3（电气件 mock）
    non_electrical: list[tuple[ComponentDef, MatchResult]] = field(default_factory=list)  # R4
    gnd: list[tuple[ComponentDef, MatchResult]] = field(default_factory=list)        # R5
    off_pages: list[dict] = field(default_factory=list)         # R6 连接符
    ports: list[dict] = field(default_factory=list)             # R6 IOPORT

    @property
    def counts(self) -> dict[str, int]:
        return {
            "R1_exact": len(self.exact),
            "R2_approx": len(self.approx),
            "R3_unmatched": len(self.unmatched),
            "R4_non_electrical": len(self.non_electrical),
            "R5_gnd": len(self.gnd),
            "R6_off_pages": len(self.off_pages),
            "R6_ports": len(self.ports),
        }


def _is_gnd_source(src: ComponentDef) -> bool:
    """R5 判定：GND/电源实例（route_class 标记或库名启发式）。"""
    if src.extra_data.get("route_class") == "R5":
        return True
    ref = (src.extra_data.get("refdes", "") or src.refdes or "").upper()
    lib = (src.library_id or "").upper()
    if ref.startswith("GND_"):
        return True
    return "GND_SYMBOL" in lib or ("GND" in lib and "SYMBOL" in lib)


#: 已命中 hdl_lib 符号的 passive 近似策略（value 精确但 size 未知/近似）。
#: 这些结果 target_library_id 非空，应归 R2（hdl_lib 符号 + 差异记录），
#: 不降级 mock（用户裁决：有 hdl_lib 对应不生成模拟图标）。
_VALUE_EXACT_STRATEGIES = (
    "PASSIVE_VALUE_ONLY",
    "PASSIVE_VALUE_NEAR",
    "PASSIVE_EXACT_MULTI",
)


def _is_crystal(src, result: MatchResult) -> bool:
    """crystal 系列判定（用户裁决：要求完全匹配）。"""
    ref = ((src.extra_data.get("refdes") if src else "") or "").upper()
    tgt = (result.target_library_id or "").lower()
    return (ref.startswith(("X", "Y")) or ref.startswith("CRY"))
    #: hdl 符号近似下限（用户裁决 2026-08-21）：R2 仅保留 conf>=0.90 且
#: 标称值精确、仅封装有微小区别的元件（VALUE_ONLY 0.80/VALUE_NEAR
#: 0.70/SIZE_ONLY 0.60 全部 <0.90 → 移 R3 生成模拟图标，不套用近似
#: hdl_lib 元件）。
_HDL_APPROX_MIN_CONF = 0.90


def _has_hdl_symbol(result: MatchResult, conf: float) -> bool:
    """判定：结果已命中 hdl_lib 符号且置信度达标（QA 风险 1 修复）。

    仅当 target 非空、conf≥0.90 且策略非 prefix-only 兜底时才归 R2
    （hdl_lib 符号 + 差异记录）；NEEDS_REVIEW/PREFIX_ONLY 回 R3 mock。

    用户裁决（2026-08-21）：crystal 系列（refdes X/Y 前缀）要求**完全
    匹配**（conf≥0.99，footprint+数值都精确）才用 hdl_lib，否则 R3。
    """
    if not result.target_library_id or conf < _HDL_APPROX_MIN_CONF:
        return False
    strat = str(result.strategy)
    if strat.startswith("PASSIVE_PREFIX"):
        return False
    if strat.startswith("NEEDS_REVIEW"):
        return False
    return True


def _pin_overlap_too_low(
    src, result: MatchResult, hdl_db=None
) -> bool:
    """2026-08-21：大芯片近似匹配的引脚重合度保护。

    近似匹配（R2，conf≥0.90）若命中引脚名完全不同的 hdl_lib 元件
    （实测：U7 Hi5662YV305_DDR_GE_0D → VC_VN1110_A1、2P5GEPHY_0 →
    RTL8201F），网络名标签将无法映射（css 键不匹配）且符号形状错误
    ——应降级 R3 复刻 DSN 原始符号（cache_pin_defs 有完整引脚线）。

    规则：源引脚数 ≥8（大芯片）且源引脚名与 target 引脚名重合度
    <30% → True（拦截 R2）。小件（R/C/L 等 ≤2 引脚）不检查。
    """
    if hdl_db is None or src is None or not result.target_library_id:
        return False
    src_pins = {
        str(k).upper()
        for k in (src.extra_data.get("pin_connections") or {}).keys()
    }
    if len(src_pins) < 8:
        return False
    try:
        tgt = hdl_db.get_by_library_id(result.target_library_id)
    except Exception:
        tgt = None
    if tgt is None or not tgt.pins:
        return False
    tgt_pins = {
        str(p.name).upper() or str(p.number).upper()
        for p in tgt.pins
        if (p.name or p.number)
    }
    if not tgt_pins:
        return False
    inter = len(src_pins & tgt_pins)
    return inter / max(len(src_pins), 1) < 0.3


def route(
    results: Iterable[MatchResult],
    sources: Optional[list[ComponentDef]] = None,
    ir: Optional[DesignIR] = None,
    hdl_db=None,
) -> RoutedPlan:
    """路由判定（§7.2 伪代码实现）。

    Args:
        results: run_batch 输出（每源实例一个 MatchResult，与 sources 同序）。
        sources: 匹配源 ComponentDef 列表（source_builder 输出；提供时
            R4/R5 判定用源标记，否则退化用库名启发式）。
        ir: DesignIR（可选；提供时补充 R6 页面级连接符/端口）。
        hdl_db: 2026-08-21 新增——hdl_lib 扫描库（大芯片近似匹配的
            引脚重合度保护用，见 ``_pin_overlap_too_low``）。

    Returns:
        RoutedPlan（六类）。
    """
    plan = RoutedPlan()
    src_iter = iter(sources) if sources else iter(())

    for result in results:
        src = next(src_iter, None)
        conf = result.confidence if result.confidence is not None else 0.0
        # R5：GND/电源实例
        if src is not None and _is_gnd_source(src):
            plan.gnd.append((src, result))
            continue
        # R4 调整（v2 用户裁决）：mark/testpoint/hole 等非电气件在
        # hdl_lib 有对应元件（hole/mark 实测存在）——**匹配成功优先用
        # hdl_lib 符号**（R1/R2 继承），仅匹配失败才进 R4（忽略/复刻）。
        if src is not None and src.extra_data.get("is_non_electrical"):
            if conf >= APPROX_THRESHOLD:
                if conf >= EXACT_THRESHOLD:
                    plan.exact.append((src, result))    # R1 hdl_lib 符号
                else:
                    plan.approx.append((src, result))   # R2 hdl_lib 符号
            else:
                plan.non_electrical.append((src, result))  # R4
            continue
        _crystal_not_exact = (
            _is_crystal(src, result) and conf < EXACT_THRESHOLD
        )
        # 2026-08-21：大芯片近似匹配引脚重合度保护（U7→VC_VN1110_A1
        # 等错误近似 → R3 mock 复刻 DSN 符号）。
        _pin_mismatch = _pin_overlap_too_low(src, result, hdl_db)
        if conf >= EXACT_THRESHOLD and not _crystal_not_exact:
            plan.exact.append((src, result))            # R1
        elif (conf >= APPROX_THRESHOLD or _has_hdl_symbol(result, conf)) \
                and not _crystal_not_exact and not _pin_mismatch:
            # R2（用户裁决）：conf≥0.90 且值精确、封装近似的元件用
            # hdl_lib 符号 + 差异记录；其余（<0.90）移 R3 mock。
            # crystal 系列要求完全匹配（X/Y 前缀 conf<0.99 → R3）。
            plan.approx.append((src, result))
        else:
            plan.unmatched.append((src, result))        # R3

    # R6：网名连接符（off-page 引用 + 页面端口）——直接来自 PageIR。
    if ir is not None:
        for page in ir.pages:
            plan.off_pages.extend(page.off_pages or [])
            plan.ports.extend(page.ports or [])

    logger.info("路由完成: %s", plan.counts)
    return plan
