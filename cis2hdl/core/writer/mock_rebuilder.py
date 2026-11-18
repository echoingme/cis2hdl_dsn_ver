"""1:1 几何复刻编排（B2）：R3 未命中电气件 → temp_lib 符号。

消费 ``ComponentDef.symbol_geometry``（R-GEO 结构化字段，44/44 符号
已就绪）：未命中 hdl_lib 的电气元件用 **DSN 原始几何**复刻 mock 图标
（不再自绘占位），落 ``output/temp_lib/`` 标准结构：

::

    <CELL>/sym_1/symbol.css   ← DSN 几何指令（dsn_geometry）+ P 属性模板
    <CELL>/sym_1/master.tag   ← "symbol.css\\n"
    <CELL>/chips/chips.prt    ← 复用 mock_icon_lib._chips_prt（PINUSE 映射）
    <CELL>/chips/master.tag
    <CELL>/entity/*           ← 复用 mock_icon_lib._entity_files

高内聚：复刻编排唯一收敛；低耦合：几何翻译依赖 ``dsn_geometry`` 纯函数，
chips.prt/entity 复用 ``mock_icon_lib`` 已验证静态方法（零重复实现），
不触碰 parser/matcher 内部。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

from cis2hdl.core.ir.component import ComponentDef
from cis2hdl.core.writer import dsn_geometry
from cis2hdl.core.writer.mock_icon_lib import MockIconLibrary, MockSymbol

logger = logging.getLogger(__name__)

#: chips.prt / entity 静态方法（复用 mock_icon_lib 已验证格式，零重复实现）。
_mock_chips_prt = MockIconLibrary._chips_prt
_mock_entity_files = MockIconLibrary._entity_files

#: 默认 P 属性模板（对齐 s16/v9 已验证声明 —— 2026-09-04 SPCOCD-553 v4：
#: 08-21 迁移时误砍 6 个属性，仅剩 3 条；v9（08-19，Cadence 实测可加载）
#: 与 mock_icon_lib._symbol_css 均为 9 条（+JEDEC_TYPE/PATH/PACKAGE_TYPE/
#: DESCRIPTION/SN_NUM/MOCK_TEXT），HDL 库为 8 条。恢复 v9 全集防库级
#: connectivity 加载失败，注释防再砍）。
_PROP_TEMPLATE = [
    'P "$LOCATION" "?" -5 -100 90 0 40 0 0 1 0 0 1 0 0',
    'P "VALUE" "?" -5 100 90 0 40 0 0 1 0 0 1 0 32',
    'P "PART_NAME" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "JEDEC_TYPE" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "PATH" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "PACKAGE_TYPE" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "DESCRIPTION" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "SN_NUM" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
    'P "MOCK_TEXT" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32',
]


@dataclass
class RebuildStats:
    """复刻统计（QA 用）。"""

    cells: int = 0
    with_geometry: int = 0
    fallback_geometry: int = 0
    written: list[str] = field(default_factory=list)


class MockRebuilder:
    """R3 电气件 → temp_lib 1:1 几何复刻编排。"""

    def __init__(
        self,
        enabled: bool = True,
        lib_name: str = "TG0C0D2_VB_lib",
        syntax_check: bool = True,
        structure_check: bool = True,
        annotate: bool = True,
        mock_text_cmd: str = "T",
    ) -> None:
        self._enabled = enabled
        self._lib_name = lib_name
        self._syntax_check = syntax_check
        self._structure_check = structure_check
        # 2026-09-03 SPCOCN-571：复刻 cell 亦标注 MOCK（默认开；语义同
        # TempLibCfg.annotate / mock_text_cmd——T 指令可见文本）。
        self._annotate = annotate
        self._mock_text_cmd = mock_text_cmd
        self._stats = RebuildStats()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def lib_name(self) -> str:
        """mock 库目录名（routing.yaml temp_lib.lib_name 驱动）。"""
        return self._lib_name

    @property
    def stats(self) -> RebuildStats:
        return self._stats

    # ── 单符号构建 ─────────────────────────────────────────────

    def build_symbol_css(self, src: ComponentDef) -> str:
        """源（extra_data.symbol_geometry）→ symbol.css 内容。

        - 有几何：``dsn_geometry.symbol_geometry_to_css``（1:1 复刻）；
        - 无几何：降级空 outline（仅属性模板，Cadence 语法仍有效）；
        - 2026-08-21：``extra_data['pin_object_offsets']``（页面流引脚
          对象区，U7 六段独立芯片）→ 全量引脚 C 命令（引脚名键），
          使 LASTPIN SIG_NAME 标签可发射（此前仅 cache_pin_defs 的
          9 条/段）。
        """
        geo = src.extra_data.get("symbol_geometry") or {}
        pin_labels = src.extra_data.get("pin_labels") or {}
        lines: list[str] = []
        if geo.get("body_box"):
            self._stats.with_geometry += 1
        else:
            self._stats.fallback_geometry += 1
        lines.extend(dsn_geometry.symbol_geometry_to_css(geo, pin_labels))
        # 2026-09-03 SPCOCN-571：1:1 几何复刻 mock cell 也标注 MOCK 大字
        # （醒目区分于 hdl_lib 真实符号；mentor"LB33 只有小方框、无 MOCK"）。
        # 与 mock_icon_lib 同款 T 指令两行结构；字号随符号宽度自适应并
        # 置于符号上方，避免压盖 1:1 复刻图形与 $LOCATION(-100) 标签。
        if self._annotate and geo.get("body_box"):
            _bb = geo["body_box"]
            try:
                _bw = abs(float(_bb[2]) - float(_bb[0]))
                _y_top = max(float(_bb[1]), float(_bb[3]))
            except (TypeError, ValueError, IndexError):
                _bw, _y_top = 40.0, 40.0
            _fs = max(24, min(64, int(_bw * 0.9)))
            if self._mock_text_cmd == "T":
                lines.append(f"T 0 {int(_y_top) + 160} 0 0 {_fs} 0 0 0 0 4 0")
                lines.append("MOCK")
            else:
                lines.append(
                    f'P "MOCK_TEXT" "MOCK" 0 {int(_y_top) + 160} 0 0 {_fs}'
                    " 0 0 0 0 0 0 0 0"
                )
        # 页面流引脚对象偏移 → C 命令（引脚名键；符号局部坐标）
        offsets = src.extra_data.get("pin_object_offsets") or {}
        for _pin, (_dx, _dy) in sorted(offsets.items()):
            lines.append(
                f'C {_dx} {_dy} "{_pin}" {_dx} {_dy} 0 0 16 1 R'
            )
        lines.extend(_PROP_TEMPLATE)
        return "\n".join(lines) + "\n"

    def cell_name(self, src: ComponentDef) -> str:
        """cell 名（``PH_<REFDES>``，大写；U7 分段带 section）。

        2026-08-21：改用 ``PH_`` 前缀——旧格式 ``{REFDES}_PH`` 对
        L 开头 refdes 生成 ``L1_PH``，Cadence DEHDL 把首字母 L 当作
        库/view 标识吞掉（报错显示 ``1_PH``）导致 SPCOCD-553。
        """
        ref = src.extra_data.get("refdes", "") or src.refdes or "X"
        sec = int(src.extra_data.get("section", 1) or 1)
        suffix = f"_{sec}" if sec > 1 else ""
        return f"PH_{ref}{suffix}".upper()

    def _mock_symbol(self, src: ComponentDef) -> MockSymbol:
        """构造 MockSymbol（供 chips.prt/entity 复用；几何自 symbol_geometry）。"""
        pins = list((src.extra_data.get("pin_connections") or {}).keys())
        names = list((src.extra_data.get("pin_labels") or {}).values())
        geo = src.extra_data.get("symbol_geometry") or {}
        bb = geo.get("body_box") or [0, 0, 0, 0]
        outline = f"{bb[0]},{bb[1]},{bb[2]},{bb[3]}"
        sym = MockSymbol(
            cell_name=self.cell_name(src),
            pin_numbers=pins or [str(i) for i in range(1, 5)],
            pin_names=names,
            outline=outline,
            kind="rebuilt",
        )
        return sym

    # ── 落盘 ───────────────────────────────────────────────────

    def write_to_temp_lib(
        self,
        unmatched: Iterable[ComponentDef],
        out_dir: Path,
    ) -> list[Path]:
        """R3 未命中电气件 → temp_lib 标准结构。

        Args:
            unmatched: 路由 R3 的源 ComponentDef 列表。
            out_dir: ``output/`` 根目录（``out_dir/temp_lib/`` 下）。

        Returns:
            写出的文件路径列表。
        """
        if not self._enabled:
            logger.info("MockRebuilder 已禁用（enabled=False）")
            return []
        lib_root = Path(out_dir) / self._lib_name
        written: list[Path] = []
        for src in unmatched:
            try:
                written.extend(self._write_one(src, lib_root))
                self._stats.cells += 1
            except Exception as exc:  # 单符号失败不阻断批次
                logger.warning("复刻 %s 失败: %s", src.library_id, exc)
        logger.info(
            "temp_lib 复刻: %d cell（几何 %d / 降级 %d）→ %s",
            self._stats.cells, self._stats.with_geometry,
            self._stats.fallback_geometry, lib_root,
        )
        return written

    def _write_one(self, src: ComponentDef, lib_root: Path) -> list[Path]:
        cell = self.cell_name(src)
        cell_dir = lib_root / cell
        sym_dir = cell_dir / "sym_1"
        sym_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        css = sym_dir / "symbol.css"
        css.write_text(self.build_symbol_css(src), encoding="utf-8")
        written.append(css)
        self._stats.written.append(str(css))

        (sym_dir / "master.tag").write_text("symbol.css\n", encoding="utf-8")
        written.append(sym_dir / "master.tag")

        sym = self._mock_symbol(src)
        chips_dir = cell_dir / "chips"
        chips_dir.mkdir(parents=True, exist_ok=True)
        prt = chips_dir / "chips.prt"
        prt.write_text(_mock_chips_prt(sym), encoding="utf-8")
        written.append(prt)
        (chips_dir / "master.tag").write_text("chips.prt\n", encoding="utf-8")
        written.append(chips_dir / "master.tag")

        ent_dir = cell_dir / "entity"
        ent_dir.mkdir(parents=True, exist_ok=True)
        # entity master.tag + pc.db（对齐 mock_icon_lib R2 结构断言）。
        (ent_dir / "master.tag").write_text("verilog.v\n", encoding="utf-8")
        written.append(ent_dir / "master.tag")
        (ent_dir / "pc.db").write_text(
            MockIconLibrary._entity_pc_db(sym), encoding="utf-8"
        )
        written.append(ent_dir / "pc.db")
        for fname, content in _mock_entity_files(sym).items():
            p = ent_dir / fname
            p.write_text(content, encoding="utf-8")
            written.append(p)

        # 结构/语法自检（对齐 mock_icon_lib R1/R2）。
        if self._syntax_check or self._structure_check:
            try:
                from cis2hdl.core.writer.validate_symbol_css import (
                    validate_symbol_css,
                )
                validate_symbol_css(css)
            except Exception as exc:
                logger.warning("symbol.css 校验警告 %s: %s", cell, exc)
        return written
