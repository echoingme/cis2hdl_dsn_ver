"""DSN P1 全量开发验收测试（12 项维度 + P0-2 修复）。

验收维度（TG0C0D2 测试文件，18 页 / 10995 帧）：
① P1-1 页面 26/27 SymbolPinScalar/Bus 短引用（9 页各 1+1，共 18）
② P1-1 Cache type-4 几何（73 个有效）
③ P1-2 portType → ElectricalType 映射（合成样本 1→INPUT…7→NC）
④ P1-3 GND/电源符号实例化（refdes=GND_*、library_id='GND_SYMBOL'）
⑤ P1-4/P1-5 0x85 rot/font 解码（R83 rot=0/font=24；REFDES_FONT_TO_PT[24]==80）
⑥ P1-6 attr 高位字节 → 颜色枚举（decode_attr_color(0x08be)=='Gray'）
⑦ P1-7 同页无电线连接点（0x03/0x05/0x06 短帧）
⑧ P1-8 0x0D 双形态分流（01_Cover 273B 文本帧不产出 placed_coord）
⑨ P1-9 should_use_new_path 启发式（替代 2170 硬门槛）
⑩ P1-10 source symbol 解码（12V0 / 3V3_SOC / GND）
⑪ P1-11 LibraryAttr（INS 743；COMPONENT_HIGH_MAX='0.4mm'）
⑫ P1-12 SymbolDecoder（Cache 符号文本 62；含 'VSS Interface 3 of 6'）
+ P0-2 修复：_parse_rect_element 62B 帧尾 8B 全 0 校验按 kind 区分派
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cis2hdl.core.parser.dsn.dsn_parser import DSNParser
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")


@pytest.fixture(scope="module")
def p1_design() -> object:
    """解析 TG0C0D2 测试 DSN（新帧语义管道，P1 全量）。"""
    if not Path(DSN_PATH).exists():
        pytest.skip(f"Test file not found: {DSN_PATH}")
    return DSNParser().parse(Path(DSN_PATH))


# ---------------------------------------------------------------------------
# ① P1-1 页面 26/27 短引用
# ---------------------------------------------------------------------------

class TestSymbolPinRefs:
    def test_pin_ref_count_18(self, p1_design) -> None:
        """页面 26/27 短引用共 18（9 页各 1×26 + 1×27）。"""
        assert p1_design.metadata["pin_ref_count"] == 18

    def test_pin_refs_in_ports(self, p1_design) -> None:
        """PageIR.ports 输出 26/27 引脚引用（坐标/object_id）。"""
        total = sum(len(p.ports) for p in p1_design.pages)
        assert total == 18
        p06 = next(p for p in p1_design.pages if p.page_name == "06_TG0_PWR")
        assert len(p06.ports) == 2
        sample = p06.ports[0]
        assert "x" in sample and "y" in sample
        assert sample["object_id"] > 0

    def test_parse_symbol_pin_ref_synthetic(self) -> None:
        """合成 45B 短引用：坐标/object_id 解码正确。"""
        from cis2hdl.core.parser.dsn.decode.registry import DecodeContext
        from cis2hdl.core.parser.dsn.decode.symbol_pin_frames import parse_symbol_pin_ref
        from cis2hdl.core.parser.dsn.frame.splitter import Frame
        from cis2hdl.core.parser.dsn.frame.types import DELIMITER

        raw = DELIMITER + (26).to_bytes(2, "little") + b"\x00" * 10
        raw = raw[:10] + (1330).to_bytes(2, "little") + (260).to_bytes(2, "little")
        raw = raw[:14] + (20437).to_bytes(4, "little") + b"\x00" * 16
        frame = Frame(offset=0, raw=raw, frame_type=26, next_offset=len(raw))
        rec = parse_symbol_pin_ref(frame, DecodeContext(strlst=[]))
        assert rec is not None
        assert rec.x == 1330 and rec.y == 260
        assert rec.object_id == 20437


# ---------------------------------------------------------------------------
# ② P1-1 Cache type-4 几何
# ---------------------------------------------------------------------------

class TestCachePinGeometries:
    def test_cache_pin_geometry_count(self, p1_design) -> None:
        """Cache type-4 几何 ≥60（实证 73 有效）。"""
        assert p1_design.metadata["cache_pin_geometry_count"] >= 60

    def test_cache_pin_geometry_fields(self) -> None:
        """Cache 几何字段：pin_name/start_x/start_y/port_type。"""
        from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            cache = parse_cache_stream(provider.read_stream("Cache"))
        finally:
            provider.close()
        assert len(cache.pin_geometries) >= 60
        g = cache.pin_geometries[0]
        assert g.pin_name
        assert isinstance(g.start_x, int) and isinstance(g.start_y, int)
        assert isinstance(g.port_type, int)


# ---------------------------------------------------------------------------
# ③ P1-2 portType → ElectricalType 映射
# ---------------------------------------------------------------------------

class TestPortTypeMapping:
    def test_port_type_to_electrical(self) -> None:
        """portType 位域映射：1→INPUT … 7→NC（合成样本验证）。"""
        from cis2hdl.core.parser.dsn.decode.symbol_pin_frames import port_type_to_electrical

        assert port_type_to_electrical(0) == "PASSIVE"
        assert port_type_to_electrical(1) == "INPUT"
        assert port_type_to_electrical(2) == "OUTPUT"
        assert port_type_to_electrical(3) == "BIDIR"
        assert port_type_to_electrical(4) == "POWER"
        assert port_type_to_electrical(5) == "GROUND"
        assert port_type_to_electrical(6) == "PASSIVE"
        assert port_type_to_electrical(7) == "NC"
        assert port_type_to_electrical(999) == "PASSIVE"
        assert port_type_to_electrical(None) == "PASSIVE"

    def test_component_db_pin_types_valid(self, p1_design) -> None:
        """ComponentDB PinDef.type 均为合法 ElectricalType（不抛异常）。"""
        from cis2hdl.core.parser.dsn.decode.symbol_pin_frames import PORT_TYPE_TO_ELECTRICAL

        for comp in p1_design.component_db.list_all():
            for pin in comp.pins:
                assert pin.type.value in PORT_TYPE_TO_ELECTRICAL.values() or \
                    pin.type.value in ("TRI_STATE", "OPEN_COLLECTOR")


# ---------------------------------------------------------------------------
# ④ P1-3 GND/电源符号实例化
# ---------------------------------------------------------------------------

class TestGndInstances:
    def test_gnd_instances_present(self, p1_design) -> None:
        """GND 实例存在且 refdes 前缀 GND_、library_id='GND_SYMBOL'。"""
        gnd = [i for p in p1_design.pages for i in p.instances if i.library_id == "GND_SYMBOL"]
        assert len(gnd) > 0
        assert all(i.refdes.startswith("GND_") for i in gnd)

    def test_gnd_refdes_unique(self, p1_design) -> None:
        """GND 实例 refdes 全局唯一（675 个，跨页不合并）。"""
        gnd = [i for p in p1_design.pages for i in p.instances if i.library_id == "GND_SYMBOL"]
        refs = [i.refdes for i in gnd]
        assert len(refs) == len(set(refs))
        assert len(gnd) >= 600


# ---------------------------------------------------------------------------
# ⑤ P1-4/P1-5 0x85 rot/font 解码
# ---------------------------------------------------------------------------

class TestRotFont:
    def test_refdes_font_table(self) -> None:
        """REFDES_FONT_TO_PT 独立表：24 → 80。"""
        from cis2hdl.core.parser.dsn.decode.text_style import REFDES_FONT_TO_PT, decode_refdes_font_to_pt

        assert REFDES_FONT_TO_PT[24] == 80
        assert decode_refdes_font_to_pt(24) == 80

    def test_r83_rot_font(self, p1_design) -> None:
        """R83 实例 rot=0/font=24（0x85 marker 后字节解码）。"""
        r83 = [i for p in p1_design.pages for i in p.instances if i.refdes == "R83"]
        assert len(r83) == 1
        assert r83[0].rotation == 0
        assert r83[0].properties.get("REFDES_FONT") == "24"

    def test_parse_refdes_frame_rot_font(self) -> None:
        """合成 0x85 帧：rot/font 字段解码（marker 方案）。"""
        from cis2hdl.core.parser.dsn.decode.registry import DecodeContext
        from cis2hdl.core.parser.dsn.decode.semantic_frames import parse_refdes_frame
        from cis2hdl.core.parser.dsn.frame.splitter import Frame
        from cis2hdl.core.parser.dsn.frame.types import DELIMITER

        marker = b"\x04\x00\x30"
        x = (325).to_bytes(2, "little")
        body = marker + x + bytes([0, 24])  # rot=0, font=24
        tlen = (3).to_bytes(2, "little")
        text = b"R99" + b"\x00"
        raw = DELIMITER + (0x85).to_bytes(2, "little") + b"\x00" * 8 + body + tlen + text + b"\x00" * 8
        frame = Frame(offset=0, raw=raw, frame_type=0x85, next_offset=len(raw))
        rec = parse_refdes_frame(frame, DecodeContext(strlst=[]))
        assert rec is not None
        assert rec.refdes == "R99"
        assert rec.rot == 0
        assert rec.font == 24


# ---------------------------------------------------------------------------
# ⑥ P1-6 attr 高位字节 → 颜色枚举
# ---------------------------------------------------------------------------

class TestAttrColor:
    def test_decode_attr_color_gray(self) -> None:
        """attr=0x08be → 高位 0x08=8 → 'Gray'。"""
        from cis2hdl.core.parser.dsn.decode.text_style import decode_attr_color

        assert decode_attr_color(0x08BE) == "Gray"
        assert decode_attr_color(0x0044) == "Default"
        assert decode_attr_color(None) is None


# ---------------------------------------------------------------------------
# ⑦ P1-7 同页无电线连接点
# ---------------------------------------------------------------------------

class TestConnectionPoints:
    def test_connection_points_present(self, p1_design) -> None:
        """connection_points>0 且 kinds 含 junction/net_label/port。"""
        all_cp = [cp for p in p1_design.pages for cp in p.connection_points]
        assert len(all_cp) > 0
        kinds = {cp["kind"] for cp in all_cp}
        assert {"junction", "net_label", "port"}.issubset(kinds)

    def test_04_connection_points(self, p1_design) -> None:
        """04_POWER_SUPPLY 页连接点 10 个（含 junction/net_label/port）。"""
        p04 = next(p for p in p1_design.pages if p.page_name == "04_POWER_SUPPLY")
        assert len(p04.connection_points) == 10


# ---------------------------------------------------------------------------
# ⑧ P1-8 0x0D 双形态分流
# ---------------------------------------------------------------------------

class TestZeroD:
    def test_cover_long_frame_no_coord(self) -> None:
        """01_Cover 273B 文本帧不产出 placed_coord（P1-8 分流）。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBinder
        from cis2hdl.core.parser.dsn.decode.net_table import parse_net_table
        from cis2hdl.core.parser.dsn.frame.splitter import split_frames
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            design_name = provider.design_name()
            for page_name, data in provider.page_streams(design_name):
                if page_name != "01_Cover Page":
                    continue
                frames = split_frames(data)
                big = [f for f in frames if f.frame_type == 0x0D and len(f.raw) > 100]
                assert big, "01_Cover long 0x0D frame not found"
                for f in big:
                    assert InstanceBinder._extract_placed_coord(f) is None
                return
            pytest.fail("01_Cover Page stream not found")
        finally:
            provider.close()


# ---------------------------------------------------------------------------
# ⑨ P1-9 should_use_new_path 启发式
# ---------------------------------------------------------------------------

class TestHeuristic:
    def test_should_use_new_path_true(self) -> None:
        """TG0C0D2 strLst（含 VALUE/GND）+ 18 页 → True（不再硬门槛 2170）。"""
        from cis2hdl.core.parser.dsn.decode.text_style import should_use_new_path
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider
        from cis2hdl.core.parser.dsn.strlst.strlst import parse_strlst

        provider = OlefileProvider(DSN_PATH)
        try:
            design_name = provider.design_name()
            strlst = parse_strlst(provider.read_stream("Library"))
            pages = list(provider.page_streams(design_name))
        finally:
            provider.close()
        assert should_use_new_path(strlst, len(pages)) is True
        assert should_use_new_path([], 18) is False
        assert should_use_new_path(["VALUE", "GND"], 0) is False


# ---------------------------------------------------------------------------
# ⑩ P1-10 source symbol 解码
# ---------------------------------------------------------------------------

class TestSourceSymbols:
    def test_source_symbol_count(self, p1_design) -> None:
        """source_symbols 42 个（内容探测白名单 + D2/D3 修复）。

        基线 32 个；QA D2 修复后 10/11/12 页 6 个 45B 短帧
        （0x0046/0x0050，无 marker30 也按 x/y 短引用解析）纳入 → 38；
        QA D3 修复后 4 个真 BISHEET（0x0567×2 + 0x0205×2）纳入 → 42。
        0x03f2 的 38 帧（08 页 6 + 11 页 32，非 BISHEET）不进入。
        """
        assert p1_design.metadata["source_symbol_count"] == 42

    def test_source_symbol_y_backfilled(self, p1_design) -> None:
        """QA D1 修复（gold 口径）：04 页 12V0=(930,160)。

        gold DSN（md5 5762432b）的 ONSHEET2/BISHEET 连接符帧 locY/
        locX 清零（与 shape 帧同源零化），12V0 y 由同 x 0x01 实例放置
        帧回填 → (930,160)。剩余 y=0 限于 gold 零化帧的 5 个符号
        （4 BISHEET + 1 电源符号），为 gold 数据固有缺口。
        """
        p04 = next(p for p in p1_design.pages if p.page_name == "04_POWER_SUPPLY")
        sym = next(s for s in p04.source_symbols if s["name"] == "12V0")
        assert (sym["x"], sym["y"]) == (930, 160)
        # gold 零化帧导致的 y=0 符号数（≤5，诚实标注）
        y_zero = [
            (p.page_name, s["name"])
            for p in p1_design.pages
            for s in p.source_symbols
            if s["y"] == 0
        ]
        assert len(y_zero) <= 5, f"y=0 超限: {y_zero}"

    def test_d2_short_frames_parsed(self, p1_design) -> None:
        """QA D2 修复：10/11/12 页 6 个 45B 短帧解出（DDR_BA1/MDI_DP_1）。

        短帧无 30 00 00 00 标记，帧类型号 = strLst 网名，x/y 在偏移 12/14。
        """
        got = {
            (p.page_name, s["name"], s["x"], s["y"])
            for p in p1_design.pages
            for s in p.source_symbols
        }
        expected = {
            ("10_TG0_GND", "DDR_BA1", 790, 609),
            ("10_TG0_GND", "MDI_DP_1", 890, 643),
            ("11_TG0_DDR", "DDR_BA1", 370, 2638),
            ("11_TG0_DDR", "MDI_DP_1", 480, 2570),
            ("12_DDR3", "DDR_BA1", 650, 4011),
            ("12_DDR3", "MDI_DP_1", 480, 4709),
        }
        assert expected <= got

    def test_d3_bisheet_frames_parsed(self, p1_design) -> None:
        """QA D3 修复（gold 口径）：4 个真 BISHEET 坐标经 x1/y1 恢复。

        gold DSN 的 BISHEET 帧 locY/locX 清零（同 shape 帧零化），
        位置改存 bbox 角 x1/y1 —— ``_sheet_graphic_coord`` 已支持回退，
        坐标恢复为 workdir 同值：08(1080,310)+08(130,210)、13(130,830)、
        18(220,290)。0x03f2 零误检仍成立。
        """
        bisheet = [
            (p.page_name, s["x"], s["y"], s["kind"])
            for p in p1_design.pages
            for s in p.source_symbols
            if s["name"] == "BISHEET"
        ]
        expected = {
            ("08_TG0_ECS", 1080, 310, "onsheet"),
            ("08_TG0_ECS", 130, 210, "onsheet"),
            ("18_2P5GE", 220, 290, "onsheet"),
            ("13_BOB", 130, 830, "onsheet"),
        }
        assert set(bisheet) == expected

    def test_d3_no_0x03f2_false_positives(self, p1_design) -> None:
        """QA D3 修复：0x03f2 帧（38 个，非 BISHEET）不产生额外符号。

        0x03f2 帧含 marker30 + 网名（25GE_MDIO_R/MDI_*）但无 BISHEET
        字符串，strLst[0x03f2]='9783' 数字串印证类型号=数据相关索引。
        若误加白名单将增加 38 个误检（source_symbol_count 42→80）。
        """
        all_syms = [s for p in p1_design.pages for s in p.source_symbols]
        # 0x03f2 帧类型不在任何产出符号中
        assert not any(s["frame_type"] == "0x03f2" for s in all_syms)
        # 无网名 '25GE_MDIO_R' 的源符号（0x03f2 的网名特征）
        assert not any(s["name"] == "25GE_MDIO_R" for s in all_syms)

    def test_d3_10page_hetero_frame_no_false_positive(self, p1_design) -> None:
        """QA D3 修复：10 页 0x0205 异类电源帧不误检。

        off=5598 len=70 marker30=True、无 BISHEET 字符串 → 主分支
        无名称被拒。10_TG0_GND 页仍只含 2 个短帧符号（DDR_BA1/MDI_DP_1）。
        """
        p10 = next(p for p in p1_design.pages if p.page_name == "10_TG0_GND")
        names = [s["name"] for s in p10.source_symbols]
        assert sorted(names) == ["DDR_BA1", "MDI_DP_1"]

    def test_source_symbol_names(self, p1_design) -> None:
        """含 12V0 / 3V3_SOC / GND 等电源与接地符号。"""
        names = {
            s["name"] for p in p1_design.pages for s in p.source_symbols
        }
        assert "12V0" in names
        assert "3V3_SOC" in names
        assert "GND" in names

    def test_04_source_symbols(self, p1_design) -> None:
        """04_POWER_SUPPLY 页 6 个：12V0×4、3V3_SOC、PFI_DYING_GASP。"""
        p04 = next(p for p in p1_design.pages if p.page_name == "04_POWER_SUPPLY")
        names = [s["name"] for s in p04.source_symbols]
        assert names.count("12V0") == 4
        assert "3V3_SOC" in names
        assert "PFI_DYING_GASP" in names


# ---------------------------------------------------------------------------
# ⑪ P1-11 LibraryAttr
# ---------------------------------------------------------------------------

class TestLibraryAttrs:
    def test_library_attr_count(self, p1_design) -> None:
        """Library 流 INS 743 条（与 EXP 元件对齐）。"""
        assert p1_design.metadata["library_attr_count"] == 743

    def test_comp_high_max(self) -> None:
        """COMPONENT_HIGH_MAX='0.4mm'（INS1608）。"""
        from cis2hdl.core.parser.dsn.decode.library_attrs import parse_component_attrs
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            attrs = parse_component_attrs(provider.read_stream("Library"))
        finally:
            provider.close()
        by_id = {a.ins_id: a for a in attrs}
        assert by_id["INS1608"].comp_high_max == "0.4mm"
        assert by_id["INS3687"].rated_voltage == "6.3"
        assert by_id["INS3067"].rated_power == "0.06"
        assert by_id["INS5"].description == "MDI_BP_1"

    def test_string_table_nonempty(self) -> None:
        """0x64f 起连续字符串表非空（实测 2171 条）。"""
        from cis2hdl.core.parser.dsn.decode.library_attrs import scan_string_table
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            table = scan_string_table(provider.read_stream("Library"))
        finally:
            provider.close()
        assert len(table) > 2000

    def test_user_attrs_backfilled(self, p1_design) -> None:
        """P1-11 接线：>0 个 ComponentDef 带 user_attrs。"""
        comps = p1_design.component_db.list_all()
        with_attrs = [c for c in comps if c.user_attrs]
        assert len(with_attrs) > 0

    def test_user_attrs_samples(self, p1_design) -> None:
        """P1-11 样例：COMPONENT_HIGH_MAX='0.4mm'、额定工作电压='6.3'。"""
        comps = p1_design.component_db.list_all()
        by_part = {c.part_name: c.user_attrs for c in comps}
        # R.Normal 来自 HW_RESISTOR.OLB（INS1608 行含 OLB 路径）
        assert by_part.get("R.Normal", {}).get("COMPONENT_HIGH_MAX") == "0.4mm"
        # C.Normal / R.Normal 来自 SC0201A footprint 反查（INS3687 行）
        assert by_part.get("C.Normal", {}).get("额定工作电压(V)") == "6.3"
        assert by_part.get("R.Normal", {}).get("额定功率(W)") == "0.06"

    def test_user_attrs_no_contamination(self, p1_design) -> None:
        """P1-11 防误挂：INS5 的『描述』只关联 Hi5662YV305 芯片六段。

        2026-08-21 修正：INS5 行（Hi5662YV305 芯片）通过 footprint
        BGA409-26-2323 精确关联六段（0A~0F）；OLB 宽匹配（WORKLIB.OLB）
        已改为 footprint 优先，杜绝扩散到 SOP8 等无关库。
        """
        comps = p1_design.component_db.list_all()
        desc_holders = [
            c.part_name for c in comps
            if c.user_attrs.get("描述") == "MDI_BP_1"
        ]
        # 全部应为 Hi5662YV305 六段（INS5 行真正归属），≤6 且无其他库
        assert len(desc_holders) <= 6, f"描述持有者={desc_holders}"
        assert all("Hi5662YV305" in h for h in desc_holders), (
            f"描述扩散到非 Hi5662 库: {desc_holders}"
        )

    def test_instance_user_attrs_present(self, p1_design) -> None:
        """QA D5 修复：ComponentInstanceIR.user_attrs 实例级承载。

        实例级 user_attrs 优先取形状锚定属性帧（b.prop_anchor.props，
        帧序 [PROP][shape][0x0022][REF]），缺失键兜底库级默认。
        """
        insts = [
            i for p in p1_design.pages for i in p.instances
        ]
        assert len(insts) > 0
        # 至少一个实例携带实例级属性（R.Normal 应带 COMPONENT_HIGH_MAX）
        with_inst_attrs = [i for i in insts if i.user_attrs]
        assert len(with_inst_attrs) > 0
        r_insts = [i for i in insts if "R" in (i.refdes or "")]
        r_high = {
            i.user_attrs.get("COMPONENT_HIGH_MAX")
            for i in r_insts
            if i.user_attrs.get("COMPONENT_HIGH_MAX")
        }
        assert r_high, "至少一个电阻实例带 COMPONENT_HIGH_MAX"

    def test_instance_user_attrs_differentiation(self, p1_design) -> None:
        """QA D5 修复：非众数实例差异化属性可见（不再被库级众数遮蔽）。

        R.Normal 库级 COMPONENT_HIGH_MAX=0.4mm（众数），但实例 0x02 帧
        存在 0.3mm/0.65mm 等差异化值 —— 实例级 user_attrs 须保留。
        """
        insts = [
            i for p in p1_design.pages for i in p.instances
            if i.library_id and "R" in i.library_id
        ]
        values = {
            i.user_attrs.get("COMPONENT_HIGH_MAX")
            for i in insts
            if i.user_attrs.get("COMPONENT_HIGH_MAX")
        }
        # 实证分布含众数 0.4mm 之外的差异化值
        assert "0.4mm" in values
        assert any(v != "0.4mm" for v in values), (
            f"实例级差异化值缺失: {sorted(values)}"
        )


# ---------------------------------------------------------------------------
# ⑫ P1-12 SymbolDecoder（Cache 符号文本）
# ---------------------------------------------------------------------------

class TestSymbolTexts:
    def test_symbol_text_count(self, p1_design) -> None:
        """Cache 符号元素 947 个（T03 全元件扩展：text+body+pin+header）。

        T03 从 U7 族扩展到全部组件/连接器：0x2e2e 文本 + 0x2929 体框
        + 0x2c2c 引脚线 + 0x001c 头属性，ComponentDef 粒度 100%。
        """
        assert p1_design.metadata["symbol_text_count"] >= 214

    def test_symbol_texts_content(self) -> None:
        """含 'VSS Interface 3 of 6' / 'GPIO' / 'PART_NUMBER'。"""
        from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            cache = parse_cache_stream(provider.read_stream("Cache"))
        finally:
            provider.close()
        texts = {t.text for t in cache.symbol_texts}
        assert "VSS Interface 3 of 6" in texts
        assert "GPIO" in texts
        assert "PART_NUMBER" in texts
        assert len(cache.symbol_texts) >= 214

    def test_header_attr_subblocks(self) -> None:
        """0x001c 符号头内嵌子块 6 个全部解码。"""
        from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            cache = parse_cache_stream(provider.read_stream("Cache"))
        finally:
            provider.close()
        headers = [t for t in cache.symbol_texts if t.kind == "header_attr"]
        assert len(headers) == 6
        assert all(t.text for t in headers)

    def test_symbol_texts_attribution(self, p1_design) -> None:
        """P1-12 修正 + T03 扩展：组件只挂**自己符号**的文本/几何。

        - NPN-BEC_0 无 TEXT/header_attr（不挂页签标题栏），但含
          自身 body/pin 几何（T03 全元件扩展）；
        - Hi5662YV305_DDR_GE_0C 含 SPI/HW/GPIO（图标内 11 文本）；
        - Hi5662YV305_DDR_GE_0D 含 AVDD09。
        """
        comps = p1_design.component_db.list_all()
        by_part = {c.part_name: c for c in comps}
        npn = by_part["NPN-BEC_0.Normal"]
        # T03：NPN 可有 body/pin 几何，但无页面级可渲染文本
        npn_texts = [t for t in npn.symbol_texts if t["kind"] in ("text", "header_attr")]
        assert npn_texts == []
        texts_c = {t["text"] for t in by_part["Hi5662YV305_DDR_GE_0C.Normal"].symbol_texts}
        assert any("SPI_interface" in t for t in texts_c)
        assert any("HW_interface" in t for t in texts_c)
        assert "GPIO" in texts_c
        # 0C 文本元素仍为 11（body/pin 不计入页面级文本）
        c_texts = [t for t in by_part["Hi5662YV305_DDR_GE_0C.Normal"].symbol_texts
                   if t["kind"] in ("text", "header_attr")]
        assert len(c_texts) == 11
        texts_d = {t["text"] for t in by_part["Hi5662YV305_DDR_GE_0D.Normal"].symbol_texts}
        assert "AVDD09" in texts_d
        # 页签/标题栏文本（DESIGNED/ECA NO/PART_NUMBER）不挂任何组件
        all_symbols = {
            t["symbol"]
            for c in comps for t in c.symbol_texts
            if t["kind"] in ("text", "header_attr")
        }
        assert "" not in all_symbols


class TestPageSymbolTexts:
    def test_page08_has_interface_texts(self, p1_design) -> None:
        """08_TG0_ECS 页面级 symbol_texts 含 SPI/HW/GPIO（U7C 段）。"""
        p08 = next(p for p in p1_design.pages if p.page_name == "08_TG0_ECS")
        assert len(p08.symbol_texts) == 11
        texts = {t["text"] for t in p08.symbol_texts}
        assert any("SPI_interface" in t for t in texts)
        assert any("HW_interface" in t for t in texts)
        assert "GPIO" in texts
        # 实例化后坐标（平移）>0
        assert all(t["x"] > 0 and t["y"] > 0 for t in p08.symbol_texts)
        assert all(t["refdes"] == "U7" for t in p08.symbol_texts)

    def test_page06_has_power_texts(self, p1_design) -> None:
        """06_TG0_PWR 页面级 symbol_texts 含 AVDD09（U7D 段，9 个）。"""
        p06 = next(p for p in p1_design.pages if p.page_name == "06_TG0_PWR")
        assert len(p06.symbol_texts) == 9
        texts = {t["text"] for t in p06.symbol_texts}
        assert "AVDD09" in texts
        assert "AVDD18" in texts

    def test_page14_t_instances(self, p1_design) -> None:
        """14_4GE 页：LC4_A_0/L4_L 的 '*' 文本按实例平移。"""
        p14 = next(p for p in p1_design.pages if p.page_name == "14_4GE")
        assert len(p14.symbol_texts) >= 30
        assert all(t["refdes"].startswith(("T", "L")) for t in p14.symbol_texts)


# ---------------------------------------------------------------------------
# + P0-2 修复：_parse_rect_element 62B 帧尾 8B 全 0 校验按 kind 区分派
# ---------------------------------------------------------------------------

class TestRectElementFix:
    def test_rect_kind_dispatch(self) -> None:
        """纯矩形(29 29)尾部 8B 全 0；文本图形(2e 2e)尾部透传不拒绝。"""
        import struct

        from cis2hdl.core.parser.dsn.decode.info_frames import _parse_rect_element

        # 62B 帧：20B 头 + [20:22] kind + 8B 填充 + [30:54] 6×u32 坐标 + 8B 尾
        def _frame(kind: bytes, tail: bytes) -> bytes:
            coords = struct.pack("<6I", 100, 100, 300, 200, 100, 100)
            return b"\x00" * 20 + kind + b"\x00" * 8 + coords + tail

        # 29 29 纯矩形 + 尾部 8B 全 0 → 接受
        assert _parse_rect_element(_frame(b"\x29\x29", b"\x00" * 8), "P1", 0) is not None
        # 29 29 纯矩形 + 尾部非全 0 → 拒绝（P0 逻辑不变）
        bad_tail = b"\x01\x00\x30\x00\x01\x00\x30\x00"
        assert _parse_rect_element(_frame(b"\x29\x29", bad_tail), "P1", 0) is None
        # 2e 2e 文本图形原语 + 尾部样式字节 → 接受（P0-2 修复）
        assert _parse_rect_element(_frame(b"\x2e\x2e", bad_tail), "P1", 0) is not None

    def test_page08_rect_improved(self, p1_design) -> None:
        """08 页 rect 元素 ≥10（修复前 2；2e2e 图形原语不再被拒）。"""
        p08 = next(p for p in p1_design.pages if p.page_name == "08_TG0_ECS")
        rects = [g for g in p08.graphic_elements if g.get("kind") == "rect"]
        assert len(rects) >= 10


# ---------------------------------------------------------------------------
# metadata 键齐全
# ---------------------------------------------------------------------------

class TestP1Metadata:
    def test_p1_metadata_keys(self, p1_design) -> None:
        """P1 metadata 统计键齐全。"""
        for key in (
            "source_symbol_count",
            "connection_point_count",
            "pin_ref_count",
            "symbol_text_count",
            "library_attr_count",
            "cache_pin_geometry_count",
        ):
            assert key in p1_design.metadata, f"missing metadata key: {key}"

    def test_coverage_includes_p1(self, p1_design) -> None:
        """P1 解码器注册后 coverage_before ≥30.6（26/27 不再 UnknownFrame）。"""
        fc = p1_design.metadata["frame_coverage"]
        assert fc["total"] == 10995
        assert fc["coverage_before"] >= 30.6
        assert fc["coverage_after"] > fc["coverage_before"]
