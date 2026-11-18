"""走线帧解码器（架构设计 §2 L3 + QA A.4）。

QA 实证：帧内 ``0x30 标记 + 4×int32`` 坐标（x1,y1,x2,y2）+ netid。
```python
idx = rec.find(b"\\x30\\x00\\x00\\x00", 8)
for net_off2 in (idx - 4, idx + 4):
    netid = u32(rec, net_off2)
    xs = [s32(rec, idx + 4 + 4*k) for k in range(4)]
```
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    from .wire_topology import PinPointRecord, WirePolyline

_COORD_MIN, _COORD_MAX = -400000, 400000
_MARKER = b"\x30\x00\x00\x00"


@dataclass
class WireRecord:
    """一条走线段。"""

    offset: int
    net_id: int | None
    x1: int
    y1: int
    x2: int
    y2: int
    # ── P2-4 走线样式（best-effort；默认 0/0/0）──────────────────
    #: 颜色（坐标块后 u32；默认 0）
    color: int = 0
    #: 线宽（坐标块后第二个 u32；默认 0）
    line_width: int = 0
    #: 线型（坐标块后第三个 u32；默认 0）
    line_style: int = 0

    @property
    def points(self) -> list[tuple[int, int]]:
        return [(self.x1, self.y1), (self.x2, self.y2)]


@dataclass
class WireScanResult:
    """一页的走线扫描结果。"""

    wires: list[WireRecord] = field(default_factory=list)
    net_ids: list[int] = field(default_factory=list)
    #: P0-4：折线链（build_wire_polylines 输出）
    polylines: list["WirePolyline"] = field(default_factory=list)
    #: P0-5：引脚点记录（collect_pin_points 输出）
    pin_points: list["PinPointRecord"] = field(default_factory=list)


def _s32(data: bytes, off: int) -> int | None:
    if off + 4 > len(data):
        return None
    v = int.from_bytes(data[off:off + 4], "little")
    return v - 0x100000000 if v >= 0x80000000 else v


def parse_wire_frame(frame: Frame, ctx: DecodeContext) -> WireRecord | None:
    """从单帧提取一条走线段（若该帧含 0x30 坐标标记）。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留接口）。

    Returns:
        WireRecord；无走线标记返回 None。
    """
    rec = frame.raw
    if len(rec) < 40:
        return None
    idx = rec.find(_MARKER, 8)
    if idx == -1 or idx < 16:
        return None
    net_id: int | None = None
    for net_off in (idx - 4, idx + 4):
        v = _u32(rec, net_off)
        if v is not None:
            net_id = v
            break
    xs_off = idx + 4
    xs = [_s32(rec, xs_off + 4 * k) for k in range(4)]
    if any(v is None or not (_COORD_MIN <= v <= _COORD_MAX) for v in xs):
        return None
    x1, y1, x2, y2 = xs
    if (x1 == x2 and y1 == y2):
        return None
    if not ((x1 == x2) or (y1 == y2)):
        return None  # 非正交，不构成标准走线段
    # P2-4 走线样式：坐标块后 [color:u32][lineWidth:u32][lineStyle:u32]
    # （StructWire 布局 [id][color][start][end][...][lineWidth][lineStyle]，
    #  坐标块即 start/end，其后为 lenAliases 区；防御性读取 + 合理域校验，
    #  避免把 marker/别名误读为大噪声值）。
    raw_color = _u32(rec, xs_off + 16) or 0
    raw_width = _u32(rec, xs_off + 20) or 0
    raw_style = _u32(rec, xs_off + 24) or 0
    color = raw_color if raw_color < 0x1000000 else 0
    line_width = raw_width if raw_width < 1000 else 0
    line_style = raw_style if raw_style < 1000 else 0
    return WireRecord(
        offset=frame.offset, net_id=net_id, x1=x1, y1=y1, x2=x2, y2=y2,
        color=color, line_width=line_width, line_style=line_style,
    )


def scan_wire_frames(frames: list[Frame], ctx: DecodeContext) -> WireScanResult:
    """扫描一页全部走线段（QA A.4 方法）。"""
    result = WireScanResult()
    for frame in frames:
        rec = parse_wire_frame(frame, ctx)
        if rec is not None:
            result.wires.append(rec)
            if rec.net_id is not None:
                result.net_ids.append(rec.net_id)
    return result


def _u32(data: bytes, off: int) -> int | None:
    return int.from_bytes(data[off:off + 4], "little") if off + 4 <= len(data) else None
