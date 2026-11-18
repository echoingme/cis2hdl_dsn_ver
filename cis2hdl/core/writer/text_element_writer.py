"""文字/图形原样照搬（§9.1，用户裁决 ③）。

把页面级/符号级文字与图形元素翻译为 DEHDL CSA 指令（纯函数）：

- ``PageIR.text_elements`` → ``T`` 文本指令（GBK 中文已解码）；
- ``PageIR.graphic_elements`` → ``B`` 矩形 / ``L`` 线段指令；
- ``PageIR.symbol_texts``（已平移/旋转到页面坐标）→ ``T`` 指令。

坐标规则：DSN 坐标按 25 栅格吸附（DEHDL 栅格口径，文档 §9.1.2）。
高内聚：文字/图形指令映射唯一收敛；低耦合：只读 PageIR 字典字段，
不触碰 writer 内部实现，零文件系统。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: DEHDL 栅格（25 mil 吸附，文档 §9.1.2）。
GRID = 25


def _snap(v: float, grid: int = GRID) -> int:
    """25 栅格吸附（四舍五入到最近栅格点）。"""
    return int(round(v / grid) * grid)


def _safe_text(text: Optional[str]) -> str:
    return str(text or "").replace('"', "'")


def _map_pt(
    transform: Optional[dict],
    x: float,
    y: float,
) -> tuple[int, int]:
    """2026-08-21：应用 CIS→DEHDL 坐标变换（与 WIRE/DOT/元件同源）。

    此前 T/B/L 用 DSN 原始坐标（正数）直接输出，与映射后的元件/走线
    错位（元件页 T 25 25 在页面左下角而元件在 -4507 处）。
    transform 为 None 时原样返回（无坐标域时保持现状）。
    """
    if not transform:
        return _snap(x), _snap(y)
    scale: float = transform["scale"]
    dx: float = x - transform["cis_cx"]
    dy: float = y - transform["cis_cy"]
    dehdl_x: int = int(transform["page_cx"] + dx * scale)
    dehdl_y: int = int(transform["page_cy"] - dy * scale)
    return _snap(dehdl_x), _snap(dehdl_y)


def text_elements_to_csa(
    texts: list, page_id: str = "", transform: Optional[dict] = None
) -> list[str]:
    """页级文字 → ``T`` 指令。

    Args:
        texts: ``PageIR.text_elements``（dict 含 x/y/text/font/rotation）。
        page_id: 页 id（日志用）。
        transform: 2026-08-21 新增——CIS→DEHDL 坐标变换（None 不映射）。

    Returns:
        CSA ``T`` 指令行列表。
    """
    lines: list[str] = []
    for t in texts or []:
        txt = _safe_text(t.get("text"))
        x, y = _map_pt(transform, t.get("x", 0) or 0, t.get("y", 0) or 0)
        font = int(t.get("font", 0) or 0)
        rot = int(t.get("rotation", 0) or 0)
        if not txt:
            continue
        # T <x> <y> <rot> <font> "text"（CSA 页面级文本原语）。
        lines.append(f'T {x} {y} {rot} {font} "{txt}"')
    return lines


def graphic_elements_to_csa(
    graphics: list, transform: Optional[dict] = None
) -> list[str]:
    """页级图形 → ``B``/``L`` 指令。

    kind=box/rect → ``B`` 矩形；kind=line/wire/segment/polyline → ``L``
    线段；kind=text 已在 text_elements 覆盖（此处跳过）；未知 kind 忽略。

    Args:
        graphics: ``PageIR.graphic_elements``（dict 含 kind/x/y/x2/y2/text）。
        transform: 2026-08-21 新增——CIS→DEHDL 坐标变换（None 不映射）。

    Returns:
        CSA ``B``/``L`` 指令行列表。
    """
    lines: list[str] = []
    for g in graphics or []:
        kind = str(g.get("kind", "")).lower()
        x1, y1 = _map_pt(transform, g.get("x", 0) or 0, g.get("y", 0) or 0)
        gx2, gy2 = g.get("x2"), g.get("y2")
        x2, y2 = _map_pt(
            transform,
            gx2 if gx2 is not None else g.get("x", 0) or 0,
            gy2 if gy2 is not None else g.get("y", 0) or 0,
        )
        if kind in ("box", "rect", "rectangle", "symbol_bbox"):
            lines.append(f"B {x1} {y1} {x2} {y2}")
        elif kind in ("line", "wire", "segment", "polyline",
                      "graphic_shape", "arc"):
            # 2026-08-21：graphic_shape（polyline 波形/箭头符号 53 个）
            # 与 arc 兜底输出为线段（DEHDL 无圆弧原语时以 L 近似）。
            lines.append(f"L {x1} {y1} {x2} {y2}")
    return lines


def symbol_texts_to_csa(
    symbol_texts: list, transform: Optional[dict] = None
) -> list[str]:
    """符号级文字（已平移页面坐标）→ ``T`` 指令。

    Args:
        symbol_texts: ``PageIR.symbol_texts``（含 x/y/text/font/attr）。
        transform: 2026-08-21 新增——CIS→DEHDL 坐标变换（None 不映射）。

    Returns:
        CSA ``T`` 指令行列表（kind=pin 的引脚标签跳过——由 symbol.css
        的 C/X 承担，避免页面重复文本）。
    """
    lines: list[str] = []
    for t in symbol_texts or []:
        if t.get("kind") in ("pin", "body"):
            continue  # 引脚标签/体框文本由符号定义承担
        txt = _safe_text(t.get("text"))
        if not txt:
            continue
        x, y = _map_pt(transform, t.get("x", 0) or 0, t.get("y", 0) or 0)
        font = int(t.get("font", 0) or 0)
        lines.append(f'T {x} {y} 0 {font} "{txt}"')
    return lines


def page_elements_to_csa(page, transform: Optional[dict] = None) -> list[str]:
    """页面全部文字/图形 → CSA 指令（汇总入口）。

    Args:
        page: PageIR（text_elements/graphic_elements/symbol_texts）。
        transform: 2026-08-21 新增——CIS→DEHDL 坐标变换，T/B/L 与
            元件/走线同一坐标系（None 不映射）。

    Returns:
        指令行列表（T/B/L，已 25 栅格吸附）。
    """
    lines: list[str] = []
    lines.extend(text_elements_to_csa(
        getattr(page, "text_elements", []), getattr(page, "page_id", ""),
        transform))
    lines.extend(graphic_elements_to_csa(
        getattr(page, "graphic_elements", []), transform))
    lines.extend(symbol_texts_to_csa(
        getattr(page, "symbol_texts", []), transform))
    return lines


def page_element_stats(page) -> dict:
    """页面文字/图形统计（QA 用）。"""
    return {
        "text_elements": len(getattr(page, "text_elements", []) or []),
        "graphic_elements": len(getattr(page, "graphic_elements", []) or []),
        "symbol_texts": len(getattr(page, "symbol_texts", []) or []),
    }
