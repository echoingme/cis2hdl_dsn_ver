"""IR 映射层（架构设计 §2 L5）。"""

from __future__ import annotations

from .mapper import (
    PAGE_DATA_FIELDS,
    build_component_db,
    build_page_ir,
    map_to_design_ir,
)

__all__ = [
    "PAGE_DATA_FIELDS",
    "build_component_db",
    "build_page_ir",
    "map_to_design_ir",
]
