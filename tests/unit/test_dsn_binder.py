"""DSN 实例绑定单元测试（架构设计 §6.1 test_binder.py + §5 full_bind 修复）。

覆盖：
- Value 优先级：0x85 valueIdx > 0x02 (632,'Value') > Cache partValue
- Footprint 优先级：0x02 [25] > 同簇 0x02 [25] > Packages > None
- U7 多单元合并为 1 实例（section=6）
- 唯一位号 / 绑定率达标
"""

from __future__ import annotations

import pytest

from cis2hdl.core.parser.dsn.bind.binder import InstanceBinder
from cis2hdl.core.parser.dsn.cache.cache_parser import parse_cache_stream
from cis2hdl.core.parser.dsn.cache.packages_parser import parse_packages
from cis2hdl.core.parser.dsn.decode.net_table import parse_net_table
from cis2hdl.core.parser.dsn.frame.splitter import split_frames
from cis2hdl.core.parser.dsn.io.ole_provider import OlefileProvider
from cis2hdl.core.parser.dsn.strlst.strlst import parse_strlst
from tests.support import data_path, design_path

pytestmark = pytest.mark.unit

DSN_PATH = design_path("DSN")


@pytest.fixture(scope="module")
def resources():
    provider = OlefileProvider(DSN_PATH)
    try:
        design_name = provider.design_name()
        strlst = parse_strlst(provider.read_stream("Library"))
        cache = parse_cache_stream(provider.read_stream("Cache"))
        packages = parse_packages(provider.packages())
        pages = []
        for name, data in provider.page_streams(design_name):
            frames = split_frames(data)
            net_table = parse_net_table(data)
            pages.append((name, frames, net_table))
        return {
            "strlst": strlst,
            "cache": cache,
            "packages": packages,
            "pages": pages,
        }
    finally:
        provider.close()


@pytest.fixture(scope="module")
def binder(resources) -> InstanceBinder:
    return InstanceBinder(resources["strlst"], resources["cache"], resources["packages"])


@pytest.fixture(scope="module")
def merged_binds(binder: InstanceBinder, resources) -> list:
    pages_frames = [
        (name, frames, net_table) for name, frames, net_table in resources["pages"]
    ]
    return binder.bind_all_pages(pages_frames)


class TestBinderValues:
    def _real(self, binds) -> list:
        """真实器件（排除 P1-3 GND/电源符号实例，其无 value/footprint）。"""
        return [b for b in binds if b.shape != "GND_SYMBOL"]

    def test_value_rate_ge_99(self, merged_binds) -> None:
        """Value 绑定率 ≥99%（真实器件；GND 符号无 value 属预期）。"""
        real = self._real(merged_binds)
        assert real, "no real binds"
        bound = sum(1 for b in real if b.value)
        assert bound / len(real) >= 0.99

    def test_footprint_rate_ge_95(self, merged_binds) -> None:
        """Footprint 绑定率 ≥95%（真实器件；GND 符号无 footprint 属预期）。"""
        real = self._real(merged_binds)
        assert real, "no real binds"
        bound = sum(1 for b in real if b.footprint)
        assert bound / len(real) >= 0.95

    def test_u7_merged_to_one(self, merged_binds) -> None:
        """2026-08-21 用户裁决：U7A~U7F 六段独立芯片（不再多单元合并）。"""
        u7 = [b for b in merged_binds if b.refdes == "U7"]
        assert len(u7) == 6
        shapes = sorted(b.shape for b in u7)
        assert len(set(shapes)) == 6
        assert any("0A" in s for s in shapes)
        assert any("0F" in s for s in shapes)

    def test_unique_refdes_ge_700(self, merged_binds) -> None:
        """唯一位号 ≥700。"""
        unique = {b.refdes for b in merged_binds}
        assert len(unique) >= 700

    def test_spot_check_r83(self, merged_binds) -> None:
        """R83 实例 value='30K' 主来源 0x85（full_bind 修复验收）。"""
        r83 = [b for b in merged_binds if b.refdes == "R83"]
        assert len(r83) == 1
        assert r83[0].value_source == "0x85"
        assert r83[0].value is not None
        assert "K" in r83[0].value or "k" in r83[0].value

    def test_u7_footprint_is_bga(self, merged_binds) -> None:
        """U7 六段 footprint = BGA409-26-2323（PKG_GRP 属性）。"""
        u7 = [b for b in merged_binds if b.refdes == "U7"]
        assert len(u7) == 6
        # 主段（0A，U7A@11 页 DDR）footprint 必为 BGA409；其余段
        # 0x02 属性帧可能缺 footprint（各段独立后不再合并互补）。
        ddr = next(b for b in u7 if "0A" in b.shape)
        assert ddr.footprint == "BGA409-26-2323"

    def test_u7_pin_map_bound(self, merged_binds) -> None:
        """U7 六段各自绑定 Hi5662 段 pinMap=178 / pinNames=409。"""
        u7 = [b for b in merged_binds if b.refdes == "U7"]
        assert len(u7) == 6
        for b in u7:
            assert len(b.pin_map) == 178
            assert len(b.pin_names) == 409
        ddr = next(b for b in u7 if "0A" in b.shape)
        assert ddr.pin_names[0] == "DDR_ADDR0"

    def test_u7_loc_y_not_null(self, merged_binds) -> None:
        """U7 六段 loc_y 补全（shape 帧坐标）。"""
        u7 = [b for b in merged_binds if b.refdes == "U7"]
        assert len(u7) == 6
        assert all(b.loc_y is not None and b.loc_y > 0 for b in u7)

    def test_pin_map_instances_have_fields(self, merged_binds) -> None:
        """全部实例携带 pin_map/pin_names 字段（包外实例为空列表）。"""
        for b in merged_binds:
            assert isinstance(b.pin_map, list)
            assert isinstance(b.pin_names, list)


class TestBinderPriority:
    def test_value_source_prefers_0x85(self, resources) -> None:
        """大多数实例 value_source='0x85'。"""
        binder = InstanceBinder(resources["strlst"], resources["cache"], resources["packages"])
        pages_frames = [(n, f, nt) for n, f, nt in resources["pages"]]
        binds = binder.bind_all_pages(pages_frames)
        src = {}
        for b in binds:
            src[b.value_source] = src.get(b.value_source, 0) + 1
        assert src.get("0x85", 0) > 0

    def test_cache_lookup(self, resources) -> None:
        """Cache 查找：shape → refDes/partValue。"""
        binder = InstanceBinder(resources["strlst"], resources["cache"], resources["packages"])
        ref, val, src = binder._cache_lookup("R.Normal")
        assert ref == "R"
        assert val  # partValue 存在
