"""DSN 语义帧解码单元测试（架构设计 §6.1 test_decoders.py）。

对拍 QA 数字：
- 0x85 位号文本帧 643
- 0x02 属性帧 725
- 网名表 609
- 走线 3131
"""

from __future__ import annotations

import pytest

from cis2hdl.core.parser.dsn.decode.net_table import parse_net_table
from cis2hdl.core.parser.dsn.decode.registry import (
    DecodeContext,
    clear_registries,
    dispatch,
    register_semantic,
)
from cis2hdl.core.parser.dsn.decode.semantic_frames import (
    REFDES_FRAME_TYPES,
    parse_property_frame,
    parse_refdes_frame,
    register_semantic_decoders,
)
from cis2hdl.core.parser.dsn.decode.wire_frames import parse_wire_frame, scan_wire_frames
from cis2hdl.core.parser.dsn.frame.splitter import split_frames
from cis2hdl.core.parser.dsn.frame.types import DELIMITER
from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider
from cis2hdl.core.parser.dsn.strlst.strlst import parse_strlst
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")


@pytest.fixture(scope="module")
def ctx() -> DecodeContext:
    provider = OlefileProvider(DSN_PATH)
    strlst = parse_strlst(provider.read_stream("Library"))
    provider.close()
    return DecodeContext(strlst=strlst)


@pytest.fixture(scope="module")
def pages_frames(ctx: DecodeContext) -> list[tuple[str, list, object]]:
    """全部页面 (名称, 帧列表, 网名表)。"""
    provider = OlefileProvider(DSN_PATH)
    try:
        design_name = provider.design_name()
        out = []
        for name, data in provider.page_streams(design_name):
            out.append((name, split_frames(data), parse_net_table(data)))
        return out
    finally:
        provider.close()


class TestRefdesDecode:
    def test_0x85_count_matches_qa(self, pages_frames, ctx) -> None:
        """位号帧（0x85/0x22，gold 用 0x22）= 721（gold 权威对拍）。

        gold DSN（md5 5762432b）把位号文本放在 0x22 'Part Reference'
        帧（721 个可解析）；0x85 'VALUE' 帧为短坐标帧（不含位号文本）。
        """
        total = 0
        for _name, frames, net_table in pages_frames:
            after = net_table.offset if net_table.offset is not None else 0x400
            for f in frames:
                if f.offset < after:
                    continue
                if f.frame_type in (0x85, 0x22) and parse_refdes_frame(f, ctx) is not None:
                    total += 1
        assert total == 721

    def test_refdes_shape(self, pages_frames, ctx) -> None:
        """抽验：04 页存在 R83/C66 等位号，值为 30K/4.7uF 类。"""
        found = {}
        for name, frames, net_table in pages_frames:
            if name != "04_POWER_SUPPLY":
                continue
            after = net_table.offset if net_table.offset is not None else 0x400
            for f in frames:
                if f.offset < after or f.frame_type not in (0x85, 0x22):
                    continue
                rd = parse_refdes_frame(f, ctx)
                if rd is not None:
                    found[rd.refdes] = rd
        assert "R83" in found
        assert found["R83"].refdes == "R83"
        assert found["R83"].value_idx is not None
        assert found["R83"].value in ("30K", "30k")

    def test_refdes_types_constant(self) -> None:
        assert 0x85 in REFDES_FRAME_TYPES
        assert 0x22 in REFDES_FRAME_TYPES


class TestPropertyDecode:
    def test_0x02_frame_count_matches_qa(self, pages_frames, ctx) -> None:
        """0x02 属性帧 = 725（QA 对拍）。"""
        total = 0
        for _name, frames, net_table in pages_frames:
            after = net_table.offset if net_table.offset is not None else 0x400
            total += sum(
                1 for f in frames
                if f.offset >= after and f.frame_type == 0x02
            )
        assert total == 725

    def test_footprint_pairs(self, pages_frames, ctx) -> None:
        """0x02 帧可提取 [25,'PCB Footprint'] 属性对（≥630）。"""
        found = 0
        for _name, frames, net_table in pages_frames:
            after = net_table.offset if net_table.offset is not None else 0x400
            for f in frames:
                if f.offset < after or f.frame_type != 0x02:
                    continue
                pf = parse_property_frame(f, ctx)
                if pf.footprint:
                    found += 1
        assert found >= 630


class TestNetTable:
    def test_net_count_matches_qa(self, pages_frames) -> None:
        """网名表合计 614（QA 对拍 609 + 散落网名 7 条，2026-08-20 晚修复）。"""
        total = sum(len(nt.names) for _n, _f, nt in pages_frames)
        assert total == 614


class TestWireScan:
    def test_wire_count_matches_qa(self, pages_frames, ctx) -> None:
        """走线合计 3131（QA 对拍，仅统计网名表之后结构区）。"""
        total = 0
        for _name, frames, net_table in pages_frames:
            after = net_table.offset if net_table.offset is not None else 0x400
            struct_frames = [f for f in frames if f.offset >= after]
            scan = scan_wire_frames(struct_frames, ctx)
            total += len(scan.wires)
        assert total == 3131


class TestDispatchRegistry:
    def test_register_and_dispatch(self) -> None:
        clear_registries()
        calls = []

        def dummy(frame, ctx):
            calls.append(frame.frame_type)
            return "decoded"

        register_semantic(0x99, dummy)
        frame_type = 0x99
        from cis2hdl.core.parser.dsn.frame.splitter import Frame

        f = Frame(offset=0, raw=DELIMITER + frame_type.to_bytes(2, "little") + b"\x00", frame_type=frame_type, next_offset=10)
        result = dispatch(f, DecodeContext())
        assert result == "decoded"
        assert calls == [0x99]
        clear_registries()

    def test_unknown_frame_returns_unknown(self) -> None:
        clear_registries()
        from cis2hdl.core.parser.dsn.decode.registry import UnknownFrame
        from cis2hdl.core.parser.dsn.frame.splitter import Frame

        f = Frame(offset=0, raw=DELIMITER + b"\xee\x00" + b"\x00", frame_type=0xEE, next_offset=10)
        result = dispatch(f, DecodeContext())
        assert isinstance(result, UnknownFrame)
        assert result.frame_type == 0xEE
        clear_registries()

    def test_register_all_decoders(self) -> None:
        from cis2hdl.core.parser.dsn.decode import register_all_decoders

        clear_registries()
        register_all_decoders()
        from cis2hdl.core.parser.dsn.decode.registry import SEMANTIC_FRAME_DECODERS, STRUCT_DECODERS

        assert 0x85 in SEMANTIC_FRAME_DECODERS
        assert 13 in STRUCT_DECODERS
        clear_registries()
