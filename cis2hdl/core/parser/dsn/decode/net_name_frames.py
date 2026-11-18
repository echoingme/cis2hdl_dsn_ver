"""网名帧解码器（P2-B 引脚级网络全量）。

帧族：marker30 + 字符串帧（QA 帧覆盖审计 §5.3「A 层 source symbol 同族
网名帧」，812 帧 / 111 类型号，与 0x0046 逐字节同构）。类型号 = strLst
索引（数据相关），因此**内容探测优先于类型白名单**。

字节布局（TG0C0D2 0x01e0 实测）::

    [8B DELIM][type:u16][??:u16][row:u16@12][00 00]
    [30 00 00 00@14][00 00 00 00][04 00@22][attr:u16]
    [len:u16@26][net_name][00][03 00 00 00][05 00 00 00][trailer]

语义：
- ``row``（偏移 12..14）＝ 该网名所在走线行（页面 mil y 坐标，实证
  DDR_A0 帧 row=330 ↔ 走线 DDR_A0 (470,330)-(580,330)）；
- ``net_name``（偏移 26 起 lstr）＝ 网名（DDR_A0 / 25GE_MDIO_R / ...）；
- ``30 00 00 00`` 几何标记（与 source symbol 同族）；
- ``04 00`` 名称前子标记。

防误检铁律（BISHEET 教训）：
- 必须 marker30 + 名称 lstr 全命中才产出；
- 名称以 ``$`` 开头（内部网 id，如 ``$8N103353``）→ 拒绝；
- 无名称 → 返回 None（不产出、不抛异常）。

覆盖率口径：注册进 SEMANTIC_FRAME_DECODERS 的帧类型按内容探测确认
（``discover_net_name_types`` 对全部页面帧扫描 marker30+名称签名），
registered 的帧即使 decode 返回 None 也计为 covered（口径与审计一致）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

#: marker30 几何标记
_MARKER30 = b"\x30\x00\x00\x00"
#: 名称前子标记（04 00）
_NAME_MARK = b"\x04\x00"
#: 名称最大长度（网名）
_MAX_NAME_LEN = 48
#: row 坐标上限（mil 页面坐标）
_MAX_COORD = 30000
#: 名称 lstr 扫描起点/上限（防御）
_NAME_SCAN_START = 22
_NAME_SCAN_END = 90

#: 已发现并注册的网名帧类型（discover_net_name_types 填充；数据相关）
_DISCOVERED_NET_NAME_TYPES: frozenset[int] = frozenset()


@dataclass
class NetNameRecord:
    """网名帧解码结果。

    Attributes:
        offset: 帧页内偏移。
        frame_type: 源帧类型（strLst 索引，诊断保留）。
        net_name: 网名（如 DDR_A0 / 25GE_MDIO_R）。
        row: 走线行 y（页面 mil 坐标）。
        object_id: 帧 dbId 低 16 位（诊断保留）。
    """

    offset: int
    frame_type: int
    net_name: str
    row: int
    object_id: int = 0

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "net_name": self.net_name,
            "row": self.row,
            "object_id": self.object_id,
        }


def _read_name_lstr(rec: bytes) -> tuple[str | None, int]:
    """在帧内读网名 lstr（``[len:u16][name][00]``）。

    Args:
        rec: 帧原始字节。

    Returns:
        (网名, 名称后偏移)；无命中返回 (None, -1)。
    """
    for off in range(_NAME_SCAN_START, min(len(rec) - 3, _NAME_SCAN_END)):
        ln = int.from_bytes(rec[off:off + 2], "little")
        if not (2 <= ln <= _MAX_NAME_LEN):
            continue
        if off + 2 + ln + 1 > len(rec):
            continue
        if rec[off + 2 + ln] != 0:
            continue
        raw = rec[off + 2:off + 2 + ln]
        if not all(32 <= b < 127 for b in raw):
            continue
        name = raw.decode("latin1")
        # 内部网 id / 噪声拒绝
        if name.startswith("$") or name.startswith("INS"):
            continue
        return name, off + 2 + ln + 1
    return None, -1


def parse_net_name_frame(frame: Frame, ctx: DecodeContext) -> NetNameRecord | None:
    """内容探测解析单个网名帧。

    防误检：marker30 + 名称 lstr 全命中才产出；无名称返回 None。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        NetNameRecord；无内容命中返回 None。
    """
    rec = frame.raw
    if len(rec) < 34:
        return None
    # 特征 1：marker30（偏移 10 之后）
    idx = rec.find(_MARKER30, 10)
    if idx == -1:
        return None
    # 特征 2：名称前子标记（04 00）在 marker30 之后
    mark = rec.find(_NAME_MARK, idx + 4)
    if mark == -1 or mark > idx + 24:
        return None
    # 特征 3：名称 lstr
    name, _end = _read_name_lstr(rec)
    if name is None:
        return None
    # 特征 4：row 坐标（偏移 12..14）
    row = int.from_bytes(rec[12:14], "little")
    if not (0 < row < _MAX_COORD):
        return None
    object_id = int.from_bytes(rec[10:14], "little")
    return NetNameRecord(
        offset=frame.offset,
        frame_type=frame.frame_type,
        net_name=name,
        row=row,
        object_id=object_id,
    )


def _has_net_name_signature(rec: bytes) -> bool:
    """判断帧是否含网名帧内容签名（marker30 + 04 00 + 名称 lstr）。"""
    if len(rec) < 34:
        return False
    if _MARKER30 not in rec[10:]:
        return False
    idx = rec.find(_MARKER30, 10)
    mark = rec.find(_NAME_MARK, idx + 4)
    if mark == -1 or mark > idx + 24:
        return False
    name, _ = _read_name_lstr(rec)
    if name is None:
        return False
    row = int.from_bytes(rec[12:14], "little")
    return 0 < row < _MAX_COORD


def discover_net_name_types(frames: list[Frame]) -> frozenset[int]:
    """内容探测收集全部页面的网名帧类型（数据相关，不硬编码白名单）。

    Args:
        frames: 页面帧列表（全部页拼接）。

    Returns:
        命中内容签名的帧类型集合。
    """
    global _DISCOVERED_NET_NAME_TYPES
    types: set[int] = set()
    for frame in frames:
        if _has_net_name_signature(frame.raw):
            types.add(frame.frame_type)
    _DISCOVERED_NET_NAME_TYPES = frozenset(types)
    logger.info("Net-name frame types discovered: %d", len(types))
    return _DISCOVERED_NET_NAME_TYPES


def register_net_name_decoders() -> None:
    """注册已发现网名帧类型的语义解码器（SEMANTIC_FRAME_DECODERS）。

    仅在 ``discover_net_name_types`` 已调用时生效；未发现时注册空集
    （防御，不影响其它解码器）。
    """
    from .registry import register_semantic

    for ftype in _DISCOVERED_NET_NAME_TYPES:
        register_semantic(ftype, parse_net_name_frame)


def scan_net_name_frames(
    frames: list[Frame], ctx: DecodeContext,
) -> list[NetNameRecord]:
    """扫描一页全部网名帧（内容探测，不依赖类型白名单）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        NetNameRecord 列表（按帧偏移升序）。
    """
    result: list[NetNameRecord] = []
    for frame in frames:
        rec = parse_net_name_frame(frame, ctx)
        if rec is not None:
            result.append(rec)
    result.sort(key=lambda r: r.offset)
    return result
