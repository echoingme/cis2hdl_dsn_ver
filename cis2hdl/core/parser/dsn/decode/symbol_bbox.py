"""SymbolBBox 解码器（P2-3 展示维度）。

结构类型 0x2C SymbolBBox 83B 几何帧（gold 实证）::

    [8B DELIM][2c 00][00 00][?:u32][len:u16][BISHEET][00]
    [dbId:u32][locY:i16][locX:i16][y2:i16][x2:i16][x1:i16][y1:i16]
    [30 00][color:u8]...

- workdir 口径：dbId 后直接 6×i16（470,1200,490,1220,1200,470），
  前 4 个即 bbox (x1,y1,x2,y2)；
- gold 口径：locY/locX 清零（与 shape 帧同源零化），y2/x2 为符号
  体框尺寸（20×20），x1/y1 为位置锚点 → bbox = (x1, y1, x1+x2, y1+y2)。

输出 ``SymbolBBoxElement``：bbox (x1,y1,x2,y2)/name/color/offset。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

logger = logging.getLogger(__name__)

#: SymbolBBox 结构类型
SYMBOL_BBOX_TYPE = 0x2C
#: 坐标上限
_MAX_COORD = 30000
#: 83B 几何帧最小长度
_GEOM_MIN_LEN = 60


@dataclass
class SymbolBBoxElement:
    """SymbolBBox 解码结果。

    Attributes:
        offset: 帧页内偏移。
        name: 帧内名称（如 'BISHEET'）。
        x1, y1, x2, y2: bbox（页面 mil）。
        color: 颜色。
    """

    offset: int
    name: str = ""
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    color: int = 0

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "name": self.name,
            "x1": self.x1,
            "y1": self.y1,
            "x2": self.x2,
            "y2": self.y2,
            "color": self.color,
        }


def parse_symbol_bbox(frame: Frame, ctx: DecodeContext) -> SymbolBBoxElement | None:
    """解析 SymbolBBox 83B 几何帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        SymbolBBoxElement；非 0x2C 几何帧或坐标非法返回 None。
    """
    if frame.frame_type != SYMBOL_BBOX_TYPE:
        return None
    rec = frame.raw
    if len(rec) < _GEOM_MIN_LEN:
        return None
    # 名称 lstr
    name = ""
    m = re.search(rb"([\x20-\x7e]{2,})\x00", rec[12:])
    if m:
        name = m.group(1).decode("latin1")
    # 名称后 dbId + i16 序列（名称 lstr 结束（含 \0）处）
    if name:
        start = 12 + m.end()  # rec[12:] 中 m.end() 为名称后位置；+12 为绝对
    else:
        start = 14
    if start + 4 > len(rec):
        return None
    vals: list[int] = []
    off = start + 4  # dbId 之后
    while off + 2 <= len(rec) and len(vals) < 10:
        v = int.from_bytes(rec[off:off + 2], "little", signed=True)
        vals.append(v)
        off += 2
    if len(vals) < 6:
        return None
    # workdir：前 4 个即 bbox
    a, b, c, d = vals[0], vals[1], vals[2], vals[3]
    if 0 < a < _MAX_COORD and 0 < b < _MAX_COORD and c > a and d > b:
        bbox = (a, b, c, d)
    elif 0 < vals[4] < _MAX_COORD and 0 < vals[5] < _MAX_COORD:
        # gold：loc 清零，x1/y1 在 vals[4]/vals[5]，y2/x2 为符号体框尺寸
        ax, ay = vals[4], vals[5]
        size_x = vals[2] if 0 < vals[2] < 1000 else 0
        size_y = vals[3] if 0 < vals[3] < 1000 else 0
        bbox = (ax, ay, ax + size_x, ay + size_y)
    else:
        return None
    # 颜色（30 00 标记后 u8）
    color = 0
    mi = rec.find(b"\x30\x00", start + 4)
    if mi != -1 and mi + 2 < len(rec):
        cand = rec[mi + 2]
        if 0 < cand < 0x80:
            color = cand
    return SymbolBBoxElement(
        offset=frame.offset,
        name=name,
        x1=bbox[0],
        y1=bbox[1],
        x2=bbox[2],
        y2=bbox[3],
        color=color,
    )


def scan_symbol_bboxes(
    frames: list[Frame], ctx: DecodeContext,
) -> list[SymbolBBoxElement]:
    """扫描一页全部 SymbolBBox 几何帧。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        SymbolBBoxElement 列表（按帧偏移升序）。
    """
    result: list[SymbolBBoxElement] = []
    for frame in frames:
        el = parse_symbol_bbox(frame, ctx)
        if el is not None:
            result.append(el)
    result.sort(key=lambda e: e.offset)
    return result


def register_symbol_bbox_decoder() -> None:
    """注册 SymbolBBox 结构解码器。"""
    from .registry import register_struct

    register_struct(SYMBOL_BBOX_TYPE, parse_symbol_bbox)
