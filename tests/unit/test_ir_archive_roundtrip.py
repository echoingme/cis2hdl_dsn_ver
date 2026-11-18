"""R-IRJSON：IR 权威归档 round-trip 保真测试（L1 写入 → load_ir_json 恢复）。"""
import json

import pytest

from cis2hdl.core.writer.ir_archive_writer import load_ir_json, write_all, write_ir_json

from tests.support import data_path, design_path

DSN_PATH = design_path("DSN")
EXP_PATH = design_path("EXP")


@pytest.fixture(scope="module")
def design():
    from pathlib import Path

    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    ir = DSNParser().parse(Path(DSN_PATH), exp_path=Path(EXP_PATH))
    return ir


def test_roundtrip_ir_json(design, tmp_path):
    """L1 ir.json 写出 → load_ir_json 恢复 → 关键字段一致。"""
    out = write_ir_json(design, tmp_path / "design.dsn.ir.json")
    assert out.exists()
    restored = load_ir_json(out)
    assert len(restored.pages) == len(design.pages)
    assert restored.project_name == design.project_name
    r_inst = {i.refdes for p in restored.pages for i in p.instances}
    d_inst = {i.refdes for p in design.pages for i in p.instances}
    assert r_inst == d_inst
    # net_id 字段保留（R-NETID）
    r_nets = {(p.page_name, n.name): n.net_id
              for p in restored.pages for n in p.nets}
    d_nets = {(p.page_name, n.name): n.net_id
              for p in design.pages for n in p.nets}
    assert r_nets == d_nets
    # nc_pins set 还原（R-NC）
    r_nc = {i.refdes: set(i.nc_pins) for p in restored.pages for i in p.instances}
    d_nc = {i.refdes: set(i.nc_pins) for p in design.pages for i in p.instances}
    assert r_nc == d_nc


def test_write_all_four_files(design, tmp_path):
    """L1/L2/L3 四文件全部产出（ir.json/summary.md/bom.csv/net_connections.csv）。"""
    result = write_all(design, tmp_path, project="TG0C0D2_VER_B_SCH")
    for key in ("ir_json", "summary_md", "bom_csv", "net_connections_csv"):
        p = result.get(key)
        assert p is not None and p.exists(), f"{key} 缺失"


def test_roundtrip_symbol_geometry(design, tmp_path):
    """R-GEO：symbol_geometry 结构化字段经归档 round-trip 保留。"""
    out = write_ir_json(design, tmp_path / "geo.dsn.ir.json")
    restored = load_ir_json(out)
    geo_found = False
    for comp in restored.component_db.list_all():
        if comp.symbol_geometry and comp.symbol_geometry.get("body_box"):
            geo_found = True
            break
    assert geo_found, "symbol_geometry 未在 round-trip 中保留"


def test_roundtrip_category_mfg(design, tmp_path):
    """R-CAT/R-MFG：category/mfg_region 实例级字段经归档保留。"""
    out = write_ir_json(design, tmp_path / "cat.dsn.ir.json")
    restored = load_ir_json(out)
    r_cat = {i.refdes: i.extra_data.get("category", "")
             for p in restored.pages for i in p.instances}
    d_cat = {i.refdes: i.extra_data.get("category", "")
             for p in design.pages for i in p.instances}
    assert r_cat == d_cat
    r_mfg = {i.refdes: i.extra_data.get("mfg_region", "")
             for p in restored.pages for i in p.instances}
    d_mfg = {i.refdes: i.extra_data.get("mfg_region", "")
             for p in design.pages for i in p.instances}
    assert r_mfg == d_mfg
