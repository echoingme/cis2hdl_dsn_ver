"""图形结构帧解码器（P2-2 展示维度）。

帧类型（strLst 索引 / 结构类型混合，gold 实证 11_TG0_DDR）：
- 57=arc / 58=ellipse / 59=polygon / 60=polyline / 61=comment_text /
  62=bitmap / 88=bezier / 89=ole_embed。
- 83B 几何帧（含 'BISHEET'/'OFFSHEET2' 名 + dbId + 坐标 + color）；
- 45B 引用帧（坐标引用，与引脚点帧同构）。

83B 布局（gold 实证，与 shape 帧同构零化模式）::

    [8B DELIM][type:u16][00 00][?:u32]
    [len:u16][name][00][dbId:u32][??:u32]
    [y2:i16][x2:i16][x1:i16][y1:i16][30 00][color:u8]...

输出 ``GraphicShapeElement``：kind/name/x/y/x2/y2/color/offset。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

logger = logging.getLogger(__name__)

#: 图形结构帧类型 → 原语类型
SHAPE_KIND: dict[int, str] = {
    57: "arc",
    58: "ellipse",
    59: "polygon",
    60: "polyline",
    61: "comment_text",
    62: "bitmap",
    88: "bezier",
    89: "ole_embed",
}

#: 图形结构帧类型集合
GRAPHIC_SHAPE_TYPES = frozenset(SHAPE_KIND.keys())

#: 坐标上限
_MAX_COORD = 30000
#: 83B 几何帧最小长度
_GEOM_MIN_LEN = 60
#: 名称 lstr 候选（BISHEET/OFFSHEET2 等可打印串）
_NAME_RE = re.compile(rb"([\x20-\x7e]{2,})\x00")


@dataclass
class GraphicShapeElement:
    """图形结构帧解码结果。

    Attributes:
        offset: 帧页内偏移。
        frame_type: 源帧类型。
        kind: 原语类型（arc/ellipse/polygon/polyline/comment_text/bitmap/
            bezier/ole_embed）。
        name: 帧内名称（如 'BISHEET'；无则空）。
        x, y: 位置（x1/y1，best-effort）。
        x2, y2: 尺寸/右上（best-effort）。
        color: 颜色（best-effort）。
    """

    offset: int
    frame_type: int
    kind: str
    name: str = ""
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    color: int = 0

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "kind": self.kind,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "x2": self.x2,
            "y2": self.y2,
            "color": self.color,
        }


def _scan_i16_pairs(rec: bytes, start: int) -> list[tuple[int, int]]:
    """扫描 i16 坐标对（best-effort，过滤越界）。"""
    result: list[tuple[int, int]] = []
    for off in range(start, max(start, len(rec) - 3)):
        x = int.from_bytes(rec[off:off + 2], "little", signed=True)
        y = int.from_bytes(rec[off + 2:off + 4], "little", signed=True)
        if 0 < x < _MAX_COORD and 0 < y < _MAX_COORD:
            result.append((x, y))
    return result


def _find_color(rec: bytes, coord_region_end: int) -> int:
    """在坐标区附近找颜色（30 00 标记后 u8）。"""
    for off in range(max(0, coord_region_end - 12), min(len(rec) - 2, coord_region_end + 12)):
        if rec[off] == 0x30:
            cand = rec[off + 2] if off + 2 < len(rec) else 0
            if 0 < cand < 0x80:
                return cand
    return 0


def parse_graphic_shape_frame(
    frame: Frame, ctx: DecodeContext,
) -> GraphicShapeElement | None:
    """解析图形结构帧（83B 几何帧；45B 引用帧也输出坐标）。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        GraphicShapeElement；非图形结构帧返回 None。
    """
    rec = frame.raw
    if frame.frame_type not in GRAPHIC_SHAPE_TYPES:
        return None
    kind = SHAPE_KIND[frame.frame_type]
    if len(rec) < 18:
        return None
    # 名称（BISHEET/OFFSHEET2 等；几何帧内含）
    name = ""
    m = _NAME_RE.search(rec, 14)
    if m:
        name = m.group(1).decode("latin1")
    # 坐标：几何帧从名称后读 dbId + i16 对；引用帧直接扫 i16 对
    if len(rec) >= _GEOM_MIN_LEN and name:
        start = m.end() + 4  # dbId 之后
        pts = _scan_i16_pairs(rec, start)
        color = _find_color(rec, start + 12)
    else:
        pts = _scan_i16_pairs(rec, 10)
        color = 0
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return GraphicShapeElement(
        offset=frame.offset,
        frame_type=frame.frame_type,
        kind=kind,
        name=name,
        x=min(xs),
        y=min(ys),
        x2=max(xs),
        y2=max(ys),
        color=color,
    )


def scan_graphic_shape_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> list[GraphicShapeElement]:
    """扫描一页全部图形结构帧。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        GraphicShapeElement 列表（按帧偏移升序）。
    """
    result: list[GraphicShapeElement] = []
    for frame in frames:
        el = parse_graphic_shape_frame(frame, ctx)
        if el is not None:
            result.append(el)
    result.sort(key=lambda e: e.offset)
    return result


def register_graphic_shape_decoders() -> None:
    """注册图形结构帧解码器（结构表，仅覆盖既有枚举范围）。"""
    from .registry import register_struct

    for ftype in sorted(GRAPHIC_SHAPE_TYPES):
        register_struct(ftype, parse_graphic_shape_frame)
