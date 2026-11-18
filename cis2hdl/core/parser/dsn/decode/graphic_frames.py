"""图形文本帧解码器（架构设计 §2 L3）。

- ``GraphicCommentText(61)``：注释文本
- ``TitleBlock(65)``：标题栏文本
提取帧内可打印文本（长度 >= 2，过滤纯数字/纯十六进制垃圾）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

#: 注释文本最小长度
_MIN_TEXT_LEN = 2


@dataclass
class GraphicText:
    """图形文本记录。"""

    offset: int
    frame_type: int
    text: str


@dataclass
class GraphicTextScan:
    """一页图形文本扫描结果。"""

    comment_texts: list[GraphicText] = field(default_factory=list)
    title_blocks: list[GraphicText] = field(default_factory=list)


def _extract_texts(rec: bytes, min_len: int = _MIN_TEXT_LEN) -> list[str]:
    return [
        m.group().decode("latin1")
        for m in re.finditer(rb"[\x20-\x7e]{%d,}" % min_len, rec)
    ]


def _clean_text(text: str) -> str | None:
    """过滤明显垃圾（纯十六进制/纯数字/纯符号）。"""
    if len(text) < _MIN_TEXT_LEN:
        return None
    if all(c in "0123456789abcdefABCDEF" for c in text):
        return None
    if all(not c.isalnum() for c in text):
        return None
    if text.count(" ") > len(text) * 0.5:
        return None
    return text


def parse_graphic_text_frame(frame: Frame, ctx: DecodeContext) -> list[GraphicText]:
    """解析图形文本帧（61 注释 / 65 标题栏）。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用）。

    Returns:
        GraphicText 列表（一个帧可能含多个文本串）。
    """
    result: list[GraphicText] = []
    for raw_text in _extract_texts(frame.raw):
        text = _clean_text(raw_text)
        if text is not None:
            result.append(
                GraphicText(offset=frame.offset, frame_type=frame.frame_type, text=text)
            )
    return result


def scan_graphic_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> GraphicTextScan:
    """扫描一页图形文本（61/65 帧）。"""
    scan = GraphicTextScan()
    for frame in frames:
        if frame.frame_type not in (61, 65):
            continue
        texts = parse_graphic_text_frame(frame, ctx)
        for t in texts:
            if frame.frame_type == 61:
                scan.comment_texts.append(t)
            else:
                scan.title_blocks.append(t)
    return scan
