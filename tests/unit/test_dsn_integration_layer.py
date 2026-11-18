"""对接层（intermediate/matcher/writer/qa）单元测试。

覆盖：ir_load 消费侧 / ir_access 只读访问 / inherit 继承合并 /
view_layer 三 CSV / source_builder 匹配源 / result_router 六类路由 /
dsn_geometry 指令 / mock_rebuilder 复刻 / golden_verify 黄金比对。

全部用**构造数据**（不依赖黄金文件），符合 A 类验收口径（纯函数单测）。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cis2hdl.core.ir.component import ComponentDef, ComponentInstanceIR, PinDef
from cis2hdl.core.ir.design import DesignIR, NetIR, PageIR
from cis2hdl.core.ir.match import MatchResult, MatchStrategy


# ═══ 构造 DesignIR（迷你设计：2 页，U7 多段 + R1 + GND + 网络） ═══

def _make_design() -> DesignIR:
    u7 = ComponentInstanceIR(
        refdes="U7", library_id="HI5662.Normal", section=6,
        loc_x=1010, loc_y=110, rotation=0, mirror=0,
        pin_connections={"C8": "GPIO_46", "F19": "PFI"},
        pin_labels={"C8": "C8", "F19": "F19"},
        nc_pins={"A10"},
        user_attrs={"描述": "DDR 芯片"},
        extra_data={"category": "集成电路", "mfg_region": "中国",
                    "page_id": "1.1"},
    )
    r1 = ComponentInstanceIR(
        refdes="R1", library_id="R.Normal", loc_x=100, loc_y=200,
        pin_connections={"1": "3V3_SOC", "2": "GND"},
        user_attrs={"额定功率(W)": "0.1"},
    )
    gnd = ComponentInstanceIR(
        refdes="GND_1", library_id="GND_SYMBOL.Normal", loc_x=50, loc_y=50,
        pin_connections={"1": "GND"},
    )
    page1 = PageIR(page_id="1.1", page_name="01_TOP", instances=[u7, r1, gnd],
                   nets=[NetIR(name="3V3_SOC", net_id=100),
                         NetIR(name="GND", net_id=101),
                         NetIR(name="GPIO_46", net_id=102)])
    page2 = PageIR(page_id="1.2", page_name="02_PWR", instances=[],
                   nets=[NetIR(name="PFI", net_id=103)])
    ir = DesignIR(
        project_name="T_MINI", pages=[page1, page2],
        global_nets=["3V3_SOC", "GND", "PFI"],
    )
    hi = ComponentDef(
        library_id="HI5662.Normal", part_name="HI5662.Normal",
        footprint="BGA409", pin_count=2, category="集成电路",
        user_attrs={"描述": "DDR 芯片"},
        symbol_geometry={"body_box": [0, 0, 600, 400],
                         "pin_lines": [0, 0, 10, 290],
                         "texts": [{"text": "VSS", "x": 100, "y": 300}],
                         "geometry_source": "cache"},
        symbol_texts=[{"symbol": "HI5662", "text": "DDR"}],
    )
    r_comp = ComponentDef(
        library_id="R.Normal", part_name="R.Normal", value="10K",
        footprint="R0402", pin_count=2,
        user_attrs={"额定功率(W)": "0.1", "阻值允许偏差": "+/-1%"},
    )
    ir.component_db.add(hi)
    ir.component_db.add(r_comp)
    return ir


@pytest.fixture()
def design() -> DesignIR:
    return _make_design()


# ═══ ir_load / ir_access ═══

def test_ir_load_roundtrip(design):
    from cis2hdl.core.intermediate.ir_load import load
    from cis2hdl.core.writer.ir_archive_writer import write_ir_json

    with tempfile.TemporaryDirectory() as td:
        p = write_ir_json(design, Path(td) / "t.dsn.ir.json")
        restored = load(p)
    assert len(restored.pages) == 2
    assert restored.component_db.get_by_library_id("HI5662.Normal") is not None


def test_ir_load_missing_file(design):
    from cis2hdl.core.intermediate.ir_load import load

    with pytest.raises(FileNotFoundError):
        load(Path("/nonexistent/x.dsn.ir.json"))


def test_ir_access(design):
    from cis2hdl.core.intermediate import ir_access

    assert ir_access.get_instance(design, "R1").refdes == "R1"
    assert ir_access.get_instance(design, "U7X") is None
    assert ir_access.net_id_of(design, "3V3_SOC") == 100
    assert ir_access.get_net(design, "PFI").net_id == 103
    assert ir_access.get_component(design, "HI5662.Normal") is not None
    # .Normal 回退
    assert ir_access.get_component(design, "HI5662").part_name == "HI5662.Normal"
    assert ir_access.page_of(design, "R1").page_id == "1.1"
    assert ir_access.global_nets(design) == {"3V3_SOC", "GND", "PFI"}


def test_ir_access_component_of(design):
    from cis2hdl.core.intermediate import ir_access

    inst = ir_access.get_instance(design, "U7")
    comp = ir_access.component_of(design, inst)
    assert comp.part_name == "HI5662.Normal"


# ═══ inherit ═══

def test_aggregate_user_attrs(design):
    from cis2hdl.core.intermediate import ir_access
    from cis2hdl.core.intermediate.inherit import aggregate_user_attrs

    insts = [ir_access.get_instance(design, "R1")]
    merged = aggregate_user_attrs(insts, "R.Normal")
    assert merged["额定功率(W)"] == "0.1"


def test_inherit_into_instance(design):
    from cis2hdl.core.intermediate import ir_access
    from cis2hdl.core.intermediate.inherit import inherit_into_instance

    r1 = ir_access.get_instance(design, "R1")
    comp = ir_access.get_component(design, "R.Normal")
    # R1 value_override 空 → 继承库 value 10K
    acts = inherit_into_instance(r1, comp, 1.0)
    assert r1.value_override == "10K"
    assert any("value_override=10K" in a for a in acts)
    # 已有效力 → 不覆盖
    r1.user_attrs["阻值允许偏差"] = "+/-5%"
    acts2 = inherit_into_instance(r1, comp, 1.0)
    assert r1.user_attrs["阻值允许偏差"] == "+/-5%"


# ═══ view_layer ═══

def test_view_layer_csv(design):
    from cis2hdl.core.intermediate.view_layer import (
        PINS_COLUMNS, write_all,
    )

    with tempfile.TemporaryDirectory() as td:
        r = write_all(design, Path(td), "T_MINI")
        assert r["components_csv"].exists()
        assert r["pins_csv"].exists()
        assert r["nets_csv"].exists()
        lines = r["pins_csv"].read_text(encoding="gbk").strip().splitlines()
        assert len(lines) - 1 == 5  # U7 2 + R1 2 + GND 1 连接
        assert len(PINS_COLUMNS) == 49  # 48 黄金列 + net_id
        nets = r["nets_csv"].read_text(encoding="gbk").splitlines()
        assert any("3V3_SOC" in ln for ln in nets)


# ═══ source_builder / result_router ═══

def test_source_builder(design):
    from cis2hdl.core.matcher.source_builder import build_source_components

    sources = build_source_components(design)
    by_ref = {s.extra_data["refdes"]: s for s in sources}
    assert "U7" in by_ref and "R1" in by_ref and "GND_1" in by_ref
    assert by_ref["U7"].extra_data.get("route_class") is None  # U7 非 GND
    assert by_ref["GND_1"].extra_data["route_class"] == "R5"
    assert by_ref["U7"].extra_data["symbol_geometry"]["body_box"] == [0, 0, 600, 400]
    assert by_ref["U7"].pin_count == 2


def test_result_router(design):
    from cis2hdl.core.matcher.result_router import route
    from cis2hdl.core.matcher.source_builder import build_source_components

    sources = build_source_components(design)
    results = []
    for s in sources:
        conf = 1.0
        if s.extra_data.get("is_non_electrical"):
            conf = 0.3
        results.append(MatchResult(confidence=conf,
                                   strategy=MatchStrategy.PASSIVE_EXACT,
                                   source_library_id=s.library_id))
    plan = route(results, sources, design)
    assert plan.counts["R5_gnd"] == 1          # GND_1
    assert len(plan.exact) == 2                 # U7 + R1
    assert plan.counts["R3_unmatched"] == 0
    assert len(plan.ports) == 0


# ═══ dsn_geometry / mock_rebuilder ═══

def test_dsn_geometry(design):
    from cis2hdl.core.writer import dsn_geometry

    css = dsn_geometry.symbol_geometry_to_css(
        design.component_db.get_by_library_id("HI5662.Normal").symbol_geometry,
        {"1": "C8"},
    )
    joined = "\n".join(css)
    assert 'P "CDS_LMAN_SYM_OUTLINE"' in joined
    assert "M 0 400 600 400 -1 0" in joined   # 体框上边
    assert "L 0 0 10 290 -1 0" in joined    # 引脚线
    assert "VSS" in joined                     # 文本


def test_mock_rebuilder(design):
    from cis2hdl.core.matcher.source_builder import build_source_components
    from cis2hdl.core.writer.mock_rebuilder import MockRebuilder

    sources = build_source_components(design)
    unmatched = [s for s in sources if s.extra_data["refdes"] == "U7"]
    rb = MockRebuilder()
    with tempfile.TemporaryDirectory() as td:
        files = rb.write_to_temp_lib(unmatched, Path(td))
        css = [f for f in files if f.name == "symbol.css"][0]
        content = css.read_text()
        assert 'P "CDS_LMAN_SYM_OUTLINE" "0,400,600,0"' in content
        assert rb.stats.with_geometry == 1
    assert rb.stats.cells == 1


# ═══ golden_verify ═══

def test_golden_verify_full(design, tmp_path):
    """构造迷你黄金文件 + 视图层 CSV → 全通过。"""
    import csv as _csv

    from cis2hdl.core.intermediate.view_layer import write_all
    from cis2hdl.core.qa.golden_verify import run

    with tempfile.TemporaryDirectory() as td:
        v = write_all(design, Path(td), "T_MINI")
        # 迷你黄金：partandpin（U7 C8/F19 + R1 1/2 + GND_1 1）
        exp_pp = Path(td) / "pp.EXP"
        with open(exp_pp, "w", encoding="gbk", newline="") as f:
            w = _csv.writer(f, delimiter="\t")
            w.writerow(["HEADER", "ID", "Part Reference", "Net Name", "Number"])
            w.writerow(["PARTINST", "1", "U7", "", ""])
            w.writerow(["PININST", "U7:C8", "U7", "GPIO_46", "C8"])
            w.writerow(["PININST", "U7:F19", "U7", "PFI", "F19"])
            w.writerow(["PARTINST", "2", "R1", "", ""])
            w.writerow(["PININST", "R1:1", "R1", "3V3_SOC", "1"])
            w.writerow(["PININST", "R1:2", "R1", "GND", "2"])
        # 迷你纯 part EXP（R1 Value/坐标/分类）
        exp_c = Path(td) / "c.EXP"
        with open(exp_c, "w", encoding="gbk", newline="") as f:
            w = _csv.writer(f, delimiter="\t")
            w.writerow(["HEADER", "ID", "Part Reference", "Value", "CATEGORY_NAME",
                        "Location X-Coordinate", "Location Y-Coordinate",
                        "额定功率(W)", "阻值允许偏差", "MFGREGION", "NC"])
            w.writerow(["PARTINST", "1", "R1", "10K", "<null>", "100", "200",
                        "0.1", "+/-1%", "<null>", "<null>"])
        # 迷你 flatnet（3V3_SOC + GND）
        fn = Path(td) / "f.EXP"
        with open(fn, "w", encoding="gbk", newline="") as f:
            w = _csv.writer(f, delimiter="\t")
            w.writerow(["FLATNET:3V3_SOC", "1", "<null>", "<null>", "Default",
                        "<null>", "True", "Default", "Default", "<null>",
                        "1", "3V3_SOC"] + ["<null>"] * 9)
            w.writerow(["FLATNET:GND", "2", "<null>", "<null>", "Default",
                        "<null>", "True", "Default", "Default", "<null>",
                        "2", "GND"] + ["<null>"] * 9)
        rep = run(v["components_csv"], v["pins_csv"], v["nets_csv"],
                  exp_c, exp_pp, fn)
        assert rep["all_passed"] is True
        assert rep["pins"]["rate"] == 100.0
        assert rep["nets"]["rate"] == 100.0
        assert rep["components"]["rate"] == 100.0
