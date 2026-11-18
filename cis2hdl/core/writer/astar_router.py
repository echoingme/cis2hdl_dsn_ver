"""A* 迷宫避障路由（Phase XXVII S13，用户要求 0 穿体）。

在原理图网格上做 A* 搜索：障碍 = 所有元件 body 矩形（膨胀 margin），
搜索从起点到终点避开全部障碍的正交路径。用于：
  * trunk 水平段穿体 → A* 绕障（阶梯形路径）
  * GND 簇 trunk 穿体 → A* 绕障
  * 密集页 trunk_blocked → 不再回退直穿，改为绕行

算法（行业标准，调研自 tscircuit/autorouting + Qrouter）：
  * 网格 = 50 单位（WIRE 端点 25 网格，路径点 snap 50 保证正交端点合法）
  * 启发式 = 曼哈顿距离（正交运动）
  * 障碍判定 = 线段与膨胀 body 矩形相交
  * 输出 = 正交路径点序列（含起点/终点，全部 25 网格）

0 穿体铁律：找不到绕路时返回 None（调用方改网络名连接，不直穿）。
"""

from __future__ import annotations

import heapq
from typing import Iterable, Sequence

Point = tuple[int, int]
Rect = tuple[int, int, int, int]  # x0, y0, x1, y1


def seg_intersects_rect(
    a: Point, b: Point, rect: Rect, margin: int = 0,
) -> bool:
    """线段 (a,b) 是否与膨胀后的 rect 相交（与报告同口径）。

    Phase XXVII 0 穿体：口径与 ``WireLayoutEngine.wires_through_bodies``
    一致 —— 段坐标深入 body 内部且范围跨越即穿；端点贴边不穿；段
    完全覆盖 body 范围（起点终点恰在边界）不穿。A* 与报告用同一判定，
    保证"绕行成功 = 报告 0 穿"。
    """
    x0, y0, x1, y1 = rect
    if margin:
        x0 -= margin
        y0 -= margin
        x1 += margin
        y1 += margin
    ax, ay, bx, by = a[0], a[1], b[0], b[1]
    if ax == bx:  # vertical
        if not (x0 < ax < x1):
            return False
        lo, hi = min(ay, by), max(ay, by)
        if lo < y1 and hi > y0 and not (lo == y0 and hi == y1):
            return True
        return False
    if ay == by:  # horizontal
        if not (y0 < ay < y1):
            return False
        lo, hi = min(ax, bx), max(ax, bx)
        if lo < x1 and hi > x0 and not (lo == x0 and hi == x1):
            return True
        return False
    return False  # 非正交（不应出现）


def path_clear(
    path: Sequence[Point],
    rects: Sequence[Rect],
    margin: int = 0,
) -> bool:
    """路径所有相邻点连线都不穿过任何 rect。"""
    for a, b in zip(path, path[1:]):
        for r in rects:
            if seg_intersects_rect(a, b, r, margin):
                return False
    return True


def astar_route(
    start: Point,
    goal: Point,
    rects: Sequence[Rect],
    margin: int = 0,
    step: int = 50,
    max_iter: int = 40000,
) -> list[Point] | None:
    """A* 迷宫避障：正交路径点序列（含 start/goal，25 网格）。

    Args:
        start: 起点（须不在任何膨胀 rect 内）。
        goal: 终点（须不在任何膨胀 rect 内）。
        rects: 障碍矩形（元件 body，绝对坐标）。
        margin: 障碍膨胀边距。
        step: 网格步长（默认 50）。
        max_iter: 迭代上限（防爆炸）。

    Returns:
        正交路径点列表；无解返回 None（调用方改网络名，不直穿）。
    """
    # 膨胀障碍 + 起点/终点合法性。
    obs: list[Rect] = []
    for r in rects:
        x0, y0, x1, y1 = r
        obs.append((x0 - margin, y0 - margin, x1 + margin, y1 + margin))

    def blocked(x: int, y: int) -> bool:
        for (x0, y0, x1, y1) in obs:
            if x0 < x < x1 and y0 < y < y1:
                return True
        return False

    # 快速路径：无障碍时直连（[start, goal] 两点）—— 0 穿体且省 WIRE。
    # 注意：调用方（hub_to_symbol_wire）须自行校验路径正交（start/goal
    # 不对齐时直连会生成斜段 = Cadence 无效 WIRE）；A* 兜底输入恒正交
    # （穿体段端点同 x/y），直连安全。
    if not obs or not any(
        seg_intersects_rect(start, goal, o) for o in obs
    ):
        return [start, goal]

    if blocked(*start) or blocked(*goal):
        # 起点/终点在障碍内（引脚在 body 内——正常，引脚在 outline 内）：
        # 不阻止搜索；仅中段需要避让。允许起终点在障碍内。
        pass

    # A*（4 邻域，正交）。
    open_heap: list[tuple[int, int, int, int]] = []  # (f, g, y, x)
    gscore: dict[Point, int] = {start: 0}
    came: dict[Point, Point] = {}
    heapq.heappush(open_heap, (0, 0, start[1], start[0]))
    visited = 0
    while open_heap and visited < max_iter:
        f, g, cy, cx = heapq.heappop(open_heap)
        cur = (cx, cy)
        visited += 1
        if cur == goal:
            # 回溯路径 + 压缩（移除共线中间点，减少 WIRE 段数）。
            path: list[Point] = []
            node: Point | None = goal
            while node is not None:
                path.append(node)
                node = came.get(node)
            path.reverse()
            compressed = _compress_path(path)
            # S18：压缩后逐段整段校验 —— A* 主循环只查 25 步小段，小段
            # 端点贴 body 边（恰覆盖 body 全高）被豁免，压缩成整段后
            # 穿过 body 内部（报告判定穿）→ 绕行"成功"却仍穿。压缩后
            # 若整段穿体，回退**安全压缩**（贪心延伸共线点，每段校验
            # 不穿体——段数远少于逐 25 步小段）；仍穿才回退未压缩路径。
            if _path_segs_clear(compressed, obs):
                return compressed
            safe = _safe_compress(path, obs)
            if _path_segs_clear(safe, obs):
                return safe
            if _path_segs_clear(path, obs):
                return path
            return None
        for dx, dy in ((step, 0), (-step, 0), (0, step), (0, -step)):
            nxt = (cx + dx, cy + dy)
            if nxt in gscore:
                continue
            ng = g + step
            # 中段障碍判定（端点贴边允许——起终点常在 outline 内）。
            h = abs(nxt[0] - goal[0]) + abs(nxt[1] - goal[1])
            blocked_mid = any(
                seg_intersects_rect(cur, nxt, o)
                for o in obs
            )
            # 允许"贴着障碍边缘走"：仅当段整体穿过障碍内部才禁止。
            if blocked_mid:
                continue
            gscore[nxt] = ng
            came[nxt] = cur
            heapq.heappush(open_heap, (ng + h, ng, nxt[1], nxt[0]))
    return None  # 无解


def _compress_path(path: list[Point]) -> list[Point]:
    """压缩路径：移除共线中间点（水平/垂直直段只留端点）。

    A* 输出逐网格点（step=50），相邻点共线 → 合并为单段，减少 WIRE 数。

    Args:
        path: A* 原始路径点序列。

    Returns:
        压缩后路径点（相邻点不再共线，最少点数）。
    """
    if len(path) <= 2:
        return path
    out: list[Point] = [path[0]]
    for i in range(1, len(path) - 1):
        a, b, c = path[i - 1], path[i], path[i + 1]
        # 三点共线（同 x 或同 y）→ 跳过 b（a→c 直连）。
        if (a[0] == b[0] == c[0]) or (a[1] == b[1] == c[1]):
            continue
        out.append(b)
    out.append(path[-1])
    return out


def _path_segs_clear(path: Sequence[Point], rects: Sequence[Rect]) -> bool:
    """路径全部相邻点连成的整段都不穿过任何 rect（S18 压缩后校验）。"""
    for a, b in zip(path, path[1:]):
        if a == b:
            continue
        for o in rects:
            if seg_intersects_rect(a, b, o):
                return False
    return True


def _safe_compress(path: Sequence[Point], rects: Sequence[Rect]) -> list[Point]:
    """安全压缩：贪心延伸共线路径点，每段校验不穿任何 rect。

    S18：全压缩（_compress_path）可能把"贴 body 边沿逐 25 步小段"合成
    穿体整段；逐 25 步未压缩路径段数暴涨（WIRE 7556→10631）。安全压缩
    在两者之间：从当前点出发尽量向直线方向延伸，延伸段穿体即停，保证
    段数少且每段不穿体。
    """
    if len(path) <= 2:
        return list(path)
    out: list[Point] = [path[0]]
    i = 1
    while i < len(path):
        best = i
        j = i
        while j < len(path):
            if out[-1] == path[j]:
                j += 1
                continue
            # 只合并共线延伸（同 x 或同 y）—— 跨拐点贪心会产生斜段。
            if out[-1][0] != path[j][0] and out[-1][1] != path[j][1]:
                break
            if any(
                seg_intersects_rect(out[-1], path[j], o) for o in rects
            ):
                break
            best = j
            j += 1
        out.append(path[best])
        i = best + 1
    return out
