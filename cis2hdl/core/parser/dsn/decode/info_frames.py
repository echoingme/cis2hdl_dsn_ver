"""信息页 0x0 帧解码器（架构设计 §2 L3 扩展 + 信息页专项）。

信息页（01_Cover / 02_Block Diagram / 03_Power Tree）几乎全部由 ``type=0x0``
帧组成（Cover 180/182、Block 330/332、Power 387/389）。本解码器把 0x0 帧
解码为统一图形原语：text / line / rect / arrow。

0x0 帧统一结构（TG0C0D2 字节级实证）::

    [8B DELIM][type 00 00][00 00][00 00][00 00]   ← 偏移 0..13
    [30 00][00 00][01 00][kind:4B][00 x8]         ← 偏移 14..29
    [x:u32][y:u32][x2:u32][y2:u32][x:u32][y:u32]  ← 偏移 30..53（坐标块）
    [font:u16][attr:u16][len:u16][text][00]        ← 偏移 54..（文本元素）
    [00 x8]                                        ← 偏移 54..（矩形/线段）

- 坐标块前 4 个 u32 为 (x,y,x2,y2)；后 2 个 u32 为锚点重复 (x,y)。
- 文本元素（kind 区含 2e 2e xx，即 '..x'）：
  - font：6=页面标题 / 8=正文 / 12=封面大标题
  - attr：颜色/样式打包字段（0x0000 / 0x197d / 0x08be 等），暂透传
  - len：文本字节数（不含结尾 00）
  - text：latin1 或 GBK（中文标签如 '40MHz晶体'、'1588接口'）
- 矩形/线段（kind 区含 29 29 20，即 ')) '）：坐标块后为 8 个 00。

坐标验证依据：Cover 页 'Sheet N' 文本锚点与 OCR 视觉位置一致；Power 树
'12V/1A'→'5V0' 等父子层级坐标符合思维导图布局。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..frame.splitter import Frame
from .registry import DecodeContext

#: 文本元素最小长度
_MIN_TEXT_LEN = 2
#: 文本元素最大长度
_MAX_TEXT_LEN = 60
#: 坐标上限（mil 页面坐标）
_MAX_COORD = 30000
#: 头部起点（跳过分隔符 + 类型区）
_HEAD_START = 10
#: 字体上限（实证字体 6~14）
_MAX_FONT = 50
#: 坐标块起始偏移
_COORD_OFF = 30
#: 坐标块长度（6 个 u32）
_COORD_LEN = 24


@dataclass
class InfoElement:
    """信息页图形原语。

    Attributes:
        kind: text | line | rect | arrow。
        page: 页面名。
        offset: 帧偏移。
        x, y: 起点坐标（文本为锚点，矩形/线为起点）。
        x2, y2: 终点坐标（文本为包围盒右上，矩形/线为终点）。
        text: 文本内容（kind=text 时有效）。
        font: 字体号（kind=text 时有效）。
        attr: 文本属性打包字段（颜色/样式，kind=text 时有效）。
        rotation: 旋转（暂恒 0，未在 0x0 帧中发现独立旋转字段）。
    """

    kind: str
    page: str
    offset: int
    x: int | None = None
    y: int | None = None
    x2: int | None = None
    y2: int | None = None
    text: str | None = None
    font: int | None = None
    attr: int = 0
    rotation: int = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "page": self.page,
            "offset": self.offset,
            "x": self.x,
            "y": self.y,
            "x2": self.x2,
            "y2": self.y2,
            "text": self.text,
            "font": self.font,
            "attr": self.attr,
            "rotation": self.rotation,
        }


def _u32(rec: bytes, off: int) -> int | None:
    if off + 4 > len(rec):
        return None
    return int.from_bytes(rec[off:off + 4], "little")


def _u16(rec: bytes, off: int) -> int | None:
    if off + 2 > len(rec):
        return None
    return int.from_bytes(rec[off:off + 2], "little")


def _coord_block(rec: bytes) -> tuple[int, int, int, int] | None:
    """从坐标块（偏移 30..53）提取 (x, y, x2, y2)。

    布局：``[x][y][x2][y2][x][y]``（锚点 x/y 重复两次）。

    Returns:
        (x, y, x2, y2)；坐标非法返回 None。
    """
    if len(rec) < _COORD_OFF + _COORD_LEN:
        return None
    x = _u32(rec, _COORD_OFF)
    y = _u32(rec, _COORD_OFF + 4)
    x2 = _u32(rec, _COORD_OFF + 8)
    y2 = _u32(rec, _COORD_OFF + 12)
    x_dup = _u32(rec, _COORD_OFF + 16)
    y_dup = _u32(rec, _COORD_OFF + 20)
    if (
        x is None or y is None or x2 is None or y2 is None
        or x_dup is None or y_dup is None
    ):
        return None
    # 锚点重复校验（QA P0-2 修复：图形帧 dup=0 合法，不再强制 x==x_dup）
    if x != x_dup or y != y_dup:
        if not (x_dup == 0 and y_dup == 0):
            return None
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    return x, y, x2, y2


#: P0-2 公共别名（page_text_frames 复用，逻辑不变）
coord_block = _coord_block


def _decode_text(raw: bytes) -> str | None:
    """解码文本字节：ASCII/latin1 优先，含高位字节时回退 GBK。

    Returns:
        解码文本；长度过短或纯十六进制噪声返回 None。
    """
    if len(raw) < _MIN_TEXT_LEN or len(raw) > _MAX_TEXT_LEN:
        return None
    if all(b in b"0123456789abcdefABCDEF" for b in raw):
        return None
    if all(not chr(b).isalnum() for b in raw):
        return None
    # 全 ASCII → latin1
    if all(32 <= b < 127 for b in raw):
        text = raw.decode("latin1")
        if text.count(" ") > len(text) * 0.5:
            return None
        return text
    # 含高位字节 → GBK（中文标签）
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 32 for c in text):
        return None
    return text


#: P0-2 公共别名（page_text_frames 复用，逻辑不变）
decode_text = _decode_text


def _parse_text_element(
    rec: bytes, page: str, offset: int,
) -> InfoElement | None:
    """按固定布局解析文本元素（坐标块 + 偏移 54 起 [font][attr][len][text]）。"""
    coords = _coord_block(rec)
    if coords is None:
        return None
    x, y, x2, y2 = coords
    tail_off = _COORD_OFF + _COORD_LEN  # = 54
    if len(rec) < tail_off + 6:
        return None
    font = _u16(rec, tail_off)
    attr = _u16(rec, tail_off + 2)
    ln = _u16(rec, tail_off + 4)
    if font is None or ln is None:
        return None
    if font >= _MAX_FONT:
        return None
    if not (1 <= ln <= _MAX_TEXT_LEN):
        return None
    if tail_off + 6 + ln > len(rec):
        return None
    raw = rec[tail_off + 6:tail_off + 6 + ln]
    if rec[tail_off + 6 + ln] != 0:
        return None
    text = _decode_text(raw)
    if text is None:
        return None
    return InfoElement(
        kind="text", page=page, offset=offset,
        x=x, y=y, x2=x2, y2=y2, text=text, font=font, attr=attr,
    )


#: P0-2 公共别名（page_text_frames 复用，逻辑不变）
parse_text_element = _parse_text_element


def _parse_rect_element(
    rec: bytes, page: str, offset: int,
) -> InfoElement | None:
    """按固定布局解析矩形/线段（坐标块；54B 图形帧无尾部，62B 帧尾 8B）。

    QA P0-2 修复：54B 图形帧坐标块（6×u32）正好填满帧尾，不要求 tail 8B。

    **P1 修复（P0-2 补丁）**：62B 帧按 kind 区（偏移 20..22）分派：
    - ``29 29``（')) '）：纯矩形/线段，尾部 8B 须全 0（P0 逻辑不变）；
    - ``2e 2e``（'..'）：文本类图形原语。OrCAD 用 ``2e 2e`` 前缀同时表示
      文本元素与矩形图形（页 08 共 48 个 ``2e2e`` 帧 = QA 审计的 rect
      口径）。本函数仅在**文本解析失败**后被调用（``parse_info_frame``
      先试文本、再试矩形），因此 ``2e2e`` 帧走到这里时即为矩形图形，
      尾部样式字节（颜色/线宽）透传，**不校验**（如 @0xc5e8 尾
      ``04 00 00 00 01 00 30 00``、@0xf00c 尾 ``01 00 4e 19 01 00 38 00``）；
    - ``28 28``（'(('）：带样式的小矩形图形（页 08 @0xc5e8 同型），放行；
    - 其它 kind：拒绝（防噪声误判）。
    修复后页 08 rect 从 2 提升到 48（QA 审计口径）。
    """
    coords = _coord_block(rec)
    if coords is None:
        return None
    x, y, x2, y2 = coords
    tail_off = _COORD_OFF + _COORD_LEN
    if len(rec) > tail_off:
        if len(rec) < tail_off + 8:
            return None
        kind_head = rec[20:22]
        if kind_head == b"\x29\x29":
            # 纯矩形/线段：尾部须全 0
            if any(rec[tail_off:tail_off + 8]):
                return None
        elif kind_head not in (b"\x2e\x2e", b"\x28\x28"):
            # 其它 kind（如 0000/5a00 行分隔噪声）→ 拒绝
            return None
    if x2 is None or y2 is None:
        return None
    if not (0 < x2 < _MAX_COORD and 0 < y2 < _MAX_COORD):
        return None
    if x2 > x and y2 > y:
        kind = "rect"
    else:
        kind = "line"
    return InfoElement(kind=kind, page=page, offset=offset, x=x, y=y, x2=x2, y2=y2)


def _parse_line_element(
    rec: bytes, page: str, offset: int,
) -> InfoElement | None:
    """best-effort 解析线/箭头（72B 帧，含 ``30 00 3d`` 标记）。

    Returns:
        坐标合理的线/箭头元素；否则 None。
    """
    if len(rec) < 70:
        return None
    idx = rec.find(b"\x30\x00\x3d", _HEAD_START)
    if idx == -1:
        return None
    x = _u16(rec, idx + 3)
    y = _u16(rec, idx + 5)
    x2 = _u16(rec, idx + 7)
    y2 = _u16(rec, idx + 9)
    if (
        x is None or y is None or x2 is None or y2 is None
    ):
        return None
    if not (
        0 < x < _MAX_COORD and 0 < y < _MAX_COORD
        and 0 < x2 < _MAX_COORD and 0 < y2 < _MAX_COORD
        and (x != x2 or y != y2)
    ):
        return None
    # 帧内含 int16 增量（如 fd ff f6 ff）→ 箭头
    has_delta = b"\xfd\xff" in rec[:44] or b"\xf6\xff" in rec[:44]
    kind = "arrow" if has_delta else "line"
    return InfoElement(kind=kind, page=page, offset=offset, x=x, y=y, x2=x2, y2=y2)


def parse_info_frame(frame: Frame, ctx: DecodeContext) -> list[InfoElement]:
    """解码单个 0x0 信息帧为图形原语列表。

    Args:
        frame: 输入帧（frame_type == 0）。
        ctx: 解码上下文（page_name 提供页面名）。

    Returns:
        InfoElement 列表（通常一个帧一个元素）。
    """
    if frame.frame_type != 0:
        return []
    page = ctx.page_name or ""
    rec = frame.raw
    # 1) 文本元素（最可靠，固定布局）
    el = _parse_text_element(rec, page, frame.offset)
    if el is not None:
        return [el]
    # 2) 矩形/线段（固定布局）
    el = _parse_rect_element(rec, page, frame.offset)
    if el is not None:
        return [el]
    # 3) 线/箭头（best-effort）
    el = _parse_line_element(rec, page, frame.offset)
    if el is not None:
        return [el]
    return []


def scan_info_frames(frames: list[Frame], ctx: DecodeContext) -> list[InfoElement]:
    """扫描一页全部 0x0 信息帧。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        InfoElement 列表（按帧偏移升序）。
    """
    result: list[InfoElement] = []
    for frame in frames:
        if frame.frame_type != 0:
            continue
        result.extend(parse_info_frame(frame, ctx))
    return result


def decode_info_page(page_name: str, data: bytes) -> dict:
    """解码整页信息流（供导出 JSON 使用）。

    Args:
        page_name: 页面名。
        data: 页面流原始字节。

    Returns:
        dict: {page, elements, texts, summary}。
    """
    from ..frame.splitter import split_frames

    ctx = DecodeContext(strlst=[], page_name=page_name)
    frames = split_frames(data)
    elements = scan_info_frames(frames, ctx)
    texts = [e.text for e in elements if e.kind == "text" and e.text]
    return {
        "page": page_name,
        "frame_count": len(frames),
        "elements": [e.to_dict() for e in elements],
        "texts": texts,
        "summary": {
            "total": len(elements),
            "text": sum(1 for e in elements if e.kind == "text"),
            "line": sum(1 for e in elements if e.kind == "line"),
            "rect": sum(1 for e in elements if e.kind == "rect"),
            "arrow": sum(1 for e in elements if e.kind == "arrow"),
        },
    }
