"""GND 元件化放置（§9.3，用户裁决 ⑤）。

675 个 GND 实例（全部 GND_SYMBOL，网名统一 "GND"）：按 DSN 坐标
原位放置 hdl_lib GND 元件（不再聚类美化）。每个 GND：

1. ``FORCEADD GND <net> (loc_x loc_y) rot``（坐标直通 0x01 短帧）；
2. 旋转：DSN 位号帧 rot（R-GNDROT 已验证：DSN 无旋转字段 → 默认 0）；
3. 标签：网名 "GND" 置于符号上方（DSN 网标坐标）；
4. mirror 透传。

纯函数：产出放置计划 + FORCEADD 指令行，不触碰文件系统。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class GndPlacement:
    """单个 GND 实例放置计划。"""

    refdes: str
    x: int
    y: int
    rotation: int = 0
    mirror: int = 0
    net: str = "GND"
    label_x: int = 0
    label_y: int = 0

    def to_dict(self) -> dict:
        return {
            "refdes": self.refdes, "x": self.x, "y": self.y,
            "rotation": self.rotation, "mirror": self.mirror,
            "net": self.net, "label_x": self.label_x, "label_y": self.label_y,
        }


def build_gnd_placements(ir) -> list[GndPlacement]:
    """DesignIR → GND 放置计划（675 个，坐标直通）。

    Args:
        ir: DesignIR。

    Returns:
        GndPlacement 列表（按页序）。
    """
    plans: list[GndPlacement] = []
    for page in ir.pages:
        for inst in page.instances:
            if not (inst.refdes or "").startswith("GND_") and \
                    "GND_SYMBOL" not in (inst.library_id or "").upper():
                continue
            net = next(iter((inst.pin_connections or {}).values()), "GND")
            plans.append(GndPlacement(
                refdes=inst.refdes,
                x=inst.loc_x, y=inst.loc_y,
                rotation=inst.rotation, mirror=inst.mirror,
                net=net or "GND",
                label_x=inst.loc_x, label_y=inst.loc_y + 30,  # 网标上方
            ))
    return plans


def to_forceadd_lines(
    placements: list[GndPlacement], gnd_cell: str = "GND"
) -> list[str]:
    """放置计划 → FORCEADD 指令行（§9.3.2）。

    Args:
        placements: GND 放置计划。
        gnd_cell: hdl_lib GND 元件名（默认 GND）。

    Returns:
        ``FORCEADD GND <net> (x y) rot`` 指令行列表。
    """
    lines: list[str] = []
    for p in placements:
        mir = " M" if p.mirror else ""
        lines.append(
            f"FORCEADD {gnd_cell} {p.net} ({p.x} {p.y}) {int(p.rotation) % 360}{mir}"
        )
    return lines


def gnd_stats(ir) -> dict:
    """GND 统计（QA 用）：总数/网名分布/唯一性/坐标域。"""
    plans = build_gnd_placements(ir)
    nets: dict[str, int] = {}
    xs, ys = [], []
    refdes_set = set()
    for p in plans:
        nets[p.net] = nets.get(p.net, 0) + 1
        xs.append(p.x)
        ys.append(p.y)
        refdes_set.add(p.refdes)
    return {
        "total": len(plans),
        "unique_refdes": len(refdes_set),
        "nets": nets,
        "x_range": (min(xs), max(xs)) if xs else (0, 0),
        "y_range": (min(ys), max(ys)) if ys else (0, 0),
        "rotations": sorted({p.rotation for p in plans}),
    }


# ---------------------------------------------------------------------------
#  GND 实例级聚类聚合（mentor"大量 GND"修复，config.aggregate_instances）
# ---------------------------------------------------------------------------
#
# 默认（DSN 直通）675 个 GND_SYMBOL 原位还原 → 页面过密。聚合后每簇只
# 保留 1 个代表符号（仍在其**源坐标**原位），其余抑制发射 + 剪除其专属
# tap WIRE。电气不变：GND 属同一电源网，元件引脚 LASTPIN SIG_NAME GND\\g
# 与源 trunk WIRE 均保留；被抑制符号的网名成员资格由网名连接表达，无需
# 额外线。
#
# 与 _retired gnd_cluster_planner 的分工：本模块只做"源符号实例的簇计划
# + tap WIRE 剪除"（轻量、原位、纯函数）；簇内 hub 短接/绕障/密度补点
# 等生成式能力保留在 gnd_cluster_planner（conn 路径 Phase XV/XVII 用）。


def is_gnd_source_instance(inst) -> bool:
    """True = DSN 源 GND 电源符号实例（refdes GND_* 或库名 GND_SYMBOL）。

    Args:
        inst: PageIR 实例（ComponentInstanceIR 或兼容对象）。

    Returns:
        True 为源 GND 符号实例（Phase XV 合成符号 GND_SYM_B* 也会命中
        refdes 前缀，但 standalone 路径无合成符号，可安全忽略）。
    """
    refdes = str(getattr(inst, "refdes", "") or "")
    lib = str(getattr(inst, "library_id", "") or "")
    return refdes.startswith("GND_") or "GND_SYMBOL" in lib.upper()


@dataclass
class GndAggregationPlan:
    """页面 GND 符号实例的聚类聚合计划（纯数据）。

    Attributes:
        clusters: 每簇的 refdes 列表（按坐标排序的贪心最近邻）。
        keep_refdes: 每簇保留的代表符号 refdes（源坐标原位）。
        drop_refdes: 被抑制（不发射 FORCEADD）的符号 refdes。
        dropped_pins: 被抑制符号的候选引脚坐标（DSN 空间：loc 与
            loc + pin_offset；用于 tap WIRE 剪除判定）。
    """

    clusters: list[list[str]] = field(default_factory=list)
    keep_refdes: set[str] = field(default_factory=set)
    drop_refdes: set[str] = field(default_factory=set)
    dropped_pins: list[tuple[int, int]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        """True = 存在被抑制符号（需要聚合发射/剪线）。"""
        return bool(self.drop_refdes)


def _iter_inst_coord(inst) -> tuple[int, int]:
    """实例 → (loc_x, loc_y) 整数坐标（缺省 0）。"""
    return (
        int(getattr(inst, "loc_x", 0) or 0),
        int(getattr(inst, "loc_y", 0) or 0),
    )


def _coord_of(inst, coord_map) -> tuple[int, int]:
    """实例坐标：优先 coord_map（DEHDL 映射后），否则源 loc。

    Args:
        inst: 实例对象。
        coord_map: ``refdes → (x, y)`` 映射（可选；None 时用 loc）。

    Returns:
        聚类用坐标（整数）。
    """
    refdes = str(getattr(inst, "refdes", "") or "")
    if coord_map and refdes in coord_map:
        dx, dy = coord_map[refdes]
        return (int(dx), int(dy))
    return _iter_inst_coord(inst)


def plan_gnd_aggregation(
    instances,
    cluster_radius: int = 2000,
    pin_offsets: tuple[tuple[int, int], ...] = ((0, 0), (0, 50)),
    coord_map: dict | None = None,
) -> GndAggregationPlan:
    """把一页 GND 符号实例按 ``cluster_radius`` 空间网格聚簇并选代表。

    mentor"大量 GND"修复：源图每引脚一个 GND 符号（675 个）过密。本计划
    采用**空间网格装箱**（cell 边长 = cluster_radius）——确定性、无长链
    塌缩（若用贪心质心链式合并，2000 阈值会整页塌成 1 簇，重蹈旧版用户
    实测 B4"GND 一页就 1 个"）。每格保留 1 个代表符号（源坐标原位不动）。

    Args:
        instances: 页面实例列表（含 GND 与非 GND）。
        cluster_radius: 网格 cell 边长（**DEHDL 输出空间**语义；≤0 = 不
            聚合全部保留）。默认 2000 与 Phase XVII ``cluster_radius``
            同语义（页内相邻 GND 共享 1 个符号）。
        pin_offsets: 被抑制符号候选引脚相对 loc 的偏移（DSN 空间，用于
            tap WIRE 剪除）。默认 (0,0) 与 (0,50)（gnd_power symbol.css
            pin (0,50)；实测 DSN tap WIRE 端点落 loc 或 loc+50）。
        coord_map: ``refdes → DEHDL (x, y)`` 映射（可选）。提供时聚类在
            DEHDL 空间进行（网格半径语义与页面视觉一致）；缺省用源 loc
            （DSN 空间）。

    Returns:
        GndAggregationPlan（radius ≤ 0 或无可聚合 GND 时为空计划）。
    """
    plan = GndAggregationPlan()
    gnds: list[tuple[str, int, int, int, int]] = [
        (refdes, cx, cy, sx, sy)
        for i in instances
        if is_gnd_source_instance(i)
        for refdes, cx, cy, sx, sy in [
            (
                str(getattr(i, "refdes", "") or ""),
                *_coord_of(i, coord_map),
                *_iter_inst_coord(i),
            )
        ]
    ]
    if len(gnds) < 2 or cluster_radius <= 0:
        return plan
    cell = int(cluster_radius)
    bins: dict[tuple[int, int], list[tuple[str, int, int, int, int]]] = {}
    for refdes, cx, cy, sx, sy in gnds:
        bx, by = round(cx / cell), round(cy / cell)
        bins.setdefault((bx, by), []).append((refdes, cx, cy, sx, sy))
    for pts in bins.values():
        if len(pts) < 2:
            plan.clusters.append([pts[0][0]])
            plan.keep_refdes.add(pts[0][0])
            continue
        ctr_x = sum(p[1] for p in pts) / len(pts)
        ctr_y = sum(p[2] for p in pts) / len(pts)
        rep = min(pts, key=lambda p: (abs(p[1] - ctr_x) + abs(p[2] - ctr_y), p[0]))
        plan.clusters.append([p[0] for p in pts])
        plan.keep_refdes.add(rep[0])
        for refdes, _cx, _cy, sx, sy in pts:
            if refdes == rep[0]:
                continue
            plan.drop_refdes.add(refdes)
            # 被抑制符号候选引脚用 **DSN 源 loc**（tap WIRE 原始坐标空间）。
            for ox, oy in pin_offsets:
                plan.dropped_pins.append((sx + int(ox), sy + int(oy)))
    return plan


def _wire_points(w) -> list[tuple[int, int]]:
    """wire 对象 → 折线点列表（DSN 原始坐标，整数化）。"""
    pts = getattr(w, "points", None) or []
    if not pts:
        pts = [
            (getattr(w, "start_x", 0), getattr(w, "start_y", 0)),
            (getattr(w, "end_x", 0), getattr(w, "end_y", 0)),
        ]
    out: list[tuple[int, int]] = []
    for p in pts:
        try:
            out.append((int(p[0]), int(p[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def prune_gnd_tap_wires(
    wires,
    dropped_pins: list[tuple[int, int]],
    tolerance: int = 25,
) -> list:
    """剪除连接到被抑制 GND 符号的专属 tap WIRE（整对象删除）。

    只删"端点悬空（degree==1）且端点落被抑制符号候选引脚附近"的 wire：
    源图中每个 GND 符号通常由一条独立短 tap（trunk 结点 → 符号引脚）
    挂到 GND trunk 上；删符号后其 tap 若不删会形成悬空线头。trunk 上
    其它 wire 与 tap 在结点处共享端点（degree≥2）→ 不受影响。

    Args:
        wires: 页面 wire 对象列表（DSN 原始坐标空间）。
        dropped_pins: 被抑制符号的候选引脚坐标（DSN 空间）。
        tolerance: 坐标容差（默认 25 = 1 网格）。

    Returns:
        剪除后的 wire 对象列表（原对象引用，未改动）。
    """
    if not dropped_pins or not wires:
        return list(wires)
    from collections import Counter

    segs: list[list[tuple[tuple[int, int], tuple[int, int]]]] = []
    deg: Counter = Counter()
    for w in wires:
        pts = _wire_points(w)
        ws = [
            (pts[i], pts[i + 1])
            for i in range(len(pts) - 1)
            if pts[i] != pts[i + 1]
        ]
        for a, b in ws:
            deg[a] += 1
            deg[b] += 1
        segs.append(ws)

    def _near(pin: tuple[int, int], pt: tuple[int, int]) -> bool:
        return abs(pin[0] - pt[0]) <= tolerance and abs(pin[1] - pt[1]) <= tolerance

    keep: list = []
    for w, ws in zip(wires, segs):
        if not ws:
            keep.append(w)
            continue
        first = ws[0][0]
        last = ws[-1][1]
        drop = False
        for pin in dropped_pins:
            if (_near(pin, first) and deg[first] == 1) or (
                _near(pin, last) and deg[last] == 1
            ):
                drop = True
                break
        if not drop:
            keep.append(w)
    return keep

