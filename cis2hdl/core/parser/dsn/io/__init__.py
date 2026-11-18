"""IO 层：OLE 流读取（IOleProvider 接口 + OlefileProvider 主实现 + OleReaderProvider 备选）。"""

from __future__ import annotations

from .ole_provider import IOleProvider, OlefileProvider, OleReaderProvider
from .stream_names import (
    DESIGN_VIEW_PREFIX,
    discover_page_streams,
    is_page_stream,
    stream_key,
)

__all__ = [
    "IOleProvider",
    "OlefileProvider",
    "OleReaderProvider",
    "DESIGN_VIEW_PREFIX",
    "discover_page_streams",
    "is_page_stream",
    "stream_key",
]
