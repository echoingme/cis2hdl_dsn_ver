"""Cache 引脚标号映射解析器（架构设计 §3.3，P0-3）。

字节布局（Cache @0x20ee5，len=678 实测）::

    [8B DELIM]                                    +0
    [01 00]                                       +8   记录类型
    [41 00]                                       +10  后续块长度（0x41=65，透传）
    [15 00][Hi5662YV305_DDR_GE_0A][00]            +12  单元名 lstr（len=21）
    [count:u16]                                   count（0A=86；0F=178 主单元）
    [ (len16)(label)(00)(bitmap=7f)               ×N   标号（AA23）
      (len16)(pinname)(00)(bitmap=7f) ]           ×N   引脚名（Y21）
    [trailer: 20 cc 00 00 00 00 00 00 00 20 0b ... 或 ff ff]

注意：
- count 字段与「有效对」不等（0A 段 count=86 但帧长仅容 43 对）→
  解码以**帧尾 trailer 防御**截断（解析越界或遇 ``XX 0b 00 00 00 00 00 00 00 20``/
  ``ff ff`` 停止），不依赖 count 精确性。
- 共 6 段：``Hi5662YV305_DDR_GE_0A(86)/0B(23)/0C(49)/0D(36)/0E(37)/0F(178)``。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.types import DELIMITER

logger = logging.getLogger(__name__)

_DELIM_RE = re.compile(re.escape(DELIMITER))

#: 标号映射记录类型
_LABEL_RECORD_TYPE = 1
#: 单元名前缀（U7 六段 0A~0F）
_UNIT_PREFIX = "Hi5662YV305"
#: 标号/引脚名最大长度
_MAX_NAME_LEN = 16
#: count 上限（防御）
_MAX_COUNT = 300
#: 有效对上限（防御，防死循环）
_MAX_PAIRS = 400
#: 尾部 trailer 特征（``XX 0b 00 00 00 00 00 00 00 20``）
_TRAILER_MARK = b"\x0b\x00\x00\x00\x00\x00\x00\x00\x20"


@dataclass
class PinLabelPair:
    """一条标号↔引脚名映射。"""

    label: str
    pin_name: str

    def to_dict(self) -> dict:
        return {"label": self.label, "pin_name": self.pin_name}


@dataclass
class PinLabelMap:
    """一个单元的引脚标号映射表。"""

    unit_name: str
    pairs: list[PinLabelPair] = field(default_factory=list)

    @property
    def by_label(self) -> dict[str, str]:
        """标号 → 引脚名。"""
        return {p.label: p.pin_name for p in self.pairs}

    @property
    def by_pin_name(self) -> dict[str, str]:
        """引脚名 → 标号。"""
        return {p.pin_name: p.label for p in self.pairs}

    def to_dict(self) -> dict:
        return {
            "unit_name": self.unit_name,
            "pairs": [p.to_dict() for p in self.pairs],
            "by_label": self.by_label,
            "by_pin_name": self.by_pin_name,
        }


def _read_lstr(
    data: bytes, off: int, limit: int,
) -> tuple[str | None, int]:
    """读 u16 len + bytes + 0x00；成功返回 (str, 下一偏移)。"""
    if off + 2 > limit:
        return None, off
    ln = int.from_bytes(data[off:off + 2], "little")
    if ln == 0xFFFF or ln > 500 or off + 2 + ln + 1 > limit:
        return None, off
    if data[off + 2 + ln] != 0:
        return None, off
    try:
        return data[off + 2:off + 2 + ln].decode("latin1"), off + 2 + ln + 1
    except Exception:
        return None, off


def _looks_like_trailer(data: bytes, off: int, limit: int) -> bool:
    """判断 off 处是否为帧尾 trailer（防御截断）。"""
    if off + 2 <= limit and data[off:off + 2] == b"\xff\xff":
        return True
    if off + len(_TRAILER_MARK) <= limit and data[off:off + len(_TRAILER_MARK)] == _TRAILER_MARK:
        return True
    return False


def _parse_pair(
    data: bytes, off: int, limit: int,
) -> tuple[PinLabelPair | None, int]:
    """解析一对 (label, pinname)。

    Layout: ``[len16][label][00][bitmap u8][len16][pinname][00][bitmap u8]``。

    Returns:
        (pair, 下一偏移)；失败返回 (None, off)。
    """
    if off + 2 > limit:
        return None, off
    l1 = int.from_bytes(data[off:off + 2], "little")
    if not (1 <= l1 <= _MAX_NAME_LEN):
        return None, off
    if off + 2 + l1 + 1 + 1 > limit:
        return None, off
    if data[off + 2 + l1] != 0:
        return None, off
    label = data[off + 2:off + 2 + l1].decode("latin1", "replace")
    off2 = off + 2 + l1 + 2
    if off2 + 2 > limit:
        return None, off
    l2 = int.from_bytes(data[off2:off2 + 2], "little")
    if not (1 <= l2 <= _MAX_NAME_LEN):
        return None, off
    if off2 + 2 + l2 + 1 + 1 > limit:
        return None, off
    if data[off2 + 2 + l2] != 0:
        return None, off
    pin_name = data[off2 + 2:off2 + 2 + l2].decode("latin1", "replace")
    next_off = off2 + 2 + l2 + 2
    return PinLabelPair(label=label, pin_name=pin_name), next_off


def scan_pin_label_maps(data: bytes) -> list[PinLabelMap]:
    """扫描 Cache 流全部引脚标号映射段。

    逐帧：type==1 && 合法 name lstr（以 ``Hi5662YV305`` 开头）&&
    count ∈ [4, 300] && 成对解析（label+pinname 均 len16+00+bitmap），
    trailer 防御截断。

    Args:
        data: Cache 流原始字节。

    Returns:
        PinLabelMap 列表（按出现顺序；实证 6 段）。
    """
    result: list[PinLabelMap] = []
    for m in _DELIM_RE.finditer(data):
        p = m.end()
        if p + 2 > len(data):
            break
        record_type = int.from_bytes(data[p:p + 2], "little")
        if record_type != _LABEL_RECORD_TYPE:
            continue
        # 跳过块长度 u16（0x41 等，透传），随后读单元名 lstr
        if p + 4 > len(data):
            continue
        name, p2 = _read_lstr(data, p + 4, len(data))
        if name is None or not name.startswith(_UNIT_PREFIX):
            continue
        if p2 + 2 > len(data):
            continue
        count = int.from_bytes(data[p2:p2 + 2], "little")
        if not (4 <= count <= _MAX_COUNT):
            continue
        q = p2 + 2
        pairs: list[PinLabelPair] = []
        guard = 0
        while q + 5 <= len(data) and guard < _MAX_PAIRS:
            guard += 1
            if _looks_like_trailer(data, q, len(data)):
                break
            pair, next_off = _parse_pair(data, q, len(data))
            if pair is None:
                break
            pairs.append(pair)
            q = next_off
        if not pairs:
            continue
        result.append(PinLabelMap(unit_name=name, pairs=pairs))
        logger.debug(
            "PinLabelMap %s: %d pairs", name, len(pairs),
        )
    logger.info("Pin label maps parsed: %d units", len(result))
    return result
