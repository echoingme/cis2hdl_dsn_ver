"""解码层（架构设计 §2 L3）。"""

from __future__ import annotations

from .connectivity_points import (
    CONNECTION_POINT_FRAME_TYPES,
    ConnectionPoint,
    parse_connection_point,
    register_connection_point_decoders,
    scan_connection_points,
)
from .graphic_frames import GraphicText, GraphicTextScan, scan_graphic_frames
from .info_frames import (
    InfoElement,
    decode_info_page,
    parse_info_frame,
    scan_info_frames,
)
from .library_attrs import (
    ComponentUserAttr,
    parse_component_attrs,
    scan_string_table,
)
from .net_table import NetTable, parse_net_table
from .coord_ref_frames import (
    CoordRefRecord,
    discover_coord_ref_types,
    parse_coord_ref_frame,
    register_coord_ref_decoders,
)
from .net_name_frames import (
    NetNameRecord,
    discover_net_name_types,
    parse_net_name_frame,
    register_net_name_decoders,
    scan_net_name_frames,
)
from .page_text_frames import (
    PAGE_TEXT_FRAME_TYPE,
    PageTextElement,
    parse_page_element_defensive,
    parse_page_text_frame,
    register_page_text_decoder,
    scan_page_elements_defensive,
    scan_page_graphic_frames,
    scan_page_text_frames,
)
from .pin_table_frames import (
    PIN_PARTITION_FRAME_TYPE,
    PinPartitionEntry,
    PinPartitionTable,
    parse_pin_partition_frame,
    register_pin_partition_decoder,
    scan_pin_partition_frames,
)
from .registry import (
    SEMANTIC_FRAME_DECODERS,
    STRUCT_DECODERS,
    DecodeContext,
    UnknownFrame,
    clear_registries,
    dispatch,
    register_semantic,
    register_struct,
)
from .semantic_frames import (
    GndFrame,
    PropertyFrame,
    RefdesFrame,
    TextFrame,
    parse_gnd_frame,
    parse_property_frame,
    parse_refdes_frame,
    parse_text_frame,
    register_semantic_decoders,
)
from .shape_frames import (
    ShapeFrame,
    extract_render_origin,
    extract_shape,
    extract_shape_coord,
    scan_shape_frames,
)
from .source_symbols import (
    SourceSymbol,
    SourceSymbolKind,
    parse_source_symbol,
    register_source_symbol_decoders,
    scan_source_symbols,
)
from .struct_frames import (
    PlacedInstanceRecord,
    StructRecord,
    WireStructRecord,
    register_struct_decoders,
)
from .symbol_geometry import SymbolTextElement, scan_symbol_texts
from .symbol_pin_frames import (
    PIN_REF_FRAME_TYPES,
    PORT_TYPE_TO_ELECTRICAL,
    PinRefRecord,
    parse_symbol_pin_ref,
    port_type_to_electrical,
    register_symbol_pin_decoders,
    scan_symbol_pin_refs,
)
from .text_style import (
    COLOR_CODES,
    FONT_TO_PT,
    REFDES_FONT_TO_PT,
    decode_attr_color,
    decode_attr_justify,
    decode_font_to_pt,
    decode_refdes_font_to_pt,
    should_use_new_path,
)
from .wire_frames import WireRecord, WireScanResult, scan_wire_frames
from .wire_topology import (
    ConnectivityBuilder,
    ConnectivityResult,
    PinPointRecord,
    WirePolyline,
    build_wire_polylines,
    collect_pin_points,
)


def register_all_decoders() -> None:
    """注册全部解码器（语义 + 结构 + P0 新解码器 + P1 新解码器 + P2 网名帧）。"""
    register_semantic_decoders()
    register_struct_decoders()
    register_pin_partition_decoder()
    register_page_text_decoder()
    register_symbol_pin_decoders()
    register_connection_point_decoders()
    register_source_symbol_decoders()
    # P2-B：网名帧（内容探测发现类型；未发现时注册空集）
    register_net_name_decoders()
    # T04 P2-7：短坐标引用帧（内容探测发现类型；未发现时注册空集）
    register_coord_ref_decoders()


__all__ = [
    "SEMANTIC_FRAME_DECODERS",
    "STRUCT_DECODERS",
    "DecodeContext",
    "UnknownFrame",
    "clear_registries",
    "dispatch",
    "register_semantic",
    "register_struct",
    "register_all_decoders",
    "GndFrame",
    "PropertyFrame",
    "RefdesFrame",
    "TextFrame",
    "parse_gnd_frame",
    "parse_property_frame",
    "parse_refdes_frame",
    "parse_text_frame",
    "register_semantic_decoders",
    "ShapeFrame",
    "extract_shape",
    "extract_shape_coord",
    "extract_render_origin",
    "scan_shape_frames",
    "NetNameRecord",
    "parse_net_name_frame",
    "scan_net_name_frames",
    "discover_net_name_types",
    "register_net_name_decoders",
    "CoordRefRecord",
    "parse_coord_ref_frame",
    "discover_coord_ref_types",
    "register_coord_ref_decoders",
    "PlacedInstanceRecord",
    "StructRecord",
    "WireStructRecord",
    "register_struct_decoders",
    "WireRecord",
    "WireScanResult",
    "scan_wire_frames",
    "GraphicText",
    "GraphicTextScan",
    "scan_graphic_frames",
    "InfoElement",
    "parse_info_frame",
    "scan_info_frames",
    "decode_info_page",
    "NetTable",
    "parse_net_table",
    # P0-1
    "PIN_PARTITION_FRAME_TYPE",
    "PinPartitionEntry",
    "PinPartitionTable",
    "parse_pin_partition_frame",
    "register_pin_partition_decoder",
    "scan_pin_partition_frames",
    # P0-2
    "PAGE_TEXT_FRAME_TYPE",
    "PageTextElement",
    "parse_page_element_defensive",
    "parse_page_text_frame",
    "register_page_text_decoder",
    "scan_page_elements_defensive",
    "scan_page_graphic_frames",
    "scan_page_text_frames",
    # P0-4/P0-5
    "WirePolyline",
    "PinPointRecord",
    "ConnectivityResult",
    "ConnectivityBuilder",
    "build_wire_polylines",
    "collect_pin_points",
    # P1
    "FONT_TO_PT",
    "REFDES_FONT_TO_PT",
    "COLOR_CODES",
    "decode_font_to_pt",
    "decode_refdes_font_to_pt",
    "decode_attr_color",
    "decode_attr_justify",
    "should_use_new_path",
    "PIN_REF_FRAME_TYPES",
    "PinRefRecord",
    "parse_symbol_pin_ref",
    "scan_symbol_pin_refs",
    "register_symbol_pin_decoders",
    "PORT_TYPE_TO_ELECTRICAL",
    "port_type_to_electrical",
    "CONNECTION_POINT_FRAME_TYPES",
    "ConnectionPoint",
    "parse_connection_point",
    "scan_connection_points",
    "register_connection_point_decoders",
    "SourceSymbol",
    "SourceSymbolKind",
    "parse_source_symbol",
    "scan_source_symbols",
    "register_source_symbol_decoders",
    "ComponentUserAttr",
    "parse_component_attrs",
    "scan_string_table",
    "SymbolTextElement",
    "scan_symbol_texts",
]
