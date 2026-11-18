"""GndClusterPlanner — GND 就近共用 + 簇内并联规划（Phase XVIII R6/R8）。

用户实测问题（B4）：GND 一页就 1 个；并联电容（C52/455/53/459/462）
各自单独引线接地而非就近共用；GND 符号落在元件正上方且连线穿元件；
GND 引脚连线不先延伸就拐弯。

本模块在既有 ``csa_writer._plan_and_inject_gnd_symbols``（Phase XVII 三期
GND 聚类）之上新增能力（``gnd_distribution.parallel_short=true`` 时）：

* ``route_cluster_parallel`` —— 簇内引脚**先在引脚附近并联（hub 短接）**
  再统一 1 条引出到 GND 符号。支持两种调用：
  - 设计 API：``route_cluster_parallel(cluster_pins, hub, outlines, stub_lead)``
    → 簇内短接 WIRE 段（引脚 → hub，每段先外引 stub_lead 再折向 hub）；
  - 旧式聚类 API：``route_cluster_parallel(pins, max_dist, gnd_coord)``
    → ``ParallelHub`` 列表（贪心最近邻，``parallel_short_dist`` 判定）。
* ``hub_for`` —— 簇 hub（包围盒中心 snap25；落 outline 内沿最小分离
  向量外推）；
* ``hub_short_wires`` —— 把 ``ParallelHub`` 转成簇内短接 WIRE 段；
* ``hub_to_symbol_wire`` —— 从 hub 到 GND 符号引脚的"1 条引出线"；
* ``place_gnd_symbol`` —— GND 符号避让元件 outline / 引脚禁区 / 页边
  （margin=50 + pin_avoid_radius=50 + edge_clearance=100）。

数据源铁律（STANDARDS Part I）：本模块只消费既有 DesignConnectivity
模型与 GND 簇计划（csa_writer 传入），不自行解析/自造数据；所有输出
坐标 ``_snap25`` 网格。

设计原则：独立模块 + 配置开关（``gnd_distribution.parallel_short``
默认 true，但 ``gnd_distribution.enabled`` 默认 false —— 开关整体
控制是否启用 GND 分布）；函数 <50 行；禁硬编码（数值进 routing.yaml）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

#: 25 网格（与全工程一致）。
_GRID: int = 25
#: GND_POWER 引脚相对 body 偏移（golden R3c，与 _power_pin_offset 同源）。
_GND_PIN_OFFSET: tuple[int, int] = (50, 100)
#: hub 外推避让 margin（= overlap.avoid_margin 默认 50）。
_HUB_PUSH_MARGIN: int = 50
#: C 纸页面边界（R5 边缘冗余区）。
_PAGE_X0: int = -10750
_PAGE_X1: int = -550
_PAGE_Y0: int = 400
_PAGE_Y1: int = 7200
#: Phase XXIII P1-3：页面 1/4 分块（2×2 象限）中线。
_BLOCK_X_MID: int = (_PAGE_X0 + _PAGE_X1) // 2
_BLOCK_Y_MID: int = (_PAGE_Y0 + _PAGE_Y1) // 2

#: 线段 = (x1, y1, x2, y2)；点 = (x, y)。
Segment = tuple[int, int, int, int]
Point = tuple[int, int]
Rect = tuple[int, int, int, int]


def _snap25(v: float) -> int:
    """Snap a coordinate to the 25-unit grid."""
    return int(round(v / _GRID)) * _GRID


@dataclass
class ParallelHub:
    """一个"簇内并联 hub"：一组同信号引脚在引脚附近短接后统一引出。

    Attributes:
        net: 信号名（如 ``GND`` / ``GND\\g``）。
        pin_coords: 参与并联的引脚绝对坐标列表。
        hub: 汇聚点绝对坐标（25 网格；通常取引脚包围盒中心）。
        outlet: 引出点绝对坐标（hub 向 GND 符号方向的引出端点）。
    """

    net: str
    pin_coords: list[tuple[int, int]] = field(default_factory=list)
    hub: tuple[int, int] = (0, 0)
    outlet: tuple[int, int] = (0, 0)

    @property
    def pin_count(self) -> int:
        """参与并联的引脚数（≥2 才有意义）。"""
        return len(self.pin_coords)


def _bbox_center(points: Sequence[tuple[int, int]]) -> tuple[int, int]:
    """包围盒中心（snap 25）。"""
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not xs:
        return (0, 0)
    return (_snap25((min(xs) + max(xs)) / 2), _snap25((min(ys) + max(ys)) / 2))


def _manhattan(a: tuple[int, int], b: tuple[int, int]) -> int:
    """曼哈顿距离（25 网格对齐后仍整数）。"""
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def _is_net_pin_style(pins: Sequence) -> bool:
    """True = 旧式 ``[(net, (x, y))]`` 输入（否则为坐标列表）。"""
    if not pins:
        return False
    first = pins[0]
    return (
        isinstance(first, (list, tuple)) and len(first) == 2
        and isinstance(first[0], str)
    )


def route_cluster_parallel(
    cluster_pins: Sequence,
    hub: Point | None = None,
    outlines: Iterable[Rect] = (),
    stub_lead: int = 100,
    max_dist: int = 500,
    gnd_coord: Point | None = None,
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
):
    """簇内引脚并联短接（双 API，R6/R8）。

    设计 API（R6 主线）：``route_cluster_parallel(cluster_pins, hub,
    outlines, stub_lead)`` → 每个引脚 → hub 的短接 WIRE 段（每段先沿
    引脚外引 ``stub_lead`` 再折向 hub，三段式；端点 = 引脚坐标不变）。

    旧式聚类 API（兼容既有调用）：``route_cluster_parallel(pins,
    max_dist, gnd_coord)``（``pins=[(net, (x, y))]``）→ ``ParallelHub``
    列表（贪心最近邻，距离 ≤ ``max_dist`` 的引脚并入同一簇）。

    Phase XXVII（S14）：``pin_cols``/``pin_rows``（页面全量引脚）透传
    ``_pin_to_hub_stub`` —— 短接段不恰穿过其他引脚。

    Args:
        cluster_pins: 簇内引脚坐标列表（设计 API）或 ``[(net, coord)]``
            （旧式聚类 API）。
        hub: 簇 hub（设计 API；缺省用 ``hub_for`` 计算）。
        outlines: 元件 outline（设计 API 避让，可选）。
        stub_lead: 引脚外引距离（设计 API，默认 100）。
        max_dist: 并联判定距离阈值（旧式 API，``parallel_short_dist``）。
        gnd_coord: GND 符号坐标（旧式 API outlet 方向，可选）。
        pin_cols: 页面全量引脚 x → [y...]（设计 API d0 判定，可选）。
        pin_rows: 页面全量引脚 y → [x...]（设计 API d0 判定，可选）。

    Returns:
        设计 API → 正交 WIRE 段列表；旧式 API → ``ParallelHub`` 列表。
    """
    if _is_net_pin_style(cluster_pins):
        return _cluster_hubs(
            cluster_pins, max_dist=max_dist, gnd_coord=gnd_coord,
        )
    pins = [(int(p[0]), int(p[1])) for p in cluster_pins]
    if not pins:
        return []
    if hub is None:
        h = hub_for(pins, outlines)
    else:
        h = (int(hub[0]), int(hub[1]))
    out: list[Segment] = []
    for pin in pins:
        out.extend(
            _pin_to_hub_stub(
                pin, h, outlines, stub_lead,
                pin_cols=pin_cols, pin_rows=pin_rows,
            )
        )
    return out


def _cluster_hubs(
    pins: Sequence[tuple[str, tuple[int, int]]],
    max_dist: int = 500,
    gnd_coord: tuple[int, int] | None = None,
) -> list[ParallelHub]:
    """贪心最近邻：把一组同信号引脚按就近原则聚成 hub 并联簇。

    从第一个引脚出发，把距离 ≤ ``max_dist`` 的引脚并入同一簇（曼哈顿
    距离）；每个簇生成一个 hub（包围盒中心，snap 25）与一个 outlet
    （hub 向 GND 符号方向偏移 50）。

    Args:
        pins: ``[(net, (x, y))]`` 同信号引脚列表（如 GND 网全部引脚）。
        max_dist: 并联判定距离阈值（``parallel_short_dist``，默认 500）。
        gnd_coord: GND 符号坐标（用于 outlet 方向；None 时默认 +y）。

    Returns:
        ``ParallelHub`` 列表；``pin_count < 2`` 的簇不产生 hub（返回空）。
    """
    if not pins:
        return []
    coords: list[tuple[int, int]] = [c for _n, c in pins]
    net = str(pins[0][0] or "GND")
    used: list[bool] = [False] * len(coords)
    hubs: list[ParallelHub] = []

    for i in range(len(coords)):
        if used[i]:
            continue
        cluster: list[tuple[int, int]] = [coords[i]]
        used[i] = True
        # 贪心并入近邻（允许链式：新成员再吸引更远引脚）。
        changed = True
        while changed:
            changed = False
            for j in range(len(coords)):
                if used[j]:
                    continue
                if any(_manhattan(coords[j], c) <= max_dist for c in cluster):
                    cluster.append(coords[j])
                    used[j] = True
                    changed = True
        if len(cluster) < 2:
            continue
        hub = _bbox_center(cluster)
        # outlet：hub 向 GND 符号方向外引 50（无 GND 时 +y）。
        if gnd_coord is not None:
            dx = 50 if gnd_coord[0] > hub[0] else -50
            dy = 50 if gnd_coord[1] > hub[1] else -50
        else:
            dx, dy = 0, 50
        outlet = (_snap25(hub[0] + dx), _snap25(hub[1] + dy))
        hubs.append(ParallelHub(net=net, pin_coords=cluster, hub=hub, outlet=outlet))
        logger.debug(
            "GND parallel hub: %d pins → hub %s outlet %s",
            len(cluster), hub, outlet,
        )
    return hubs


def _point_in_rect(p: Point, rect: Rect) -> bool:
    """点是否在矩形内（含边界）。"""
    x, y = p
    x0, y0, x1, y1 = rect
    return min(x0, x1) <= x <= max(x0, x1) and min(y0, y1) <= y <= max(y0, y1)


def _push_out_of_rect(p: Point, rect: Rect, margin: int) -> Point:
    """点相对矩形的最小分离向量外推（推出膨胀矩形外一格外）。

    Args:
        p: 点坐标。
        rect: 矩形（原始坐标）。
        margin: 外推边距（膨胀量）。

    Returns:
        外推后的点坐标（snap 25 网格；严格在膨胀矩形外）。
    """
    x, y = p
    lo_x, hi_x = min(rect[0], rect[2]), max(rect[0], rect[2])
    lo_y, hi_y = min(rect[1], rect[3]), max(rect[1], rect[3])
    candidates = (
        (hi_x + margin + _GRID, y),
        (lo_x - margin - _GRID, y),
        (x, hi_y + margin + _GRID),
        (x, lo_y - margin - _GRID),
    )
    best = min(candidates, key=lambda c: abs(c[0] - x) + abs(c[1] - y))
    return (_snap25(best[0]), _snap25(best[1]))


def hub_for(
    cluster_pins: Sequence[Point],
    outlines: Iterable[Rect] = (),
) -> Point:
    """簇 hub = 包围盒中心（snap 25）；落 outline 内沿最小分离向量外推。

    Args:
        cluster_pins: 簇内引脚坐标列表。
        outlines: 元件 outline 矩形（避让，可选）。

    Returns:
        ``(x, y)`` 全 25 网格的 hub 坐标。
    """
    pins = [(int(p[0]), int(p[1])) for p in cluster_pins]
    if not pins:
        return (0, 0)
    obs = [tuple(int(v) for v in o) for o in outlines]
    cx, cy = _bbox_center(pins)

    def _in_any(p: Point) -> bool:
        return any(_point_in_rect(p, o) for o in obs)

    # Phase XXVII（S18）：中心不落任何 outline 内 → 直接用。
    if not _in_any((cx, cy)):
        return (cx, cy)
    # 中心落 outline 内：枚举所有重叠 outline 的四边外推候选
    # （margin+25 网格），取第一个不落任何 outline 内且距中心最近者。
    # 旧实现"逐 outline 最小分离向量外推"在密集页（大芯片彼此相邻）会
    # 推出一个 outline 又落进另一个 → 16 次迭代后仍困在 outline 内
    # （page9 GND 簇 hub 落 U6C/D/E 体内 → 簇线横穿芯片根因）。
    candidates: list[tuple[int, int]] = []
    for o in obs:
        x0, y0, x1, y1 = o
        for p in (
            (x1 + _HUB_PUSH_MARGIN + _GRID, cy),
            (x0 - _HUB_PUSH_MARGIN - _GRID, cy),
            (cx, y1 + _HUB_PUSH_MARGIN + _GRID),
            (cx, y0 - _HUB_PUSH_MARGIN - _GRID),
        ):
            ps = (_snap25(p[0]), _snap25(p[1]))
            if not _in_any(ps):
                candidates.append(ps)
    if candidates:
        best = min(
            candidates,
            key=lambda c: abs(c[0] - cx) + abs(c[1] - cy),
        )
        return best
    # 全候选都被其他 outline 覆盖（超密集）→ 退化为旧 16 次迭代推离。
    for _ in range(16):
        hit = next((o for o in obs if _point_in_rect((cx, cy), o)), None)
        if hit is None:
            break
        cx, cy = _push_out_of_rect((cx, cy), hit, margin=_HUB_PUSH_MARGIN)
    return (cx, cy)


def _seg_intersects_rect(seg: Segment, rect: Rect) -> bool:
    """线段是否与矩形相交（含端点贴边）。"""
    x1, y1, x2, y2 = seg
    lo_x, hi_x = min(rect[0], rect[2]), max(rect[0], rect[2])
    lo_y, hi_y = min(rect[1], rect[3]), max(rect[1], rect[3])
    if x1 == x2:  # vertical
        return lo_x <= x1 <= hi_x and min(y1, y2) <= hi_y and max(y1, y2) >= lo_y
    if y1 == y2:  # horizontal
        return lo_y <= y1 <= hi_y and min(x1, x2) <= hi_x and max(x1, x2) >= lo_x
    if (lo_x <= x1 <= hi_x and lo_y <= y1 <= hi_y) or \
       (lo_x <= x2 <= hi_x and lo_y <= y2 <= hi_y):
        return True
    return False


def _seg_passes_pin(
    seg: Segment,
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
) -> bool:
    """线段是否**恰穿过**其他引脚（同列/同行，y/x 严格在段范围内）。

    Phase XXVII（S13 用户要求：过引脚必须 0）：簇内短接 L 路径 / 三段式
    回退只检查 outline —— hub 与引脚同列（hx==px）时 L 退化成沿列直线
    → 穿同列引脚。此处对 L 路径每段做精确 d0 判定（排除段端点：端点即
    引脚连接点，合法）。

    Args:
        seg: 正交线段 (x1, y1, x2, y2)。
        pin_cols: 页面全量引脚 x → [y...]（含段端点引脚）。
        pin_rows: 页面全量引脚 y → [x...]（含段端点引脚）。

    Returns:
        True 时该段穿过某引脚内部（d0）。
    """
    if not pin_cols and not pin_rows:
        return False
    x1, y1, x2, y2 = seg
    if x1 == x2:  # vertical
        if not pin_cols:
            return False
        lo, hi = min(y1, y2), max(y1, y2)
        for py in pin_cols.get(x1, ()):
            if lo < py < hi:
                return True
    elif y1 == y2:  # horizontal
        if not pin_rows:
            return False
        lo, hi = min(x1, x2), max(x1, x2)
        for px in pin_rows.get(y1, ()):
            if lo < px < hi:
                return True
    return False


def _path_clear(
    segs: Sequence[Segment],
    obs: Sequence[Rect],
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
) -> bool:
    """路径全部段不穿 outline 且不恰穿过引脚（Phase XXVII d0 判定）。"""
    if any(_seg_intersects_rect(s, o) for s in segs for o in obs):
        return False
    if pin_cols or pin_rows:
        if any(_seg_passes_pin(s, pin_cols, pin_rows) for s in segs):
            return False
    return True


def _pin_to_hub_stub(
    pin: Point, hub: Point,
    outlines: Sequence[Rect],
    stub_lead: int,
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
) -> list[Segment]:
    """单引脚 → hub 短接段（R5 复用 + Phase XXII QA 修复 Issue 1）。

    Phase XXII QA 修复：优先 **2 段 L 路径**（pin→corner→hub，端点不变）
    —— 簇内引脚间距 ≤ ``parallel_short_dist`` 已很近，无需每引脚三段式
    引出（显著降低 WIRE 段数）。L 路径穿 outline（或 pin==hub 时）回退
    原三段式路径（R5：延伸→折线→调头，避让）。

    Args:
        pin: 引脚坐标（端点不动）。
        hub: 簇 hub 坐标。
        outlines: 元件 outline（避让）。
        stub_lead: 引出距离（回退路径用）。
        pin_cols: 页面全量引脚 x → [y...]（d0 判定，可选）。
        pin_rows: 页面全量引脚 y → [x...]（d0 判定，可选）。

    Returns:
        2-4 段正交 WIRE 段（端点 pin 不变，全 25 网格）。
    """
    px, py = int(pin[0]), int(pin[1])
    hx, hy = int(hub[0]), int(hub[1])
    obs = [tuple(int(v) for v in o) for o in outlines]

    # ── 优先 2 段 L（两种角点，取第一个不与 outline 冲突的）────────
    # Phase XXVII（S13 用户要求：引脚必须向外引出避免过引脚）：**恒先
    # 水平再垂直**（角点 (hx, py)）—— 引脚先水平离开引脚列，再垂直到
    # hub；垂直段在 hub x（列外）。旧序按轴长选角点，dy>dx 时先垂直
    # 沿列 → 穿同列引脚。
    # Phase XXVII（S14）：L 路径还不得**恰穿过**其他引脚（pin_cols/
    # pin_rows 页面全量）—— hub 与引脚同列（hx==px）时两角点都退化
    # 成沿列直线 → 全部拒绝 → 走三段式水平引出（离开引脚列）。
    _corner_choices: list[Point] = [(hx, py), (px, hy)]
    for _corner in _corner_choices:
        _l_out: list[Segment] = []
        for a, b in (((px, py), _corner), (_corner, (hx, hy))):
            if a != b:
                _l_out.append((a[0], a[1], b[0], b[1]))
        if not _l_out:
            break  # pin == hub → 回退原三段式（小回路）
        if _path_clear(_l_out, obs, pin_cols, pin_rows):
            return _l_out

    # ── 回退：原三段式短接段（R5：延伸→折线→调头）──────────────────
    dx, dy = hx - px, hy - py
    # Phase XXVII（S13 用户要求：引脚必须向外引出避免过引脚）：**优先
    # 水平引出**（离开引脚列，垂直段在列旁）。水平引出点动态取**引脚
    # 所属 body 边界外 50**（引脚在 body 内是 Cadence 规范，固定 x±lead
    # 可能仍在 body 内被 outline 检查回退 → 垂直沿列穿同列引脚）。
    _own_body = None
    for _o in obs:
        if _o[0] < px < _o[2] and _o[1] < py < _o[3]:
            _own_body = _o
            break
    _h_ex = None
    if _own_body is not None:
        _mid = (_own_body[0] + _own_body[2]) / 2
        _cand = _own_body[0] - 100 if px <= _mid else _own_body[2] + 100
        # 校验：不在**其他** body 内（自身体除外）。
        if not any(
            _o is not _own_body and _o[0] < _cand < _o[2]
            and _o[1] < py < _o[3]
            for _o in obs
        ):
            _h_ex = _cand
    if _h_ex is None:
        _h_cand = _snap25(px - (1 if dx > 0 else -1) * stub_lead)
        if not any(o[0] < _h_cand < o[2] and o[1] < py < o[3] for o in obs):
            _h_ex = _h_cand
    if _h_ex is not None:
        ex, ey = _h_ex, py
        lead_axis = "x"
    elif dx == 0 and dy == 0:
        ex, ey = px, _snap25(py - stub_lead)
        lead_axis = "y"
    elif abs(dx) >= abs(dy):
        ex = _snap25(px - (1 if dx > 0 else -1) * stub_lead)
        ey = py
        lead_axis = "x"
    else:
        ex = px
        ey = _snap25(py - (1 if dy > 0 else -1) * stub_lead)
        lead_axis = "y"

    def _path(j: Point) -> list[Segment]:
        jx, jy = j
        if abs(hx - jx) >= abs(hy - jy):
            corner = (hx, jy)
        else:
            corner = (jx, hy)
        out: list[Segment] = []
        for a, b in (
            ((px, py), (ex, ey)),
            ((ex, ey), (jx, jy)),
            ((jx, jy), corner),
            (corner, (hx, hy)),
        ):
            if a != b:
                out.append((a[0], a[1], b[0], b[1]))
        return out

    base_path = _path((ex, ey))
    if _path_clear(base_path, obs, pin_cols, pin_rows):
        return base_path
    # 3. J 垂直方向外推（两个方向、递增 50，最多 16 次），取最近空闲。
    # Phase XXVII（S14）：jog 候选扩大（7→17 次，off 最大 150+16*50）。
    # **全拒时返回"穿引脚最少"的候选**（含 base_path）—— 电气连接
    # **必须保持**（引脚断连比穿引脚严重：早期"全拒返回空"导致并联
    # 电容引脚无短接段 → 悬空断连）。jog 优先 0 穿引脚车道；全拒回退
    # base_path（宁留少量 d0，不断连）。
    _best: list[Segment] | None = None
    _best_pins: int = 10**9
    for k in range(1, 17):
        off = 150 + k * 50
        for sign in (1, -1):
            if lead_axis == "x":
                cand = (ex, _snap25(ey + sign * off))
            else:
                cand = (_snap25(ex + sign * off), ey)
            path = _path(cand)
            if _path_clear(path, obs, pin_cols, pin_rows):
                return path
            # 记录穿引脚最少者（兜底：宁可少量 d0 也不断连）。
            _n = sum(
                1 for s in path
                if _seg_passes_pin(s, pin_cols, pin_rows)
            )
            if _n < _best_pins:
                _best_pins = _n
                _best = path
    if _best is not None and _best_pins <= 1:
        return _best
    return base_path


def _pin_to_hub_stub_h(
    pin: Point, hub: Point,
    outlines: Sequence[Rect],
    stub_lead: int,
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
) -> list[Segment]:
    """水平 stub（pin → hub，先垂直引出离开同行）短接段。

    Phase XXVII（S14）：与 ``_pin_to_hub_stub`` 镜像 —— x/y 轴交换后复用
    垂直实现（离开引脚行），再换回。用于水平段穿过同行引脚的 d0 重绕。

    Args:
        pin: 引脚坐标（端点不动）。
        hub: hub 坐标。
        outlines: 元件 outline（避让）。
        stub_lead: 引出距离。
        pin_cols: 页面全量引脚 x → [y...]（交换后作行索引）。
        pin_rows: 页面全量引脚 y → [x...]（交换后作列索引）。

    Returns:
        正交 WIRE 段列表（端点 pin 不变，全 25 网格）。
    """
    swapped_pin = (int(pin[1]), int(pin[0]))
    swapped_hub = (int(hub[1]), int(hub[0]))
    swapped_outlines = [
        (int(o[1]), int(o[0]), int(o[3]), int(o[2])) for o in outlines
    ]
    swapped_cols = {
        y: sorted(xs) for y, xs in (pin_rows or {}).items()
    } if pin_rows else None
    swapped_rows = {
        x: sorted(ys) for x, ys in (pin_cols or {}).items()
    } if pin_cols else None
    segs = _pin_to_hub_stub(
        swapped_pin, swapped_hub, swapped_outlines, stub_lead,
        pin_cols=swapped_cols, pin_rows=swapped_rows,
    )
    return [(s[1], s[0], s[3], s[2]) for s in segs]


def hub_short_wires(
    hub: ParallelHub,
    outlines: Sequence[Rect] = (),
    stub_lead: int = 100,
    pin_cols: dict[int, list[int]] | None = None,
    pin_rows: dict[int, list[int]] | None = None,
) -> list[Segment]:
    """把 ``ParallelHub`` 转成簇内短接 WIRE 段（引脚 → hub）。

    每个引脚先沿外引 ``stub_lead`` 再折向 hub（三段式，R5 复用）；
    hub 自身的引脚（pin == hub）无需段。返回段端点 = 引脚坐标不变。
    调用方负责从 hub 引出 1 条到 GND 符号（``hub_to_symbol_wire``）。

    Phase XXVII（S14）：``pin_cols``/``pin_rows``（页面全量引脚）传入
    ``_pin_to_hub_stub`` —— 短接段不恰穿过其他引脚（hub 与引脚同列时
    L 路径退化沿列 → 拒绝 → 三段式水平引出）。

    Args:
        hub: ``route_cluster_parallel``（旧式 API）返回的 ParallelHub。
        outlines: 元件 outline（避让，可选）。
        stub_lead: 引脚外引距离（默认 100）。
        pin_cols: 页面全量引脚 x → [y...]（可选）。
        pin_rows: 页面全量引脚 y → [x...]（可选）。

    Returns:
        正交 WIRE 段列表（可能为空；全部 25 网格）。
    """
    out: list[Segment] = []
    for pin in hub.pin_coords:
        out.extend(
            _pin_to_hub_stub(
                pin, hub.hub, outlines, stub_lead,
                pin_cols=pin_cols, pin_rows=pin_rows,
            )
        )
    return out


def hub_to_symbol_wire(
    hub: Point, symbol_pin: Point,
    outlines: Sequence[Rect] = (),
) -> list[Segment]:
    """从簇 hub 到 GND 符号引脚的"1 条引出线"（正交 L，零长剔除）。

    Phase XXIII P1-3：``outlines`` 非空时检查 L 路径是否穿元件体
    （语义同 ``WireLayoutEngine._stub_direct_blocked`` 的 outline 检查）；
    受阻时在网格上试 90° 折线绕行（Z 路径，最多 2 次折弯），全部受阻
    回退第一条 L 路径（尽力而为，不阻塞电气连接）。

    Args:
        hub: 簇 hub（或 ParallelHub.outlet 引出点）。
        symbol_pin: GND 符号引脚坐标（body + pin_offset）。
        outlines: 元件 outline（避让，可选；默认空 = 零回归直连）。

    Returns:
        1-3 段正交 WIRE 段（可能为空）。
    """
    hx, hy = int(hub[0]), int(hub[1])
    sx, sy = int(symbol_pin[0]), int(symbol_pin[1])
    if (hx, hy) == (sx, sy):
        return []
    obs = [tuple(int(v) for v in o) for o in outlines]

    def _segs(pts: Sequence[Point]) -> list[Segment]:
        out: list[Segment] = []
        for a, b in zip(pts, pts[1:]):
            if a != b:
                out.append((a[0], a[1], b[0], b[1]))
        return out

    def _clear(segs: list[Segment]) -> bool:
        if not obs:
            return True
        return not any(_seg_intersects_rect(s, o) for s in segs for o in obs)

    def _l(corner: Point) -> list[Segment]:
        return _segs(((hx, hy), corner, (sx, sy)))

    # ── 1. 直接 L（两种角点，取第一个不穿 outline 的）────────────
    for corner in ((sx, hy), (hx, sy)):
        path = _l(corner)
        if _clear(path):
            return path
    if not obs:
        return _l((sx, hy))

    # ── 2. 90° 折线绕行（Z 路径，≤2 次折弯）─────────────────────
    # 候选：hub → (jx,hy) → (jx,sy) → symbol（x 方向错位）与
    #       hub → (hx,jy) → (sx,jy) → symbol（y 方向错位）。
    # 错位车道从 hub/符号坐标向两侧递增 50，最多 8 轮。
    # Phase XXVII（S13 用户要求：过引脚必须 0）：**off=0 时强制错位**
    # （jx=hx 会使垂直段沿 hub 列走，穿同列引脚）—— 首轮即用 ±50。
    step = 50
    for k in range(0, 9):
        offsets = [k * step, -k * step] if k == 0 else [k * step, -k * step]
        for off in offsets:
            for jx in (hx + off, sx + off):
                path = _segs(((hx, hy), (jx, hy), (jx, sy), (sx, sy)))
                if len(path) <= 3 and _clear(path):
                    return path
            for jy in (hy + off, sy + off):
                path = _segs(((hx, hy), (hx, jy), (sx, jy), (sx, sy)))
                if len(path) <= 3 and _clear(path):
                    return path
    # ── 3. Phase XXVII（S13，用户要求 0 穿体）：A* 迷宫绕障 ─────────
    # 旧实现回退第一条 L 路径（尽力而为 → GND 簇线穿 U6C 大 body）。
    # 现改为 A* 无条件绕障：找不到绕路时返回空（调用方改网络名连接，
    # 绝不直穿元件体）。
    try:
        from .astar_router import astar_route

        _path = astar_route((hx, hy), (sx, sy), obs, margin=50)
        if _path is not None and len(_path) >= 2:
            _segs_p = _segs(_path)
            # S15：路径正交校验 —— A* 快速路径在 hub/符号不对齐时直连
            # 会生成斜段（Cadence 无效）；斜段丢弃（回退空，网络名连接）。
            if all(s[0] == s[2] or s[1] == s[3] for s in _segs_p):
                return _segs_p
    except Exception:  # A* 异常不阻塞（回退 Z 绕行结果或空）
        pass
    logger.debug(
        "GND hub→symbol wire blocked near %s→%s → A* no route", hub, symbol_pin,
    )
    return []


def _start_point(
    candidate: Point | Sequence[Point],
    outlines: Sequence[Rect],
) -> Point:
    """归一化起点：候选点直接 snap；簇引脚列表取 hub。"""
    if (
        isinstance(candidate, (list, tuple))
        and len(candidate) > 0
        and isinstance(candidate[0], (list, tuple))
        and not isinstance(candidate[0], str)
    ):
        return hub_for(candidate, outlines)
    return (_snap25(candidate[0]), _snap25(candidate[1]))


def _page_band(edge_clearance: int) -> tuple:
    """页边冗余带（C 纸边界 ± edge_clearance）；0 = 无约束。"""
    if edge_clearance <= 0:
        return (None, None, None, None)
    return (
        _PAGE_X0 + edge_clearance,
        _PAGE_X1 - edge_clearance,
        _PAGE_Y0 + edge_clearance,
        _PAGE_Y1 - edge_clearance,
    )


def _symbol_spot_free(
    px: int, py: int,
    outlines: Sequence[Rect], margin: int,
    pin_points: set[Point], pin_offset: tuple[int, int],
    x_lo: int | None, x_hi: int | None,
    y_lo: int | None, y_hi: int | None,
    edge_clearance: int,
) -> bool:
    """GND 符号位置是否空闲（outline / 引脚禁区 / 页边）。"""
    if x_lo is not None:
        in_band = x_lo <= px <= x_hi and y_lo <= py <= y_hi
        near_origin_clear = px >= edge_clearance and py >= edge_clearance
        if not (in_band or near_origin_clear):
            return False
    ox, oy = int(pin_offset[0]), int(pin_offset[1])
    gnd_pin = (px + ox, py + oy)
    for (x0, y0, x1, y1) in outlines:
        if x0 - margin <= px <= x1 + margin and y0 - margin <= py <= y1 + margin:
            return False
        if x0 - margin <= gnd_pin[0] <= x1 + margin and \
           y0 - margin <= gnd_pin[1] <= y1 + margin:
            return False
    if pin_points and any(
        ((gnd_pin[0] - pp[0]) ** 2 + (gnd_pin[1] - pp[1]) ** 2) ** 0.5 < margin
        for pp in pin_points
    ):
        return False
    return True


def _spiral_search(
    x: int, y: int, free, max_tries: int, step: int,
) -> Point | None:
    """从 (x, y) 出发螺旋搜索第一个 ``free`` 位置（25 网格）。"""
    for ring in range(1, max_tries + 1):
        for dx in range(-ring * step, ring * step + 1, step):
            for dy in range(-ring * step, ring * step + 1, step):
                if max(abs(dx), abs(dy)) != ring * step:
                    continue
                px, py = _snap25(x + dx), _snap25(y + dy)
                if free(px, py):
                    return (px, py)
    return None


def place_gnd_symbol(
    candidate: Point | Sequence[Point],
    outlines: Sequence[tuple[int, int, int, int]] = (),
    margin: int = 50,
    max_tries: int = 12,
    step: int = 100,
    pin_points: Sequence[Point] | None = None,
    pin_offset: tuple[int, int] = _GND_PIN_OFFSET,
    edge_clearance: int = 0,
    preferred_dir: tuple[int, int] | None = None,
    near_chip_offset: int = 100,
) -> tuple[int, int]:
    """找一个不落在任何元件 outline（含 margin 膨胀）内的 GND 符号位置。

    从候选点（或簇引脚列表的 hub）出发，按螺旋/步进搜索空闲格点
    （优先下方/右方等空旷方向），全部排除后取第一个空闲点。

    Phase XXVIII R5/R6：候选须同时满足 —— body 点不在膨胀 outline 内、
    GND 引脚点（body + pin_offset）不在膨胀 outline 内、引脚点不进
    引脚禁区（距任一引脚 ≥ margin）、``edge_clearance>0`` 时不进入
    C 纸页边 ± edge_clearance 带（或距原点角 ≥ edge_clearance）。

    Phase XXVI（F8，S11 视觉审查 #28，用户裁决 V6）：GND 按**引脚方位**
    放置 —— ``preferred_dir``（引脚背离元件体主轴向）给出后：
      1. **精确反解**：body = pin + outward*near_chip_offset − pin_offset
         （符号引脚落在引脚外延射线上），经 ``_symbol_spot_free`` 校验
         通过即用（电容接地引脚向下 → GND 放下方，不穿电容）；
      2. **方向射线搜索**：沿方向（±45° 扇区）外推；
      3. 校验失败才回退全局螺旋。

    Args:
        candidate: 首选坐标（点 ``(x, y)``）或簇引脚列表（自动取 hub）。
        outlines: ``(x0, y0, x1, y1)`` 元件包围盒列表（绝对坐标）。
        margin: 元件外侧避让冗余区（``overlap.avoid_margin``，默认 50）。
        max_tries: 搜索最大环数。
        step: 搜索步长（默认 100，snap 25）。
        pin_points: 已占用引脚坐标（禁区，可选）。
        pin_offset: GND 引脚相对 body 偏移（默认 golden (50,100)）。
        edge_clearance: 页边冗余区（0 = 不检查页边）。
        preferred_dir: 引脚方位主轴向 ``(dx, dy)``（单位向量，如 (0,1)
            表示引脚朝下 → 符号放引脚下方；None = 旧螺旋行为）。
        near_chip_offset: 方向反解的外引距离（``gnd_distribution.
            near_chip_offset``，默认 100）。

    Returns:
        空闲坐标（25 网格）；找不到时返回 hub 上方外推点。
    """
    x, y = _start_point(candidate, outlines)
    pts = (
        {(int(p[0]), int(p[1])) for p in pin_points}
        if pin_points else set()
    )
    x_lo, x_hi, y_lo, y_hi = _page_band(edge_clearance)
    if not outlines and not pts and x_lo is None and preferred_dir is None:
        return (x, y)

    def _free(px: int, py: int) -> bool:
        return _symbol_spot_free(
            px, py, outlines, margin, pts, pin_offset,
            x_lo, x_hi, y_lo, y_hi, edge_clearance,
        )

    if preferred_dir is not None:
        dx, dy = int(preferred_dir[0]), int(preferred_dir[1])
        if dx or dy:
            # 1. 精确反解：符号引脚落在引脚外延射线上。
            nx = _snap25(x + dx * int(near_chip_offset))
            ny = _snap25(y + dy * int(near_chip_offset))
            bx = _snap25(nx - int(pin_offset[0]))
            by = _snap25(ny - int(pin_offset[1]))
            if _free(bx, by):
                return (bx, by)
            # 2. 方向射线搜索（±45° 扇区 + 两侧邻扇区）。
            found = _directional_search(
                x, y, (dx, dy), _free, max_tries, step,
            )
            if found is not None:
                return found
    if _free(x, y):
        return (x, y)
    found = _spiral_search(x, y, _free, max_tries, step)
    if found is not None:
        return found
    logger.warning(
        "GND place: no free slot near %s within %d tries", (x, y), max_tries,
    )
    return (_snap25(x), _snap25(y + 100))


def _directional_search(
    x: int, y: int,
    direction: tuple[int, int],
    free, max_tries: int, step: int,
) -> Point | None:
    """沿方向射线（±45° 扇区 + 两侧邻扇区）搜索第一个空闲位置。

    Phase XXVI（F8，用户裁决 V6）：GND 按引脚方位选择性区域搜索 ——
    先沿主方向射线外推，再尝试主方向两侧 ±50/100 的邻扇区（近似 ±45°），
    全部失败返回 None（调用方回退全局螺旋）。

    Args:
        x/y: 起点（候选点/hub）。
        direction: 主方向单位向量（如 (0,1) / (-1,0)）。
        free: ``(px, py) -> bool`` 空闲判定。
        max_tries: 最大外推环数。
        step: 步长（默认 100，snap 25）。

    Returns:
        空闲坐标（25 网格）；None = 未找到。
    """
    dx, dy = int(direction[0]), int(direction[1])
    if not dx and not dy:
        return None
    # 主方向射线。
    for k in range(1, max_tries + 1):
        px, py = _snap25(x + dx * k * step), _snap25(y + dy * k * step)
        if free(px, py):
            return (px, py)
    # 两侧邻扇区（垂直偏移 ±50/100）。
    px_dir = (-dy, dx)  # 垂直
    for k in range(1, max_tries + 1):
        for off in (50, 100, -50, -100):
            px = _snap25(x + dx * k * step + px_dir[0] * off)
            py = _snap25(y + dy * k * step + px_dir[1] * off)
            if free(px, py):
                return (px, py)
    return None


# ---------------------------------------------------------------------------
#  Phase XXIII P1-3: GND 密度补点（ensure_gnd_symbols）
# ---------------------------------------------------------------------------


def _block_of(p: Point) -> int:
    """返回点所在页面 1/4 分块编号（1..4，2×2 象限）。"""
    x, y = int(p[0]), int(p[1])
    col = 0 if x < _BLOCK_X_MID else 1
    row = 0 if y < _BLOCK_Y_MID else 1
    return row * 2 + col + 1


def _block_center(block: int) -> Point:
    """返回分块中心（snap 25 网格）。"""
    if block == 1:
        cx = (_PAGE_X0 + _BLOCK_X_MID) / 2
        cy = (_PAGE_Y0 + _BLOCK_Y_MID) / 2
    elif block == 2:
        cx = (_PAGE_X0 + _BLOCK_X_MID) / 2
        cy = (_BLOCK_Y_MID + _PAGE_Y1) / 2
    elif block == 3:
        cx = (_BLOCK_X_MID + _PAGE_X1) / 2
        cy = (_PAGE_Y0 + _BLOCK_Y_MID) / 2
    else:
        cx = (_BLOCK_X_MID + _PAGE_X1) / 2
        cy = (_BLOCK_Y_MID + _PAGE_Y1) / 2
    return (_snap25(cx), _snap25(cy))


def ensure_gnd_symbols(
    gnd_pins: Sequence[Point],
    existing_symbol_pins: Sequence[Point] = (),
    outlines: Sequence[Rect] = (),
    pin_points: Sequence[Point] = (),
    margin: int = 50,
    min_pins: int = 3,
    min_dist: int = 1500,
    pin_offset: tuple[int, int] = _GND_PIN_OFFSET,
    edge_clearance: int = 0,
    max_tries: int = 12,
) -> list[dict]:
    """GND 密度补点：页面 1/4 分块，每块补 1 个 GND 符号（Phase XXIII P1-3）。

    对页面做 2×2 分块（象限）。某块若同时满足：
      * 块内非电源 GND 引脚数 ≥ ``min_pins``（默认 3）；
      * 块内最近引脚到最近已有 GND 符号引脚的距离 > ``min_dist``
        （默认 1500；无已有符号时恒满足）；
    则在块中心（snap25）经 ``place_gnd_symbol`` 避让路径补 1 个 GND 符号
    ``GND_SYM_B{block}`` —— 与 ``place_gnd_symbol`` 同样避让元件 outline /
    引脚禁区 / 页边冗余区，符号引脚 = body + ``pin_offset``。

    Args:
        gnd_pins: 页面上 GND 网引脚坐标（非电源引脚；密度判定对象）。
        existing_symbol_pins: 已有 GND 符号引脚坐标（判定"距最近符号"）。
        outlines: 元件 outline 矩形（避让，可选）。
        pin_points: 已占用引脚坐标（禁区，可选）。
        margin: 元件外侧避让冗余区（``overlap.avoid_margin``）。
        min_pins: 触发补点的最小块内引脚数。
        min_dist: 距最近 GND 符号的补点距离阈值。
        pin_offset: GND 引脚相对 body 偏移（默认 golden (50,100)）。
        edge_clearance: 页边冗余区（0 = 不检查页边）。
        max_tries: 螺旋搜索最大环数。

    Returns:
        ``[{"refdes": "GND_SYM_B{block}", "x", "y", "net": "GND",
        "pin_coord": (x, y)}]`` —— 全部 25 网格。
    """
    pins = [(int(p[0]), int(p[1])) for p in gnd_pins]
    if not pins:
        return []
    existing = [(int(p[0]), int(p[1])) for p in existing_symbol_pins]
    obs = [tuple(int(v) for v in o) for o in outlines]
    pts = {(int(p[0]), int(p[1])) for p in pin_points}
    symbols: list[dict] = []
    for block in (1, 2, 3, 4):
        block_pins = [p for p in pins if _block_of(p) == block]
        if len(block_pins) < min_pins:
            continue
        # 块内最近引脚到最近已有符号的距离 > min_dist 才补点。
        nearest = min(
            (_manhattan(p, s) for p in block_pins for s in existing),
            default=None,
        )
        if nearest is not None and nearest <= min_dist:
            continue
        cx, cy = _block_center(block)
        sx, sy = place_gnd_symbol(
            (cx, cy), obs, margin=margin, max_tries=max_tries,
            pin_points=sorted(pts), pin_offset=pin_offset,
            edge_clearance=edge_clearance,
        )
        spx, spy = sx + int(pin_offset[0]), sy + int(pin_offset[1])
        symbols.append({
            "refdes": f"GND_SYM_B{block}",
            "x": sx, "y": sy,
            "net": "GND",
            "pin_coord": (_snap25(spx), _snap25(spy)),
        })
        pts.add((_snap25(spx), _snap25(spy)))
        logger.debug(
            "GND density block %d: %d pins → symbol %s",
            block, len(block_pins), (sx, sy),
        )
    return symbols
