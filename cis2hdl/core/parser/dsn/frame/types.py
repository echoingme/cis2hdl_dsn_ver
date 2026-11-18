"""帧类型常量与语义（架构设计 §2 L2/L3）。

帧类型 = ``raw[8:10]`` 的 u16，实证规律：该值即 Library 流 strLst 的下标。
语义表从 ``Library`` strLst 动态生成，不硬编码魔法数字。
"""

from __future__ import annotations

#: 8 字节记录分隔符（QA 实证；禁止 4B 魔数扫描）
DELIMITER = b"\xFF\xE4\x5C\x39\x00\x00\x00\x00"

#: 分隔符长度
DELIMITER_LEN = len(DELIMITER)

#: 帧类型（strLst 索引语义）—— 以下为 TG0C0D2 实证常量，仅供测试/默认值
TYPE_0x01_GND = 0x01            # strLst[1] = 'GND'
TYPE_0x02_PROP = 0x02            # strLst[2] = 'OriginalSymbolOrigin' → 属性帧
TYPE_0x22_PART_REF = 0x22        # strLst[34] = 'Part Reference' → 位号显示帧
TYPE_0x85_VALUE = 0x85           # strLst[133] = 'VALUE' → 位号+valueIdx 帧
TYPE_0x083F_NAME = 0x083F        # strLst[2111] = 'Name'
TYPE_0x0842_NUMBER = 0x0842      # strLst[2114] = 'Number'

#: 标准结构类型（OpenOrCadParser Structure.hpp 枚举，u8 语义）
STRUCT_PLACED_INSTANCE = 13
STRUCT_WIRE_SCALAR = 20
STRUCT_WIRE_BUS = 21
STRUCT_LIBRARY_PART = 24
STRUCT_SYMBOL_PIN_SCALAR = 26
STRUCT_SYMBOL_PIN_BUS = 27
STRUCT_PACKAGE = 31
STRUCT_DEVICE = 32
STRUCT_GLOBAL_SYMBOL = 33
STRUCT_PORT_SYMBOL = 34
STRUCT_OFFPAGE_SYMBOL = 35
STRUCT_GLOBAL = 37
STRUCT_OFFPAGE_CONNECTOR = 38
STRUCT_SYMBOL_DISPLAY_PROP = 39
STRUCT_GRAPHIC_COMMENT_TEXT = 61
STRUCT_TITLE_BLOCK = 65
STRUCT_SYMBOL_BBOX = 0x2C

#: 常用结构类型 → 名称（诊断用）
STRUCT_NAMES: dict[int, str] = {
    2: "SthInPages0", 4: "DsnStream", 6: "PartCell", 9: "SchLib", 10: "Page",
    11: "PartInstance", 12: "DrawnInstance", 13: "PlacedInstance", 16: "T0x10",
    20: "WireScalar", 21: "WireBus", 23: "Port", 24: "LibraryPart",
    26: "SymbolPinScalar", 27: "SymbolPinBus", 29: "BusEntry", 31: "Package",
    32: "Device", 33: "GlobalSymbol", 34: "PortSymbol", 35: "OffPageSymbol",
    37: "Global", 38: "OffPageConnector", 39: "SymbolDisplayProp",
    48: "SymbolVector", 49: "Alias", 52: "T0x34", 53: "T0x35",
    55: "GraphicBoxInst", 56: "GraphicLineInst", 57: "GraphicArcInst",
    58: "GraphicEllipseInst", 59: "GraphicPolygonInst", 60: "GraphicPolylineInst",
    61: "GraphicCommentTextInst", 62: "GraphicBitMapInst", 64: "TitleBlockSymbol",
    65: "TitleBlock", 66: "SthInHierarchy1", 67: "NetDbIdMapping", 68: "SthInHierarchy2",
    69: "T0x45", 75: "ERCSymbol", 76: "BookMarkSymbol", 77: "ERCObject",
    78: "BookMarkSymbolInst", 82: "SthInHierarchy3", 88: "GraphicBezierInst",
    89: "GraphicOleEmbedInst", 91: "T0x5b", 98: "PinShapeSymbol", 103: "NetGroup",
    0x2C: "SymbolBBox",
}
