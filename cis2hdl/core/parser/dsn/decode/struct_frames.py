"""标准结构帧解码器（架构设计 §2 L3）。

标准结构类型（OpenOrCadParser Structure.hpp u8 枚举）：
- ``13 PlacedInstance``：放置实例
- ``20 WireScalar`` / ``21 WireBus``：走线
- ``37 Global``：全局信号
- ``38 OffPageConnector``：跨页连接符
- ``0x2C SymbolBBox``：符号包围盒

注意：TG0C0D2 平铺页面中结构数据主要通过"语义帧"（0x85/0x02/0x01）表达，
此处提供标准结构类型解码器以满足注册制架构的扩展要求（RTL 变体走线等）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

#: 结构类型常量
STRUCT_PLACED_INSTANCE = 13
STRUCT_WIRE_SCALAR = 20
STRUCT_WIRE_BUS = 21
STRUCT_GLOBAL = 37
STRUCT_OFFPAGE_CONNECTOR = 38
STRUCT_SYMBOL_BBOX = 0x2C


@dataclass
class StructRecord:
    """通用结构帧记录。"""

    struct_type: int
    offset: int
    texts: list[str] = field(default_factory=list)
    ints: list[int] = field(default_factory=list)


@dataclass
class PlacedInstanceRecord:
    """PlacedInstance 结构记录。"""

    offset: int
    texts: list[str] = field(default_factory=list)


@dataclass
class WireStructRecord:
    """WireScalar/WireBus 结构记录（含坐标候选）。"""

    offset: int
    struct_type: int
    ints: list[int] = field(default_factory=list)


def _extract_texts(rec: bytes, min_len: int = 2) -> list[str]:
    return [
        m.group().decode("latin1")
        for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, rec)
    ]


def _extract_ints(rec: bytes, max_count: int = 32) -> list[int]:
    out: list[int] = []
    for off in range(8, len(rec) - 3, 2):
        v = int.from_bytes(rec[off:off + 2], "little")
        if len(out) >= max_count:
            break
        out.append(v)
    return out


def parse_placed_instance(frame: Frame, ctx: DecodeContext) -> PlacedInstanceRecord:
    """解析 PlacedInstance 结构帧（13）。"""
    return PlacedInstanceRecord(
        offset=frame.offset,
        texts=_extract_texts(frame.raw),
    )


def parse_wire_struct(frame: Frame, ctx: DecodeContext) -> WireStructRecord:
    """解析 WireScalar(20)/WireBus(21) 结构帧。"""
    return WireStructRecord(
        offset=frame.offset,
        struct_type=frame.frame_type,
        ints=_extract_ints(frame.raw),
    )


def parse_global(frame: Frame, ctx: DecodeContext) -> StructRecord:
    """解析 Global 结构帧（37）。"""
    return StructRecord(
        struct_type=frame.frame_type,
        offset=frame.offset,
        texts=_extract_texts(frame.raw),
    )


def parse_offpage(frame: Frame, ctx: DecodeContext) -> StructRecord:
    """解析 OffPageConnector 结构帧（38）。"""
    return StructRecord(
        struct_type=frame.frame_type,
        offset=frame.offset,
        texts=_extract_texts(frame.raw),
    )


def parse_symbol_bbox(frame: Frame, ctx: DecodeContext) -> StructRecord:
    """解析 SymbolBBox 结构帧（0x2C）。"""
    return StructRecord(
        struct_type=frame.frame_type,
        offset=frame.offset,
        texts=_extract_texts(frame.raw),
        ints=_extract_ints(frame.raw, max_count=8),
    )


def register_struct_decoders() -> None:
    """注册全部标准结构类型解码器。"""
    from .registry import register_struct

    register_struct(STRUCT_PLACED_INSTANCE, parse_placed_instance)
    register_struct(STRUCT_WIRE_SCALAR, parse_wire_struct)
    register_struct(STRUCT_WIRE_BUS, parse_wire_struct)
    register_struct(STRUCT_GLOBAL, parse_global)
    register_struct(STRUCT_OFFPAGE_CONNECTOR, parse_offpage)
    register_struct(STRUCT_SYMBOL_BBOX, parse_symbol_bbox)
    # P1-1：26/27 SymbolPinScalar/Bus（页面级短引用）
    from .symbol_pin_frames import register_symbol_pin_decoders

    register_symbol_pin_decoders()
