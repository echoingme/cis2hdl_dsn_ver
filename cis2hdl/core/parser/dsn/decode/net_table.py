"""页面头部网名表解析（架构设计 §2 L3 + QA A.3 + P0-6）。

布局（QA 实证）：``[00 00 00 00][count:u8][ (len16)(name)(00)(id32) ]*count``
全页合计 609 网名。

P0-6：count 支持 u16 探测 + u8 回退（最大页 117 网未超 255，u16 分支
由单测构造 >255 条网名表验证）。``NetTable.count_bytes`` 记录计数宽度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: u8 计数表头（00 00 00 00 + count:u8 + 00 + 条目首 len16）
_NET_TABLE_RE = re.compile(rb"\x00\x00\x00\x00([\x01-\x7f])\x00")
#: u16 计数表头候选：00 00 00 00 + count:u16（其后可直连条目，也可带 00 分隔）
_NET_TABLE_HEADER_RE = re.compile(rb"\x00\x00\x00\x00")


@dataclass
class NetTable:
    """页面头部网名表。"""

    offset: int | None
    names: dict[int, str] = field(default_factory=dict)
    #: P0-6：计数宽度（1=u8 旧 | 2=u16 新）
    count_bytes: int = 1

    def name_of(self, net_id: int | None) -> str:
        if net_id is None:
            return ""
        return self.names.get(net_id, f"NET_{net_id}")


def _parse_at(data: bytes, off: int, cnt: int, count_bytes: int) -> dict[int, str] | None:
    """从 off 处解析 count 条网名条目。

    Args:
        data: 页面流原始字节。
        off: 条目起始偏移（count 字段之后）。
        cnt: 条目数。
        count_bytes: 计数宽度（1/2）。

    Returns:
        {net_id: name}；任一校验失败返回 None。
    """
    p = off
    names: dict[int, str] = {}
    for _ in range(cnt):
        if p + 2 > len(data):
            return None
        l = int.from_bytes(data[p:p + 2], "little")
        if l < 1 or l > 64 or p + 2 + l + 1 + 4 > len(data):
            return None
        name = data[p + 2:p + 2 + l]
        if not all(32 <= b < 127 for b in name):
            return None
        p += 2 + l
        if data[p] != 0:
            return None
        nid = int.from_bytes(data[p + 1:p + 5], "little")
        names[nid] = name.decode("latin1")
        p += 5
    if len(names) != cnt:
        return None
    return names


def _try_u16(data: bytes) -> NetTable | None:
    """尝试 u16 计数解析。

    扫描 ``[00 00 00 00][cnt:u16]`` 候选：cnt 落在 [3, 3000]，
    随后条目直接起始（len16+name+00+nid32），可选带一个 00 分隔字节。
    任一强校验失败（条目越界/非法长度/非 ASCII/缺终止/重复 nid）即放弃该候选。
    """
    for m in _NET_TABLE_HEADER_RE.finditer(data):
        off = m.start()
        if off + 6 > len(data):
            continue
        cnt = int.from_bytes(data[off + 4:off + 6], "little")
        # 只有 cnt>255 才可能是真正的 u16 表：cnt<=255 时 u8/u16 布局字节完全相同，
        # 高字节为 0，按旧 u8 处理（count_bytes=1）与历史行为一致。
        if cnt <= 255 or cnt > 3000:
            continue
        # 优先：条目直连（与 u8 布局一致，仅计数宽度不同）
        names = _parse_at(data, off + 6, cnt, 2)
        if names is not None and len(names) == cnt:
            return NetTable(offset=off, names=names, count_bytes=2)
        # 备选：00 分隔字节（00 00 00 00 + cnt:u16 + 00 + 条目）
        if off + 7 <= len(data) and data[off + 6] == 0:
            names = _parse_at(data, off + 7, cnt, 2)
            if names is not None and len(names) == cnt:
                return NetTable(offset=off, names=names, count_bytes=2)
    return None


def _scan_scattered_names(data: bytes) -> dict[int, str]:
    """扫描散落网名记录 ``[len16][ASCII][00][nid32]``（稀疏页兜底）。

    仅当标准网名表解析失败时调用；nid 限 100000-999999（OrCAD 网名 id
    域）防误检。返回 {nid: name}；无则空 dict。
    """
    names: dict[int, str] = {}
    for off in range(0, len(data) - 9):
        l = int.from_bytes(data[off:off + 2], "little")
        if not (1 <= l <= 64):
            continue
        name = data[off + 2:off + 2 + l]
        if not all(32 <= b < 127 for b in name):
            continue
        if data[off + 2 + l] != 0:
            continue
        nid = int.from_bytes(data[off + 2 + l + 1:off + 2 + l + 5], "little")
        if 100000 <= nid <= 999999:
            names.setdefault(nid, name.decode("latin1"))
    return names


def parse_net_table(data: bytes) -> NetTable:
    """解析页面头部网名表。

    先试 u16 计数（``[00 00 00 00][cnt:u16]``），校验失败回退 u8。
    校验：条目数==cnt、每条 len16+ASCII+00+nid32、nid 无重复、cnt>=3。

    Args:
        data: 页面流原始字节。

    Returns:
        NetTable（offset 为网名表起点，None 表示未找到）。
    """
    # P0-6: u16 优先（强校验 + u8 回退）
    u16_result = _try_u16(data)
    if u16_result is not None:
        return u16_result

    for m in _NET_TABLE_RE.finditer(data):
        off = m.start()
        cnt = m.group(1)[0]
        p = off + 6
        names = _parse_at(data, p, cnt, 1)
        if names is not None and len(names) == cnt and cnt >= 3:
            return NetTable(offset=off, names=names, count_bytes=1)
    # 散落网名记录兜底（2026-08-20 晚，主理人实证）：10_TG0_GND 页的
    # 网名表 count 字段布局异常（稀疏 GND 页），标准表解析失败 →
    # 网名以独立记录 ``[len16][ASCII][00][nid32]`` 散落页面
    # （GND nid=430119 @401 等）。扫描捕获，修复 U7 VSS 等 176 条
    # 连接网名兜底 'NET_{id}' 的缺口。
    names = _scan_scattered_names(data)
    if names:
        return NetTable(offset=None, names=names, count_bytes=1)
    return NetTable(offset=None, names={})
