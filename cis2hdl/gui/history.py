"""S11 转换历史 — 轻量 JSON 持久化（history.json）。

设计：每次转换完成后记录一条历史（时间戳/输入/profile/输出/结果统计/报告路径），
支持回看（重新打开报告弹窗）与重跑（复用输入重新转换）。JSON 存储避免引入
数据库依赖，单条记录 < 1KB，历史上限 50 条防膨胀。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = ["HistoryEntry", "ConversionHistory", "history_path"]

_HISTORY_FILE = "history.json"
_MAX_ENTRIES = 50


def history_path(base: Path | None = None) -> Path:
    """历史文件路径：优先用户配置目录，缺省输出目录。"""
    if base is not None:
        return base / _HISTORY_FILE
    home = Path.home() / ".cis2hdl"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError:
        home = Path(".")
    return home / _HISTORY_FILE


@dataclass
class HistoryEntry:
    """一条转换历史记录。"""

    ts: str = ""                     # ISO 时间戳
    project: str = ""                # 工程名
    input_file: str = ""             # 输入文件
    profile: str = ""                # 使用的 profile
    output_dir: str = ""             # 输出目录
    success: bool = False
    pages: int = 0
    instances: int = 0
    nets: int = 0
    errors: int = 0
    warnings: int = 0
    elapsed: float = 0.0
    report_paths: list[str] = field(default_factory=list)  # 生成的报告文件

    @property
    def summary(self) -> str:
        """侧边栏展示一行。"""
        mark = "✔" if self.success else "✘"
        return (
            f"{mark} {self.project} · {self.pages}页/{self.instances}元件"
            f" · {self.profile}"
        )


class ConversionHistory:
    """转换历史（JSON 持久化）。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else history_path()
        self._entries: list[HistoryEntry] = []
        self._load()

    # ── 持久化 ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("history 读取失败: %s", exc)
            return
        if not isinstance(data, list):
            return
        for item in data:
            try:
                self._entries.append(HistoryEntry(**item))
            except (TypeError, ValueError):
                continue
        self._entries = self._entries[:_MAX_ENTRIES]

    def _save(self) -> None:
        try:
            self._path.write_text(
                json.dumps(
                    [asdict(e) for e in self._entries],
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("history 写入失败: %s", exc)

    # ── 操作 ───────────────────────────────────────────────────────────

    def add(self, report: object, input_file: str = "", profile: str = "") -> HistoryEntry:
        """转换完成后追加一条（自动截断到 _MAX_ENTRIES）。"""
        entry = HistoryEntry(
            ts=datetime.now().isoformat(timespec="seconds"),
            project=str(getattr(report, "project_name", "") or "design"),
            input_file=input_file,
            profile=profile,
            output_dir=str(getattr(report, "output_dir", "") or ""),
            success=bool(getattr(report, "success", False)),
            pages=int(getattr(report, "pages", 0) or 0),
            instances=int(getattr(report, "instances", 0) or 0),
            nets=int(getattr(report, "nets", 0) or 0),
            errors=len(getattr(report, "errors", []) or []),
            warnings=len(getattr(report, "warnings", []) or []),
            elapsed=float(getattr(report, "total_elapsed", 0.0) or 0.0),
            report_paths=[
                str(p) for p in (getattr(report, "output_files", []) or [])
            ],
        )
        self._entries.insert(0, entry)
        del self._entries[_MAX_ENTRIES:]
        self._save()
        return entry

    def entries(self, limit: int = 50) -> list[HistoryEntry]:
        return self._entries[:limit]

    def clear(self) -> None:
        """清空历史。"""
        self._entries.clear()
        self._save()

    @property
    def path(self) -> Path:
        return self._path
