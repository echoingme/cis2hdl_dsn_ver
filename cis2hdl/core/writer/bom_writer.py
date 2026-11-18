"""BOM 生成器（对齐参考格式 M17.140002_V1500-5G-R_V1.0_BOM_20250701.xlsx）。

列：物料编号 | 名称 | 规格描述 | 封装 | 器件编号 | 数量 | 备注
- 物料编号：hdl_lib 匹配行的 ``sn_num``（M01/M02.xxxxxx 规范）；未匹配
  （R3 mock / R4 忽略）单独列写 **MOCK**；
- 名称/规格描述/封装：hdl_lib 匹配行（description/specification/
  package_type）规范登记；mock 用 DSN 侧信息（value/footprint）；
- 器件编号：同物料元件 refdes 逗号连接；
- 备注：匹配情况（R1/R2 conf）、未匹配信息（DSN value/footprint/
  category 缺失等）、是否 mock 替代；
- 分类行：按 hdl_lib 元件 category 分组（片式电阻/片式电容…），
  mock 归入 ``未匹配(MOCK)`` 组。

文件名：``<工程名>_BOM_<YYYYMMDD>.xlsx``（参考命名）。
输出目录：``output/<工程名>/bom/``（用户裁决 2：不散落 output 根）。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

#: BOM 表头（参考 xlsx 行 3）。
_BOM_HEADER = ["物料编号", "名称", "规格描述", "封装", "器件编号", "数量", "备注"]


#: 规格描述开头的类别词前缀（剥离，类别词已入名称列）。
_CAT_PREFIXES = (
    "片式电容", "片式电阻", "片式电感", "片式磁珠", "磁珠",
    "片式厚膜电阻器", "贴片电容", "贴片电阻", "功率电感", "晶振",
)


def _strip_cat_prefix(desc: str) -> str:
    """剥离规格描述开头的类别词（如 '片式电阻22K...' → '22K...'）。"""
    d = str(desc or "").strip()
    for pre in _CAT_PREFIXES:
        if d.startswith(pre):
            return d[len(pre):].lstrip()
    return d


def _sn_num_of(matched_row: Optional[dict]) -> str:
    """匹配行物料编号（M01/M02.xxxxxx）；无 → 空。"""
    if not isinstance(matched_row, dict):
        return ""
    return str(matched_row.get("sn_num", "") or "").strip()


def _name_of(category: str) -> str:
    """hdl_lib 类别 → BOM 名称（片式电阻/片式电容等）。"""
    cat = (category or "").strip()
    if not cat:
        return ""
    if cat in ("resistor", "电阻"):
        return "片式电阻"
    if cat in ("capacitor", "电容"):
        return "片式电容"
    if cat in ("inductor", "电感"):
        return "片式电感"
    if cat in ("ferrite_bead", "磁珠", "fb"):
        return "磁珠"
    if cat in ("connector", "连接器"):
        return "连接器"
    if cat in ("crystal", "oscillator"):
        return "晶振/振荡器"
    return cat


def _note_of(src, result, kind: str) -> str:
    """备注：匹配情况 + 未匹配信息 + mock 替代。"""
    ref = (src.extra_data.get("refdes", "") if src else "") or ""
    conf = result.confidence if result is not None else None
    if kind == "R1":
        return f"R1 精确匹配 conf={conf:.2f}"
    if kind == "R2":
        diff = []
        mr = (result.extra_data.get("_matched_row") or {}) if result else {}
        if src.value and mr.get("value") and str(src.value).upper() != str(mr.get("value")).upper():
            diff.append(f"DSN value={src.value!r} vs 库 {mr.get('value')!r}")
        if src.footprint and mr.get("package_type") and str(src.footprint).upper() != str(mr.get("package_type")).upper():
            diff.append(f"DSN fp={src.footprint!r} vs 库 {mr.get('package_type')!r}")
        return f"R2 近似 conf={conf:.2f}" + ("；" + "；".join(diff) if diff else "")
    if kind == "MOCK":
        info = []
        if src.value:
            info.append(f"value={src.value!r}")
        if src.footprint:
            info.append(f"fp={src.footprint!r}")
        if not (src.category or src.extra_data.get("category")):
            info.append("category 缺失")
        return "未匹配 hdl_lib，MOCK 模拟图标替代" + (f"（{ '，'.join(info)}）" if info else "")
    if kind == "R4":
        return "非电气/辅助件（hdl_lib 无对应，忽略/复刻）"
    if kind == "GND":
        return "GND 原位放置"
    return ""


def build_bom_rows(ir, plan) -> list[dict]:
    """构建 BOM 行（按 物料编号+规格 分组）。

    返回::

        [{"group": "片式电阻", "rows": [ {物料编号/名称/规格描述/封装/
        器件编号/数量/备注}, ...]}, ...]
    """
    groups: dict[str, list[dict]] = {}
    order: list[str] = []

    def _add(group: str, row: dict) -> None:
        if group not in groups:
            groups[group] = []
            order.append(group)
        groups[group].append(row)

    from collections import defaultdict
    bucket: dict[tuple, dict] = defaultdict(lambda: {
        "物料编号": "", "名称": "", "规格描述": "", "封装": "",
        "器件编号": [], "数量": 0, "备注": "", "_group": "",
    })

    def _flush(b: dict) -> None:
        if not b["器件编号"]:
            return
        row = dict(b)
        row["器件编号"] = ",".join(sorted(row["器件编号"]))
        _add(row.pop("_group"), row)

    # R1/R2：按 物料编号+规格 聚组
    for section, kind in ((plan.exact, "R1"), (plan.approx, "R2")):
        for src, res in section:
            mr = (res.extra_data.get("_matched_row") or {}) if res else {}
            sn = _sn_num_of(mr) or (src.library_id or "")
            desc = _strip_cat_prefix(
                str(mr.get("description", "") or "")) or (src.category or "")
            spec = str(mr.get("specification", "") or "")
            pkg = str(mr.get("package_type", "") or "") or (src.footprint or "")
            # 用户裁决：匹配件名称 = 类别中文（片式电容/片式电阻），
            # 由 hdl_lib 元件类别（target_library_id）映射。
            grp = _name_of(res.target_library_id) or "匹配件"
            key = (sn, desc, spec, pkg)
            b = bucket[key]
            b["物料编号"] = sn
            b["名称"] = grp
            b["规格描述"] = desc
            b["封装"] = pkg
            b["器件编号"].append(src.extra_data.get("refdes", "") or src.refdes or "")
            b["数量"] += 1
            b["备注"] = _note_of(src, res, kind)
            b["_group"] = grp
            if len(bucket) > 1 and len(b["器件编号"]) == 1:
                pass
    for b in bucket.values():
        _flush(b)

    # MOCK（R3 未匹配 + R4）——用户裁决二：每个器件单独一行，或
    # 同样规格（value+footprint）合并一行；规格描述=value、封装=footprint
    # 填入封装列（不再只堆在规格描述里）。
    mock_grp = "MOCK"
    mbucket: dict[tuple, dict] = defaultdict(lambda: {
        "物料编号": "MOCK", "名称": "", "规格描述": "", "封装": "",
        "器件编号": [], "数量": 0, "备注": "", "_group": mock_grp,
    })
    for section, kind in ((plan.unmatched, "MOCK"), (plan.non_electrical, "R4")):
        for src, res in section:
            val = src.value or (src.extra_data.get("category", "") or "")
            fp = src.footprint or ""
            key = (val, fp)
            b = mbucket[key]
            # 用户裁决：MOCK 名称 = 原本元件的 category name
            # （如 C63 "高Q射频陶瓷电容"、C198/C58 "大容量陶瓷电容"），
            # 无分类时回退 DSN library_id。
            b["名称"] = (src.category or src.extra_data.get("category", "")
                         or src.library_id or "")
            b["器件编号"].append(src.extra_data.get("refdes", "") or src.refdes or "")
            b["数量"] += 1
            b["规格描述"] = str(val)
            b["封装"] = str(fp)
            b["备注"] = _note_of(src, res, kind)
    for b in mbucket.values():
        _flush(b)

    return [{"group": g, "rows": groups[g]} for g in order]


def write_bom_xlsx(ir, plan, out_dir: Path, project: str) -> Path:
    """写 BOM xlsx（参考格式），返回路径。"""
    import openpyxl
    from openpyxl.styles import Font, Alignment, PatternFill

    groups = build_bom_rows(ir, plan)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"{project}_BOM_{date.today().strftime('%Y%m%d')}.xlsx"
    path = out_dir / fname

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{project} PCBA"

    # 文件头（参考行 1-2）
    ws.append(["文件编号： ", "RD-XXXX", "", "制表日期：" + date.today().strftime("%Y/%m/%d")])
    ws.append(["产品名称：", project, "", "", "V1.0"])
    ws.append(_BOM_HEADER + ["需求\n数量", "实发\n数量", "实收\n数量", "退料\n数量"])

    from openpyxl.styles import Border, Side
    bold = Font(bold=True)
    thin = Side(style="thin")
    medium = Side(style="medium")
    # 用户裁决（HG322G 参考）：表头不高亮（无填充），全表框线。
    all_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_border = Border(left=medium, right=medium, top=medium, bottom=medium)
    for c in range(1, len(_BOM_HEADER) + 5):
        cell = ws.cell(3, c)
        cell.font = bold
        cell.border = head_border

    total = 0
    for grp in groups:
        ws.append([grp["group"]])
        ws.cell(ws.max_row, 1).font = bold
        for row in grp["rows"]:
            ws.append([row["物料编号"], row["名称"], row["规格描述"], row["封装"],
                       row["器件编号"], row["数量"], row["备注"], "", "", "", ""])
            total += row["数量"]

    # 列宽
    widths = [14, 16, 46, 14, 52, 8, 46, 10, 10, 10, 10]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = w
    for r in ws.iter_rows(min_row=4, max_row=ws.max_row):
        for cell in r:
            cell.border = all_border
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    wb.save(path)
    logger.info("BOM xlsx 写出: %s（%d 分组，%d 器件）", path.name, len(groups), total)
    return path
