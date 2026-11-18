"""0x0000 全页面文字帧解码器（架构设计 §3.2，P0-2）。

0x0000 帧与 ``info_frames`` 布局同源（TG0C0D2 字节级实证）::

    [8B DELIM]                      页内偏移 +0
    [00 00][00 00][00 00]           +8..13
    [30 00 00 00]                   +14..17   标记
    [01 00]                         +18..19
    [2e 2e 35 | 2e 2e 3d]           +20..22   样式 kind（'..5' / '..='，透传）
    [00]*7                          +23..29
    [x:u32][y:u32][x2:u32][y2:u32]  +30..45   锚点 + 包围盒右上
    [x:u32][y:u32]                  +46..53   锚点重复
    [font:u16][attr:u16][len:u16]   +54..59
    [text][00]                      +60..     文本字节 + 00 终止

实现要点：
- **复用** ``info_frames._coord_block`` / ``_decode_text`` / ``_parse_text_element``
  （改造为公共函数后），把 0x0000 帧按三段式 text → rect → line 解析。
- 文本元素转成 ``PageTextElement``；图形（rect/line）返回 ``InfoElement``
  （由调用方并入 ``graphic_elements``）。
- 防御性：解析失败返回空列表，不抛异常。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..frame.splitter import Frame
from .info_frames import (
    InfoElement,
    coord_block,
    decode_text,
    parse_info_frame,
    parse_text_element,
)
from .registry import DecodeContext

logger = logging.getLogger(__name__)

#: 0x0000 帧类型
PAGE_TEXT_FRAME_TYPE = 0x0000


@dataclass
class PageTextElement:
    """全页面文字元素。

    Attributes:
        page: 页面名。
        offset: 帧页内偏移。
        x, y: 文本锚点。
        x2, y2: 包围盒右上。
        text: 解码文本（latin1 或 GBK）。
        font: 字体号（透传数字；字号映射列 P2）。
        attr: 文本属性打包字段（颜色/样式，透传）。
        rotation: 旋转（恒 0，0x0000 帧未发现独立旋转字段）。
        encoding: "latin1" | "gbk"。
    """

    page: str
    offset: int
    x: int
    y: int
    x2: int
    y2: int
    text: str
    font: int | None
    attr: int = 0
    rotation: int = 0
    encoding: str = "latin1"

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "offset": self.offset,
            "x": self.x,
            "y": self.y,
            "x2": self.x2,
            "y2": self.y2,
            "text": self.text,
            "font": self.font,
            "attr": self.attr,
            "rotation": self.rotation,
            "encoding": self.encoding,
        }


def _info_to_text_element(el: InfoElement, page: str) -> PageTextElement | None:
    """把 info_frames 的 text 元素转成 PageTextElement。

    Args:
        el: InfoElement（kind == "text"）。
        page: 页面名。

    Returns:
        PageTextElement；非文本元素返回 None。
    """
    if el.kind != "text" or el.text is None:
        return None
    if el.x is None or el.y is None or el.x2 is None or el.y2 is None:
        return None
    # 编码推断：文本含 >127 的码位 → GBK（与 _decode_text 双路一致）
    encoding = "gbk" if any(ord(c) > 127 for c in el.text) else "latin1"
    return PageTextElement(
        page=page,
        offset=el.offset,
        x=el.x,
        y=el.y,
        x2=el.x2,
        y2=el.y2,
        text=el.text,
        font=el.font,
        attr=el.attr,
        rotation=el.rotation,
        encoding=encoding,
    )


def parse_page_text_frame(
    frame: Frame, ctx: DecodeContext,
) -> list[PageTextElement]:
    """解析单个 0x0000 帧为文字元素列表。

    复用 ``info_frames`` 三段式解析（text → rect → line）；仅返回
    文本元素，图形元素由调用方经 ``parse_info_frame`` 另行收集。

    Args:
        frame: 输入帧（frame_type == 0）。
        ctx: 解码上下文（page_name 提供页面名）。

    Returns:
        PageTextElement 列表（通常 0 或 1 个）。
    """
    if frame.frame_type != PAGE_TEXT_FRAME_TYPE:
        return []
    page = ctx.page_name or ""
    els = parse_info_frame(frame, ctx)
    result: list[PageTextElement] = []
    for el in els:
        converted = _info_to_text_element(el, page)
        if converted is not None:
            result.append(converted)
    return result


def _fallback_coord(rec: bytes) -> tuple[int, int] | None:
    """防御性坐标提取：帧内任意合法 (x, y) 坐标对（u32 优先）。

    Returns:
        (x, y)；无合法坐标返回 None。
    """
    # u32 坐标（30..53 布局失败时全帧扫描）
    for off in range(14, len(rec) - 7):
        x = int.from_bytes(rec[off:off + 4], "little")
        y = int.from_bytes(rec[off + 4:off + 8], "little")
        if 0 < x < 30000 and 0 < y < 30000:
            return x, y
    # u16 坐标
    for off in range(14, len(rec) - 3):
        x = int.from_bytes(rec[off:off + 2], "little")
        y = int.from_bytes(rec[off + 2:off + 4], "little")
        if 0 < x < 30000 and 0 < y < 30000:
            return x, y
    return None


def parse_page_element_defensive(
    frame: Frame, ctx: DecodeContext,
) -> PageTextElement | None:
    """防御性解析 0x0000 帧：任何含合法坐标的帧都产出元素。

    与审计「1794 文字帧」口径对齐：0x0000 帧即使不含文本尾（rect/line/
    结构帧），只要存在合法坐标对即产出 ``PageTextElement``（text 可为空）。

    Args:
        frame: 输入帧（frame_type == 0）。
        ctx: 解码上下文。

    Returns:
        PageTextElement；非 0x0000 或无坐标返回 None。
    """
    if frame.frame_type != PAGE_TEXT_FRAME_TYPE:
        return None
    page = ctx.page_name or ""
    # 优先真实文本
    els = parse_info_frame(frame, ctx)
    for el in els:
        converted = _info_to_text_element(el, page)
        if converted is not None:
            return converted
    # 兜底：任意合法坐标
    coord = _fallback_coord(frame.raw)
    if coord is None:
        return None
    x, y = coord
    return PageTextElement(
        page=page,
        offset=frame.offset,
        x=x,
        y=y,
        x2=x,
        y2=y,
        text="",
        font=None,
        attr=0,
        rotation=0,
        encoding="latin1",
    )


def scan_page_elements_defensive(
    frames: list[Frame], ctx: DecodeContext,
) -> list[PageTextElement]:
    """扫描一页全部 0x0000 元素（防御性，含 rect/line/结构帧兜底）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        PageTextElement 列表（每个 0x0000 帧至多 1 个）。
    """
    result: list[PageTextElement] = []
    for frame in frames:
        el = parse_page_element_defensive(frame, ctx)
        if el is not None:
            result.append(el)
    return result


def scan_page_text_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> list[PageTextElement]:
    """扫描一页全部 0x0000 文字元素（按帧偏移升序）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        PageTextElement 列表。
    """
    result: list[PageTextElement] = []
    for frame in frames:
        if frame.frame_type != PAGE_TEXT_FRAME_TYPE:
            continue
        result.extend(parse_page_text_frame(frame, ctx))
    return result


def scan_page_graphic_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> list[InfoElement]:
    """扫描一页全部 0x0000 图形元素（rect/line，供 graphic_elements 合并）。

    复用 ``info_frames`` 三段式解析；0x0000 帧同时含 text/rect/line
    三类，与信息页一致。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        InfoElement 列表（含 text；调用方按需过滤）。
    """
    result: list[InfoElement] = []
    for frame in frames:
        if frame.frame_type != PAGE_TEXT_FRAME_TYPE:
            continue
        result.extend(parse_info_frame(frame, ctx))
    return result


def register_page_text_decoder() -> None:
    """注册 0x0000 → parse_page_text_frame（基类-注册模式）。"""
    from .registry import register_semantic

    register_semantic(PAGE_TEXT_FRAME_TYPE, parse_page_text_frame)
