"""OffPage 连接符解码器（P2-5 展示维度）。

结构类型 38（0x26）OffPageConnector 45B 引用帧（gold 实证）::

    [8B DELIM][26 00][x:u16][y:u16][dbId:u32][trailer 24B]

关联跨页 net：
1. 坐标 ∈ 走线端点 → 网名表（net_table）；
2. 最近网标文本（≤200mil）→ 文本即网名；
3. 兜底 None。

输出 ``OffPageConnector``：x/y/dbId/net_name/offset。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    from .net_table import NetTable

logger = logging.getLogger(__name__)

#: OffPageConnector 结构类型
OFFPAGE_FRAME_TYPE = 38
#: 45B 引用帧最大长度
_REF_MAX_LEN = 60
#: 坐标上限
_MAX_COORD = 30000


@dataclass
class OffPageConnector:
    """OffPage 连接符解码结果。

    Attributes:
        offset: 帧页内偏移。
        x, y: 页面坐标（mil）。
        object_id: 对象 id（dbId）。
        net_name: 关联网名（尽力而为；未命中空串）。
        target_pages: 跨页目标页（尽力而为；未命中空列表）。
    """

    offset: int
    x: int
    y: int
    object_id: int = 0
    net_name: str = ""
    #: 2026-08-21：符号名（'OFFSHEET2'/'ONSHEET2'，内容探测帧）
    name: str = ""
    #: 2026-08-21：符号 bbox（网名关联用边缘匹配）
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    #: 2026-08-21：帧类型 u16（strLst 索引语义；无网名帧可作兜底来源）
    frame_type: int = 0
    target_pages: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "x": self.x,
            "y": self.y,
            "object_id": self.object_id,
            "net_name": self.net_name,
            "name": self.name,
            "x1": self.x1, "y1": self.y1, "x2": self.x2, "y2": self.y2,
            "frame_type": self.frame_type,
            "target_pages": list(self.target_pages),
        }


def parse_offpage_connector(
    frame: Frame, ctx: DecodeContext,
) -> OffPageConnector | None:
    """解析 OffPageConnector 45B 引用帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        OffPageConnector；非 type 38 帧或坐标非法返回 None。
    """
    if frame.frame_type != OFFPAGE_FRAME_TYPE:
        return None
    rec = frame.raw
    if len(rec) > _REF_MAX_LEN or len(rec) < 18:
        return None
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    object_id = int.from_bytes(rec[14:18], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    return OffPageConnector(
        offset=frame.offset,
        x=x,
        y=y,
        object_id=object_id,
    )


def scan_offpage_connectors(
    frames: list[Frame], ctx: DecodeContext,
) -> list[OffPageConnector]:
    """扫描一页全部 OffPage 连接符（含跨页 net 关联）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        OffPageConnector 列表（按帧偏移升序）。
    """
    result: list[OffPageConnector] = []
    for frame in frames:
        conn = parse_offpage_connector(frame, ctx)
        if conn is not None:
            result.append(conn)
    result.sort(key=lambda c: c.offset)
    return result


def associate_offpage_nets(
    connectors: list[OffPageConnector],
    endpoint_to_net: dict[tuple[int, int], str],
) -> list[OffPageConnector]:
    """按坐标关联 OffPage 连接符网名（尽力而为）。

    Args:
        connectors: OffPage 连接符列表。
        endpoint_to_net: 走线端点 → 网名映射（同页）。

    Returns:
        回填 net_name 后的连接符列表。
    """
    for conn in connectors:
        # 2026-08-21：中心 + bbox 边缘匹配（连接符引脚贴走线端点）
        net = endpoint_to_net.get((conn.x, conn.y))
        if not net and (conn.x1 or conn.y1 or conn.x2 or conn.y2):
            for (_x, _y), _n in endpoint_to_net.items():
                if (abs(_x - conn.x) <= 40 and abs(_y - conn.y) <= 40):
                    net = _n
                    break
                # 边缘（含容差 25）
                if (min(conn.x1, conn.x2) - 25 <= _x <= max(conn.x1, conn.x2) + 25
                        and min(conn.y1, conn.y2) - 25 <= _y <= max(conn.y1, conn.y2) + 25):
                    net = _n
                    break
        if net:
            conn.net_name = net
    return connectors


def register_offpage_decoder() -> None:
    """注册 OffPageConnector 结构解码器。"""
    from .registry import register_struct

    register_struct(OFFPAGE_FRAME_TYPE, parse_offpage_connector)


#: 2026-08-21：strLst 属性名（帧尾 25-值索引过滤，非网名）
_CONN_ATTR_NAMES = {
    "DEVICE", "Pin Order", "SINGAL", "NETNAME", "PINORDER",
    "NetCreated", "NetImplemented", "PIID", "HETERO", "VALUE",
    "Part Reference", "GND", "Power Pins Visible", "-",
}


def scan_offpage_connectors_content(
    data: bytes, strlst: "list | None" = None,
) -> list[OffPageConnector]:
    """2026-08-21 内容探测：扫描页面流全部 offsheet/onsheet 连接符帧。

    帧类型 = strLst 索引（数据相关），OFFPAGE_FRAME_TYPE=38 仅覆盖
    本数据部分帧（off_pages 6 个）——ONSHEET2/OFFSHEET2 符号名帧
    （169 个，类型 1033/1690/1072 等各不相同）此前全部漏解析。
    帧结构（字节级实证）：
      [DELIM][type:u16][4:u32][len:u16][符号名][00][方向标记 2B][00 00]
      [x1:u16][y1:u16][x2:u16][y2:u16]...  ← bbox（符号局部）

    Returns:
        OffPageConnector 列表（name='ONSHEET2'/'OFFSHEET2'，
        net_name 待 associate_offpage_nets 走线端点回填）。
    """
    import re as _re
    import struct as _st

    result: list[OffPageConnector] = []
    start = 0
    while True:
        i = data.find(b"\xff\xe4\x5c\x39\x00\x00\x00\x00", start)
        if i < 0:
            break
        end = data.find(b"\xff\xe4\x5c\x39\x00\x00\x00\x00", i + 8)
        if end < 0:
            end = len(data)
        raw = data[i:end]
        m = _re.search(rb"(OFFSHEET2|ONSHEET2|BISHEET)\x00", raw)
        if m:
            after = raw[m.end() + 4:]
            if len(after) >= 8:
                x1, y1, x2, y2 = _st.unpack_from("<4H", after, 0)
                if (0 < x1 < 5000 and 0 < y1 < 5000
                        and 0 < x2 < 5000 and 0 < y2 < 5000):
                    # 2026-08-21：帧尾属性对 ``25 <u16 idx> <u16 val>`` =
                    # strLst 值索引（字节级实证：25 79 00 00 00 →
                    # strLst[121]='DDR_A11'、25 4B 01 00 00 → strLst[331]=
                    # 'MDI_DN_3'）——val 可能非零（25 73 00 00 21 →
                    # strLst[115]='MDI_AN_3'），正则只锚定 idx。
                    # 取第一个非属性名/非路径的值作为网名。
                    _net = ""
                    if strlst:
                        # 仅从符号名之后搜索（属性区），避免 bbox 字节误匹配
                        _tail = raw[m.end():]
                        for _vm in _re.finditer(rb"\x25(..)", _tail):
                            _idx = _st.unpack_from("<H", _vm.group(1), 0)[0]
                            _v = strlst[_idx] if _idx < len(strlst) else ""
                            if _v and _v not in _CONN_ATTR_NAMES \
                                    and not _re.search(r"[:\\/]", _v):
                                _net = _v
                                break
                        # 兜底：25 属性区无有效网名时，frame_type（strLst
                        # 索引语义）可能即网名（字节级实证：type=518 →
                        # 'TXDN_XPON'、type=757 → '25GE_TRXN0_R'，
                        # 均系页内真实网络）
                        if not _net:
                            _ft = _st.unpack_from("<H", raw, 8)[0]
                            _v = strlst[_ft] if _ft < len(strlst) else ""
                            if _v and _v not in _CONN_ATTR_NAMES \
                                    and not _re.search(r"[:\\/]", _v):
                                _net = _v
                    result.append(OffPageConnector(
                        x=(x1 + x2) // 2,
                        y=(y1 + y2) // 2,
                        net_name=_net,
                        name=m.group(1).decode(),
                        x1=x1, y1=y1, x2=x2, y2=y2,
                        frame_type=_st.unpack_from("<H", raw, 8)[0],
                        offset=i,
                    ))
        start = i + 8
    return result
