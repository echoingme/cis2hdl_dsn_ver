"""Cache 流引脚定义扫描器（T04 逆向实证：小 IC 引脚名+几何）。

背景（2026-08-20 逆向突破）：
- Packages 流仅含 11 个大符号 pinMap（Hi5662/DDR3/2P5GEPHY…），
  3A BUCK/LDO_1/PSW6M2/SOP8 等小 IC 的引脚定义**不在 Packages 流**；
- 用户指出：OLB 是从 DSN 分离出来的，DSN Cache 流必然包含全部符号
  定义（含引脚）。实测证实：**Cache 流存在 914 个引脚名帧**，格式::

    [8B DELIM][type:u16][name(len==type)][00]
    [x1:i32][y1:i32][x2:i32][y2:i32]   ← 引脚线（符号局部 mil）

  帧类型 u16 == 引脚名字符串长度（SW→2、GND/VIN→3、BOOT→4），
  载荷 = 引脚名 + 引脚引出线坐标（体框边缘→外部 20mil）：
  - 右侧引脚：x 70→90（体框右缘→外）
  - 左侧引脚：x 0→-20（体框左缘→外）
- 与带 DELIM 0x2c2c 引脚线帧的关系：0x2c2c 是**几何线**（无名字），
  本扫描器是**命名引脚**（名字+线），两者互补。

输出 ``CachePinDef``：symbol 归属 + name + 引脚线端点 + 帧偏移。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..frame.types import DELIMITER

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

#: 帧类型上限（引脚名长度，实际 1..40 已覆盖全部）
_MAX_NAME_LEN = 40
#: 坐标上限（符号局部 mil，小 IC <2000；DDR/U7 大符号 <4000）
_MAX_PIN_COORD = 4000

_DELIM_RE = re.compile(re.escape(DELIMITER))


@dataclass
class CachePinDef:
    """Cache 命名引脚定义。

    Attributes:
        symbol: 所属符号名（如 '3A BUCK'，去 .Normal）。
        name: 引脚名（如 'SW'/'GND'/'DDR_ADDR0'）。
        x1, y1, x2, y2: 引脚引出线端点（符号局部 mil）。
        offset: 帧在 Cache 流中的偏移。
    """

    symbol: str
    name: str
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0
    offset: int = 0


def _owner_part(regions: list[tuple[int, int, str]], offset: int) -> str:
    """返回包含 offset 的 part 名；区域外返回空串。"""
    for start, end, name in regions:
        if start <= offset < end:
            return name
    return ""


def _scan_part_regions(data: bytes) -> list[tuple[int, int, str]]:
    """扫描 Cache 流 LibraryPart 区域边界（与 symbol_geometry 同款）。"""
    starts: list[tuple[int, str]] = []
    for m in _DELIM_RE.finditer(data):
        p = m.end()
        if p + 2 > len(data):
            continue
        ln = int.from_bytes(data[p:p + 2], "little")
        if not (1 <= ln <= 200) or p + 2 + ln + 1 > len(data):
            continue
        cand = data[p + 2:p + 2 + ln]
        if not (cand.endswith(b".Normal") or cand.endswith(b".Convert")):
            continue
        if not all(32 <= b < 127 for b in cand):
            continue
        starts.append((m.start(), cand.decode("latin1").rsplit(".", 1)[0]))
    regions: list[tuple[int, int, str]] = []
    for i, (start, name) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(data)
        regions.append((start, end, name))
    return regions


def _parse_pin_def(data: bytes, offset: int) -> CachePinDef | None:
    """解析单个 Cache 引脚名帧。

    布局（实证）：``[DELIM][type:u16][name(len==type)][00][x1][y1][x2][y2]``。
    type 1..40、name 全 ASCII 可打印、坐标在合法范围内才接受。

    Args:
        data: Cache 流原始字节（从 DELIM 起）。
        offset: 帧起始偏移。

    Returns:
        CachePinDef；非引脚名帧返回 None。
    """
    if len(data) < 10:
        return None
    t = int.from_bytes(data[8:10], "little")
    if not (1 <= t <= _MAX_NAME_LEN):
        return None
    name_b = data[10:10 + t]
    if len(name_b) != t or not all(32 <= b < 127 for b in name_b):
        return None
    if data[10 + t:10 + t + 1] != b"\x00":
        return None
    # 坐标块（符号局部 mil；缺省为 0）
    coords_off = 10 + t + 1
    vals: list[int] = []
    for k in range(4):
        pos = coords_off + k * 4
        if pos + 4 > len(data):
            vals.append(0)
            continue
        v = int.from_bytes(data[pos:pos + 4], "little", signed=True)
        if abs(v) > _MAX_PIN_COORD:
            return None  # 超大值 = 误匹配（非引脚帧）
        vals.append(v)
    return CachePinDef(
        symbol="",
        name=name_b.decode("latin1"),
        x1=vals[0], y1=vals[1], x2=vals[2], y2=vals[3],
        offset=offset,
    )


def scan_cache_pin_defs(data: bytes) -> list[CachePinDef]:
    """扫描 Cache 流全部命名引脚定义，按 LibraryPart 区域归属。

    Args:
        data: Cache 流原始字节。

    Returns:
        CachePinDef 列表（914+ 预期；含归属 symbol 名）。
    """
    regions = _scan_part_regions(data)
    result: list[CachePinDef] = []
    seen: set[tuple[str, str]] = set()
    for m in _DELIM_RE.finditer(data):
        off = m.start()
        pd = _parse_pin_def(data[off:], off)
        if pd is None:
            continue
        pd.symbol = _owner_part(regions, off)
        # 去重：同名同符号只保留首帧（Cache 多单元符号 0A-0F 同名引脚）
        key = (pd.symbol, pd.name)
        if key in seen:
            continue
        seen.add(key)
        result.append(pd)
    logger.info("Cache pin defs parsed: %d", len(result))
    return result
