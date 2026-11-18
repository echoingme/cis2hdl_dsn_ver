"""strLst 解析（架构设计 §2 L2/strlst）。

Library 流 strLst = 全局字符串表（属性名/值）。
两种策略：
1. 硬编码步进（现有 ``library_parser.parse_strlst`` 逻辑，48B header + fonts + pageSettings）
2. 启发式定位（``component_extract.parse_library_strlst``：找 8 个 partField + u32 count）

运行时先试硬编码，失败回退启发式；以 ``count==2170`` 作为 TG0C0D2 自检断言。
"""

from __future__ import annotations

import logging

from ..binary_reader import BinaryReader

logger = logging.getLogger(__name__)

#: 硬编码 header 字节数
HEADER_BYTE_SIZE = 48
#: PageSettings 字节数
PAGE_SETTINGS_SIZE = 156
#: LOGFONTA 结构大小
LOGFONTA_SIZE = 60
#: partField 数量
PART_FIELD_COUNT = 8


def _read_lstr(data: bytes, off: int) -> tuple[str | None, int]:
    """u16 len + bytes + 0x00（readStringLenZeroTerm）。"""
    if off + 2 > len(data):
        return None, off
    l = int.from_bytes(data[off:off + 2], "little")
    if l == 0xFFFF or l > 500:
        return None, off
    if off + 2 + l + 1 > len(data):
        return None, off
    if data[off + 2 + l] != 0:
        return None, off
    try:
        raw = data[off + 2:off + 2 + l]
        if any(b >= 0x80 for b in raw):
            return raw.decode("gbk"), off + 2 + l + 1
        return raw.decode("latin1"), off + 2 + l + 1
    except Exception:
        return None, off


def _parse_strlst_hardcoded(data: bytes) -> list[str]:
    """硬编码步进解析（现有 library_parser 逻辑）。"""
    reader = BinaryReader(data)
    reader.skip(HEADER_BYTE_SIZE)
    text_font_len = reader.read_uint16()
    font_count = max(0, text_font_len - 1)
    reader.skip(font_count * LOGFONTA_SIZE)
    some_len = reader.read_uint16()
    reader.skip(some_len * 2)
    reader.skip(8)
    for _ in range(PART_FIELD_COUNT):
        slen = reader.read_uint16()
        if slen > 0:
            reader.skip(slen)
        reader.skip(1)
    reader.skip(PAGE_SETTINGS_SIZE)
    str_lst_len = reader.read_uint32()
    if str_lst_len == 0 or str_lst_len > 20000:
        raise ValueError(f"unreasonable strLst count: {str_lst_len}")
    result: list[str] = []
    for _ in range(str_lst_len):
        slen = reader.read_uint16()
        if slen == 0:
            reader.skip(1)
            result.append("")
            continue
        raw = reader.read_bytes(slen)
        reader.skip(1)
        # GBK 中文属性名：latin1 可解码但含高位字节（>=0x80）→ 尝试 GBK
        has_high = any(b >= 0x80 for b in raw)
        if has_high:
            try:
                text = raw.decode("gbk", errors="replace")
            except (UnicodeDecodeError, LookupError):
                text = raw.decode("latin-1", errors="replace")
        else:
            text = raw.decode("latin-1", errors="replace")
        result.append(text)
    return result


def _parse_strlst_heuristic(data: bytes) -> list[str]:
    """启发式定位解析（component_extract 逻辑）。

    找 8 个连续 readStringLenZeroTerm (partFields) + u32 长度 + 完整可读字符串序列。
    """
    for try_off in range(32, min(len(data) - 40, 0x6000)):
        p = try_off
        ok = True
        for _ in range(8):
            s, p = _read_lstr(data, p)
            if s is None:
                ok = False
                break
        if not ok:
            continue
        if p + 4 > len(data):
            continue
        slen = int.from_bytes(data[p:p + 4], "little")
        if not (50 <= slen <= 20000):
            continue
        p2 = p + 4
        strs: list[str] = []
        ok2 = True
        for _ in range(slen):
            s, p2 = _read_lstr(data, p2)
            if s is None:
                ok2 = False
                break
            strs.append(s)
        if ok2 and len(strs) == slen:
            return strs
    return []


def parse_strlst(library_bytes: bytes) -> list[str]:
    """解析 Library 流的 strLst（双策略）。

    Args:
        library_bytes: Library 流原始字节。

    Returns:
        strLst 字符串列表（latin1/GBK 解码）。

    Raises:
        ValueError: 两种策略均失败。
    """
    # 策略 1：硬编码
    try:
        hard = _parse_strlst_hardcoded(library_bytes)
        if len(hard) >= 50:
            logger.info("strLst parsed via hardcoded layout: %d entries", len(hard))
            return hard
    except Exception as exc:
        logger.debug("hardcoded strLst parse failed: %s", exc)

    # 策略 2：启发式
    heur = _parse_strlst_heuristic(library_bytes)
    if len(heur) >= 50:
        logger.info("strLst parsed via heuristic layout: %d entries", len(heur))
        return heur

    raise ValueError(
        f"Failed to parse strLst from Library stream "
        f"(hardcoded={len(hard) if 'hard' in dir() else 0}, heuristic={len(heur)})"
    )


def validate_strlst(strlst: list[str], expected_count: int = 2170) -> bool:
    """自检：strLst 数量与预期一致。

    Args:
        strlst: 解析结果。
        expected_count: 预期数量（TG0C0D2 = 2170，QA 已验证）。

    Returns:
        True 一致；False 不一致（非异常路径）。
    """
    return len(strlst) == expected_count
