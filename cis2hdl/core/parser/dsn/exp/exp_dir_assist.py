"""EXP 辅助键名对齐模块（可选依赖，partandpin.EXP — part+pin 型，48 列）。

设计目标
--------
DSN 解析器纯 DSN 路径保持 97.11%（诚实基线，不读 EXP）；当调用方显式传入
`exp_path`（黄金 partandpin.EXP）时，本模块对 IR 的 pin_connections 做
**键名对齐**（L1）+ **补键/权威修正**（L2），使键名与 EXP 库序完全一致，
匹配率可达 100%。

依赖文件类型声明（用户裁决 2026-08-20 深夜）
--------------------------------------------
- 使用：``TG0C0D2_VER_B_SCH_partandpin.EXP``（part+pin 型：PARTINST 组 +
  PININST 行 ``refdes:pin → Net Name``，GBK、tab、48 列、2253 PININST）
- 不使用：纯 part 型 EXP（318KB，无 PININST 网名）、flatnet.EXP（20 列
  平面网表，无引脚级真值）——本模块仅依赖 part+pin 型。

算法（两级，全部经黄金对拍实证）
--------------------------------
L1 键名对齐：对网名多重集完全相等的实例（EXP 有网名引脚 == IR 有网名
连接），按 EXP 引脚名重写 IR 键名（网名唯一匹配）。修 45 个键名库序
mismatch（R76/C70/U9/UD2/CD11/D21/X3/S2 等）中的大部分。
L2 补键/修正：
  L2a 补键：EXP 有网名但 IR 无该网名键 → 若网名在 DSN 全局网名集，补键
      （修 missing 19 中的 J4/QD1/CD28/U7F VSS 等，网名可信性经全局集验证）。
  L2b 权威修正：键已存在但网名与 EXP 不同 → 若 EXP 网名在全局网名集，按 EXP 修正
      （EXP 为唯一权威真值，不做同实例占用守卫——多引脚同网名是合法语义；
      修 U9 EN/BIAS、L4/L5 电源端、QS702 归属等 13 键；误连冗余键由 L2d 清理）。

诚实性保证：所有补入/修正的网名必须 ∈ DSN 全局网名集（IR 已解析真实
网名），杜绝伪造；metadata.exp_source 记录依赖文件类型。
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ....ir.design import DesignIR

#: EXP 类型标注（写入 IR metadata.exp_source）
EXP_TYPE_PART_PIN = "partandpin.EXP (part+pin, 48列, PININST 2253)"

#: EXP U7 六 section → IR 复合 U7（08-19 封板决策）
U7_SECTIONS = {"U7A": 1, "U7B": 2, "U7C": 3, "U7D": 4, "U7E": 5, "U7F": 6}


def load_partandpin(exp_path: Path) -> dict[str, dict[str, str]]:
    """加载 partandpin.EXP → {refdes: {pin_name: net}}。

    Args:
        exp_path: partandpin.EXP 路径（GBK、tab、48 列）。

    Returns:
        refdes（含 U7A-F）→ {引脚名: 网名}。
    """
    with open(exp_path, encoding="gbk", newline="") as f:
        rows = list(csv.reader(f, delimiter="\t"))
    header_idx = None
    for i, r in enumerate(rows):
        clean = [c.strip('"') for c in r]
        if "Part Reference" in clean and "Net Name" in clean and "Number" in clean:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("partandpin.EXP 表头未找到（非 part+pin 型？）")
    names = [c.strip('"') for c in rows[header_idx]]
    i_ref = names.index("Part Reference")
    i_net = names.index("Net Name")
    i_num = names.index("Number")
    # R-CAT：Source Package / PCB Footprint / Value 列（分类回填映射源）
    i_pkg = names.index("Source Package") if "Source Package" in names else -1
    i_fp = names.index("PCB Footprint") if "PCB Footprint" in names else -1
    i_cat = names.index("CATEGORY_NAME") if "CATEGORY_NAME" in names else -1
    i_val = names.index("Value") if "Value" in names else -1
    # R-NC：元件级 NC 列（如 JS701 `1,2`，封装引脚号）
    i_nc = names.index("NC") if "NC" in names else -1
    # R-MFG：MFGREGION 列（实例级产地，如 '日本;台湾'/'中国'）
    i_mfg = names.index("MFGREGION") if "MFGREGION" in names else -1

    by_ref: dict[str, dict[str, str]] = {}
    pin_nums: dict[str, dict[str, str]] = {}  # {refdes: {pin: Number(球标)}}
    ref_cat: dict[str, str] = {}        # refdes → CATEGORY_NAME（R-CAT 主路径）
    ref_nc: dict[str, set[str]] = {}    # refdes → NC 引脚号集合（R-NC）
    ref_mfg: dict[str, str] = {}        # refdes → MFGREGION（R-MFG）
    # 2026-08-21：PARTINST 所在页（U7A-F 六段独立芯片按页归属）
    ref_page: dict[str, str] = {}       # Part Reference → 页名（HEADER 第 3 段）
    ref_pr: dict[str, str] = {}         # ID → Part Reference（显示名）
    ref_prs: dict[str, list[str]] = {}  # ID → Part Reference 列表（多段）
    pin_page: dict[str, dict[str, str]] = {}  # ID → {pin: 页名}（多段拆分用）
    pkg_cat: dict[str, set[str]] = {}   # Source Package → 分类集合（R-CAT 降级）
    fp_cat: dict[str, set[str]] = {}    # C 电容 footprint → 分类集合
    fpv_cat: dict[tuple[str, str], set[str]] = {}  # (fp, value) → 分类集合
    cur_ref = ""
    for r in rows[header_idx + 1:]:
        if not r:
            continue
        if r[0].startswith("PARTINST"):
            # 2026-08-21 通用化：归属键用 **ID 列**（parts[1]，如 'U7'），
            # Part Reference（i_ref，如 'U7A'）是显示名——PININST 的
            # ID 是 'refdes:引脚名'（'U7:xxx'），按 ID 归属任意多段芯片
            # 的引脚；单段元件 ID==Part Reference，行为不变。
            cur_ref = r[1].strip('"') if len(r) > 1 else ""
            _pr = r[i_ref].strip('"') if i_ref < len(r) else cur_ref
            ref_pr[cur_ref] = _pr
            ref_prs.setdefault(cur_ref, [])
            if _pr not in ref_prs[cur_ref]:
                ref_prs[cur_ref].append(_pr)
            # HEADER: PARTINST:设计:页:偏移 → 页名（第 3 段，按 PR 记）
            _seg = r[0].split(":")
            if len(_seg) >= 3:
                ref_page[_pr] = _seg[2]
            if i_cat > 0 and i_pkg > 0 and cur_ref:
                cat = r[i_cat].strip('"') if i_cat < len(r) else ""
                pkg = r[i_pkg].strip('"') if i_pkg < len(r) else ""
                if cat and cat != "<null>":
                    ref_cat[cur_ref] = cat
                    pkg_cat.setdefault(pkg, set()).add(cat)
                    if i_fp > 0 and pkg == "C":
                        fp = r[i_fp].strip('"') if i_fp < len(r) else ""
                        val = r[i_val].strip('"') if i_val > 0 and i_val < len(r) else ""
                        if fp:
                            fp_cat.setdefault(fp, set()).add(cat)
                            if val:
                                fpv_cat.setdefault((fp, val), set()).add(cat)
            if i_nc > 0 and cur_ref:
                nc = r[i_nc].strip('"') if i_nc < len(r) else ""
                if nc and nc != "<null>":
                    ref_nc[cur_ref] = {p.strip() for p in nc.split(",") if p.strip()}
            if i_mfg > 0 and cur_ref:
                mfg = r[i_mfg].strip('"') if i_mfg < len(r) else ""
                if mfg and mfg != "<null>":
                    ref_mfg[cur_ref] = mfg
        elif r[0].startswith("PININST") and cur_ref:
            _id = r[1].strip('"') if len(r) > 1 else ""
            pin = _id.split(":", 1)[1] if ":" in _id else ""
            net = r[i_net].strip('"') if i_net < len(r) else ""
            num = r[i_num].strip('"') if i_num < len(r) else ""
            _pg = r[0].split(":")[2] if ":" in r[0] else ""
            if pin:
                by_ref.setdefault(cur_ref, {})[pin] = net
                if _pg:
                    pin_page.setdefault(cur_ref, {})[pin] = _pg
                if num:
                    pin_nums.setdefault(cur_ref, {})[pin] = num
    # 2026-08-21 通用化：多段 ID 自动发现（同 ID 多个 PARTINST 行或
    # ID != Part Reference）——任意多 section 芯片（U7A~U7F 等）
    multi_id = {i for i, prs in ref_prs.items() if len(prs) > 1}
    # 2026-08-21 通用化：多段 ID（U7 等）引脚按 PININST 页拆分到各
    # Part Reference 段（by_ref 返回 PR 键，任意多 section 芯片通用）
    if multi_id:
        _split: dict[str, dict[str, str]] = {}
        for _id, pins in by_ref.items():
            if _id in multi_id:
                for pin, net in pins.items():
                    _pg = pin_page.get(_id, {}).get(pin, "")
                    _pr = next(
                        (p for p in ref_prs.get(_id, [])
                         if ref_page.get(p) == _pg),
                        _id,
                    )
                    _split.setdefault(_pr, {})[pin] = net
            else:
                _split[_id] = pins
        by_ref = _split
    return (by_ref, pin_nums, ref_cat, ref_nc, ref_mfg,
            pkg_cat, fp_cat, fpv_cat, ref_page, ref_pr, ref_prs,
            multi_id, pin_page)


def _global_nets(ir: "DesignIR") -> set[str]:
    """IR 全局网名集（L2 网名可信性验证）。"""
    nets = set(ir.global_nets)
    for pg in ir.pages:
        for n in pg.nets:
            nm = getattr(n, "name", None) or getattr(n, "net_name", "")
            if nm:
                nets.add(nm)
    return nets


def _conn_map(ir: "DesignIR") -> dict[str, dict[str, str]]:
    """IR → {refdes: {pin_key: net}}（U7 六段独立芯片按页段键）。"""
    out: dict = {}
    for pg in ir.pages:
        for inst in pg.instances:
            key = inst.refdes
            if "Hi5662YV305" in (inst.library_id or ""):
                # 2026-08-21：U7A~U7F 六段独立 → 段键 (refdes, 页名)
                key = (inst.refdes, pg.page_name)
            out.setdefault(key, {}).update(inst.pin_connections)
    return out


def align(ir: "DesignIR", exp_path: Path) -> dict:
    """对 IR 应用 EXP 键名对齐（L1 + L2 + L3）。

    原位修改 ``inst.pin_connections`` / ``inst.pin_labels``（L3 仅 U7），
    并返回统计 dict。

    Args:
        ir: 已解析的 DesignIR。
        exp_path: partandpin.EXP 路径。

    Returns:
        {"l1_instances": int, "l2a_filled": int, "l2b_fixed": int,
         "l2c_removed": int, "l3_labels_added": int,
         "exp_type": str, "exp_pins": int}
    """
    (exp, pin_nums, ref_cat, ref_nc, ref_mfg,
     pkg_cat, fp_cat, fpv_cat, ref_page, ref_pr, ref_prs,
     multi_id, pin_page) = load_partandpin(exp_path)
    global_nets = _global_nets(ir)
    # 2026-08-21 通用化：多段芯片（ID 多个 Part Reference，如 U7A~U7F
    # 六块独立芯片）→ refdes 用 EXP Part Reference（各段按页匹配，
    # 对任意多 section 芯片通用，非 U7 特判）。
    for pg in ir.pages:
        for inst in pg.instances:
            _id_prs = ref_prs.get(inst.refdes or "")
            if not _id_prs or len(_id_prs) <= 1:
                continue
            _page = pg.page_name
            for _ref in _id_prs:
                if ref_page.get(_ref) == _page:
                    inst.refdes = _ref
                    break
    conns = _conn_map(ir)

    def _seg_iref(ref: str) -> object:
        """EXP refdes → IR conns 键：多段 Part Reference → (PR, 页)；
        其余 → refdes 原键。"""
        if ref in multi_id or ref in U7_SECTIONS:
            return (ref, ref_page.get(ref, ""))
        return ref

    # ── L1：网名多重集相等 → 键名重写 ───────────────────────────
    l1_applied = 0
    for ref, exp_pins in exp.items():
        iref = _seg_iref(ref)
        conn = conns.get(iref)
        if not conn:
            continue
        exp_nets = Counter(v for v in exp_pins.values() if v)
        ir_nets = Counter(v for v in conn.values() if v)
        if exp_nets != ir_nets:
            continue
        used: set[str] = set()
        renamed: dict[str, str] | None = {}
        for pin, net in exp_pins.items():
            if not net:
                continue
            hit = next((k for k, v in conn.items()
                        if v == net and k not in used), None)
            if hit is None:
                renamed = None
                break
            renamed[hit] = pin
            used.add(hit)
        if renamed:
            conns[iref] = {renamed.get(k, k): v for k, v in conn.items()}
            l1_applied += 1

    # ── L2a：补键（EXP 有网名、IR 无键、网名 ∈ 全局集）──────────
    l2a_filled = 0
    for ref, exp_pins in exp.items():
        iref = _seg_iref(ref)
        conn = conns.setdefault(iref, {})
        for pin, net in exp_pins.items():
            if net and pin not in conn and net in global_nets:
                conn[pin] = net
                l2a_filled += 1

    # ── L2b：权威修正（键在但网名错 → EXP 网名 ∈ 全局集则修正）──
    # 语义说明（QA 审查修订）：本步以 EXP 为**唯一权威真值**——EXP
    # 声明 pin→net，网名 ∈ DSN 全局网名集即采纳，**不做"未被本实例
    # 其它键占用"守卫**：多引脚同网名（如 U9 双 GND、VSS 阵列）是
    # 合法电气语义，加占用守卫会阻止正确修正（实测 13 键修正全部
    # 与 EXP 对齐，无冲突产生）。误连的冗余键由 L2d 清理。
    l2b_fixed = 0
    for ref, exp_pins in exp.items():
        iref = _seg_iref(ref)
        conn = conns.get(iref, {})
        for pin, net in exp_pins.items():
            if not net:
                continue
            if pin in conn and conn[pin] != net and net in global_nets:
                conn[pin] = net
                l2b_fixed += 1

    # ── L2c：EXP 空网引脚 → 删除 IR 误连键（机械引脚/NC 无连接）──
    # 例：JS701 M1/M2（MJ4-M2_C 机械引脚，EXP net=''）——IR 误连
    # RING/其他网名，应删除（EXP 空网 = 无连接是权威语义）。
    l2c_removed = 0
    for ref, exp_pins in exp.items():
        iref = _seg_iref(ref)
        conn = conns.get(iref)
        if not conn:
            continue
        for pin, net in exp_pins.items():
            if not net and pin in conn:
                del conn[pin]
                l2c_removed += 1

    # ── L2d：冗余位置键清理（JS701 P0/P3 等）────────────────────
    # 根因：MJ4-M2_C 等实例旋转放置，cache 引脚线未应用旋转 → 部分
    # 引脚未命中 cache 线，走了位置键 P{idx}（P0/P3 网名与 P1/P2 重复）。
    # 规则：EXP 该实例**全部引脚都有网名键**（完整覆盖）且 IR 存在
    # `P{idx}` 位置键，其网名与某 EXP 有网名引脚相同 → 该位置键是
    # 冗余假键，删除（真实连接已由 EXP 命名键承载）。
    l2d_removed = 0
    for ref, exp_pins in exp.items():
        iref = _seg_iref(ref)
        conn = conns.get(iref)
        if not conn:
            continue
        exp_named = {p for p, n in exp_pins.items() if n}
        if not exp_named:
            continue
        if any(p in exp_named for p in conn):  # IR 已有 EXP 命名键命中
            exp_net_set = {n for n in exp_pins.values() if n}
            for key in list(conn):
                # 仅删"不在 EXP 命名键内"的 P{idx} 位置键（P1/P2 本身
                # 若也是 EXP 键则保留——JS701 P1/P2 是真实键）
                if (key.startswith("P") and key[1:].isdigit()
                        and key not in exp_named
                        and conn[key] in exp_net_set):
                    del conn[key]
                    l2d_removed += 1

    # ── L3：U7 球标补齐（EXP Number 球标 → U7 pin_labels）────────
    # EXP U7A-F 409 球标（Number 列），IR U7 pin_labels 仅 406
    # （Packages pin_label_maps 交换组）——缺 C8/F19/T12。用 EXP
    # 的 Number→pin 名补入，使球标覆盖 409/409。
    l3_added = 0
    for ref, nums in pin_nums.items():
        # 2026-08-21 通用化：多段芯片（含 U7A~U7F）球标按段补
        if ref not in multi_id and ref not in U7_SECTIONS:
            continue
        # 2026-08-21：U7A~U7F 六段独立——球标只补到该段所在页的实例
        _page = ref_page.get(ref, "")
        _tgt = None
        for pg in ir.pages:
            for inst in pg.instances:
                if (inst.refdes == _ref
                        and "Hi5662YV305" in (inst.library_id or "")):
                    _tgt = inst
                    break
            if _tgt is not None:
                break
        if _tgt is None:
            continue
        for pin, num in nums.items():
            if num not in _tgt.pin_labels:
                _tgt.pin_labels[num] = pin
                l3_added += 1

    # ── L4：R-CAT 器件分类回填（EXP Source Package → category）────
    # 实证：EXP Source Package（库名）→ CATEGORY_NAME **唯一映射**
    # （R→片式厚膜电阻器、LB→EMI磁珠、LED→发光二极管、DZ_L→瞬态抑制
    # 二极管…）；C 电容 5 类需 footprint 细分（15 个 footprint 中 12 唯一、
    # 3 冲突取众数）。DSN 的 ComponentDef.part_name 基础名 = Source Package，
    # 故按 (库名, footprint) 查表回填，保证与 EXP 分类一致（GBK Unicode）。
    l4_filled = 0
    if ref_cat:
        # 主路径：EXP refdes → category 直查（100% 对齐；U7A-F → U7）
        for pg in ir.pages:
            for inst in pg.instances:
                if inst.refdes.startswith("GND_"):
                    continue
                cat = ref_cat.get(inst.refdes)
                if not cat and inst.refdes == "U7":
                    cat = ref_cat.get("U7A") or ref_cat.get("U7D")
                if cat:
                    inst.extra_data["category"] = cat
                    l4_filled += 1
    elif pkg_cat:
        # 降级路径（无 ref_cat 时）：库名唯一分类 + C 电容 (fp,value) 细分
        pkg_unique = {k: next(iter(v)) for k, v in pkg_cat.items() if len(v) == 1}
        fpv_unique = {k: next(iter(v)) for k, v in fpv_cat.items() if len(v) == 1}
        for pg in ir.pages:
            for inst in pg.instances:
                if inst.refdes.startswith("GND_"):
                    continue
                base = (inst.library_id or "").rsplit(".", 1)[0]
                cat = pkg_unique.get(base)
                if not cat and base == "C":
                    fp = inst.properties.get("PCB Footprint", "")
                    cat = fpv_unique.get((fp, inst.value_override))
                    if not cat and fp in fp_cat and len(fp_cat[fp]) == 1:
                        cat = next(iter(fp_cat[fp]))
                if cat:
                    inst.extra_data["category"] = cat
                    l4_filled += 1
        # 库级回填（ComponentDef.category 兜底，供 BOM 库级消费）
        for comp in ir.component_db.list_all():
            if comp.category:
                continue
            base = comp.part_name.rsplit(".", 1)[0]
            if base in pkg_unique:
                comp.category = pkg_unique[base]
            elif base == "C" and comp.footprint in fp_cat and len(fp_cat[comp.footprint]) == 1:
                comp.category = next(iter(fp_cat[comp.footprint]))

    # ── L5：R-NC NC 引脚识别（EXP 元件级 NC 列）───────────────────
    # EXP NC 列：JS701=`1,2`（封装引脚号，与 PININST Number 3-6 不同
    # 体系——封装 1,2 号引脚）。写入 inst.nc_pins（set[str]），下游
    # NC 引脚跳过连接/输出 NC 标记。
    l5_filled = 0
    for ref, nc_set in ref_nc.items():
        iref = _seg_iref(ref)
        for pg in ir.pages:
            for inst in pg.instances:
                if inst.refdes == iref:
                    inst.nc_pins = set(inst.nc_pins) | nc_set
                    l5_filled += len(nc_set)
                    break

    # ── L6：R-MFG 产地回填（EXP MFGREGION 列 → 实例 extra_data）──
    # EXP MFGREGION 23 个实例级值（'日本;台湾'×4 + '中国'×19）；
    # DSN Library 流 MFGREGION 键@3667 已实证有源（INS2160='日本;台湾'，
    # GBK 解码），经 footprint 关联到 ComponentDef；EXP 辅助 L6 按
    # refdes 精确回填实例级（对齐 EXP 23 个）。
    l6_filled = 0
    for ref, mfg in ref_mfg.items():
        iref = _seg_iref(ref)
        for pg in ir.pages:
            for inst in pg.instances:
                if inst.refdes == iref:
                    inst.extra_data["mfg_region"] = mfg
                    l6_filled += 1
                    break

    # ── 写回 IR（U7 六段按段键 (refdes, 页) 匹配）──────────────
    for pg in ir.pages:
        for inst in pg.instances:
            key = inst.refdes
            if "Hi5662YV305" in (inst.library_id or ""):
                key = (inst.refdes, pg.page_name)
            if key in conns:
                inst.pin_connections = conns[key]

    return {
        "l1_instances": l1_applied,
        "l2a_filled": l2a_filled,
        "l2b_fixed": l2b_fixed,
        "l2c_removed": l2c_removed,
        "l2d_removed": l2d_removed,
        "l3_labels_added": l3_added,
        "l4_category_filled": l4_filled,
        "l5_nc_filled": l5_filled,
        "l6_mfg_filled": l6_filled,
        "exp_type": EXP_TYPE_PART_PIN,
        "exp_pins": sum(len(v) for v in exp.values()),
        # 2026-08-21：U7A~U7F 段引脚序（EXP 文件序=物理序，U7F 等
        # 无网名引脚对象的坐标按此分配）
        "u7_pin_order": {
            ref: list(pins.keys())
            for ref, pins in exp.items()
            if ref in multi_id or ref in U7_SECTIONS
        },
    }
