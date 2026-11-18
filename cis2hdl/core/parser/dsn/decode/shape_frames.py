"""外形名帧解码器（架构设计 §2 L3 + P2-A 坐标补全）。

扫描帧内 ``X.Normal`` / ``X.Convert`` 外形名 + 坐标。
实测：shape 帧类型多为 0x6xx（strLst 索引对应外形名），
但也可能出现在其它帧类型中（如 0x02 属性帧内嵌）。

注意：一页有 1700+ 含 ``.Normal`` 串的帧，但其中大量是图形/引脚帧；
实例绑定只取"含完整外形名（<=60 字符）且非路径"的帧。

P2-A 多形态坐标提取（字节级实证，见核心文档 A §5.3 难点对策）：
- 形态 A（GraphicInst，70B+ 标准外形帧）::

      [len16][shape.Normal][00][dbId:u32][locY:i16][locX:i16]
      [y2:i16][x2:i16][x1:i16][y1:i16][color:u8][...]

  字段顺序实证（OpenOrCadParser StructGraphicInst::read）：locY 在前、
  locX 在后；**EXP/CSV Location = bbox 角 (x1, y1)**（TG0C0D2 737/737
  交叉验证 100%），locX/locY 为渲染原点（符号文本平移用）。
- 形态 B（marker30 标记帧）：帧内含 ``30 00 00 00`` 几何标记，
  坐标候选在标记前/后（best-effort 扫描 u16 对）。
- 形态 C（45B 短引用帧）：``[DELIM][type][x:u16][y:u16][dbId:u32][trailer]``。
- 形态 D（62B/83B 图形帧）：全帧 u16 坐标对扫描（防御性）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..frame.splitter import Frame
from .registry import DecodeContext

_SHAPE_RE = re.compile(r"^[A-Za-z0-9_\-\. ]{1,}\.(Normal|Convert)$")
#: 坐标定位正则：允许 1 字符外形名（R.Normal / C.Normal 等）
_SHAPE_COORD_RE = re.compile(rb"[\x20-\x7e]{1,}\.(Normal|Convert)\x00")
#: marker30 几何标记（source symbol / 图形几何）
_MARKER30 = b"\x30\x00\x00\x00"
#: 页面坐标上限（mil；排除 dbId/噪声）
_COORD_MAX = 20000

#: 符号体框尺寸回退表（P2-B gold 权威口径）。
#: gold DSN（md5 5762432b，10988 帧）的 GraphicInst 帧将 locY/locX/
#: y2/x2 字段清零（89% 帧 bbox 退化），仅保留 (x1,y1) 锚点。体框是
#: **符号库属性**（跨导出确定性），此表由同设计 workdir 全量 bbox
#: 实测众数导出，用于重建页面 bbox = (x1, y1, x1+w, y1+h)。
#: 键为外形基础名（如 Hi5662YV305_DDR_GE_0A / R / C）。
_SHAPE_BODY_SIZE_FALLBACK: dict[str, tuple[int, int]] = {
    "2P5GEPHY_0": (305, 295),
    "3A BUCK": (95, 61),
    "C": (53, 21),
    "CP": (41, 42),
    "DDR3_0": (145, 581),
    "DZ3": (24, 80),
    "DZ_L": (21, 75),
    "D_A": (45, 21),
    "GPON BOSA": (115, 61),
    "HOLE": (11, 21),
    "Hi5662YV305_DDR_GE_0A": (385, 601),
    "Hi5662YV305_DDR_GE_0B": (375, 251),
    "Hi5662YV305_DDR_GE_0C": (625, 411),
    "Hi5662YV305_DDR_GE_0D": (325, 331),
    "Hi5662YV305_DDR_GE_0E": (275, 301),
    "Hi5662YV305_DDR_GE_0F": (285, 911),
    "ID-BOARD": (21, 21),
    "L": (89, 15),
    "L4_L": (85, 61),
    "LASER DRIVER": (185, 185),
    "LB": (65, 41),
    "LB_0": (65, 41),
    "LC4_A_0": (85, 41),
    "LC_J_0": (89, 35),
    "LDO_1": (115, 61),
    "LED": (55, 44),
    "L_E": (71, 65),
    "MJ4-M2_C": (47, 85),
    "MJ8-M2": (75, 111),
    "MOSFET": (144, 135),
    "NPN-BEC_0": (64, 65),
    "PNP-BCEC_0": (65, 65),
    "PSW4_A": (65, 51),
    "PSW6M2": (65, 81),
    "PWC3_A": (65, 61),
    "R": (44, 21),
    "SIP1": (45, 21),
    "SIP3": (55, 51),
    "SIP5": (31, 61),
    "SLC NAND FLASH_1": (165, 91),
    "SLIC": (105, 311),
    "SOP8": (105, 51),
    "USB-4S4_0": (85, 91),
    "X_B": (85, 65),
}


def resolve_bbox_fallback(
    shape: str, coord: tuple[int, int], bbox: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    """按外形名回填退化体框（P2-B gold 口径）。

    gold DSN 中 89% 的 GraphicInst 帧 locY/locX/y2/x2 清零，导致
    bbox 退化为点 (x1,y1,x1,y1)。体框是符号库属性，用实测表重建：
    ``(x1, y1, x1+w, y1+h)``。

    Args:
        shape: 外形名（X.Normal）。
        coord: 实例坐标 (x1, y1)（锚点）。
        bbox: 当前体框（可能退化）。

    Returns:
        完整体框；无法回填返回原 bbox/None。
    """
    if bbox is not None:
        x1, y1, x2, y2 = bbox
        if x2 > x1 and y2 > y1:
            return bbox
    base = (shape or "").rsplit(".", 1)[0]
    size = _SHAPE_BODY_SIZE_FALLBACK.get(base)
    if size is None:
        return bbox
    if coord is None:
        return bbox
    return (coord[0], coord[1], coord[0] + size[0], coord[1] + size[1])


@dataclass
class ShapeFrame:
    """含外形名的帧。"""

    offset: int
    frame_type: int
    shape: str
    #: 实例坐标候选 (x, y) —— **bbox 角 (x1,y1)**，与 EXP/CSV Location 一致
    #: （实证 [len16][名][00][dbId:u32][locY][locX][y2][x2][x1][y1][color]）。
    coord: tuple[int, int] | None = None
    #: 渲染原点 (locX, locY)（符号文本平移用；P2-A 与 EXP 坐标分离保留）。
    render_origin: tuple[int, int] | None = None
    #: 体框 (x1, y1, x2, y2)（P2-B 引脚邻近判定用；字段缺失回落 (x1,y1,x1,y1)）。
    bbox: tuple[int, int, int, int] | None = None

    @property
    def base_name(self) -> str:
        """去掉 .Normal/.Convert 后缀的基础名。"""
        return self.shape.rsplit(".", 1)[0]


def _valid_coord(x: int, y: int) -> bool:
    """坐标合法性（mil 级页面坐标；排除 dbId/位号噪声）。"""
    return (
        x is not None and y is not None
        and 0 < x <= _COORD_MAX and 0 < y <= _COORD_MAX
    )


def _read_graphic_inst(rec: bytes, m: re.Match) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    """读取 GraphicInst 形态（形态 A）的坐标。

    外形名后布局（OpenOrCadParser 实证）：:

        [dbId:u32][locY:i16][locX:i16][y2:i16][x2:i16][x1:i16][y1:i16][color:u8]

    返回 ((x1,y1) 实例坐标, (locX,locY) 渲染原点, (x1,y1,x2,y2) bbox)；
    字段不足返回 None。
    """
    after = rec[m.end():m.end() + 18]
    if len(after) < 16:
        return None
    locY = int.from_bytes(after[4:6], "little", signed=True)
    locX = int.from_bytes(after[6:8], "little", signed=True)
    y2 = int.from_bytes(after[8:10], "little", signed=True)
    x2 = int.from_bytes(after[10:12], "little", signed=True)
    x1 = int.from_bytes(after[12:14], "little", signed=True)
    y1 = int.from_bytes(after[14:16], "little", signed=True)
    if not _valid_coord(x1, y1):
        return None
    render = (locX, locY) if _valid_coord(locX, locY) else None
    bbox = (x1, y1, x2, y2) if _valid_coord(x2, y2) else (x1, y1, x1, y1)
    return (x1, y1), render, bbox


def _short45_coord(rec: bytes) -> tuple[int, int] | None:
    """形态 C：45B 短引用帧坐标（``[x:u16][y:u16]`` at 偏移 10..14）。"""
    if len(rec) < 16:
        return None
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    if _valid_coord(x, y):
        return x, y
    return None


def _marker30_coord(rec: bytes) -> tuple[int, int] | None:
    """形态 B：marker30 标记帧坐标（best-effort）。

    在 ``30 00 00 00`` 标记附近（前 32B / 后 32B）扫描合法 u16 坐标对；
    全防御：任一帧形态未知均返回 None，不抛异常。
    """
    idx = rec.find(_MARKER30)
    if idx == -1:
        return None
    # 候选窗口：标记前 32B 到标记后 32B
    lo = max(0, idx - 32)
    hi = min(len(rec) - 3, idx + 32)
    for off in range(lo, hi):
        x = int.from_bytes(rec[off:off + 2], "little")
        y = int.from_bytes(rec[off + 2:off + 4], "little")
        if _valid_coord(x, y):
            return x, y
    return None


def _scan_u16_coords(rec: bytes) -> tuple[int, int] | None:
    """形态 D：全帧 u16 坐标对扫描（62B/83B 图形帧防御）。"""
    for off in range(10, max(10, len(rec) - 3)):
        x = int.from_bytes(rec[off:off + 2], "little")
        y = int.from_bytes(rec[off + 2:off + 4], "little")
        if _valid_coord(x, y):
            return x, y
    return None


def _shape_coord(rec: bytes) -> tuple[int, int] | None:
    """从外形名帧提取实例坐标候选 (x, y)。

    返回 **(x1, y1) bbox 角** —— 与 EXP/CSV Location 交叉验证 100%
    （TG0C0D2 737/737 within 1mil）。早期实现误读 locX/locY（渲染原点），
    与 EXP 偏差 4~31 mil（0% 命中）——P2-A 修正。

    Args:
        rec: 帧原始字节。

    Returns:
        (x, y)；无法定位返回 None。
    """
    m = _SHAPE_COORD_RE.search(rec)
    if not m:
        return None
    got = _read_graphic_inst(rec, m)
    if got is None:
        return None
    (x, y), _render, _bbox = got
    return x, y


def extract_render_origin(rec: bytes) -> tuple[int, int] | None:
    """提取外形名帧渲染原点 (locX, locY)（符号文本平移用，P2-A）。

    与 ``extract_shape_coord`` 分离：IR loc_x/loc_y 取 bbox 角 (x1,y1)
    （EXP 口径），而符号内部文本渲染需按放置原点 (locX,locY) 平移
    （QA P1-12 U7C SPI_interface=353+235,1+96 实证）。

    Args:
        rec: 帧原始字节。

    Returns:
        (locX, locY)；无法定位返回 None。
    """
    m = _SHAPE_COORD_RE.search(rec)
    if not m:
        return None
    got = _read_graphic_inst(rec, m)
    if got is None:
        return None
    _coord, render, _bbox = got
    return render


def extract_shape_bbox(rec: bytes) -> tuple[int, int, int, int] | None:
    """提取外形名帧体框 (x1, y1, x2, y2)（P2-B 引脚邻近判定）。

    Args:
        rec: 帧原始字节。

    Returns:
        (x1, y1, x2, y2)；无法定位返回 None。
    """
    m = _SHAPE_COORD_RE.search(rec)
    if not m:
        return None
    got = _read_graphic_inst(rec, m)
    if got is None:
        return None
    _coord, _render, bbox = got
    return bbox


def extract_shape_coord(rec: bytes) -> tuple[int, int] | None:
    """多形态坐标提取（P2-A 核心）。

    优先级（全部防御性，未知结构返回 None 不抛异常）：
    1. 形态 A：外形名 GraphicInst → (x1, y1)（EXP 口径，TG0C0D2 100%）；
    2. 形态 C：45B 短引用帧 → (x, y)；
    3. 形态 B：marker30 标记附近坐标；
    4. 形态 D：62B/83B 图形帧 u16 坐标扫描。

    Args:
        rec: 帧原始字节。

    Returns:
        (x, y)；无法定位返回 None。
    """
    m = _SHAPE_COORD_RE.search(rec)
    if m is not None:
        got = _read_graphic_inst(rec, m)
        if got is not None:
            return got[0]
    short = _short45_coord(rec)
    if short is not None:
        return short
    marker = _marker30_coord(rec)
    if marker is not None:
        return marker
    return _scan_u16_coords(rec)


def extract_shape(frame: Frame, max_len: int = 60) -> str | None:
    """从帧中提取外形名（X.Normal / X.Convert）。

    Args:
        frame: 输入帧。
        max_len: 外形名最大长度。

    Returns:
        外形名字符串；无则 None。
    """
    rec = frame.raw
    for m in re.finditer(rb"[\x20-\x7e]{2,}", rec):
        s = m.group().decode("latin1")
        if len(s) <= max_len and _SHAPE_RE.match(s):
            return s
    return None


def parse_shape_frame(frame: Frame, ctx: DecodeContext) -> ShapeFrame | None:
    """解析外形名帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（未使用）。

    Returns:
        ShapeFrame；无外形名返回 None。
    """
    shape = extract_shape(frame)
    if shape is None:
        return None
    rec = frame.raw
    coord = extract_shape_coord(rec)
    bbox = extract_shape_bbox(rec)
    return ShapeFrame(
        offset=frame.offset,
        frame_type=frame.frame_type,
        shape=shape,
        coord=coord,
        render_origin=extract_render_origin(rec),
        bbox=resolve_bbox_fallback(shape, coord, bbox),
    )


def scan_shape_frames(frames: list[Frame], ctx: DecodeContext) -> list[ShapeFrame]:
    """扫描一页全部外形名帧。"""
    result: list[ShapeFrame] = []
    for frame in frames:
        sf = parse_shape_frame(frame, ctx)
        if sf is not None:
            result.append(sf)
    return result
