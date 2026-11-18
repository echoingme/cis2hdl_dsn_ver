"""三层归档输出器（T05，OFA-20260820）。

把 DSN 解析器产出的 ``DesignIR`` 归档为三份可消费文件：

- **L1** ``<project>.dsn.ir.json``：全字段无损 IR JSON（程序消费，
  支持 round-trip 反序列化保真验证）；
- **L2** ``<project>.summary.md``：人读摘要（页/实例/网络/文本/图形
  统计 + 帧覆盖率 + 警告列表）；
- **L3** ``<project>.bom.csv``：BOM 元件清单（表格工具消费）；
      ``<project>.net_connections.csv``：引脚网络连接表。

序列化治理（OFA §4.4）：
- ``nc_pins: set[str]`` → list（JSON 不可序列化 set）；
- ``DesignIR.component_db``（arbitrary_types_allowed 普通 class）→ dict；
- 统一 ``json.dumps(..., indent=2, ensure_ascii=False, default=str)``。

不触碰 FR9 DEHDL 字节级等价铁律：本模块只读 IR，不参与 CSA 输出。
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: JSON 兜底序列化（dataclass/Pydantic 混合对象的统一 fallback）
_JSON_DEFAULTS = {"default": str, "indent": 2, "ensure_ascii": False}


def _component_db_to_dict(component_db: object) -> dict:
    """ComponentDB（普通 class）→ 序列化 dict（按 part_name 为键）。"""
    if component_db is None:
        return {}
    out: dict = {}
    for comp in component_db.list_all():
        comp_dict = comp.model_dump() if hasattr(comp, "model_dump") else vars(comp)
        out[comp.part_name] = comp_dict
    return out


def _ir_to_dict(design: object) -> dict:
    """DesignIR → 纯 JSON 可序列化 dict（set→list、component_db→dict）。"""
    data = design.model_dump()
    # component_db 是普通 class（arbitrary_types_allowed），model_dump 会丢 → 手工替换
    data["component_db"] = _component_db_to_dict(design.component_db)
    # set → list 兜底（nc_pins 等）
    for page in data.get("pages", []):
        for inst in page.get("instances", []):
            if isinstance(inst.get("nc_pins"), set):
                inst["nc_pins"] = sorted(inst["nc_pins"])
    return data


def write_ir_json(design: object, output_path: Path) -> Path:
    """L1：IR JSON 权威归档。

    Args:
        design: DesignIR。
        output_path: 输出 .json 路径。

    Returns:
        output_path。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = _ir_to_dict(design)
    output_path.write_text(
        json.dumps(data, **_JSON_DEFAULTS), encoding="utf-8",
    )
    logger.info("IR JSON written: %s", output_path)
    return output_path


def write_summary_md(design: object, output_path: Path) -> Path:
    """L2：人读摘要 summary.md。

    Args:
        design: DesignIR。
        output_path: 输出 .md 路径。

    Returns:
        output_path。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    md = design.summary_md() if hasattr(design, "summary_md") else _fallback_summary(design)
    output_path.write_text(md, encoding="utf-8")
    logger.info("summary.md written: %s", output_path)
    return output_path


def _fallback_summary(design: object) -> str:
    """DesignIR 无 summary_md 方法时的兜底摘要（自建表格）。"""
    pages = getattr(design, "pages", [])
    nets = getattr(design, "global_nets", [])
    metadata = getattr(design, "metadata", {}) or {}
    lines = [
        f"# {getattr(design, 'project_name', 'dsn')} 解析摘要",
        "",
        f"- 源格式：{getattr(design, 'source_format', '')}",
        f"- 页面数：{len(pages)}",
        f"- 全局网络：{len(nets)}",
        f"- 帧覆盖率：{metadata.get('frame_stats', {}).get('covered_pct', 'n/a')}%",
        f"- 解码器版本：{metadata.get('decoder_version', 'n/a')}",
        "",
        "## 实例规模",
        "",
        "| refdes | 页 | x | y | 值 | 引脚连接 |",
        "|---|---|---|---|---|---|",
    ]
    for page in pages:
        for inst in getattr(page, "instances", []):
            lines.append(
                f"| {getattr(inst, 'refdes', '')} | {page.page_name} | "
                f"{getattr(inst, 'loc_x', 0)} | {getattr(inst, 'loc_y', 0)} | "
                f"{getattr(inst, 'value_override', '') or ''} | "
                f"{len(getattr(inst, 'pin_connections', {}) or {})} |"
            )
    warnings = metadata.get("warnings", []) or []
    if warnings:
        lines += ["", "## 警告", ""]
        for w in warnings[:50]:
            lines.append(f"- [{w.get('severity', 'WARN')}] {w.get('code', '')}: {w.get('msg', '')}")
    return "\n".join(lines) + "\n"


def write_bom_csv(design: object, output_path: Path) -> Path:
    """L3a：BOM 元件清单 CSV（743 行元件）。

    列：Reference, Value, Footprint, Library, X, Y, Page, Category,
    Description, PCB Footprint。

    Args:
        design: DesignIR。
        output_path: 输出 .csv 路径。

    Returns:
        output_path。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for page in getattr(design, "pages", []):
        for inst in getattr(page, "instances", []):
            attrs = getattr(inst, "user_attrs", {}) or {}
            rows.append({
                "Reference": getattr(inst, "refdes", ""),
                "Value": getattr(inst, "value_override", "") or "",
                "Footprint": "",
                "Library": getattr(inst, "library_id", ""),
                "X": getattr(inst, "loc_x", 0),
                "Y": getattr(inst, "loc_y", 0),
                "Page": page.page_name,
                "Category": attrs.get("CATEGORY_NAME", ""),
                "Description": attrs.get("描述", ""),
                "PCB Footprint": attrs.get("PCB Footprint", ""),
            })
    rows.sort(key=lambda r: r["Reference"])
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    logger.info("BOM CSV written: %s (%d rows)", output_path, len(rows))
    return output_path


def write_net_connections_csv(design: object, output_path: Path) -> Path:
    """L3b：引脚网络连接表 CSV（≥2189 行连接）。

    列：Reference, Pin, Net。

    Args:
        design: DesignIR。
        output_path: 输出 .csv 路径。

    Returns:
        output_path。
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for page in getattr(design, "pages", []):
        for inst in getattr(page, "instances", []):
            for pin, net in (getattr(inst, "pin_connections", {}) or {}).items():
                rows.append({
                    "Reference": getattr(inst, "refdes", ""),
                    "Pin": pin,
                    "Net": net,
                })
    with output_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Reference", "Pin", "Net"])
        writer.writeheader()
        writer.writerows(rows)
    logger.info("net_connections CSV written: %s (%d rows)", output_path, len(rows))
    return output_path


def load_ir_json(json_path: Path) -> "DesignIR":
    """R-IRJSON 反序列化：IR JSON → DesignIR（round-trip 保真）。

    component_db 在序列化时为 ``{part_name: comp_dict}`` dict，反序列化
    时重建 ``ComponentDB``（add_batch 恢复全部索引）。其余字段经
    ``DesignIR.model_validate`` 恢复（nc_pins set 自动还原）。

    Args:
        json_path: ``<project>.dsn.ir.json`` 路径。

    Returns:
        DesignIR（与 write_ir_json 输入等价，round-trip 一致）。
    """
    from cis2hdl.core.db.component_db import ComponentDB
    from cis2hdl.core.ir.component import ComponentDef
    from cis2hdl.core.ir.design import DesignIR

    doc = json.loads(Path(json_path).read_text(encoding="utf-8"))
    comp_dicts = doc.pop("component_db", {}) or {}
    db = ComponentDB()
    for part_name, comp_d in comp_dicts.items():
        db.add(ComponentDef.model_validate(comp_d))
    design = DesignIR.model_validate(doc)
    design.component_db = db
    return design


def write_all(design: object, output_dir: Path, project: str = "design") -> dict:
    """写全部四层归档，返回各文件路径。

    Args:
        design: DesignIR。
        output_dir: 输出目录（``output_dir/ir/`` 下）。
        project: 工程名（文件名前缀）。

    Returns:
        {"ir_json": Path, "summary_md": Path, "bom_csv": Path,
         "net_connections_csv": Path, "frames_jsonl": Path,
         "frame_coverage_csv": Path}。
    """
    ir_dir = output_dir / "ir"
    ir_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "ir_json": write_ir_json(design, ir_dir / f"{project}.dsn.ir.json"),
        "summary_md": write_summary_md(design, ir_dir / f"{project}.summary.md"),
        "bom_csv": write_bom_csv(design, ir_dir / f"{project}.bom.csv"),
        "net_connections_csv": write_net_connections_csv(
            design, ir_dir / f"{project}.net_connections.csv",
        ),
    }
    # ── L0 证据层：全帧级中间文件（OFA-20260820 晚）──
    # 帧引用由 DSNParser.parse 挂到 metadata["_pages_frames_ref"]，
    # 此处消费并写出；无引用时静默跳过（writer 可独立使用）。
    pages_frames = getattr(design, "metadata", {}).get("_pages_frames_ref")
    if pages_frames:
        from .frame_records_writer import write_frame_records

        frame_paths = write_frame_records(
            pages_frames, output_dir=ir_dir, project=project,
        )
        result["frames_jsonl"] = frame_paths["jsonl"]
        result["frame_coverage_csv"] = frame_paths["coverage_csv"]
    return result
