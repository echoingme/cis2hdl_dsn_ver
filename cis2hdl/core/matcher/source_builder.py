"""匹配源构建（A 类）：IR 实例 → 匹配器可消费的 ComponentDef 列表。

现有 ``MatcherPipeline.run_batch(sources: list[ComponentDef], db)`` 的
source 侧消费字段：``refdes``/``value``/``library_id``/``pin_count``/
``footprint``/``extra_data.pst_*``（可选）。本模块把 DesignIR 实例转成
等价 ComponentDef（DSN 为唯一信息源，PST 特征切换），并保留实例上下文
到 ``extra_data``（loc/rotation/mirror/pin_connections/category/NC 等），
供匹配后路由与几何复刻消费。

高内聚：实例→源特征的映射逻辑唯一收敛于此；低耦合：只依赖
``ir_access``/``ComponentDef``，不触碰 matcher 内部实现。
"""

from __future__ import annotations

import logging
from typing import Optional

from cis2hdl.core.ir.component import ComponentDef, ComponentInstanceIR
from cis2hdl.core.ir.design import DesignIR
from cis2hdl.core.intermediate import ir_access

logger = logging.getLogger(__name__)

#: 非电气/辅助元件启发式标记（R4 路由判定：test point / mark / 机械件）。
NON_ELECTRICAL_MARKS = (
    "TP", "TEST", "MARK", "HOLE", "MOUNT", "ID-BOARD", "SCREW",
    "FIDUCIAL", "MECH",
)


def _is_non_electrical(inst: ComponentInstanceIR) -> bool:
    """R4 判定：非电气/辅助元件（test point / mark / 机械符号）。"""
    ref = (inst.refdes or "").upper()
    lib = (inst.library_id or "").upper()
    if any(m in ref for m in NON_ELECTRICAL_MARKS):
        return True
    if any(m in lib for m in NON_ELECTRICAL_MARKS):
        return True
    return False


def to_component_def(
    inst: ComponentInstanceIR, comp: Optional[ComponentDef]
) -> ComponentDef:
    """实例 + 库元件 → 匹配源 ComponentDef（PST 特征切换）。

    - ``refdes``/``library_id``/``part_name``：实例身份（part_name 优先
      库元件名，缺省回退 library_id）；
    - ``value``：``value_override`` 优先，库 value 兜底；
    - ``footprint``/``pin_count``/``category``/``user_attrs``：库元件为
      底，实例属性覆盖（DSN 真值优先）；
    - ``extra_data``：实例上下文（refdes/loc/rotation/mirror/page_id/
      section/pin_connections/pin_labels/nc_pins/category/mfg_region/
      is_non_electrical），供 result_router 与下游几何复刻消费。
    """
    src = ComponentDef(
        library_id=inst.library_id,
        part_name=(comp.part_name if comp else inst.library_id),
        refdes=inst.refdes,
        value=inst.value_override or (comp.value if comp else ""),
        footprint=inst.properties.get("PCB Footprint")
        or (comp.footprint if comp else ""),
        pin_count=len(inst.pin_connections)
        or (comp.pin_count if comp else 0),
        category=(comp.category if comp else "")
        or inst.extra_data.get("category", ""),
        user_attrs=dict(inst.user_attrs or {}),
    )
    src.extra_data = {
        "refdes": inst.refdes,
        "loc_x": inst.loc_x,
        "loc_y": inst.loc_y,
        "rotation": inst.rotation,
        "mirror": inst.mirror,
        "section": inst.section,
        "page_id": inst.extra_data.get("page_id", ""),
        "pin_connections": dict(inst.pin_connections or {}),
        "pin_labels": dict(inst.pin_labels or {}),
        "nc_pins": set(inst.nc_pins or set()),
        "category": src.category,
        "mfg_region": inst.extra_data.get("mfg_region", ""),
        "is_non_electrical": _is_non_electrical(inst),
        # 非电气件直查提示（R4 用户裁决）：库名基础名（HOLE/ID-BOARD…）
        # 直接映射 hdl_lib 同名元件（HOLE/TEST_MARK 实测存在），
        # 不强制生成模拟图标。ID-BOARD 等 hdl_lib 缺失时仍走 R4。
        "direct_hdl": (inst.library_id or "").rsplit(".", 1)[0]
        if _is_non_electrical(inst) else "",
        "symbol_geometry": (comp.symbol_geometry if comp else {}),
        "symbol_texts": list(comp.symbol_texts if comp else []),
        # 2026-08-21：U7 六段独立芯片——页面流引脚对象偏移
        # （mock 全量引脚 C 命令 / LASTPIN 标签坐标来源）
        "pin_object_offsets": dict(
            inst.extra_data.get("pin_object_offsets") or {}
        ),
    }
    return src


def build_source_components(ir: DesignIR) -> list[ComponentDef]:
    """DesignIR → 全部实例的匹配源 ComponentDef 列表。

    跳过 R5 GND 实例（GND 走独立放置路径，不进 run_batch）——由
    result_router 单独路由。非电气件保留（路由时判 R4）。
    """
    sources: list[ComponentDef] = []
    for page in ir.pages:
        for inst in page.instances:
            comp = ir_access.component_of(ir, inst)
            src = to_component_def(inst, comp)
            # 实例上下文补充页面号（ir_access 不落实例，这里补记）
            src.extra_data["page_id"] = src.extra_data.get("page_id") or page.page_id
            if _is_gnd(inst):
                src.extra_data["route_class"] = "R5"
            sources.append(src)
    return sources


def _is_gnd(inst: ComponentInstanceIR) -> bool:
    """R5 判定：GND/电源实例（refdes 前缀或库名 GND_SYMBOL）。"""
    if (inst.refdes or "").startswith("GND_"):
        return True
    lib = (inst.library_id or "").upper()
    return "GND_SYMBOL" in lib or "GND" in lib and "SYMBOL" in lib
