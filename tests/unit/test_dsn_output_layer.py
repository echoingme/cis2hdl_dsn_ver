"""§9 输出模块 + C2 本地等价单元测试（构造数据，纯函数口径）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from cis2hdl.core.ir.component import ComponentDef, ComponentInstanceIR
from cis2hdl.core.ir.design import DesignIR, NetIR, PageIR


def _design() -> DesignIR:
    u7 = ComponentInstanceIR(
        refdes="U7", library_id="HI5662.Normal", section=6,
        loc_x=1010, loc_y=110,
        pin_connections={"C8": "GPIO_46"},
    )
    gnd = ComponentInstanceIR(
        refdes="GND_1", library_id="GND_SYMBOL.Normal", loc_x=550, loc_y=870,
        pin_connections={"1": "GND"},
    )
    page = PageIR(
        page_id="1.1", page_name="01_TOP",
        instances=[u7, gnd],
        nets=[NetIR(name="GND", net_id=1), NetIR(name="GPIO_46", net_id=2)],
        text_elements=[{"page": "01", "x": 400, "y": 327, "text": "Sheet 3",
                        "font": 8, "rotation": 0}],
        graphic_elements=[{"kind": "box", "x": 100, "y": 100, "x2": 300,
                           "y2": 200}],
        source_symbols=[
            {"kind": "ground_symbol", "name": "GROUND_SYMBOL", "net": "",
             "x": 1070, "y": 430, "rotation": 0, "mirror": 0},
            {"kind": "power_symbol", "name": "POWER_SYMBOL", "net": "3V3_SOC",
             "x": 500, "y": 700},
        ],
    )
    ir = DesignIR(project_name="T", pages=[page], global_nets=["GND"])
    comp = ComponentDef(
        library_id="HI5662.Normal", part_name="HI5662.Normal",
        symbol_geometry={"body_box": [0, 0, 600, 400],
                         "pin_lines": [[0, 0, 10, 290, "1"],
                                       [100, 0, 110, 290, "2"]],
                         "texts": [{"text": "VSS", "x": 100, "y": 300}],
                         "geometry_source": "cache"},
    )
    ir.component_db.add(comp)
    return ir


@pytest.fixture()
def design() -> DesignIR:
    return _design()


# ═══ text_element_writer（§9.1） ═══

def test_text_element_writer(design):
    from cis2hdl.core.writer import text_element_writer as tew

    lines = tew.page_elements_to_csa(design.pages[0])
    joined = "\n".join(lines)
    assert 'T 400 325 0 8 "Sheet 3"' in joined       # 25 栅格吸附 327→325
    assert "B 100 100 300 200" in joined              # 矩形
    # pin/body kind 跳过
    page2 = design.pages[0].model_copy(deep=True)
    page2.symbol_texts = [{"kind": "pin", "text": "C8", "x": 1, "y": 1},
                          {"kind": "text", "text": "DDR", "x": 50, "y": 60}]
    lines2 = tew.symbol_texts_to_csa(page2.symbol_texts)
    assert len(lines2) == 1  # 仅 kind=text 输出（pin 跳过）
    assert "DDR" in lines2[0]


# ═══ source_symbol_writer（§9.2） ═══

def test_source_symbol_writer(design):
    from cis2hdl.core.writer import source_symbol_writer as ssw

    plans = ssw.build_all_source_symbol_plan(design)
    assert len(plans) == 2
    gnd = [p for p in plans if p.kind == "ground_symbol"][0]
    assert gnd.x == 1070 and gnd.y == 430
    lines = ssw.to_forceadd_lines(plans)
    assert lines[0].startswith("FORCEADD GND GND (1070 430) 0")  # 网名回填
    assert "FORCEADD POWER_3V3_SOC 3V3_SOC (500 700) 0" in lines[1]
    stats = ssw.source_symbol_stats(design)
    assert stats["total"] == 2


# ═══ gnd_placement（§9.3） ═══

def test_gnd_placement(design):
    from cis2hdl.core.writer import gnd_placement as gp

    st = gp.gnd_stats(design)
    assert st["total"] == 1
    assert st["unique_refdes"] == 1
    assert st["nets"] == {"GND": 1}
    lines = gp.to_forceadd_lines(gp.build_gnd_placements(design))
    assert lines[0] == "FORCEADD GND GND (550 870) 0"


# ═══ dsn_geometry 嵌套几何 + C/X 格式（C2 修复回归） ═══

def test_dsn_geometry_nested_and_format(design):
    from cis2hdl.core.writer import dsn_geometry

    comp = design.component_db.get_by_library_id("HI5662.Normal")
    css = dsn_geometry.symbol_geometry_to_css(comp.symbol_geometry)
    joined = "\n".join(css)
    assert 'P "CDS_LMAN_SYM_OUTLINE" "0,400,600,0"' in joined
    assert "L 0 0 10 290 -1 0" in joined
    # C 行末 justify ∈ {R,L}；X 类型 PIN_TEXT（C2 铁律）
    c_lines = [l for l in css if l.startswith("C ")]
    assert c_lines and all(l.rstrip().endswith(("L", "R")) for l in c_lines)
    x_lines = [l for l in css if l.startswith("X ")]
    assert x_lines and all('"PIN_TEXT"' in l for l in x_lines)

    # 扁平形态兼容
    flat = [0, 0, 10, 290, 100, 0, 110, 290]
    css2 = dsn_geometry.symbol_geometry_to_css({"body_box": [0, 0, 600, 400],
                                                "pin_lines": flat})
    assert any(l.startswith("L ") for l in css2)


# ═══ mock_rebuilder entity 结构（C2 修复回归） ═══

def test_mock_rebuilder_entity_structure(design):
    from cis2hdl.core.matcher.source_builder import build_source_components
    from cis2hdl.core.writer.mock_rebuilder import MockRebuilder
    from cis2hdl.core.writer.validate_symbol_css import (
        validate_symbol_css, validate_temp_lib_structure,
    )

    sources = build_source_components(design)
    unmatched = [s for s in sources if s.extra_data.get("symbol_geometry", {}).get("body_box")]
    rb = MockRebuilder()
    with tempfile.TemporaryDirectory() as td:
        files = rb.write_to_temp_lib(unmatched, Path(td))
        css = [f for f in files if f.name == "symbol.css"][0]
        assert validate_symbol_css(css.read_text(), str(css)) == []
        # 2026-08-21：mock 库名由配置驱动（TG0C0D2_VB_lib），
        # 测试用 rb.lib_name 而非硬编码 temp_lib。
        struct_errs = validate_temp_lib_structure(Path(td) / rb.lib_name)
        assert struct_errs == []
    assert rb.stats.cells == 1


# ═══ route_preview + config 注册 ═══

def test_route_preview(design):
    from cis2hdl.core.matcher.result_router import route
    from cis2hdl.core.matcher.source_builder import build_source_components
    from cis2hdl.core.qa.route_preview import preview
    from cis2hdl.core.ir.match import MatchResult, MatchStrategy

    sources = build_source_components(design)
    results = [MatchResult(confidence=0.0, strategy=MatchStrategy.NEEDS_REVIEW,
                           source_library_id=s.library_id) for s in sources]
    plan = route(results, sources, design)
    txt = preview(design, sources, plan, max_rows=5)
    assert "R5 GND 1" in txt
    assert "GND_1" in txt


def test_output_config_archive_flags():
    from cis2hdl.core.config import OutputConfig

    c = OutputConfig()
    assert c.ir_json is True
    assert c.view_csv is True


def test_write_all_archive(design):
    from cis2hdl.core.intermediate.view_layer import write_all_archive

    with tempfile.TemporaryDirectory() as td:
        r = write_all_archive(design, Path(td), "T")
        for k in ("ir_json", "summary_md", "bom_csv",
                  "components_csv", "pins_csv", "nets_csv"):
            assert Path(r[k]).exists(), k
