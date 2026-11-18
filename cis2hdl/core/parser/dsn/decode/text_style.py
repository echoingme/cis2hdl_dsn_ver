"""文本样式解码（P1-5/P1-6/P1-9）。

提供四组纯函数/常量：
- ``FONT_TO_PT``：正文/信息页 0x0 帧 font 号 → 字号（pt）。实证：
  font=6 → 页面标题、8 → 正文、12 → 封面大标题。映射表与 QA
  golden-files-inventory 对照（font 号 → 高度 mil，再按 1pt≈1.333mil
  换算，取整）。
- ``REFDES_FONT_TO_PT``：0x85 位号帧 font 字节（marker 后第 2 字节）→
  字号（pt）。**与正文 FONT_TO_PT 独立**（用户裁决 ④）。实证 R83
  font=24 → 80pt（审计误读 font=3→24 已修正）。
- ``COLOR_CODES``：attr 高位字节 → 颜色名（Color.hpp 0-48 枚举）。
- 辅助函数 ``decode_attr_color`` / ``decode_attr_justify`` /
  ``decode_font_to_pt`` / ``decode_refdes_font_to_pt``。

启发式 ``should_use_new_path(strlst, pages)``（P1-9）：
    替代旧 ``strlst==2170`` 硬门槛。新帧语义管道适用条件：
    - strLst 非空且包含 0x85 位号帧依赖的 'VALUE' 索引（strLst[133]）
      与 'GND'（strLst[1]）；
    - 页面数 >= 1（0x85 位号帧在页面结构区解码）。
    任一不满足 → 回退旧路径（RTL 变体）。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 正文 font 号 → 字号（pt）。实证 font 高度（mil）：6=90、8=120、
#: 12=180、16=240（Cache 0x2e2e 符号文本）。1pt ≈ 1.333mil → 取整。
#: 未收录的 font 号按 1pt≈1.333mil 线性换算兜底（见 decode_font_to_pt）。
FONT_TO_PT: dict[int, int] = {
    6: 68,
    8: 90,
    12: 135,
    16: 180,
}

#: 0x85 位号帧 font 字节 → 字号（pt）——独立映射（用户裁决 ④）。
#: 实证 R83 font=0x18=24 → 80pt（QA golden 文件口径）。
REFDES_FONT_TO_PT: dict[int, int] = {
    24: 80,
}

#: 属性位号 font 字节上限（防御：超过视为非法）
_REFDES_FONT_MAX = 64

#: 属性帧 attr 字段颜色枚举（Color.hpp 0-48）。attr 高位字节为颜色码，
#: 低位字节为对齐/样式（见 decode_attr_justify）。
COLOR_CODES: dict[int, str] = {
    0: "Default",
    1: "Red",
    2: "Green",
    3: "Blue",
    4: "Yellow",
    5: "Magenta",
    6: "Cyan",
    7: "White",
    8: "Gray",
    9: "DarkRed",
    10: "DarkGreen",
    11: "DarkBlue",
    12: "DarkYellow",
    13: "DarkMagenta",
    14: "DarkCyan",
    15: "DarkGray",
    16: "LightGray",
    17: "Orange",
    18: "Brown",
    19: "Purple",
    20: "Pink",
    21: "Gold",
    22: "Silver",
    23: "Maroon",
    24: "Navy",
    25: "Teal",
    26: "Olive",
    27: "Lime",
    28: "Aqua",
    29: "Fuchsia",
    30: "Khaki",
    31: "Coral",
    32: "Salmon",
    33: "Turquoise",
    34: "Plum",
    35: "Ivory",
    36: "Beige",
    37: "Wheat",
    38: "Violet",
    39: "Orchid",
    40: "Indigo",
    41: "Crimson",
    42: "Chocolate",
    43: "Coral",
    44: "Azure",
    45: "Lavender",
    46: "Mint",
    47: "Slate",
    48: "SkyBlue",
}


def decode_font_to_pt(font: int | None) -> int | None:
    """正文 font 号 → 字号（pt）。

    Args:
        font: 0x0/0x2e2e 文本帧的 font 号。

    Returns:
        字号（pt）；未知 font 号按 1pt≈1.333mil 线性换算，None 不变。
    """
    if font is None:
        return None
    if font in FONT_TO_PT:
        return FONT_TO_PT[font]
    # 兜底：高度 mil = 15 * font（实证 6→90、8→120、12→180、16→240）
    if 1 <= font <= 200:
        pt = round(15 * font / 1.333)
        return max(pt, 1)
    return None


def decode_refdes_font_to_pt(font: int | None) -> int | None:
    """0x85 位号帧 font 字节 → 字号（pt）。

    使用独立 ``REFDES_FONT_TO_PT`` 表（用户裁决 ④）；未知值按
    ``decode_font_to_pt`` 兜底（保持不抛异常）。

    Args:
        font: 0x85 marker 后第 2 字节（font 字节）。

    Returns:
        字号（pt）；None 不变。
    """
    if font is None:
        return None
    if font in REFDES_FONT_TO_PT:
        return REFDES_FONT_TO_PT[font]
    if 1 <= font <= _REFDES_FONT_MAX:
        return decode_font_to_pt(font)
    return None


def decode_attr_color(attr: int | None) -> str | None:
    """从 attr 打包字段解码颜色名（attr 高位字节）。

    OrCAD 属性 attr 字段：高位字节 = 颜色码（Color.hpp 0-48），
    低位字节 = 对齐/样式（1=left、2=center、4=right、8=top、
    16=bottom 等）。实证 'PCB Footprint' 文本 attr=0x08be →
    高位 0x08=8 → 'Gray'（用户裁决 ⑥ 允许以 Color.hpp 对照表解释）。

    Args:
        attr: 文本属性打包字段（u16）。

    Returns:
        颜色名；attr 为 None 返回 None。
    """
    if attr is None:
        return None
    hi = (attr >> 8) & 0xFF
    return COLOR_CODES.get(hi)


def decode_attr_justify(attr: int | None) -> str:
    """从 attr 打包字段解码对齐/样式描述（attr 低位字节）。

    Args:
        attr: 文本属性打包字段（u16）。

    Returns:
        可读的对齐/样式描述（空格分隔的标签；未知返回 'unknown'）。
    """
    if attr is None:
        return "unknown"
    lo = attr & 0xFF
    if lo == 0:
        return "default"
    tags: list[str] = []
    if lo & 0x01:
        tags.append("left")
    if lo & 0x02:
        tags.append("center")
    if lo & 0x04:
        tags.append("right")
    if lo & 0x08:
        tags.append("top")
    if lo & 0x10:
        tags.append("bottom")
    if lo & 0x20:
        tags.append("vcenter")
    if lo & 0x40:
        tags.append("rotate")
    if lo & 0x80:
        tags.append("mirror")
    return " ".join(tags) if tags else f"0x{lo:02x}"


def should_use_new_path(strlst: list[str], page_count: int) -> bool:
    """判断新帧语义管道是否适用（P1-9，替代 strLst==2170 硬门槛）。

    启发式规则：
    1. strLst 非空；
    2. 存在 0x85 位号帧依赖的 'VALUE' 字符串（strLst[133] 或任意
       下标含 'VALUE'）；
    3. 存在 'GND'（strLst[1] 或任意下标）；
    4. 页面数 >= 1。

    Args:
        strlst: Library 流解析出的字符串表。
        page_count: 发现的页面流数量。

    Returns:
        适用返回 True；否则 False。
    """
    if not strlst or page_count < 1:
        return False
    values = {s.upper() for s in strlst if s}
    if "VALUE" not in values:
        return False
    if "GND" not in values:
        return False
    return True
