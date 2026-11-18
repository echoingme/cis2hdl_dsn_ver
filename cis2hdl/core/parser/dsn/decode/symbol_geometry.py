"""Cache 符号文本/几何解码器（P1-12）。

仅读 DSN **Cache 流**解析符号几何/文本（OLB 仅反推参考，运行时不解 OLB）：
- ``0x2929``：符号体框（symbol body box）。
- ``0x2c2c``：引脚线（pin line）。
- ``0x2e2e``：符号文本帧（长帧 ≥60B 为符号文本，短帧 <60B 为符号头
  内嵌属性 DESIGNED/ECA NO/DATE/NA/VER/SHEET）。

0x2e2e 长帧坐标块在 **+18..53**（与页级 0x0000 的 +30..53 不同）::

    [8B DELIM][2e 2e][00 00][00 00]
    [x:u32][y:u32][x2:u32][y2:u32]   ← +18..33
    [x:u32][y:u32]                   ← +34..41 锚点重复
    [font:u16][attr:u16][len:u16]    ← +42..47
    [text][00]                       ← +48..

0x001c 符号头帧：6 个（0A~0F 六段 U7）。内嵌子块起点用
``[2e 2e 3c]`` 标记定位（名称/路径长度可变，不能用固定偏移）；
坐标在标记后 +9。

输出 ``SymbolTextElement``：实例平移/旋转后输出芯片图标全文本。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.types import DELIMITER

if TYPE_CHECKING:
    from .info_frames import InfoElement

logger = logging.getLogger(__name__)

_DELIM_RE = re.compile(re.escape(DELIMITER))

#: 0x2e2e 符号文本帧坐标块起始（Cache 专用，与页级不同）
_COORD_OFF = 18
#: 长帧阈值（≥60B 为符号文本；<60B 为符号头内嵌属性）
_TEXT_MIN_LEN = 60
#: 坐标上限
_MAX_COORD = 30000
#: 文本最大长度
_MAX_TEXT_LEN = 80
#: 符号头内嵌子块标记（2e 2e + 变体 3b/3c/3d/3e/3f，实测全范围）
_SUBBLOCK_MARKS = tuple(
    b"\x2e\x2e" + bytes([b]) for b in range(0x3B, 0x40)
)


@dataclass
class SymbolTextElement:
    """Cache 符号文本元素。

    Attributes:
        symbol: 所属符号名（如 'Hi5662YV305_DDR_GE_0F'，尽力关联）。
        text: 文本内容。
        x, y: 符号内坐标（相对原点）。
        x2, y2: 包围盒右上。
        font: 字体号。
        attr: 属性打包字段（颜色/样式）。
        offset: 帧页内偏移。
        kind: 'text' | 'body' | 'pin' | 'header_attr'。
    """

    symbol: str
    text: str
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    font: int = 0
    attr: int = 0
    offset: int = 0
    kind: str = "text"

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "text": self.text,
            "x": self.x,
            "y": self.y,
            "x2": self.x2,
            "y2": self.y2,
            "font": self.font,
            "attr": self.attr,
            "offset": self.offset,
            "kind": self.kind,
        }


def _decode_text(raw: bytes) -> str | None:
    """解码文本字节（latin1；含高位字节回退 GBK）。"""
    if not raw or len(raw) > _MAX_TEXT_LEN:
        return None
    if all(32 <= b < 127 for b in raw):
        return raw.decode("latin1")
    try:
        return raw.decode("gbk")
    except UnicodeDecodeError:
        return None


def parse_symbol_text_frame(
    rec: bytes, offset: int, symbol: str = "",
) -> SymbolTextElement | None:
    """解析 0x2e2e 长帧为符号文本元素。

    Args:
        rec: 帧原始字节。
        offset: 帧页内偏移。
        symbol: 所属符号名（默认空）。

    Returns:
        SymbolTextElement；非 0x2e2e 长帧或解析失败返回 None。
    """
    if len(rec) < _TEXT_MIN_LEN:
        return None
    ftype = int.from_bytes(rec[8:10], "little")
    if ftype != 0x2E2E:
        return None
    if len(rec) < _COORD_OFF + 24 + 6:
        return None
    x = int.from_bytes(rec[_COORD_OFF:_COORD_OFF + 4], "little")
    y = int.from_bytes(rec[_COORD_OFF + 4:_COORD_OFF + 8], "little")
    x2 = int.from_bytes(rec[_COORD_OFF + 8:_COORD_OFF + 12], "little")
    y2 = int.from_bytes(rec[_COORD_OFF + 12:_COORD_OFF + 16], "little")
    font = int.from_bytes(rec[42:44], "little")
    attr = int.from_bytes(rec[44:46], "little")
    ln = int.from_bytes(rec[46:48], "little")
    if not (1 <= ln <= _MAX_TEXT_LEN):
        return None
    if 48 + ln > len(rec):
        return None
    text = _decode_text(rec[48:48 + ln])
    if text is None:
        return None
    return SymbolTextElement(
        symbol=symbol,
        text=text,
        x=x if x < _MAX_COORD else 0,
        y=y if y < _MAX_COORD else 0,
        x2=x2 if x2 < _MAX_COORD else 0,
        y2=y2 if y2 < _MAX_COORD else 0,
        font=font,
        attr=attr,
        offset=offset,
        kind="text",
    )


def _find_subblock_mark(rec: bytes) -> int:
    """定位符号头内嵌子块标记（[2e 2e 3b..3f]，实测全范围）。

    Args:
        rec: 帧原始字节。

    Returns:
        标记偏移；无命中返回 -1。
    """
    for candidate in _SUBBLOCK_MARKS:
        m = rec.find(candidate, 10)
        if m != -1:
            return m
    return -1


def _build_symbol_text(
    symbol: str, text: str, offset: int, kind: str,
    x: int, y: int, x2: int, y2: int, font: int, attr: int,
) -> SymbolTextElement:
    """构造 SymbolTextElement（坐标越界置 0，诚实标注未知）。"""
    return SymbolTextElement(
        symbol=symbol,
        text=text,
        x=x if x < _MAX_COORD else 0,
        y=y if y < _MAX_COORD else 0,
        x2=x2 if x2 < _MAX_COORD else 0,
        y2=y2 if y2 < _MAX_COORD else 0,
        font=font,
        attr=attr,
        offset=offset,
        kind=kind,
    )


def parse_symbol_header_attr(
    rec: bytes, offset: int, symbol: str = "",
) -> SymbolTextElement | None:
    """解析 0x001c 符号头内嵌子块为属性文本元素。

    子块起点用 ``[2e 2e 3c]`` 标记定位；坐标在标记后 +10。

    Args:
        rec: 帧原始字节。
        offset: 帧页内偏移。
        symbol: 所属符号名（默认空）。

    Returns:
        SymbolTextElement；无子块或解析失败返回 None。
    """
    if len(rec) < 30:
        return None
    ftype = int.from_bytes(rec[8:10], "little")
    if ftype != 0x001C:
        return None
    mark = _find_subblock_mark(rec)
    if mark == -1:
        return None
    # 坐标在标记后 +10（实测 0xcfad：marker@73，坐标@83）
    x_off = mark + 10
    if x_off + 24 > len(rec):
        return None
    x = int.from_bytes(rec[x_off:x_off + 4], "little")
    y = int.from_bytes(rec[x_off + 4:x_off + 8], "little")
    x2 = int.from_bytes(rec[x_off + 8:x_off + 12], "little")
    y2 = int.from_bytes(rec[x_off + 12:x_off + 16], "little")
    font = int.from_bytes(rec[x_off + 24:x_off + 26], "little")
    attr = int.from_bytes(rec[x_off + 26:x_off + 28], "little")
    ln = int.from_bytes(rec[x_off + 28:x_off + 30], "little")
    if not (1 <= ln <= _MAX_TEXT_LEN):
        return None
    if x_off + 30 + ln > len(rec):
        return None
    text = _decode_text(rec[x_off + 30:x_off + 30 + ln])
    if text is None:
        return None
    return _build_symbol_text(
        symbol, text, offset, "header_attr",
        x, y, x2, y2, font, attr,
    )


def parse_symbol_body_frame(
    rec: bytes, offset: int, symbol: str = "",
) -> SymbolTextElement | None:
    """解析 0x2929 符号体框/线段帧为几何元素（P2-全元件符号文本）。

    帧布局（Cache 实证）：``[8B DELIM][29 29][20 00 00 00 00 00 00 00]
    [x:u32@10][y:u32@14][x2:u32@18][y2:u32@22]``（符号局部 mil）。

    Args:
        rec: 帧原始字节。
        offset: 帧页内偏移。
        symbol: 所属符号名（默认空）。

    Returns:
        SymbolTextElement(kind='body', text='')；非 0x2929 帧或坐标
        非法返回 None。
    """
    ftype = int.from_bytes(rec[8:10], "little")
    if ftype != 0x2929:
        return None
    if len(rec) < 30:
        return None
    x = int.from_bytes(rec[10:14], "little")
    y = int.from_bytes(rec[14:18], "little")
    x2 = int.from_bytes(rec[18:22], "little")
    y2 = int.from_bytes(rec[22:26], "little")
    # 符号局部坐标可能为 0（线段锚点），只需 4 坐标都在合理范围
    if max(x, y, x2, y2) >= _MAX_COORD:
        return None
    return SymbolTextElement(
        symbol=symbol, text="", x=x, y=y, x2=x2, y2=y2,
        font=0, attr=0, offset=offset, kind="body",
    )


def _scan_u16_coords(rec: bytes, start: int) -> list[tuple[int, int]]:
    """扫描 0x2c2c 引脚线帧内 u16 坐标对（best-effort）。

    Args:
        rec: 帧原始字节。
        start: 扫描起点。

    Returns:
        合法坐标对列表。
    """
    result: list[tuple[int, int]] = []
    for off in range(start, max(start, len(rec) - 3)):
        x = int.from_bytes(rec[off:off + 2], "little")
        y = int.from_bytes(rec[off + 2:off + 4], "little")
        if 0 < x < _MAX_COORD and 0 < y < _MAX_COORD:
            result.append((x, y))
    return result


def parse_symbol_pin_line_frame(
    rec: bytes, offset: int, symbol: str = "",
) -> SymbolTextElement | None:
    """解析 0x2c2c 引脚线/引脚引出帧为几何元素（P2-全元件符号文本）。

    帧布局（Cache 实证）：``[8B DELIM][2c 2c][2a 00 ...][04 00]`` 后为
    u16 坐标对（引脚线端点，符号局部 mil）。

    Args:
        rec: 帧原始字节。
        offset: 帧页内偏移。
        symbol: 所属符号名（默认空）。

    Returns:
        SymbolTextElement(kind='pin', text='')；非 0x2c2c 帧返回 None。
    """
    ftype = int.from_bytes(rec[8:10], "little")
    if ftype != 0x2C2C:
        return None
    if len(rec) < 30:
        return None
    # 帧布局：``[DELIM][2c 2c][2a 00][0x00 填充][01 00 00 00 00 00 00 00]
    # [04 00][4×u16 点]``（4 点 = 2 条引脚线，2 点/线，符号局部 mil）。
    # 精确定位 ``04 00`` 标记（2a 00 后 40B 内），避免盲目 u16 扫描把
    # 帧外字节当作坐标（旧 _scan_u16_coords 产生 (1,1)-(29976,29976) 脏数据）。
    seg = rec[12:52]
    idx = seg.find(b"\x04\x00")
    if idx < 0:
        return None
    pts_off = 12 + idx + 2
    if pts_off + 8 > len(rec):
        return None
    pts: list[tuple[int, int]] = []
    for k in range(4):
        x = int.from_bytes(rec[pts_off + k * 4:pts_off + k * 4 + 2], "little")
        y = int.from_bytes(rec[pts_off + k * 4 + 2:pts_off + k * 4 + 4], "little")
        if x >= 2000 or y >= 2000:
            return None
        pts.append((x, y))
    if pts[0] == pts[1] and pts[2] == pts[3]:
        return None
    # 输出 2 条线为 2 个 pin 元素（与内嵌块一致，几何更精细）
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return SymbolTextElement(
        symbol=symbol, text="",
        x=min(xs), y=min(ys), x2=max(xs), y2=max(ys),
        font=0, attr=0, offset=offset, kind="pin",
    )


def _scan_part_regions(data: bytes) -> list[tuple[int, int, str]]:
    """扫描 Cache 流 LibraryPart 区域边界。

    每个区域 = ``[part_start, next_part_start)``；name 为 part 名（去 .Normal）。
    区域头部是 ``[DELIM][len16][name.Normal][00][len16][srcLib][00]...``。

    Args:
        data: Cache 流原始字节。

    Returns:
        [(start, end, part_name)]，按 start 升序。
    """
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


def _owner_part(regions: list[tuple[int, int, str]], offset: int) -> str:
    """返回包含 offset 的 part 名；区域外（Cache 头部）返回空串。

    Args:
        regions: [(start, end, name)]。
        offset: 帧偏移。

    Returns:
        part 名；无归属返回 ""。
    """
    for start, end, name in regions:
        if start <= offset < end:
            return name
    return ""


def _scan_inline_pinline_blocks(
    data: bytes, regions: list[tuple[int, int, str]],
) -> list[SymbolTextElement]:
    """扫描 Cache 流**无 DELIM 内嵌** 0x2c2c 引脚线子块。

    背景（T04 逆向实证）：多数符号的 0x2c2c 引脚线是带 8B DELIM 的独立
    帧（``parse_symbol_pin_line_frame`` 已覆盖）；但 3A BUCK/LDO_1/
    PSW6M2/SOP8/LC_J/PNP/DDR3 等符号的引脚线表以**内嵌子块**形态存在
    （位于源库路径 ``LIBRARY1.OLB\\x00`` 之后），模式::

        [OLB 路径][00 30 00 00 00][count:u16][2c 2c][2a 00][0x00 填充]
        [01 00 00 00 00 00 00 00][04 00][4×u16 点]（每条引脚线 2 点）

    与带 DELIM 帧同构：``[04 00][点1][点2][点3][点4]`` = 4 条引脚线
    （2 点/线，符号局部 mil）。仅用 u16 扫描坐标对（best-effort，
    无名称即按几何元素输出，kind='pin'）。

    Args:
        data: Cache 流原始字节。
        regions: _scan_part_regions 产出的 [(start, end, name)]。

    Returns:
        SymbolTextElement(kind='pin') 列表。
    """
    result: list[SymbolTextElement] = []
    for m in re.finditer(rb"OLB\x00\x30\x00\x00\x00", data):
        base = m.end()  # count:u16 之前
        if base + 2 > len(data):
            continue
        count = int.from_bytes(data[base:base + 2], "little")
        if not (1 <= count <= 64):
            continue
        mark = base + 2
        if data[mark:mark + 2] != b"\x2c\x2c":
            continue
        # 找到后续 04 00 引脚线数量标记（向前最多 40B）
        scan_off = mark + 2
        seg = data[scan_off:scan_off + 40]
        idx = seg.find(b"\x04\x00")
        if idx < 0:
            continue
        pts_off = scan_off + idx + 2
        # 4 个 u16 点 = 2 条引脚线（每条线 2 点，与带 DELIM 0x2c2c 帧同构）
        if pts_off + 8 > len(data):
            continue
        pts: list[tuple[int, int]] = []
        ok = True
        for k in range(4):
            x = int.from_bytes(data[pts_off + k * 4:pts_off + k * 4 + 2], "little")
            y = int.from_bytes(data[pts_off + k * 4 + 2:pts_off + k * 4 + 4], "little")
            # 符号局部坐标校验：小 IC 符号尺寸通常 < 2000 mil；
            # 超大值（如 29976）说明 04 00 标记误匹配（PSW6M2/DDR3 观察）
            if x >= 2000 or y >= 2000:
                ok = False
                break
            pts.append((x, y))
        if not ok or len(pts) < 4:
            continue
        # 排除退化线（两点相同/全零）
        if pts[0] == pts[1] and pts[2] == pts[3]:
            continue
        owner = _owner_part(regions, m.start())
        # 2 条线：线0=(pts[0],pts[1])，线1=(pts[2],pts[3])
        for li in (0, 2):
            p0, p1 = pts[li], pts[li + 1]
            result.append(SymbolTextElement(
                symbol=owner, text="",
                x=p0[0], y=p0[1], x2=p1[0], y2=p1[1],
                font=0, attr=0, offset=m.start(), kind="pin",
            ))
    if result:
        logger.info("Cache inline 0x2c2c pinline blocks parsed: %d", len(result))
    return result


def scan_symbol_texts(data: bytes) -> list[SymbolTextElement]:
    """扫描 Cache 流符号文本/头属性，按所属 LibraryPart 区域关联。

    归属（P1-12 修正）：文本帧只关联到**包含它的 LibraryPart 区域**
    （``[part_start, next_part_start)``）。Cache 头部（第一个 part 之前）
    的 24 个页签/标题栏文本（DESIGNED/ECA NO/DATE/VER/SHEET/PART_NUMBER/
    BOM 说明等）**不归属任何符号**（symbol=""），不挂到组件上。

    Args:
        data: Cache 流原始字节。

    Returns:
        SymbolTextElement 列表（symbol 字段 = 所属 part 名，可能为空）。
    """
    regions = _scan_part_regions(data)
    result: list[SymbolTextElement] = []
    for m in _DELIM_RE.finditer(data):
        off = m.start()
        owner = _owner_part(regions, off)
        el = parse_symbol_text_frame(data[off:], off, owner)
        if el is not None:
            result.append(el)
            continue
        el = parse_symbol_header_attr(data[off:], off, owner)
        if el is not None:
            result.append(el)
            continue
        el = parse_symbol_body_frame(data[off:], off, owner)
        if el is not None:
            result.append(el)
            continue
        el = parse_symbol_pin_line_frame(data[off:], off, owner)
        if el is not None:
            result.append(el)
    # T04：无 DELIM 内嵌 0x2c2c 引脚线子块（3A BUCK/LDO_1/PSW6M2 等小 IC）
    result.extend(_scan_inline_pinline_blocks(data, regions))
    logger.info("Cache symbol texts parsed: %d (attributed to %d parts)",
                len(result), len({t.symbol for t in result if t.symbol}))
    return result
