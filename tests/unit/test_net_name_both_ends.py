"""Phase XXVI F6 — 长网双端标签 + 删中间段 + 连通性断言（用户裁决 V4）。

SKiDL ``auto_stub`` 范式：引脚间距超阈值（或 break_long）的网 → 两端
各 1 个 SIG_NAME + 删连接岛的 trunk 中间段；删线后必须通过
``assert_net_connectivity``（岛内 BFS 可达 + 同网标签同名 + 无裸悬空端）。
"""

from __future__ import annotations

from cis2hdl.core.writer.net_name_connect import (
    assert_net_connectivity,
    net_name_both_ends,
)


def _pin(coord, refdes="U1", pin="1"):
    return {"refdes": refdes, "pin": pin, "coord": coord,
            "is_power_symbol": False}


def _build_3_island_net():
    """构造 3 岛网：岛 A（引脚 0/100）、岛 B（引脚 2500/2600）、
    岛 C（引脚 5000/5100）—— 岛间距 2400 > 2000，默认即触发双端；
    break_long 强制触发。
    """
    net_pin_map = {
        "NET_A": [
            _pin((0, 0), "U1", "1"), _pin((100, 0), "U1", "2"),
            _pin((2500, 0), "U2", "1"), _pin((2600, 0), "U2", "2"),
            _pin((5000, 0), "U3", "1"), _pin((5100, 0), "U3", "2"),
        ],
    }
    # 每岛 stub→本地 trunk + 岛间 trunk（水平 trunk y=0 结构）。
    wires = {
        "NET_A": [
            (0, 0, 100, 0),       # 岛 A 内部
            (100, 0, 2400, 0),    # 岛 A→B 连接 trunk（待删）
            (2500, 0, 2600, 0),   # 岛 B 内部
            (2600, 0, 4900, 0),   # 岛 B→C 连接 trunk（待删）
            (5000, 0, 5100, 0),   # 岛 C 内部
        ],
    }
    return net_pin_map, wires


class TestNetNameBothEnds:
    def test_no_trigger_when_close(self):
        """岛间距 ≤ max_dist 且未 break_long → 不删段、无标签。"""
        net_pin_map, wires = _build_3_island_net()
        labels, to_delete = net_name_both_ends(
            net_pin_map, wires, set(), False, max_dist=5000, break_long=False,
        )
        assert labels == []
        assert to_delete == []

    def test_break_long_triggers_both_ends(self):
        """break_long=true → 删 2 条岛间 trunk + 两端 2 标签。"""
        net_pin_map, wires = _build_3_island_net()
        labels, to_delete = net_name_both_ends(
            net_pin_map, wires, set(), False, max_dist=2000, break_long=True,
        )
        assert len(to_delete) == 2, to_delete
        # 被删段 = 岛间 trunk（A→B 与 B→C）。
        assert (100, 0, 2400, 0) in to_delete
        assert (2600, 0, 4900, 0) in to_delete
        # 两端各 1 标签（最远两端岛 A / C）。
        assert len(labels) == 2, labels
        assert labels[0][1] == "NET_A" and labels[1][1] == "NET_A"

    def test_max_dist_trigger(self):
        """岛间距 > max_dist → 触发双端（无需 break_long）。"""
        net_pin_map, wires = _build_3_island_net()
        # 岛间距 2400 > 1500 → 触发。
        labels, to_delete = net_name_both_ends(
            net_pin_map, wires, set(), False, max_dist=1500, break_long=False,
        )
        assert len(to_delete) == 2
        assert len(labels) == 2

    def test_single_island_noop(self):
        """单岛（引脚互近）→ 不触发。"""
        net_pin_map = {"N1": [_pin((0, 0)), _pin((100, 0))]}
        wires = {"N1": [(0, 0, 100, 0)]}
        labels, to_delete = net_name_both_ends(
            net_pin_map, wires, set(), False, max_dist=2000, break_long=True,
        )
        assert labels == [] and to_delete == []


class TestAssertNetConnectivity:
    def test_intact_single_island_ok(self):
        """未删段的单岛网 → 全断言通过。"""
        net_pin_map = {"N1": [_pin((0, 0), "U1", "1"), _pin((100, 0), "U1", "2")]}
        wires = {"N1": [(0, 0, 100, 0)]}
        ok, violations = assert_net_connectivity(net_pin_map, wires, [])
        assert ok, violations

    def test_after_delete_islands_ok_with_labels(self):
        """删中间段后：各岛内部连通 + 两端标签同名 + 无裸悬空端。"""
        net_pin_map, wires = _build_3_island_net()
        labels, to_delete = net_name_both_ends(
            net_pin_map, wires, set(), False, max_dist=2000, break_long=True,
        )
        remaining = {
            "NET_A": [w for w in wires["NET_A"] if w not in to_delete],
        }
        ok, violations = assert_net_connectivity(
            net_pin_map, remaining, labels,
        )
        assert ok, violations

    def test_disconnected_island_detected(self):
        """删段后某岛引脚不可达 → 断言失败。"""
        net_pin_map = {
            "N1": [
                _pin((0, 0)), _pin((100, 0)),
                _pin((2000, 0)), _pin((2100, 0)),
            ],
        }
        # 岛 A 连通；岛 B 的 stub（2000,0 → 2100,0）缺失 → (2100,0) 不可达。
        wires = {"N1": [(0, 0, 100, 0)]}
        ok, violations = assert_net_connectivity(net_pin_map, wires, [])
        assert not ok
        assert any("not reachable" in v for v in violations)

    def test_unlabeled_dangling_detected(self):
        """裸悬空端（非引脚/标签/交点）→ 断言失败。"""
        net_pin_map = {"N1": [_pin((0, 0)), _pin((100, 0))]}
        wires = {"N1": [(0, 0, 100, 0), (100, 0, 300, 0)]}
        ok, violations = assert_net_connectivity(net_pin_map, wires, [])
        assert not ok
        assert any("dangling" in v for v in violations)

    def test_wrong_label_name_detected(self):
        """标签坐标不在其网 WIRE 端点/引脚上（挂错网名）→ 断言失败。"""
        net_pin_map = {"N1": [_pin((0, 0)), _pin((100, 0))]}
        wires = {"N1": [(0, 0, 100, 0)]}
        ok, violations = assert_net_connectivity(
            net_pin_map, wires, [((50, 0), "WRONG_NAME")],
        )
        # ② 标签 (50,0) 不在网 WRONG_NAME 的 WIRE 端点/引脚上。
        assert not ok
        assert any("not on net wire/pin" in v for v in violations)
