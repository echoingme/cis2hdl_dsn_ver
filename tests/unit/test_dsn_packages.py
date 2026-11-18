"""Packages 流 pinMap/pinNames 解析单元测试（修复验收）。

覆盖（架构设计 §2 cache + QA package_full_decode.json 11 包对拍）：
- 11 包全部解析出 pinMap/pinNames（DDR3=96、Hi5662=178+409 names、USB=8…）
- 与 QA 字节级解码结果完全一致（pinMap 序列 + pinNames 序列 + pinCount）
- 多单元包 Hi5662 合并 409 个引脚名（主单元 frame 收集）
"""

from __future__ import annotations

import json

import pytest

from cis2hdl.core.parser.dsn.cache.packages_parser import parse_packages
from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")
QA_PATH = data_path("package_full_decode.json")

#: 各包期望引脚数（QA 实证）
EXPECTED_PIN_COUNTS = {
    "2P5GEPHY_0": 49,
    "DDR3_0": 96,
    "Hi5662YV305_DDR_GE_0": 178,
    "LB_0": 2,
    "LC4_A_0": 4,
    "LC_J_0": 4,
    "NPN-BEC_0": 3,
    "PNP-BCEC_0": 4,
    "SLC NAND FLASH_0": 9,
    "SLC NAND FLASH_1": 9,
    "USB-4S4_0": 8,
}


@pytest.fixture(scope="module")
def qa_data() -> dict:
    with open(QA_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def packages():
    provider = OlefileProvider(DSN_PATH)
    try:
        return parse_packages(provider.packages())
    finally:
        provider.close()


class TestPackagesParser:
    def test_all_packages_parsed(self, packages) -> None:
        """11 个 Packages/* 流全部解析出 pinMap。"""
        assert len(packages) == 11
        ok = sum(1 for p in packages if p.pin_count is not None and p.pin_map)
        assert ok == 11

    def test_pin_counts_match_qa(self, packages) -> None:
        """pinCount 与 QA 一致。"""
        by_stream = {p.stream_name: p for p in packages}
        for stream, expected in EXPECTED_PIN_COUNTS.items():
            assert by_stream[stream].pin_count == expected

    def test_pin_map_names_match_qa(self, packages, qa_data) -> None:
        """pinMap/pinNames 与 QA package_full_decode.json 完全一致。"""
        by_stream = {p.stream_name: p for p in packages}
        for stream, pkg in by_stream.items():
            q = qa_data[f"Packages/{stream}"]
            assert pkg.pin_map == q["pinMap"], f"{stream} pinMap mismatch"
            assert pkg.pin_names == q["pinNames"], f"{stream} pinNames mismatch"

    def test_multi_unit_hi5662(self, packages) -> None:
        """Hi5662 多单元包：主单元 F，pinCount=178，pinNames=409。"""
        pkg = next(p for p in packages if p.stream_name == "Hi5662YV305_DDR_GE_0")
        assert pkg.unit == "F"
        assert pkg.pin_count == 178
        assert len(pkg.pin_map) == 178
        assert len(pkg.pin_names) == 409
        assert pkg.pin_map[0] == "A1"
        assert pkg.pin_names[0] == "DDR_ADDR0"
