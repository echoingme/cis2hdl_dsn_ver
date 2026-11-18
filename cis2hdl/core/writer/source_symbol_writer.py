"""Source Symbol 复刻（§9.2，用户裁决 ④）。

``PageIR.source_symbols``（Power 28 / Onsheet 4 / Ground 10 = 42）按
DSN **原始坐标**放置（删除旧版"堆右上角"逻辑；无坐标项按网名回填）：

- ``power_symbol`` → 电源符号放置（FORCEADD 电源网名元件）；
- ``ground_symbol`` → hdl_lib GND 元件（FORCEADD，坐标直通）；
- ``onsheet/offsheet`` → 页面连接符（IOPORT 块，网名标注于网标坐标）。

纯函数 + 薄数据类：产出放置计划（供 CSAWriter/后续编排消费），
不触碰文件系统，不感知 matcher 内部。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class SourceSymbolPlan:
    """单个 source symbol 的放置计划。"""

    kind: str                 # power_symbol / ground_symbol / onsheet / offsheet
    name: str = ""            # 符号名（POWER_SYMBOL / ONSHEET2 / ...）
    net: str = ""             # 关联网名（12V0 / 3V3_SOC / GND ...）
    x: int = 0
    y: int = 0
    rotation: int = 0
    mirror: int = 0
    page_id: str = ""
    target_pages: list = field(default_factory=list)  # OFFSHEET 目标页
    has_coords: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "name": self.name, "net": self.net,
            "x": self.x, "y": self.y, "rotation": self.rotation,
            "mirror": self.mirror, "page_id": self.page_id,
            "target_pages": list(self.target_pages),
            "has_coords": self.has_coords,
        }


#: source symbol 语义映射（DEHDL 输出类）。
_KIND_TO_OUTPUT = {
    "power_symbol": "POWER",
    "ground_symbol": "GND",
    "onsheet": "ONSHEET",
    "offsheet": "OFFSHEET",
}


def build_source_symbol_plan(page) -> list[SourceSymbolPlan]:
    """页面 source_symbols → 放置计划列表（DSN 坐标直通）。

    坐标缺失项（y 缺失等）按网名回填（§9.2.1：37/42 有坐标；回填
    策略：同网名其它实例坐标均值，无参考则 0 并标记 has_coords=False）。
    """
    raw = []
    for s in page.source_symbols or []:
        kind = str(s.get("kind", "")).lower()
        if kind not in _KIND_TO_OUTPUT:
            continue
        x = int(s.get("x", 0) or 0)
        y = int(s.get("y", 0) or 0)
        has = (s.get("x") is not None and s.get("y") is not None
               and (s.get("x") != 0 or s.get("y") != 0))
        raw.append(SourceSymbolPlan(
            kind=kind,
            name=str(s.get("name", "") or ""),
            net=str(s.get("net", "") or ""),
            x=x, y=y,
            rotation=int(s.get("rotation", 0) or 0),
            mirror=int(s.get("mirror", 0) or 0),
            page_id=str(s.get("page", "") or getattr(page, "page_id", "")),
            target_pages=list(s.get("target_pages", []) or []),
            has_coords=has,
        ))
    # 同网名均值回填（docstring 承诺；QA F3）：has_coords=False 的项
    # 用同 kind+网名其它实例坐标均值补位。
    by_key: dict[tuple, list[tuple[int, int]]] = {}
    for p in raw:
        if p.has_coords:
            by_key.setdefault((p.kind, p.net), []).append((p.x, p.y))
    for p in raw:
        if p.has_coords:
            continue
        refs = by_key.get((p.kind, p.net), [])
        if refs:
            p.x = int(sum(x for x, _ in refs) / len(refs))
            p.y = int(sum(y for _, y in refs) / len(refs))
    return raw


def build_all_source_symbol_plan(ir) -> list[SourceSymbolPlan]:
    """全页 source symbols → 放置计划（42 个）。"""
    plans: list[SourceSymbolPlan] = []
    for page in ir.pages:
        plans.extend(build_source_symbol_plan(page))
    return plans


def to_forceadd_lines(
    plans: list[SourceSymbolPlan], gnd_cell: str = "GND"
) -> list[str]:
    """放置计划 → FORCEADD 指令行（§9.2.2 输出策略）。

    - ground_symbol → ``FORCEADD GND <cell> (x y) rot``；
    - power_symbol → ``FORCEADD POWER <net> (x y) rot``；
    - onsheet/offsheet → ``IOPORT <net> (x y)``（页面连接符块）。

    Args:
        plans: 放置计划。
        gnd_cell: hdl_lib GND 元件名（默认 GND）。

    Returns:
        CSA 指令行列表。
    """
    lines: list[str] = []
    for p in plans:
        rot = _dehdl_rotation(p.rotation)
        mir = " M" if p.mirror else ""
        if p.kind == "ground_symbol":
            net = p.net or "GND"  # 网名回填（ground 网名统一 GND）
            lines.append(
                f"FORCEADD {gnd_cell} {net} ({p.x} {p.y}) {rot}{mir}"
            )
        elif p.kind == "power_symbol":
            net = p.net or p.name or "PWR"
            lines.append(
                f"FORCEADD POWER_{net} {net} ({p.x} {p.y}) {rot}{mir}"
            )
        else:  # onsheet / offsheet 连接符
            lines.append(f"IOPORT {p.net or p.name} ({p.x} {p.y})")
    return lines


def source_symbol_stats(ir) -> dict:
    """source symbols 统计（QA 用）。"""
    plans = build_all_source_symbol_plan(ir)
    kinds: dict[str, int] = {}
    no_coord = 0
    for p in plans:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
        if not p.has_coords:
            no_coord += 1
    return {"total": len(plans), "kinds": kinds, "no_coord": no_coord}


def _dehdl_rotation(rot: int) -> int:
    """DSN 旋转 → DEHDL 旋转（0/90/180/270 透传，负值归一）。"""
    return int(rot) % 360
