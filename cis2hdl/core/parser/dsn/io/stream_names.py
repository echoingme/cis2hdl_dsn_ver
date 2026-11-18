"""流名发现规则（架构设计 §2 L1）。

页面流按 ``["Views", <设计名>, "Pages", *]`` 前缀识别；
Cache / Library / Packages 按顶层流名定位。
"""

from __future__ import annotations

#: 页面流固定前缀（设计名可变）
DESIGN_VIEW_PREFIX = ("Views", "Pages")
#: 页面流固定前缀（含设计名占位）
VIEW_PAGES_PREFIX = ("Views", "Pages")

#: 顶层流名
STREAM_CACHE = "Cache"
STREAM_LIBRARY = "Library"
STREAM_PACKAGES = "Packages"
STREAM_SCHEMATIC = "Schematic"

#: 顶层流名集合（用于快速判断）
TOP_LEVEL_STREAMS = {
    STREAM_CACHE,
    STREAM_LIBRARY,
    STREAM_PACKAGES,
    STREAM_SCHEMATIC,
    "AdminData",
    "DsnStream",
    "Graphics",
    "HSObjects",
    "NetBundleMapData",
    "ExportBlocks Directory",
    "Cells Directory",
}


def stream_key(entry: tuple[str, ...]) -> str:
    """把 OLE 流路径元组转为 ``/`` 连接键。"""
    return "/".join(entry)


def is_page_stream(entry: tuple[str, ...]) -> bool:
    """判断是否为页面流（``Views/<设计>/Pages/<页名>`` 四段结构）。"""
    return len(entry) == 4 and entry[0] == "Views" and entry[2] == "Pages"


def discover_page_streams(
    entries: list[tuple[str, ...]], design_name: str | None = None,
) -> list[tuple[str, ...]]:
    """从扁平流列表中发现页面流。

    Args:
        entries: ``ole.listdir()`` 扁平条目列表。
        design_name: 设计名；为 None 时从条目中自动推断。

    Returns:
        按页面流名排序的页面流条目列表。
    """
    if design_name is None:
        # 自动推断：任意 Views/<x>/Pages/* 的四段条目
        matched = [e for e in entries if is_page_stream(e)]
    else:
        prefix = ("Views", design_name, "Pages")
        matched = [e for e in entries if len(e) == 4 and e[:3] == prefix]
    matched.sort(key=lambda e: e[3])
    return matched
