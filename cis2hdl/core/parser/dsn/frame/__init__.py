"""帧切分层（架构设计 §2 L2）。"""

from __future__ import annotations

from .splitter import Frame, count_delims, frames_after, split_frames
from .types import (
    DELIMITER,
    STRUCT_NAMES,
    TYPE_0x01_GND,
    TYPE_0x02_PROP,
    TYPE_0x083F_NAME,
    TYPE_0x0842_NUMBER,
    TYPE_0x22_PART_REF,
    TYPE_0x85_VALUE,
)

__all__ = [
    "Frame",
    "split_frames",
    "count_delims",
    "frames_after",
    "DELIMITER",
    "STRUCT_NAMES",
    "TYPE_0x01_GND",
    "TYPE_0x02_PROP",
    "TYPE_0x083F_NAME",
    "TYPE_0x0842_NUMBER",
    "TYPE_0x22_PART_REF",
    "TYPE_0x85_VALUE",
]
