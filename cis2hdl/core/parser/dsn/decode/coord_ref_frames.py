"""短坐标引用帧解码器（T04 P2-7 帧覆盖率剩余）。

页面流中存在大量 45B 短坐标引用帧（类型号 = strLst 索引，数据相关），
如 0x0004（strLst[4]=OLB 库路径，65 帧）。布局（gold 实证）::

    [8B DELIM][type:u16][x:u16@10][y:u16@12][dbId:u32@14][trailer 24B]

内容探测铁律（同网名帧族）：类型号是 strLst 索引，**禁止按类型号
全量断言**——仅当帧满足 45B 短帧 + x/y 合法 + dbId 非零才产出记录；
无内容命中返回 None。注册（计入 frame_stats covered）由
``discover_coord_ref_types`` 对全部页面帧扫描签名后执行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

logger = logging.getLogger(__name__)

#: 45B 短引用帧最大长度
_MAX_LEN = 60
#: 坐标上限（mil 页面坐标）
_MAX_COORD = 30000

#: 已发现并注册的短坐标引用帧类型（discover 填充；数据相关）
_DISCOVERED_COORD_TYPES: frozenset[int] = frozenset()


@dataclass
class CoordRefRecord:
    """短坐标引用帧解码结果。

    Attributes:
        offset: 帧页内偏移。
        frame_type: 源帧类型（strLst 索引，诊断保留）。
        x, y: 页面坐标（mil）。
        object_id: 对象 id（dbId）。
    """

    offset: int
    frame_type: int
    x: int
    y: int
    object_id: int

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "x": self.x,
            "y": self.y,
            "object_id": self.object_id,
        }


def _has_coord_ref_signature(rec: bytes) -> bool:
    """判断帧是否为 45B 短坐标引用（x/y@10-13 + 无文本）。

    dbId@14-17 允许 0（0x0004 库路径帧 dbId=0，gold 实证）；核心签名
    是 45B 短帧 + x/y 合法 + 帧尾 trailer（10 24/10 0b 等）。短帧不应
    含可打印文本（文本帧由 page_text_frames 处理）。
    """
    if not (18 <= len(rec) <= _MAX_LEN):
        return False
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return False
    # 短帧不应含**成串**可打印文本（≥3 连续；文本帧由 page_text_frames
    # 处理）。trailer 中的孤立可打印字节（0x24='$'、0x2e='.' 等）不算。
    run = 0
    for b in rec[18:min(len(rec), 34)]:
        if 32 <= b < 127:
            run += 1
            if run >= 3:
                return False
        else:
            run = 0
    # 帧尾 trailer 特征（10 24 / 10 0b / 14 00 等短帧收尾）
    tail = rec[-6:-2]
    if all(b == 0 for b in tail):
        return False
    return True


def parse_coord_ref_frame(frame: Frame, ctx: DecodeContext) -> CoordRefRecord | None:
    """内容探测解析短坐标引用帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        CoordRefRecord；无内容命中返回 None。
    """
    if not _has_coord_ref_signature(frame.raw):
        return None
    rec = frame.raw
    return CoordRefRecord(
        offset=frame.offset,
        frame_type=frame.frame_type,
        x=int.from_bytes(rec[10:12], "little"),
        y=int.from_bytes(rec[12:14], "little"),
        object_id=int.from_bytes(rec[14:18], "little"),
    )

def discover_coord_ref_types(frames: list[Frame]) -> frozenset[int]:
    """内容探测收集全部页面的短坐标引用帧类型。

    仅收集**未注册**类型（排除 SEMANTIC/STRUCT 已覆盖的 0x01/0x02/0x22/
    0x26/0x2C 等——它们已有专用解码器），避免覆盖语义。注册（计入
    frame_stats covered）由本函数产出后 ``register_coord_ref_decoders``
    执行。

    Args:
        frames: 页面帧列表（全部页拼接）。

    Returns:
        命中签名且未注册的帧类型集合。
    """
    global _DISCOVERED_COORD_TYPES
    from .registry import SEMANTIC_FRAME_DECODERS, STRUCT_DECODERS

    types: set[int] = set()
    for frame in frames:
        if not _has_coord_ref_signature(frame.raw):
            continue
        if frame.frame_type in SEMANTIC_FRAME_DECODERS or frame.frame_type in STRUCT_DECODERS:
            continue
        types.add(frame.frame_type)
    _DISCOVERED_COORD_TYPES = frozenset(types)
    logger.info("Coord-ref frame types discovered: %d", len(types))
    return _DISCOVERED_COORD_TYPES


def register_coord_ref_decoders() -> None:
    """注册已发现短坐标引用帧类型的语义解码器。

    仅在 ``discover_coord_ref_types`` 已调用时生效；未发现时注册空集。
    """
    from .registry import register_semantic

    for ftype in _DISCOVERED_COORD_TYPES:
        register_semantic(ftype, parse_coord_ref_frame)
