"""实例绑定层（架构设计 §2 L4 + §5 full_bind 修复规格）。

绑定链：``0x85 位号帧 → 帧簇邻近 0x02 属性帧 → shape 帧 → Cache generalProperties
→ Packages pinMap``。

修复要点（相对 full_bind_v2）：
1. **Value 主来源 = 0x85 帧 valueIdx**（QA 证据 1，643/643 可读）；0x02 帧
   ``(632,'Value')→值`` 作覆盖；Cache partValue 仅兜底。
2. **Footprint 主来源 = 0x02 帧 [25 'PCB Footprint']→值**；兜底顺序：
   0x02 [25]→值 > 0x02 [29 'PKG_GRP']→值（U7 BGA409-26-2323）>
   同 shape 簇其它 0x02 [25]→值 > Packages pcbFootprint > None。
3. **关联改用"帧簇邻近"**（按帧索引窗口 ±N 帧）替代绝对偏移阈值
   （<500/<600/<800 是 54 个 no_prop 的根因）。
4. **多单元 U7（Hi5662，6 页 0A~0F）按 refdes 合并为 1 实例**。
5. **pin_map/pin_names**：每个实例从 Packages（shape 基础名匹配）绑定
   引脚编号表与引脚名表（U7 主单元 = 178 pin + 409 pinNames）。
6. **loc_y 补全**：从最近 shape 帧坐标（x,y）与 PlacedInstance(0x0D) 帧
   坐标候选补全（0x85/0x22 位号帧只含 x）。
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field

from ..cache.cache_parser import CacheData
from ..cache.packages_parser import PackageData
from ..decode.net_table import NetTable
from ..decode.semantic_frames import (
    IDX_VALUE,
    REFDES_FRAME_TYPES,
    PropertyFrame,
    RefdesFrame,
    parse_property_frame,
    parse_refdes_frame,
)
from ..decode.shape_frames import (
    ShapeFrame,
    extract_render_origin,
    extract_shape,
    extract_shape_bbox,
    extract_shape_coord,
    resolve_bbox_fallback,
)
from ..frame.splitter import Frame

logger = logging.getLogger(__name__)

#: 帧簇邻近窗口（帧索引前后各 N 帧）
CLUSTER_WINDOW = 12
#: 走线 netid 解析最大扫描帧数（用于 GND/网络统计）
_MAX_NET_SCAN = 64
#: 0x02 属性帧最小长度（实证属性帧 ≥100B；45B 为引脚/连接记录）
_PROP_MIN_LEN = 60
#: 0x02 帧内 0x0d 属性对扫描起点（跳过帧头）
_PROP_SCAN_START = 10


@dataclass
class InstanceBind:
    """实例全息记录（最终交付 JSON/IR 的核心契约）。"""

    refdes: str
    page: str
    value: str | None
    value_source: str
    value_idx: int | None
    footprint: str | None
    footprint_source: str
    shape: str | None
    cache_ref_des: str | None
    cache_source_lib: str | None
    loc_x: int | None
    loc_y: int | None
    frame_offset: int
    frame_type: int
    section: int = 1
    properties: dict[str, str] = field(default_factory=dict)
    #: 引脚网络连接（0x01/GND 帧解析；pin → net 名）
    pin_connections: dict[str, str] = field(default_factory=dict)
    #: 引脚编号表（Packages pinMap；如 ['1','2',...]）
    pin_map: list[str] = field(default_factory=list)
    #: 引脚名表（Packages pinNames；如 ['VCC','GND',...]）
    pin_names: list[str] = field(default_factory=list)
    #: 引脚标号映射（Cache 标号表；标号 → 引脚名，如 AA23 → Y21）
    pin_label_map: dict[str, str] = field(default_factory=dict)
    # ── P1 扩展（默认值向后兼容）──────────────────────────────
    #: 引脚几何（Cache type-4；列表 dict）
    pin_geometry: list = field(default_factory=list)
    #: 电气类型（P1-2 portType 映射；默认 PASSIVE）
    io_direction: str = "PASSIVE"
    #: 旋转角度（P1-4；0x85 marker 后 rot 字节，度）
    rot: int = 0
    #: 位号字号（P1-5；0x85 marker 后 font 字节）
    font: int = 0
    #: 镜像标志（P1-4；0=无、1=X 镜像、2=Y 镜像）
    mirror: int = 0
    #: 旋转方向标志（08-20：0x85 marker `04 00 30` vs `04 40 30`，
    #: 0x40 位区分 R90/R270 → cache 引脚线旋转方向）
    rot_marker40: bool = False
    #: 位号文本颜色（P1-6；attr 高位字节 → 颜色名）
    color: str | None = None
    #: 多单元分片放置记录（P1-12 页面级符号文本用）。
    #: 每个单元：{"shape": str, "page": str, "loc_x": int, "loc_y": int}
    section_placements: list[dict] = field(default_factory=list)
    # ── P1-11 修复（QA D5）：形状锚定属性帧 ────────────────────
    #: 由最近 shape 帧锚定选出的 0x02 属性帧（帧序 [PROP][shape][0x0022][REF]）。
    #: 相比"最近位号帧"直接选属性帧，shape 锚定避免把下一实例的
    #: 属性帧误配到本实例（R190→U12 的 0x02 错位根因）。
    prop_anchor: dict | None = None
    # ── P2-A：渲染原点 (locX, locY)（符号文本平移用；与 EXP loc 分离）──
    #: 最近 shape 帧的渲染原点（locX/locY）。IR loc_x/loc_y 取 bbox 角
    #: (x1,y1)（EXP 口径），此处保留放置原点供 symbol_texts 平移。
    render_origin: tuple[int, int] | None = None
    # ── P2-B：体框 (x1, y1, x2, y2)（引脚邻近判定）───────────────────
    #: 最近 shape 帧的体框（页面 mil 坐标）。引脚点位于体框边界附近，
    #: 用于把走线端点/引脚点精确归属到实例（避免密集区误配）。
    bbox: tuple[int, int, int, int] | None = None
    # ── T04：Cache 引脚线端点（页面 mil，符号局部→实例 loc 平移）──
    #: [(pin_name, x1, y1, x2, y2)]——来自 Cache 命名引脚定义帧族
    #: （scan_cache_pin_defs）。小 IC（3A BUCK/LDO_1/PSW6M2/SOP8 等）
    #: 无 Packages pinMap，引脚几何由 Cache 提供；走线端点落在引脚线
    #: 端点 ±容差内即归属该引脚（替代 bbox 竞争，解决 P2-B 连接缺口）。
    cache_pin_lines: list[tuple] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "refdes": self.refdes,
            "page": self.page,
            "value": self.value,
            "value_source": self.value_source,
            "valueIdx": self.value_idx,
            "footprint": self.footprint,
            "footprint_source": self.footprint_source,
            "shape": self.shape,
            "cache_refDes": self.cache_ref_des,
            "cache_src": self.cache_source_lib,
            "loc_x": self.loc_x,
            "loc_y": self.loc_y,
            "frame_offset": self.frame_offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "section": self.section,
            "properties": dict(self.properties),
            "pin_map": list(self.pin_map),
            "pin_names": list(self.pin_names),
            "pin_label_map": dict(self.pin_label_map),
            # P1 扩展
            "pin_geometry": [dict(g) if hasattr(g, "to_dict") else g for g in self.pin_geometry],
            "io_direction": self.io_direction,
            "rot": self.rot,
            "font": self.font,
            "mirror": self.mirror,
            "color": self.color,
            "section_placements": [dict(sp) for sp in self.section_placements],
            "render_origin": list(self.render_origin) if self.render_origin else None,
            "bbox": list(self.bbox) if self.bbox else None,
        }


def _nearest_by_order(
    candidates: list[tuple[int, object]], index: int, window: int,
) -> tuple[int, object] | None:
    """在候选列表中找帧索引最接近 ``index`` 的项（帧顺序距离）。"""
    best: tuple[int, object] | None = None
    best_d = window + 1
    for cand_index, obj in candidates:
        d = abs(cand_index - index)
        if d < best_d:
            best_d = d
            best = (cand_index, obj)
    return best


def _shape_base(shape: str | None) -> str | None:
    if not shape:
        return None
    return shape.rsplit(".", 1)[0]


class InstanceBinder:
    """跨帧实例绑定器。

    Usage::

        binder = InstanceBinder(strlst, cache, packages)
        binds = binder.bind_all_pages(pages_data)
    """

    def __init__(
        self,
        strlst: list[str],
        cache: CacheData | None = None,
        packages: list[PackageData] | None = None,
        cluster_window: int = CLUSTER_WINDOW,
    ) -> None:
        """初始化绑定器。

        Args:
            strlst: Library strLst。
            cache: Cache 解析结果（兜底 Value/refDes 前缀）。
            packages: Packages 解析结果（pinMap/pcbFootprint 兜底）。
            cluster_window: 帧簇邻近窗口。
        """
        self.strlst = strlst
        self.cache = cache
        self.packages = packages or []
        self.cluster_window = cluster_window
        self._cache_by_shape: dict[str, tuple[str | None, str | None, str | None]] = {}
        self._package_by_name: dict[str, PackageData] = {}
        #: P1-3：GND 实例序号（按网名跨页全局递增，保证 refdes 全局唯一，
        #: 避免 ``GND_1`` 等跨页同名被 ``_merge_multi_unit`` 错误合并）
        self._gnd_seq: dict[str, int] = {}
        self._init_lookups()

    def _init_lookups(self) -> None:
        if self.cache is not None:
            for part in self.cache.parts:
                self._cache_by_shape[part.name] = (
                    part.ref_des, part.part_value, part.source_library,
                )
        for pkg in self.packages:
            if pkg.name:
                self._package_by_name[pkg.name] = pkg

    # ── Cache 查找 ──────────────────────────────────────────────────

    def _cache_lookup(self, shape: str | None) -> tuple[str | None, str | None, str | None]:
        """按 shape 查找 Cache（refDes, value, sourceLib）。"""
        if not shape:
            return None, None, None
        if shape in self._cache_by_shape:
            return self._cache_by_shape[shape]
        base = _shape_base(shape)
        if base:
            for key, val in self._cache_by_shape.items():
                if _shape_base(key) == base:
                    return val
        return None, None, None

    # ── Packages 兜底 ───────────────────────────────────────────────

    def _package_lookup(self, shape: str | None) -> PackageData | None:
        """按 shape 基础名匹配 Packages 流。

        匹配规则（严格，避免单字符前缀误配）：
        1. 基础名 == 包名（如 ``LB_0.Normal`` → ``LB_0``）。
        2. 包名以 ``基础名_`` 开头（如 ``LB.Normal`` → ``LB_0``）。
        3. 多单元包：包名去掉单元字母后为基础名前缀
           （如 ``Hi5662YV305_DDR_GE_0D.Normal`` → ``Hi5662YV305_DDR_GE_0F``）。

        Args:
            shape: 外形名（如 ``Hi5662YV305_DDR_GE_0D.Normal``）。

        Returns:
            PackageData；无匹配返回 None。
        """
        if not shape:
            return None
        base = _shape_base(shape)
        if not base:
            return None
        if base in self._package_by_name:
            return self._package_by_name[base]
        for pkg in self.packages:
            if pkg.name is None:
                continue
            # 规则 2：包名 = 基础名 + "_" + 后缀（LB_0 ← LB）
            if pkg.name.startswith(base + "_"):
                return pkg
            # 规则 3：多单元包（包名去掉单元字母为基础名前缀）
            if pkg.unit and pkg.name[:-1] and base.startswith(pkg.name[:-1]):
                return pkg
        return None

    def _package_footprint(self, shape: str | None) -> str | None:
        """Packages 流 pcbFootprint 兜底（按 shape 基础名匹配）。"""
        pkg = self._package_lookup(shape)
        return pkg.pcb_footprint if pkg is not None else None

    def _package_pins(self, shape: str | None) -> tuple[list[str], list[str]]:
        """Packages 流 pinMap/pinNames 绑定（按 shape 基础名匹配）。

        Args:
            shape: 外形名。

        Returns:
            (pin_map, pin_names)；无匹配返回 ([], [])。
        """
        pkg = self._package_lookup(shape)
        if pkg is None:
            return [], []
        return list(pkg.pin_map), list(pkg.pin_names)

    def _bind_pin_labels(self, shape: str | None) -> dict[str, str]:
        """按 shape 基础名匹配 Cache 引脚标号映射（P0-3）。

        多单元 U7 六段（0A..0F）：主单元取 0F(178)，各单元独立保留。

        Args:
            shape: 外形名（如 ``Hi5662YV305_DDR_GE_0A.Normal``）。

        Returns:
            标号 → 引脚名 映射；无匹配返回 {}。
        """
        if not shape or self.cache is None:
            return {}
        base = _shape_base(shape)
        if not base:
            return {}
        maps = self.cache.pin_label_maps or []
        # 1) 精确匹配单元名
        for pin_map in maps:
            if pin_map.unit_name == base:
                return pin_map.by_label
        # 2) 多单元：基础名前缀匹配（取后缀字母最大的 = 主单元 0F）
        best: dict[str, str] = {}
        best_suffix = ""
        for pin_map in maps:
            unit = pin_map.unit_name
            if unit.startswith(base) and len(unit) == len(base) + 1:
                suffix = unit[len(base):]
                if suffix >= best_suffix:
                    best_suffix = suffix
                    best = pin_map.by_label
        return best

    # ── 单页绑定 ────────────────────────────────────────────────────

    def bind_page(
        self,
        frames: list[Frame],
        net_table: NetTable,
        page_name: str,
    ) -> list[InstanceBind]:
        """绑定一页实例。

        Args:
            frames: 该页全部帧（含网名表之前的部分，内部按 net_table.offset 过滤）。
            net_table: 网名表。
            page_name: 页面名。

        Returns:
            InstanceBind 列表（未合并多单元）。
        """
        after = net_table.offset if net_table.offset is not None else 0x400
        # 结构区帧（网名表之后）
        struct_frames = [f for f in frames if f.offset >= after]

        # 位号帧（0x85 优先，0x22 次之，其余含位号文本帧兜底）
        from ..decode.registry import DecodeContext

        ctx = DecodeContext(strlst=self.strlst, page_name=page_name)
        refdes_entries: list[tuple[int, RefdesFrame]] = []
        for idx, frame in enumerate(struct_frames):
            if frame.frame_type == 0:
                continue  # 0 型帧为页标签/空帧，跳过
            if frame.frame_type not in REFDES_FRAME_TYPES:
                continue
            rd = parse_refdes_frame(frame, ctx)
            if rd is not None:
                refdes_entries.append((idx, rd))

        # 去重：同 refdes 且帧索引相近 → 0x85 优先
        refdes_entries = self._dedupe_refdes(refdes_entries)

        # 属性帧（内容探测：0x0d 标记 + 长度；frame_type 数据相关，
        # 不限于 0x02——实测 C74 属性帧 type=17625、C148 type=2）
        prop_entries: list[tuple[int, PropertyFrame]] = []
        for idx, frame in enumerate(struct_frames):
            if len(frame.raw) < _PROP_MIN_LEN:
                continue
            pf = parse_property_frame(frame, ctx)
            if not pf.prop_pairs:
                continue
            prop_entries.append((idx, pf))

        # shape 帧（含外形名 + 坐标 + 渲染原点 + 体框）
        shape_entries: list[tuple[int, ShapeFrame]] = []
        for idx, frame in enumerate(struct_frames):
            shape = extract_shape(frame)
            if shape is not None:
                coord = self._extract_shape_coord(frame)
                render = self._extract_shape_render_origin(frame)
                bbox = resolve_bbox_fallback(
                    shape, coord, extract_shape_bbox(frame.raw),
                )
                shape_entries.append(
                    (idx, ShapeFrame(
                        offset=frame.offset,
                        frame_type=frame.frame_type,
                        shape=shape,
                        coord=coord,
                        render_origin=render,
                        bbox=bbox,
                    ))
                )

        # PlacedInstance 结构帧（0x0D）→ loc_y 坐标候选
        placed_entries: list[tuple[int, int, int]] = []
        for idx, frame in enumerate(struct_frames):
            coord = self._extract_placed_coord(frame)
            if coord is not None:
                placed_entries.append((idx, coord[0], coord[1]))

        binds: list[InstanceBind] = []
        for idx, rd in refdes_entries:
            bind = self._bind_one(
                idx, rd, prop_entries, shape_entries, placed_entries, page_name,
            )
            # P1-4/P1-5：0x85 位号帧 rot/font/mirror 回填
            bind.rot = rd.rot
            bind.font = rd.font
            bind.mirror = rd.mirror
            # 08-20：marker 0x40 位 = 旋转方向（R90/R270）
            bind.rot_marker40 = rd.rot_marker40
            # P1-12：记录本单元放置（shape 基础名 + 页面 + 坐标）
            if bind.shape:
                rx, ry = bind.render_origin if bind.render_origin is not None else (None, None)
                bind.section_placements.append({
                    "shape": bind.shape,
                    "page": page_name,
                    "loc_x": bind.loc_x or 0,
                    "loc_y": bind.loc_y or 0,
                    # P2-A：渲染原点（符号文本平移用，与 EXP loc 分离保留）
                    "render_x": rx if rx is not None else (bind.loc_x or 0),
                    "render_y": ry if ry is not None else (bind.loc_y or 0),
                })
            binds.append(bind)

        # P1-3：GND/电源符号实例化（0x01 短帧 → refdes=GND_网名_n）
        gnd_binds = self._collect_gnd_instances(frames, net_table, page_name)
        binds.extend(gnd_binds)
        return binds

    def _collect_gnd_instances(
        self,
        frames: list[Frame],
        net_table: NetTable,
        page_name: str,
    ) -> list[InstanceBind]:
        """收集 0x01 短帧为 GND/电源符号实例（P1-3）。

        0x01 短帧（≤50B）布局：``[DELIM][0x01][x:u16][y:u16][dbId:u32][trailer]``。
        实例命名：``GND_<网名>_<n>``；library_id='GND_SYMBOL'（用户裁决 ②）。

        Args:
            frames: 页面帧列表（含网名表之前部分）。
            net_table: 网名表（用于按坐标反查网名）。
            page_name: 页面名。

        Returns:
            InstanceBind 列表（GND 符号实例）。
        """
        from ..decode.wire_topology import PIN_POINT_FRAME_TYPES, PIN_POINT_MAX_LEN

        result: list[InstanceBind] = []
        seen: set[tuple[int, int]] = set()
        for frame in frames:
            if frame.frame_type != 0x01:
                continue
            if len(frame.raw) > PIN_POINT_MAX_LEN:
                continue
            rec = frame.raw
            if len(rec) < 18:
                continue
            if b"\x18" in rec[10:16]:
                continue
            x = int.from_bytes(rec[10:12], "little")
            y = int.from_bytes(rec[12:14], "little")
            if x <= 0 or y <= 0 or x > 10000 or y > 10000:
                continue
            key = (x, y)
            if key in seen:
                continue
            seen.add(key)
            # 网名：按坐标匹配走线端点（最近网名表）
            net_name = self._net_name_at(x, y, frames, net_table)
            # 序号按网名跨页全局递增（保证 refdes 唯一）
            seq_key = net_name or ""
            self._gnd_seq[seq_key] = self._gnd_seq.get(seq_key, 0) + 1
            n = self._gnd_seq[seq_key]
            refdes = f"GND_{net_name}_{n}" if net_name else f"GND_{n}"
            result.append(InstanceBind(
                refdes=refdes,
                page=page_name,
                value=None,
                value_source="none",
                value_idx=None,
                footprint=None,
                footprint_source="none",
                shape="GND_SYMBOL",
                cache_ref_des="GND",
                cache_source_lib=None,
                loc_x=x,
                loc_y=y,
                frame_offset=frame.offset,
                frame_type=0x01,
            ))
        return result

    @staticmethod
    def _net_name_at(
        x: int, y: int, frames: list[Frame], net_table: NetTable,
    ) -> str | None:
        """按坐标匹配最近走线端点反查网名（GND 实例命名用）。

        Args:
            x, y: 目标坐标。
            frames: 页面帧列表。
            net_table: 网名表。

        Returns:
            网名；无匹配返回 None。
        """
        if net_table is None:
            return None
        best: tuple[int, str] | None = None
        for frame in frames:
            if frame.frame_type not in (20, 21):
                continue
            rec = frame.raw
            # 走线帧坐标候选（防御扫描 u16 对）
            for off in range(10, min(len(rec) - 3, 60)):
                x1 = int.from_bytes(rec[off:off + 2], "little")
                y1 = int.from_bytes(rec[off + 2:off + 4], "little")
                if not (0 < x1 < 10000 and 0 < y1 < 10000):
                    continue
                d = abs(x1 - x) + abs(y1 - y)
                if d > 200:
                    continue
                net_name = net_table.name_of(frame.frame_type)
                if net_name and (best is None or d < best[0]):
                    best = (d, net_name)
        return best[1] if best else None

    @staticmethod
    def _extract_placed_coord(frame: "Frame") -> tuple[int, int] | None:
        """从 0x0D PlacedInstance 结构帧提取坐标候选 (x, y)（P2-A 兜底）。

        实证帧布局（TG0C0D2）：``[DELIM][0x0D][x:u16][y:u16][dbId:u32][00...]``
        （如 U7 帧 off=0x5cb0：``0d 00 de 03 72 01 9d 50`` → x=990, y=370）。

        **P1-8 双形态分流**：0x0D 帧有两种形态：
        - 信息页（01_Cover Page / 03_Power Tree）：273B 长文本帧
          （含 '01_Cover Page' / '03_Power Tree' 页名，坐标区不是 x/y）；
        - 元件页：45B 坐标帧。
        本函数先按长度/文本特征分流：>100B 或帧内含可打印文本
        （offset 10 之后）→ 返回 None（非坐标帧）。

        **P2-A 多形态兜底**：0x0D 帧是实例放置帧（PlacedInstance 结构枚举
        13），在 shape 帧坐标缺失时作替代坐标源。坐标读法：
        1. 45B 短帧 ``[x:u16][y:u16]``（偏移 10..14）；
        2. marker30 标记附近坐标（best-effort）。

        Args:
            frame: 输入帧。

        Returns:
            (x, y)；非 0x0D 帧或坐标非法返回 None。
        """
        if frame.frame_type != 0x0D:
            return None
        rec = frame.raw
        if len(rec) < 16:
            return None
        # P1-8：长文本帧分流（>100B 或含可打印文本 → 非坐标帧）
        if len(rec) > 100:
            return None
        if re.search(rb"[\x20-\x7e]{3,}", rec[10:]):
            return None
        x = int.from_bytes(rec[10:12], "little")
        y = int.from_bytes(rec[12:14], "little")
        if x <= 0 or y <= 0 or x > 10000 or y > 10000:
            # P2-A：45B 直接读失败 → marker30 标记附近坐标兜底
            return extract_shape_coord(rec)
        return x, y

    @staticmethod
    def _extract_shape_coord(frame: "Frame") -> tuple[int, int] | None:
        """从外形名帧提取实例坐标候选 (x, y)（P2-A 核心修正）。

        **P2-A 修正**：实例坐标取 **(x1, y1) bbox 角** —— 与 EXP/CSV
        Location 交叉验证 100%（TG0C0D2 737/737 within 1mil）。
        早期实现误读 (locX,locY)（渲染原点），与 EXP 偏差 4~31 mil
        （0% 命中），即审计 P2-A 坐标缺口根因。

        形态优先级（防御性，未知结构返回 None）：
        1. 外形名 GraphicInst → (x1, y1)；
        2. 45B 短引用帧 → (x, y)；
        3. marker30 标记附近坐标；
        4. 62B/83B 图形帧 u16 坐标扫描。

        Args:
            frame: 输入帧。

        Returns:
            (x, y)；无法定位返回 None。
        """
        return extract_shape_coord(frame.raw)

    @staticmethod
    def _extract_shape_render_origin(frame: "Frame") -> tuple[int, int] | None:
        """从外形名帧提取渲染原点 (locX, locY)（P2-A 符号文本平移用）。

        IR loc_x/loc_y 取 bbox 角 (x1,y1)（EXP 口径），而符号内部文本
        渲染需按放置原点 (locX,locY) 平移（QA P1-12 U7C
        SPI_interface=353+235,1+96 实证）。

        Args:
            frame: 输入帧。

        Returns:
            (locX, locY)；无法定位返回 None。
        """
        return extract_render_origin(frame.raw)

    @staticmethod
    def _dedupe_refdes(
        entries: list[tuple[int, RefdesFrame]],
    ) -> list[tuple[int, RefdesFrame]]:
        """去重：同页同 refdes 取 0x85 优先（0x22 次之，其它帧兜底）。"""
        # 按 refdes 分组
        by_refdes: dict[str, list[tuple[int, RefdesFrame]]] = defaultdict(list)
        for idx, rd in entries:
            by_refdes[rd.refdes].append((idx, rd))
        result: list[tuple[int, RefdesFrame]] = []
        for refdes, group in by_refdes.items():
            # 0x85 > 0x22 > 其它
            def _prio(rd: RefdesFrame) -> int:
                if rd.frame_type == 0x85:
                    return 0
                if rd.frame_type == 0x22:
                    return 1
                return 2

            group.sort(key=lambda t: (_prio(t[1]), t[0]))
            result.append(group[0])
        result.sort(key=lambda t: t[0])
        return result

    def _bind_one(
        self,
        idx: int,
        rd: RefdesFrame,
        prop_entries: list[tuple[int, PropertyFrame]],
        shape_entries: list[tuple[int, ShapeFrame]],
        placed_entries: list[tuple[int, int, int]],
        page_name: str,
    ) -> InstanceBind:
        """绑定单个位号帧。

        Args:
            idx: 位号帧索引。
            rd: 位号帧解码结果。
            prop_entries: 0x02 属性帧（已过滤短帧）。
            shape_entries: 外形名帧（含坐标候选）。
            placed_entries: 0x0D PlacedInstance 坐标候选 [(idx, x, y)]。
            page_name: 页面名。

        Returns:
            InstanceBind。
        """
        window = self.cluster_window

        # ── 最近 shape 帧（单向：帧序 [PROP][shape][0x0022][REF]，shape
        # 帧在 REF 之前；只向前找避免跨实例串挂——P2 攻坚 ① prop_anchor
        # 串挂 15 例修复）──
        nearest_shape = _nearest_by_order(
            [(i, sh) for i, sh in shape_entries if i < idx], idx, window,
        )
        shape: str | None = nearest_shape[1].shape if nearest_shape else None
        shape_coord: tuple[int, int] | None = nearest_shape[1].coord if nearest_shape else None
        shape_render: tuple[int, int] | None = (
            nearest_shape[1].render_origin if nearest_shape else None
        )
        shape_bbox: tuple[int, int, int, int] | None = (
            nearest_shape[1].bbox if nearest_shape else None
        )

        # ── 最近 0x02 属性帧（位号帧邻近；Value/Footprint/properties） ──
        # 2026-08-21：单向向前（同 prop_anchor 帧序实证
        # [PROP][shape][0x0022][REF]）——双向 abs 最近会错配到
        # 下一实例的属性帧（C148→U7D：位号帧距 U7D 属性帧 2 帧
        # 比自己的 3 帧更近，footprint 被串成 BGA409-26-2323）。
        nearest_prop = _nearest_by_order(
            [(i, p) for i, p in prop_entries if i < idx], idx, window,
        )
        prop: PropertyFrame | None = nearest_prop[1] if nearest_prop else None

        # ── P1-11 D5 修复：形状锚定属性帧（仅 user_attrs 用） ──
        # 帧序实证：[PROP][shape][0x0022][REF]（同组件记录）。位号帧与
        # 属性帧之间隔着 0x0022 描述帧 + shape 帧，直接按位号帧取"最近
        # 属性帧"常错配到下一实例（R190→U12 的 0x02 错位根因）。以最近
        # shape 帧为锚取最近属性帧，承载实例级差异化 user_attrs。
        anchor_prop: PropertyFrame | None = None
        anchor_index: int | None = None
        if nearest_shape is not None:
            # 单向：属性帧在 shape 帧之前（同实例），只向前找——
            # 双向 abs 最近会锚到下一实例（R190→U12 错位根因已修，
            # 串挂 15 例本轮清零）。
            anchored = _nearest_by_order(
                [(i, p) for i, p in prop_entries if i < nearest_shape[0]],
                nearest_shape[0], window,
            )
            if anchored is not None:
                anchor_prop = anchored[1]
                anchor_index = anchored[0]
        prop_anchor = {
            "prop_index": anchor_index,
            "shape_index": nearest_shape[0] if nearest_shape else None,
            "shape": shape,
            "props": dict(anchor_prop.props) if anchor_prop is not None else None,
        }

        # ── Value：0x85 valueIdx 为主 ──
        value: str | None = rd.value
        value_source = "0x85"
        value_idx: int | None = rd.value_idx
        if value is None:
            # 0x02 帧 (632,'Value') 覆盖
            if prop is not None:
                for name_idx, val_idx in prop.prop_pairs:
                    if name_idx == IDX_VALUE and val_idx < len(self.strlst):
                        candidate = self.strlst[val_idx]
                        if candidate and candidate != "NExist" and not candidate.startswith("$") and len(candidate) <= 40:
                            value = candidate
                            value_idx = val_idx
                            value_source = "0x02"
                            break
            # Cache 兜底
            if value is None:
                _, cache_val, _ = self._cache_lookup(shape)
                if cache_val:
                    value = cache_val
                    value_source = "cache"
                    value_idx = None

        # ── Footprint：0x02 [25]→值 为主；PKG_GRP [29] 次之 ──
        footprint: str | None = None
        footprint_source = "none"
        if prop is not None:
            if prop.footprint:
                footprint = prop.footprint
                footprint_source = "0x02"
            elif prop.pkg_grp:
                footprint = prop.pkg_grp
                footprint_source = "0x02_pkg_grp"
        if footprint is None:
            # 同 shape 簇其它 0x02 [25]→值（窗口扩大一倍）
            fp2 = self._find_cluster_footprint(idx, prop_entries, window * 2)
            if fp2 is not None:
                footprint = fp2
                footprint_source = "0x02_cluster"
            else:
                # Packages 兜底
                pkg_fp = self._package_footprint(shape)
                if pkg_fp:
                    footprint = pkg_fp
                    footprint_source = "packages"

        # ── Cache refDes 前缀 ──
        cache_ref, _, cache_src = self._cache_lookup(shape)

        # ── 坐标：P2-A 修正 —— shape 帧取 (x1,y1) bbox 角（EXP 口径）；
        #    缺失时 0x0D PlacedInstance 帧兜底（x 与 y 均兜底）──
        x = shape_coord[0] if shape_coord is not None else rd.x
        y: int | None = shape_coord[1] if shape_coord is not None else None
        if x is None or y is None:
            np_placed = _nearest_by_order(
                [(pi, (px, py)) for pi, px, py in placed_entries], idx, window,
            )
            if np_placed is not None:
                if x is None:
                    x = np_placed[1][0]
                if y is None:
                    y = np_placed[1][1]

        # ── pin_map / pin_names（Packages 绑定） ──
        pin_map, pin_names = self._package_pins(shape)
        # P0-3：pin_label_map（Cache 标号映射）
        pin_label_map = self._bind_pin_labels(shape)

        # 属性字典
        properties: dict[str, str] = {}
        if prop is not None:
            properties = dict(prop.props)
        if value is not None:
            properties.setdefault("VALUE", value)
        if footprint is not None:
            properties.setdefault("PCB Footprint", footprint)

        return InstanceBind(
            refdes=rd.refdes,
            page=page_name,
            value=value,
            value_source=value_source,
            value_idx=value_idx,
            footprint=footprint,
            footprint_source=footprint_source,
            shape=shape,
            cache_ref_des=cache_ref,
            cache_source_lib=cache_src,
            loc_x=x,
            loc_y=y,
            frame_offset=rd.offset,
            frame_type=rd.frame_type,
            properties=properties,
            pin_map=pin_map,
            pin_names=pin_names,
            pin_label_map=pin_label_map,
            prop_anchor=prop_anchor,
            render_origin=shape_render,
            bbox=shape_bbox,
            cache_pin_lines=self._cache_pin_lines_for(
                shape, x, y, None,
            ),
        )

    def _cache_pin_lines_for(
        self, shape: str | None,
        loc_x: int | None, loc_y: int | None,
        render_origin: tuple[int, int] | None = None,
    ) -> list[tuple]:
        """从 Cache 命名引脚定义注入小 IC 引脚线端点（页面 mil）。

        Cache 引脚线坐标为符号局部（如 3A BUCK 的 (70,30)-(90,30)），
        平移实例**渲染原点**（locX/locY，与 symbol_texts 平移一致）；
        渲染原点缺失时回退 bbox 角 (x1,y1)（EXP 口径，容差内近似）。
        仅当实例无 Packages pin_map（小 IC）时注入——大符号（U7/DDR3）
        已有 Packages/标号体系，避免覆盖。

        Args:
            shape: 外形名（如 'R.Normal'/'3A BUCK.Normal'）。
            loc_x, loc_y: 实例坐标（bbox 角，EXP 口径）。
            render_origin: 渲染原点（locX/locY，可能 None）。

        Returns:
            [(pin_name, page_x1, page_y1, page_x2, page_y2)]。
        """
        if not shape or loc_x is None or loc_y is None:
            return []
        if self.cache is None or not getattr(self.cache, "pin_defs", None):
            return []
        # U7/DDR3 等大符号有 Packages pin_map（178/409）但**只有引脚号
        # 无引脚几何**——连接归属仍靠 bbox+叶端点，走线非叶端点的引脚
        # （NFC/UART 等）会漏连（实测 U7 六段连接 393 vs 应有 391+，
        # 08 页 0C 段缺 10 个）。**大符号也注入 Cache 引脚线用于归属**
        # （键名优先 pin_map 球标，无球标时 fallback Cache 功能名）。
        base = shape.rsplit(".", 1)[0]
        # 仅当 Cache 有该符号的引脚几何时注入（无则空）
        has_cache_pins = any(
            getattr(pd_, "symbol", "") == base
            for pd_ in self.cache.pin_defs
        )
        if not has_cache_pins:
            return []
        # 2 引脚无源件（R/C/L/磁珠）也注入——cache pin_defs 定义
        # **A 左 B 右**（R: A(0,10)-(-10,10) 左、B(20,10)-(30,10) 右），
        # 用引脚线端点精确归属可修正位置键的 A/B 方向错误
        # （R202 CLK_25M_N/P 极性反、C230 GND/DVDD3V3 交换，137 个
        # mismatch 根因）。注入后键名用 cache 名（A/B），配合实例
        # 旋转定位。注：R190:A 旧回归是 bbox 竞争所致，引脚线精确
        # 匹配 + 距离阈值 20 已规避。
        ox, oy = render_origin if render_origin is not None else (loc_x, loc_y)
        lines: list[tuple] = []
        for pd_ in self.cache.pin_defs:
            if getattr(pd_, "symbol", "") != base:
                continue
            lines.append((
                pd_.name,
                ox + pd_.x1, oy + pd_.y1,
                ox + pd_.x2, oy + pd_.y2,
            ))
        return lines

    def _find_cluster_footprint(
        self,
        idx: int,
        prop_entries: list[tuple[int, PropertyFrame]],
        window: int,
    ) -> str | None:
        """在同 shape 簇（帧索引窗口）内找任意 0x02 [25]→值。"""
        for cand_idx, pf in prop_entries:
            if abs(cand_idx - idx) <= window and pf.footprint:
                return pf.footprint
        return None

    # ── 全页绑定 + 多单元合并 ──────────────────────────────────────

    def bind_all_pages(
        self,
        pages_frames: list[tuple[str, list[Frame], NetTable]],
    ) -> list[InstanceBind]:
        """绑定全部页面并合并多单元实例（U7 跨 6 页 → 1 实例）。

        Args:
            pages_frames: [(页面名, 帧列表, 网名表)]。

        Returns:
            合并后的 InstanceBind 列表。
        """
        all_binds: list[InstanceBind] = []
        for page_name, frames, net_table in pages_frames:
            binds = self.bind_page(frames, net_table, page_name)
            all_binds.extend(binds)

        # 多单元合并：同 refdes 跨页 → 合并为一个（保留主单元 + source_units）
        merged = self._merge_multi_unit(all_binds)
        return merged

    def _merge_multi_unit(self, binds: list[InstanceBind]) -> list[InstanceBind]:
        """按 refdes 合并多单元实例（U7）。"""
        by_refdes: dict[str, list[InstanceBind]] = defaultdict(list)
        for b in binds:
            by_refdes[b.refdes].append(b)

        result: list[InstanceBind] = []
        for refdes, group in by_refdes.items():
            if len(group) == 1:
                result.append(group[0])
                continue
            # P1-3：GND/电源符号实例（shape='GND_SYMBOL'）是独立放置，
            # 永不按 refdes 合并（同名 GND_n 已全局唯一，防御兜底）。
            if group[0].shape == "GND_SYMBOL":
                result.extend(group)
                continue
            # 2026-08-21 通用化（用户裁决）：同 refdes 多段实例一律
            # 保留独立（不再合并）——OrCAD 多单元 section 各自携带
            # 引脚/图标/Part Reference（如 U7A~U7F 六块独立芯片），
            # 合并会丢失其余段的引脚与连接；下游（con/csa）按
            # refdes+库名段分别渲染，EXP 辅助按页覆盖 Part Reference。
            # 真多单元（Part Reference 相同）如需合并由 exp 辅助按
            # ref_pr 一致时归并（见 exp_dir_assist）。
            result.extend(group)
            continue
            # 多单元：取第一个为主，其余合并
            primary = group[0]
            # pin_map/pin_names：优先取 pinCount 最大的单元（主单元）
            for other in group:
                if len(other.pin_map) > len(primary.pin_map):
                    primary.pin_map = other.pin_map
                    primary.pin_names = other.pin_names
            # P0-3：pin_label_map 取对最多的单元（主单元 0F=89 对）
            for other in group:
                if len(other.pin_label_map) > len(primary.pin_label_map):
                    primary.pin_label_map = other.pin_label_map
            for other in group[1:]:
                # 合并 properties
                for k, v in other.properties.items():
                    primary.properties.setdefault(k, v)
                if primary.value is None and other.value is not None:
                    primary.value = other.value
                    primary.value_source = other.value_source
                    primary.value_idx = other.value_idx
                if primary.footprint is None and other.footprint is not None:
                    primary.footprint = other.footprint
                    primary.footprint_source = other.footprint_source
                # P1-12：合并分片放置（页面级符号文本输出用）
                for sp in other.section_placements:
                    if sp not in primary.section_placements:
                        primary.section_placements.append(sp)
            primary.section = len(group)
            # 记 source_units（extra 信息）
            primary.properties.setdefault(
                "source_units",
                ";".join(f"{o.page}" for o in group),
            )
            result.append(primary)
        return result
