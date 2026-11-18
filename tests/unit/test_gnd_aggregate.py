"""GND 实例级聚类聚合（mentor"大量 GND"修复，config.aggregate_instances）。

验证 ``gnd_placement.plan_gnd_aggregation``（空间网格装箱：cell =
cluster_radius，每格保留 1 个代表符号）与 ``prune_gnd_tap_wires``
（只剪"端点悬空且落被抑制符号候选引脚附近"的整对象 wire——trunk
保留，电气不变由同网 SIG_NAME GND\\g 网名连接保证）。

默认关闭（DSN 直通 675 原位还原）；``aggregate_instances=True`` 才启用。
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace


def _inst(refdes: str, x: int, y: int, lib: str = "GND_SYMBOL"):
    """构造最小实例桩（PageIR 兼容字段子集）。"""
    return SimpleNamespace(
        refdes=refdes, library_id=lib, loc_x=x, loc_y=y,
        section=1, pin_connections={}, properties={},
    )


def _wire(points):
    """构造最小 wire 桩（DSN 原始坐标空间）。"""
    return SimpleNamespace(
        net_name="GND",
        points=[list(p) for p in points],
        start_x=points[0][0], start_y=points[0][1],
        end_x=points[-1][0], end_y=points[-1][1],
    )


def _mk_coord_map(insts, scale: float = 2.0, dx: int = 100, dy: int = 200):
    """模拟 DEHDL 映射：coord_map（refdes → 放大后坐标）。"""
    return {
        str(i.refdes): (int(round(i.loc_x * scale + dx)),
                        int(round(i.loc_y * scale + dy)))
        for i in insts
    }


class TestGndAggregateConfig:
    def test_default_off(self):
        """默认 aggregate_instances=False（DSN 直通，不改变默认行为）。"""
        from cis2hdl.core.config import GndDistributionCfg

        cfg = GndDistributionCfg()
        assert cfg.aggregate_instances is False
        assert cfg.cluster_radius == 2000

    def test_roundtrip_enable(self):
        """聚合开关可经 config 单例开启（routing.yaml / CLI 注入）。"""
        from cis2hdl.core.config import Config

        cfg = Config()
        cfg.routing.gnd_distribution.aggregate_instances = True
        assert cfg.routing.gnd_distribution.aggregate_instances is True


class TestGndAggregatePlan:
    def _plan(self, insts, radius=2000, coord_map=None):
        from cis2hdl.core.writer.gnd_placement import plan_gnd_aggregation

        return plan_gnd_aggregation(
            insts, cluster_radius=radius, coord_map=coord_map,
        )

    def test_near_symbols_merge_dehdl_space(self):
        """DEHDL 空间同一 cell 的 3 个 GND → 保留 1 个、抑制 2 个。"""
        insts = [_inst("GND_1", 0, 0), _inst("GND_2", 100, 0),
                 _inst("GND_3", 50, 80)]
        cm = _mk_coord_map(insts)
        plan = self._plan(insts, coord_map=cm)
        assert len(plan.keep_refdes) == 1
        assert len(plan.drop_refdes) == 2
        assert len(plan.keep_refdes & plan.drop_refdes) == 0

    def test_far_symbols_separate_cells(self):
        """不同 cell 的 GND 各自保留（不链式塌缩）。"""
        insts = [_inst("GND_1", 0, 0), _inst("GND_2", 0, 5000)]
        cm = _mk_coord_map(insts)
        plan = self._plan(insts, coord_map=cm)
        assert len(plan.keep_refdes) == 2
        assert len(plan.drop_refdes) == 0

    def test_radius_zero_disables(self):
        """cluster_radius=0 → 空计划（全部保留）。"""
        insts = [_inst("GND_1", 0, 0), _inst("GND_2", 50, 0)]
        plan = self._plan(insts, radius=0)
        assert not plan.active
        assert not plan.drop_refdes

    def test_non_gnd_ignored(self):
        """非 GND 实例不进计划；单 GND（<2）无聚合 → 空计划不抑制。"""
        insts = [_inst("C1", 0, 0, lib="C.Normal"), _inst("GND_1", 0, 0)]
        plan = self._plan(insts, radius=2000)
        assert not plan.active
        assert not plan.keep_refdes
        assert not plan.drop_refdes

    def test_dropped_pins_include_loc_and_plus50(self):
        """dropped_pins 含被抑制符号 loc 与 loc+(0,50)。"""
        insts = [_inst("GND_1", 100, 200), _inst("GND_2", 150, 210)]
        cm = _mk_coord_map(insts)
        plan = self._plan(insts, coord_map=cm)
        assert len(plan.drop_refdes) == 1
        assert (100, 200) in plan.dropped_pins or (150, 210) in plan.dropped_pins
        assert (100, 250) in plan.dropped_pins or (150, 260) in plan.dropped_pins


class TestGndTapWirePrune:
    def _plan_drop(self, dropped_refdes, insts, radius=2000):
        from cis2hdl.core.writer.gnd_placement import plan_gnd_aggregation

        cm = _mk_coord_map(insts)
        plan = plan_gnd_aggregation(insts, radius, coord_map=cm)
        assert dropped_refdes in plan.drop_refdes
        return plan

    def test_dangling_tap_pruned(self):
        """端点悬空的 tap wire（trunk 结点 → 符号引脚）整对象剪除。"""
        from cis2hdl.core.writer.gnd_placement import prune_gnd_tap_wires

        insts = [_inst("GND_1", 1000, 600), _inst("GND_2", 1200, 600),
                 _inst("C1", 900, 600, lib="C.Normal")]
        plan = self._plan_drop("GND_2", insts)
        # tap: GND_2 引脚 (1200,650) ← (1200,600 trunk 结点)
        # trunk: (900,600)-(1000,600)-(1200,600) 由多段 wire 表达
        wires = [
            _wire([(900, 600), (1000, 600)]),     # C1 → trunk（保留）
            _wire([(1000, 600), (1200, 600)]),    # trunk 段（保留）
            _wire([(1200, 600), (1200, 650)]),    # GND_2 tap（剪除）
        ]
        kept = prune_gnd_tap_wires(wires, plan.dropped_pins)
        assert len(kept) == 2
        assert all(w.end_y != 650 for w in kept)

    def test_trunk_segment_kept(self):
        """共享端点（degree≥2）的 trunk 段不因端点近符号被误剪。"""
        from cis2hdl.core.writer.gnd_placement import prune_gnd_tap_wires

        insts = [_inst("GND_1", 1000, 600), _inst("GND_2", 1200, 600),
                 _inst("C1", 900, 600, lib="C.Normal")]
        plan = self._plan_drop("GND_2", insts)
        wires = [
            _wire([(1200, 600), (1400, 600)]),    # trunk 继续段（保留）
            _wire([(1200, 600), (1200, 650)]),    # GND_2 tap（剪除）
        ]
        # 先有 trunk 段共享端点 (1200,600) → 该端点 degree≥2
        kept = prune_gnd_tap_wires(wires, plan.dropped_pins)
        assert len(kept) == 1
        assert kept[0].end_x == 1400

    def test_no_dropped_pins_no_change(self):
        from cis2hdl.core.writer.gnd_placement import prune_gnd_tap_wires

        wires = [_wire([(0, 0), (100, 0)])]
        kept = prune_gnd_tap_wires(wires, [])
        assert len(kept) == 1
