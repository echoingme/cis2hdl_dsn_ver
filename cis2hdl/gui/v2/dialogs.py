"""S11 GUI v2 弹窗模块 — 5 个功能弹窗（QDialog）。

设计依据：``docs/gui-design.md`` + S11 用户要求：
- 主面板只留配置编辑工作台，低频操作全部弹窗化
- 输出设置 + 报告设置 **合并为一个弹窗**（OutputSettingsDialog）
- 文件导入 / 报告查看 / 手动匹配 / 原理图预览 各为独立弹窗

所有弹窗为轻量 QDialog（模态，用完即关），不常驻内存。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..colors import Colors, FontSize, Spacing
from .qss import STYLE_REPORT

logger = logging.getLogger(__name__)

__all__ = [
    "ImportDialog",
    "OutputSettingsDialog",
    "ReportDialog",
    "ManualMatchDialog",
    "PreviewDialog",
]


def _section(title: str) -> QLabel:
    lbl = QLabel(title)
    lbl.setObjectName("section_title")
    return lbl


# ── ① 文件导入弹窗 ─────────────────────────────────────────────────────────


class ImportDialog(QDialog):
    """文件导入：输入文件 / hdl_lib / extra 库列表。"""

    def __init__(
        self,
        controller: Any,
        input_path: str = "",
        hdl_lib: str = "",
        extra: list[str] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("导入文件")
        self.setMinimumWidth(560)
        self._result: tuple[str, str, list[str]] | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(Spacing.SM)

        form = QFormLayout()
        form.setSpacing(Spacing.SM)

        # 输入文件（EDIF/DSN）
        self._input_edit = QLineEdit(input_path)
        input_row = QHBoxLayout()
        input_row.addWidget(self._input_edit, 1)
        btn = QPushButton("浏览…")
        btn.clicked.connect(self._pick_input)
        input_row.addWidget(btn)
        form.addRow("输入文件:", input_row)

        # 主 HDL 库
        self._hdl_edit = QLineEdit(hdl_lib)
        hdl_row = QHBoxLayout()
        hdl_row.addWidget(self._hdl_edit, 1)
        btn2 = QPushButton("浏览…")
        btn2.clicked.connect(self._pick_hdl)
        hdl_row.addWidget(btn2)
        form.addRow("HDL 库:", hdl_row)

        # 附加库（逗号分隔）
        self._extra_edit = QLineEdit(",".join(extra or []))
        form.addRow("附加库(逗号分隔):", self._extra_edit)

        layout.addLayout(form)

        hint = QLabel(
            "输入文件：EDIF(.edf/.edif) 或 DSN(.dsn) 原理图；"
            "HDL 库：元件符号库目录（--hdl-lib）。"
        )
        hint.setStyleSheet(
            "color: %s; font-size: %dpx;" % (Colors.TEXT_MUTED, FontSize.XS)
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_input(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择原理图", "", "原理图 (*.edf *.edif *.dsn);;所有文件 (*)"
        )
        if path:
            self._input_edit.setText(path)

    def _pick_hdl(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 HDL 元件库")
        if path:
            self._hdl_edit.setText(path)

    def _accept(self) -> None:
        self._result = (
            self._input_edit.text().strip(),
            self._hdl_edit.text().strip(),
            [
                s.strip()
                for s in self._extra_edit.text().split(",")
                if s.strip()
            ],
        )
        self.accept()

    @property
    def result_values(self) -> tuple[str, str, list[str]]:
        return self._result or ("", "", [])


# ── ② 输出设置 + 报告设置（合并弹窗） ─────────────────────────────────────


class OutputSettingsDialog(QDialog):
    """输出文件 + 报告设置（用户 S11 要求：合并为同一弹窗）。"""

    FILE_OPTIONS = [
        ("csa", "CSA 原理图(.csa)"),
        ("con", "Netlist(.con)"),
        ("xcon", "XML 网表(.xcon)"),
        ("csv", "CSV 表格(.csv)"),
        ("cpc", "CPC 文件(.cpc)"),
        ("cpm", "CPM 文件(.cpm)"),
        ("cds_lib", "cds.lib 库"),
    ]
    REPORT_OPTIONS = [
        ("aesthetic", "美观报告"),
        ("ioport", "IOPORT 审计"),
        ("mapping", "映射表(mapping.csv)"),
        ("error", "错误日志(errors.log)"),
    ]

    def __init__(
        self,
        controller: Any,
        files: list[str] | None = None,
        reports: list[str] | None = None,
        output_dir: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("输出与报告设置")
        self.setMinimumWidth(520)
        self._result: tuple[list[str], list[str], str] | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(Spacing.SM)

        # 输出目录
        dir_row = QHBoxLayout()
        dir_lbl = QLabel("输出目录:")
        self._dir_edit = QLineEdit(output_dir)
        dir_btn = QPushButton("浏览…")
        dir_btn.clicked.connect(self._pick_dir)
        dir_row.addWidget(dir_lbl)
        dir_row.addWidget(self._dir_edit, 1)
        dir_row.addWidget(dir_btn)
        layout.addLayout(dir_row)

        # 输出文件（files）
        layout.addWidget(_section("输出文件"))
        self._file_checks: dict[str, QCheckBox] = {}
        file_grid = QHBoxLayout()
        for key, label in self.FILE_OPTIONS:
            cb = QCheckBox(label)
            cb.setChecked(key in (files or []))
            self._file_checks[key] = cb
            file_grid.addWidget(cb)
        layout.addLayout(file_grid)

        # 报告（reports）
        layout.addWidget(_section("报告"))
        self._report_checks: dict[str, QCheckBox] = {}
        report_grid = QHBoxLayout()
        for key, label in self.REPORT_OPTIONS:
            cb = QCheckBox(label)
            cb.setChecked(key in (reports or []))
            self._report_checks[key] = cb
            report_grid.addWidget(cb)
        layout.addLayout(report_grid)

        hint = QLabel("勾选决定转换后写出的文件与报告；对应 pipeline.yaml 的 output.files / output.reports。")
        hint.setStyleSheet(
            "color: %s; font-size: %dpx;" % (Colors.TEXT_MUTED, FontSize.XS)
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _pick_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择输出目录")
        if path:
            self._dir_edit.setText(path)

    def _accept(self) -> None:
        self._result = (
            [k for k, cb in self._file_checks.items() if cb.isChecked()],
            [k for k, cb in self._report_checks.items() if cb.isChecked()],
            self._dir_edit.text().strip(),
        )
        self.accept()

    @property
    def result_values(self) -> tuple[list[str], list[str], str]:
        return self._result or ([], [], "")


# ── ③ 报告查看弹窗 ─────────────────────────────────────────────────────────


class ReportDialog(QDialog):
    """报告查看（aesthetic/ioport/mapping/error 四标签页，可放大滚动）。"""

    def __init__(
        self,
        controller: Any,
        kind: str = "aesthetic",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("转换报告")
        self.resize(820, 600)
        self.setStyleSheet(STYLE_REPORT)

        layout = QVBoxLayout(self)
        layout.setSpacing(Spacing.SM)

        self._tabs = QPlainTextEdit()
        self._tabs.setObjectName("report_content")
        self._tabs.setReadOnly(True)
        self._tabs.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self._tabs, 1)

        open_btn = QPushButton("在外部打开报告文件")
        open_btn.clicked.connect(self._open_external)
        layout.addWidget(open_btn, alignment=Qt.AlignRight)

        self._tabs.setPlainText(controller.get_report(kind))

    def _open_external(self) -> None:
        out = getattr(self._controller, "_last_output_dir", None)
        if out:
            import subprocess

            try:
                subprocess.Popen(["open", str(out)])
            except OSError:
                logger.warning("无法用系统打开 %s", out)


# ── ④ 手动匹配弹窗（FR3） ──────────────────────────────────────────────────


class ManualMatchDialog(QDialog):
    """手动匹配干预：未匹配列表 → 手动指定 HDL / 强制 mock（FR3）。"""

    MOCK_PREFIXES = ("J", "T", "U", "IC")

    def __init__(
        self,
        controller: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("手动匹配干预（FR3）")
        self.resize(860, 480)
        self.setStyleSheet(STYLE_REPORT)

        layout = QVBoxLayout(self)
        layout.setSpacing(Spacing.SM)

        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(
            ["refdes", "置信度", "策略", "推荐 HDL"]
        )
        self._table.setColumnWidth(0, 110)
        self._table.setColumnWidth(1, 70)
        self._table.setColumnWidth(2, 110)
        self._table.setColumnWidth(3, 260)
        self._table.verticalHeader().setVisible(False)
        layout.addWidget(self._table, 1)

        row = QHBoxLayout()
        row.addWidget(QLabel("手动指定 HDL:"))
        self._hdl_edit = QLineEdit()
        self._hdl_edit.setPlaceholderText("如 RES_0603；清空并应用=撤销")
        row.addWidget(self._hdl_edit, 1)
        apply_btn = QPushButton("应用")
        apply_btn.clicked.connect(self._apply)
        row.addWidget(apply_btn)
        layout.addLayout(row)

        mock_row = QHBoxLayout()
        mock_row.addWidget(QLabel("强制 mock 前缀:"))
        self._mock_checks: dict[str, QCheckBox] = {}
        for prefix in self.MOCK_PREFIXES:
            cb = QCheckBox(prefix)
            self._mock_checks[prefix] = cb
            mock_row.addWidget(cb)
        mock_row.addStretch(1)
        layout.addLayout(mock_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

        self._load()

    def _load(self) -> None:
        self._table.setRowCount(0)
        for entry in self._controller.get_unmatched():
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(entry.refdes))
            self._table.setItem(
                row, 1,
                QTableWidgetItem(f"{entry.confidence:.2f}"),
            )
            self._table.setItem(row, 2, QTableWidgetItem(entry.strategy))
            self._table.setItem(
                row, 3, QTableWidgetItem(entry.recommended_hdl)
            )

    def _apply(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        refdes = self._table.item(row, 0).text()
        hdl = self._hdl_edit.text().strip()
        try:
            self._controller.set_manual_match(refdes, hdl or None, False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("manual match apply failed: %s", exc)
            return
        self._load()
        self._hdl_edit.clear()


# ── ⑤ 原理图预览弹窗 ───────────────────────────────────────────────────────


class PreviewDialog(QDialog):
    """原理图预览（占位实现；后续可接 schematic_view 增强）。"""

    def __init__(
        self,
        controller: Any,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._controller = controller
        self.setWindowTitle("原理图预览")
        self.resize(900, 640)
        self.setStyleSheet(STYLE_REPORT)

        layout = QVBoxLayout(self)
        layout.setSpacing(Spacing.SM)

        hint = QLabel(
            "原理图预览（S11 占位）：转换完成后可在此查看 CSA 文本。"
            "后续版本接入图形化 schematic_view。"
        )
        hint.setStyleSheet(
            "color: %s; font-size: %dpx;" % (Colors.TEXT_MUTED, FontSize.XS)
        )
        layout.addWidget(hint)

        self._text = QPlainTextEdit()
        self._text.setObjectName("report_content")
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(self._text, 1)

        # 显示最近一个 .csa 文件内容（若有）
        out = getattr(self._controller, "_last_output_dir", None)
        if out is not None:
            csa_files = sorted(Path(out).rglob("*.csa"))
            if csa_files:
                try:
                    self._text.setPlainText(
                        csa_files[0].read_text(encoding="utf-8", errors="replace")
                    )
                except OSError:
                    pass
