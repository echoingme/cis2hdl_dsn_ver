"""DSN 帧切分器单元测试（架构设计 §6.1 test_frame_splitter.py）。"""

from __future__ import annotations

import pytest

from cis2hdl.core.parser.dsn.frame.splitter import (
    Frame,
    count_delims,
    frames_after,
    split_frames,
)
from cis2hdl.core.parser.dsn.frame.types import DELIMITER, TYPE_0x02_PROP, TYPE_0x85_VALUE

pytestmark = pytest.mark.unit


def _frame_raw(frame_type: int, payload: bytes) -> bytes:
    """构造一个帧原始字节：8B DELIM + type u16 + payload。"""
    return DELIMITER + frame_type.to_bytes(2, "little") + payload


class TestSplitFrames:
    def test_empty_data_returns_empty_list(self) -> None:
        assert split_frames(b"") == []

    def test_no_delimiter_returns_empty_list(self) -> None:
        assert split_frames(b"\x00\x01\x02\x03") == []

    def test_single_frame(self) -> None:
        data = _frame_raw(0x85, b"\x00\x00\x10\x00")
        frames = split_frames(data)
        assert len(frames) == 1
        f = frames[0]
        assert f.offset == 0
        assert f.frame_type == 0x85
        assert f.raw == data
        assert f.next_offset == len(data)

    def test_multiple_frames(self) -> None:
        f1 = _frame_raw(0x85, b"\x00\x00\x10\x00")
        f2 = _frame_raw(0x02, b"\x00\x00\x20\x00")
        data = f1 + f2
        frames = split_frames(data)
        assert len(frames) == 2
        assert frames[0].frame_type == 0x85
        assert frames[0].next_offset == len(f1)
        assert frames[1].offset == len(f1)
        assert frames[1].frame_type == 0x02
        assert frames[1].next_offset == len(data)

    def test_internal_4b_magic_prefix_not_split(self) -> None:
        """帧内出现 4B 魔数 FF E4 5C 39 不得导致误切帧（架构 Bug B）。"""
        # 第 1 帧 payload 内含 4B 魔数（但没有 8B 完整分隔符）
        internal = b"\x00\x00" + b"\xff\xe4\x5c\x39\x00\x00\x00\x01" + b"\x00\x10"
        f1 = _frame_raw(0x85, internal)
        f2 = _frame_raw(0x02, b"\x00\x00\x20\x00")
        data = f1 + f2
        frames = split_frames(data)
        # 只有 2 帧（4B 魔数不会切开 f1）
        assert len(frames) == 2
        assert frames[0].frame_type == 0x85
        assert frames[1].frame_type == 0x02

    def test_consecutive_delimiters(self) -> None:
        """连续分隔符产生空帧（或最短帧）。"""
        data = DELIMITER + DELIMITER
        frames = split_frames(data)
        assert len(frames) == 2
        assert frames[0].offset == 0
        assert frames[1].offset == len(DELIMITER)

    def test_frame_type_is_strlst_index(self) -> None:
        """帧类型 = raw[8:10] u16（strLst 索引语义）。"""
        data = _frame_raw(0x85, b"\x00" * 8)
        frames = split_frames(data)
        assert frames[0].frame_type == 0x85
        assert frames[0].type_hex == "0x0085"

    def test_short_frame_type_zero(self) -> None:
        """不足 10 字节的帧 type 按 raw[8:10] 解析（不崩溃）。"""
        data = DELIMITER + b"\x00\x00"
        frames = split_frames(data)
        assert len(frames) == 1
        assert frames[0].frame_type == 0


class TestCountDelims:
    def test_count(self) -> None:
        data = _frame_raw(0x85, b"x") + _frame_raw(0x02, b"y")
        assert count_delims(data) == 2


class TestFramesAfter:
    def test_frames_after_offset(self) -> None:
        f1 = _frame_raw(0x85, b"\x00" * 4)
        f2 = _frame_raw(0x02, b"\x00" * 4)
        data = f1 + f2
        frames = split_frames(data)
        after = frames_after(frames, len(f1))
        assert len(after) == 1
        assert after[0].frame_type == 0x02
