"""视图层三 CSV（B1）：components / pins / nets，对齐黄金列。

归属（v2.1 用户裁决 + §5.5）：视图层 CSV 列由**下游定义**（本模块），
内容从 DesignIR 派生（DSN 唯一信息源）；黄金标准列级核对的唯一比对面
就是这三张 CSV。列名直接复用黄金表头（EXP 43 / partandpin 48 /
flatnet 20 位置语义），无源/常量列按黄金默认（``<null>``/``Default``）
输出，保证比对脚本可按列名逐列对拍。

编码：GBK（黄金文件为 GBK 导出；Unicode 分类/描述列可无损承载）。
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path
from typing import Iterable, Optional

from cis2hdl.core.ir.design import DesignIR

logger = logging.getLogger(__name__)

# ═══ 黄金表头（实测提取，勿改列序） ═══

#: components.csv：纯 part EXP 43 列（TG0C0D2_VER_B_SCH.EXP 表头行 1）。
COMPONENTS_COLUMNS = [
    "HEADER", "ID", "Part Reference", "Value", "CATEGORY_NAME", "CBBIID",
    "CBBMARK", "CODE", "COMPONENT_HIGH_MAX", "COMPUID", "COMPUTING_RANKING",
    "DESCRIPTION", "DEVICE", "FORWARD_PCB", "Graphic", "HETERO", "HIGHLIGHT",
    "LEVEL", "LIBRARYCREATED", "Location X-Coordinate", "Location Y-Coordinate",
    "MFGREGION", "NC", "Need To Mirror/Rotate Pin Display Properties",
    "OriginalSymbolOrigin", "PACKAGE_TYPE", "PARTS", "PCB Footprint", "PINOFF",
    "PINSWAP", "PKG_GRP", "PKG_TYPE", "Power Pins Visible", "SUID",
    "Source Library", "Source Package", "Source Part", "VARIANT_ID", "WELD",
    "额定工作电压(V)", "额定功率(W)", "描述", "阻值允许偏差",
]

#: pins.csv：partandpin EXP 48 列 + 视图层附加 net_id 列。
PINS_COLUMNS = [
    "HEADER", "ID", "Part Reference", "Value", "CATEGORY_NAME", "CBBIID",
    "CBBMARK", "CODE", "COMPONENT_HIGH_MAX", "COMPUID", "COMPUTING_RANKING",
    "DESCRIPTION", "DEVICE", "FORWARD_PCB", "Graphic", "HETERO", "HIGHLIGHT",
    "Is No Connect", "LEVEL", "LIBRARYCREATED", "Location X-Coordinate",
    "Location Y-Coordinate", "MFGREGION", "NC", "Need To Mirror/Rotate Pin Display Properties",
    "Net Name", "Number", "OriginalSymbolOrigin", "PACKAGE_TYPE", "PARTS",
    "PCB Footprint", "PINOFF", "PINSWAP", "PINTYPE", "PKG_GRP", "PKG_TYPE",
    "Power Pins Visible", "SUID", "Source Library", "Source Package",
    "Source Part", "Type", "VARIANT_ID", "WELD", "额定工作电压(V)",
    "额定功率(W)", "描述", "阻值允许偏差",
] + ["net_id"]  # 视图层附加列（R-NETID 显式字段）

#: nets.csv：flatnet 20 位置语义（附录-A §4.4.3）。
NETS_COLUMNS = [
    "Net Name", "ID", "BISHEET", "CURRENT", "Color", "PINORDER",
    "Is Global", "Line Style", "Line Width", "NETNAME", "Net ID",
    "Net Name 2", "DEVICE", "POWER_TYPE", "SINGAL", "SHEET", "SUID",
    "Pin Order", "VOLTAGE", "描述",
]

_NULL = "<null>"


# ═══ 行构建 ═══

def _components_row(ir: DesignIR, inst) -> list[str]:
    """实例 → components.csv 行（43 列）。"""
    comp = _comp_of(ir, inst)
    ed = inst.extra_data or {}
    props = inst.properties or {}
    row = [_NULL] * len(COMPONENTS_COLUMNS)
    col = {c: i for i, c in enumerate(COMPONENTS_COLUMNS)}
    row[col["Part Reference"]] = inst.refdes
    row[col["Value"]] = inst.value_override or comp.value if comp else inst.value_override
    row[col["CATEGORY_NAME"]] = ed.get("category") or (comp.category if comp else "") or _NULL
    row[col["COMPONENT_HIGH_MAX"]] = inst.user_attrs.get("COMPONENT_HIGH_MAX", _NULL)
    row[col["Location X-Coordinate"]] = str(inst.loc_x)
    row[col["Location Y-Coordinate"]] = str(inst.loc_y)
    row[col["MFGREGION"]] = ed.get("mfg_region") or inst.user_attrs.get("MFGREGION", _NULL)
    row[col["NC"]] = ",".join(sorted(inst.nc_pins)) if inst.nc_pins else _NULL
    row[col["OriginalSymbolOrigin"]] = "0,0"
    row[col["PCB Footprint"]] = props.get("PCB Footprint") or (comp.footprint if comp else "") or _NULL
    row[col["Source Library"]] = "LIBRARY1.OLB"
    row[col["Source Package"]] = inst.library_id
    row[col["Source Part"]] = comp.part_name if comp else inst.library_id
    row[col["额定工作电压(V)"]] = inst.user_attrs.get("额定工作电压(V)", _NULL)
    row[col["额定功率(W)"]] = inst.user_attrs.get("额定功率(W)", _NULL)
    row[col["描述"]] = inst.user_attrs.get("描述", _NULL)
    row[col["阻值允许偏差"]] = inst.user_attrs.get("阻值允许偏差", _NULL)
    return row


def _pins_row(ir: DesignIR, inst, pin: str, net: str, net_id: Optional[int]) -> list[str]:
    """引脚连接 → pins.csv 行（48+1 列）。"""
    comp = _comp_of(ir, inst)
    ed = inst.extra_data or {}
    row = [_NULL] * len(PINS_COLUMNS)
    col = {c: i for i, c in enumerate(PINS_COLUMNS)}
    row[col["Part Reference"]] = inst.refdes
    row[col["Value"]] = inst.value_override or ""
    row[col["CATEGORY_NAME"]] = ed.get("category") or (comp.category if comp else "") or _NULL
    row[col["Location X-Coordinate"]] = str(inst.loc_x)
    row[col["Location Y-Coordinate"]] = str(inst.loc_y)
    row[col["MFGREGION"]] = ed.get("mfg_region") or _NULL
    row[col["NC"]] = ",".join(sorted(inst.nc_pins)) if inst.nc_pins else _NULL
    row[col["Net Name"]] = net or _NULL
    row[col["Number"]] = inst.pin_labels.get(pin, pin)  # 球标优先，缺省引脚名
    row[col["PCB Footprint"]] = (inst.properties or {}).get("PCB Footprint") or (comp.footprint if comp else "") or _NULL
    row[col["Source Library"]] = "LIBRARY1.OLB"
    row[col["Source Package"]] = inst.library_id
    row[col["Source Part"]] = comp.part_name if comp else inst.library_id
    row[col["net_id"]] = str(net_id) if net_id is not None else _NULL
    return row


def _nets_row(ir: DesignIR, net) -> list[str]:
    """网络 → nets.csv 行（20 列，flatnet 位置语义）。"""
    row = [_NULL] * len(NETS_COLUMNS)
    col = {c: i for i, c in enumerate(NETS_COLUMNS)}
    name = net.name or ""
    row[col["Net Name"]] = name
    row[col["ID"]] = str(net.net_id) if net.net_id is not None else _NULL
    row[col["Is Global"]] = "True" if name in ir.global_nets else "False"
    row[col["NETNAME"]] = name
    row[col["Net ID"]] = str(net.net_id) if net.net_id is not None else _NULL
    row[col["Net Name 2"]] = name
    row[col["POWER_TYPE"]] = "GND" if name == "GND" else ("POWER" if name in ir.global_nets else _NULL)
    return row


def _comp_of(ir: DesignIR, inst):
    from cis2hdl.core.intermediate.ir_access import component_of

    return component_of(ir, inst)


def _write_csv(path: Path, columns: list[str], rows: Iterable[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="gbk", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(columns)
        w.writerows(rows)


# ═══ 公共入口 ═══

def write_components_csv(ir: DesignIR, path: Path) -> Path:
    """实例清单（含 GND，U7 单行 + section）。"""
    rows = [_components_row(ir, inst) for page in ir.pages for inst in page.instances]
    _write_csv(path, COMPONENTS_COLUMNS, rows)
    logger.info("components.csv: %d 行 → %s", len(rows), path)
    return path


def write_pins_csv(ir: DesignIR, path: Path) -> Path:
    """引脚连接清单（pin_connections 全量 + net_id）。"""
    rows: list[list[str]] = []
    for page in ir.pages:
        for inst in page.instances:
            for pin, net in (inst.pin_connections or {}).items():
                nid = _net_id_of(ir, net)
                rows.append(_pins_row(ir, inst, pin, net, nid))
    _write_csv(path, PINS_COLUMNS, rows)
    logger.info("pins.csv: %d 行 → %s", len(rows), path)
    return path


def write_nets_csv(ir: DesignIR, path: Path) -> Path:
    """网络清单（全部页面网名 + net_id + Is Global）。"""
    seen: set[str] = set()
    rows: list[list[str]] = []
    for page in ir.pages:
        for net in page.nets:
            if net.name in seen:
                continue
            seen.add(net.name)
            rows.append(_nets_row(ir, net))
    _write_csv(path, NETS_COLUMNS, rows)
    logger.info("nets.csv: %d 行 → %s", len(rows), path)
    return path


def _net_id_of(ir: DesignIR, net_name: str) -> Optional[int]:
    from cis2hdl.core.intermediate.ir_access import net_id_of

    return net_id_of(ir, net_name)


def write_all(ir: DesignIR, out_dir: Path, project: str = "design") -> dict:
    """写三 CSV 全部，返回路径 dict。"""
    out_dir = Path(out_dir)
    return {
        "components_csv": write_components_csv(ir, out_dir / f"{project}.components.csv"),
        "pins_csv": write_pins_csv(ir, out_dir / f"{project}.pins.csv"),
        "nets_csv": write_nets_csv(ir, out_dir / f"{project}.nets.csv"),
    }


def write_all_archive(ir: DesignIR, out_dir: Path, project: str = "design") -> dict:
    """一站式归档：IR JSON + summary + 三 CSV（R-IRJSON 注册点）。

    对应 ``OutputConfig.ir_json``/``view_csv``（默认 True）——DSN
    解析后的权威归档 + 黄金比对面一次落盘：

    ::

        <out_dir>/ir/<project>.dsn.ir.json   （IR 权威归档，L1）
        <out_dir>/ir/summary.md              （解析统计，L2）
        <out_dir>/ir/bom.csv + net_connections.csv（L3）
        <out_dir>/<project>.components.csv   （视图层）
        <out_dir>/<project>.pins.csv
        <out_dir>/<project>.nets.csv

    Args:
        ir: DesignIR。
        out_dir: 输出根目录。
        project: 工程名（文件名前缀）。

    Returns:
        全部产出路径 dict。
    """
    from cis2hdl.core.writer.ir_archive_writer import write_all as _write_ir

    out_dir = Path(out_dir)
    archive = _write_ir(ir, out_dir, project)
    views = write_all(ir, out_dir, project)
    return {**archive, **views}
