"""DSN P0 补齐验收测试（架构设计 §7 T05 验收 + §8 统计键）。

六维度验收（TG0C0D2 测试文件）：
1. ``pin_partitions==18``（0x0867 引脚分区表，18 页各 1 帧）
2. ``text_elements>0``（0x0000 全页面文字元素，含 GBK 中文）
3. ``graphic_elements>0``（0x0000 图形元素）
4. ``wires`` 含 ``points>2`` 折线（P0-4 折线拼接）
5. ``NetIR.connections>0``（P0-5 线↔引脚连接关系）
6. ``pin_connections>0``（实例级连接）

metadata 新增键（架构 §8.9）：``frame_coverage`` / ``pin_partition_count`` /
``text_element_count`` / ``polyline_count`` / ``connection_count`` /
``pin_label_pairs`` / ``net_count_u16_pages``。

帧覆盖率：0x0867/0x0000 不再 UnknownFrame，覆盖率从 30.6% 提升。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cis2hdl.core.parser.dsn.dsn_parser import DSNParser
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")


@pytest.fixture(scope="module")
def p0_design() -> object:
    """解析 TG0C0D2 测试 DSN（新帧语义管道）。"""
    if not Path(DSN_PATH).exists():
        pytest.skip(f"Test file not found: {DSN_PATH}")
    return DSNParser().parse(Path(DSN_PATH))


class TestP0Pipeline:
    def test_pages_count(self, p0_design) -> None:
        """18 平铺页面。"""
        assert len(p0_design.pages) == 18

    def test_pin_partitions_18(self, p0_design) -> None:
        """① pin_partitions==18（0x0867 每页 1 表）。"""
        assert p0_design.metadata["pin_partition_table_count"] == 18
        total_entries = sum(len(p.pin_partitions) for p in p0_design.pages)
        assert total_entries > 0
        # gold 权威：08 页 54 条（gold 0x0867 帧条目少于 workdir 98）
        p08 = next(p for p in p0_design.pages if p.page_name == "08_TG0_ECS")
        assert len(p08.pin_partitions) >= 50

    def test_text_elements_with_gbk(self, p0_design) -> None:
        """② text_elements>0 且含 GBK 中文「串联电阻靠变压器端放置」。"""
        total_text = sum(len(p.text_elements) for p in p0_design.pages)
        assert total_text > 0
        gbk_texts = [
            el.get("text")
            for p in p0_design.pages
            for el in p.text_elements
            if el.get("encoding") == "gbk"
        ]
        assert gbk_texts, "No GBK text elements found"
        assert "串联电阻靠变压器端放置" in gbk_texts
        # 坐标锚点验证
        target = [
            el for p in p0_design.pages
            for el in p.text_elements
            if el.get("text") == "串联电阻靠变压器端放置"
        ]
        assert target and target[0]["x"] == 1220 and target[0]["y"] == 207

    def test_graphic_elements_present(self, p0_design) -> None:
        """③ graphic_elements>0。"""
        total = sum(len(p.graphic_elements) for p in p0_design.pages)
        assert total > 0

    def test_wires_have_polyline_points(self, p0_design) -> None:
        """④ wires 含 points>2 折线（P0-4）。"""
        points_gt2 = sum(
            1 for p in p0_design.pages for w in p.wires if len(w.points) > 2
        )
        assert points_gt2 > 0
        assert p0_design.metadata["polyline_count"] > 0

    def test_net_connections_positive(self, p0_design) -> None:
        """⑤ NetIR.connections>0（P0-5 坐标级连接）。"""
        total_conns = sum(
            len(n.connections) for p in p0_design.pages for n in p.nets
        )
        assert total_conns > 0
        assert p0_design.metadata["connection_count"] > 0

    def test_pin_connections_positive(self, p0_design) -> None:
        """⑥ pin_connections>0（实例级连接）。"""
        total_pin_conns = sum(
            len(i.pin_connections) for p in p0_design.pages for i in p.instances
        )
        assert total_pin_conns > 0

    def test_metadata_keys_present(self, p0_design) -> None:
        """metadata 新增键齐全（架构 §8.9）。"""
        for key in (
            "frame_coverage",
            "pin_partition_count",
            "text_element_count",
            "polyline_count",
            "connection_count",
            "pin_label_pairs",
            "net_count_u16_pages",
        ):
            assert key in p0_design.metadata, f"missing metadata key: {key}"

    def test_frame_coverage_improved(self, p0_design) -> None:
        """帧覆盖率从 30.6% 提升（0x0867/0x0000 不再 UnknownFrame）。

        P1 增强：26/27 被 struct 解码器接管，coverage_before 从 30.6
        升至 30.8（18 个 26/27 帧不再 UnknownFrame）。
        """
        fc = p0_design.metadata["frame_coverage"]
        assert fc["total"] == 10995
        assert fc["coverage_before"] >= 30.6
        assert fc["coverage_after"] > fc["coverage_before"]
        assert fc["p0_frame_types"].get(0x0867) == 18
        assert fc["p0_frame_types"].get(0x0000) > 0

    def test_pin_labels_bound(self, p0_design) -> None:
        """P0-3：U7 实例绑定 Cache 标号映射（P2-E 扩展后 ≥89 对）。"""
        bound = [
            i for p in p0_design.pages for i in p.instances if i.pin_labels
        ]
        assert bound, "No instance has pin_labels bound"
        # P0：主单元 0F（89 对）；P2-E 扩展：六单元聚合（203 对）
        u7 = next(i for i in bound if i.refdes == "U7")
        assert len(u7.pin_labels) >= 89

    def test_pin_label_maps_parsed(self) -> None:
        """P0-3 单元级：6 段标号映射全解出，0A 首对 AA23→Y21。"""
        from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
        from cis2hdl.core.parser.dsn.cache.pin_labels_parser import scan_pin_label_maps
        from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

        provider = OlefileProvider(DSN_PATH)
        try:
            data = provider.read_stream("Cache")
        finally:
            provider.close()
        maps = scan_pin_label_maps(data)
        assert len(maps) == 6
        by_unit = {m.unit_name: m for m in maps}
        assert "Hi5662YV305_DDR_GE_0A" in by_unit
        assert by_unit["Hi5662YV305_DDR_GE_0A"].by_label.get("AA23") == "Y21"
        # 0F 主单元 89 对（count 字段 178 = 89 对 × 2 串）
        assert len(by_unit["Hi5662YV305_DDR_GE_0F"].pairs) == 89


class TestP0Dispatch:
    def test_0867_and_0000_not_unknown(self) -> None:
        """dispatch 对 0x0867/0x0000 不再返回 UnknownFrame。"""
        from cis2hdl.core.parser.dsn.decode import register_all_decoders
        from cis2hdl.core.parser.dsn.decode.registry import (
            DecodeContext,
            UnknownFrame,
            clear_registries,
            dispatch,
        )
        from cis2hdl.core.parser.dsn.frame.splitter import Frame
        from cis2hdl.core.parser.dsn.frame.types import DELIMITER

        clear_registries()
        register_all_decoders()
        try:
            ctx = DecodeContext(strlst=[], page_name="08_TG0_ECS")
            for ftype in (0x0867, 0x0000):
                raw = DELIMITER + ftype.to_bytes(2, "little") + b"\x00" * 60
                frame = Frame(offset=0, raw=raw, frame_type=ftype, next_offset=len(raw))
                result = dispatch(frame, ctx)
                assert not isinstance(result, UnknownFrame), f"{ftype:#x} UnknownFrame"
        finally:
            clear_registries()


class TestNetTableU16:
    def test_u16_branch_synthetic(self) -> None:
        """构造 >255 网名表：u16 分支通过（P0-6）。"""
        import struct

        from cis2hdl.core.parser.dsn.decode.net_table import parse_net_table

        blob = b"\x00\x00\x00\x00" + struct.pack("<H", 300)
        for i in range(300):
            name = f"N{i:03d}".encode()
            blob += struct.pack("<H", len(name)) + name + b"\x00" + struct.pack("<I", i)
        nt = parse_net_table(blob)
        assert nt.count_bytes == 2
        assert len(nt.names) == 300

    def test_u8_branch_unchanged(self) -> None:
        """u8 网名表（count≤255）行为不变。"""
        import struct

        from cis2hdl.core.parser.dsn.decode.net_table import parse_net_table

        blob = b"\x00\x00\x00\x00\x05\x00"
        for i in range(5):
            name = f"U{i}".encode()
            blob += struct.pack("<H", len(name)) + name + b"\x00" + struct.pack("<I", 100 + i)
        nt = parse_net_table(blob)
        assert nt.count_bytes == 1
        assert len(nt.names) == 5


class TestConnectivityBuilder:
    def test_pin_connections_nonempty(self) -> None:
        """ConnectivityBuilder 单测：2 实例 + 1 网走线 → pin_connections 非空。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBind
        from cis2hdl.core.parser.dsn.decode.net_table import NetTable
        from cis2hdl.core.parser.dsn.decode.wire_frames import WireRecord
        from cis2hdl.core.parser.dsn.decode.wire_topology import (
            ConnectivityBuilder,
            PinPointRecord,
        )

        net_table = NetTable(offset=0, names={155685: "1V8"})
        wires = [
            WireRecord(offset=100, net_id=155685, x1=100, y1=100, x2=200, y2=100),
            WireRecord(offset=200, net_id=155685, x1=200, y1=100, x2=300, y2=100),
        ]
        binds = [
            InstanceBind(
                refdes="U1", page="P1", value=None, value_source="none",
                value_idx=None, footprint=None, footprint_source="none",
                shape="IC1", cache_ref_des=None, cache_source_lib=None,
                loc_x=100, loc_y=100, frame_offset=0, frame_type=0x85,
            ),
            InstanceBind(
                refdes="U2", page="P1", value=None, value_source="none",
                value_idx=None, footprint=None, footprint_source="none",
                shape="IC2", cache_ref_des=None, cache_source_lib=None,
                loc_x=300, loc_y=100, frame_offset=0, frame_type=0x85,
            ),
        ]
        pin_points = [
            PinPointRecord(offset=150, frame_type=0x01, x=100, y=100, object_id=1),
            PinPointRecord(offset=250, frame_type=0x03, x=300, y=100, object_id=2),
        ]
        result = ConnectivityBuilder(net_table).build(wires, pin_points, binds)
        assert result.connection_count > 0
        assert len(result.pin_point_to_net) == 2
        assert result.pin_point_to_instance[1] == "U1"
        assert result.pin_point_to_instance[2] == "U2"


class TestMapperBackCompat:
    def test_map_to_design_ir_old_4tuple(self) -> None:
        """map_to_design_ir 兼容旧 4 元组 pages_data（EDIF/OLB 回归）。"""
        from cis2hdl.core.parser.dsn.bind.binder import InstanceBind
        from cis2hdl.core.parser.dsn.decode.net_table import NetTable
        from cis2hdl.core.parser.dsn.ir.mapper import map_to_design_ir

        net_table = NetTable(offset=0, names={1: "VCC", 2: "GND"})
        binds = [
            InstanceBind(
                refdes="R1", page="P1", value="10K", value_source="0x85",
                value_idx=3, footprint="0603", footprint_source="0x02",
                shape="RES", cache_ref_des="R", cache_source_lib="LIB",
                loc_x=10, loc_y=20, frame_offset=0, frame_type=0x85,
            ),
        ]
        design = map_to_design_ir(
            project_name="old_style",
            pages_data=[("P1", binds, net_table, [])],
        )
        assert len(design.pages) == 1
        assert design.pages[0].instances[0].refdes == "R1"
        assert design.metadata["pin_partition_count"] == 0
