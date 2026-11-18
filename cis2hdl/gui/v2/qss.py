"""S9 GUI v2 QSS 样式表 — 复用 ``cis2hdl/gui/colors.py`` Token（Anthropic 暖米色）。

设计依据：``docs/gui-design.md`` §6（视觉方向：工程工作台，克制中性色 +
深色可停靠面板 + 清晰层级）。所有样式值来自 colors.py Token，禁止裸值。
"""

from __future__ import annotations

from ..colors import (
    Colors,
    Fonts,
    FontSize,
    Layout,
    Radius,
    Spacing,
    rgba,
)

__all__ = ["STYLE_V2", "STYLE_RUNNER", "STYLE_YAML", "STYLE_REPORT", "STYLE_SIDEBAR"]

#: 侧边栏（Profile 列表）样式。
STYLE_SIDEBAR = f"""
    QWidget#v2_sidebar {{
        background-color: {Colors.BG_RAISED};
        border-right: 1px solid {Colors.BORDER_SUBTLE};
    }}
    QListWidget#profile_list {{
        background-color: {Colors.BG_RAISED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
        padding: {Spacing.XS}px;
    }}
    QListWidget#profile_list::item {{
        padding: {Spacing.SM}px {Spacing.MD}px;
        border-radius: {Radius.MD};
        color: {Colors.TEXT_PRIMARY};
        font-size: {FontSize.SM}px;
    }}
    QListWidget#profile_list::item:selected {{
        background-color: {rgba(Colors.ACCENT, 0.14)};
        color: {Colors.TEXT_PRIMARY};
        border-left: 3px solid {Colors.ACCENT};
    }}
    QListWidget#profile_list::item:hover:!selected {{
        background-color: {rgba(Colors.ACCENT, 0.06)};
    }}
"""

#: Profile 工具栏 / 配置编辑器区样式。
STYLE_V2 = f"""
    QWidget#v2_root {{
        background-color: {Colors.BG_BASE};
        color: {Colors.TEXT_PRIMARY};
        font-family: {Fonts.UI};
        font-size: {FontSize.SM}px;
    }}
    QWidget#card {{
        background-color: {Colors.BG_RAISED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
    }}
    QLabel#section_title {{
        color: {Colors.TEXT_SECONDARY};
        font-size: {FontSize.XS}px;
        font-weight: bold;
        letter-spacing: 1px;
    }}
    QLabel#plugin_desc {{
        color: {Colors.TEXT_SECONDARY};
        font-size: {FontSize.XS}px;
    }}
    QLabel#badge_builtin {{
        background-color: {rgba(Colors.AUX_BLUE, 0.16)};
        color: {Colors.INFO};
        border-radius: {Radius.SM};
        padding: 1px {Spacing.SM}px;
        font-size: {FontSize.XXS}px;
    }}
    QLabel#badge_custom {{
        background-color: {rgba(Colors.AUX_SAND, 0.30)};
        color: {Colors.TEXT_SECONDARY};
        border-radius: {Radius.SM};
        padding: 1px {Spacing.SM}px;
        font-size: {FontSize.XXS}px;
    }}
    QLabel#dup_feedback {{
        background-color: {rgba(Colors.WARNING, 0.12)};
        color: {Colors.WARNING};
        border: 1px solid {rgba(Colors.WARNING, 0.4)};
        border-radius: {Radius.MD};
        padding: {Spacing.SM}px {Spacing.MD}px;
        font-size: {FontSize.XS}px;
    }}
    QPushButton {{
        background-color: {Colors.BG_OVERLAY};
        color: {Colors.TEXT_PRIMARY};
        border: 1px solid {Colors.BORDER_DEFAULT};
        border-radius: {Radius.MD};
        padding: {Spacing.SM}px {Spacing.MD}px;
        font-size: {FontSize.SM}px;
        min-height: {Layout.BUTTON_MIN_H - 8}px;
    }}
    QPushButton:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
    QPushButton#primary {{
        background-color: {Colors.ACCENT};
        color: {Colors.BG_OVERLAY};
        border: none;
        font-weight: bold;
    }}
    QPushButton#primary:hover {{
        background-color: {Colors.ACCENT_HOVER};
        color: {Colors.BG_OVERLAY};
    }}
    QPushButton#danger {{
        color: {Colors.ERROR};
        border-color: {Colors.ERROR};
    }}
    QTabWidget::pane {{
        border: none;
        background: transparent;
    }}
    QTabBar::tab {{
        padding: 8px 18px;
        border: none;
        color: {Colors.TEXT_SECONDARY};
        font-size: {FontSize.SM}px;
    }}
    QTabBar::tab:selected {{
        color: {Colors.TEXT_PRIMARY};
        border-bottom: 2px solid {Colors.ACCENT};
    }}
    QTabBar::tab:hover:!selected {{
        color: {Colors.TEXT_PRIMARY};
    }}
    QScrollArea {{
        border: none;
        background: transparent;
    }}
    QScrollBar:vertical {{
        background: transparent;
        width: 10px;
    }}
    QScrollBar::handle:vertical {{
        background: {Colors.BORDER_DEFAULT};
        border-radius: 5px;
        min-height: 24px;
    }}
"""

#: 转换执行区样式（进度条/日志）。
STYLE_RUNNER = f"""
    QWidget#v2_runner {{
        background-color: {Colors.BG_RAISED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
    }}
    QProgressBar {{
        border: none;
        border-radius: {Radius.SM};
        background-color: {rgba(Colors.ACCENT, 0.12)};
        height: 8px;
        text-align: center;
        font-size: {FontSize.XXS}px;
    }}
    QProgressBar::chunk {{
        background-color: {Colors.ACCENT};
        border-radius: {Radius.SM};
    }}
    QPlainTextEdit#log_content {{
        background-color: {Colors.BG_INVERTED};
        color: {Colors.TEXT_INVERTED};
        border: none;
        border-radius: {Radius.MD};
        font-family: {Fonts.MONO};
        font-size: {FontSize.XS}px;
    }}
    QLabel#stage_label {{
        font-size: {FontSize.XXS}px;
        color: {Colors.TEXT_SECONDARY};
    }}
"""

#: yaml 编辑器样式（等宽 + 深色）。
STYLE_YAML = f"""
    QPlainTextEdit#yaml_editor {{
        background-color: {Colors.BG_INVERTED};
        color: {Colors.TEXT_INVERTED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.MD};
        font-family: {Fonts.MONO};
        font-size: {FontSize.XS}px;
        selection-background-color: {rgba(Colors.ACCENT, 0.45)};
    }}
    QLabel#yaml_invalid {{
        color: {Colors.ERROR};
        font-size: {FontSize.XS}px;
    }}
"""

#: 结果面板样式。
STYLE_REPORT = f"""
    QWidget#v2_result {{
        background-color: {Colors.BG_RAISED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
    }}
    QPlainTextEdit#report_content {{
        background-color: {Colors.BG_OVERLAY};
        color: {Colors.TEXT_PRIMARY};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.MD};
        font-family: {Fonts.MONO};
        font-size: {FontSize.XS}px;
    }}
    QTableWidget {{
        background-color: {Colors.BG_OVERLAY};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.MD};
        font-size: {FontSize.XS}px;
    }}
    QHeaderView::section {{
        background-color: {Colors.BG_RAISED};
        color: {Colors.TEXT_SECONDARY};
        border: none;
        border-bottom: 1px solid {Colors.BORDER_SUBTLE};
        padding: {Spacing.XS}px;
        font-size: {FontSize.XS}px;
        font-weight: bold;
    }}
    QTableWidget::item:selected {{
        background-color: {rgba(Colors.AUX_BLUE, 0.18)};
    }}
"""

#: S11 面板窗口化：QDockWidget 红绿灯关闭按钮 + 标题栏（macOS 风格）。
STYLE_DOCKS = f"""
    QDockWidget {{
        color: {Colors.TEXT_PRIMARY};
        titlebar-close-icon: none;
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
    }}
    QDockWidget::title {{
        background-color: {Colors.BG_RAISED};
        border-top-left-radius: {Radius.LG};
        border-top-right-radius: {Radius.LG};
        padding: 4px {Spacing.MD}px;
        font-size: {FontSize.XS}px;
        font-weight: bold;
        color: {Colors.TEXT_SECONDARY};
    }}
    QDockWidget::close-button {{
        background-color: {Colors.ERROR};
        border: none;
        border-radius: 6px;
        width: 12px;
        height: 12px;
        margin-right: {Spacing.SM}px;
        subcontrol-position: top right;
    }}
    QDockWidget::close-button:hover {{
        background-color: #E0443E;
        border: 1px solid #C0453A;
    }}
    QDockWidget::float-button {{
        background-color: {Colors.AUX_SAND};
        border: none;
        border-radius: 6px;
        width: 12px;
        height: 12px;
        margin-right: 2px;
        subcontrol-position: top right;
    }}
    QDockWidget::float-button:hover {{
        background-color: {Colors.ACCENT};
    }}
"""

#: S11 主面板摘要卡样式。
STYLE_SUMMARY = f"""
    QFrame#summary_card {{
        background-color: {Colors.BG_RAISED};
        border: 1px solid {Colors.BORDER_SUBTLE};
        border-radius: {Radius.LG};
    }}
    QLabel#summary_value {{
        color: {Colors.TEXT_PRIMARY};
        font-size: {FontSize.SM}px;
        font-weight: bold;
    }}
    QLabel#summary_label {{
        color: {Colors.TEXT_SECONDARY};
        font-size: {FontSize.XS}px;
    }}
    QPushButton#summary_btn {{
        background-color: {Colors.BG_OVERLAY};
        border: 1px solid {Colors.BORDER_DEFAULT};
        border-radius: {Radius.MD};
        padding: {Spacing.XS}px {Spacing.SM}px;
        font-size: {FontSize.XS}px;
    }}
    QPushButton#summary_btn:hover {{
        border-color: {Colors.ACCENT};
        color: {Colors.ACCENT};
    }}
"""

#: S11 视觉 P0/P1：背景渐变晕染 + 橙色焦点环 + 危险操作强调。
#: 轻量化原则：纯 QSS（渲染器原生，零 JS/零自绘，内存增量 < 1MB）。
STYLE_VISUAL = f"""
    QWidget#v2_root {{
        background-color: {Colors.BG_BASE};
        background-image: qradialgradient(
            cx: 0.2, cy: 0.1, radius: 0.9,
            fx: 0.2, fy: 0.1,
            stop: 0 rgba(217, 119, 87, 0.07),
            stop: 1 rgba(217, 119, 87, 0)
        );
    }}
    QPushButton:focus, QLineEdit:focus, QComboBox:focus, QCheckBox:focus,
    QPlainTextEdit:focus, QListWidget:focus {{
        border: 1px solid {Colors.ACCENT};
        outline: 2px solid {rgba(Colors.ACCENT, 0.35)};
    }}
    QPushButton#danger {{
        background-color: {rgba(Colors.ERROR, 0.10)};
        color: {Colors.ERROR};
        border: 1px solid {Colors.ERROR};
    }}
    QPushButton#danger:hover {{
        background-color: {Colors.ERROR};
        color: {Colors.BG_OVERLAY};
    }}
"""
