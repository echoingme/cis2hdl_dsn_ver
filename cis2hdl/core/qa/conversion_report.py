"""转换报告 + 模拟图标清单（C 类验证产物，复用 report 生成思想微调）。

输出两份报告（``output/`` 下）：

1. ``conversion_report.md``——转换结果总览：
   - **100% 转换成功清单**（R1 精确命中 conf≥0.99 + R5 GND 原位放置 +
     R6 连接符）；
   - **未 100% 匹配清单及程度**（R2 近似 conf∈[0.75,0.99) 附置信度 /
     R3 未命中 → mock 复刻 / R4 非电气忽略）；
   - 汇总统计（总实例/各类占比/整体匹配率）。

2. ``mock_manifest.md``——模拟图标清单（R3 mock cell）：
   - 每个 mock cell：对应元件（refdes/库 id/类别）、位置（loc_x/y/
     page）、继承的电气信息（pin_connections 数/网名、pin_labels、
     nc_pins、category、mfg_region）与几何来源（symbol_geometry）。

数据源：``result_router.RoutedPlan`` + source_builder 源 ComponentDef
（extra_data 已含实例上下文）+ MatchResult（confidence/target）。
纯函数 + 薄类，不触碰 writer 内部。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class MockEntry:
    """一个 mock cell 的清单条目。"""

    refdes: str
    cell_name: str
    library_id: str
    category: str = ""
    page_id: str = ""
    loc_x: int = 0
    loc_y: int = 0
    rotation: int = 0
    pin_count: int = 0
    connections: dict = field(default_factory=dict)
    labels: dict = field(default_factory=dict)
    nc_pins: set = field(default_factory=set)
    mfg_region: str = ""
    has_geometry: bool = False

    def to_dict(self) -> dict:
        return {
            "refdes": self.refdes, "cell_name": self.cell_name,
            "library_id": self.library_id, "category": self.category,
            "page_id": self.page_id, "loc_x": self.loc_x, "loc_y": self.loc_y,
            "rotation": self.rotation, "pin_count": self.pin_count,
            "net_count": len(self.connections),
            "connections": dict(self.connections),
            "labels": dict(self.labels),
            "nc_pins": sorted(self.nc_pins),
            "mfg_region": self.mfg_region,
            "has_geometry": self.has_geometry,
        }


def _mock_entries(plan) -> list[MockEntry]:
    """R3 未命中源 → MockEntry 清单（含继承电气/引脚信息）。"""
    entries: list[MockEntry] = []
    for src, _res in plan.unmatched:
        ed = src.extra_data or {}
        geo = ed.get("symbol_geometry") or {}
        entries.append(MockEntry(
            refdes=ed.get("refdes", "") or src.refdes or "",
            cell_name=f"PH_{ed.get('refdes', 'X')}".upper(),
            library_id=src.library_id,
            category=ed.get("category", "") or src.category or "",
            page_id=ed.get("page_id", ""),
            loc_x=ed.get("loc_x", 0), loc_y=ed.get("loc_y", 0),
            rotation=ed.get("rotation", 0),
            pin_count=len(ed.get("pin_connections") or {}),
            connections=dict(ed.get("pin_connections") or {}),
            labels=dict(ed.get("pin_labels") or {}),
            nc_pins=set(ed.get("nc_pins") or set()),
            mfg_region=ed.get("mfg_region", ""),
            has_geometry=bool(geo.get("body_box")),
        ))
    return entries


def build_report(ir, plan) -> dict:
    """转换报告构建（结构化 dict，供 md/json 输出）。"""
    # 100% 成功：R1 精确 + R5 GND + R6 连接符（含 R2? 不——R2 是近似）。
    exact = [(s.extra_data.get("refdes", "") or s.refdes or "",
              res.target_library_id or "", res.confidence or 0.0)
             for s, res in plan.exact]
    approx = [{
        "refdes": s.extra_data.get("refdes", "") or s.refdes or "",
        "hdl_cell": res.target_library_id or "",
        "conf": round(res.confidence or 0.0, 3),
        "value": s.value or "",
        "footprint": s.footprint or "",
        "pkg_type": (res.extra_data.get("_matched_row") or {}).get("package_type", "")
        if isinstance(res.extra_data.get("_matched_row"), dict) else "",
        "category": s.category or s.extra_data.get("category", ""),
        "page": s.extra_data.get("page_id", ""),
        "loc": (s.extra_data.get("loc_x", 0), s.extra_data.get("loc_y", 0)),
        "strategy": res.strategy.name if res.strategy else "",
    } for s, res in plan.approx]
    gnd = [(s.extra_data.get("refdes", "") or s.refdes or "",
            s.extra_data.get("loc_x", 0), s.extra_data.get("loc_y", 0))
           for s, _res in plan.gnd]
    non_elec = [(s.extra_data.get("refdes", "") or s.refdes or "",
                 res.target_library_id or "")
                for s, res in plan.non_electrical]
    mocks = _mock_entries(plan)
    counts = plan.counts
    total = len(plan.exact) + len(plan.approx) + len(plan.unmatched) \
        + len(plan.non_electrical) + len(plan.gnd)
    # 100% 精确：R1 + GND + 连接符（DSN 唯一信息源确定性输出）。
    ok100 = len(plan.exact) + len(plan.gnd) + counts["R6_off_pages"] \
        + counts["R6_ports"]
    rate = ok100 / max(total + counts["R6_off_pages"] + counts["R6_ports"], 1)
    # hdl 转换成功（用户口径）：R1/R2 均用 hdl_lib 符号 + GND + 连接符；
    # 仅 R3 mock 复刻与 R4 忽略 算未 100%（R2 附 conf 记录差异）。
    hdl_ok = ok100 + len(plan.approx)
    hdl_rate = hdl_ok / max(total + counts["R6_off_pages"] + counts["R6_ports"], 1)
    return {
        "summary": {
            "total_instances": total,
            "R1_exact": len(plan.exact),
            "R2_approx": len(plan.approx),
            "R3_mock": len(plan.unmatched),
            "R4_non_electrical": len(plan.non_electrical),
            "R5_gnd": len(plan.gnd),
            "R6_connectors": counts["R6_off_pages"] + counts["R6_ports"],
            "mock_cells": len(mocks),
            "ok100": ok100,
            "ok100_rate": round(rate * 100, 2),
            "hdl_ok": hdl_ok,
            "hdl_ok_rate": round(hdl_rate * 100, 2),
            "r2_using_hdl": len(plan.approx),
        },
        "exact_list": exact,
        "approx_list": approx,
        "gnd_list": gnd,
        "non_electrical_list": non_elec,
        "mock_manifest": [m.to_dict() for m in mocks],
    }


def to_markdown(report: dict) -> str:
    """结构化报告 → Markdown 文本。"""
    s = report["summary"]
    lines = [
        "# 转换报告（DSN → DEHDL）",
        "",
        "## 汇总",
        "",
        f"- 总实例：**{s['total_instances']}**",
        f"- R1 精确命中（conf≥0.99，hdl_lib 符号）：**{s['R1_exact']}**",
        f"- R2 近似命中（conf≥0.60，hdl_lib 符号 + 差异记录）：**{s['R2_approx']}**",
        f"- R3 未命中（mock 复刻）：**{s['R3_mock']}**（cell {s['mock_cells']}）",
        f"- R4 非电气/辅助：**{s['R4_non_electrical']}**",
        f"- R5 GND 原位放置：**{s['R5_gnd']}**",
        f"- R6 网名连接符：**{s['R6_connectors']}**",
        f"- **100% 精确转换：{s['ok100']}（{s['ok100_rate']}%，R1+GND+连接符）**",
        f"- **hdl 符号转换成功（含 R2 近似 conf≥0.60）：{s['hdl_ok']}"
        f"（{s['hdl_ok_rate']}%）**——R2 {s['r2_using_hdl']} 个用 hdl_lib "
        f"符号但非 100% 匹配（差异记录于下方 R2 清单）",
        "",
        "## 100% 精确转换清单（R1 + R5 GND + R6 连接符）",
        "",
        "| refdes | hdl_cell | conf |",
        "|---|---|---|",
    ]
    for ref, cell, conf in report["exact_list"][:200]:
        lines.append(f"| {ref} | {cell} | {conf:.2f} |")
    if len(report["exact_list"]) > 200:
        lines.append(f"| … 共 {len(report['exact_list'])} 个 R1 |")
    lines += [
        "",
        "## 未 100% 匹配清单及程度",
        "",
        "### R2 近似命中（hdl_lib 符号，记录差异）",
        "",
        "| refdes | hdl_cell | conf | value | footprint | pkg_type | category | page | 位置 | strategy |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for a in report["approx_list"]:
        lines.append(
            f"| {a['refdes']} | {a['hdl_cell']} | {a['conf']:.2f} | "
            f"{a['value'] or '-'} | {a['footprint'] or '-'} | "
            f"{a['pkg_type'] or '-'} | {a['category'] or '-'} | "
            f"{a['page']} | ({a['loc'][0]},{a['loc'][1]}) | {a['strategy']} |"
        )
    lines += [
        "",
        "### R3 未命中（mock 复刻，详见 mock_manifest.md）",
        "",
    ]
    lines += [f"- {m['refdes']}（{m['library_id']}，{m['category'] or '未分类'}）"
              for m in report["mock_manifest"]]
    lines += [
        "",
        "### R4 非电气/辅助（忽略或 hdl_lib 符号）",
        "",
    ]
    lines += [f"- {ref}（{cell}）" for ref, cell in report["non_electrical_list"]]
    return "\n".join(lines)


def to_mock_manifest_md(report: dict) -> str:
    """mock 清单 → Markdown（元件/位置/继承电气引脚信息）。"""
    mocks = report["mock_manifest"]
    lines = [
        "# 模拟图标清单（mock_cells）",
        "",
        f"共 **{len(mocks)}** 个 mock cell（R3 未命中电气件 1:1 几何复刻）。",
        "",
        "| cell | refdes | 库元件 | 分类 | 页 | 位置(x,y) | 旋转 | 引脚数 | 网名数 | NC | 产地 | 几何 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in mocks:
        lines.append(
            f"| {m['cell_name']} | {m['refdes']} | {m['library_id']} | "
            f"{m['category'] or '-'} | {m['page_id']} | "
            f"({m['loc_x']},{m['loc_y']}) | {m['rotation']} | "
            f"{m['pin_count']} | {m['net_count']} | "
            f"{','.join(m['nc_pins']) or '-'} | {m['mfg_region'] or '-'} | "
            f"{'✅' if m['has_geometry'] else '❌'} |"
        )
    lines += [
        "",
        "## 继承的电气/引脚信息（逐 cell）",
        "",
    ]
    for m in mocks:
        lines.append(f"### {m['cell_name']}（{m['refdes']}）")
        lines.append(f"- 库元件：`{m['library_id']}`；分类：{m['category'] or '未分类'}")
        lines.append(f"- 位置：页 {m['page_id']} ({m['loc_x']},{m['loc_y']}) 旋转 {m['rotation']}")
        if m["connections"]:
            conns = ", ".join(f"{k}={v}" for k, v in
                              list(m["connections"].items())[:12])
            lines.append(f"- 引脚连接（{m['net_count']} 条）：{conns}"
                         + ("…" if m["net_count"] > 12 else ""))
        if m["labels"]:
            lbl = ", ".join(f"{k}={v}" for k, v in
                            list(m["labels"].items())[:8])
            lines.append(f"- 引脚标签：{lbl}")
        if m["nc_pins"]:
            lines.append(f"- NC 引脚：{','.join(m['nc_pins'])}")
        if m["mfg_region"]:
            lines.append(f"- 产地：{m['mfg_region']}")
        lines.append(f"- 几何来源：{'DSN symbol_geometry（1:1 复刻）' if m['has_geometry'] else '无（降级占位）'}")
        lines.append("")
    return "\n".join(lines)


def write_reports(ir, plan, out_dir: Path, project: str = "design") -> dict:
    """写转换报告 + mock 清单（``out_dir/`` 下）。"""
    report = build_report(ir, plan)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    conv = out_dir / f"{project}.conversion_report.md"
    conv.write_text(to_markdown(report), encoding="utf-8")
    mani = out_dir / f"{project}.mock_manifest.md"
    mani.write_text(to_mock_manifest_md(report), encoding="utf-8")
    logger.info(
        "转换报告写出: %s（100%%=%s%%），mock 清单 %s cell",
        conv.name, report["summary"]["ok100_rate"], report["summary"]["mock_cells"],
    )
    return {"conversion_report": conv, "mock_manifest": mani}
