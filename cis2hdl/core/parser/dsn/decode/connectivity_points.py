"""同页无电线连接点解码（P1-7）。

端口/连接点帧（0x03/0x05/0x06）在无电线时表达引脚级连接点：
- ``0x03``：连接点（junction/connectivity point）
- ``0x05``：网标（net label）引用
- ``0x06``：端口（port）引用

布局与 0x01 引脚引用同源（短帧）：
``[8B DELIM][type:u16][x:u16][y:u16][object_id:u32][trailer]``

本解码器把短帧（≤50B）解析为 ``ConnectionPoint``；长文本帧交给
``page_text_frames``（0x0000）处理，不做重复解析。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    from .wire_topology import PinPointRecord

logger = logging.getLogger(__name__)

#: 连接点帧类型（0x03=连接点、0x05=网标、0x06=端口）
CONNECTION_POINT_FRAME_TYPES = frozenset((0x03, 0x05, 0x06))
#: 连接点帧最大长度（短帧；长文本帧交给 page_text_frames）
_CONN_MAX_LEN = 50
#: 坐标上限（mil）
_MAX_COORD = 30000


@dataclass
class ConnectionPoint:
    """同页无电线连接点。

    Attributes:
        offset: 帧页内偏移。
        frame_type: 0x03=连接点、0x05=网标、0x06=端口。
        x, y: 连接点坐标（mil）。
        object_id: 对象 id。
        net_name: 关联网名（经 net 表坐标匹配后回填，默认空）。
        kind: 'junction' | 'net_label' | 'port'（按 frame_type）。
    """

    offset: int
    frame_type: int
    x: int
    y: int
    object_id: int
    net_name: str = ""

    @property
    def kind(self) -> str:
        return {
            0x03: "junction",
            0x05: "net_label",
            0x06: "port",
        }.get(self.frame_type, "unknown")

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "kind": self.kind,
            "x": self.x,
            "y": self.y,
            "object_id": self.object_id,
            "net_name": self.net_name,
        }


def parse_connection_point(frame: Frame, ctx: DecodeContext) -> ConnectionPoint | None:
    """解析单个连接点短帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        ConnectionPoint；非连接点帧或坐标非法返回 None。
    """
    if frame.frame_type not in CONNECTION_POINT_FRAME_TYPES:
        return None
    rec = frame.raw
    if len(rec) > _CONN_MAX_LEN:
        return None
    if len(rec) < 18:
        return None
    # 跳过短帧里可能存在的文本标记（0x18）
    if b"\x18" in rec[10:16]:
        return None
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    object_id = int.from_bytes(rec[14:18], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    return ConnectionPoint(
        offset=frame.offset,
        frame_type=frame.frame_type,
        x=x,
        y=y,
        object_id=object_id,
    )


def scan_connection_points(
    frames: list[Frame], ctx: DecodeContext,
) -> list[ConnectionPoint]:
    """扫描一页全部连接点短帧。

    Args:
        frames: 页面帧列表（结构区）。
        ctx: 解码上下文。

    Returns:
        ConnectionPoint 列表（按帧偏移升序）。
    """
    result: list[ConnectionPoint] = []
    for frame in frames:
        cp = parse_connection_point(frame, ctx)
        if cp is not None:
            result.append(cp)
    return result


def register_connection_point_decoders() -> None:
    """注册 0x03/0x05/0x06 结构帧解码器。"""
    from .registry import register_struct

    for ftype in CONNECTION_POINT_FRAME_TYPES:
        register_struct(ftype, parse_connection_point)
