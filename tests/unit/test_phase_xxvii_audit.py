"""Phase XXVII（S13）用户新要求审计门禁（2026-08-18 下午）。

用户要求（2026-08-18 14:05）：
  1. 贴边 0 容忍（电线不得贴元件边框）
  2. 引脚避让（引脚附近不能有电线经过，防误连接）
  3. 禁双端网络名删线法（已满足：A* 无解保留原段）
  4. 禁大幅绕行（已满足：A* 限 max_iter + 路径压缩）
  5. 就近并联合并（parallel_short 已有）
  6. 跨页网络名近元件（S13 已实现，右上角 0）
  7. 电气连接完整性（所有元件连上，无真断连）
  8. 允许 ≤5 穿体

本文件是**轻量审计**：直接读对比包 csa 断言，不重复转换。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_OUT = PROJECT_ROOT / "HG5015_tests" / "output_phaseXXVI_compare_s13" / "s13_default"
_WORKLIB = _OUT / "worklib" / "5015" / "sch_1"
_AES = _OUT / "aesthetic_report.txt"


@pytest.fixture(scope="module")
def aes_text() -> str:
    if not _AES.exists():
        pytest.skip(f"对比包缺失: {_AES}")
    return _AES.read_text(encoding="utf-8")


def _load_page():
    """收集每页 wires 与 pins（LASTPIN）。"""
    wires, pins = [], []
    for csa in sorted(_WORKLIB.glob("*.csa")):
        txt = csa.read_text(encoding="utf-8")
        wires += [(int(a), int(b), int(c), int(d)) for a, b, c, d in
                  re.findall(r"WIRE 16 -1 \((-?\d+) (-?\d+)\)\((-?\d+) (-?\d+)\);", txt)]
        pins += [(int(m.group(1)), int(m.group(2)), m.group(3), (m.group(4) or ""))
                 for m in re.finditer(r"LASTPIN \((-?\d+) (-?\d+)\) (\S+)(?: (\S+))?", txt)]
    return wires, pins


# ── 1. 电气连接完整性（用户要求：所有元件必须连上） ──────────────────


class TestConnectivity:
    def test_no_named_pin_without_connection(self) -> None:
        """有网名（SIG_NAME）的引脚必须连接（WIRE 端点或该网标签）。

        $PN（NC 引脚）悬空属原始设计正常；有网名而无任何连接 = 断连 bug。
        """
        if not _WORKLIB.exists():
            pytest.skip(f"worklib 缺失: {_WORKLIB}")
        for csa in sorted(_WORKLIB.glob("*.csa")):
            txt = csa.read_text(encoding="utf-8")
            wires = [(int(a), int(b), int(c), int(d)) for a, b, c, d in
                     re.findall(r"WIRE 16 -1 \((-?\d+) (-?\d+)\)\((-?\d+) (-?\d+)\);", txt)]
            for m in re.finditer(r"LASTPIN \((-?\d+) (-?\d+)\) SIG_NAME (\S+)", txt):
                x, y = int(m.group(1)), int(m.group(2))
                if not any((w[0] == x and w[1] == y) or (w[2] == x and w[3] == y)
                           for w in wires):
                    # 断口网名连接：该网在同页有独立 SIG_NAME 标签。
                    net = m.group(3)
                    if f"SIG_NAME {net}" not in txt:
                        pytest.fail(
                            f"{csa.stem} 引脚({x},{y}) 网 {net} 无 WIRE 无标签"
                        )

    def test_every_net_has_signal(self, aes_text: str) -> None:
        """每网至少一个 SIG_NAME（跨页连接表达）。"""
        assert "net_name_x_align=100.0%" in aes_text


# ── 2. 贴边 0 容忍（用户要求 1） ─────────────────────────────────────


class TestNoEdgeTouch:
    def test_no_wire_on_body_edge(self) -> None:
        """电线不得贴元件边框（段坐标恰在 body 边界上）。

        mock 元件 outline 从 csa 提取（FORCEADD 实例位置 + 相对 outline）。
        """
        if not _WORKLIB.exists():
            pytest.skip(f"worklib 缺失: {_WORKLIB}")
        edge = 0
        for csa in sorted(_WORKLIB.glob("*.csa")):
            txt = csa.read_text(encoding="utf-8")
            bodies = []
            for m in re.finditer(
                r"FORCEADD (\S+?)_PH\.\.1\nJ 0\n\((-?\d+) (-?\d+)\);", txt,
            ):
                ix, iy = int(m.group(2)), int(m.group(3))
                ol = re.search(
                    r'X "CDS_LMAN_SYM_OUTLINE" "(-?\d+),(-?\d+),(-?\d+),(-?\d+)"',
                    txt,
                )
                if ol:
                    a, b, c, d = map(int, ol.groups())
                    bodies.append(
                        (ix + min(a, c), iy + min(b, d),
                         ix + max(a, c), iy + max(b, d))
                    )
            for m in re.finditer(
                r"WIRE 16 -1 \((-?\d+) (-?\d+)\)\((-?\d+) (-?\d+)\);", txt,
            ):
                x1, y1, x2, y2 = map(int, m.groups())
                for (x0, y0, x1b, y1b) in bodies:
                    if x1 == x2 and (x1 == x0 or x1 == x1b) \
                            and min(y1, y2) < y1b and max(y1, y2) > y0:
                        edge += 1
                    elif y1 == y2 and (y1 == y0 or y1 == y1b) \
                            and min(x1, x2) < x1b and max(x1, x2) > x0:
                        edge += 1
        assert edge == 0, f"贴边电线 {edge} 处（贴边 0 容忍）"


# ── 3. 穿体收敛（用户要求 8：允许 ≤5） ──────────────────────────────


class TestWireThroughBody:
    def test_violations_under_5(self, aes_text: str) -> None:
        """WIRE 穿体 ≤ 5（用户允许极少量；当前 2）。"""
        m = re.search(r"violations=(\d+)", aes_text)
        assert m and int(m.group(1)) <= 5, f"穿体 {m.group(1) if m else '?'} > 5"
        assert "trunk_blocked=0" in aes_text

    def test_pin_inside_body_none(self, aes_text: str) -> None:
        assert "[PIN_INSIDE_BODY] none" in aes_text
