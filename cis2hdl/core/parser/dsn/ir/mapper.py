"""IR 映射层（架构设计 §2 L5 + §4 映射表）。

将 DSN 解析产物（InstanceBind / 网表 / 走线 / 图形文本 / 引脚分区 /
连接关系）映射为 Pydantic ``DesignIR / PageIR / NetIR / ComponentInstanceIR``。

映射规则（架构 §4.2）：
- ``refdes`` → ``ComponentInstanceIR.refdes``（多单元合并后唯一）
- ``shape`` → ``ComponentInstanceIR.library_id``
- ``value`` → ``ComponentInstanceIR.value_override`` + ``properties["Value"]``
- ``footprint`` → ``ComponentInstanceIR.properties["PCB Footprint"]``（实例级）
- ``x/y`` → ``loc_x / loc_y``
- 引脚网络连接（GND/0x01 帧 + net table）→ ``pin_connections`` + ``NetIR.connections``
- 走线 → ``PageIR.wires``
- P0-1 0x0867 引脚分区 → ``PageIR.pin_partitions``
- P0-2 0x0000 文字/图形 → ``PageIR.text_elements`` / ``PageIR.graphic_elements``
- P0-3 标号映射 → ``ComponentInstanceIR.pin_labels``
- P0-4 折线 → ``WireSegment.points``（points>2 折线链）
- P0-5 连接关系 → ``NetIR.connections`` / ``pin_connections``（坐标级）
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ....ir.component import ComponentDef, ComponentInstanceIR, ElectricalType, PinDef
from ....ir.design import DesignIR, NetCategory, NetConnection, NetIR, PageIR, WireSegment

if TYPE_CHECKING:
    from ....db.component_db import ComponentDB
    from ...bind.binder import InstanceBind
    from ...cache.cache_parser import CacheData, LibraryPart
    from ...decode.info_frames import InfoElement
    from ...decode.library_attrs import ComponentUserAttr
    from ...decode.net_table import NetTable
    from ...decode.page_text_frames import PageTextElement
    from ...decode.pin_table_frames import PinPartitionTable
    from ...decode.wire_frames import WireRecord
    from ...decode.wire_topology import ConnectivityResult, WirePolyline

logger = logging.getLogger(__name__)

#: P1-11：用户属性键（与 decode/library_attrs 的 _ATTR_NAMES 对齐）
_USER_ATTR_KEYS = (
    "COMPONENT_HIGH_MAX",
    "额定工作电压(V)",
    "额定功率(W)",
    "阻值允许偏差",
    "描述",
)


# ---------------------------------------------------------------------------
#  Net category 辅助
# ---------------------------------------------------------------------------

def _classify(name: str) -> NetCategory:
    """按网名粗分类（GND / 电源 / BUS / FLAT）。"""
    up = name.upper()
    if up in ("GND", "AGND", "DGND", "PGND", "GNDA", "GNDD"):
        return NetCategory.GROUND
    if up.startswith(("VCC", "VDD", "VSS", "AVCC", "AVDD", "VBAT", "PWR", "3V3", "5V", "12V")):
        return NetCategory.POWER
    if "[" in name or "]" in name or ":" in name or "<" in name or ">" in name:
        return NetCategory.BUS
    return NetCategory.FLAT


# ---------------------------------------------------------------------------
#  ComponentDef 构建（Cache LibraryPart + Packages pinMap）
# ---------------------------------------------------------------------------

def _normalize_user_attr(value: str) -> str | None:
    """归一化用户属性值（噪声值不进入 IR）。

    库默认属性常携带哨兵值（``9999998mm``/``-99999999MM``/``-``），
    它们不代表真实器件规格，丢弃避免污染 ComponentDef.user_attrs。

    Args:
        value: 原始属性值。

    Returns:
        清洗后的值；噪声返回 None。
    """
    if not value:
        return None
    v = value.strip()
    if v in ("-", "--", "NExist", "none", "None"):
        return None
    if "9999998" in v or "99999999" in v:
        return None
    return v


def _normalize_user_attr_for(key: str, value: str) -> str | None:
    """按属性键归一化（P2-C：「描述」键 '-' 哨兵保留）。

    描述列 EXP 黄金标准 229/743 为 ``-``（非 <null>）——保留 ``-``
    与黄金标准**一致**（不冲突），使 user_attrs 描述键 5 键齐全。
    其余键走默认哨兵过滤。

    Args:
        key: 属性名（如 ``描述``）。
        value: 原始属性值。

    Returns:
        清洗后的值；噪声返回 None。
    """
    if key == "描述":
        if value is None:
            return None
        v = value.strip()
        if v in ("", "<null>", "NExist", "None", "none"):
            return None
        return v
    return _normalize_user_attr(value)


def build_component_db(
    cache: CacheData | None,
    packages: list | None,
    component_db: "ComponentDB | None" = None,
    library_attrs: list | None = None,
    pages_data: list[tuple] | None = None,
) -> "ComponentDB":
    """从 Cache LibraryPart + Packages pinMap 构建 ComponentDB。

    Args:
        cache: Cache 解析结果（44 个 LibraryPart）。
        packages: Packages 解析结果（11 包）。
        component_db: 已有 ComponentDB（可选，缺省新建）。
        library_attrs: Library 流 INS 属性记录（P1-11，可选）。
        pages_data: 页面数据元组（P1-11 实例属性聚合，可选）。

    Returns:
        填充后的 ComponentDB。
    """
    from ....db.component_db import ComponentDB

    db = component_db if component_db is not None else ComponentDB()

    # 包名 → pinMap（多单元前缀匹配）
    pin_map_by_pkg: dict[str, list[str]] = {}
    pkg_by_base: dict[str, list] = {}
    for pkg in packages or []:
        if pkg.name and pkg.pin_map:
            pin_map_by_pkg[pkg.name] = pkg.pin_map
            base = pkg.name.rsplit("_", 1)[0]
            pkg_by_base.setdefault(base, []).append(pkg)

    for part in (cache.parts if cache else []):
        base = part.name.rsplit(".", 1)[0]
        pins: list[PinDef] = []
        pin_map = pin_map_by_pkg.get(base) or pin_map_by_pkg.get(part.name)
        if pin_map is None:
            # 多单元：取基础名前缀匹配
            for base_key, pkgs in pkg_by_base.items():
                if base.startswith(base_key):
                    pin_map = pkgs[0].pin_map
                    break
        # P1-1：Cache type-4 几何 → 引脚名 → portType（按几何 pin_name 序回填）
        geometry_by_name: dict[str, int] = {}
        if cache is not None:
            for geom in cache.pin_geometries:
                if getattr(geom, "pin_name", None):
                    geometry_by_name.setdefault(geom.pin_name, geom.port_type)
        for i, num in enumerate(pin_map or []):
            # P1-2：引脚名优先取 Packages pin_names；portType 映射电气类型。
            # pkg 解析与上方 pin_map 同规则（基础名/前缀匹配），修复
            # ``2P5GEPHY_0`` 等带后缀 base 取不到 pkg 导致引脚名为空的缺陷。
            pkgs_for_base = pkg_by_base.get(base)
            if not pkgs_for_base:
                for base_key, pkgs_ in pkg_by_base.items():
                    if base.startswith(base_key):
                        pkgs_for_base = pkgs_
                        break
            pkg = pkgs_for_base[0] if pkgs_for_base else None
            pin_name = ""
            if pkg is not None and i < len(pkg.pin_names):
                pin_name = pkg.pin_names[i]
            # P1-2：portType → ElectricalType（按几何引脚名匹配；未知 PASSIVE）
            electrical = ElectricalType.PASSIVE
            if pin_name in geometry_by_name:
                from ..decode.symbol_pin_frames import port_type_to_electrical

                mapped = port_type_to_electrical(geometry_by_name[pin_name])
                electrical = ElectricalType(mapped)
            else:
                # P2-D 回退链：Cache 几何缺失时按引脚名语义推断
                # （电源/接地引脚 → POWER/GROUND；其余保持 PASSIVE 诚实标注）。
                from ..decode.symbol_pin_frames import infer_pin_type_from_name

                inferred = infer_pin_type_from_name(pin_name)
                if inferred is not None:
                    electrical = ElectricalType(inferred)
            pins.append(PinDef(
                number=num, name=pin_name, type=electrical,
            ))

        # T04：Cache 命名引脚定义回填（缺口 P2-B/P2-D 根因）
        # Packages 流仅 11 个大符号 pinMap；3A BUCK/LDO_1/PSW6M2/SOP8 等
        # 小 IC 的引脚名/几何在 Cache 流 [DELIM][len][name][00][坐标] 帧族
        # （scan_cache_pin_defs，914+）。按符号名精确归属，补 pin 名与几何。
        cache_pin_geom: list[tuple] | None = None
        if cache is not None and getattr(cache, "pin_defs", None):
            base_pin_defs = [
                p for p in cache.pin_defs
                if getattr(p, "symbol", "") == base
            ]
            if base_pin_defs:
                # 无 Packages pin_map（小 IC）→ 用 Cache 引脚定义构建完整 PinDef
                if not pins:
                    from ..decode.symbol_pin_frames import (
                        infer_pin_type_from_name,
                    )

                    for i, pdef in enumerate(base_pin_defs):
                        inferred = infer_pin_type_from_name(pdef.name)
                        electrical = ElectricalType.PASSIVE
                        if inferred is not None:
                            electrical = ElectricalType(inferred)
                        pins.append(PinDef(
                            number=str(i + 1), name=pdef.name, type=electrical,
                        ))
                    cache_pin_geom = [
                        (pdef.name, pdef.x1, pdef.y1, pdef.x2, pdef.y2)
                        for pdef in base_pin_defs
                    ]
                # 有 Packages pin_map 但名缺失 → 仅回填名（按几何顺序补）
                elif any(not p.name for p in pins):
                    names = [p.name for p in base_pin_defs]
                    for i, pd_ in enumerate(pins):
                        if not pd_.name and i < len(names):
                            pins[i] = PinDef(
                                number=pd_.number,
                                name=names[i],
                                type=pd_.type,
                            )
                # 2026-08-21 修复：有完整 Packages pin_map 的符号
                # （LB/LB_0 磁珠等）前两个分支都不走 → cache_pin_geom
                # 保持 None → symbol_geometry.pin_lines 空 → mock css
                # 无 C 命令 → 引脚网络名标签无法发射。统一补几何。
                if cache_pin_geom is None:
                    cache_pin_geom = [
                        (pdef.name, pdef.x1, pdef.y1, pdef.x2, pdef.y2)
                        for pdef in base_pin_defs
                    ]

        comp = ComponentDef(
            library_id=part.name,
            part_name=part.name,
            value=part.part_value or "",
            footprint="",
            pins=pins,
            source_format="CIS_DSN",
        )
        comp.extra_data["refDes_prefix"] = part.ref_des or ""
        comp.extra_data["implementation_path"] = part.implementation_path or ""
        comp.extra_data["implementation"] = part.implementation or ""
        if cache_pin_geom is not None:
            comp.extra_data["pin_geometry_from_cache"] = cache_pin_geom
        # P1-12：Cache 符号文本（只挂**该符号自己**的文本；按 part 名精确匹配）
        if cache is not None and getattr(cache, "symbol_texts", None):
            comp.symbol_texts = [
                t.to_dict() if hasattr(t, "to_dict") else dict(t)
                for t in cache.symbol_texts
                if getattr(t, "symbol", "") == base
            ]
            # R-GEO：符号几何结构化聚合（0x2929 体框 + 0x2c2c 引脚线 +
            # 0x2e2e 文本/头属性 → ComponentDef.symbol_geometry，mock 1:1 输入）
            own = [t for t in cache.symbol_texts if getattr(t, "symbol", "") == base]
            body: list[tuple[int, int, int, int]] = []
            pins: list[tuple[int, int, int, int]] = []
            texts: list[dict] = []
            headers: list[dict] = []
            for t in own:
                d = t.to_dict() if hasattr(t, "to_dict") else dict(t)
                kind = d.get("kind", "")
                if kind == "body":
                    body.append((d.get("x", 0), d.get("y", 0),
                                 d.get("x2", 0), d.get("y2", 0)))
                elif kind == "pin":
                    pins.append((d.get("x", 0), d.get("y", 0),
                                 d.get("x2", 0), d.get("y2", 0)))
                elif kind == "header":
                    headers.append(d)
                elif d.get("text"):
                    texts.append(d)
            geo: dict = {
                "body_box": None,
                "pin_lines": [],
                "texts": texts,
                "header_attrs": headers,
                "geometry_source": "cache",
            }
            if body:
                xs = [p[0] for p in body] + [p[2] for p in body]
                ys = [p[1] for p in body] + [p[3] for p in body]
                geo["body_box"] = [min(xs), min(ys), max(xs), max(ys)]
            # 2026-08-21 修复：symbol_texts 的 kind=='pin' 对多数符号
            # 不全（R/C/L 为空、U7 六段仅 3/段）——其引脚线在
            # cache_pin_defs 命名引脚帧（0E 段 37 个 = 37 引脚全量）。
            # 用 T04 回填的 cache_pin_geom **覆盖** pin_lines（不足时），
            # 否则 mock symbol.css 无完整 C 命令（引脚线）→ 引脚网络名
            # 标签 LASTPIN SIG_NAME 无法发射（235/290 mock 缺失）。
            if len(pins) < len(cache_pin_geom) and cache_pin_geom:
                pins = [(x1, y1, x2, y2, _n)
                        for (_n, x1, y1, x2, y2) in cache_pin_geom]
            for _p in pins:
                if len(_p) >= 5 and _p[4]:
                    # 2026-08-21：第 5 元素带 cache 引脚名（VDD_COMMON1
                    # 等）→ mock css C 命令文本=引脚名 → LASTPIN 键
                    # 匹配（pc 键==引脚名）→ 标签发射 + 引脚号显示
                    geo["pin_lines"].append([_p[0], _p[1], _p[2], _p[3],
                                             str(_p[4])])
                else:
                    geo["pin_lines"].append(
                        [_p[0], _p[1], _p[2], _p[3], ""])
            comp.symbol_geometry = geo
        elif cache_pin_geom:
            # 2026-08-21 修复：无 symbol_texts 但有 cache_pin_geom 的
            # 符号（LB/LB_0 磁珠等）——geo 块被 symbol_texts 条件跳过，
            # mock 无 C 命令（引脚线）→ 引脚网络名标签无法发射。
            comp.symbol_geometry = {
                "body_box": None,
                "pin_lines": [
                    [x1, y1, x2, y2, ""]
                    for (_n, x1, y1, x2, y2) in cache_pin_geom
                ],
                "texts": [],
                "header_attrs": [],
                "geometry_source": "cache",
            }
        # P1-11：用户属性回填（INS 行字段 → 按 OLB 基础名关联；兜底
        # 实例属性聚合见 ``_backfill_user_attrs``）。此处先挂 INS 行直关联。
        db.add(comp)
    # P1-11：跨源用户属性聚合（INS 行字段 + 实例属性众数）→ ComponentDef.user_attrs
    _backfill_user_attrs(db, cache, pages_data, library_attrs)
    # T04 P2-6：PKG_TYPE 等类型字段 → ComponentDef.extra_data（实例属性众数）
    _backfill_type_fields(db, pages_data)
    logger.info("ComponentDB built: %d components", len(db.list_all()))
    return db


#: T04 P2-6 类型字段（strLst 名称；实例属性众数聚合）
_TYPE_FIELD_NAMES = ("PKG_TYPE", "PACKAGE_TYPE", "POWER_TYPE")


def _backfill_type_fields(
    db: "ComponentDB", pages_data: list[tuple],
) -> None:
    """按库元件聚合类型字段（PKG_TYPE/PACKAGE_TYPE/POWER_TYPE）到
    ComponentDef.extra_data（T04 P2-6；实例 0x02 属性帧众数）。

    Args:
        db: ComponentDB（原地更新）。
        pages_data: 页面数据（绑定含 properties）。
    """
    from collections import defaultdict, Counter as _Counter

    by_lib: dict[str, dict[str, _Counter]] = defaultdict(lambda: defaultdict(_Counter))
    for page in pages_data:
        for bind in _page_data_item(page, 1, []):
            lib = getattr(bind, "shape", None) or ""
            if not lib or lib == "GND_SYMBOL":
                continue
            for k, v in getattr(bind, "properties", {}).items():
                if k not in _TYPE_FIELD_NAMES:
                    continue
                if not v or v == "-":
                    continue
                by_lib[lib][k][v] += 1
    for comp in db.list_all():
        lib = comp.library_id
        base = lib.rsplit(".", 1)[0]
        for key, counter in by_lib.get(lib, {}).items():
            if counter:
                comp.extra_data[key] = counter.most_common(1)[0][0]
        if not comp.extra_data.get("PKG_TYPE") and base:
            # 多单元：按基础名前缀匹配
            for lib2, fields in by_lib.items():
                if lib2.rsplit(".", 1)[0] == base or base.startswith(lib2.rsplit(".", 1)[0]):
                    for key, counter in fields.items():
                        if counter and key == "PKG_TYPE" and "PKG_TYPE" not in comp.extra_data:
                            comp.extra_data[key] = counter.most_common(1)[0][0]


def _backfill_user_attrs(
    db: "ComponentDB",
    cache: "CacheData | None",
    pages_data: list[tuple] | None = None,
    library_attrs: list | None = None,
) -> None:
    """聚合用户属性到 ComponentDef.user_attrs（P1-11 接线）。

    数据源（按优先级）：
    1. **Library 流 INS 行字段**（``ComponentUserAttr.row_fields``）：
       ``[footprint][attr名,值...]``——仅采用**行内**出现的属性名+值
       （避免流扫描把无关 INS 的『描述』误挂到全库），按两种键关联：
       a) 行内含 OLB 路径 → OLB 基础名（如 ``HW_RESISTOR.OLB``）；
       b) 行首 footprint（如 ``SC0201A``）→ 实例 PCB Footprint 反查。
    2. **实例 0x02 属性帧**（``InstanceBind.properties``）——按实例
       library_id 聚合：逐键取**众数**（排除哨兵值），兜底库级默认。

    Args:
        db: 已构建的 ComponentDB。
        cache: Cache 解析结果（可选）。
        pages_data: 页面数据元组（含绑定列表；可选）。
        library_attrs: Library 流 INS 属性记录（可选）。
    """
    comps = db.list_all()
    if not comps:
        return
    cache_olb = _cache_olb_map(cache)
    footprint_libs = _footprint_lib_map(pages_data)
    ins_records = _collect_ins_records(library_attrs)
    inst_mode = _instance_attr_modes(pages_data)

    for comp in comps:
        merged: dict[str, str] = {}
        _merge_ins_records(merged, comp, ins_records, cache_olb, footprint_libs)
        _merge_instance_modes(merged, comp, inst_mode)
        if merged:
            comp.user_attrs = dict(merged)


def _cache_olb_map(cache: "CacheData | None") -> dict[str, str]:
    """Cache 每 part 的 source_library OLB 基础名 → part 基础名。"""
    result: dict[str, str] = {}
    for part in (cache.parts if cache else []):
        src = (part.source_library or "").rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        base = part.name.rsplit(".", 1)[0]
        if src and base:
            result[base] = src.upper()
    return result


def _footprint_lib_map(pages_data: list[tuple] | None) -> dict[str, set[str]]:
    """实例 footprint → 使用该 footprint 的 library_id 集合（INS 行关联）。"""
    result: dict[str, set[str]] = {}
    if not pages_data:
        return result
    for page in pages_data:
        for bind in _page_data_item(page, 1, []):
            lib = getattr(bind, "shape", None) or ""
            if not lib or lib == "GND_SYMBOL":
                continue
            fp = (getattr(bind, "properties", {}) or {}).get("PCB Footprint", "")
            if fp:
                result.setdefault(fp, set()).add(lib)
    return result


def _collect_ins_records(library_attrs: list | None) -> list[dict]:
    """INS 行字段 → 属性记录（仅行内属性名+值；OLB/footprint 关联键）。

    P2-C：INS description 字段（LibraryAttrDecoder 已解出，如
    INS5 → 'MDI_BP_1'）直接回填「描述」键——描述属性名可能不在
    row_fields 内（实测 INS5 row_fields 无 '描述'）。
    """
    records: list[dict] = []
    for ins in library_attrs or []:
        row = getattr(ins, "row_fields", []) or []
        attrs = _extract_row_user_attrs(row)
        desc = getattr(ins, "description", None)
        if desc and "描述" not in attrs:
            attrs["描述"] = desc
        category = getattr(ins, "category", None)
        # 注：INS 行分类名链式混排不可靠（多元件字段共享），不注入
        # CATEGORY_NAME（R-CAT 由 EXP 辅助 L4 回填，见 exp_dir_assist）
        mfg = getattr(ins, "mfg_region", None)
        if mfg and "MFGREGION" not in attrs:
            attrs["MFGREGION"] = mfg
        if not attrs:
            continue
        olb_hit = None
        for f in row:
            up = str(f).upper()
            if up.endswith(".OLB") or up.endswith(".DSN"):
                olb_hit = up.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
                break
        footprint = _find_row_footprint(row)
        if olb_hit is None and footprint is None:
            continue
        records.append({
            "olb": olb_hit,
            "footprint": footprint,
            "attrs": attrs,
        })
    return records


def _instance_attr_modes(pages_data: list[tuple] | None) -> dict[str, dict[str, str]]:
    """实例 0x02 属性帧 → 按 library_id 逐键众数（排除哨兵值）。"""
    if not pages_data:
        return {}
    from collections import defaultdict

    seen: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(lambda: defaultdict(int))
    )
    for page in pages_data:
        for bind in _page_data_item(page, 1, []):
            lib = getattr(bind, "shape", None) or ""
            if not lib or lib == "GND_SYMBOL":
                continue
            for k, v in getattr(bind, "properties", {}).items():
                if k not in _USER_ATTR_KEYS:
                    continue
                # 组件级（库级）用户属性：描述 '-' 哨兵仍丢弃（QA P1
                # user_attrs 组件数口径 35/44 保持）；实例级见 build_page_ir
                clean = _normalize_user_attr(v)
                if clean is None:
                    continue
                seen[lib][k][clean] += 1
    result: dict[str, dict[str, str]] = {}
    for lib, keys in seen.items():
        mode: dict[str, str] = {}
        for k, cnt in keys.items():
            best_val, best_n = "", 0
            for val, n in cnt.items():
                if n > best_n:
                    best_val, best_n = val, n
            if best_val:
                mode[k] = best_val
        if mode:
            result[lib] = mode
    return result


def _merge_ins_records(
    merged: dict[str, str],
    comp: ComponentDef,
    ins_records: list[dict],
    cache_olb: dict[str, str],
    footprint_libs: dict[str, set[str]],
) -> None:
    """把 INS 行属性按 OLB / footprint 关联合并进 comp.user_attrs。

    匹配优先级（2026-08-21 修复：新版 DSN 全部组件 source_library=
    WORKLIB.OLB，OLB 宽匹配曾把 INS5 描述污染到 7 个无关组件）：
    1. footprint 精确匹配（实例 PCB Footprint 反查，唯一性强）；
    2. OLB 匹配仅作为**无 footprint 行**的兜底。
    """
    base = comp.part_name.rsplit(".", 1)[0]
    olb = cache_olb.get(base, "")
    for rec in ins_records:
        fp = rec["footprint"]
        if fp is not None:
            # 有 footprint 的行：精确匹配（不参与 OLB 宽匹配）
            if comp.part_name in footprint_libs.get(fp, set()):
                for k, v in rec["attrs"].items():
                    merged.setdefault(k, v)
        else:
            # 无 footprint 的行：OLB 兜底
            if rec["olb"] is not None and rec["olb"] == olb:
                for k, v in rec["attrs"].items():
                    merged.setdefault(k, v)
    # 注：CATEGORY_NAME 不回填到这里——INS 行分类名链式混排（多元件字段
    # 共享），纯 DSN 无法可靠关联（实测 L 电感被 '光模块组件' 误判）。
    # R-CAT 由 EXP 辅助模块 L4 用 EXP Source Package→category 映射回填
    # （见 exp/exp_dir_assist.py），保证与 EXP 分类一致。


def _merge_instance_modes(
    merged: dict[str, str],
    comp: ComponentDef,
    inst_mode: dict[str, dict[str, str]],
) -> None:
    """把实例属性众数合并进 comp.user_attrs（按 library_id 精确匹配）。"""
    mode = inst_mode.get(comp.part_name)
    if mode:
        for k, v in mode.items():
            merged.setdefault(k, v)


#: INS 行属性名匹配正则（行内出现的用户属性名）
_ATTR_NAME_SET = frozenset(_USER_ATTR_KEYS)
#: footprint 候选正则（大写字母开头、含数字、无路径分隔符）
_FOOTPRINT_RE = re.compile(r"^[A-Z][A-Z0-9\-\.]{2,30}$")


def _extract_row_user_attrs(row: list[str]) -> dict[str, str]:
    """从 INS 行字段提取【行内】属性名+值（属性名紧邻值时）。

    只采用属性名出现在行内、且其后字段为值的记录——避免流扫描
    把无关 INS 的『描述』等属性误挂（INS5 行内无属性名 → 不产出）。

    Args:
        row: INS 行后续 lstr 字段。

    Returns:
        属性键值（已归一化，过滤哨兵值）。
    """
    result: dict[str, str] = {}
    for i, f in enumerate(row):
        if f not in _ATTR_NAME_SET:
            continue
        if i + 1 >= len(row):
            continue
        # 组件级 INS 行属性：描述 '-' 哨兵丢弃（QA 组件数口径保持）
        clean = _normalize_user_attr(row[i + 1])
        if clean is not None:
            result.setdefault(f, clean)
    return result


def _find_row_footprint(row: list[str]) -> str | None:
    """从 INS 行字段找 footprint（优先 'PCB Footprint' 属性名后的值）。

    Args:
        row: INS 行后续 lstr 字段。

    Returns:
        footprint；无命中返回 None。
    """
    # 优先：'PCB Footprint' 属性名后的值（INS5 行实测
    # ['LEVEL', ..., 'PCB Footprint', 'BGA409-26-2323', ...]——
    # 若按行首 token 扫，'LEVEL' 会误命中 footprint 正则）
    if row:
        for i, f in enumerate(row):
            if f == "PCB Footprint" and i + 1 < len(row):
                cand = row[i + 1]
                if cand and len(cand) <= 30 \
                        and _FOOTPRINT_RE.match(cand) \
                        and any(ch.isdigit() for ch in cand):
                    return cand
    # 兜底：行首/属性值后的 footprint 形 token（跳过已知属性名；
    # 真实封装名必含数字——'LEVEL'/'PINSWAP' 等属性名纯字母被排除）
    for f in row:
        if not f or f in _ATTR_NAME_SET:
            continue
        if f.startswith("INS") or "\\" in f or "/" in f:
            continue
        if len(f) > 30:
            continue
        if _FOOTPRINT_RE.match(f) and any(ch.isdigit() for ch in f):
            return f
    return None


# ---------------------------------------------------------------------------
#  PageIR 构建
# ---------------------------------------------------------------------------

def build_page_ir(
    page_name: str,
    page_id: str,
    binds: list["InstanceBind"],
    net_table: "NetTable",
    wires: list["WireRecord"],
    width: int = 3520,
    height: int = 2720,
    *,
    pin_partitions: list["PinPartitionTable"] | None = None,
    text_elements: list["PageTextElement"] | None = None,
    graphic_elements: list["InfoElement"] | None = None,
    polylines: list["WirePolyline"] | None = None,
    connectivity: "ConnectivityResult | None" = None,
    source_symbols: list | None = None,
    connection_points: list | None = None,
    pin_refs: list | None = None,
    component_db: "ComponentDB | None" = None,
    page_size: dict | None = None,
    graphic_shapes: list | None = None,
    symbol_bboxes: list | None = None,
    offpage_connectors: list | None = None,
) -> PageIR:
    """构建单个 PageIR。

    Args:
        page_name: 页面流名（如 ``04_POWER_SUPPLY``）。
        page_id: 页面编号（如 ``1.4``）。
        binds: 该页绑定实例。
        net_table: 该页网名表。
        wires: 该页走线段。
        width: 页面宽度（mil；T04 P2-5 页面头解析值覆盖默认）。
        height: 页面高度（mil）。
        pin_partitions: P0-1 0x0867 引脚分区表（默认 []）。
        text_elements: P0-2 0x0000 全页面文字元素（默认 []）。
        graphic_elements: P0-2 0x0000 图形元素（InfoElement，默认 []）。
        polylines: P0-4 折线链（默认 []；命中时走线按折线输出 points）。
        connectivity: P0-5 线↔引脚连接关系（默认 None）。
        source_symbols: P1-10 Source symbol 列表（默认 []）。
        connection_points: P1-7 同页无电线连接点列表（默认 []）。
        pin_refs: P1-1 页面 26/27 引脚引用列表（默认 []）。
        component_db: P1-12 组件库（用于按 shape 查符号文本，默认 None）。
        page_size: T04 P2-5 页面尺寸 dict（默认 None）。
        graphic_shapes: T04 P2-2 图形结构帧元素（默认 None）。
        symbol_bboxes: T04 P2-3 SymbolBBox 元素（默认 None）。
        offpage_connectors: T04 P2-5 OffPage 连接符（默认 None）。

    Returns:
        PageIR。
    """
    instances: list[ComponentInstanceIR] = []
    net_conns: dict[str, list[NetConnection]] = defaultdict(list)

    # T04 P2-5：页面尺寸（页面头 µm → mil，覆盖硬编码默认）
    if page_size:
        if page_size.get("width_mil"):
            width = int(page_size["width_mil"])
        if page_size.get("height_mil"):
            height = int(page_size["height_mil"])

    for b in binds:
        inst = ComponentInstanceIR(
            refdes=b.refdes,
            library_id=b.shape or "",
            section=b.section,
            loc_x=b.loc_x if b.loc_x is not None else 0,
            loc_y=b.loc_y if b.loc_y is not None else 0,
            value_override=b.value or "",
            properties=dict(b.properties),
        )
        # P1-11 修复（QA D5）：实例级 user_attrs —— 优先取形状锚定属性帧
        # （b.prop_anchor.props，帧序 [PROP][shape][0x0022][REF] 中锚定本实例
        # 的 0x02 帧，避免错配下一实例），缺失键兜底库级 ComponentDef.user_attrs。
        # EXP 交叉验证按实例逐条比对（非库级众数）。
        inst_attrs: dict[str, str] = {}
        if b.prop_anchor is not None and b.prop_anchor.get("props"):
            anchor_props: dict = b.prop_anchor["props"]
            for key in _USER_ATTR_KEYS:
                v = anchor_props.get(key)
                if v is not None and _normalize_user_attr_for(key, v) is not None:
                    inst_attrs[key] = v
        # 兜底：位号帧邻近属性帧（老路径，保持 Value/Footprint 语义）
        for key in _USER_ATTR_KEYS:
            if key in inst_attrs:
                continue
            v = b.properties.get(key)
            if v is not None and _normalize_user_attr_for(key, v) is not None:
                inst_attrs[key] = v
        if component_db is not None and b.shape:
            comp_attrs = component_db.get_by_library_id(b.shape)
            if comp_attrs is None:
                comp_attrs = component_db.get_by_library_id(
                    b.shape.rsplit(".", 1)[0] + ".Normal"
                )
            if comp_attrs is not None:
                for key in _USER_ATTR_KEYS:
                    if key not in inst_attrs:
                        cv = comp_attrs.user_attrs.get(key)
                        if cv is not None and _normalize_user_attr_for(key, cv) is not None:
                            inst_attrs[key] = cv
        inst.user_attrs = inst_attrs
        # P1-4：旋转/镜像回填
        inst.rotation = b.rot
        inst.mirror = b.mirror
        # P1-5/P1-6：位号字号/颜色
        if b.font:
            inst.properties.setdefault("REFDES_FONT", str(b.font))
        if b.color:
            inst.properties.setdefault("REFDES_COLOR", b.color)
        if b.footprint:
            inst.properties.setdefault("PCB Footprint", b.footprint)
        # P0-3：Cache 引脚标号映射（标号 → 引脚名）
        if b.pin_label_map:
            inst.pin_labels = dict(b.pin_label_map)
        # P1-1：页面 26/27 引脚引用 → extra_data
        if b.pin_geometry:
            inst.extra_data["pin_geometry"] = [
                dict(g) if hasattr(g, "to_dict") else g for g in b.pin_geometry
            ]
        if b.io_direction and b.io_direction != "PASSIVE":
            inst.extra_data["io_direction"] = b.io_direction
        # 页面内 GND/0x01 连接引用 → pin_connections 占位（net 由 net table 解析）
        for pin, net in b.pin_connections.items():
            inst.pin_connections[pin] = net
            net_conns[net].append(NetConnection(refdes=b.refdes, pin_number=pin))
        instances.append(inst)

    # P0-5：坐标级连接 → 实例 pin_connections（refdes.pin_key → net）。
    # pin_key 为尽力而为（标号:引脚名 / pin_names 序 / object_id），
    # 精确引脚号依赖 SymbolPin 26/27（P1 扩展审计）。
    if connectivity is not None:
        inst_by_refdes = {inst.refdes: inst for inst in instances}
        for (refdes, pin_key), net_name in connectivity.pin_connections.items():
            inst = inst_by_refdes.get(refdes)
            if inst is not None and pin_key and net_name:
                inst.pin_connections[pin_key] = net_name

    # 网表 → NetIR（含分类 + 连接）
    nets: list[NetIR] = []
    seen: set[str] = set()
    # P0-5：坐标级连接（wire 端点→net；pin_point→instance；pin_point→net）
    conn_by_net: dict[str, list[NetConnection]] = defaultdict(list)
    conn_refdes_pin: set[tuple[str, str]] = set()
    if connectivity is not None:
        for (refdes, pin_key), net_name in connectivity.pin_connections.items():
            if net_name and refdes and pin_key:
                conn_by_net[net_name].append(NetConnection(refdes=refdes, pin_number=pin_key))
                conn_refdes_pin.add((refdes, pin_key))
    net_names: dict[int, str] = net_table.names if net_table is not None else {}
    for net_id, name in sorted(net_names.items(), key=lambda kv: kv[0]):
        if name in seen:
            continue
        seen.add(name)
        conns = list(net_conns.get(name, []))
        # 合并坐标级连接（去重）
        for conn in conn_by_net.get(name, []):
            if (conn.refdes, conn.pin_number) not in conn_refdes_pin or \
               (conn.refdes, conn.pin_number) not in {
                   (c.refdes, c.pin_number) for c in conns
               }:
                conns.append(conn)
        nets.append(NetIR(
            name=name,
            category=_classify(name),
            net_id=net_id,
            connections=conns,
        ))

    # 走线 → WireSegment（P0-4：优先折线链 points；否则单段）
    def _net_name(net_id: int | None) -> str:
        """防御性网名解析（net_table 可空）。"""
        if net_table is not None:
            return net_table.name_of(net_id)
        return f"NET_{net_id}" if net_id is not None else ""

    wire_segments: list[WireSegment] = []
    if polylines:
        for poly in polylines:
            if len(poly.points) < 2:
                continue
            net_name = poly.net_name or _net_name(poly.net_id)
            wire_segments.append(WireSegment(
                start_x=poly.points[0][0],
                start_y=poly.points[0][1],
                end_x=poly.points[-1][0],
                end_y=poly.points[-1][1],
                net_name=net_name,
                page_id=page_id,
                points=list(poly.points),
                # P2-4 走线样式（取折线首段样式）
                color=getattr(poly, "color", 0) or 0,
                line_width=getattr(poly, "line_width", 0) or 0,
                line_style=getattr(poly, "line_style", 0) or 0,
            ))
    else:
        for w in wires:
            net_name = _net_name(w.net_id)
            wire_segments.append(WireSegment(
                start_x=w.x1,
                start_y=w.y1,
                end_x=w.x2,
                end_y=w.y2,
                net_name=net_name,
                page_id=page_id,
                points=[(w.x1, w.y1), (w.x2, w.y2)],
                # P2-4 走线样式
                color=getattr(w, "color", 0) or 0,
                line_width=getattr(w, "line_width", 0) or 0,
                line_style=getattr(w, "line_style", 0) or 0,
            ))

    # P0-1：引脚分区表 → dict 列表
    pin_partition_dicts: list[dict] = []
    for table in pin_partitions or []:
        for entry in table.entries:
            pin_partition_dicts.append(entry.to_dict())

    # P0-2：文字元素 → dict 列表
    text_element_dicts: list[dict] = []
    for el in text_elements or []:
        text_element_dicts.append(el.to_dict())

    # P0-2：图形元素 → dict 列表（InfoElement；text 也保留供下游）
    graphic_dicts: list[dict] = []
    for el in graphic_elements or []:
        graphic_dicts.append(el.to_dict())

    # P1-10：Source symbol → dict 列表
    source_symbol_dicts: list[dict] = []
    for sym in source_symbols or []:
        source_symbol_dicts.append(sym.to_dict() if hasattr(sym, "to_dict") else dict(sym))

    # P1-7：连接点 → dict 列表
    conn_point_dicts: list[dict] = []
    for cp in connection_points or []:
        conn_point_dicts.append(cp.to_dict() if hasattr(cp, "to_dict") else dict(cp))

    # P1-1：页面 26/27 引脚引用 → PageIR.ports（端口/连接点结构化输出）
    port_dicts: list[dict] = []
    for pref in pin_refs or []:
        port_dicts.append(pref.to_dict() if hasattr(pref, "to_dict") else dict(pref))

    # P1-12：页面级符号文本（实例平移/旋转后输出）。
    # 组件符号文本 → ComponentDef.symbol_texts；按实例 shape 基础名匹配；
    # 多单元各段按 ``section_placements`` 各自平移/旋转到对应页面。
    symbol_text_dicts: list[dict] = []
    if component_db is not None:
        for b in binds:
            if not b.shape or b.shape == "GND_SYMBOL":
                continue
            # 本 bind 在**当前页**的放置：优先 section_placements 中
            # page==page_name 的记录（多单元各段）；否则主 loc。
            place = None
            for sp in getattr(b, "section_placements", []) or []:
                if sp.get("page") == page_name:
                    place = sp
                    break
            if place is None:
                place = {
                    "shape": b.shape,
                    "page": page_name,
                    "loc_x": b.loc_x if b.loc_x is not None else 0,
                    "loc_y": b.loc_y if b.loc_y is not None else 0,
                }
            comp = component_db.get_by_library_id(b.shape)
            if comp is None:
                comp = component_db.get_by_library_id(b.shape.rsplit(".", 1)[0] + ".Normal")
            if comp is None or not comp.symbol_texts:
                continue
            ox = place.get("render_x", place.get("loc_x", 0)) or 0
            oy = place.get("render_y", place.get("loc_y", 0)) or 0
            for t in comp.symbol_texts:
                # P2-全元件符号文本：body/pin 几何元素无渲染文本，
                # 不进入页面级 symbol_texts（仅 ComponentDef 粒度承载）。
                if t.get("kind") in ("body", "pin"):
                    continue
                tx = int(t.get("x", 0) or 0)
                ty = int(t.get("y", 0) or 0)
                symbol_text_dicts.append({
                    "refdes": b.refdes,
                    "symbol": t.get("symbol", ""),
                    "text": t.get("text", ""),
                    "x": ox + tx,
                    "y": oy + ty,
                    "rotation": b.rot,
                    "mirror": b.mirror,
                    "font": int(t.get("font", 0) or 0),
                    "attr": int(t.get("attr", 0) or 0),
                })

    # T04 P2-2：图形结构帧 → graphic_elements（原语 kind 标注）
    shape_dicts: list[dict] = []
    for sh in graphic_shapes or []:
        d = sh.to_dict() if hasattr(sh, "to_dict") else dict(sh)
        d["primitive"] = d.get("kind", "")
        d["kind"] = "graphic_shape"
        shape_dicts.append(d)
    # T04 P2-3：SymbolBBox → graphic_elements（kind=symbol_bbox）
    for bb in symbol_bboxes or []:
        d = bb.to_dict() if hasattr(bb, "to_dict") else dict(bb)
        d["kind"] = "symbol_bbox"
        shape_dicts.append(d)
    graphic_dicts.extend(shape_dicts)

    # T04 P2-5：OffPage 连接符 → PageIR.off_pages
    off_page_dicts: list[dict] = []
    for op in offpage_connectors or []:
        d = op.to_dict() if hasattr(op, "to_dict") else dict(op)
        off_page_dicts.append({
            "name": d.get("net_name", "") or "",
            "net_name": d.get("net_name", "") or "",
            "x": d.get("x", 0),
            "y": d.get("y", 0),
            "offset": d.get("offset", 0),
            "object_id": d.get("object_id", 0),
        })

    return PageIR(
        page_id=page_id,
        page_name=page_name,
        width=width,
        height=height,
        instances=instances,
        nets=nets,
        wires=wire_segments,
        graphic_elements=graphic_dicts,
        pin_partitions=pin_partition_dicts,
        text_elements=text_element_dicts,
        source_symbols=source_symbol_dicts,
        connection_points=conn_point_dicts,
        ports=port_dicts,
        symbol_texts=symbol_text_dicts,
        off_pages=off_page_dicts,
    )


# ---------------------------------------------------------------------------
#  DesignIR 构建（顶层）
# ---------------------------------------------------------------------------

#: PageData 元组字段索引（P0 扩展后：0=页面名 1=绑定 2=网表 3=走线）
#: 4=pin_partitions 5=text_elements 6=graphic_elements 7=polylines 8=connectivity
#: 9=source_symbols 10=connection_points 11=pin_refs
PAGE_DATA_FIELDS = (
    "page_name",
    "binds",
    "net_table",
    "wires",
    "pin_partitions",
    "text_elements",
    "graphic_elements",
    "polylines",
    "connectivity",
    "source_symbols",
    "connection_points",
    "pin_refs",
)


def _page_data_item(
    page: tuple, index: int, default: object = None,
) -> object:
    """安全读取 page_data 元组的第 index 个字段（向后兼容旧 4 元组）。"""
    return page[index] if index < len(page) else default


def map_to_design_ir(
    project_name: str,
    pages_data: list[tuple],
    cache: "CacheData | None" = None,
    packages: list | None = None,
    library_attrs: list | None = None,
    *,
    frame_stats: dict | None = None,
    warnings: list | None = None,
    decoder_version: str | None = None,
) -> DesignIR:
    """将全部页面数据映射为 DesignIR。

    ``pages_data`` 支持两种形态：
    - 旧 4 元组 ``(页面流名, 实例绑定列表, 网名表, 走线列表)``；
    - 新 P0 9 元组 ``(页面流名, 绑定, 网表, 走线, pin_partitions,
      text_elements, graphic_elements, polylines, connectivity)``。

    P2-A 基础设施（frame_stats/decoder_version/warnings）：
    - ``frame_stats`` 由调用方（DSNParser，持有原始帧）计算传入；
    - 未传入时注入默认结构（``total/covered/unknown/covered_pct/
      top_unknown_types`` 全 0/空），保证 metadata 键齐全；
    - ``warnings`` 为解析警告列表（severity/code/msg/page_id）；
    - ``decoder_version`` 缺省 ``DEFAULT_DECODER_VERSION``。

    Args:
        project_name: 设计名（DSN 文件名 stem）。
        pages_data: 每页数据元组（见上）。
        cache: Cache 解析结果（可选）。
        packages: Packages 解析结果（可选）。
        library_attrs: Library 流 INS 属性记录（P1-11，可选）。
        frame_stats: P2 帧覆盖率统计 dict（可选；DSNParser 计算传入）。
        warnings: P2 解析警告列表（可选）。
        decoder_version: 解码器版本号（可选）。

    Returns:
        DesignIR（Pydantic 校验通过）。
    """
    from ....db.component_db import ComponentDB
    from ....ir.design import (
        DEFAULT_DECODER_VERSION,
        default_frame_stats,
        ensure_p2_metadata,
    )

    design = DesignIR(
        project_name=project_name,
        source_format="CIS_DSN",
        component_db=ComponentDB(),
    )

    total_pin_partitions = 0
    total_pin_partition_tables = 0
    total_text_elements = 0
    total_polyline_points = 0
    total_polyline_count = 0
    total_connections = 0
    total_pin_label_pairs = 0
    u16_pages: list[str] = []

    # P1-12：组件库先构建（页面级符号文本按 shape 查 ComponentDef.symbol_texts）
    if cache is not None or packages:
        build_component_db(
            cache, packages, design.component_db,
            library_attrs=library_attrs,
            pages_data=pages_data,
        )

    for i, page in enumerate(pages_data):
        page_name = _page_data_item(page, 0, "")
        binds = _page_data_item(page, 1, [])
        net_table = _page_data_item(page, 2, None)
        wires = _page_data_item(page, 3, [])
        pin_partitions = _page_data_item(page, 4, None)
        text_elements = _page_data_item(page, 5, None)
        graphic_elements = _page_data_item(page, 6, None)
        polylines = _page_data_item(page, 7, None)
        connectivity = _page_data_item(page, 8, None)
        source_symbols = _page_data_item(page, 9, None)
        connection_points = _page_data_item(page, 10, None)
        pin_refs = _page_data_item(page, 11, None)

        page_ir = build_page_ir(
            page_name=page_name,
            page_id=f"1.{i + 1}",
            binds=binds,
            net_table=net_table,
            wires=wires,
            pin_partitions=pin_partitions,
            text_elements=text_elements,
            graphic_elements=graphic_elements,
            polylines=polylines,
            connectivity=connectivity,
            source_symbols=source_symbols,
            connection_points=connection_points,
            pin_refs=pin_refs,
            component_db=design.component_db,
            page_size=_page_data_item(page, 12, None),
            graphic_shapes=_page_data_item(page, 13, None),
            symbol_bboxes=_page_data_item(page, 14, None),
            offpage_connectors=_page_data_item(page, 15, None),
        )
        design.pages.append(page_ir)

        # P0 统计
        if pin_partitions:
            total_pin_partition_tables += len(pin_partitions)
            total_pin_partitions += sum(len(t.entries) for t in pin_partitions)
        if text_elements:
            total_text_elements += len(text_elements)
        if polylines:
            total_polyline_count += len(polylines)
            total_polyline_points += sum(len(p.points) for p in polylines)
        if connectivity is not None:
            total_connections += connectivity.connection_count

        # P0-6：u16 网名表页记录
        if net_table is not None and getattr(net_table, "count_bytes", 1) == 2:
            u16_pages.append(str(page_name))

        if net_table is not None:
            for g in net_table.names.values():
                if g and g not in design.global_nets:
                    design.global_nets.append(g)

    # P0-3：pin_label_pairs 以 Cache 标号映射为准（6 单元 203 对）。
    # 注意不能用 per-page binds 求和 —— ``_merge_multi_unit`` 会原地把
    # primary 的 pin_label_map 替换为主单元 0F(89)，导致跨页重复计数。
    if cache is not None and cache.pin_label_maps:
        total_pin_label_pairs = sum(len(m.pairs) for m in cache.pin_label_maps)
    else:
        total_pin_label_pairs = sum(
            len(b.pin_label_map)
            for p in pages_data
            for b in _page_data_item(p, 1, [])
        )

    design.metadata["page_count"] = len(pages_data)
    design.metadata["total_instances"] = sum(
        len(_page_data_item(p, 1, [])) for p in pages_data
    )
    design.metadata["total_wires"] = sum(
        len(_page_data_item(p, 3, [])) for p in pages_data
    )
    # ── P0 metadata（架构 §8 约定键）───────────────────────────────
    design.metadata["pin_partition_count"] = total_pin_partitions
    design.metadata["pin_partition_table_count"] = total_pin_partition_tables
    design.metadata["text_element_count"] = total_text_elements
    design.metadata["polyline_count"] = total_polyline_count
    design.metadata["polyline_point_count"] = total_polyline_points
    design.metadata["connection_count"] = total_connections
    design.metadata["pin_label_pairs"] = total_pin_label_pairs
    design.metadata["net_count_u16_pages"] = u16_pages

    # ── P2-A 基础设施：frame_stats / decoder_version / warnings ──────
    ensure_p2_metadata(design.metadata)
    if frame_stats is not None:
        design.metadata["frame_stats"] = dict(frame_stats)
    else:
        # 调用方未提供时注入默认结构（键齐全，覆盖率为 0）
        design.metadata["frame_stats"] = default_frame_stats()
    if warnings is not None:
        design.metadata["warnings"] = list(warnings)
    design.metadata["decoder_version"] = decoder_version or DEFAULT_DECODER_VERSION

    logger.info(
        "DesignIR built: %d pages, %d instances, %d wires, "
        "%d pin_partitions, %d text_elements, %d polylines, %d connections",
        len(design.pages),
        design.metadata["total_instances"],
        design.metadata["total_wires"],
        total_pin_partitions,
        total_text_elements,
        total_polyline_count,
        total_connections,
    )
    return design
