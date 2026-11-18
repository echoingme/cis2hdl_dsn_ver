"""MockIconLibrary — temp_lib 模拟图标生成（Phase XVII M1，用户 D3/D8/D11）。

为未匹配到具体 HDL 符号的芯片/connector（如 U6 主芯片、J4 等）生成
**模拟图标**（替代 placeholder 占位方块），写入独立 ``output/temp_lib/``
（不污染 hdl_lib）。图标按硬件设计规范 §2.2.2 绘制：

* 引脚分布三档（统一 ``distribute_mock_pin_offsets``）：
    - n ≤ 12       ：左右两列（沿用 ``distribute_ic_pin_offsets``）；
    - 12 < n ≤ 64  ：四列 -200/-100/+100/+200，pitch ≥ 50（修 pitch<50 bug）；
    - n > 64       ：**BGA 矩形四边**（顶/底/左/右，引脚朝外，坐标算法见
      phase17-supplement 第三部分）。
* 功能名标签（CIS 原引脚功能名；重复加序号后缀 GND/GND_2…；空回退引脚号）；
* 标签旋转对齐（顶 0°/右 90°/底 180°/左 270°，对齐规则见 supplement）；
* 生成 symbol.css + chips.prt + entity（目录大写，cell 名 ``<REFDES>_PH``，
  含 ``MOCK/模拟图标`` 标注文本，字号 24 —— 可见图形文本而非被删属性）。

数据链路（与 csa_writer 硬约束）：mock 图标 C 指令偏移 = csa_writer
pin_coords 偏移（同源），LASTPIN/WIRE 精确重合。

设计原则（STANDARDS Part I）：独立模块 + 配置开关（``temp_lib.enabled``
默认 true）；placeholder 保留为逃生舱（``enabled=false`` 回退）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  纯几何：三档引脚分布（n≤12 两列 / 12<n≤64 四列 / n>64 BGA 四边）
# ---------------------------------------------------------------------------


#: Phase XXVI（S11 视觉审查 26 条问题，用户裁决 V1）：引脚名文本渲染
#: 宽度。X "PIN_TEXT" 字号由 29 降为 **22** 后实测渲染字符宽 ~16-18，
#: 全项目统一取 **18**（旧值 28 是 Phase XXI 矫枉过正 —— 把 outline 拉
#: 到 1500~3800 宽，压元件、引脚半在框内、SPCOCN-543 属性被删）。
_CHAR_W: int = 18

#: 引脚外列与边框之间的基础余量（= 引脚 L 线长 + tip 外 80 + C 号区）。
#: Phase XXVI（F1）：outline 与标签长度**解耦** —— outline 由引脚数
#: 分档决定（``mock_outline`` 本体优先），引脚列从 ``half +
#: _PIN_LINE_LEN + _LABEL_MARGIN`` 开始向外排。全部对齐 50 栅格。
_LABEL_MARGIN: int = 200

#: 相邻列之间需容纳的最小间隙基础（X 长名 tip 外 80 + 余量）。
#: 铁律 ``列距 ≥ max_len*18 + 80`` 由 ``mock_text_overlap_count``
#: 自检兜底（``symbol_for`` 增宽循环 ≤3 轮）。
_COL_GAP_REQ: int = 160

#: 引脚 L 段向外长度（对齐 ``temp_lib.pin_line_len`` 默认 50）。
_PIN_LINE_LEN: int = 50


def _tier_half_width(pin_count: int) -> int:
    """Return the body half width for a mock icon (body-first, F1).

    Phase XXVI（用户裁决 V1：图标宽度至少缩到 1/3）：outline 只由
    引脚数分档决定，**与标签长度无关** —— 旧实现把标签文本宽塞进
    outline（U6H 3000/U6I 2400），是"图标过宽、引脚半在框内"的根因。

    Args:
        pin_count: Number of connected pins.

    Returns:
        Half width（outline 宽度 = 2×half）：
        n≤12 → 150（宽 300）；13≤n≤64 → 200（宽 400）；n>64 → 300（宽 600）。
    """
    n = max(int(pin_count or 0), 1)
    if n <= 12:
        return 150
    if n <= 64:
        return 200
    return 300


def _min_mock_width(refdes: str, pin_count: int) -> int:
    """Return the minimum mock pin-span clamp (F1 1/3 兜底).

    Phase XXVI：删除 per-refdes 覆盖（U6H 3000/U6I 2400/U6A 2400/U12
    1200 全部移除）；分档兜底降为 —— BGA（n>64）1000、四列中大型
    （24≤n≤64）800、四列小型（13≤n≤23）400、两列（n≤12）不钳制
    （公式即可）。仅对**极端长标签**触发，正常布局不再钳到 3000。
    全部 50 栅格对齐在 ``distribute_mock_pin_offsets`` 内完成。

    Args:
        refdes: Instance reference designator (e.g. ``"U6H"``).
        pin_count: Number of connected pins.

    Returns:
        Minimum pin span in schematic units (0 = no clamp).
    """
    n = max(int(pin_count or 0), 1)
    if n > 64:
        return 1000
    if n >= 24:
        return 800
    if n >= 13:
        return 400
    return 0


def distribute_mock_pin_offsets(
    pin_count: int,
    pin_names: Iterable[str] | None = None,
    pitch: int = 50,
    min_width: int = 0,
) -> dict[int, tuple[int, int, str]]:
    """Distribute N pins across a mock icon body (three tiers).

    Phase XXVI（S11 视觉审查，用户裁决 V1/V7）：**引脚从边框向外排**，
    outline 与标签长度解耦（F1）：
    * 列起点 = ``half + _PIN_LINE_LEN + _LABEL_MARGIN``（全部列在
      outline 外 —— 引脚 tip 数学上 > 框半宽，消除"内列引脚在框内"
      的结构性错误）；
    * 字符宽 28→**18**、基础边距 355→**200**、列距基础 255→**160**
      （列距仍需 ≥ max_len*18 + 80，由 ``mock_text_overlap_count``
      自检 + ``symbol_for`` 增宽循环兜底）；
    * ``min_width`` 钳制降为 1/3 兜底（>64→1000、24-64→800、
      13-23→400；仅为极端长标签兜底，正常不再触发）；
    * n≤12 两列、13≤n≤64 四列、n>64 BGA 多列 —— 全部框外、50 栅格。

    Args:
        pin_count: Number of connected pins.
        pin_names: Optional pin names (used to size the label area and
            to stabilise the BGA side assignment; may be empty).
        pitch: Small-chip column pitch (n ≤ 12, Phase XXI default 50).
        min_width: Minimum pin-span clamp (0 = formula only).

    Returns:
        ``{index: (x, y, side)}`` — 1-based position index → relative
        offset + side ("left"/"right"/"top"/"bottom").  The caller maps
        them onto the instance's real pin numbers/names.
    """
    n = max(int(pin_count), 1)
    names = list(pin_names or [])
    offsets: dict[int, tuple[int, int, str]] = {}

    half = _tier_half_width(n)
    _max_len = max((len(str(x)) for x in names), default=2)
    # 外列 = 边框半宽 + 引脚线长 + 标签余量（与标签长度解耦的基础列）。
    _label_span = _max_len * _CHAR_W
    _col0 = max(
        ((half + _PIN_LINE_LEN + _LABEL_MARGIN + 49) // 50) * 50, 250,
    )
    if min_width > 0:
        # min_width 只影响引脚跨度（列位），不影响 outline（本体优先）。
        _col0 = max(
            _col0, ((min_width // 2 + _PIN_LINE_LEN + 49) // 50) * 50,
        )
    # 相邻列间距：至少容纳最长标签 + 间隙（下限 150）。
    _col_step = max(150, ((_label_span + _COL_GAP_REQ + 49) // 50) * 50)

    if n <= 12:
        half = (n + 1) // 2
        # Phase XIX：小芯片仍对称 top→bottom 两列。
        y_start = 100
        pitch = max(int(pitch) or 50, 50)
        for i in range(1, n + 1):
            if i <= half:
                offsets[i] = (-_col0, y_start - (i - 1) * pitch, "left")
            else:
                j = i - half
                offsets[i] = (_col0, y_start - (j - 1) * pitch, "right")
        return offsets

    if n <= 64:
        # 四列：全部列在 outline 外（最内列 > half + 引脚线长）。
        # 内列 = 边框外最近；外列 = 内列 + 列距（列距 ≥ 标签宽 + 间隙）。
        _lab2 = max(half + _PIN_LINE_LEN, 250)
        _col0 = _lab2 + max(150, ((_label_span + _COL_GAP_REQ + 49) // 50) * 50)
        if min_width > 0:
            _col0 = max(
                _col0, ((min_width // 2 + _PIN_LINE_LEN + 49) // 50) * 50,
            )
        cols = 4
        per_col = (n + cols - 1) // cols
        col_pitch = 150   # Phase XXVII（A3）：100→150（引脚间距拉大）
        col_x = (-_col0, -_lab2, _lab2, _col0)
        sides = ("left", "left", "right", "right")
        for i in range(1, n + 1):
            col = (i - 1) // per_col
            row = (i - 1) % per_col
            offsets[i] = (col_x[col], 300 - row * col_pitch, sides[col])
        return offsets

    # ── BGA 多列两侧（n > 64）──────────────────────────────────────
    # Phase XVIII R9 修复（SPCOCN-1158）：左右两侧多列（对齐真实库
    # 形态，引脚仅 x 方向伸出，py 全在 outline y 内）。
    cols_per_side = max(1, (n + 47) // 48)  # 每侧列数（每列 ≤ 48 行）
    cols = cols_per_side * 2
    per_col = (n + cols - 1) // cols
    row_pitch = 150  # Phase XXVII（A3）：100→150（行距拉大，引脚不重叠）
    col_gap = _col_step
    _col0 = max(_col0, half + _PIN_LINE_LEN + _LABEL_MARGIN)
    for i in range(1, n + 1):
        col = (i - 1) // per_col
        row = (i - 1) % per_col
        if col < cols_per_side:
            x = -_col0 - (cols_per_side - 1 - col) * col_gap
            side = "left"
        else:
            x = _col0 + (col - cols_per_side) * col_gap
            side = "right"
        offsets[i] = (x, 300 - row * row_pitch, side)
    return offsets


def mock_outline(pin_count: int, offsets: dict[int, tuple[int, int, str]]) -> str:
    """CDS_LMAN_SYM_OUTLINE rectangle (body-first, F1).

    Phase XXVI（S11 视觉审查，用户裁决 V1/V7）：outline 与标签长度
    **解耦** —— 只由引脚数分档决定（``_tier_half_width``：n≤12 → 半宽
    150、13-64 → 200、>64 → 300），不再由引脚 offset 反推内缩。引脚
    全部位于 outline 外（``distribute_mock_pin_offsets`` 从边框向外排）。

    Phase XVIII R9 修复（SPCOCN-1158）保留：outline y 覆盖引脚 y
    （仅 x 方向引脚伸出）—— 侧边引脚 L 线起点 ``(±x_edge, py)`` 的 py
    全部落在 outline y 范围内，不产生悬空。

    Args:
        pin_count: Number of pins.
        offsets: ``distribute_mock_pin_offsets`` output (y extent source).

    Returns:
        ``"x1,y1,x2,y2"`` outline string.
    """
    ys = [off[1] for off in offsets.values()]
    half = _tier_half_width(pin_count)
    if not ys:
        return "-300,300,300,-300"
    # y 方向覆盖引脚 y（R9 铁律），向外留 50 余量。
    y_top = max(ys) + 50
    y_bot = min(ys) - 50
    return f"{-half},{y_top},{half},{y_bot}"


def mock_text_overlap_count(
    offsets: dict[int, tuple[int, int, str]],
    labels: dict[int, str],
    pin_numbers: dict[int, str] | None = None,
    char_w: int = _CHAR_W,
    y_tol: int = 30,
) -> int:
    """Count C-short-number vs X-long-name text overlaps (Phase XXI F3).

    用户 P13 授权"重叠检测避让函数"：mock 引脚布局自检 —— 同 y（±
    ``y_tol``）的 C 短号文本矩形与相邻 X 长名文本矩形在 x 方向重叠即计
    1。几何口径与 ``_append_pin_line`` 完全一致：

    * C 短号：left 贴 outline 边 ``x0+25``（钳制不落框外）、right 贴
      ``x1-25``、top/bottom 在 px；文本半宽 ``14``（短号 1-2 字符）；
    * X 长名：left/top 锚定 ``px-80`` justify=1 向左延伸、right/bottom
      锚定 ``px+80`` justify=0 向右延伸，文本宽 ``len*char_w``；
    * outline 由 offsets 同源推导（inset 100/50 规则与 ``mock_outline``
      一致）。

    布局公式（``distribute_mock_pin_offsets``）已按铁律
    ``列距 ≥ max_len*28 + 255`` 保证 0 碰撞 —— 本函数是防回归自检 +
    ``symbol_for`` 增宽循环的判据。

    Args:
        offsets: ``distribute_mock_pin_offsets`` 输出（位置索引 → 偏移）。
        labels: 位置索引 → X 长名（功能名标签）。
        pin_numbers: 位置索引 → C 短号文本（缺省回退位置索引）。
        char_w: 文本字符宽（默认 28，与布局口径一致）。
        y_tol: 同 y 判定容差（默认 30）。

    Returns:
        重叠对数（0 = 通过）。
    """
    if not offsets:
        return 0
    half = _tier_half_width(len(offsets))
    x0 = -half
    x1 = half
    cw = max(int(char_w) or 1, 1)

    def _c_rect(idx: int) -> tuple[int, int, int]:
        """(x_lo, x_hi, y) for the C short-number text."""
        px, py, side = offsets[idx]
        if side == "left":
            cx = max(px + 25, int(x0) + 25)
        elif side == "right":
            cx = min(px - 25, int(x1) - 25)
        else:
            cx = px
        return (cx - 14, cx + 14, py)

    def _x_rect(idx: int) -> tuple[int, int, int]:
        """(x_lo, x_hi, y) for the X long-name text."""
        px, py, side = offsets[idx]
        text_w = len(str(labels.get(idx, ""))) * cw
        if side == "left":
            return (px - 80 - text_w, px - 80, py)
        if side == "right":
            return (px + 80, px + 80 + text_w, py)
        if side == "top":
            return (px - text_w, px, py - 50)
        return (px, px + text_w, py + 50)

    count = 0
    n = max(offsets.keys(), default=0)
    for i in range(1, n + 1):
        if i not in offsets:
            continue
        ci = _c_rect(i)
        for j in range(1, n + 1):
            if j == i or j not in offsets:
                continue
            xj = _x_rect(j)
            if abs(ci[2] - xj[2]) > y_tol:
                continue
            if ci[1] > xj[0] and ci[0] < xj[1]:
                count += 1
    return count


def unique_functional_labels(
    pin_numbers: Iterable[str], pin_names: Iterable[str],
) -> dict[int, str]:
    """Functional-name labels with duplicate suffixes + empty fallback.

    CIS 原引脚功能名去重：重复加序号后缀（GND → GND_2 → GND_3…）；
    空功能名回退引脚号。

    Args:
        pin_numbers: Pin numbers (in position order).
        pin_names: Pin function names (may repeat / be empty).

    Returns:
        ``{index: label}`` for the C-command text (1-based position index).
    """
    numbers = [str(x) for x in pin_numbers]
    names = [str(x or "") for x in pin_names]
    counts: dict[str, int] = {}
    labels: dict[int, str] = {}
    for i, name in enumerate(names):
        idx = i + 1
        if not name:
            labels[idx] = numbers[i] if i < len(numbers) else str(idx)
            continue
        cnt = counts.get(name, 0) + 1
        counts[name] = cnt
        labels[idx] = name if cnt == 1 else f"{name}_{cnt}"
    return labels


# ---------------------------------------------------------------------------
#  模拟图标数据结构
# ---------------------------------------------------------------------------


@dataclass
class MockSymbol:
    """A generated mock-icon symbol for one unmatched multi-pin IC."""

    cell_name: str
    """HDL cell/directory name (``<refdes>_PH``, uppercased)."""

    pin_numbers: list[str]
    """EDIF pin numbers (unique)."""

    pin_names: list[str]
    """EDIF pin function names (may repeat / be empty)."""

    offsets: dict[str, tuple[int, int]] = field(default_factory=dict)
    """pin number (and unique pin name) → (x, y) relative offset."""

    sides: dict[str, str] = field(default_factory=dict)
    """pin number → side ("left"/"right"/"top"/"bottom")."""

    labels: dict[str, str] = field(default_factory=dict)
    """pin number → C-command display label (functional name, suffixed)."""

    outline: str = "-150,150,150,-150"

    #: 符号种类（M1 模拟图标；csa_writer 据此区分 PLACEHOLDER 属性发射）。
    kind: str = "mock"

    @property
    def pin_count(self) -> int:
        """Number of pins on the mock symbol."""
        return len(self.pin_numbers)

    def offset_for(self, pin_number: str, pin_name: str = "") -> tuple[int, int]:
        """Resolve a pin's relative offset by number then name.

        Phase XXI F3（U5_PH 310 实测）：``offsets`` 的**名称键**带前缀
        ``"name:"`` 隔离 —— BGA 引脚号（如 U5 的 ``A7``）可能恰好等于
        另一引脚的功能名（如 DDR 地址线 ``A7``），旧实现两者共用裸键
        → 功能名覆盖引脚号坐标 → 两个引脚同一坐标（SPCOCN-310）。这里
        查询名称键时加同前缀。

        Args:
            pin_number: The instance pin number.
            pin_name: The instance pin name (fallback key).

        Returns:
            ``(x, y)`` relative offset; ``(0, 0)`` when unresolved.
        """
        if pin_number in self.offsets:
            return self.offsets[pin_number]
        if pin_name:
            _name_key = f"name:{pin_name}"
            if _name_key in self.offsets:
                return self.offsets[_name_key]
        return (0, 0)


# ---------------------------------------------------------------------------
#  模拟图标库（生成 + 写入）
# ---------------------------------------------------------------------------


class MockIconLibrary:
    """Generates and persists mock-icon symbols into ``output/temp_lib/``.

    Usage::

        lib = MockIconLibrary()
        sym = lib.symbol_for("U6", 1, [("K18", "A0"), ("G20", "A1")])
        lib.write_to_temp_lib(Path("output/temp_lib"))
        # csa_writer uses ``sym.cell_name`` + ``sym.offsets`` directly.
    """

    def __init__(
        self,
        enabled: bool = True,
        lib_name: str = "TG0C0D2_VB_lib",
        annotate: bool = True,
        mock_text: str = "MOCK/模拟图标",
        pin_font_size: int = 16,
        pin_text_size: int = 16,
        pin_line_len: int = 50,
        mock_text_cmd: str = "T",
        syntax_check: bool = True,
        structure_check: bool = True,
    ) -> None:
        """Initialize the mock icon library.

        Phase XVIII R1/R2/R9：新增 mock 语法/字号/引脚/校验参数。
        - ``pin_font_size``: C 指令引脚标签字号（32→16，用户"缩小一半"）。
        - ``pin_text_size``: ``X "PIN_TEXT"`` 可见引脚名字号。
        - ``pin_line_len``: 引脚 L 段向外长度（真实库 ch347 golden = 50）。
        - ``mock_text_cmd``: MOCK_TEXT 文本指令（默认 "P" 属性）。
          SPCOCN-1158 修复（08-13）：**禁用 "X"** —— 真实库 X 指令类型
          只有 PIN_TEXT/VHDL_PORT/HDL_PORT（grep 64980+6 条实锤），
          ``X "MOCK_TEXT"`` 是未知指令类型 → Cadence 解析 symbol.css
          报 "pin property not preceded by connection"（错误定位到
          后续第一个引脚行）。P 是属性定义指令，容忍自定义属性名。
        - ``syntax_check``: 写盘后全量 symbol.css 语法校验（R1，默认开）。
        - ``structure_check``: master.tag 分目录 / entity 四文件结构断言
          （R2，默认开）。

        Args:
            enabled: Master switch (``temp_lib.enabled``).
            lib_name: Output library directory name (default "temp_lib").
            annotate: Draw the "模拟图标" annotation inside the body.
            mock_text: Annotation text (用户 D11：中英双标).
            pin_font_size: C 指令字号。
            pin_text_size: X PIN_TEXT 字号。
            pin_line_len: 引脚 L 段向外长度。
            mock_text_cmd: MOCK_TEXT 指令（"T" 可见文本 / "P" 属性逃生）。
            syntax_check: R1 语法校验开关。
            structure_check: R2 结构断言开关。
        """
        self._enabled: bool = enabled
        self.lib_name: str = lib_name or "TG0C0D2_VB_lib"
        self._annotate: bool = annotate
        self._mock_text: str = mock_text or "MOCK/模拟图标"
        self._pin_font_size: int = int(pin_font_size or 16)
        self._pin_text_size: int = int(pin_text_size or 16)
        self._pin_line_len: int = int(pin_line_len or 50)
        self._mock_text_cmd: str = str(mock_text_cmd or "X").upper()
        self._syntax_check: bool = bool(syntax_check)
        self._structure_check: bool = bool(structure_check)
        self._symbols: dict[tuple[str, int], MockSymbol] = {}
        self._written: set[str] = set()

    @property
    def enabled(self) -> bool:
        """Whether mock icon generation is enabled."""
        return self._enabled

    @property
    def annotate(self) -> bool:
        """Whether the MOCK annotation (T text + CSA label) is enabled."""
        return self._annotate

    # ------------------------------------------------------------------
    #  Generation
    # ------------------------------------------------------------------

    def symbol_for(
        self,
        refdes: str,
        section: int,
        pins: Iterable[tuple[str, str]],
        recovered_pin_names: dict[str, str] | None = None,
    ) -> MockSymbol | None:
        """Return (or lazily build) the mock icon symbol for an instance.

        Phase XXI E（用户 P7/P8/P11/P19）：芯片宽度按
        ``_min_mock_width``（per-refdes 覆盖 + 引脚数分档）钳制目标值；
        F3（用户 P13）：生成后跑 ``mock_text_overlap_count`` 自检，若
        C 短号与 X 长名重叠则增宽 100 单位重布（≤8 轮，0 碰撞为止）。

        Phase XXI D（用户 P6）：``recovered_pin_names``（pstchip.dat
        恢复的 ``{pin_number: 功能名}``）在 ``pins`` 为空时替代默认
        8 个占位引脚 —— IC3(AMS1117) 输出 INPUT/OUTPUT/GND/TAP 真实
        引脚名而非 1-8。

        Args:
            refdes: Instance reference designator (e.g. ``"U6A"``).
            section: Symbol view number.
            pins: ``(pin_number, pin_name)`` pairs from the instance.
            recovered_pin_names: Optional pstchip-recovered pin name map
                (used only when ``pins`` is empty).

        Returns:
            A memoized ``MockSymbol``, or None when disabled.
        """
        if not self._enabled:
            return None
        entries: list[tuple[str, str]] = [
            (str(num), str(name or "")) for num, name in pins
        ]
        if not entries:
            # Phase XX 补丁（08-13）：匹配数据缺失（irec.pins 空，如
            # AMS1117→CH347、RJ45_2X2_LED）也必须输出 mock 图标——否则
            # Cadence 显示**错误匹配的真实库图标**（用户实测 IC3/J19
            # 痛点）。用默认占位 8 引脚生成几何（无电气引脚数据，仅
            # 图形占位；引脚数据修复后接线自然恢复）。
            # Phase XXI D（用户 P6）：pstchip.dat 已恢复真实引脚名时用
            # 真实名（如 AMS1117 → INPUT/OUTPUT/GND/TAP），替代 1-8。
            if recovered_pin_names:
                entries = [
                    (str(num), str(name or ""))
                    for num, name in sorted(
                        recovered_pin_names.items(),
                        key=lambda kv: (
                            int(kv[0]) if str(kv[0]).isdigit() else 0,
                        ),
                    )
                ]
            if not entries:
                entries = [(str(i), "") for i in range(1, 9)]
        key = (refdes, int(section or 1))
        if key in self._symbols:
            return self._symbols[key]

        cell_name = self._cell_name(refdes, int(section or 1))
        numbers = [num for num, _name in entries]
        names = [name for _num, name in entries]
        # 先算标签（去重后缀 GND→GND_2…）：布局尺寸必须按**实际显示文本**
        # 长度算（后缀使标签比原始功能名更长，如 VDD_PMU2_1P8_FNPLL_45
        # =19 字符 —— 用原始名 17 会低估 → 文本重叠）。
        label_by_idx = unique_functional_labels(numbers, names)
        min_width = _min_mock_width(refdes, len(entries))
        distributed = distribute_mock_pin_offsets(
            len(entries), label_by_idx.values(), min_width=min_width,
        )

        # F3（用户 P13 授权"重叠检测避让函数"）：自检 C 短号 vs X 长名
        # 文本重叠；若 >0 增宽 100 重布（Phase XXVI：≤3 轮，防极端标签
        # 把列距推到 3000）。增宽只加**列距**（``min_width`` → 引脚列
        # 位），不改变 outline（本体优先）。
        _ov_round = 0
        while _ov_round < 3:
            _ov = mock_text_overlap_count(distributed, label_by_idx)
            if _ov == 0:
                break
            _ov_round += 1
            min_width += 100
            distributed = distribute_mock_pin_offsets(
                len(entries), label_by_idx.values(), min_width=min_width,
            )
        if _ov_round:
            logger.info(
                "MockIcon %s: F3 text overlap → widened %d×100 to %d",
                refdes, _ov_round, min_width,
            )

        offsets: dict[str, tuple[int, int]] = {}
        sides: dict[str, str] = {}
        labels: dict[str, str] = {}
        used_names: set[str] = set()
        for idx, (num, name) in enumerate(entries):
            off = distributed[idx + 1]
            offsets[num] = (off[0], off[1])
            sides[num] = off[2]
            labels[num] = label_by_idx[idx + 1]
            # 名称键（首次出现的功能名；重复名不覆盖 —— 位置键才是权威）。
            # Phase XXI F3（U5_PH 310 实测）：名称键加 ``"name:"`` 前缀隔离
            # —— BGA 引脚号（U5 的 A7）可能等于另一引脚功能名（DDR 地址线
            # A7），裸键覆盖 → 两引脚同坐标（SPCOCN-310）。``offset_for``
            # 查询名称键时加同前缀。
            if name and name not in used_names:
                offsets[f"name:{name}"] = (off[0], off[1])
                sides[f"name:{name}"] = off[2]
                used_names.add(name)

        symbol = MockSymbol(
            cell_name=cell_name,
            pin_numbers=numbers,
            pin_names=names,
            offsets=offsets,
            sides=sides,
            labels=labels,
            outline=mock_outline(len(entries), distributed),
        )
        self._symbols[key] = symbol
        logger.info(
            "MockIcon: %s → %s (%d pins, temp_lib)",
            refdes, cell_name, len(entries),
        )
        return symbol

    @staticmethod
    def _cell_name(refdes: str, section: int) -> str:
        """Build a DEHDL-safe mock cell name (uppercase directory).

        ``U6`` → ``U6_PH``; multi-section ``U6A`` → ``U6A_PH_S2``.

        Args:
            refdes: Instance reference designator.
            section: Symbol view number.

        Returns:
            Cell directory name (uppercased, non-alnum stripped).
        """
        base = "".join(ch for ch in str(refdes).upper() if ch.isalnum()) or "PH"
        return f"{base}_PH" if section <= 1 else f"{base}_PH_S{section}"

    # ------------------------------------------------------------------
    #  File generation (symbol.css + chips.prt + entity)
    # ------------------------------------------------------------------

    def write_to_temp_lib(self, temp_lib_root: Path) -> list[Path]:
        """Write every generated mock symbol into a temp_lib tree.

        Layout (aligned to golden hdl_lib cell format, Phase XVIII R2)::

            <temp_lib_root>/<CELL>/sym_1/symbol.css
            <temp_lib_root>/<CELL>/sym_1/master.tag      → "symbol.css"
            <temp_lib_root>/<CELL>/chips/chips.prt
            <temp_lib_root>/<CELL>/chips/master.tag      → "chips.prt"
            <temp_lib_root>/<CELL>/entity/master.tag     → "verilog.v"
            <temp_lib_root>/<CELL>/entity/pc.db
            <temp_lib_root>/<CELL>/entity/verilog.v
            <temp_lib_root>/<CELL>/entity/vhdl.vhd
            <temp_lib_root>/<CELL>/entity/vlog004u.sir

        cell 根目录**不**写 master.tag（与真实库一致）。写盘完成后按
        ``temp_lib.syntax_check`` / ``structure_check`` 全量校验
        （``validate_symbol_css`` / ``validate_temp_lib_structure``），
        0 错才正常返回；有错 logger.error + 报告清单（不阻断写盘）。

        Args:
            temp_lib_root: Destination temp library root (e.g.
                ``output/temp_lib``).

        Returns:
            List of written file paths.
        """
        if temp_lib_root is None:
            return []
        written: list[Path] = []
        for symbol in self._symbols.values():
            if symbol.cell_name in self._written:
                continue
            cell_dir = temp_lib_root / symbol.cell_name
            sym_dir = cell_dir / "sym_1"
            chips_dir = cell_dir / "chips"
            entity_dir = cell_dir / "entity"
            sym_dir.mkdir(parents=True, exist_ok=True)
            chips_dir.mkdir(parents=True, exist_ok=True)
            entity_dir.mkdir(parents=True, exist_ok=True)

            css_path = sym_dir / "symbol.css"
            css_path.write_text(
                self._symbol_css(symbol), encoding="utf-8",
            )
            written.append(css_path)
            written.append(self._write_tag(sym_dir, self._master_tag("sym")))

            prt_path = chips_dir / "chips.prt"
            prt_path.write_text(
                self._chips_prt(symbol), encoding="utf-8",
            )
            written.append(prt_path)
            written.append(self._write_tag(chips_dir, self._master_tag("chips")))

            written.append(self._write_tag(entity_dir, self._master_tag("entity")))
            pc_db = entity_dir / "pc.db"
            pc_db.write_text(
                self._entity_pc_db(symbol), encoding="utf-8",
            )
            written.append(pc_db)
            for fname, content in self._entity_files(symbol).items():
                fp = entity_dir / fname
                fp.write_text(content, encoding="utf-8")
                written.append(fp)

            self._written.add(symbol.cell_name)
            logger.debug("MockIcon written: %s (%s)", symbol.cell_name, css_path)

        # ── R1/R2: 生成后语法/结构校验（0 错才通过）──────────────
        self._validate_written(temp_lib_root, written)
        return written

    @staticmethod
    def _write_tag(tag_dir: Path, content: str) -> Path:
        """Write a ``master.tag`` file with the given content."""
        tag = tag_dir / "master.tag"
        tag.write_text(content, encoding="ascii")
        return tag

    @staticmethod
    def _master_tag(role: str) -> str:
        """按目录角色返回 master.tag 内容（R2，对齐真实库 golden）。

        - ``sym``    → ``"symbol.css\\n"``（sym_1..N 目录）
        - ``chips``  → ``"chips.prt\\n"``
        - ``entity`` → ``"verilog.v\\n"``

        Args:
            role: 目录角色（"sym" / "chips" / "entity"）。

        Returns:
            master.tag 内容（含换行）。
        """
        if role == "chips":
            return "chips.prt\n"
        if role == "entity":
            return "verilog.v\n"
        return "symbol.css\n"

    @staticmethod
    def _entity_files(symbol: "MockSymbol") -> dict[str, str]:
        """entity 目录最小 ASCII 声明（R2，真实库 entity 含四文件）。

        真实库 capacitor/entity 含 ``pc.db`` + ``verilog.v`` +
        ``vhdl.vhd`` + ``vlog004u.sir``；mock 补齐 verilog.v / vhdl.vhd /
        vlog004u.sir（最小 ASCII 声明，pc.db 由 ``_entity_pc_db`` 单独写）。

        Args:
            symbol: The mock symbol.

        Returns:
            ``{文件名: 内容}``（不含 pc.db —— 已由写盘主流程单独处理）。
        """
        cell = symbol.cell_name
        return {
            "verilog.v": (
                f"// mock entity verilog.v — {cell}\n"
                f"module {cell} ();\n"
                f"endmodule\n"
            ),
            "vhdl.vhd": (
                f"-- mock entity vhdl.vhd — {cell}\n"
                f"entity {cell} is\n"
                f"end {cell};\n"
            ),
            "vlog004u.sir": (
                f"// mock entity vlog004u.sir — {cell}\n"
                f"// minimal ASCII declaration (real file is binary)\n"
            ),
        }

    def _validate_written(
        self, temp_lib_root: Path, written: list[Path],
    ) -> list[str]:
        """Run R1/R2 validators over the written temp_lib (0 错才通过).

        ``syntax_check=true`` 时对每个写入的 symbol.css 跑
        ``validate_symbol_css``；``structure_check=true`` 时跑
        ``validate_temp_lib_structure``。错误只记日志/报告，不阻断写盘。

        Args:
            temp_lib_root: temp_lib 根目录。
            written: 已写文件清单。

        Returns:
            校验错误清单（空 = 通过）。
        """
        errors: list[str] = []
        try:
            from .validate_symbol_css import (
                validate_symbol_css, validate_temp_lib_structure,
            )
        except Exception as exc:  # pragma: no cover - import guard
            logger.warning("validate_symbol_css import failed: %s", exc)
            return errors

        if self._syntax_check:
            for fp in written:
                if fp.name != "symbol.css":
                    continue
                try:
                    content = fp.read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    errors.append(f"{fp}: read failed: {exc}")
                    continue
                line_errors = validate_symbol_css(content, str(fp))
                if line_errors:
                    errors.extend(line_errors)
        if self._structure_check:
            structure_errors = validate_temp_lib_structure(
                Path(temp_lib_root),
            )
            if structure_errors:
                errors.extend(structure_errors)

        if errors:
            logger.error(
                "temp_lib validation failed (%d errors):\n%s",
                len(errors), "\n".join(errors[:50]),
            )
        else:
            logger.info("temp_lib validation OK: %d symbol.css, structure OK",
                        sum(1 for f in written if f.name == "symbol.css"))
        return errors

    # ------------------------------------------------------------------
    #  Format builders
    # ------------------------------------------------------------------

    def _symbol_css(self, symbol: MockSymbol) -> str:
        """Build symbol.css content for a mock symbol.

        C 指令文本 = 功能名标签（重复加序号后缀，空回退引脚号）；偏移与
        csa_writer pin_coords 同源（LASTPIN/WIRE 精确重合）。BGA 四边
        标签旋转对齐（顶 0°/右 90°/底 180°/左 270°）。

        Args:
            symbol: The mock symbol.

        Returns:
            symbol.css content string.
        """
        lines: list[str] = []
        a = lines.append
        outline = symbol.outline
        a(f'P "CDS_LMAN_SYM_OUTLINE" "{outline}" 0 0 0.00 0.00 22 0 0 0 0 0 0 0 0')
        # Body rectangle (4 edge lines) sized to the outline.
        o = [float(v) for v in outline.split(",")]
        x0, y0, x1, y1 = o[0], o[1], o[2], o[3]
        if x0 > x1:
            x0, x1 = x1, x0
        if y0 > y1:
            y0, y1 = y1, y0
        a(f"M {int(x0)} {int(y1)} {int(x1)} {int(y1)} -1 0")
        a(f"M {int(x1)} {int(y1)} {int(x1)} {int(y0)} -1 0")
        a(f"M {int(x1)} {int(y0)} {int(x0)} {int(y0)} -1 0")
        a(f"M {int(x0)} {int(y0)} {int(x0)} {int(y1)} -1 0")
        # Property labels (STANDARDS Part III §2.5 defaults).
        a('P "$LOCATION" "?" -5 -100 90 0 40 0 0 1 0 0 1 0 0')
        a('P "VALUE" "?" -5 100 90 0 40 0 0 1 0 0 1 0 32')
        # Phase XXI A（用户 Cadence 16.6 实测 SPCOCN-542/545 报错刷屏）：
        # 真实库 capacitor/sym_1/symbol.css 声明 9 个默认 P 属性（含
        # JEDEC_TYPE/PACKAGE_TYPE/DESCRIPTION/SN_NUM）；mock 旧实现只声明
        # 5 个 → FORCEPROP 1 LAST 注入的默认属性若 symbol 未声明，Cadence
        # 视为"默认属性被删" → SPCOCN-542 + 545 STICKY 提示（实测报错元件
        # 全部是 _PH mock cell）。顺序对齐真实库：
        # PART_NAME/JEDEC_TYPE/PATH/PACKAGE_TYPE/DESCRIPTION/SN_NUM。
        # MOCK_TEXT 是 csa_writer 注入的实例属性（Phase XXI B 标签方式），
        # 一并 P 声明防止 542 复发。
        a('P "PART_NAME" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        a('P "JEDEC_TYPE" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        a('P "PATH" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        a('P "PACKAGE_TYPE" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        a('P "DESCRIPTION" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        a('P "SN_NUM" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        # MOCK_TEXT 是 csa_writer 注入的实例属性（Phase XXI B 标签方式），
        # 一并 P 声明防止 542 复发；``annotate=false`` 时无标注也不注入。
        if self._annotate:
            a('P "MOCK_TEXT" "?" 0 0 0 0 48 0 0 0 0 0 0 0 32')
        # 模拟图标标注（用户要求醒目可见 + 红色，08-13 Phase XIX/XX；
        # 08-14 Phase XXI B 字号 59→**89** = 1.5×，用户 P5"放大 1.5x"）。
        # 实现：**T 指令**（真实库 INPORT/IOPORT 先例，两行结构：指令行 +
        # 文本行）——P 属性不渲染可见文本、X "MOCK_TEXT" 是未知 X 指令类型
        # （1158 根因）。颜色字段 c11=4（标题栏醒目色先例）+ **CSA 实例
        # 属性标签**（csa_writer FORCEPROP MOCK_TEXT + PAINT PINK，双保险：
        # 用户 P5"标签方式比较大个显示、可改颜色"）。``mock_text_cmd``
        # 保留 "P" 逃生。
        if self._annotate:
            if self._mock_text_cmd == "T":
                # Phase XX 补丁 2：字号 41→59（用户"放大 1.5x"）；真实库
                # T 字号合法域含 41/64，89 合法。Phase XXI：59→**89**。
                a(f"T 0 {int(y1) - 90} 0 0 89 0 0 0 0 4 0")
                a("MOCK")
            else:
                a(f'P "MOCK_TEXT" "{self._mock_text}" 0 0 0 0 24 0 0 0 0 0 0 0 0')
        # Pin lines + C labels (side-aware orientation/justify R1/R9)。
        for _idx, num in enumerate(symbol.pin_numbers, 1):
            off = symbol.offsets.get(num)
            if off is None:
                continue
            px, py = off
            label = symbol.labels.get(num, num)
            side = symbol.sides.get(num, "left" if px < 0 else "right")
            self._append_pin_line(
                a, px, py, side, x0, x1, y0, y1, label, str(_idx))
        a("")
        return "\n".join(lines)

    def _append_pin_line(
        self, a, px: int, py: int, side: str,
        x0: float, x1: float, y0: float, y1: float, label: str,
        pin_number: str = "",
    ) -> None:
        """Append the L + C + X PIN_TEXT lines for one mock pin.

        Phase XVIII R1（SPCOCN-1158 修复）：C 指令 justify 仅允许 R/L ——
        全库 65689 条真实 C 指令只有 R/L（grep 实锤），U/D 导致 parse
        error。四边参数表（设计 §R1）：

        | side   | L 线（tip→body edge） | orient | justify | X PIN_TEXT |
        |--------|------------------------|:------:|:-------:|-----------|
        | left   | tip→edge(px→x0)       | 0      | R       | px-50 外   |
        | right  | tip→edge(px→x1)       | 0      | L       | px+50 外   |
        | top    | tip→edge(py→y1)       | 90     | R       | px, py-50  |
        | bottom | tip→edge(py→y0)       | 270    | R       | px, py+50  |

        Phase XX 补丁 3（08-13 用户复测"引脚名仍重叠"）：**C 指令文本
        改用短引脚号**（1/2/3…），X PIN_TEXT 保留功能名（长名）——
        此前 C 与 X 都显示同一长名且在引脚同侧 → 156 组文本碰撞（U6B
        实测）。真实库单列无此问题（C 根部外、X tip 内反方向）；mock
        多列布局框外空间不足，按用户 p13 建议"只留一个"：C=短号
        （引脚根部）、X=长功能名（框外延伸），互不重叠。

        Args:
            a: list.append bound method.
            px/py: Pin tip coordinate (relative to body origin).
            side: "left"/"right"/"top"/"bottom".
            x0/x1/y0/y1: Body outline edges (left/right/bottom/top).
            label: C-command display text (functional name).
            pin_number: Short pin number for the C text (1-based).
        """
        font = self._pin_font_size
        text_size = self._pin_text_size
        # Phase XXVI（S11 视觉审查）：C/X 字号 29→**22**（合法域
        # {0,22,23,24,29,...} 内）—— 29 号字渲染字符宽 ~24-28 迫使
        # outline 拉宽到 3000；字号降档后字符宽 ~16-18（``_CHAR_W=18``
        # 同口径），配合 1/3 缩窄。
        font = max(int(font), 22)
        text_size = max(int(text_size), 22)
        # Phase XX 补丁 2（08-13 用户复测"U6B/U6 引脚名重叠"）：
        # X PIN_TEXT 移到**引脚 tip 外侧**（真实库 prx126a1bi 布局：left
        # 引脚 X 在 tip 外、文本朝框外延伸）——旧实现 left 列 X 在
        # ``px + 60``（框内）向右延伸，长名（DDR_ADDR14 等 14 字符）
        # 文本侵入内列引脚区 → 视觉重叠。新布局：
        #   left 列：X 右对齐（justify=1）锚定 ``px - 80``（tip 外侧，
        #             Phase XXI C：旧 px-50 与 C 号视觉靠近，→ px-80），
        #            文本向左延伸 → 全在框外，不碰内列；
        #   right 列：X 左对齐（justify=0）锚定 ``px + 80``（tip 外侧），
        #            文本向右延伸 → 全在框外。
        # C 指令（短名）保持在引脚根部框内边缘（left justify R / right L）。
        # C 指令文本 = 短引脚号（补丁3：与 X 长名分居，消除 156 组碰撞）。
        # C 号放**框内**（left 向右 / right 向左），X 长名放**框外**朝
        # 侧边延伸——两者方向相反不重叠。
        # Phase XXI E 几何铁律（用户 P7/P8/P11/P19）：外列 px 拉远后
        # （U6H ±1600 等），C 号若仍在 ``px±25`` 会落到 outline 外 ——
        # 改为**贴 outline 边**（left ``x0+25`` / right ``x1-25``），内列
        # 引脚（px 在框内）仍 ``px±25``（取 max/min 统一钳制）。
        # X 指令字段：``X "PIN_TEXT" "label" x y rot justify font ...``
        # （token[6]=justify：0 左对齐向右 / 1 右对齐向左）。
        if side == "left":
            # L 起点固定在 outline 左边界 x0（引脚 tip 伸出到 px）。
            a(f"L {int(x0)} {py} {px} {py} -1 0")
            _cx = max(px + 25, int(x0) + 25)
            a(f'C {px} {py} "{pin_number}" {_cx} {py} 0 0 {font} 1 L')
            a(f'X "PIN_TEXT" "{label}" {px - 80} {py} 0 1 {text_size} 0 0 0 0 0 1 0 0')
        elif side == "right":
            a(f"L {int(x1)} {py} {px} {py} -1 0")
            _cx = min(px - 25, int(x1) - 25)
            a(f'C {px} {py} "{pin_number}" {_cx} {py} 0 0 {font} 1 R')
            a(f'X "PIN_TEXT" "{label}" {px + 80} {py} 0 0 {text_size} 0 0 0 0 0 1 0 0')
        elif side == "top":
            a(f"L {px} {int(y1)} {px} {py} -1 0")
            a(f'C {px} {py} "{pin_number}" {px} {py - 15} 90 0 {font} 1 L')
            a(f'X "PIN_TEXT" "{label}" {px} {py - 80} 0 1 {text_size} 0 0 0 0 0 1 0 0')
        elif side == "bottom":
            a(f"L {px} {int(y0)} {px} {py} -1 0")
            a(f'C {px} {py} "{pin_number}" {px} {py + 15} 270 0 {font} 1 L')
            a(f'X "PIN_TEXT" "{label}" {px} {py + 80} 0 0 {text_size} 0 0 0 0 0 1 0 0')

    @staticmethod
    def _chips_prt(symbol: MockSymbol) -> str:
        """Build chips.prt content declaring the mock's pins.

        ``PIN_NUMBER`` stores the EDIF pin number; the ``'功能名':`` key
        stores the pin function name when available（STANDARDS 收尾 T03）。

        2026-08-21 修复：pin_names 可能为空（当前 mock 的
        pin_connections 键为功能名 GND/EN/FB 且无独立 pin_names）——
        ``zip(pin_numbers, pin_names)`` 会在 pin_names 为空时**零次**
        迭代 → chips.prt pin 区空 → DEHDL "fail to load component cell
        in temp_lib / connectivity error"。改为独立遍历 pin_numbers，
        引脚号缺省时按位置索引补号（对齐 s16 数字引脚号 + 功能名
        成对格式）。

        Args:
            symbol: The mock symbol.

        Returns:
            chips.prt content string.
        """
        lines: list[str] = []
        a = lines.append
        a("FILE_TYPE=LIBRARY_PARTS;")
        a(f"primitive '{symbol.cell_name}';")
        a("  pin")
        numbers = list(symbol.pin_numbers)
        names = list(symbol.pin_names)
        for i, num in enumerate(numbers):
            pin_no = str(num).strip()
            name = names[i] if i < len(names) else ""
            # 2026-08-21：PIN_NUMBER 只接受数字/序号——功能名（GND/EN/FB
            # 等）一律按位置补号（1-based），功能名作为独立 'name': 键。
            # 对齐 s16 数字引脚号 + 功能名成对格式，避免 Cadence 拒绝
            # 'PIN_NUMBER=(GND)' 类非数字引脚号。
            if pin_no and pin_no.isdigit():
                a(f"    '{pin_no}':")
                a(f"      PIN_NUMBER='({pin_no})';")
                a("      PINUSE='UNSPEC';")
            else:
                _idx = str(i + 1)
                a(f"    '{_idx}':")
                a(f"      PIN_NUMBER='({_idx})';")
                a("      PINUSE='UNSPEC';")
                if pin_no and pin_no != _idx:
                    a(f"    '{pin_no}':")
                    a("      PINUSE='UNSPEC';")
            if name and name.strip() and name != pin_no:
                a(f"    '{name}':")
                a("      PINUSE='UNSPEC';")
        a("  end_pin;")
        a("  body")
        a(f"    PART_NAME='{symbol.cell_name}';")
        a(f"    BODY_NAME='{symbol.cell_name}';")
        a("    PHYS_DES_PREFIX='U';")
        a("    CLASS='IC';")
        a("  end_body;")
        a("end_primitive;")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _entity_pc_db(symbol: MockSymbol) -> str:
        """Build a minimal ``entity/pc.db`` for a mock cell.

        Args:
            symbol: The mock symbol.

        Returns:
            pc.db content string.
        """
        return (
            f"# mock entity pc.db — {symbol.cell_name}\n"
            f"primitive {symbol.cell_name}\n"
            f"view symbol\n"
            f"end_primitive\n"
        )
