"""0x0867 芯片引脚分区表解码器（架构设计 §3.1，P0-1）。

帧结构（TG0C0D2 字节级实证）::

    [8B DELIM]                      页内偏移 +0
    [67 08 00 00]                   +8   帧类型 0x0867
    [77 08 00 00]                   +12  疑似对象/子类型 id（透传）
    [02 00][30][31 00][...]         +16.. 记录头（坐标/标记/计数交错）
    ...                             （头部语义未全定案，透传不解析）
    [lstr 串区]                     帧尾（len16 + printable|gbk + 00 + id32）

串区样例（08 页）：``3V3_SOC / SLIC_RST / UART1_SOUT / ...``；
11 页 DDR 页含 ``DDR_DM0/DDR_DQ0..DDR_A0..`` 等。

**解码策略**：全帧扫描 + 校验（不做顺序步进）。每个合法
``[len16][text][00]`` 记录即为一条分区条目；尾部 id32 的 2 字节可打印
噪声（如 ``wL``）通过「至少含一个字母/数字 + 非纯十六进制」过滤。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

logger = logging.getLogger(__name__)

#: 0x0867 帧类型（strLst[2151]）
PIN_PARTITION_FRAME_TYPE = 0x0867
#: 文本串最大长度
_MAX_TEXT_LEN = 64
#: 扫描起点（跳过分隔符 + 类型区）
_SCAN_START = 8
#: 坐标上限（mil 页面坐标）
_MAX_COORD = 30000
#: 串前坐标回看窗口（找 0x30 标记后的合法坐标）
_COORD_LOOKBACK = 20


@dataclass
class PinPartitionEntry:
    """芯片引脚分区表的一条文本条目。

    Attributes:
        offset: 页内绝对偏移（= frame.offset + 帧内偏移）。
        text: 解码文本。
        encoding: "latin1" | "gbk"。
        x, y: best-effort 关联坐标（串前 20B 内合法坐标块；无则 None）。
    """

    offset: int
    text: str
    encoding: str = "latin1"
    x: int | None = None
    y: int | None = None

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "text": self.text,
            "encoding": self.encoding,
            "x": self.x,
            "y": self.y,
        }


@dataclass
class PinPartitionTable:
    """一页 0x0867 帧解码结果。"""

    page: str
    offset: int
    length: int
    entries: list[PinPartitionEntry] = field(default_factory=list)

    @property
    def texts(self) -> list[str]:
        """有序文本串列表。"""
        return [e.text for e in self.entries]

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "offset": self.offset,
            "length": self.length,
            "entries": [e.to_dict() for e in self.entries],
            "texts": self.texts,
        }


def _u16(rec: bytes, off: int) -> int | None:
    return int.from_bytes(rec[off:off + 2], "little") if off + 2 <= len(rec) else None


def _decode_chunk(chunk: bytes) -> tuple[str, str] | None:
    """解码一段文本字节：全 ASCII → latin1；含高位 → GBK。

    Returns:
        (text, encoding)；解码失败返回 None。
    """
    if not chunk:
        return None
    if all(32 <= b < 127 for b in chunk):
        text = chunk.decode("latin1")
        # 纯十六进制噪声（id32 片段等）过滤
        if all(c in "0123456789abcdefABCDEF" for c in text):
            return None
        # 空格占比过高 → 噪声
        if text.count(" ") > len(text) * 0.3:
            return None
        return text, "latin1"
    try:
        text = chunk.decode("gbk")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 32 for c in text):
        return None
    return text, "gbk"


def _is_word_like(text: str) -> bool:
    """至少含一个字母或数字（过滤纯标点噪声）。"""
    return bool(re.search(r"[A-Za-z0-9]", text))


def _is_hex_noise(text: str) -> bool:
    """纯十六进制字符（id32 片段等噪声）。"""
    return all(c in "0123456789abcdefABCDEF" for c in text)


def _nearby_coord(rec: bytes, text_off: int) -> tuple[int | None, int | None]:
    """best-effort：在串前回看窗口内找 ``30 00 00 00`` 标记后的坐标。

    Args:
        rec: 帧原始字节。
        text_off: 文本 len16 前缀偏移。

    Returns:
        (x, y)；无合法坐标返回 (None, None)。
    """
    start = max(_SCAN_START, text_off - _COORD_LOOKBACK)
    window = rec[start:text_off]
    marker = window.rfind(b"\x30\x00\x00\x00")
    if marker == -1:
        return None, None
    base = start + marker + 4
    if base + 8 > text_off:
        return None, None
    x = int.from_bytes(rec[base:base + 4], "little")
    y = int.from_bytes(rec[base + 4:base + 8], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None, None
    return x, y


def parse_pin_partition_frame(
    frame: Frame, ctx: DecodeContext,
) -> PinPartitionTable | None:
    """解析单个 0x0867 帧为芯片引脚分区表。

    全帧扫描 + 校验（不做顺序步进）：
    1. 合法 ``[len16][text][00]`` 记录 → 分区条目；
    2. 兜底捕获可打印串（len≥2、含字母/数字、非纯十六进制），
       与审计「98 文本串 = printable runs≥2」口径对齐。

    防御性：0x0867 帧即使无文本串也返回空表（不抛异常、不返回 None），
    保证 18 页全部产出 pin_partitions。

    Args:
        frame: 输入帧（frame_type == 0x0867）。
        ctx: 解码上下文（page_name 提供页面名）。

    Returns:
        PinPartitionTable；非 0x0867 返回 None。
    """
    if frame.frame_type != PIN_PARTITION_FRAME_TYPE:
        return None
    rec = frame.raw
    page = ctx.page_name or ""
    entries: list[PinPartitionEntry] = []
    seen_spans: list[tuple[int, int]] = []  # (lstr 偏移, 段尾偏移)
    off = _SCAN_START
    while off < len(rec) - 3:
        text_len = _u16(rec, off)
        if (
            text_len is not None
            and 1 <= text_len <= _MAX_TEXT_LEN
            and off + 2 + text_len + 1 <= len(rec)
            and rec[off + 2 + text_len] == 0
        ):
            chunk = rec[off + 2:off + 2 + text_len]
            decoded = _decode_chunk(chunk)
            if decoded is not None and _is_word_like(decoded[0]):
                text, encoding = decoded
                if not _is_hex_noise(text):
                    x, y = _nearby_coord(rec, off)
                    entries.append(
                        PinPartitionEntry(
                            offset=frame.offset + off,
                            text=text,
                            encoding=encoding,
                            x=x,
                            y=y,
                        )
                    )
                    seen_spans.append((off, off + 2 + text_len + 1))
                # 命中合法文本串才步进（加速）
                off += 2 + text_len + 1
                continue
        # 未命中（噪声/非文本）继续逐字节扫描（防止跳过失配起点）
        off += 1

    # 兜底：可打印串（len≥2、含字母/数字、非纯十六进制、非过长空格）
    for run in re.finditer(rb"[\x20-\x7e]{2,}", rec):
        run_off = run.start()
        text = run.group().decode("latin1")
        if not _is_word_like(text) or _is_hex_noise(text):
            continue
        if text.count(" ") > len(text) * 0.3:
            continue
        # 跳过已被 lstr 覆盖的串（run 起点在 lstr 内容内）
        if any(
            span_start < run_off < span_end
            for span_start, span_end in seen_spans
        ):
            continue
        entries.append(
            PinPartitionEntry(
                offset=frame.offset + run_off,
                text=text,
                encoding="latin1",
            )
        )

    logger.debug(
        "0x0867 frame @0x%x: %d entries", frame.offset, len(entries),
    )
    return PinPartitionTable(
        page=page,
        offset=frame.offset,
        length=len(rec),
        entries=entries,
    )


def scan_pin_partition_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> list[PinPartitionTable]:
    """扫描一页全部 0x0867 帧（实证每页恰 1 帧）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        PinPartitionTable 列表（按帧偏移升序）。
    """
    result: list[PinPartitionTable] = []
    for frame in frames:
        if frame.frame_type != PIN_PARTITION_FRAME_TYPE:
            continue
        table = parse_pin_partition_frame(frame, ctx)
        if table is not None:
            result.append(table)
    return result


def register_pin_partition_decoder() -> None:
    """注册 0x0867 → parse_pin_partition_frame（基类-注册模式）。"""
    from .registry import register_semantic

    register_semantic(PIN_PARTITION_FRAME_TYPE, parse_pin_partition_frame)
