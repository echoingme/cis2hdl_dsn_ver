"""走线折线拼接 + 线↔引脚连接关系（架构设计 §3.4/§3.5，P0-4/P0-5）。

折线拼接（P0-4）：
    同 net 的 WireRecord 线段建无向邻接图（坐标级合并），
    度 1 = 端点、度 2 = 拐点、度 ≥3 = 分支点、全环 = 无端点。
    从端点/分支点出发沿邻接追踪，得有序折线链。

连接关系（P0-5）：
    - wire 端点 → net：端点坐标精确匹配网名表。
    - pin_point → net：引脚点坐标 ∈ wire 端点集合 → 该端点 net。
    - pin_point → instance：最近实例 body（曼哈顿距离阈值）。
    - pin_key 解析：实例 pin_label_map 标号 > Packages pin_names 序 >
      str(object_id)。

诚实口径：精确「线→引脚号」依赖 SymbolPinScalar/Bus(26/27) 解码器
（P1 已列入扩展审计）。P0 交付坐标级连接 + 实例级关联 + 引脚键尽力而为。
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..decode.net_table import NetTable
from ..frame.splitter import Frame

if TYPE_CHECKING:
    from ..bind.binder import InstanceBind
    from .wire_frames import WireRecord

logger = logging.getLogger(__name__)

#: 引脚点帧类型（0x01=引脚/连接引用、0x03=连接点、短 0x05/0x06=网标/端口引用）
PIN_POINT_FRAME_TYPES = frozenset((0x01, 0x03, 0x05, 0x06))
#: 引脚点帧最大长度（短帧；长文本帧交给 page_text_frames）
PIN_POINT_MAX_LEN = 50
#: 实例关联曼哈顿距离阈值（mil）
INSTANCE_PROXIMITY = 500
#: 引脚点坐标上限
_MAX_COORD = 30000
#: 2 引脚无源件方向判定容差（mil）：页面引脚点帧（SymbolPin 位置，
#: 即 EXP 引脚名 A 的物理端）距 cache 引脚线外端 ≤ 该值才判定方向。
#: 实证（08-20 主理人攻坚）：引脚点距线端 0~2 为真命中（R65/R56/
#: R202），阵列器件最近引脚点距线端 20+（误配，排除）。
_PIN_DIR_TOL = 8


@dataclass
class WirePolyline:
    """同 net 折线链。

    Attributes:
        net_id: 网 id（net_table 键）。
        net_name: 网名。
        points: 有序拐点序列（首尾为端点/分支点）。
        offsets: 每段所属帧页内偏移（长度 = points-1）。
        branch_points: 度≥3 共享端点（T 形/十字）。
        is_closed: 是否闭环（无端点）。
    """

    net_id: int | None
    net_name: str
    points: list[tuple[int, int]] = field(default_factory=list)
    offsets: list[int] = field(default_factory=list)
    branch_points: list[tuple[int, int]] = field(default_factory=list)
    is_closed: bool = False
    # ── P2-4 走线样式（取首段样式；默认 0/0/0）──────────────────
    color: int = 0
    line_width: int = 0
    line_style: int = 0

    def to_dict(self) -> dict:
        return {
            "net_id": self.net_id,
            "net_name": self.net_name,
            "points": [list(p) for p in self.points],
            "offsets": list(self.offsets),
            "branch_points": [list(p) for p in self.branch_points],
            "is_closed": self.is_closed,
        }


@dataclass
class PinPointRecord:
    """引脚点记录（0x01/0x03/0x05/0x06 短帧）。"""

    offset: int
    frame_type: int
    x: int
    y: int
    object_id: int

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": self.frame_type,
            "x": self.x,
            "y": self.y,
            "object_id": self.object_id,
        }


@dataclass
class ConnectivityResult:
    """一页的线↔引脚连接关系。"""

    page: str
    pin_points: list[PinPointRecord] = field(default_factory=list)
    wire_endpoint_to_net: dict[tuple[int, int], str] = field(default_factory=dict)
    pin_point_to_net: dict[int, str] = field(default_factory=dict)
    pin_point_to_instance: dict[int, str] = field(default_factory=dict)
    pin_connections: dict[tuple[str, str], str] = field(default_factory=dict)
    net_connections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @property
    def connection_count(self) -> int:
        return len(self.pin_connections)

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "pin_points": [p.to_dict() for p in self.pin_points],
            "wire_endpoint_to_net": {
                f"{x},{y}": net for (x, y), net in self.wire_endpoint_to_net.items()
            },
            "pin_point_to_net": dict(self.pin_point_to_net),
            "pin_point_to_instance": dict(self.pin_point_to_instance),
            "pin_connections": {
                f"{refdes}.{pin}": net for (refdes, pin), net in self.pin_connections.items()
            },
            "net_connections": dict(self.net_connections),
        }


def _parse_pin_point(frame: Frame) -> PinPointRecord | None:
    """从短帧（0x01/0x03/0x05/0x06）提取引脚点。

    布局（08 页 0x01@0x57f7 实测）：``[DELIM][type:u16][x:u16][y:u16][id:u32][trailer]``。

    Args:
        frame: 输入帧。

    Returns:
        PinPointRecord；非引脚点帧或坐标非法返回 None。
    """
    if frame.frame_type not in PIN_POINT_FRAME_TYPES:
        return None
    if len(frame.raw) > PIN_POINT_MAX_LEN:
        return None
    rec = frame.raw
    if len(rec) < 18:
        return None
    # 跳过短帧里可能存在的文本标记（0x18），仅取纯坐标短帧
    if b"\x18" in rec[10:16]:
        return None
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    object_id = int.from_bytes(rec[14:18], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    return PinPointRecord(
        offset=frame.offset,
        frame_type=frame.frame_type,
        x=x,
        y=y,
        object_id=object_id,
    )


def collect_pin_points(frames: list[Frame]) -> list[PinPointRecord]:
    """扫描一页全部引脚点短帧。

    Args:
        frames: 页面帧列表（结构区）。

    Returns:
        PinPointRecord 列表（按帧偏移升序）。
    """
    result: list[PinPointRecord] = []
    for frame in frames:
        rec = _parse_pin_point(frame)
        if rec is not None:
            result.append(rec)
    return result


def build_wire_polylines(
    wires: list["WireRecord"],
    net_table: NetTable,
) -> list[WirePolyline]:
    """按 net_id 分组拼接折线链（无向邻接图）。

    Args:
        wires: 一页走线段。
        net_table: 网名表。

    Returns:
        WirePolyline 列表（每网至少一条；含分支）。
    """
    # 按 net_id 分组
    by_net: dict[int | None, list["WireRecord"]] = defaultdict(list)
    for wire in wires:
        by_net[wire.net_id].append(wire)

    polylines: list[WirePolyline] = []
    for net_id, group in by_net.items():
        polylines.extend(_stitch_net(net_id, net_table, group))
    return polylines


def _stitch_net(
    net_id: int | None,
    net_table: NetTable,
    wires: list["WireRecord"],
) -> list[WirePolyline]:
    """拼接单个 net 的折线链。

    算法：
    1. 无向邻接表 point → [(neighbor, wire)]（同坐标合并）。
    2. 度 1 = 端点；度 ≥3 = 分支点；无端点 = 闭环。
    3. 从每个端点出发沿未访问边追踪，遇分支点时**优选直行邻居**
       （保持行进方向），把分支点作为过路点；遇另一端点/已访问停。
    4. 剩余未使用边（分支臂 / 闭环）单独输出。
    """
    if not wires:
        return []
    net_name = net_table.name_of(net_id)

    # 邻接表
    adj: dict[tuple[int, int], list[tuple[tuple[int, int], "WireRecord"]]] = defaultdict(list)
    for wire in wires:
        p1 = (wire.x1, wire.y1)
        p2 = (wire.x2, wire.y2)
        adj[p1].append((p2, wire))
        adj[p2].append((p1, wire))

    used: set[int] = set()  # id(wire) 去重
    branches = {p for p, nbrs in adj.items() if len(nbrs) >= 3}
    endpoints = [p for p, nbrs in adj.items() if len(nbrs) == 1]

    def _straight_neighbor(
        current: tuple[int, int],
        came_from: tuple[int, int],
        candidates: list[tuple[tuple[int, int], "WireRecord"]],
    ) -> tuple[tuple[int, int], "WireRecord"] | None:
        """候选邻居中优选直行（与行进方向一致）的未使用边。"""
        dx = current[0] - came_from[0]
        dy = current[1] - came_from[1]
        usable = [
            (nbr, w) for nbr, w in candidates
            if id(w) not in used and nbr != came_from
        ]
        if not usable:
            return None
        for nbr, w in usable:
            # 直行：继续沿同一 x 或同一 y
            if (dx != 0 and nbr[0] == current[0] + (1 if dx > 0 else -1) and nbr[1] == current[1]) or \
               (dy != 0 and nbr[1] == current[1] + (1 if dy > 0 else -1) and nbr[0] == current[0]):
                return nbr, w
        # 无直行邻居 → 取第一个未使用
        return usable[0]

    def trace_to_endpoint(start: tuple[int, int]) -> list[tuple[int, int]] | None:
        """从 start（端点）出发追踪到另一端点；返回链或 None。"""
        chain: list[tuple[int, int]] = [start]
        current = start
        came_from: tuple[int, int] | None = None
        while True:
            candidates = adj[current]
            # 端点且非起点 → 到达另一端
            if len(candidates) == 1 and current != start:
                break
            if came_from is None:
                # 起点：只有一个邻居（端点）
                if len(candidates) != 1:
                    return None
                nbr, wire = candidates[0]
                if id(wire) in used:
                    return None
                used.add(id(wire))
                chain.append(nbr)
                came_from = current
                current = nbr
                continue
            pick = _straight_neighbor(current, came_from, candidates)
            if pick is None:
                break
            nbr, wire = pick
            used.add(id(wire))
            chain.append(nbr)
            came_from = current
            current = nbr
        if len(chain) < 2:
            return None
        return chain

    result: list[WirePolyline] = []

    # 从每个端点出发追踪（含过路分支点）
    for endpoint in sorted(endpoints):
        if all(id(w) in used for _n, w in adj[endpoint]):
            continue
        chain = trace_to_endpoint(endpoint)
        if chain is None:
            continue
        # 反向规范化：起点取字典序小者（保序稳定）
        if chain[0] > chain[-1]:
            chain = list(reversed(chain))
        result.append(WirePolyline(
            net_id=net_id,
            net_name=net_name,
            points=chain,
            branch_points=sorted(branches),
            is_closed=False,
            # P2-4：取首段样式
            color=wires[0].color if hasattr(wires[0], "color") else 0,
            line_width=wires[0].line_width if hasattr(wires[0], "line_width") else 0,
            line_style=wires[0].line_style if hasattr(wires[0], "line_style") else 0,
        ))

    # 从分支点出发沿未使用边追踪分支臂
    for branch in sorted(branches):
        for neighbor, _wire in adj[branch]:
            if id(_wire) in used:
                continue
            chain = [branch]
            current = branch
            came_from: tuple[int, int] | None = None
            while True:
                candidates = adj[current]
                if came_from is None:
                    # 分支起点：取该未使用边
                    nbr, wire = neighbor, _wire
                    if id(wire) in used:
                        break
                    used.add(id(wire))
                    chain.append(nbr)
                    came_from = current
                    current = nbr
                    continue
                if len(candidates) == 1:
                    break  # 到达端点
                pick = _straight_neighbor(current, came_from, candidates)
                if pick is None:
                    break
                nbr, wire = pick
                used.add(id(wire))
                chain.append(nbr)
                came_from = current
                current = nbr
            if len(chain) >= 2:
                result.append(WirePolyline(
                    net_id=net_id,
                    net_name=net_name,
                    points=chain,
                    branch_points=sorted(branches),
                    is_closed=False,
                ))

    # 剩余未使用边（闭环 / 残留）兜底
    remaining = [w for w in wires if id(w) not in used]
    for wire in remaining:
        result.append(WirePolyline(
            net_id=net_id,
            net_name=net_name,
            points=[(wire.x1, wire.y1), (wire.x2, wire.y2)],
            offsets=[wire.offset],
            branch_points=sorted(branches),
            is_closed=not endpoints,
        ))
        used.add(id(wire))

    # 填充 offsets：按链内相邻点反查 wire.offset
    offset_by_pair: dict[tuple[tuple[int, int], tuple[int, int]], int] = {}
    for wire in wires:
        offset_by_pair[((wire.x1, wire.y1), (wire.x2, wire.y2))] = wire.offset
        offset_by_pair[((wire.x2, wire.y2), (wire.x1, wire.y1))] = wire.offset
    for poly in result:
        poly.offsets = [
            offset_by_pair.get((poly.points[i], poly.points[i + 1]), 0)
            for i in range(len(poly.points) - 1)
        ]
        poly.is_closed = (
            len(poly.points) > 2 and poly.points[0] == poly.points[-1]
        )
        if poly.is_closed:
            poly.points = poly.points[:-1]
            poly.offsets = poly.offsets[:-1]

    return result


def _nearest_instance(
    point: tuple[int, int],
    binds: list["InstanceBind"],
) -> "InstanceBind | None":
    """找曼哈顿距离 ≤ 阈值的最近实例。"""
    best: "InstanceBind | None" = None
    best_dist = INSTANCE_PROXIMITY + 1
    for bind in binds:
        if bind.loc_x is None or bind.loc_y is None:
            continue
        dist = abs(bind.loc_x - point[0]) + abs(bind.loc_y - point[1])
        if dist <= INSTANCE_PROXIMITY and dist < best_dist:
            best_dist = dist
            best = bind
    return best


#: P2-B：引脚邻近判定 bbox 容差（mil；引脚位于体框边界外侧）
_PIN_BBOX_TOL = 60
#: Cache 引脚线端点匹配容差（20mil）：真引脚端点 dist 恒 ≤2，
#: 收紧防斜向杂散点（轴距各自 ≤60 会吸入 dist 100 的伪命中）。
_CACHE_PIN_TOL = 20
#: P2-B：GND 符号邻近判定容差（mil）
_GND_PROXIMITY = 60
#: P2-B：小元件体框面积阈值（mil²；R/C/L/NPN 等无源件）
_SMALL_BBOX_AREA = 10000
#: P2-B：小元件最大引脚数（无源件惯例 2 引脚）
_SMALL_MAX_PINS = 2


def _bbox_edge_dist(
    bbox: tuple[int, int, int, int], point: tuple[int, int], tol: int,
) -> int | None:
    """计算点到实例体框边界的最小曼哈顿距离（引脚判定）。

    引脚位于体框边界外侧（或恰在边界）：点在 bbox 的 x 跨度内且贴近
    y1/y2（水平边界），或 y 跨度内且贴近 x1/x2（垂直边界）。返回最小
    边界距离；不贴边返回 None。

    Args:
        bbox: (x1, y1, x2, y2) 体框（页面 mil 坐标）。
        point: (px, py) 候选点。
        tol: 容差（mil）。

    Returns:
        最小边界距离；不在边界附近返回 None。
    """
    x1, y1, x2, y2 = bbox
    px, py = point
    best: int | None = None
    if x1 - tol <= px <= x2 + tol:
        for edge_y in (y1, y2):
            d = abs(py - edge_y)
            if d <= tol and (best is None or d < best):
                best = d
    if y1 - tol <= py <= y2 + tol:
        for edge_x in (x1, x2):
            d = abs(px - edge_x)
            if d <= tol and (best is None or d < best):
                best = d
    return best


def _bbox_pin_score(
    bbox: tuple[int, int, int, int], point: tuple[int, int], tol: int,
) -> tuple[int, int] | None:
    """计算引脚候选点的归属评分（P2-B 密集区判别）。

    小元件（无源件，bbox 面积 < ``_SMALL_BBOX_AREA``）：引脚位于
    体框**主轴中心**（水平件在左右端、垂直件在上下端），评分 =
    边界距离 + 垂直主轴偏移惩罚。这使真实引脚（中心位置）胜出密集区
    的邻近噪声端点（如 R190 与相邻 R189 的边界竞争）。

    大元件（IC/BGA）：引脚分布全边界，评分 = 纯边界距离。

    Args:
        bbox: (x1, y1, x2, y2) 体框。
        point: (px, py) 候选点。
        tol: 容差（mil）。

    Returns:
        (评分, 边界距离)；不贴边返回 None。评分越小越可能是该实例引脚。
    """
    x1, y1, x2, y2 = bbox
    px, py = point
    w = x2 - x1
    h = y2 - y1
    edge: int | None = None
    perp = 0
    if x1 - tol <= px <= x2 + tol:
        for edge_y in (y1, y2):
            d = abs(py - edge_y)
            if d <= tol and (edge is None or d < edge):
                edge = d
    if y1 - tol <= py <= y2 + tol:
        for edge_x in (x1, x2):
            d = abs(px - edge_x)
            if d <= tol and (edge is None or d < edge):
                edge = d
    if edge is None:
        return None
    if w * h <= _SMALL_BBOX_AREA:
        # 小元件：垂直主轴偏移惩罚（水平件 y≈center；垂直件 x≈center）
        if w >= h:
            perp = abs(py - (y1 + y2) / 2)
        else:
            perp = abs(px - (x1 + x2) / 2)
        score = edge * 2 + int(perp)
    else:
        score = edge
    return score, edge


def _assign_pin_instance(
    point: tuple[int, int],
    binds: list["InstanceBind"],
    tol: int = _PIN_BBOX_TOL,
) -> "InstanceBind | None":
    """把引脚候选点归属到实例（P2-B 精确判定）。

    优先级：
    1. 最近 bbox 边界（容差 tol）的**非 GND** 实例 —— 引脚点在体框
       边界附近，取最小边界距离；
    2. GND 符号（容差 _GND_PROXIMITY）—— GND 走线端点；
    3. 最近 loc（≤500，旧逻辑兜底）。

    返回 None 表示无法归属（防御，不抛异常）。
    """
    best, _score = _assign_pin_scored(point, binds, tol)
    return best


def _assign_pin_scored(
    point: tuple[int, int],
    binds: list["InstanceBind"],
    tol: int = _PIN_BBOX_TOL,
) -> tuple["InstanceBind | None", int]:
    """``_assign_pin_instance`` 的评分版：返回 (实例, 评分)。

    Args:
        point: 候选点。
        binds: 实例列表。
        tol: 容差。

    Returns:
        (实例, 评分)；无法归属返回 (None, 0)。
    """
    best: "InstanceBind | None" = None
    best_d = 10**9
    for bind in binds:
        if bind.bbox is None or bind.shape == "GND_SYMBOL":
            continue
        got = _bbox_pin_score(bind.bbox, point, tol)
        if got is not None and got[0] < best_d:
            best_d = got[0]
            best = bind
    if best is not None:
        return best, best_d
    # GND 符号
    gnd: "InstanceBind | None" = None
    gnd_d = _GND_PROXIMITY + 1
    for bind in binds:
        if bind.shape != "GND_SYMBOL" or bind.loc_x is None or bind.loc_y is None:
            continue
        d = abs(bind.loc_x - point[0]) + abs(bind.loc_y - point[1])
        if d < gnd_d:
            gnd_d = d
            gnd = bind
    if gnd is not None:
        return gnd, gnd_d
    nearest = _nearest_instance(point, binds)
    return (nearest, 0) if nearest is not None else (None, 0)


def _resolve_pin_key(
    instance: "InstanceBind",
    point: tuple[int, int],
) -> str:
    """尽力解析引脚键（P2-A 兼容，P2-B 位置化）。

    优先级：实例 pin_label_map 标号 > Packages pin_names 序 > object_id。
    P0 精度受限（无 SymbolPin 26/27 精确坐标），返回尽力而为结果。
    """
    # 1) 标号映射（Cache）：取距离最近的标号？P0 无标号坐标 → 取第一个非空
    if instance.pin_label_map:
        # 无精确标号坐标，返回首个标号（诚实标注：待 P1 精确化）
        for label, pin_name in instance.pin_label_map.items():
            if pin_name:
                return f"{label}:{pin_name}"
    # 2) pin_names 序
    if instance.pin_names:
        return instance.pin_names[0]
    return ""


def _sort_pin_points(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """按几何位置排序引脚点（P2-B 位置化引脚键）。

    先按主轴排序：若点在 y 方向分散更大（垂直引脚列）按 y；否则按 x。
    """
    if not points:
        return []
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if (max(ys) - min(ys)) > (max(xs) - min(xs)):
        return sorted(points, key=lambda p: (p[1], p[0]))
    return sorted(points, key=lambda p: (p[0], p[1]))


def _pin_key_positional(
    instance: "InstanceBind",
    point: tuple[int, int],
    ordered_points: list[tuple[int, int]],
) -> str:
    """按实例引脚表位置解析引脚键（P2-B）。

    优先级：
    1. 实例 pin_names 且连接点数 == len(pin_names)（**全部引脚已连接**，
       如 NPN/USB 小 IC）→ 按位置序取引脚名（映射可靠）；
    2. 实例 pin_map 且连接点数 == len(pin_map) → 按位置序取引脚号；
    3. 双引脚且无引脚表 → 'A'/'B'（无源件 OrCAD 惯例，左/上为 A）；
    4. 兜底 'P{idx}'（位置索引，诚实唯一键）。

    **U7 多单元注意**：每单元只显示芯片引脚子集（连接数 << 409），
    位置序与 pin_names 全表序不匹配（实证：0D 段位置 0 实为 AVDD09_GE1
    而非 pin_names[0]=DDR_ADDR0）。此时不使用引脚名（避免错名），
    改用位置键 'P{idx}'（下游按位置/页面反查）。

    Args:
        instance: 实例绑定。
        point: 当前引脚点。
        ordered_points: 该实例全部引脚点（已按位置排序）。

    Returns:
        引脚键（非空）。
    """
    try:
        idx = ordered_points.index(point)
    except ValueError:
        idx = 0
    n = len(ordered_points)
    if instance.pin_names and len(instance.pin_names) == n:
        name = instance.pin_names[idx]
        if name:
            return name
    if instance.pin_map and len(instance.pin_map) == n:
        num = instance.pin_map[idx]
        if num:
            return num
    if n == 2:
        return "A" if idx == 0 else "B"
    return f"P{idx}"


class ConnectivityBuilder:
    """连接关系构建器（P0-5）。

    Usage::

        builder = ConnectivityBuilder(net_table)
        result = builder.build(wires, pin_points, binds)
    """

    def __init__(self, net_table: NetTable) -> None:
        """初始化。

        Args:
            net_table: 该页网名表。
        """
        self.net_table = net_table

    def build(
        self,
        wires: list["WireRecord"],
        pin_points: list[PinPointRecord],
        binds: list["InstanceBind"],
        *,
        net_name_records: list | None = None,
    ) -> ConnectivityResult:
        """构建一页连接关系（P0-5 + P2-B 引脚级网络全量）。

        P2-B 增强：除引脚点帧（0x01/0x03/0x05/0x06）外，把**全部走线
        端点**（含网名）作为引脚候选点，经体框边界精确归属到实例
        （``_assign_pin_instance``），并位置化解析引脚键
        （``_pin_key_positional``）。网名帧（``net_name_records``）仅作
        诊断透传（网名已由走线网名表承载）。

        Args:
            wires: 该页走线段。
            pin_points: 该页引脚点。
            binds: 该页实例绑定。
            net_name_records: P2-B 网名帧记录（可选，诊断）。

        Returns:
            ConnectivityResult。
        """
        page = binds[0].page if binds else ""
        result = ConnectivityResult(page=page, pin_points=list(pin_points))

        # 1) wire 端点 → net
        endpoint_to_net: dict[tuple[int, int], str] = {}
        for wire in wires:
            net_name = self.net_table.name_of(wire.net_id)
            if net_name:
                endpoint_to_net[(wire.x1, wire.y1)] = net_name
                endpoint_to_net[(wire.x2, wire.y2)] = net_name
        result.wire_endpoint_to_net = endpoint_to_net

        # 2) pin_point → net
        for pp in pin_points:
            net_name = endpoint_to_net.get((pp.x, pp.y), "")
            if net_name:
                result.pin_point_to_net[pp.object_id] = net_name

        # 3) pin_point → instance
        for pp in pin_points:
            inst = _nearest_instance((pp.x, pp.y), binds)
            if inst is not None:
                result.pin_point_to_instance[pp.object_id] = inst.refdes

        # 4) pin_connections / net_connections（P2-B 位置化引脚键）
        #    候选 = 引脚点（旧路径）+ 全部走线端点（P2-B 新增）
        candidates: dict[tuple[int, int], str] = {}
        for pp in pin_points:
            net_name = result.pin_point_to_net.get(pp.object_id)
            if net_name:
                candidates[(pp.x, pp.y)] = net_name
        for point, net_name in endpoint_to_net.items():
            candidates.setdefault(point, net_name)

        # P2-B：网名帧驱动补充 —— 网名标签（net, row=y）标记引脚行；
        # 该行上与网名匹配的走线端点即引脚（填补 degree≥2 结点引脚）。
        net_name_row_points: set[tuple[int, int]] = set()
        if net_name_records:
            for rec in net_name_records:
                for (px, py), net in endpoint_to_net.items():
                    if net != rec.net_name:
                        continue
                    if abs(py - rec.row) > 10:
                        continue
                    candidates.setdefault((px, py), net)
                    net_name_row_points.add((px, py))

        # 走线图叶端点集（大元件引脚判定：仅叶端点=引脚；避免过路走线污染）
        leaf_points: set[tuple[int, int]] = set()
        adj: dict[tuple[int, int], list] = defaultdict(list)
        for wire in wires:
            p1 = (wire.x1, wire.y1)
            p2 = (wire.x2, wire.y2)
            adj[p1].append(p2)
            adj[p2].append(p1)
        leaf_points = {p for p, nbrs in adj.items() if len(nbrs) == 1}

        # 按实例分组候选点（体框归属 + 评分）
        by_instance: dict[str, dict[tuple[int, int], tuple[str, int]]] = defaultdict(dict)
        # ── 4a-1) cache 引脚线 90° 旋转双假设匹配（08-20 攻坚）──────
        # 18 页 2P5GE / 14 页 MDI / 16 页 SLIC 的 C、DZ_L 等实例旋转 90°
        # 放置（cache 线垂直但走线左右/上下）：cache 符号坐标未旋转 →
        # 线端与真实引脚位置垂直错位（C188/C190/C192/C206 实测）。
        # **双假设匹配**：不旋转 vs 顺时针 90°（绕两外端中点），取
        # cache 线外端与走线端点**匹配总距离更优**者。实证旋转中心 =
        # cache 外端中点（C188=(380,740)、C192=(380,770) 全部精确命中）。
        # 自动保护：水平符号（R65 等）旋转后无命中 → 选不旋转。
        rotated_lines: dict[str, list] = {}
        for bind in binds:
            lines = getattr(bind, "cache_pin_lines", None)
            if not lines or len(lines) != 2:
                continue

            def _match_cost(ls: list) -> tuple[int, int]:
                total, hits = 0, 0
                for _n, _a1, _b1, _a2, _b2 in ls:
                    best = 99
                    for (px, py) in candidates:
                        d = abs(px - _a2) + abs(py - _b2)
                        if d < best:
                            best = d
                    if best <= _CACHE_PIN_TOL:
                        total += best
                        hits += 1
                    else:
                        total += _CACHE_PIN_TOL * 2
                return total, hits

            c1, h1 = _match_cost(lines)
            # LED 符号（A 右 B 左）不参与旋转：LED 由 4a0 特例统一翻转
            # （阳极 A 恒接 3V3_SOC），旋转会破坏其左右排列
            # （网格搜索误伤 D2/D4/D9 等 8 个 LED 的根因）。
            if bind.shape and bind.shape.rsplit(".", 1)[0] == "LED":
                continue
            ax2, ay2 = lines[0][3], lines[0][4]
            bx2, by2 = lines[1][3], lines[1][4]
            mx = (ax2 + bx2) / 2.0
            my = (ay2 + by2) / 2.0
            # 已完美匹配（两端命中且总距 ≤8）→ 不旋转
            if h1 >= 2 and c1 <= 8:
                continue
            # 网格搜索旋转参数：方向（顺/逆）× 中心偏移（外端中点 ±15）。
            # 方向不唯一（RD8 与 D21 同 marker 0x40 却方向相反）→ 双向
            # 都测，取匹配最优；等距歧义时 marker 方向优先（RD8 类）。
            # 中心偏移因实例而异（D11 中点+(0,15)）。仅对未完美匹配
            # 的器件搜索。
            marker40 = bool(getattr(bind, "rot_marker40", False))
            if marker40:
                # marker 0x40：先测屏幕顺时针（cx-dy，RD8 用），
                # 若匹配不优再测逆时针（D21 用）
                signs = (-1, 1)
            else:
                # marker 00：先测屏幕逆时针（cx+dy，C188 用）
                signs = (1, -1)
            best: tuple[int, int, list] | None = None
            for _sign in signs:
                for _dx in (0, 5, 10, 15, -5, -10, -15):
                    for _dy in (0, 5, 10, 15, -5, -10, -15):
                        cx = mx + _dx
                        cy = my + _dy
                        rl: list = []
                        for name, lx1, ly1, lx2, ly2 in lines:
                            def _rot(px, py, s=_sign, cxx=cx, cyy=cy):
                                if s > 0:  # 屏幕逆时针（y 向下系）
                                    return (cxx + (py - cyy),
                                            cyy - (px - cxx))
                                # 屏幕顺时针
                                return (cxx - (py - cyy),
                                        cyy + (px - cxx))
                            nx1, ny1 = _rot(lx1, ly1)
                            nx2, ny2 = _rot(lx2, ly2)
                            rl.append((name, nx1, ny1, nx2, ny2))
                        c2, h2 = _match_cost(rl)
                        if h2 >= 2 and (best is None or c2 < best[0]):
                            best = (c2, h2, rl)
            if best is not None and best[0] < c1 - 4:
                rotated_lines[bind.refdes] = best[2]

        def _lines_of(bind: "InstanceBind") -> list:
            return rotated_lines.get(bind.refdes, bind.cache_pin_lines)

        # ── 4a0) 2 引脚无源件方向判定（08-20 攻坚根因）──────────────
        # cache 符号定义 A/B 是**符号局部**坐标（R A 左 B 右、C A 上 B 下、
        # LED A 右 B 左），平移未应用实例旋转/镜像 → 翻转实例的 A/B
        # 与页面物理位置相反（实测 R65/R56 等 131 个 mismatch 根因）。
        # **页面引脚点帧（SymbolPin 位置）即 EXP 引脚名 A 的物理端**。
        # 判定：页面引脚点距 cache 引脚线**外端** ≤_PIN_DIR_TOL 且命中
        # B 线端 → 实例翻转，cache A/B 键名交换（EXP 口径：翻转后 A 端
        # 物理在 B 侧）。实证：真命中距线端 0~2（R65/R56/R202），阵列
        # 器件最近引脚点距线端 20+（08 页 R 阵/14 页 D 阵，误配，排除）。
        # 特例：LED 符号（A 右 B 左）实例统一翻转放置（阳极 A 恒接
        # 3V3_SOC 电源侧），其引脚点距线端 30mil（bbox 平移偏移）——
        # 按符号名统一处理。
        pin_dir_flip: dict[str, dict[str, str]] = {}
        for bind in binds:
            lines = _lines_of(bind)
            if not lines or len(lines) != 2:
                continue
            a_name, a_x1, a_y1, a_x2, a_y2 = lines[0]
            b_name, b_x1, b_y1, b_x2, b_y2 = lines[1]
            a_end = (a_x2, a_y2)
            b_end = (b_x2, b_y2)
            best_d: int | None = None
            best_which: str | None = None
            for pp in pin_points:
                da = abs(pp.x - a_end[0]) + abs(pp.y - a_end[1])
                db = abs(pp.x - b_end[0]) + abs(pp.y - b_end[1])
                m = min(da, db)
                if best_d is None or m < best_d:
                    best_d = m
                    best_which = "A" if da < db else "B"
            flip = False
            if best_d is not None and best_d <= _PIN_DIR_TOL \
                    and best_which == "B":
                flip = True
            elif bind.shape and bind.shape.rsplit(".", 1)[0] == "LED":
                # LED 实例统一翻转（阳极 A 物理在 cache B 侧）
                flip = True
            if flip:
                pin_dir_flip[bind.refdes] = {a_name: b_name, b_name: a_name}
        # ── 4a0b) 多引脚器件引脚名反向映射（08-20 攻坚）────────────
        # J1701-1704（MJ8-M2 排针）等实例**垂直镜像/库序**放置：EXP
        # 引脚名 A1 的物理端（页面引脚点帧）命中 cache **最后一条**线
        # 外端（A8/M2），与 cache A1（第一条，物理上）相反 → 引脚名
        # 列表整体反向（A1↔A8、A2↔A7…、M1↔M2）。判定：最后一线外端
        # 距最近引脚点 ≤_PIN_DIR_TOL 且明显近于第一线外端。实测
        # J1704：A8 端 (730,780) 距引脚点 0、A1 端 (730,710) 距 20。
        # U7 等大 IC 第一/最后线端都有引脚点（d_last 不优）→ 不反向。
        pin_reverse: dict[str, dict[str, str]] = {}
        for bind in binds:
            lines = _lines_of(bind)
            if not lines or len(lines) < 3:
                continue
            # 与第一线同侧（外端 x 同列）的最后一条线 —— 排针
            # A1-A8 同列（外端 x 相同），M1/M2 在另一侧（x 不同）。
            # 用 A8（同侧末条）而非 lines[-1]（M2）判定镜像。
            f_end = (lines[0][3], lines[0][4])
            same_side = [l for l in lines if l[3] == f_end[0]]
            if len(same_side) < 2:
                # 水平排针（外端 y 同行）
                same_side = [l for l in lines if l[4] == f_end[1]]
            last_line = same_side[-1] if same_side else lines[-1]
            l_end = (last_line[3], last_line[4])
            d_first = min(
                (abs(pp.x - f_end[0]) + abs(pp.y - f_end[1])
                 for pp in pin_points), default=99,
            )
            d_last = min(
                (abs(pp.x - l_end[0]) + abs(pp.y - l_end[1])
                 for pp in pin_points), default=99,
            )
            # 反向要求：第一线外端**无**引脚点（d_first > tol，镜像后
            # 原首引脚位置无 SymbolPin）且末线外端有（d_last 小）。
            # U3（SOP8）首引脚外端有引脚点 → d_first 小 → 不反向。
            if (d_last <= _PIN_DIR_TOL and d_first > _PIN_DIR_TOL
                    and d_last < d_first - 4):
                # 按"外端 x 同列"分组，组内反向（A1-A8 同列 → A1↔A8、
                # A2↔A7…；M1/M2 同列 → M1↔M2；T24 P1/P4 左列 → P1↔P4）。
                # 全不同列（如退化点 x 各异）才按外端 y 同行分组。
                # 整列表反向会把 A1 映到 M2（错位，实测 J1704 键名乱套）。
                groups: dict[int, list] = {}
                for line in lines:
                    groups.setdefault(line[3], []).append(line)
                if len(groups) == len(lines):
                    groups = {}
                    for line in lines:
                        groups.setdefault(line[4], []).append(line)
                rev: dict[str, str] = {}
                for g in groups.values():
                    for i, line in enumerate(g):
                        rev[line[0]] = g[-(i + 1)][0]
                pin_reverse[bind.refdes] = rev
        # 4a) Cache 引脚线优先归属（最高优先级：精确几何 > bbox 竞争）
        # 无 Packages pinMap 的小 IC（3A BUCK/LDO_1/PSW6M2/SOP8 等），其引脚
        # 线端点（页面 mil，Cache 命名引脚定义 + 渲染原点平移）是走线端点的
        # 精确目标。候选点落在引脚线端点 ±容差内 → 直接归属该实例并记录
        # 真实引脚名；命中点从通用归属循环排除（每点只归属一次，杜绝
        # U1 SW/VIN/GND/EN 被邻近 GND_71/R83 的 bbox 竞争抢走的 115 缺口）。
        # **引脚线匹配取垂直距离最小者**：相邻引脚线（如 3A BUCK 的
        # SW y=810 与 BOOT y=790）仅差 20mil < 容差 60，先到先得会串位
        # （SW 误取 BOOT 网名）→ 对每个候选点收集全部命中线，按
        # 垂直/水平距离排序取最近。
        cache_claimed: set[tuple[int, int]] = set()
        cache_pin_hits: dict[str, dict[tuple[int, int], str]] = defaultdict(dict)
        # 收集全部 (point, refdes, pin_name, dist) 候选命中
        _cache_matches: list[tuple[tuple[int, int], str, str, int]] = []
        for bind in binds:
            if not getattr(bind, "cache_pin_lines", None):
                continue
            for pin_name, lx1, ly1, lx2, ly2 in _lines_of(bind):
                for (px, py), net_name in candidates.items():
                    if (px, py) in cache_claimed:
                        continue
                    # 命中判定：点到线段的**联合距离** ≤ 容差，且**距离
                    # 阈值收紧到 20mil**——真引脚端点（走线端点恰好接在
                    # 引脚线端）dist 恒为 0~2；轴距各自 ≤60 的宽松判定会
                    # 把斜向杂散点（(390,870)→FB dist=80、(200,890)→GND
                    # dist=100）吸入，与真点生成相同键名后被错误网名覆盖。
                    if ly1 == ly2:
                        perp = abs(py - ly1)
                        along = 0 if min(lx1, lx2) <= px <= max(lx1, lx2) \
                            else min(abs(px - lx1), abs(px - lx2))
                    else:
                        perp = abs(px - lx1)
                        along = 0 if min(ly1, ly2) <= py <= max(ly1, ly2) \
                            else min(abs(py - ly1), abs(py - ly2))
                    if perp > _CACHE_PIN_TOL or along > _CACHE_PIN_TOL:
                        continue
                    dist = perp + along
                    _cache_matches.append(((px, py), bind.refdes, pin_name, dist))
        # 两级去重：①每点取最近命中线；②每 (refdes, pin_name) 只保留
        # dist 最小点（同一引脚名多个命中点会生成相同键 → dict 覆盖错乱）
        _best: dict[tuple[int, int], tuple[str, str, int]] = {}
        for point, refdes, pin_name, dist in _cache_matches:
            cur = _best.get(point)
            if cur is None or dist < cur[2]:
                _best[point] = (refdes, pin_name, dist)
        _best_by_pin: dict[tuple[str, str], tuple[tuple[int, int], int]] = {}
        for point, (refdes, pin_name, dist) in _best.items():
            key = (refdes, pin_name)
            cur = _best_by_pin.get(key)
            if cur is None or dist < cur[1]:
                _best_by_pin[key] = (point, dist)
        _final_hits: dict[tuple[int, int], tuple[str, str, int]] = {
            pt: (refdes, pin_name, dist)
            for (refdes, pin_name), (pt, dist) in _best_by_pin.items()
        }

        for point, (refdes, pin_name, _dist) in _final_hits.items():
            bind = next((b for b in binds if b.refdes == refdes), None)
            if bind is None:
                continue
            net_name = candidates.get(point, "")
            # D4：GND/电源符号连接网名语义归一（与通用归属一致）
            if bind.shape == "GND_SYMBOL":
                net_name = "GND"
            by_instance[refdes][point] = (net_name, -1)
            cache_pin_hits[refdes][point] = pin_name
            cache_claimed.add(point)
        for point, net_name in candidates.items():
            if point in cache_claimed:
                continue
            inst, score = _assign_pin_scored(point, binds)
            if inst is None:
                continue
            # D4：GND/电源符号连接网名语义归一为 'GND'（符号代表地，
            # 走线端点网名可能是邻近电源网；黄金标准 648/652 一致）
            if inst.shape == "GND_SYMBOL":
                net_name = "GND"
            # 大元件（IC/BGA）：非叶端点（过路走线）不作为引脚，
            # 但网名帧标记的引脚行除外（结点引脚有网名标签）
            if inst.bbox is not None:
                x1, y1, x2, y2 = inst.bbox
                if (x2 - x1) * (y2 - y1) > _SMALL_BBOX_AREA \
                        and point not in leaf_points \
                        and point not in net_name_row_points:
                    continue
            by_instance[inst.refdes][point] = (net_name, score)

        # 小元件（无源件）引脚数封顶：仅对**无引脚表**的小元件生效。
        # 封顶值 = 附近引脚点（最近归属）的去重网名数（R83→2、R190→2），
        # 下限 2；有引脚表（NPN/USB 等）→ 封顶 = 引脚表长度。
        # 保留评分最低的 cap 个端点，避免密集区邻近走线噪声端点污染。
        # 注：cache_pin_hits 命中的点（真实 Cache 引脚几何）**永不裁剪**，
        # 且小 IC 封顶值取其 cache 引脚数（U1→6），替代旧"宁可少解不误解"
        # 的封顶 2（115 缺口主要来源）。
        pin_point_nets_by_ref: dict[str, set[str]] = defaultdict(set)
        for pp in pin_points:
            net_name = result.pin_point_to_net.get(pp.object_id)
            if not net_name:
                continue
            inst, _score = _assign_pin_scored((pp.x, pp.y), binds)
            if inst is not None:
                pin_point_nets_by_ref[inst.refdes].add(net_name)

        for refdes, points in list(by_instance.items()):
            inst = next((b for b in binds if b.refdes == refdes), None)
            if inst is None or inst.bbox is None:
                continue
            x1, y1, x2, y2 = inst.bbox
            if (x2 - x1) * (y2 - y1) > _SMALL_BBOX_AREA:
                continue
            if inst.pin_map or inst.pin_names:
                cap = max(len(inst.pin_map), len(inst.pin_names)) or _SMALL_MAX_PINS
            elif getattr(inst, "cache_pin_lines", None):
                # 有 Cache 引脚几何的小 IC → 封顶 = 引脚数（可高于 2）
                cap = max(len(inst.cache_pin_lines), 2)
            else:
                cap = max(2, len(pin_point_nets_by_ref.get(refdes, set())))
            if len(points) > cap:
                ranked = sorted(points.items(), key=lambda kv: kv[1][1])
                keep = {p for p, _v in ranked[:cap]}
                # cache 命中点（真实引脚几何）永不裁剪
                keep |= set(cache_pin_hits.get(refdes, {}))
                by_instance[refdes] = {p: v for p, v in points.items() if p in keep}

        # 位置化引脚键（每实例点排序）
        for refdes, points_nets in by_instance.items():
            inst = next((b for b in binds if b.refdes == refdes), None)
            ordered = _sort_pin_points(list(points_nets.keys()))
            for point, (net_name, _score) in points_nets.items():
                pin_key = (
                    _pin_key_positional(inst, point, ordered)
                    if inst is not None else str(point)
                )
                # T04：Cache 引脚线命中 → 用真实引脚名作键（替代 P{idx} 位置键）
                hit_name = cache_pin_hits.get(refdes, {}).get(point)
                if hit_name:
                    # 翻转实例（页面引脚点帧锚定符号 A 端在 cache B 侧）
                    # → A/B 键名交换，对齐 EXP 引脚名语义
                    pin_key = pin_dir_flip.get(refdes, {}).get(
                        hit_name, hit_name
                    )
                    # 多引脚镜像实例（J1704 等）：引脚名列表反向
                    pin_key = pin_reverse.get(refdes, {}).get(
                        pin_key, pin_key
                    )
                if not pin_key:
                    continue
                key = (refdes, pin_key)
                result.pin_connections[key] = net_name
                result.net_connections.setdefault(net_name, []).append(key)

        logger.debug(
            "Connectivity built: page=%s connections=%d nets=%d",
            page, result.connection_count, len(result.net_connections),
        )
        return result
