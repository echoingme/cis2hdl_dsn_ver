"""S9/S11 GUI v2 MainWindow — 工程工作台（S11：面板窗口化 + 弹窗化 + 主面板摘要）。

设计依据：``docs/gui-design.md`` §2 组件树 + S11 用户要求：
- 主面板只留"配置编辑工作台"（ProfileBar + StageTabs + YamlEditor + 运行区）
- 低频操作全部弹窗化（导入/输出+报告设置/报告查看/手动匹配/预览）
- 全部面板 QDockWidget 化：可关闭（红绿灯叉号）+ 可调大小 + 菜单栏重开
- 主面板顶部：弹窗入口 + 简要信息卡（导入/导出/报告/匹配摘要）
"""

from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cis2hdl import __version__

from ...gui.controller import PipelineController
from ...gui.history import ConversionHistory, HistoryEntry
from ...gui.yaml_bridge import (
    cfg_to_yaml_text,
    is_text_in_sync,
    set_stage_plugins,
    yaml_text_to_cfg,
)
from ..colors import Colors, Fonts, FontSize, Layout, Spacing
from .dialogs import (
    ImportDialog,
    ManualMatchDialog,
    OutputSettingsDialog,
    PreviewDialog,
    ReportDialog,
)
from .qss import STYLE_DOCKS, STYLE_SIDEBAR, STYLE_SUMMARY, STYLE_V2, STYLE_VISUAL
from .report_view import ManualMatchPanel, ReportView, SchematicPreview
from .runner import ConversionRunner
from .widgets import ProfileBar, ProfileList, StageTabs
from .yaml_editor import YamlEditor

logger = logging.getLogger(__name__)

__all__ = ["MainWindow"]


class MainWindow(QMainWindow):
    """CIS2HDL 工程工作台主窗口（S9 + S11）。"""

    def __init__(self, controller: PipelineController | None = None) -> None:
        super().__init__()
        self._controller = controller or PipelineController()
        self.setWindowTitle(f"CIS2HDL v{__version__} — 工程工作台")
        self.setMinimumSize(Layout.WINDOW_MIN_W, Layout.WINDOW_MIN_H)
        self.resize(1500, 920)

        self._history: list[str] = []
        self._history_store = ConversionHistory()
        self._build_menubar()
        self._build_central()
        self._build_docks()
        self._build_statusbar()
        self.setStyleSheet(
            STYLE_V2 + STYLE_SIDEBAR + STYLE_DOCKS + STYLE_SUMMARY + STYLE_VISUAL
        )

        self._wire_signals()
        self._refresh_all()

    # ── 布局构建 ──────────────────────────────────────────────────────────

    def _build_central(self) -> None:
        central = QWidget()
        central.setObjectName("v2_root")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── S11: 主面板顶部摘要信息区（弹窗入口 + 简要信息） ──────────
        root.addWidget(self._build_summary_bar())

        splitter = QSplitter(Qt.Horizontal)
        root.addWidget(splitter, 1)

        # ── ② 配置编辑器（中央核心，恒显示） ─────────────────────────
        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(
            Spacing.BASE, Spacing.BASE, Spacing.BASE, Spacing.SM
        )
        editor_layout.setSpacing(Spacing.SM)

        self.profile_bar = ProfileBar(self._controller)
        editor_layout.addWidget(self.profile_bar)

        self.manual_panel = ManualMatchPanel(self._controller)
        self.stage_tabs = StageTabs(
            self._controller, manual_widget=self.manual_panel
        )
        editor_layout.addWidget(self.stage_tabs, 3)

        self.yaml_editor = YamlEditor(self._controller)
        self.yaml_editor.setMaximumHeight(300)
        editor_layout.addWidget(self.yaml_editor, 2)

        splitter.addWidget(editor)

        # ── ③ 转换执行区（底部，dock 化） ────────────────────────────
        self.runner = ConversionRunner(self._controller)

        splitter.setSizes([Layout.SIDEBAR_WIDTH, 1100])

    def _build_summary_bar(self) -> QWidget:
        """S11 主面板摘要：4 张信息卡（导入/导出/报告/匹配）+ 弹窗入口。"""
        bar = QWidget()
        bar.setObjectName("v2_root")
        grid = QGridLayout(bar)
        grid.setContentsMargins(Spacing.BASE, Spacing.SM, Spacing.BASE, 0)
        grid.setSpacing(Spacing.MD)

        # ① 导入卡
        self._sum_input_value = QLabel("未导入")
        grid.addWidget(
            self._summary_card("导入文件", self._sum_input_value, "导入…", self._on_open_import),
            0, 0,
        )
        # ② 导出设置卡（输出+报告合并弹窗入口）
        self._sum_output_value = QLabel("未设置")
        grid.addWidget(
            self._summary_card("输出/报告", self._sum_output_value, "设置…", self._on_open_output),
            0, 1,
        )
        # ③ 报告摘要卡
        self._sum_report_value = QLabel("未转换")
        grid.addWidget(
            self._summary_card("报告", self._sum_report_value, "查看…", self._on_open_report),
            0, 2,
        )
        # ④ 手动匹配卡
        self._sum_match_value = QLabel("0 未匹配")
        grid.addWidget(
            self._summary_card("手动匹配", self._sum_match_value, "干预…", self._on_open_match),
            0, 3,
        )
        # ⑤ 预览入口
        preview_btn = QPushButton("🔍 预览")
        preview_btn.setObjectName("summary_btn")
        preview_btn.clicked.connect(self._on_open_preview)
        grid.addWidget(preview_btn, 0, 4)
        return bar

    @staticmethod
    def _summary_card(
        title: str,
        value_lbl: QLabel,
        btn_text: str,
        handler,
    ) -> QWidget:
        card = QFrame()
        card.setObjectName("summary_card")
        lay = QHBoxLayout(card)
        lay.setContentsMargins(Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM)
        lay.setSpacing(Spacing.MD)

        title_lbl = QLabel(title)
        title_lbl.setObjectName("summary_label")
        value_lbl.setObjectName("summary_value")
        btn = QPushButton(btn_text)
        btn.setObjectName("summary_btn")
        btn.clicked.connect(handler)

        lay.addWidget(title_lbl)
        lay.addWidget(value_lbl, 1)
        lay.addWidget(btn)
        return card

    def _build_docks(self) -> None:
        """S11 面板窗口化：侧边栏/执行区/结果面板 = 3 个 QDockWidget。

        全部支持：关闭（红绿灯叉号）/ 浮动 / 拖拽调大小 / 菜单栏重开。
        """
        # ── ① 侧边栏 dock（Profile 列表 + 转换历史） ─────────────────
        self._sidebar_dock = QDockWidget("侧边栏", self)
        self._sidebar_dock.setObjectName("v2_sidebar_dock")
        self._sidebar_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        sidebar = QWidget()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(
            Spacing.MD, Spacing.SM, Spacing.MD, Spacing.SM
        )
        side_layout.setSpacing(Spacing.SM)

        brand = QLabel("CIS2HDL")
        brand.setStyleSheet(
            # S11 P0③ 衬线标题（Anthropic 叙事感；中文回退 Noto Serif SC）
            "font-family: 'Lora', 'Noto Serif SC', 'Songti SC', serif;"
            " font-size: %dpx; font-weight: bold; color: %s;"
            % (FontSize.LG, Colors.TEXT_PRIMARY)
        )
        side_layout.addWidget(brand)

        side_layout.addWidget(self._section_label("PROFILE"))
        self.profile_list = ProfileList()
        self.profile_list.setMaximumHeight(220)
        side_layout.addWidget(self.profile_list)

        side_layout.addWidget(self._section_label("转换历史"))
        self.history_list = QListWidget()
        self.history_list.setMaximumHeight(180)
        self.history_list.itemDoubleClicked.connect(self._on_history_rerun)
        side_layout.addWidget(self.history_list)

        side_layout.addStretch(1)

        verify_btn = QPushButton("⚡ verify 快捷入口")
        verify_btn.clicked.connect(self._on_verify)
        side_layout.addWidget(verify_btn)

        version_lbl = QLabel(f"v{__version__} · pipeline.yaml 权威")
        version_lbl.setStyleSheet(
            "color: %s; font-size: %dpx;" % (Colors.TEXT_MUTED, FontSize.XXS)
        )
        side_layout.addWidget(version_lbl)

        self._sidebar_dock.setWidget(sidebar)
        self.addDockWidget(Qt.LeftDockWidgetArea, self._sidebar_dock)

        # ── ③ 执行区 dock（底部） ────────────────────────────────────
        self._runner_dock = QDockWidget("转换执行", self)
        self._runner_dock.setObjectName("v2_runner_dock")
        self._runner_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        self._runner_dock.setWidget(self.runner)
        self.addDockWidget(Qt.BottomDockWidgetArea, self._runner_dock)

        # ── ④ 结果面板 dock（右侧） ─────────────────────────────────
        self._result_dock = QDockWidget("结果面板", self)
        self._result_dock.setObjectName("v2_result")
        self._result_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea
        )
        tabs = QTabWidget()
        self.report_view = ReportView(self._controller)
        tabs.addTab(self.report_view, "报告")
        tabs.addTab(self.manual_panel, "手动匹配")
        tabs.addTab(SchematicPreview(), "原理图预览")
        self._result_dock.setWidget(tabs)
        self.addDockWidget(Qt.RightDockWidgetArea, self._result_dock)

    def _build_statusbar(self) -> None:
        self._status = QLabel("就绪")
        self._status.setStyleSheet(
            "color: %s; font-size: %dpx;" % (Colors.TEXT_SECONDARY, FontSize.XXS)
        )
        self.statusBar().addWidget(self._status)

    def _build_menubar(self) -> None:
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        save_action = QAction("保存 pipeline.yaml", self)
        save_action.setShortcut("Ctrl+S")
        save_action.triggered.connect(self._on_yaml_save)
        file_menu.addAction(save_action)
        file_menu.addSeparator()
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        run_menu = menubar.addMenu("运行")
        run_action = QAction("运行转换", self)
        run_action.setShortcut("Ctrl+R")
        run_action.triggered.connect(lambda: self.runner._on_run())
        run_menu.addAction(run_action)

        # ── S11: 工具菜单（弹窗入口） ────────────────────────────────
        tools_menu = menubar.addMenu("工具")
        import_action = QAction("导入文件…", self)
        import_action.triggered.connect(self._on_open_import)
        tools_menu.addAction(import_action)
        out_action = QAction("输出/报告设置…", self)
        out_action.triggered.connect(self._on_open_output)
        tools_menu.addAction(out_action)
        report_action = QAction("查看报告…", self)
        report_action.triggered.connect(self._on_open_report)
        tools_menu.addAction(report_action)
        match_action = QAction("手动匹配干预…", self)
        match_action.triggered.connect(self._on_open_match)
        tools_menu.addAction(match_action)
        preview_action = QAction("原理图预览…", self)
        preview_action.triggered.connect(self._on_open_preview)
        tools_menu.addAction(preview_action)

        # ── S11: 视图菜单（面板窗口开关，toggleViewAction） ──────────
        view_menu = menubar.addMenu("视图")
        view_menu.addAction(self._sidebar_dock.toggleViewAction())
        view_menu.addAction(self._runner_dock.toggleViewAction())
        view_menu.addAction(self._result_dock.toggleViewAction())

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section_title")
        return lbl

    # ── 信号接线 ──────────────────────────────────────────────────────────

    def _wire_signals(self) -> None:
        self.profile_list.profile_selected.connect(self._on_profile_selected)
        self.profile_bar.profile_selected.connect(self._on_profile_selected)
        self.profile_bar.profiles_changed.connect(self._on_profiles_changed)

        self.stage_tabs.plugin_toggled.connect(self._on_plugin_toggled)
        self.stage_tabs.plugin_reordered.connect(self._on_plugin_reordered)
        self.stage_tabs.param_changed.connect(self._on_param_changed)
        self.stage_tabs.reports_changed.connect(self._on_reports_changed)

        self.yaml_editor.apply_requested.connect(self._on_yaml_apply)
        self.yaml_editor.save_requested.connect(self._on_yaml_save)

        self.runner.run_finished.connect(self._on_run_finished)

        self.manual_panel.match_applied.connect(self._on_match_applied)

    # ── 双通道协调 ────────────────────────────────────────────────────────

    def _refresh_all(self) -> None:
        """profile 切换 / 启动后全量刷新。"""
        self._refresh_profiles()
        self._refresh_config_views()
        self._refresh_summary()

    def _refresh_profiles(self) -> None:
        infos = self._controller.profile_infos()
        self.profile_list.set_profiles(infos)
        self.profile_bar.refresh(current=self._controller.current_config.profile)

    def _refresh_config_views(self) -> None:
        """按当前 cfg 刷新表单 + yaml 预览（external，不标记脏）。"""
        self.stage_tabs.refresh()
        self.manual_panel.refresh()
        self.yaml_editor.set_text(
            cfg_to_yaml_text(self._controller.current_config), external=True
        )
        self._set_status(f"Profile: {self._controller.current_config.profile}")

    def _refresh_summary(self) -> None:
        """S11 主面板摘要：从当前配置 + 上次报告刷新 4 张卡。"""
        cfg = self._controller.current_config
        # 导入卡
        input_path = getattr(self._controller, "_input_path", None)
        self._sum_input_value.setText(
            str(input_path) if input_path else "未导入（可在导入弹窗选择）"
        )
        # 导出设置卡
        files = getattr(cfg.output, "files", [])
        reports = getattr(cfg.output, "reports", [])
        self._sum_output_value.setText(
            f"{len(files)} 文件 · {len(reports)} 报告"
        )
        # 报告摘要卡
        last = getattr(self._controller, "_last_report", None)
        if last is not None:
            self._sum_report_value.setText(
                f"{last.pages}页 / {last.instances}元件 / {last.nets}网"
            )
        else:
            self._sum_report_value.setText("未转换")
        # 手动匹配卡
        try:
            n = len(self._controller.get_unmatched())
        except Exception:  # noqa: BLE001
            n = 0
        self._sum_match_value.setText(f"{n} 未匹配")

    def _sync_yaml_from_form(self) -> None:
        """表单改动 → 实时 yaml 预览（用户未手动编辑 yaml 时）。"""
        if not self.yaml_editor.is_dirty():
            self.yaml_editor.set_text(
                cfg_to_yaml_text(self._controller.current_config), external=True
            )
        self.yaml_editor.set_sync_state(
            is_text_in_sync(self.yaml_editor.text(), self._controller.current_config)
        )

    def _set_status(self, msg: str) -> None:
        self._status.setText(msg)
        self.statusBar().showMessage(msg, 5000)

    # ── 表单 → cfg ────────────────────────────────────────────────────────

    def _on_plugin_toggled(self, stage: str, name: str, enabled: bool) -> None:
        cfg = self._controller.current_config
        names = list(getattr(self._stage_section(cfg, stage), self._stage_attr(stage)))
        if enabled and name not in names:
            names.append(name)
        elif not enabled and name in names:
            names.remove(name)
        set_stage_plugins(cfg, stage, names)
        self._sync_yaml_from_form()

    def _on_plugin_reordered(self, stage: str, names: list[str]) -> None:
        set_stage_plugins(self._controller.current_config, stage, names)
        self._sync_yaml_from_form()

    def _on_param_changed(self, plugin: str, path: str, value: Any) -> None:
        try:
            self._controller.apply_plugin_param(plugin, path, value)
        except Exception as exc:  # noqa: BLE001 — 表单异常不崩溃
            logger.warning("param apply failed: %s", exc)
            return
        self._sync_yaml_from_form()

    def _on_reports_changed(self, reports: list[str]) -> None:
        self._controller.current_config.output.reports = list(reports)
        self._sync_yaml_from_form()

    @staticmethod
    def _stage_section(cfg: Any, stage: str) -> Any:
        return {
            "input": cfg.input,
            "match": cfg.match,
            "beautify": cfg.beautify,
            "output": cfg.output,
            "test": cfg.test,
        }[stage]

    @staticmethod
    def _stage_attr(stage: str) -> str:
        return {
            "input": "plugins",
            "match": "plugins",
            "beautify": "plugins",
            "output": "files",
            "test": "suites",
        }[stage]

    # ── yaml → 表单 ───────────────────────────────────────────────────────

    def _on_yaml_apply(self) -> None:
        """yaml 校验合法后刷新表单（§4 双通道反向）。"""
        text = self.yaml_editor.text()
        try:
            parsed = yaml_text_to_cfg(text)
        except Exception as exc:  # noqa: BLE001 — YamlValidationError
            self.yaml_editor.mark_invalid(str(exc))
            return
        self._controller.set_current_config(parsed)
        self._refresh_config_views()
        self.yaml_editor.set_sync_state(True)
        self._set_status("已应用 yaml → 表单")

    def _on_yaml_save(self) -> None:
        """保存（原子写）：yaml 与表单冲突时提示覆盖确认。"""
        cfg = self._controller.current_config
        if self.yaml_editor.is_dirty():
            if not self.yaml_editor.validate():
                QMessageBox.warning(self, "保存失败", "yaml 非法，无法保存")
                return
            parsed = yaml_text_to_cfg(self.yaml_editor.text())
            if parsed.to_dict() != cfg.to_dict():
                ret = QMessageBox.question(
                    self,
                    "yaml 与表单不同步",
                    "yaml 已手动编辑且与表单不同。保存时以哪一侧为准？\n"
                    "Yes = 以 yaml 为准（覆盖表单）\n"
                    "No = 以表单为准（丢弃 yaml 编辑）",
                )
                if ret == QMessageBox.Yes:
                    self._controller.set_current_config(parsed)
                    self._refresh_config_views()
                else:
                    self.yaml_editor.set_text(cfg_to_yaml_text(cfg), external=True)
        try:
            path = self._controller.save_pipeline()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", str(exc))
            return
        self.yaml_editor.set_sync_state(True)
        self._set_status(f"已原子写保存: {path}")

    # ── Profile 动作 ──────────────────────────────────────────────────────

    def _on_profile_selected(self, name: str) -> None:
        try:
            self._controller.load_profile(name)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Profile 加载失败", str(exc))
            return
        self._refresh_config_views()
        self._refresh_summary()

    def _on_profiles_changed(self) -> None:
        self._refresh_profiles()

    # ── S11 弹窗入口 ──────────────────────────────────────────────────────

    def _on_open_import(self) -> None:
        dlg = ImportDialog(
            self._controller,
            input_path=str(getattr(self._controller, "_input_path", "") or ""),
            hdl_lib="",
            extra=[],
            parent=self,
        )
        if dlg.exec() == ImportDialog.Accepted:
            in_path, hdl_lib, extra = dlg.result_values
            if in_path:
                self._controller.set_input_path(in_path)
            if hdl_lib:
                self._controller.set_hdl_lib(hdl_lib)
            self._refresh_summary()
            self._set_status(f"已导入: {in_path}")

    def _on_open_output(self) -> None:
        cfg = self._controller.current_config
        dlg = OutputSettingsDialog(
            self._controller,
            files=list(getattr(cfg.output, "files", [])),
            reports=list(getattr(cfg.output, "reports", [])),
            output_dir=str(getattr(self._controller, "_output_dir", "") or ""),
            parent=self,
        )
        if dlg.exec() == OutputSettingsDialog.Accepted:
            files, reports, out_dir = dlg.result_values
            cfg.output.files = list(files)
            cfg.output.reports = list(reports)
            if out_dir:
                self._controller.set_output_dir(out_dir)
            self._sync_yaml_from_form()
            self._refresh_summary()
            self._set_status("输出/报告设置已更新")

    def _on_open_report(self) -> None:
        dlg = ReportDialog(self._controller, parent=self)
        dlg.exec()

    def _on_open_match(self) -> None:
        dlg = ManualMatchDialog(self._controller, parent=self)
        dlg.exec()
        self.manual_panel.refresh()
        self._refresh_summary()

    def _on_open_preview(self) -> None:
        dlg = PreviewDialog(self._controller, parent=self)
        dlg.exec()

    # ── 转换结果 ──────────────────────────────────────────────────────────

    def _on_run_finished(self, report: Any) -> None:
        self.report_view.refresh()
        self.manual_panel.refresh()
        self._refresh_summary()
        # S11: 写入持久化历史
        input_file = str(getattr(self._controller, "_input_path", "") or "")
        profile = getattr(self._controller.current_config, "profile", "") or ""
        entry = self._history_store.add(report, input_file=input_file, profile=profile)
        self.history_list.insertItem(0, entry.summary)
        if self.history_list.count() > 50:
            self.history_list.takeItem(self.history_list.count() - 1)
        self._set_status("转换完成，结果已刷新")

    def _on_history_rerun(self, item: Any) -> None:
        """双击历史项 → 重跑该次转换（复用输入文件）。"""
        idx = self.history_list.row(item)
        entries = self._history_store.entries()
        if idx >= len(entries):
            return
        entry = entries[idx]
        if not entry.input_file:
            self._set_status("该历史记录无输入文件，无法重跑")
            return
        try:
            self._controller.set_input_path(entry.input_file)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重跑失败", str(exc))
            return
        self.runner.set_input_text(entry.input_file)
        self._refresh_summary()
        self._set_status(f"已载入历史输入: {entry.input_file}（点击运行转换重跑）")

    def _on_match_applied(self, refdes: str, hdl: str, force_mock: bool) -> None:
        try:
            self._controller.set_manual_match(refdes, hdl or None, force_mock)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "手动匹配失败", str(exc))
            return
        self._sync_yaml_from_form()
        self._refresh_summary()
        if hdl:
            self._set_status(f"手动匹配: {refdes} → {hdl}（已写入 chip_config_gui.yaml）")
        else:
            self._set_status(f"已撤销 {refdes} 的手动匹配")

    # ── verify 快捷入口 ───────────────────────────────────────────────────

    def _on_verify(self) -> None:
        try:
            lines = self._controller.run_verify()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, "verify 失败", str(exc))
            return
        text = "\n".join(lines) if lines else "（无验证输出）"
        QMessageBox.information(self, "verify 结果", text)
