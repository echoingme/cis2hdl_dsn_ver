"""帧切分器（架构设计 §2 L2）。

- 使用 8 字节分隔符 ``FF E4 5C 39 00 00 00 00`` 一次性 ``re.finditer`` 切帧
- **禁止** 4B 魔数扫描（帧内数据可能含 4B 前缀导致误判）
- 帧类型 = ``raw[8:10]`` u16 —— 即 strLst 索引语义
- 帧尾 = 下一分隔符（末帧 = 缓冲尾）
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .types import DELIMITER, DELIMITER_LEN

_DELIM_RE = re.compile(re.escape(DELIMITER))


@dataclass(frozen=True)
class Frame:
    """一个语义帧。

    Attributes:
        offset: 帧在页面流中的绝对偏移（分隔符起点）。
        raw: 帧原始字节（``[delim .. 下一 delim)``）。
        frame_type: ``raw[8:10]`` u16（strLst 索引语义）。
        next_offset: 下一帧偏移（末帧 = len(data)）。
    """

    offset: int
    raw: bytes
    frame_type: int
    next_offset: int

    @property
    def length(self) -> int:
        """帧原始长度。"""
        return len(self.raw)

    @property
    def type_hex(self) -> str:
        """帧类型十六进制描述（诊断用）。"""
        return f"0x{self.frame_type:04x}"

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"Frame(offset=0x{self.offset:x}, type=0x{self.frame_type:04x}, "
            f"len={len(self.raw)})"
        )


def split_frames(data: bytes) -> list[Frame]:
    """按 8B 分隔符切帧。

    Args:
        data: 页面流原始字节。

    Returns:
        Frame 列表（按偏移升序）。

    Raises:
        ValueError: 数据不含任何分隔符（不是有效 CIS 页面流）。
    """
    delims = [m.start() for m in _DELIM_RE.finditer(data)]
    if not delims:
        return []
    frames: list[Frame] = []
    for i, pos in enumerate(delims):
        end = delims[i + 1] if i + 1 < len(delims) else len(data)
        raw = data[pos:end]
        frame_type = (
            int.from_bytes(raw[8:10], "little") if len(raw) > 9 else 0
        )
        frames.append(
            Frame(offset=pos, raw=raw, frame_type=frame_type, next_offset=end)
        )
    return frames


def count_delims(data: bytes) -> int:
    """统计分隔符数量（与 QA ``find_delims`` 一致）。"""
    return len(_DELIM_RE.findall(data))


def frames_after(
    frames: list[Frame], offset: int,
) -> list[Frame]:
    """返回偏移 >= offset 的帧（网名表之后的结构区）。"""
    return [f for f in frames if f.offset >= offset]
