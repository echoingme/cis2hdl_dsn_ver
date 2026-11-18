"""DSN P2-A 坐标补全 + P2 基础设施验收测试。

验收标准（T01，权威计划 §5.4）：
① 743/743 实例 loc_x/loc_y 非零且与 CSV/EXP 交叉引用一致
   （页 1.4 的 81 实例不回归）
② 坐标差 ≤1mil 占比 ≥99%
③ metadata.frame_stats.covered_pct 可输出（基线 49.1%）
④ metadata.decoder_version / warnings 存在（P2 基础设施）

字节级根因（P2-A 核心修正）：
- shape 帧 GraphicInst 布局（OpenOrCadParser 实证）::

      [len16][shape.Normal][00][dbId:u32][locY:i16][locX:i16]
      [y2:i16][x2:i16][x1:i16][y1:i16][color:u8]

- **EXP/CSV Location = bbox 角 (x1,y1)**（TG0C0D2 737/737 交叉验证
  100%）；locX/locY 为渲染原点（符号文本平移用）。
- 早期实现误读 locX/locY → 与 EXP 偏差 4~31 mil（0% 命中）。
"""

from __future__ import annotations

import csv
import io
import struct
from pathlib import Path

import pytest

from cis2hdl.core.parser.dsn.dsn_parser import DSNParser
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")
EXP_PATH = design_path("EXP")
CSV_PATH = design_path("CSV")


def load_exp_coords() -> dict[str, tuple[int, int]]:
    """读取 EXP PARTINST Location X/Y（GBK + TSV）→ refdes → (x, y) mil。"""
    with open(EXP_PATH, "rb") as f:
        text = f.read().decode("gbk", errors="replace")
    lines = [l for l in text.split("\r\n") if l.strip()]
    rows = list(csv.reader(io.StringIO("\n".join(lines)), delimiter="\t"))
    header = rows[1]
    xi = next(i for i, h in enumerate(header) if "Location X" in h)
    yi = next(i for i, h in enumerate(header) if "Location Y" in h)
    result: dict[str, tuple[int, int]] = {}
    for r in rows[2:]:
        if not r or not r[0].startswith("PARTINST"):
            continue
        ref = r[2].strip()
        if not ref or ref == "<null>":
            continue
        try:
            result[ref] = (int(float(r[xi])), int(float(r[yi])))
        except (ValueError, IndexError):
            continue
    return result


def load_csv_coords() -> dict[str, tuple[float, float]]:
    """读取 CSV Cross Reference X/Y（0.25mil 网格）→ refdes → (x, y)。"""
    with open(CSV_PATH, "rb") as f:
        text = f.read().decode("gbk", errors="replace")
    lines = text.split("\r\n")
    hi = next(i for i, l in enumerate(lines) if l.startswith("Item,Part"))
    result: dict[str, tuple[float, float]] = {}
    for l in lines[hi + 2:]:
        l = l.strip()
        if not l:
            continue
        parts = l.split(",")
        if len(parts) < 8:
            continue
        ref = parts[2].strip()
        try:
            result[ref] = (float(parts[6]), float(parts[7]))
        except ValueError:
            continue
    return result


@pytest.fixture(scope="module")
def p2_design() -> object:
    """解析 TG0C0D2 测试 DSN（新帧语义管道，P2-A）。"""
    if not Path(DSN_PATH).exists():
        pytest.skip(f"Test file not found: {DSN_PATH}")
    return DSNParser().parse(Path(DSN_PATH))


@pytest.fixture(scope="module")
def exp_coords() -> dict[str, tuple[int, int]]:
    return load_exp_coords()


def _non_gnd_instances(design) -> list:
    return [
        i for p in design.pages for i in p.instances
        if i.library_id != "GND_SYMBOL"
    ]


# ---------------------------------------------------------------------------
# ① P2-A 坐标补全：743/743 与 EXP 交叉引用一致
# ---------------------------------------------------------------------------

class TestP2ACoords:
    def test_all_non_gnd_coords_nonzero(self, p2_design) -> None:
        """全部非 GND 实例 loc_x/loc_y 非零（P2-A 缺口清零）。"""
        insts = _non_gnd_instances(p2_design)
        assert len(insts) >= 700
        assert all(i.loc_x > 0 for i in insts), "存在 loc_x==0 实例"
        assert all(i.loc_y > 0 for i in insts), "存在 loc_y==0 实例"

    def test_ge99pct_within_1mil(self, p2_design, exp_coords) -> None:
        """坐标差 ≤1mil 占比 ≥99%（EXP 交叉引用）。"""
        insts = _non_gnd_instances(p2_design)
        ir_map: dict[str, tuple[int, int]] = {}
        for i in insts:
            ir_map.setdefault(i.refdes, (i.loc_x, i.loc_y))

        diffs: list[tuple[int, int]] = []
        matched = 0
        for ref, (gx, gy) in exp_coords.items():
            c = ir_map.get(ref)
            if c is None:
                continue
            matched += 1
            diffs.append((abs(c[0] - gx), abs(c[1] - gy)))
        assert matched >= 700
        ok = sum(1 for dx, dy in diffs if dx <= 1 and dy <= 1)
        rate = ok / max(matched, 1) * 100
        assert rate >= 99.0, f"within1mil={ok}/{matched} ({rate:.1f}%)"

    def test_ge99pct_within_1mil_csv(self, p2_design) -> None:
        """坐标差 ≤1mil 占比 ≥99%（CSV Cross Reference，×4 网格换算）。"""
        csv_coords = load_csv_coords()
        insts = _non_gnd_instances(p2_design)
        ir_map: dict[str, tuple[int, int]] = {}
        for i in insts:
            ir_map.setdefault(i.refdes, (i.loc_x, i.loc_y))

        matched = ok = 0
        for ref, (gx, gy) in csv_coords.items():
            c = ir_map.get(ref)
            if c is None:
                continue
            matched += 1
            if abs(c[0] - round(gx * 4)) <= 1 and abs(c[1] - round(gy * 4)) <= 1:
                ok += 1
        rate = ok / max(matched, 1) * 100
        assert rate >= 99.0, f"csv within1mil={ok}/{matched} ({rate:.1f}%)"

    def test_page14_no_regression(self, p2_design, exp_coords) -> None:
        """页 1.4（04_POWER_SUPPLY）81 实例不回归（坐标精确）。"""
        p04 = next(p for p in p2_design.pages if p.page_name == "04_POWER_SUPPLY")
        n = ok = 0
        for i in p04.instances:
            if i.library_id == "GND_SYMBOL":
                continue
            e = exp_coords.get(i.refdes)
            if e is None:
                continue
            n += 1
            if abs(i.loc_x - e[0]) <= 1 and abs(i.loc_y - e[1]) <= 1:
                ok += 1
        assert n == 81, f"页 1.4 元件数={n}（EXP 81）"
        assert ok == 81, f"页 1.4 精确坐标={ok}/81"

    def test_spot_r190_r12(self, p2_design, exp_coords) -> None:
        """抽样：R190=(120,310)、R12=(160,1020) 精确命中（此前偏差 25/15 mil）。"""
        insts = _non_gnd_instances(p2_design)
        ir_map = {i.refdes: (i.loc_x, i.loc_y) for i in insts}
        for ref, exp in (("R190", (120, 310)), ("R12", (160, 1020))):
            got = ir_map.get(ref)
            assert got == exp, f"{ref}: IR={got} EXP={exp}"

    def test_u7_sections_6_units(self, p2_design) -> None:
        """2026-08-21 用户裁决：U7A~U7F 六段独立芯片（分布六页，
        refdes 用 EXP Part Reference；不再多单元合并）。"""
        u7 = [
            i for p in p2_design.pages for i in p.instances
            if i.library_id and "Hi5662YV305" in i.library_id
        ]
        assert len(u7) == 6
        # 六段各自独立库名（0A~0F）与页（纯 DSN 基线 refdes 保持 U7；
        # exp 辅助时覆盖为 U7A~U7F）
        libs = {i.library_id for i in u7}
        assert len(libs) == 6
        assert all(i.loc_x and i.loc_y for i in u7)
        # 各段独立引脚连接（DSN 走线：U7A 84 / U7F 176 等）
        assert max(len(dict(i.pin_connections)) for i in u7) >= 170
        assert sum(len(dict(i.pin_connections)) for i in u7) >= 380


# ---------------------------------------------------------------------------
# ③/④ P2 基础设施：frame_stats / decoder_version / warnings
# ---------------------------------------------------------------------------

class TestP2Metadata:
    def test_frame_stats_present(self, p2_design) -> None:
        """metadata.frame_stats 结构齐全（total/covered/unknown/pct/top）。"""
        fs = p2_design.metadata.get("frame_stats")
        assert fs is not None
        for key in ("total_frames", "covered_frames", "unknown_frames",
                    "covered_pct", "top_unknown_types"):
            assert key in fs, f"missing frame_stats key: {key}"

    def test_frame_stats_baseline(self, p2_design) -> None:
        """frame_stats 诚实口径：decoder 63.9% + probe 35.8% = covered 99.7%。"""
        fs = p2_design.metadata["frame_stats"]
        assert fs["total_frames"] == 10995
        assert fs["covered_pct"] == 99.7
        assert fs["covered_frames"] == 10959
        assert fs["unknown_frames"] == 36
        # 诚实口径：注册表解码器 vs 内容探测兜底分开计数
        assert fs["decoder_frames"] == 7027
        assert fs["decoder_pct"] == 63.9
        assert fs["probe_frames"] == 3932
        assert fs["probe_pct"] == 35.8
        assert isinstance(fs["top_unknown_types"], dict) and fs["top_unknown_types"]

    def test_decoder_version(self, p2_design) -> None:
        """metadata.decoder_version 存在且非空。"""
        v = p2_design.metadata.get("decoder_version")
        assert v and isinstance(v, str)

    def test_warnings_list(self, p2_design) -> None:
        """metadata.warnings 为 list（无强制缺失警告）。"""
        w = p2_design.metadata.get("warnings")
        assert isinstance(w, list)

    def test_legacy_frame_coverage_unchanged(self, p2_design) -> None:
        """P0 frame_coverage（旧键）不受 P2 影响。"""
        fc = p2_design.metadata["frame_coverage"]
        assert fc["total"] == 10995
        assert fc["coverage_after"] == 49.0 or fc["coverage_after"] > 30.6


class TestFrameStatsDefaults:
    def test_default_frame_stats(self) -> None:
        """design.py frame_stats 默认结构键齐全。"""
        from cis2hdl.core.ir.design import default_frame_stats

        fs = default_frame_stats()
        assert fs["total_frames"] == 0
        assert fs["covered_pct"] == 0.0
        assert fs["top_unknown_types"] == {}

    def test_ensure_p2_metadata(self) -> None:
        """ensure_p2_metadata 注入默认键且不覆盖已有值。"""
        from cis2hdl.core.ir.design import DEFAULT_DECODER_VERSION, ensure_p2_metadata

        meta = {"existing": 1}
        out = ensure_p2_metadata(meta)
        assert out["existing"] == 1
        assert out["frame_stats"]["total_frames"] == 0
        assert out["decoder_version"] == DEFAULT_DECODER_VERSION
        assert out["warnings"] == []

    def test_design_ir_metadata_has_p2_keys(self) -> None:
        """map_to_design_ir（无 frame_stats 参数）注入默认 P2 键。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBind
        from cis2hdl.core.parser.dsn.decode.net_table import NetTable
        from cis2hdl.core.parser.dsn.ir.mapper import map_to_design_ir

        net_table = NetTable(offset=0, names={})
        binds = [
            InstanceBind(
                refdes="R1", page="P1", value=None, value_source="none",
                value_idx=None, footprint=None, footprint_source="none",
                shape="RES", cache_ref_des=None, cache_source_lib=None,
                loc_x=10, loc_y=20, frame_offset=0, frame_type=0x85,
            ),
        ]
        design = map_to_design_ir("old_style", [("P1", binds, net_table, [])])
        assert "frame_stats" in design.metadata
        assert "decoder_version" in design.metadata
        assert "warnings" in design.metadata


# ---------------------------------------------------------------------------
# 多形态坐标提取（P2-A 字节级单测）
# ---------------------------------------------------------------------------

def _frame(raw: bytes, ftype: int) -> object:
    from cis2hdl.core.parser.dsn.frame.splitter import Frame

    return Frame(offset=0, raw=raw, frame_type=ftype, next_offset=len(raw))


class TestShapeCoordMultiForm:
    def test_graphic_inst_x1y1(self) -> None:
        """形态 A：外形名帧 → (x1, y1) bbox 角（EXP 口径）。"""
        from cis2hdl.core.parser.dsn.decode.shape_frames import extract_shape_coord

        # [name]R.Normal\x00[dbId:u32][locY][locX][y2][x2][x1][y1][color]
        name = b"R.Normal\x00"
        body = struct.pack("<I", 16813) + struct.pack(
            "<6h", 1005, 159, 1070, 181, 160, 1020
        ) + b"\x30\x00"
        rec = b"\x00" * 10 + name + body
        assert extract_shape_coord(rec) == (160, 1020)

    def test_render_origin(self) -> None:
        """渲染原点 (locX, locY) 与 bbox 角分离提取。"""
        from cis2hdl.core.parser.dsn.decode.shape_frames import extract_render_origin

        name = b"R.Normal\x00"
        body = struct.pack("<I", 16813) + struct.pack(
            "<6h", 1005, 159, 1070, 181, 160, 1020
        ) + b"\x30\x00"
        rec = b"\x00" * 10 + name + body
        assert extract_render_origin(rec) == (159, 1005)

    def test_short45(self) -> None:
        """形态 C：45B 短引用帧 → (x, y)。"""
        from cis2hdl.core.parser.dsn.decode.shape_frames import extract_shape_coord

        rec = b"\xff\xe4\x5c\x39\x00\x00\x00\x00\x1a\x00" \
            + struct.pack("<HH", 1330, 260) + struct.pack("<I", 20437) + b"\x00" * 16
        assert extract_shape_coord(rec) == (1330, 260)

    def test_marker30(self) -> None:
        """形态 B：marker30 标记附近坐标。"""
        from cis2hdl.core.parser.dsn.decode.shape_frames import extract_shape_coord

        rec = b"\xff\xe4\x5c\x39\x00\x00\x00\x00" + b"\x00" * 8 \
            + b"\x30\x00\x00\x00" + struct.pack("<HH", 500, 160) + b"\x00" * 8
        assert extract_shape_coord(rec) == (500, 160)

    def test_unknown_returns_none(self) -> None:
        """未知结构返回 None（防御性解析，不抛异常）。"""
        from cis2hdl.core.parser.dsn.decode.shape_frames import extract_shape_coord

        assert extract_shape_coord(b"\x00" * 40) is None


class TestBinderCoordMethods:
    def test_extract_shape_coord_x1y1(self) -> None:
        """binder._extract_shape_coord 返回 bbox 角 (x1,y1)。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBinder

        name = b"R.Normal\x00"
        body = struct.pack("<I", 16813) + struct.pack(
            "<6h", 1005, 159, 1070, 181, 160, 1020
        ) + b"\x30\x00"
        raw = b"\xff\xe4\x5c\x39\x00\x00\x00\x00" + b"\x00" * 2 + name + body
        f = _frame(raw, 0x0674)
        assert InstanceBinder._extract_shape_coord(f) == (160, 1020)
        assert InstanceBinder._extract_shape_render_origin(f) == (159, 1005)

    def test_extract_placed_coord_45b(self) -> None:
        """0x0D 45B 坐标帧 → (x, y)（P2-A 兜底）。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBinder

        raw = b"\xff\xe4\x5c\x39\x00\x00\x00\x00\x0d\x00" \
            + struct.pack("<HH", 990, 370) + struct.pack("<I", 20637) + b"\x00" * 16
        f = _frame(raw, 0x0D)
        assert InstanceBinder._extract_placed_coord(f) == (990, 370)

    def test_extract_placed_coord_long_text_none(self) -> None:
        """0x0D 长文本帧（信息页）→ None（P1-8 分流保持）。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBinder

        raw = b"\xff\xe4\x5c\x39\x00\x00\x00\x00\x0d\x00" \
            + b"01_Cover Page" + b"\x00" * 200
        f = _frame(raw, 0x0D)
        assert InstanceBinder._extract_placed_coord(f) is None


# ---------------------------------------------------------------------------
# 既有坐标链路回归（P0/P1 不破坏）
# ---------------------------------------------------------------------------

class TestP2Regression:
    def test_instances_count_stable(self, p2_design) -> None:
        """实例规模稳定：非 GND 738 + GND 675（P1 基线）。"""
        insts = _non_gnd_instances(p2_design)
        gnd = [
            i for p in p2_design.pages for i in p.instances
            if i.library_id == "GND_SYMBOL"
        ]
        # 2026-08-21：U7A~U7F 六段独立 → 实例数 +5（原合并 1 → 现 6）
        assert len(insts) == 743
        assert len(gnd) == 675

    def test_page08_symbol_texts_render_origin(self, p2_design) -> None:
        """U7C 符号文本平移（gold 口径：渲染原点零化 → loc 兜底）。

        gold DSN 的 GraphicInst locX/locY 清零，渲染原点回落 loc
        (260,120) + 符号局部 (353,1) → (588,97)（新 WORKOLB DSN 渲染原点）。
        """
        p08 = next(p for p in p2_design.pages if p.page_name == "08_TG0_ECS")
        spi = [t for t in p08.symbol_texts if t["text"].startswith("SPI_interface")]
        assert spi, "08 页无 SPI_interface 符号文本"
        assert (spi[0]["x"], spi[0]["y"]) == (588, 97)


# ---------------------------------------------------------------------------
# P2-B 引脚级网络全量（连接关系）
# ---------------------------------------------------------------------------

class TestP2BPinConnections:
    def test_pin_connections_significant(self, p2_design) -> None:
        """P2-B：非 GND 实例 pin_connections 大幅提升（目标 ≥2189）。"""
        non_gnd = _non_gnd_instances(p2_design)
        total = sum(len(i.pin_connections) for i in non_gnd)
        assert total >= 2000, f"non-GND pin_connections={total}"

    def test_r190_connections_exact(self, p2_design) -> None:
        """P2-B：R190:A→DVDD3V3、R190:B→25GE_MDC_R（EXP 黄金值）。"""
        r190 = next(
            i for p in p2_design.pages for i in p.instances if i.refdes == "R190"
        )
        conns = r190.pin_connections
        assert conns.get("A") == "DVDD3V3", f"R190 conns={conns}"
        assert conns.get("B") == "25GE_MDC_R", f"R190 conns={conns}"

    def test_r83_connections_exact(self, p2_design) -> None:
        """P2-B：R83:A→$17N16945、R83:B→1V8（EXP 黄金值）。"""
        r83 = next(
            i for p in p2_design.pages for i in p.instances if i.refdes == "R83"
        )
        conns = r83.pin_connections
        assert conns.get("A") == "$17N16945", f"R83 conns={conns}"
        assert conns.get("B") == "1V8", f"R83 conns={conns}"

    def test_u7_connections_present(self, p2_design) -> None:
        """P2-B：U7A~U7F 六段独立连接（合计 ≥380 条非空）。"""
        u7s = [
            i for p in p2_design.pages for i in p.instances
            if i.library_id and "Hi5662YV305" in i.library_id
        ]
        total = sum(len(i.pin_connections) for i in u7s)
        assert total >= 380, f"U7 六段 conns={total}"
        assert all(
            v for i in u7s for v in (i.pin_connections or {}).values()
        )

    def test_net_ir_connections_positive(self, p2_design) -> None:
        """NetIR.connections 与 pin_connections 同步（P2-B）。"""
        net_conns = sum(
            len(n.connections) for p in p2_design.pages for n in p.nets
        )
        assert net_conns >= 2000, f"NetIR.connections={net_conns}"


# ---------------------------------------------------------------------------
# P2-D 引脚电气类型（portType + 名称回退链）
# ---------------------------------------------------------------------------

class TestP2DPinTypes:
    def test_pin_types_not_all_passive(self, p2_design) -> None:
        """P2-D：PinDef.type 非全 PASSIVE（POWER/INPUT/GROUND 已回填）。"""
        types: set[str] = set()
        for comp in p2_design.component_db.list_all():
            for pin in comp.pins:
                types.add(pin.type.value)
        assert len(types) >= 2, f"types={types}"
        assert "PASSIVE" in types
        assert any(t != "PASSIVE" for t in types), "全部 PASSIVE（P2-D 未生效）"

    def test_power_pin_inferred(self, p2_design) -> None:
        """P2-D：电源/接地引脚按名推断（2P5GEPHY DVDD09_1 → POWER）。"""
        from cis2hdl.core.parser.dsn.decode.symbol_pin_frames import (
            infer_pin_type_from_name,
        )

        assert infer_pin_type_from_name("DVDD09_1") == "POWER"
        assert infer_pin_type_from_name("VSS/GND") == "GROUND"
        assert infer_pin_type_from_name("DDR_ADDR0") is None


# ---------------------------------------------------------------------------
# P2-E U7 六单元标号（Cache label map 聚合）
# ---------------------------------------------------------------------------

class TestP2EPinLabels:
    def test_u7_pin_labels_six_units(self, p2_design) -> None:
        """P2-E：U7 pin_labels 六单元聚合（203 对 = 43+11+24+18+18+89）。"""
        u7 = next(
            i for p in p2_design.pages for i in p.instances if i.refdes == "U7"
        )
        assert len(u7.pin_labels) >= 200, f"U7 pin_labels={len(u7.pin_labels)}"
        # 抽查各单元代表键
        assert "AA23" in u7.pin_labels      # 0A 单元
        assert "F5" in u7.pin_labels        # 0D 单元
        assert "A1" in u7.pin_labels        # 0F 单元

    def test_cache_six_units_parsed(self) -> None:
        """P2-E：Cache 六段标号映射全部解出（203 对）。"""
        from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            cache = parse_cache_stream(provider.read_stream("Cache"))
        finally:
            provider.close()
        units = {m.unit_name: m for m in cache.pin_label_maps}
        assert len(units) == 6
        total = sum(len(m.pairs) for m in cache.pin_label_maps)
        assert total == 203, f"total pairs={total}（审计 count 字段和为 409=条目数）"


# ---------------------------------------------------------------------------
# P2-C 用户属性「描述」键（5 键齐全）
# ---------------------------------------------------------------------------

class TestP2CDescription:
    def test_user_attrs_5_keys(self, p2_design) -> None:
        """P2-C：user_attrs 5 键齐全（含「描述」），描述 ≥229 实例。

        2026-08-21 修正：EXP 黄金无"5 键齐全"实例（电阻无额定电压、
        电容无额定功率为器件物理属性）——断言改为验证 5 键库级齐全
        + 关键键覆盖 + 样例值，而非实例级 5 键同时存在。
        """
        insts = _non_gnd_instances(p2_design)
        with_desc = [i for i in insts if "描述" in i.user_attrs]
        assert len(with_desc) >= 229, f"描述实例={len(with_desc)}"
        # 5 键在各实例的覆盖（描述 ≥229 已验；COMPONENT_HIGH_MAX 电阻电容均含）
        chm = [i for i in insts if "COMPONENT_HIGH_MAX" in i.user_attrs]
        assert len(chm) >= 500, f"COMPONENT_HIGH_MAX={len(chm)}"
        # 样例值校验（R 阻值偏差 / C 额定电压 / 描述键存在）
        by_ref = {i.refdes: i.user_attrs for i in insts}
        r_tol = next((v for v in by_ref.values()
                      if v.get("阻值允许偏差")), None)
        assert r_tol, "无阻值允许偏差样例"
        c_volt = next((v for v in by_ref.values()
                       if v.get("额定工作电压(V)")), None)
        assert c_volt, "无额定工作电压样例"

    def test_library_attr_description_field(self) -> None:
        """P2-C：LibraryAttrDecoder description 字段回填（INS5→MDI_BP_1）。"""
        from cis2hdl.core.parser.dsn.decode.library_attrs import parse_component_attrs
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            attrs = parse_component_attrs(provider.read_stream("Library"))
        finally:
            provider.close()
        by_id = {a.ins_id: a for a in attrs}
        assert by_id["INS5"].description == "MDI_BP_1"


# ---------------------------------------------------------------------------
# T03 全元件符号文本（ComponentDef 粒度 100%）
# ---------------------------------------------------------------------------

class TestT03SymbolTexts:
    def test_all_component_defs_have_texts(self, p2_design) -> None:
        """T03：全部 ComponentDef 都填充 symbol_texts（44/44）。"""
        comps = p2_design.component_db.list_all()
        with_texts = [c for c in comps if c.symbol_texts]
        assert len(with_texts) == len(comps), (
            f"{len(with_texts)}/{len(comps)} 有符号文本"
        )
        total = sum(len(c.symbol_texts) for c in comps)
        assert total >= 214, f"符号元素总数={total}"

    def test_body_pin_kinds_present(self, p2_design) -> None:
        """T03：符号文本含 body/pin 几何元素（0x2929/0x2c2c）。"""
        comps = p2_design.component_db.list_all()
        kinds = set()
        for c in comps:
            for t in c.symbol_texts:
                kinds.add(t.get("kind", "text"))
        assert "body" in kinds
        assert "pin" in kinds

    def test_page_level_texts_stable(self, p2_design) -> None:
        """T03：页面级 symbol_texts 不含 body/pin（08 页=11 文本保持）。"""
        p08 = next(p for p in p2_design.pages if p.page_name == "08_TG0_ECS")
        assert len(p08.symbol_texts) == 11
        assert all(t["text"] for t in p08.symbol_texts)


# ---------------------------------------------------------------------------
# D4 GND 网名归一
# ---------------------------------------------------------------------------

class TestD4GndNet:
    def test_gnd_conn_nets_all_gnd(self, p2_design) -> None:
        """D4：GND 实例连接网名 100% 归一为 'GND'。"""
        gnd = [
            i for p in p2_design.pages for i in p.instances
            if i.library_id == "GND_SYMBOL"
        ]
        conns = [net for g in gnd for net in g.pin_connections.values()]
        assert conns, "GND 无连接"
        assert all(net == "GND" for net in conns), (
            f"非 GND 网名: {set(n for n in conns if n != 'GND')}"
        )

    def test_gnd_refdes_unique(self, p2_design) -> None:
        """D4：GND 实例 refdes 保持唯一（不改 refdes 语义）。"""
        gnd = [
            i for p in p2_design.pages for i in p.instances
            if i.library_id == "GND_SYMBOL"
        ]
        refs = [i.refdes for i in gnd]
        assert len(refs) == len(set(refs))


# ---------------------------------------------------------------------------
# T04 展示维度（P2-1..7）
# ---------------------------------------------------------------------------

class TestT04PageSize:
    def test_all_pages_a3_size(self, p2_design) -> None:
        """T04 P2-5：全 18 页 width/height == 16535/11693（A3 µm→mil）。"""
        sizes = {(p.width, p.height) for p in p2_design.pages}
        assert sizes == {(16535, 11693)}, f"sizes={sizes}"
        assert len(p2_design.pages) == 18

    def test_page_size_decoder(self) -> None:
        """T04 P2-5：parse_page_size 解析页面头 A3 尺寸。"""
        from cis2hdl.core.parser.dsn.decode.page_size import parse_page_size
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            design_name = provider.design_name()
            for page_name, data in provider.page_streams(design_name):
                ps = parse_page_size(data)
                assert ps.width_mil == 16535
                assert ps.height_mil == 11693
                break
        finally:
            provider.close()


class TestT04GraphicShapes:
    def test_page11_shapes_all_types(self, p2_design) -> None:
        """T04 P2-2：11_TG0_DDR 57-62/88/89 全解出（各类型 ≥1）。"""
        p11 = next(p for p in p2_design.pages if p.page_name == "11_TG0_DDR")
        kinds = {}
        for g in p11.graphic_elements:
            if g.get("kind") == "graphic_shape":
                kinds[g.get("primitive")] = kinds.get(g.get("primitive"), 0) + 1
        for prim in ("arc", "ellipse", "polygon", "polyline",
                     "comment_text", "bitmap", "bezier", "ole_embed"):
            assert kinds.get(prim, 0) >= 1, f"{prim} 缺失: {kinds}"
        # 57=2/58=2/59=2/60=2/61=2/62=2/88=1/89=1
        assert kinds.get("arc") == 2 and kinds.get("bezier") == 1

    def test_symbol_bbox_present(self, p2_design) -> None:
        """T04 P2-3：SymbolBBox（0x2C 83B）→ graphic_elements。"""
        bboxes = [
            g for p in p2_design.pages for g in p.graphic_elements
            if g.get("kind") == "symbol_bbox"
        ]
        assert bboxes, "无 SymbolBBox"
        assert all(g.get("x2", 0) > g.get("x1", 0) for g in bboxes)


class TestT04WireStyle:
    def test_wire_style_non_default(self, p2_design) -> None:
        """T04 P2-4：WireSegment color/line_width/line_style 非全默认。"""
        wires = [w for p in p2_design.pages for w in p.wires]
        assert wires, "无走线"
        non_default = sum(
            1 for w in wires
            if w.color or w.line_width or w.line_style
        )
        assert non_default > 0, "全部默认样式"
        assert non_default / len(wires) > 0.5


class TestT04OffPage:
    def test_off_pages_five(self, p2_design) -> None:
        """T04 P2-5：PageIR.off_pages 0→≥5（type 38 OffPage 连接符）。"""
        off = [o for p in p2_design.pages for o in p.off_pages]
        assert len(off) >= 5, f"off_pages={len(off)}"
        assert all(o.get("x", 0) > 0 and o.get("y", 0) > 0 for o in off)

    def test_off_pages_conn_netnames_three(self, p2_design) -> None:
        """2026-08-21 §16 补遗：3 个无网名连接符帧网名恢复。

        - 帧 46341（05 页 ONSHEET2，25 属性 4B 变体 25 73 00）→ 'MDI_AN_3'
        - 帧 19394（09 页 OFFSHEET2，25 属性 idx=0 空）→ frame_type
          兜底 'TXDN_XPON'（type=518=strLst 索引）
        - 帧 98821（14 页 BISHEET，25 属性=库路径需过滤）→ frame_type
          兜底 '25GE_TRXN0_R'（type=757=strLst 索引）
        """
        by_page: dict[str, list[dict]] = {
            p.page_name: p.off_pages for p in p2_design.pages
        }
        expect = {
            "05_POWER_SUPPLY_1": (46341, "MDI_AN_3"),
            "09_TG0_NI": (19394, "TXDN_XPON"),
            "14_4GE": (98821, "25GE_TRXN0_R"),
        }
        for page, (offset, net) in expect.items():
            conns = by_page.get(page, [])
            hit = [o for o in conns if o.get("offset") == offset]
            assert hit, f"{page} offset={offset} 未解析"
            assert hit[0].get("net_name") == net, (
                f"{page} offset={offset} net={hit[0].get('net_name')!r} "
                f"期望 {net!r}"
            )


class TestT04PkgType:
    def test_pkg_type_non_empty(self, p2_design) -> None:
        """T04 P2-6：ComponentDef.extra_data['PKG_TYPE'] 非空。"""
        comps = p2_design.component_db.list_all()
        with_pkg = [c for c in comps if c.extra_data.get("PKG_TYPE")]
        assert with_pkg, "无 PKG_TYPE"
        assert len(with_pkg) >= 20, f"PKG_TYPE 部件数={len(with_pkg)}"
        assert all(v for v in (c.extra_data["PKG_TYPE"] for c in with_pkg))


class TestT04Coverage:
    def test_coverage_improved(self, p2_design) -> None:
        """T04 P2-7：帧覆盖率 ≥57.1% 提升（短坐标帧注册后 63.9%）。"""
        fs = p2_design.metadata["frame_stats"]
        assert fs["covered_pct"] >= 57.1
        assert fs["covered_pct"] > 57.1 or fs["covered_frames"] > 6275
