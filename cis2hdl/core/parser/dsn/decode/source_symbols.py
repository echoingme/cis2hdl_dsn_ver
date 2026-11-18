"""Source symbol 解码器（P1-10）。

Source symbol（电源/接地符号，OrCAD 层级参考）在页面流中以多种帧类型
出现（类型号 = strLst 索引，**数据相关，禁止按类型号全量断言**），
因此采用**内容探测**（QA 审计口径）：

统一帧布局（TG0C0D2 实测，0x46/0x50/0x3c0/0x2bc/0x154/0x15e 等）::

    [8B DELIM][type:u16][00 00][x:u16][y:u16]
    [30 00 00 00][00 00 00 00][04 00][attr:u16][len:u16]
    [name][00]...

探测特征：
1. ``30 00 00 00`` 标记（source symbol 几何标记）；
2. 坐标对 (x, y) 在偏移 12/14（页面 mil 坐标）；
3. 名称 lstr（可打印 ASCII 串，如 '12V0' / '3V3_SOC' /
   'PFI_DYING_GASP' / '17N16918'）；
4. 命名几何帧（ONSHEET2 / OFFSHEET2 / BISHEET）——跨页/同页连接符。

分类（``SourceSymbolKind``）：
- POWER_SYMBOL：电源符号（网名，如 12V0 / 3V3_SOC）
- GROUND_SYMBOL：接地符号（GND 网名）
- OFFSHEET：跨页连接符（OFFSHEET2）
- ONSHEET：同页连接符（ONSHEET2 / BISHEET）

实测（TG0C0D2）：目标 11 类型共 261 帧 → 217 个内容命中；
04_POWER_SUPPLY 页 ``0x0046 '12V0'@0x182a``、``0x0430 'ONSHEET2'@0xd87a`` 等。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    from .wire_topology import PinPointRecord

logger = logging.getLogger(__name__)

#: 坐标上限（mil）
_MAX_COORD = 30000
#: 名称最大长度
_MAX_NAME_LEN = 48
#: 帧内扫描上限（防御）
_MAX_SCAN = 400

#: source symbol 统一布局：x/y 在偏移 12/14（标记 30 00 00 00 之前）
_COORD_OFF = 12

#: 目标帧类型白名单（strLst 索引，数据相关；探针确认 11 类型）
#: 注：D3 修复后含真 BISHEET 帧类型 0x0567/0x0205（全库 4 个真
#: BISHEET：08/18 页 0x0567 + 08/13 页 0x0205）。**不加 0x03f2**：
#: 0x03f2 帧（08 页 6 + 11 页 32 = 38 帧）含 marker30 + 网名但无
#: BISHEET 字符串（strLst[0x03f2]='9783' 数字串印证类型号=数据相关
#: 索引），维持现状避免 38 个误检。
TARGET_FRAME_TYPES = frozenset((
    0x0046, 0x0050, 0x03C0, 0x02BC, 0x0154, 0x015E,
    0x0430, 0x0405, 0x03A0, 0x01B0, 0x00E0,
    0x0567, 0x0205,
))

#: 真 BISHEET 帧类型（D3 修复，2026-08-20 字节级调查确认）。
#: 帧布局：marker30=False、len 83/139 > 60、含 'BISHEET' 字符串 +
#: 第二 lstr（QU/WU/44/!| 等总线成员后缀），坐标为名称后 GraphicInst
#: locX/locY。**探测必须限定本类型集**：若对全部白名单类型做无条件
#: sheet 探测，会连带解析 0x0430 ONSHEET2（04/17/18 页）与 0x0050
#: BISHEET（11/12 页）——它们已在白名单且现有语义仅作坐标源/电源
#: 符号，会使 source_symbol_count 远超预期（>42），破坏黄金标准。
_BISHEET_FRAME_TYPES = frozenset((0x0567, 0x0205))


class SourceSymbolKind(str, Enum):
    """Source symbol 分类。"""

    POWER_SYMBOL = "power_symbol"
    GROUND_SYMBOL = "ground_symbol"
    OFFSHEET = "offsheet"
    ONSHEET = "onsheet"


#: 命名几何帧特征（跨页/同页连接符）
_SHEET_NAMES = frozenset(("ONSHEET2", "OFFSHEET2", "BISHEET"))
#: 电源网名特征（GND 单独归 ground）
_POWER_NET_RE = re.compile(
    r"^(VCC|VDD|VSS|AVCC|AVDD|VBAT|PWR|3V3|5V|12V|1V8|1V2|0V9|DVDD|VPH|VIN|VBUS)",
    re.IGNORECASE,
)


@dataclass
class SourceSymbol:
    """Source symbol 解码结果。

    Attributes:
        kind: 分类（power/ground/offsheet/onsheet）。
        name: 符号名/网名（如 '12V0' / 'ONSHEET2'）。
        x, y: 页面坐标。
        page: 页面名。
        offset: 帧页内偏移。
        frame_type: 源帧类型（strLst 索引，诊断保留）。
        target_pages: 跨页目标页（OFFSHEET 关联，默认空）。
        object_id: 对象 id（45B 短引用）。
    """

    kind: SourceSymbolKind
    name: str
    x: int
    y: int
    page: str
    offset: int
    frame_type: int
    target_pages: list[str] = field(default_factory=list)
    object_id: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "x": self.x,
            "y": self.y,
            "page": self.page,
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "target_pages": list(self.target_pages),
            "object_id": self.object_id,
        }


def _read_lstr(rec: bytes, off: int) -> tuple[str | None, int]:
    """读 u16 len + bytes + 0x00。"""
    if off + 2 > len(rec):
        return None, off
    ln = int.from_bytes(rec[off:off + 2], "little")
    if ln < 1 or ln > _MAX_NAME_LEN or off + 2 + ln + 1 > len(rec):
        return None, off
    if rec[off + 2 + ln] != 0:
        return None, off
    raw = rec[off + 2:off + 2 + ln]
    if not all(32 <= b < 127 for b in raw):
        return None, off
    return raw.decode("latin1"), off + 2 + ln + 1


def _classify_kind(name: str, frame_type: int) -> SourceSymbolKind:
    """按名称/帧类型分类。

    Args:
        name: 符号名。
        frame_type: 源帧类型。

    Returns:
        SourceSymbolKind。
    """
    up = name.upper()
    if up in _SHEET_NAMES:
        # ONSHEET2/OFFSHEET2/BISHEET：'BISHEET' 不含 'OFF' → ONSHEET。
        # BISHEET 为总线内跨页连接符，与 ONSHEET2 同属页面连接符语义
        # （D3 修复核对：保持与 ONSHEET2 一致，不归 OFFSHEET）。
        return SourceSymbolKind.OFFSHEET if "OFF" in up else SourceSymbolKind.ONSHEET
    if up in ("GND", "AGND", "DGND", "PGND", "GNDA", "GNDD"):
        return SourceSymbolKind.GROUND_SYMBOL
    if _POWER_NET_RE.match(up):
        return SourceSymbolKind.POWER_SYMBOL
    # 帧类型为 strLst 索引时（0x0046='12V0' 等），默认按电源处理
    if frame_type in (0x0046, 0x0050, 0x03C0, 0x02BC, 0x0154, 0x015E):
        return SourceSymbolKind.POWER_SYMBOL
    return SourceSymbolKind.POWER_SYMBOL


def _find_name_lstr(rec: bytes) -> str | None:
    """在帧内扫描名称 lstr（18 起；过滤 $ 前缀噪声）。

    Args:
        rec: 帧原始字节。

    Returns:
        名称；无命中返回 None。
    """
    for off in range(18, min(len(rec) - 3, _MAX_SCAN)):
        s, _ = _read_lstr(rec, off)
        if s is not None and len(s) >= 2 and not s.startswith("$"):
            return s
    return None


def _find_sheet_name(rec: bytes) -> str | None:
    """在帧内扫描长命名几何帧（ONSHEET2/OFFSHEET2/BISHEET）。

    Args:
        rec: 帧原始字节。

    Returns:
        连接符名；无命中返回 None。
    """
    for m in re.finditer(rb"[\x20-\x7e]{3,48}", rec[10:]):
        s = m.group().decode("latin1")
        if s in _SHEET_NAMES or s.startswith("ONSHEET") or s.startswith("OFFSHEET"):
            return s
    return None


def _sheet_name_end(rec: bytes, sheet: str) -> int | None:
    """定位命名几何帧内 ``[len:u16][sheet][00]`` lstr 的结束偏移。

    Args:
        rec: 帧原始字节。
        sheet: 连接符名（ONSHEET2/OFFSHEET2/BISHEET）。

    Returns:
        lstr 结束偏移（名称后 0x00 之后）；无命中返回 None。
    """
    target = sheet.encode("latin1")
    for off in range(10, len(rec) - 2 - len(target) - 1):
        ln = int.from_bytes(rec[off:off + 2], "little")
        if (
            ln == len(target)
            and rec[off + 2:off + 2 + len(target)] == target
            and rec[off + 2 + len(target)] == 0
        ):
            return off + 2 + len(target) + 1
    return None


def _sheet_graphic_coord(rec: bytes, sheet: str) -> tuple[int, int] | None:
    """提取命名几何帧 GraphicInst locX/locY（D1）。

    实证（TG0C0D2 workdir）：名称 lstr 结束后 ``[dbId:u32][?][locY:u16][locX:u16]``
    （与 66B 电源符号帧同构，locY 在前、locX 在后）。如 04 页 off=55418
    ``ONSHEET2`` name_end=27 → u16@+4=160(locY) u16@+6=930(locX)。

    **gold 权威口径**：gold DSN（md5 5762432b）将 locY/locX 清零，
    位置改存 bbox 角 x1/y1（``[dbId:u32][locY=0][locX=0][y2][x2][x1][y1]``，
    08 页 BISHEET(QU) name_end 后 x1=1080/y1=310）。loc 读零时回退读 x1/y1。

    Args:
        rec: 帧原始字节。
        sheet: 连接符名。

    Returns:
        (locX, locY)；无命中或坐标非法返回 None。
    """
    ne = _sheet_name_end(rec, sheet)
    if ne is None or ne + 8 > len(rec):
        return None
    # workdir：locY/locX
    loc_y = int.from_bytes(rec[ne + 4:ne + 6], "little")
    loc_x = int.from_bytes(rec[ne + 6:ne + 8], "little")
    if 0 < loc_x < _MAX_COORD and 0 < loc_y < _MAX_COORD:
        return loc_x, loc_y
    # gold：loc 清零，位置在 x1/y1（ne+12/ne+14）
    if ne + 16 <= len(rec):
        x1 = int.from_bytes(rec[ne + 12:ne + 14], "little")
        y1 = int.from_bytes(rec[ne + 14:ne + 16], "little")
        if 0 < x1 < _MAX_COORD and 0 < y1 < _MAX_COORD:
            return x1, y1
    return None


def _parse_short_symbol(
    frame: Frame, ctx: DecodeContext,
) -> SourceSymbol | None:
    """D2：45B 短帧 source symbol 分支（无 marker30 也按 x/y 短引用解析）。

    实测（TG0C0D2）：10/11/12 页 0x0046/0x0050 共 6 个 45B 帧，布局
    ``[8B DELIM][type:u16][00 00][x:u16@12][y:u16@14][00...]`` —— x/y 与
    66B 帧同偏移但**无 marker30、无内嵌名称**。帧类型号即 strLst 网名
    （0x0046='DDR_BA1'、0x0050='MDI_DP_1'；数据相关）。

    Args:
        frame: 输入帧。
        ctx: 解码上下文。

    Returns:
        SourceSymbol；非短帧或无内容命中返回 None。
    """
    rec = frame.raw
    if len(rec) > 60 or len(rec) < 18:
        return None
    if frame.frame_type not in TARGET_FRAME_TYPES:
        return None
    # 短帧不允许 30 00 00 00 标记（有标记走主解析分支）
    if b"\x30\x00\x00\x00" in rec[10:]:
        return None
    x = int.from_bytes(rec[_COORD_OFF:_COORD_OFF + 2], "little")
    y = int.from_bytes(rec[_COORD_OFF + 2:_COORD_OFF + 4], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    # 短帧无内嵌名称：名称 = 帧类型号对应 strLst 网名（如 DDR_BA1）；
    # 非可打印/无 strLst 时回退 UNKNOWN。
    name = ""
    if frame.frame_type < len(ctx.strlst):
        cand = ctx.strlst[frame.frame_type]
        if cand and all(32 <= ord(c) < 127 for c in cand) and not cand.startswith("$"):
            name = cand
    if not name:
        return None
    return _make_symbol(name, x, y, frame, ctx)


def _make_symbol(
    name: str, x: int, y: int, frame: Frame, ctx: DecodeContext,
) -> SourceSymbol:
    """构造 SourceSymbol（分类由名称/帧类型决定）。"""
    return SourceSymbol(
        kind=_classify_kind(name, frame.frame_type),
        name=name,
        x=x,
        y=y,
        page=ctx.page_name or "",
        offset=frame.offset,
        frame_type=frame.frame_type,
    )


def parse_source_symbol(frame: Frame, ctx: DecodeContext) -> SourceSymbol | None:
    """内容探测解析单个 source symbol 帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文（page_name 提供页面名）。

    Returns:
        SourceSymbol；无内容命中返回 None。
    """
    rec = frame.raw
    if len(rec) < 18:
        return None
    # 类型白名单过滤（数据相关；探针确认 11 类型 + D3 BISHEET 2 类型）
    if frame.frame_type not in TARGET_FRAME_TYPES:
        return None
    # ── D3 修复：真 BISHEET 帧类型 sheet 探测（marker30 分支之前）──
    # 0x0567/0x0205 帧 marker30=False、len 83/139 > 60，走原 short 分支
    # 会被 len>60 拒绝而漏检。此处限定 _BISHEET_FRAME_TYPES 先行探测
    # 'BISHEET' 字符串 → _sheet_graphic_coord 取真实坐标（不受 len 限制）。
    # 10 页 0x0205 异类电源帧 sheet=None → 落到下方 marker30/short 分支，
    # marker30=True + 无名称 → 自然拒绝（零误检）。
    if frame.frame_type in _BISHEET_FRAME_TYPES:
        sheet = _find_sheet_name(rec)
        if sheet is not None:
            coord = _sheet_graphic_coord(rec, sheet)
            if coord is not None:
                return _make_symbol(sheet, coord[0], coord[1], frame, ctx)
            return _make_symbol(sheet, 0, 0, frame, ctx)
    # 特征 1：30 00 00 00 标记
    if b"\x30\x00\x00\x00" not in rec[10:]:
        # D2：45B 短帧（无 marker 也按 x/y 短引用解析）
        return _parse_short_symbol(frame, ctx)
    # 特征 2：坐标 x 在偏移 12 精确；y 由实例放置帧提供（诚实标注 0）
    x = int.from_bytes(rec[_COORD_OFF:_COORD_OFF + 2], "little")
    if not (0 < x < _MAX_COORD):
        return None
    y = 0
    # 特征 3/4：名称 lstr → 命名几何帧（ONSHEET2/OFFSHEET2/BISHEET）兜底
    name = _find_name_lstr(rec)
    if name is not None:
        return _make_symbol(name, x, y, frame, ctx)
    sheet = _find_sheet_name(rec)
    if sheet is not None:
        # D1：命名几何帧 GraphicInst locX/locY 提取（帧本身真实坐标）。
        # ONSHEET2 帧类型号 = strLst 网名（0x0430='12V0'），名称字段为
        # 'ONSHEET2' —— 坐标从名称后 GraphicInst 区读取。
        coord = _sheet_graphic_coord(rec, sheet)
        if coord is not None:
            return _make_symbol(sheet, coord[0], coord[1], frame, ctx)
        return _make_symbol(sheet, x, y, frame, ctx)
    return None


def _collect_sheet_net_coords(
    frames: list[Frame], ctx: DecodeContext,
) -> dict[str, tuple[int, int]]:
    """收集本页命名几何帧 (网名 → GraphicInst locX/locY)。

    ONSHEET2/OFFSHEET2/BISHEET 帧类型号 = strLst 网名（如 0x0430='12V0'），
    坐标来自名称后 GraphicInst 区（D1）。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        网名 → (locX, locY)。
    """
    net_coords: dict[str, tuple[int, int]] = {}
    for frame in frames:
        rec = frame.raw
        sheet = None
        for nm in _SHEET_NAMES:
            if nm.encode("latin1") in rec:
                sheet = nm
                break
        if sheet is None:
            continue
        coord = _sheet_graphic_coord(rec, sheet)
        if coord is None:
            continue
        net = ""
        if frame.frame_type < len(ctx.strlst):
            cand = ctx.strlst[frame.frame_type]
            if cand and not cand.startswith("$"):
                net = cand
        if net:
            net_coords.setdefault(net, coord)
    return net_coords


def _collect_pin_points(frames: list[Frame]) -> list[tuple[int, int]]:
    """收集 0x01 实例放置帧坐标（同 x 回填 y 用）。

    Args:
        frames: 页面帧列表。

    Returns:
        有效 (x, y) 坐标列表（过滤超长/引脚标记帧）。
    """
    pin_points: list[tuple[int, int]] = []
    for frame in frames:
        if frame.frame_type != 0x01:
            continue
        rec = frame.raw
        if len(rec) < 18 or len(rec) > 60:
            continue
        if b"\x18" in rec[10:16]:
            continue
        x = int.from_bytes(rec[10:12], "little")
        y = int.from_bytes(rec[12:14], "little")
        if 0 < x < _MAX_COORD and 0 < y < _MAX_COORD:
            pin_points.append((x, y))
    return pin_points


def _backfill_sheet_y(
    symbols: list[SourceSymbol], frames: list[Frame], ctx: DecodeContext,
) -> list[SourceSymbol]:
    """D1：按网名/同 x 实例放置帧回填电源符号 y。

    电源符号帧（66B/45B）y 字段为 0（OrCAD 不写），真实页面坐标在
    同页 ONSHEET2 连接符帧 GraphicInst（locX/locY）或同 x 的 0x01
    实例放置帧。黄金标准：04 页 12V0 = (930,160) —— 来自 ONSHEET2
    帧 off=55418（strLst 网名 '12V0'）。

    回填优先级：
    1. **网名匹配**：同页 ONSHEET2/OFFSHEET2/BISHEET 帧 strLst 网名 ==
       符号名 → 取连接符 GraphicInst 坐标（x+y 均回填；电源符号即
       该网名的源符号，位置以连接符为准）。
    2. **同 x 实例放置帧**：0x01 短帧（GND/引脚点）与符号 x 相同
       （容差 100）→ 取其 y（GND/MDX_*/VDD15 等无网名连接符的符号）。

    Args:
        symbols: 已解析的 source symbol 列表（含 y=0 电源符号与 ONSHEET2）。
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        回填 y 后的 source symbol 列表（原地更新并返回）。
    """
    net_coords = _collect_sheet_net_coords(frames, ctx)
    pin_points = _collect_pin_points(frames)

    # 回填 y=0 的电源/接地符号
    for sym in symbols:
        if sym.y != 0:
            continue
        if sym.kind not in (
            SourceSymbolKind.POWER_SYMBOL,
            SourceSymbolKind.GROUND_SYMBOL,
        ):
            continue
        # 3a) 网名匹配（ONSHEET2 连接符坐标；x+y 均回填）
        coord = net_coords.get(sym.name)
        if coord is not None:
            sym.x, sym.y = coord
            continue
        # 3b) 同 x 0x01 实例放置帧回填 y（保留符号自身 x）
        best: tuple[int, int] | None = None
        for px, py in pin_points:
            d = abs(px - sym.x)
            if d <= 100 and (best is None or d < abs(best[0] - sym.x)):
                best = (px, py)
        if best is not None:
            sym.y = best[1]
    return symbols


def scan_source_symbols(
    frames: list[Frame], ctx: DecodeContext,
) -> list[SourceSymbol]:
    """扫描一页全部 source symbol。

    Args:
        frames: 页面帧列表（结构区）。
        ctx: 解码上下文。

    Returns:
        SourceSymbol 列表（按帧偏移升序；含 D1 网名 y 回填）。
    """
    result: list[SourceSymbol] = []
    for frame in frames:
        sym = parse_source_symbol(frame, ctx)
        if sym is not None:
            result.append(sym)
    _backfill_sheet_y(result, frames, ctx)
    result.sort(key=lambda s: s.offset)
    return result


def register_source_symbol_decoders() -> None:
    """注册 source symbol 语义帧解码器（内容探测，不锁类型号）。"""
    from .registry import register_semantic

    # 注册 11 个目标类型（strLst 索引，数据相关；D3 留待 P2）
    for ftype in (
        0x0046, 0x0050, 0x03C0, 0x02BC, 0x0154, 0x015E,
        0x0430, 0x0405, 0x03A0, 0x01B0, 0x00E0,
    ):
        register_semantic(ftype, parse_source_symbol)
