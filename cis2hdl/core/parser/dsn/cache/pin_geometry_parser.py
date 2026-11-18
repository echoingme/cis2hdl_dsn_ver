"""Cache type-4 SymbolPin 几何解析器（P1-1）。

字节布局（TG0C0D2 Cache 流实测，99 个 type-4 帧）：:

    [8B DELIM][type:u16=4][name zstr][prefix:u32]
    [startX:i32][startY:i32][hotptX:i32]   ← 3 个坐标（无 hotptY）
    [pinShape:u16][unknown:u16][portType:u32]

- name：引脚名（0 终止字符串，如 'TIP1' / 'GATE' / 'GND2' / 'DQL0'）。
- prefix：前缀 u32（实证 20/80/90/100/130 等，疑似单元序号）。
- startX/startY：引脚起点坐标；hotptX：热热点 x（hotptY 缺失）。
- pinShape：实证恒 0x21（33）。
- unknown：0x77d3 或 0（保留字段，透传）。
- portType：实证恒 1（INPUT）。

**坐标校验**：80 个有效几何（startX/startY 均为正 mil 坐标）；其余
19 个为 LibraryPart 前导帧（含 OLB 路径/包名，坐标非法）自动排除。

与页面级 26/27 短引用的关联：
    页面 26/27 帧 ``[x][y][dbId]`` 的 dbId ≈ Cache 几何 object_id；
    实现按 ``(x, y)`` 精确匹配 + 名称回填（见 binder/connectivity）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.types import DELIMITER

logger = logging.getLogger(__name__)

_DELIM_RE = re.compile(re.escape(DELIMITER))

#: type-4 几何帧类型
_GEOM_TYPE = 4
#: 坐标上限（mil）
_MAX_COORD = 30000
#: 引脚名最大长度（防御）
_MAX_NAME_LEN = 64
#: prefix 上限（防御）
_MAX_PREFIX = 10000
#: 解析上限（防御防死循环）
_MAX_GEOMS = 2000


@dataclass
class CachePinGeometry:
    """Cache type-4 SymbolPin 几何。

    Attributes:
        pin_name: 引脚名（如 'DQL0'）。
        start_x, start_y: 引脚起点坐标。
        hotpt_x: 热热点 x（hotptY 缺失，恒 0）。
        pin_shape: pinShape（实证 0x21=33）。
        port_type: portType（实证 1=INPUT）。
        unknown: 保留字段（0x77d3/0）。
        prefix: 前缀 u32（单元序号候选）。
        offset: 帧页内偏移。
    """

    pin_name: str
    start_x: int
    start_y: int
    hotpt_x: int = 0
    pin_shape: int = 0
    port_type: int = 0
    unknown: int = 0
    prefix: int = 0
    offset: int = 0

    def to_dict(self) -> dict:
        return {
            "pin_name": self.pin_name,
            "start_x": self.start_x,
            "start_y": self.start_y,
            "hotpt_x": self.hotpt_x,
            "pin_shape": self.pin_shape,
            "port_type": self.port_type,
            "unknown": self.unknown,
            "prefix": self.prefix,
            "offset": self.offset,
        }


def _read_zstr(data: bytes, off: int, limit: int) -> tuple[str | None, int]:
    """读 0 终止字符串。

    Args:
        data: 缓冲。
        off: 起始偏移。
        limit: 上限。

    Returns:
        (串, 终止符之后偏移)；失败返回 (None, off)。
    """
    if off >= limit:
        return None, off
    z = data.find(b"\x00", off, min(off + _MAX_NAME_LEN + 1, limit))
    if z == -1:
        return None, off
    raw = data[off:z]
    if not raw:
        return None, off
    if not all(32 <= b < 127 for b in raw):
        return None, off
    return raw.decode("latin1"), z + 1


def parse_pin_geometry(data: bytes, offset: int) -> CachePinGeometry | None:
    """解析单个 type-4 几何帧。

    Args:
        data: 帧原始字节（含 8B 分隔符）。
        offset: 帧页内偏移。

    Returns:
        CachePinGeometry；坐标非法（LibraryPart 前导帧）返回 None。
    """
    if len(data) < 10:
        return None
    ftype = int.from_bytes(data[8:10], "little")
    if ftype != _GEOM_TYPE:
        return None
    name, p = _read_zstr(data, 10, len(data))
    if name is None:
        return None
    if p + 4 + 12 + 8 > len(data):
        return None
    prefix = int.from_bytes(data[p:p + 4], "little")
    start_x = int.from_bytes(data[p + 4:p + 8], "little", signed=True)
    start_y = int.from_bytes(data[p + 8:p + 12], "little", signed=True)
    hotpt_x = int.from_bytes(data[p + 12:p + 16], "little", signed=True)
    pin_shape = int.from_bytes(data[p + 16:p + 18], "little")
    unknown = int.from_bytes(data[p + 18:p + 20], "little")
    port_type = int.from_bytes(data[p + 20:p + 24], "little")
    if not (0 <= prefix <= _MAX_PREFIX):
        return None
    # 坐标校验：符号内坐标为相对原点（可负），仅排除 LibraryPart
    # 前导帧（OLB 路径/包名被误读为超大坐标，如 977666090）。
    if not (-_MAX_COORD < start_x < _MAX_COORD and -_MAX_COORD < start_y < _MAX_COORD):
        return None
    return CachePinGeometry(
        pin_name=name,
        start_x=start_x,
        start_y=start_y,
        hotpt_x=hotpt_x,
        pin_shape=pin_shape,
        port_type=port_type,
        unknown=unknown,
        prefix=prefix,
        offset=offset,
    )


def scan_cache_pin_geometries(data: bytes) -> list[CachePinGeometry]:
    """扫描 Cache 流全部 type-4 几何。

    Args:
        data: Cache 流原始字节。

    Returns:
        CachePinGeometry 列表（按出现顺序；实证 80 个有效几何）。
    """
    result: list[CachePinGeometry] = []
    guard = 0
    for m in _DELIM_RE.finditer(data):
        guard += 1
        if guard > _MAX_GEOMS:
            break
        geom = parse_pin_geometry(data[m.start():], m.start())
        if geom is not None:
            result.append(geom)
    logger.info("Cache pin geometries parsed: %d", len(result))
    return result
