from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ElectricalType(str, Enum):
    """Unified electrical type enumeration — shared by all formats."""

    INPUT = "INPUT"
    OUTPUT = "OUTPUT"
    BIDIR = "BIDIR"
    POWER = "POWER"
    GROUND = "GROUND"
    PASSIVE = "PASSIVE"
    NC = "NC"
    TRI_STATE = "TRI_STATE"
    OPEN_COLLECTOR = "OPEN_COLLECTOR"


class PinDef(BaseModel):
    """Unified pin definition — all formats map here."""

    number: str
    name: str = ""
    type: ElectricalType = ElectricalType.PASSIVE
    is_power: bool = False  # Auto-set from type; use model_post_init
    position: Optional[tuple[float, float]] = None

    def model_post_init(self, _context: object) -> None:
        """Auto-detect is_power from POWER/GROUND types."""
        if self.type in (ElectricalType.POWER, ElectricalType.GROUND):
            object.__setattr__(self, "is_power", True)


class ComponentDef(BaseModel):
    """Unified component library definition.

    Represents a component *in the library*, not a schematic instance.
    ComponentInstanceIR references this definition.
    """

    library_id: str
    part_name: str
    #: 匹配源位号（dsn_ver 源构建用；库定义场景默认空——源组件
    #: ComponentDef 由 source_builder 携带实例 refdes 供匹配器
    #: 类型假设与路由使用，避免退化用库名当位号）。
    refdes: str = ""

    category: str = ""
    phys_des_prefix: str = ""  # PHYS_DES_PREFIX from chips.prt (e.g. 'U', 'IC', 'XS')
    footprint: str = ""
    footprint_alt: list[str] = Field(default_factory=list)

    pins: list[PinDef] = Field(default_factory=list)
    pin_count: int = 0

    # ── P1-12: 符号文本元素（Cache 0x2e2e/0x001c；芯片图标全文本）──
    symbol_texts: list[dict] = Field(default_factory=list)
    """符号几何文本（Cache 流解析，OLB 仅反推参考）。每条 dict:
    ``{"symbol": str, "text": str, "x": int, "y": int, "x2": int,
    "y2": int, "font": int, "attr": int, "offset": int,
    "kind": str}``。只含**该符号自己**的文本（按 part 名精确匹配）。"""
    # ── R-GEO: 符号几何（0x2929 体框 + 0x2c2c 引脚线，mock 1:1 输入）──
    symbol_geometry: dict = Field(default_factory=dict)
    """符号几何（R-GEO 对接层聚合，供 mock 1:1 复刻）。结构：
    ``{"body_box": [x1,y1,x2,y2]|None, "pin_lines": [[x1,y1,x2,y2,name]...],
    "texts": [...], "header_attrs": [...], "geometry_source": "cache"}``。
    坐标保持符号局部 mil（页面输出 = 实例 loc + 平移/旋转）。"""
    # ── P1-11: 用户属性（Library 流 INS → 组件关联）───────────────
    user_attrs: dict[str, str] = Field(default_factory=dict)
    """用户属性键值（Library 流字符串表解析；如 COMPONENT_HIGH_MAX、
    额定工作电压(V)、额定功率(W)、阻值允许偏差、描述）。"""

    value: str = ""
    tolerance: str = ""
    mpn: str = ""
    description: str = ""

    bom_seq: str = ""
    sn_num: str = ""

    sections: int = 1
    section_pin_maps: dict[int, list[str]] = Field(default_factory=dict)

    symbols: list[dict] = Field(default_factory=list)

    source_format: str = ""
    source_file: str = ""

    #: Arbitrary extra data — used to store ptf_rows, cross_ref info, etc.
    extra_data: dict = Field(default_factory=dict)

    def model_post_init(self, _context: object) -> None:
        if not self.pin_count and self.pins:
            object.__setattr__(self, "pin_count", len(self.pins))

    @property
    def fingerprint(self) -> str:
        """Matching fingerprint: footprint + value + pin count."""
        return f"{self.footprint}|{self.value}|{self.pin_count}"


class ComponentInstanceIR(BaseModel):
    """Unified component instance — a component placed on a schematic page.

    Separate from ComponentDef: ComponentDef is the library definition,
    ComponentInstanceIR is a specific instance on a page.
    Multiple instances can reference the same ComponentDef.
    """

    refdes: str
    library_id: str
    section: int = 1

    loc_x: int = 0
    loc_y: int = 0
    rotation: int = 0
    #: Mirror flag — EDIF ``(orientation MY/MX/...)``; 0 = none,
    #: 1 = X-axis mirror, 2 = Y-axis mirror (P1-4).  Only meaningful
    #: when the placement transform carried an explicit orientation.
    mirror: int = 0

    value_override: str = ""
    properties: dict[str, str] = Field(default_factory=dict)
    # ── P1-11 修复（QA D5）：实例级用户属性 ────────────────────
    #: 实例级用户属性键值（优先取自本实例 0x02 属性帧；兜底
    #: 库级 ComponentDef.user_attrs）。EXP 交叉验证按实例逐条比对，
    #: 解决"库级众数无法承载差异化实例值"的 D5 缺陷。
    user_attrs: dict[str, str] = Field(default_factory=dict)
    pin_connections: dict[str, str] = Field(default_factory=dict)
    #: No-connect pins — pin names whose net is "NC" (P1-4).  These are
    #: stored so writers can emit NC flags / skip dangling pins.
    nc_pins: set[str] = Field(default_factory=set)
    #: 引脚标号映射（DSN Cache 标号表；标号 → 引脚名，如 AA23 → Y21）。
    #: 带默认值，EDIF/OLB 等其它 parser 不受影响（P0 DSN 补齐）。
    pin_labels: dict[str, str] = Field(default_factory=dict)
    extra_data: dict = Field(default_factory=dict)
