"""strLst 解析单元测试（架构设计 §6.1 test_library_table.py）。"""

from __future__ import annotations

import pytest

from cis2hdl.core.parser.dsn.strlst.strlst import (
    _parse_strlst_hardcoded,
    _parse_strlst_heuristic,
    parse_strlst,
    validate_strlst,
)

from tests.support import data_path, design_path

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def tg0_library_bytes() -> bytes:
    """TG0C0D2 Library 流（真实数据；不存在则跳过）。"""
    from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider

    path = design_path("DSN")
    import os

    if not os.path.exists(path):
        pytest.skip("TG0C0D2 DSN not available")
    provider = OlefileProvider(path)
    try:
        return provider.read_stream("Library")
    finally:
        provider.close()


@pytest.fixture(scope="module")
def tg0_strlst(tg0_library_bytes: bytes) -> list[str]:
    return parse_strlst(tg0_library_bytes)


class TestStrLstParse:
    def test_hardcoded_2171(self, tg0_library_bytes: bytes) -> None:
        strs = _parse_strlst_hardcoded(tg0_library_bytes)
        assert len(strs) == 2171

    def test_heuristic_2171(self, tg0_library_bytes: bytes) -> None:
        strs = _parse_strlst_heuristic(tg0_library_bytes)
        assert len(strs) == 2171

    def test_both_strategies_equal(self, tg0_library_bytes: bytes) -> None:
        """硬编码与启发式双策略内容一致（QA 要求）。"""
        hard = _parse_strlst_hardcoded(tg0_library_bytes)
        heur = _parse_strlst_heuristic(tg0_library_bytes)
        assert hard == heur

    def test_validate_2171(self, tg0_strlst: list[str]) -> None:
        assert validate_strlst(tg0_strlst, 2171) is True

    def test_key_indices(self, tg0_strlst: list[str]) -> None:
        """关键索引语义（F4）：[133]='VALUE'、[25]='PCB Footprint'、[632]='Value'。"""
        assert tg0_strlst[133] == "VALUE"
        assert tg0_strlst[25] == "PCB Footprint"
        assert tg0_strlst[632] == "Value"
        assert tg0_strlst[1] == "GND"

    def test_parse_raises_on_garbage(self) -> None:
        with pytest.raises(ValueError):
            parse_strlst(b"\x00\x01\x02\x03\x04\x05\x06\x07")
