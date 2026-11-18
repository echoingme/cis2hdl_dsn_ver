"""NetNameConnect — M5 跨页网网络名连接（Phase XVII，用户 D2）。

用户 D2：**IOPORT→网络名，同步去除，con 层也可以去除**（CSA + con 都改
网络名表达）。本模块提供跨页网判定 + IOPORT 跳过计划 + 网络名标签补发射：

* ``cross_page_bare_names`` —— 设计级网在 >1 页出现的 bare 名集合
  （数据源 = DesignConnectivity 模型，数据源铁律）；
* ``ioport_skip_plan``     —— ``ioport.use_net_name=true`` 时全部 IOPORT
  符号不生成（跨页连接由同名 SIG_NAME 网络名表达）；
* ``net_name_labels``      —— 跨页网若本页无 source-pin 标签，补一条
  线上 SIG_NAME（``_sig_name_on_wire`` 同格式）；
* ``net_name_endpoints``   —— **Phase XXII D3 主接线点（Q3 单一调用点）**：
  路由完成后对跨页网 WIRE 悬空端补 SIG_NAME；非跨页补全由 csa_writer
  泛化 has_label 循环承担（去重：同网不双标签）。``net_name_labels``
  保留（向后兼容 + 单测引用），但 csa_writer use_net_name 分支不再调用。

con/xcon/cpm 评估：con 层本就没有 IOPORT 概念（跨页连接 = 设计级网
scope=2 + alias），天然是"网络名"表达；``use_net_name`` 只关 CSA 的
IOPORT 符号发射，不碰 con/xcon/cpm 结构（避免冗余开关）。

配置开关：``ioport.use_net_name``（默认 false，保留 IOPORT 符号；
true 时改为网络名表达）。
"""

from __future__ import annotations

import logging
from typing import Iterable

logger = logging.getLogger(__name__)


def cross_page_bare_names(conn) -> set[str]:
    """设计级网中出现在 >1 页的 bare 名集合。

    Args:
        conn: DesignConnectivity（NetRecord.pages 为页号列表）。

    Returns:
        ``{"NET_A", "12V0", ...}`` —— 跨页网 bare 名（小写）。
    """
    names: set[str] = set()
    for net in getattr(conn, "nets", []) or []:
        pages = getattr(net, "pages", None)
        if pages is None:
            continue
        if len(pages) > 1 and getattr(net, "bare_name", ""):
            names.add(str(net.bare_name).lower())
    return names


def ioport_skip_plan(
    off_pages: Iterable[dict],
    use_net_name: bool,
) -> list[dict]:
    """决定哪些 off_page 条目不生成 IOPORT 符号。

    Args:
        off_pages: 页级 off_page 列表（``{"name", "net_name"}``）。
        use_net_name: ``ioport.use_net_name``（用户 D2）。

    Returns:
        应**跳过**的 off_page 条目列表（``use_net_name=true`` 时全部）。
    """
    off = list(off_pages or [])
    if not use_net_name:
        return []
    return off


def net_name_labels(
    net_pin_map: dict[str, list],
    source_pin_keys: set[str],
    cross_page: set[str],
    use_net_name: bool,
) -> list[tuple[tuple[int, int], str]]:
    """跨页网缺失 source-pin 标签时，补线上 SIG_NAME。

    Args:
        net_pin_map: 网显示名 → 引脚列表。
        source_pin_keys: ``refdes.pin`` source-pin 键集合。
        cross_page: ``cross_page_bare_names`` 输出（小写 bare 名）。
        use_net_name: 用户 D2 开关。

    Returns:
        ``[(coord, net_display)]`` —— 需要 ``_sig_name_on_wire`` 的条目。
    """
    if not use_net_name:
        return []
    out: list[tuple[tuple[int, int], str]] = []
    for net_display, pins in (net_pin_map or {}).items():
        bare = str(net_display).lower().replace("\\g", "")
        if bare not in cross_page:
            continue
        has_label = any(
            (str(p.get("refdes", "")), str(p.get("pin", ""))) in source_pin_keys
            for p in pins if isinstance(p, dict)
        )
        if has_label or not pins:
            continue
        coord = tuple(pins[0]["coord"])
        out.append((coord, net_display))
    return out


def ioport_net_mapping(off_pages: Iterable[dict]) -> dict[str, str]:
    """off_page 条目 → 网络名映射（``{"OFFPAGE_0": "NET_A"}``）。

    供审计/报告：IOPORT 符号与网络名的对应关系。

    Args:
        off_pages: 页级 off_page 列表。

    Returns:
        ``{name: net_name}``。
    """
    mapping: dict[str, str] = {}
    for op in off_pages or []:
        name = str(op.get("name", "") or "")
        net_name = str(op.get("net_name", "") or name)
        if name:
            mapping[name] = net_name
    return mapping


def net_name_endpoints(
    net_pin_map: dict[str, list],
    wire_segments: dict[str, list[tuple[int, int, int, int]]],
    cross_page: set[str],
    use_net_name: bool,
) -> list[tuple[tuple[int, int], str]]:
    """网络名标签落到**电线末端/悬空端**（Phase XVIII R7）。

    用户实测 B5：use_net_name 版本中，跨页网电线延伸到原 IOPORT 位置
    后**悬空**且无网络名标签，信号去向不可知。本函数对每个网（Phase
    XXVI 起推广到**所有网**，不只跨页网），找出其 WIRE 段中**不与任何
    引脚重合**的开放端点（悬空端），在该处补 SIG_NAME 网络名标签。

    Phase XXVI（F6/G，S11 视觉审查 #3/#25）：移除 ``cross_page`` 过滤
    —— 页内网悬空端同样补标签（原实现只在跨页网生效，非跨页悬空端
    无标签 → 线头）。``use_net_name=false`` 仍返回空（调用方在
    csa_writer 按开关决定是否启用；默认保持现状 → FR9 零影响）。

    Args:
        net_pin_map: 网显示名 → 引脚列表（引脚坐标集）。
        wire_segments: 网显示名 → WIRE 段列表（(x1,y1,x2,y2)）。
        cross_page: 跨页网 bare 名集合（小写；保留兼容，不再过滤）。
        use_net_name: ``ioport.use_net_name``（仅该模式启用悬空端补标签）。

    Returns:
        ``[(coord, net_display)]`` —— 悬空端坐标 + 网络名。
    """
    if not use_net_name:
        return []
    out: list[tuple[tuple[int, int], str]] = []
    for net_display, segs in (wire_segments or {}).items():
        pins = net_pin_map.get(net_display, []) or []
        pin_coords = {
            (int(p["coord"][0]), int(p["coord"][1]))
            for p in pins if isinstance(p, dict) and p.get("coord")
        }
        # 收集悬空端点：段端点不与任何引脚重合，且不是 ≥2 段的合法交点
        # （T 型/角接）—— 只有"1 段端点"才是真悬空（线头）。
        dangling: list[tuple[int, int]] = []
        _incident: dict[tuple[int, int], int] = {}
        for s in segs:
            for ep in ((s[0], s[1]), (s[2], s[3])):
                _incident[ep] = _incident.get(ep, 0) + 1
        for s in segs:
            for ep in ((s[0], s[1]), (s[2], s[3])):
                if ep in pin_coords or _incident.get(ep, 0) >= 2:
                    continue
                if ep not in dangling:
                    dangling.append(ep)
        if not dangling:
            continue
        # 取第一个悬空端（距引脚最远者优先，更可能是"尽头"）。
        def _far(ep: tuple[int, int]) -> float:
            if not pin_coords:
                return 0.0
            return max(
                ((ep[0] - px) ** 2 + (ep[1] - py) ** 2) ** 0.5
                for (px, py) in pin_coords
            )

        dangling.sort(key=_far, reverse=True)
        out.append((dangling[0], net_display))
    return out


def _cluster_islands(
    pin_coords: Iterable[tuple[int, int]], max_dist: int,
) -> list[list[tuple[int, int]]]:
    """按曼哈顿距离把引脚聚成"岛"（Phase XXVI F6，SKiDL auto_stub 范式）。

    完全连接（complete-linkage）：每个引脚归入某岛当且仅当它与岛内
    **所有**引脚曼哈顿距离 ≤ ``max_dist``（"岛内引脚互相 ≤ max_dist"）
    —— 防止贪心链式合并（0→100→2000 逐步 1900 距离被并进一岛）。
    岛内引脚互相靠近（stub→本地 trunk 可达），岛间由长 trunk 连接
    （距离超阈值 → 删中间段、两端各放网络名标签）。

    Args:
        pin_coords: 网引脚坐标集合。
        max_dist: 岛内最大曼哈顿距离（参考 SKiDL ``auto_stub_max_wire_dist``
            默认 2000）。

    Returns:
        岛列表（每个岛 = 引脚坐标列表）。
    """
    islands: list[list[tuple[int, int]]] = []
    for p in pin_coords:
        placed = False
        for isl in islands:
            if all(abs(p[0] - q[0]) + abs(p[1] - q[1]) <= max_dist for q in isl):
                isl.append(p)
                placed = True
                break
        if not placed:
            islands.append([p])
    return islands


def _nearest_island(
    point: tuple[int, int],
    islands: list[list[tuple[int, int]]],
) -> int:
    """返回距 ``point`` 最近的岛下标（曼哈顿距离）。"""
    best = 0
    best_d = None
    for idx, isl in enumerate(islands):
        d = min(abs(point[0] - q[0]) + abs(point[1] - q[1]) for q in isl)
        if best_d is None or d < best_d:
            best_d = d
            best = idx
    return best


def net_name_both_ends(
    net_pin_map: dict[str, list],
    wire_segments: dict[str, list[tuple[int, int, int, int]]],
    cross_page: set[str] | None = None,
    use_net_name: bool = False,
    max_dist: int = 2000,
    break_long: bool = False,
) -> tuple[list[tuple[tuple[int, int], str]], list[tuple[int, int, int, int]]]:
    """长网"两端各 1 个网络名 + 删中间段"（Phase XXVI F6，用户裁决 V4）。

    SKiDL ``auto_stub`` 范式：引脚间距超阈值（或 ``break_long`` 开启）的
    网，按曼哈顿距离聚类成"岛"；岛间由长 trunk 连接 → **删除连接岛的
    trunk 中间段**，在最远两端岛的悬空端各放 1 个 SIG_NAME（DEHDL 同名
    即连接 —— 删线后网络仍电气连通，需配合 ``assert_net_connectivity``
    验证）。

    触发条件：网被分为 ≥2 岛 **且**（岛间最小曼哈顿距离 > max_dist 或
    ``break_long`` 开启）。仅 debug（``wire_simplify.break_long=true``
    或新开关）启用 —— 默认零影响（FR9）。

    Args:
        net_pin_map: 网显示名 → 引脚列表。
        wire_segments: 网显示名 → WIRE 段列表（(x1,y1,x2,y2)）。
        cross_page: 保留兼容参数（不再使用）。
        use_net_name: 保留兼容参数（调用方已按开关判定）。
        max_dist: 岛内最大曼哈顿距离（默认 2000，对齐 SKiDL）。
        break_long: 强制双端标签（``wire_simplify.break_long``）。

    Returns:
        ``(labels, wires_to_delete)`` —— ``labels`` 为两端 SIG_NAME
        ``[(coord, net_display)]``（共 2 个/网）；``wires_to_delete`` 为
        连接岛的中间段（调用方发射 WIRE 时跳过）。
    """
    labels: list[tuple[tuple[int, int], str]] = []
    wires_to_delete: list[tuple[int, int, int, int]] = []
    for net_display, segs in (wire_segments or {}).items():
        pins = net_pin_map.get(net_display, []) or []
        pin_coords = {
            (int(p["coord"][0]), int(p["coord"][1]))
            for p in pins if isinstance(p, dict) and p.get("coord")
        }
        if len(pin_coords) < 2 or not segs:
            continue
        islands = _cluster_islands(pin_coords, max_dist)
        if len(islands) < 2:
            continue
        # 岛间最小曼哈顿距离 > max_dist → 触发双端；break_long 强制触发。
        min_island_dist = None
        for i in range(len(islands)):
            for j in range(i + 1, len(islands)):
                d = min(
                    abs(a[0] - b[0]) + abs(a[1] - b[1])
                    for a in islands[i] for b in islands[j]
                )
                if min_island_dist is None or d < min_island_dist:
                    min_island_dist = d
        if not break_long and (min_island_dist is None or min_island_dist <= max_dist):
            continue
        # 桥段判定：段两端最近岛不同 → 连接岛 → 删除。
        deleted: list[tuple[int, int, int, int]] = []
        for s in segs:
            ia = _nearest_island((s[0], s[1]), islands)
            ib = _nearest_island((s[2], s[3]), islands)
            if ia != ib:
                deleted.append(tuple(s))
        if not deleted:
            continue
        # 删除后各岛保留段；悬空端 = 保留段端点曾连接被删段者。
        deleted_set = {tuple(d) for d in deleted}
        remaining = [
            tuple(s) for s in segs if tuple(s) not in deleted_set
        ]
        _incident: dict[tuple[int, int], int] = {}
        for s in remaining:
            for ep in ((s[0], s[1]), (s[2], s[3])):
                _incident[ep] = _incident.get(ep, 0) + 1
        dangling_by_island: dict[int, list[tuple[int, int]]] = {}
        for s in remaining:
            for ep in ((s[0], s[1]), (s[2], s[3])):
                if ep in pin_coords or _incident.get(ep, 0) >= 2:
                    continue
                idx = _nearest_island(ep, islands)
                dangling_by_island.setdefault(idx, []).append(ep)
        # 最远两端岛各放 1 个标签。
        if len(islands) >= 2:
            for idx in (0, len(islands) - 1):
                eps = dangling_by_island.get(idx) or []
                if not eps:
                    # 无悬空端 → 用该岛最外侧引脚坐标（兜底）。
                    eps = [max(
                        islands[idx],
                        key=lambda p: abs(p[0]) + abs(p[1]),
                    )]
                labels.append((eps[0], net_display))
            wires_to_delete.extend(deleted)
    return labels, wires_to_delete


def assert_net_connectivity(
    net_pin_map: dict[str, list],
    wires: dict[str, list[tuple[int, int, int, int]]],
    labels: list[tuple[tuple[int, int], str]],
    max_dist: int = 2000,
) -> tuple[bool, list[str]]:
    """连通性断言（Phase XXVI F6，用户裁决 V4"删线需连通性测试"）。

    对每网验证：
    ① **岛内 BFS 可达**：按曼哈顿距离聚类成岛（同 ``net_name_both_ends``
       口径）；每岛内全部引脚经 WIRE 互相可达，且该岛标签端点可达
       （删中间段后每岛内部仍连通 —— 断网即失败）；
    ② **标签落在本网 WIRE 端点/引脚上**：标签坐标必须 ∈ 该网 WIRE 端点
       ∪ 引脚坐标（DEHDL 同名即连接 —— 标签若挂在别的网名上会造成短路）；
    ③ **无未标签悬空端**：每个 WIRE 端点 ∈ {引脚, 标签, ≥2 段交点}
       （删段后不允许出现新的裸悬空端）。

    Args:
        net_pin_map: 网显示名 → 引脚列表。
        wires: 网显示名 → WIRE 段列表（删段后的最终段）。
        labels: ``[(coord, net_display)]`` 网络名标签列表。
        max_dist: 岛聚类最大曼哈顿距离（与 ``net_name_both_ends`` 同口径）。

    Returns:
        ``(ok, violations)`` —— ok=False 时 violations 列出具体问题。
    """
    violations: list[str] = []

    # ② 每个标签必须落在其网 WIRE 端点/引脚上。
    for coord, net in labels:
        eps = {
            ep for s in (wires.get(net, []) or [])
            for ep in ((s[0], s[1]), (s[2], s[3]))
        }
        pinset = {
            (int(p["coord"][0]), int(p["coord"][1]))
            for p in (net_pin_map.get(net, []) or [])
            if isinstance(p, dict) and p.get("coord")
        }
        if tuple(coord) not in eps and tuple(coord) not in pinset:
            violations.append(
                f"{net}: label {tuple(coord)} not on net wire/pin"
            )

    for net_display, segs in (wires or {}).items():
        pins = net_pin_map.get(net_display, []) or []
        pin_coords = [
            (int(p["coord"][0]), int(p["coord"][1]))
            for p in pins if isinstance(p, dict) and p.get("coord")
        ]
        seg_list = list(segs or [])
        adj: dict[tuple[int, int], list[tuple[int, int]]] = {}
        for s in seg_list:
            a, b = (s[0], s[1]), (s[2], s[3])
            adj.setdefault(a, []).append(b)
            adj.setdefault(b, []).append(a)
        # 无段但多引脚 → 断网。
        if len(pin_coords) >= 2 and not seg_list:
            violations.append(
                f"{net_display}: pins {len(pin_coords)} but 0 wires"
            )
            continue
        # ③ 无未标签悬空端（端点 ∈ {引脚, 标签, ≥2 段交点}）。
        endpoints = {ep for s in seg_list for ep in ((s[0], s[1]), (s[2], s[3]))}
        label_coords = {
            tuple(c) for c, n in labels if n == net_display
        }
        incident: dict[tuple[int, int], int] = {}
        for s in seg_list:
            for ep in ((s[0], s[1]), (s[2], s[3])):
                incident[ep] = incident.get(ep, 0) + 1
        for ep in sorted(endpoints):
            if ep in pin_coords or ep in label_coords:
                continue
            if incident.get(ep, 0) >= 2:
                continue  # T 型/角接合法交点
            violations.append(
                f"{net_display}: dangling end {ep} without label"
            )
        # ① 岛内 BFS 可达（含岛标签端点）。
        if len(pin_coords) >= 2:
            islands = _cluster_islands(pin_coords, max_dist)
            for isl_idx, isl in enumerate(islands):
                reachable = _bfs_reachable(adj, isl[0])
                for p in isl[1:]:
                    if p not in reachable:
                        violations.append(
                            f"{net_display}: island{isl_idx} pin {p} "
                            f"not reachable from {isl[0]}"
                        )
                # 该岛标签端点可达。
                for c in label_coords:
                    if _nearest_island(c, islands) == isl_idx and c not in reachable:
                        violations.append(
                            f"{net_display}: island{isl_idx} label {c} "
                            f"not reachable"
                        )
            # 完全无 WIRE 的岛（该岛所有 stub 被删）→ 引脚断连。
            for isl_idx, isl in enumerate(islands):
                if not any(p in endpoints for p in isl):
                    violations.append(
                        f"{net_display}: island{isl_idx} pins {isl} "
                        f"have no wires"
                    )
    return (len(violations) == 0, violations)


def _bfs_reachable(
    adj: dict[tuple[int, int], list[tuple[int, int]]],
    start: tuple[int, int],
) -> set[tuple[int, int]]:
    """BFS 从 ``start`` 出发的可达端点集合（不含零段自环）。"""
    seen: set[tuple[int, int]] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in adj.get(cur, []):
            if nxt not in seen:
                stack.append(nxt)
    return seen
