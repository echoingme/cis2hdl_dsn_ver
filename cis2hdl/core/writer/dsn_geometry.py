"""符号几何 → symbol.css 指令（B2，纯函数）。

消费 ``ComponentDef.symbol_geometry``（R-GEO 结构化输出）：
- ``body_box: [x1, y1, x2, y2]`` → ``P "CDS_LMAN_SYM_OUTLINE"`` + 4 条
  ``M`` 边框线（指令格式对齐 ``mock_icon_lib._symbol_css``，Cadence 16.6
  已验证语法）；
- ``pin_lines: [x1, y1, x2, y2, ...]`` → ``L`` 引脚线 + ``C`` 短号 +
  ``X`` 长名（对齐 mock 引脚指令，side 由 x 方向推断）；
- ``texts``（kind=text/body/header_attr）→ ``T`` 文本指令。

设计原则：**纯函数、零文件系统**；指令行只依赖传入几何，不感知
实例/页面（实例平移由调用方负责，见 ``mock_rebuilder``）。符号内坐标
保持局部 mil（与 Cache 一致）。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: 指令常量（对齐 mock_icon_lib 已验证格式）。
_OUTLINE_PREFIX = 'P "CDS_LMAN_SYM_OUTLINE" "{outline}" 0 0 0.00 0.00 22 0 0 0 0 0 0 0 0'


def body_box_to_css(body_box: list) -> list[str]:
    """体框 → P outline + 4 条 M 边线（body-first, F1）。

    Args:
        body_box: ``[x1, y1, x2, y2]``（可含 5 元素容错）。

    Returns:
        symbol.css 指令行列表（空几何 → 空列表）。
    """
    if not body_box or len(body_box) < 4:
        return []
    x0, y0, x1, y1 = (float(v) for v in body_box[:4])
    if x0 > x1:
        x0, x1 = x1, x0
    if y0 > y1:
        y0, y1 = y1, y0
    outline = f"{int(x0)},{int(y1)},{int(x1)},{int(y0)}"
    return [
        _OUTLINE_PREFIX.format(outline=outline),
        f"M {int(x0)} {int(y1)} {int(x1)} {int(y1)} -1 0",
        f"M {int(x1)} {int(y1)} {int(x1)} {int(y0)} -1 0",
        f"M {int(x1)} {int(y0)} {int(x0)} {int(y0)} -1 0",
        f"M {int(x0)} {int(y0)} {int(x0)} {int(y1)} -1 0",
    ]


def pin_lines_to_css(
    pin_lines: list, labels: Optional[dict] = None, pin_line_len: int = 50
) -> list[str]:
    """引脚线 → L + C + X 指令（side 由 x 方向推断）。

    兼容两种几何形态（R-GEO 实测）：扁平 ``[x1,y1,x2,y2, ...]``（每 4
    元素一条）与嵌套 ``[[x1,y1,x2,y2,label], ...]``（每条 4-5 元素，
    第 5 元素为引脚号）。

    Args:
        pin_lines: 引脚线几何（扁平或嵌套）。
        labels: ``{pin_number: C 短号文本}``（缺省用索引/嵌套 label）。
        pin_line_len: L 段向外长度（对齐 temp_lib.pin_line_len 默认 50）。

    Returns:
        指令行列表。
    """
    lines: list[str] = []
    labels = labels or {}
    # 归一化：嵌套 → 段列表；扁平 → 按 4 元素切段。
    if pin_lines and isinstance(pin_lines[0], (list, tuple)):
        segs = [list(s) for s in pin_lines]
    else:
        flat = pin_lines or []
        segs = [flat[i:i + 4] for i in range(0, len(flat) - 3, 4)]
    for i, seg in enumerate(segs):
        if len(seg) < 4:
            continue
        px1, py1, px2, py2 = (int(v) for v in seg[:4])
        num = str(seg[4]) if len(seg) > 4 and seg[4] else str(i + 1)
        label = labels.get(num, num)
        # 2026-08-21 修复：零长度引脚线（px1==px2 且 py1==py2，DSN 源
        # 数据 L 元件实测 [0,10,0,10]）——原样输出 L 0 10 0 10（退化点）
        # → Cadence connectivity server 无法解析 → SPCOCD-553 整个
        # mock 库加载失败。自动补最小伸出长度 pin_line_len（默认 50，
        # 对齐 s16 参考包 -150→-400 外伸语义），C 引脚点放外端点。
        _len = int(pin_line_len or 50)
        _dx, _dy = px2 - px1, py2 - py1
        if _dx == 0 and _dy == 0:
            # 方向推断：默认向左（s16 惯例）；body_box 宽度>0 时向右
            side = "left"
            # 用 body_box 宽度决定方向：若 px1 接近 0 视为左侧
            if px1 > 0:
                side = "right"
            if side == "left":
                px2 = px1 - _len
            else:
                px2 = px1 + _len
        lines.append(f"L {px1} {py1} {px2} {py2} -1 0")  # 对齐 mock 基线 6 字段
        side = "left" if px2 < px1 else "right"
        # C/X 指令对齐 mock_icon_lib._append_pin_line 已验证格式
        # （R1 铁律：C 行末 justify ∈ {R,L}；X 类型固定 PIN_TEXT）。
        if side == "left":
            lines.append(
                f'C {px2} {py2} "{num}" {px2} {py2} 0 0 16 1 L'
            )
            lines.append(
                f'X "PIN_TEXT" "{label}" {px2 - 80} {py2} 0 1 16 0 0 0 0 0 1 0 0'
            )
        else:
            lines.append(
                f'C {px2} {py2} "{num}" {px2} {py2} 0 0 16 1 R'
            )
            lines.append(
                f'X "PIN_TEXT" "{label}" {px2 + 80} {py2} 0 0 16 0 0 0 0 0 1 0 0'
            )
    return lines


def texts_to_css(texts: list) -> list[str]:
    """符号文本（kind=text/body/header_attr）→ T 指令。

    Args:
        texts: ``symbol_texts`` 条目（dict 含 text/x/y/kind 等）。

    Returns:
        T 指令行列表（文本按行输出，坐标局部 mil）。
    """
    lines: list[str] = []
    for t in texts or []:
        txt = str(t.get("text", "") or "").replace('"', "'")
        if not txt:
            continue
        x = int(t.get("x", 0) or 0)
        y = int(t.get("y", 0) or 0)
        font = int(t.get("font", 0) or 0)
        lines.append(f"T 0 {x} {y} 0 0 {font} 0 0 0 0 4 0")
        lines.append(txt)
    return lines


def symbol_geometry_to_css(
    symbol_geometry: dict,
    pin_labels: Optional[dict] = None,
) -> list[str]:
    """完整几何 → symbol.css 指令（体框 + 引脚线 + 文本）。

    Args:
        symbol_geometry: ``ComponentDef.symbol_geometry``。
        pin_labels: ``{pin_number: 标签}``（C/X 文本，可选）。

    Returns:
        指令行列表（P/M/L/C/X/T）。
    """
    geo = symbol_geometry or {}
    lines: list[str] = []
    lines.extend(body_box_to_css(geo.get("body_box") or []))
    lines.extend(
        pin_lines_to_css(geo.get("pin_lines") or [], labels=pin_labels)
    )
    lines.extend(texts_to_css(geo.get("texts") or []))
    return lines


def geometry_stats(symbol_geometry: dict) -> dict:
    """几何完整性统计（QA 用）。"""
    geo = symbol_geometry or {}
    return {
        "has_body_box": bool(geo.get("body_box")),
        "body_box": geo.get("body_box"),
        "pin_line_count": len((geo.get("pin_lines") or [])) // 4,
        "text_count": len(geo.get("texts") or []),
        "geometry_source": geo.get("geometry_source", ""),
    }
