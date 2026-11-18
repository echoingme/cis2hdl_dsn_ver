from __future__ import annotations

from enum import Enum
from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field

from ..db.component_db import ComponentDB
from .component import ComponentInstanceIR


class NetCategory(str, Enum):
    """ISCF 4-class network classification.

    Cadence internal exchange format (ISCF) classifies nets into:
        FLAT   — ordinary signal nets (BEGIN_NETS)
        GROUND — ground nets (BEGIN_GROUND)
        POWER  — power nets (BEGIN_POWER)
        BUS    — bus nets (BEGIN_BUSES)
    """

    FLAT = "FLAT"
    GROUND = "GROUND"
    POWER = "POWER"
    BUS = "BUS"


class NetConnection(BaseModel):
    """A single pin-to-net connection.

    Describes which pin of which component instance is connected to a net.
    """

    refdes: str
    pin_number: str


class NetIR(BaseModel):
    """Unified net definition using Cadence ISCF 4-class model."""

    name: str
    category: NetCategory = NetCategory.FLAT
    #: 网络序号（DSN net table dict key；R-NETID，flatnet ID 448 唯一对齐）
    net_id: int | None = None
    connections: list[NetConnection] = Field(default_factory=list)
    is_bus: bool = False
    bus_members: list[str] = Field(default_factory=list)
    # ── Phase XI P0-A1: wire geometry owned by this net ──────────────
    wires: list[WireSegment] = Field(default_factory=list)
    """Wire segments (EDIF figure WIRE polylines / DSN Wire) belonging to
    this net.  Populated by EDIFParser._parse_net (Phase XI P0-A1)."""

    @property
    def connection_signature(self) -> frozenset[str]:
        """返回网络连接的标准化签名（frozenset of "refdes.pin"）。

        用于跨来源网络拓扑比对——两个来源的 net 名称可能不同，
        但连接同一组器件/引脚时签名相同。
        """
        return frozenset(
            f"{conn.refdes}.{conn.pin_number}"
            for conn in self.connections
        )


class WireSegment(BaseModel):
    """A wire segment with coordinates.

    Supports both single-segment (DSN Wire §7.6, ``start_x/y`` + ``end_x/y``)
    and polyline (EDIF ``figure WIRE (path (pointList (pt x y) ...))``,
    ``points``) representations.  ``net_name`` carries the owning net when
    known (from DSN Net Name Table or EDIF net context); otherwise it is
    empty and the connection is established by coordinate overlap in the
    CSA/connectivity layer.

    Note: the historical docstring "EDIF does not contain coordinates" was
    incorrect — EDIF 2 0 0 ``(figure WIRE (path (pointList ...)))`` does
    contain full polyline coordinates (verified on HG5015-BE36_V10.EDF:
    2516 wire figures / 4257 points).  See Phase XI P0-A1.
    """

    start_x: int = 0
    start_y: int = 0
    end_x: int = 0
    end_y: int = 0
    net_name: str = ""
    # ── v1.2.0 (Phase XI P0-A1): polyline support ──────────────────
    points: list[tuple[int, int]] = Field(default_factory=list)
    """Full polyline vertex list from EDIF figure WIRE (ordered).  When
    non-empty, it is authoritative over start/end (which mirror the first
    and last points for backward compatibility)."""
    page_id: str = ""
    """Owning page id (e.g. '1.5'); used to route wires to the correct
    CSA page during generation."""
    # ── P2-4 走线样式（best-effort；默认 0/0/0）──────────────────
    color: int = 0
    """走线颜色（DSN StructWire 坐标块后 u32；默认 0）。"""
    line_width: int = 0
    """走线线宽（默认 0）。"""
    line_style: int = 0
    """走线线型（默认 0）。"""

    def __init__(self, **data: object) -> None:
        if data.get("points"):
            pts = list(data["points"])  # type: ignore[arg-type]
            data["start_x"] = pts[0][0]
            data["start_y"] = pts[0][1]
            data["end_x"] = pts[-1][0]
            data["end_y"] = pts[-1][1]
        super().__init__(**data)


class PageIR(BaseModel):
    """A single schematic page (or hierarchical sheet)."""

    page_id: str = "1.1"
    page_name: str = ""
    width: int = 3520
    height: int = 2720
    instances: list[ComponentInstanceIR] = Field(default_factory=list)
    nets: list[NetIR] = Field(default_factory=list)
    wires: list[WireSegment] = Field(default_factory=list)
    ports: list[dict] = Field(default_factory=list)
    # ── Phase XI P0-A3: off-page / cross-page connectors ─────────────
    off_pages: list[dict] = Field(default_factory=list)
    """Off-page / cross-page connector references (EDIF portRef without
    instanceRef, or DSN OffPageConnector §7.8).  Each dict: ``{"name": ...,
    "net_name": ..., "x": ..., "y": ...}`` — used for cross-page net
    validation and CSA IOPORT generation (P0-C5)."""
    graphic_elements: list[dict] = Field(default_factory=list)
    """页面级图形元素（文本、线条、矩形等）。用于信息页（Cover_Page,
    Block_Diagram, Clock_Tree, Power_Tree）的 TitleBlock/GraphicInst 文本提取。"""
    # ── P0 (DSN 补齐): 芯片引脚分区表 / 全页面文字元素 ──────────────
    pin_partitions: list[dict] = Field(default_factory=list)
    """芯片引脚分区表（DSN 0x0867 帧）。每页 1 帧，含引脚分区/引脚名/
    硬件配置字表格。每条 dict: ``{"offset": int, "text": str,
    "encoding": str, "x": int|None, "y": int|None}``。"""
    text_elements: list[dict] = Field(default_factory=list)
    """全页面文字元素（DSN 0x0000 帧，含 GBK 中文）。每条 dict:
    ``{"offset": int, "x": int, "y": int, "x2": int, "y2": int,
    "text": str, "font": int, "attr": int, "rotation": int,
    "encoding": str}``。"""
    # ── P1-10: source symbols（电源/接地符号、跨页/同页连接符）──────
    source_symbols: list[dict] = Field(default_factory=list)
    """Source symbol（电源/接地/跨页连接符）。每条 dict:
    ``{"kind": str, "name": str, "x": int, "y": int, "page": str,
    "offset": int, "frame_type": str, "target_pages": list,
    "object_id": int}``。"""
    # ── P1-7: 同页无电线连接点 ──────────────────────────────────────
    connection_points: list[dict] = Field(default_factory=list)
    """同页无电线连接点（0x03/0x05/0x06 短帧）。每条 dict:
    ``{"offset": int, "frame_type": str, "kind": str, "x": int,
    "y": int, "object_id": int, "net_name": str}``。"""
    # ── P1-12: 页面级符号文本（实例平移/旋转后）────────────────────
    symbol_texts: list[dict] = Field(default_factory=list)
    """页面级符号文本（组件符号文本经实例 loc/rotation 变换后输出）。
    每条 dict: ``{"refdes": str, "symbol": str, "text": str, "x": int,
    "y": int, "rotation": int, "mirror": int, "font": int, "attr": int}``。"""

    def add_instance(self, inst: ComponentInstanceIR) -> None:
        self.instances.append(inst)

    def add_net(self, net: NetIR) -> None:
        self.nets.append(net)


class FrameStats(BaseModel):
    """P2 帧覆盖率统计结构（metadata.frame_stats）。

    页面流帧覆盖率口径：covered = 类型号 ∈ 已注册解码器集合；
    unknown = 类型号无解码器（UnknownFrame / 静默保留）。
    ``top_unknown_types`` 为未覆盖类型号 Top N（strLst 索引，数据相关），
    供后续补全计划定位最高价值帧族。
    """

    total_frames: int = 0
    covered_frames: int = 0
    unknown_frames: int = 0
    covered_pct: float = 0.0
    top_unknown_types: dict[str, int] = Field(default_factory=dict)


#: 解码器版本（P2 基础设施；随解码器集合演进递增）
DEFAULT_DECODER_VERSION = "1.2.0"

#: frame_stats 默认结构（其它解析器不设置时可注入）
_FRAME_STATS_DEFAULTS: dict = {
    "total_frames": 0,
    "covered_frames": 0,
    "unknown_frames": 0,
    "covered_pct": 0.0,
    "top_unknown_types": {},
}


def default_frame_stats() -> dict:
    """返回 frame_stats 默认 dict（防御：metadata 缺省可注入）。"""
    return dict(_FRAME_STATS_DEFAULTS)


def ensure_p2_metadata(metadata: dict) -> dict:
    """为 metadata 注入 P2 基础设施默认键（frame_stats/decoder_version/warnings）。

    不覆盖已存在的值；用于 EDIF/OLB 等其它解析器创建的 DesignIR 也具备
    P2 metadata 结构（Pydantic 默认值语义，向后兼容）。
    """
    metadata.setdefault("frame_stats", default_frame_stats())
    metadata.setdefault("decoder_version", DEFAULT_DECODER_VERSION)
    metadata.setdefault("warnings", [])
    return metadata


class DesignIR(BaseModel):
    """Top-level design IR — the unified output of all parsers."""

    project_name: str = ""
    source_format: str = "CIS"
    pages: list[PageIR] = Field(default_factory=list)
    component_db: ComponentDB = Field(default_factory=lambda: _default_db())
    global_nets: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    def add_page(self, page: PageIR) -> None:
        self.pages.append(page)

    def invalidate_caches(self) -> None:
        """Invalidate all cached_property values (Phase XII R1).

        ``cached_property`` results are stored in the instance ``__dict__``.
        Call this after mutating ``pages[].instances`` / ``pages[].nets``
        (e.g. after the ConversionEngine rebuilds page instances from the
        ComponentCatalog) so that ``all_instances`` / ``all_nets`` /
        ``instance_refdes_set`` are recomputed on next access.
        """
        for _key in ("all_instances", "all_nets", "instance_refdes_set"):
            self.__dict__.pop(_key, None)

    @cached_property
    def all_instances(self) -> list[ComponentInstanceIR]:
        result: list[ComponentInstanceIR] = []
        for page in self.pages:
            result.extend(page.instances)
        return result

    @cached_property
    def all_nets(self) -> list[NetIR]:
        result: list[NetIR] = []
        for page in self.pages:
            result.extend(page.nets)
        return result

    @property
    def total_pins(self) -> int:
        return sum(len(net.connections) for net in self.all_nets)

    @cached_property
    def instance_refdes_set(self) -> set[str]:
        """所有实例的 refdes 集合（跨页面）。"""
        result: set[str] = set()
        for page in self.pages:
            for inst in page.instances:
                result.add(inst.refdes)
        return result

    def instances_by_refdes(self) -> dict[str, 'ComponentInstanceIR']:
        """refdes → 对应 ComponentInstanceIR 的映射。

        如果多个页面有同名 refdes，返回遇到的第一个。
        """
        result: dict[str, 'ComponentInstanceIR'] = {}
        for page in self.pages:
            for inst in page.instances:
                if inst.refdes not in result:
                    result[inst.refdes] = inst
        return result

    def net_connection_map(self) -> dict[str, frozenset[str]]:
        """返回 net_name → connection_signature 的映射（跨页面）。"""
        result: dict[str, frozenset[str]] = {}
        for page in self.pages:
            for net in page.nets:
                result[net.name] = net.connection_signature
        return result

    def instances_by_type(self) -> dict[str, list['ComponentInstanceIR']]:
        """按器件类型分组返回实例列表。

        分类规则（基于 library_id 前缀）：
          - RES* → "Resistor"
          - CAP* → "Capacitor"
          - IND* / BEAD* / FERRITE* → "Inductor"
          - DIODE* / LED* / TVS* → "Diode"
          - XTAL* / CRYSTAL* / OSC* → "Crystal"
          - CONN* / J* / HEADER* → "Connector"
          - IC* / U* / RTL* → "IC"
          - 其他 → "Other"
        """
        import re

        categories: dict[str, list['ComponentInstanceIR']] = {}

        TYPE_RULES = [
            (r"^(RES|R_|RESISTOR)", "Resistor"),
            (r"^(CAP|C_|CAPACITOR|CAPSYM)", "Capacitor"),
            (r"^(IND|INDUCTOR|BEAD|FERRITE|L_)", "Inductor"),
            (r"^(DIODE|LED|TVS|D_|ZENER)", "Diode"),
            (r"^(XTAL|CRYSTAL|OSC|Y_)", "Crystal"),
            (r"^(CONN|J[A-Z]?$|HEADER|JACK|SOCKET)", "Connector"),
            (r"^(IC|U[A-Z]?$|RTL8|BCM|MTK|QCA|IPQ)", "IC"),
        ]

        for page in self.pages:
            for inst in page.instances:
                category = "Other"
                for pattern, cat in TYPE_RULES:
                    if re.match(pattern, inst.library_id.upper()):
                        category = cat
                        break
                categories.setdefault(category, []).append(inst)

        return categories


# ------------------------------------------------------------------
#  Rebuild Pydantic models to resolve forward references
#  (required by Pydantic v2 when 'from __future__ import annotations' is active)
# ------------------------------------------------------------------

PageIR.model_rebuild()
DesignIR.model_rebuild()


def _default_db() -> ComponentDB:
    from ..db.component_db import ComponentDB

    return ComponentDB()
