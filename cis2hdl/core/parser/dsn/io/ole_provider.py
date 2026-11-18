"""OLE 复合文档流读取提供者。

设计目标（架构设计 §2 L1）：
- ``IOleProvider`` 定义"命名流 → bytes"的最小接口
- ``OlefileProvider`` 基于成熟的 ``olefile`` 库（研究目录所有 QA 实证脚本已验证）
- ``OleReaderProvider`` 基于项目内自研 ``ole_reader``（Bug A 修复后作为无第三方依赖备选）

关键决策：
- 页面流识别**不依赖 CFB 目录树遍历**（OrCAD 文件 sibling/child 指针可能损坏），
  而是使用 ``ole.listdir()`` 扁平流名列表 + ``["Views", <设计名>, "Pages", *]`` 前缀匹配。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


class IOleProvider(Protocol):
    """OLE 复合文档读取接口（Protocol）。"""

    def read_stream(self, name: str) -> bytes:
        """读取顶层流（Cache / Library / Packages / Schematic 等）。

        Args:
            name: 顶层流名，如 ``"Cache"``、``"Library"``。

        Returns:
            流原始字节。

        Raises:
            KeyError: 流不存在。
        """
        ...

    def page_streams(self, design_name: str) -> list[tuple[str, bytes]]:
        """按 ``["Views", design_name, "Pages", *]`` 前缀发现全部页面流。

        Returns:
            [(页面流名, bytes)]，按页面流名排序（保证 01~18 顺序稳定）。
        """
        ...

    def packages(self) -> list[tuple[str, bytes]]:
        """发现全部 Packages/* 流。

        Returns:
            [(包流名, bytes)]。
        """
        ...


class OlefileProvider:
    """基于 ``olefile`` 的 OLE 流读取器（主实现）。

    Usage::

        provider = OlefileProvider(Path("design.dsn"))
        cache = provider.read_stream("Cache")
        for name, data in provider.page_streams("TG0C0D2_VB"):
            ...
    """

    def __init__(self, path: str | Path) -> None:
        """打开 OLE 复合文档。

        Args:
            path: .dsn 文件路径。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 文件不是合法 OLE2 复合文档（魔数校验失败）。
        """
        import olefile

        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"DSN file not found: {self._path}")
        self._ole = olefile.OleFileIO(str(self._path))
        if not self._ole:
            raise ValueError(f"Not a valid OLE2 compound document: {self._path}")
        self._entries = [tuple(e) for e in self._ole.listdir()]

    # ── 基础流读取 ──────────────────────────────────────────────────

    def read_stream(self, name: str) -> bytes:
        """读取顶层命名流。

        Args:
            name: 顶层流名。

        Returns:
            流原始字节。

        Raises:
            KeyError: 流不存在。
        """
        try:
            return self._ole.openstream(name).read()
        except OSError as exc:
            raise KeyError(f"OLE stream not found: {name}") from exc

    def has_stream(self, name: str) -> bool:
        """顶层流是否存在。"""
        return any(len(e) == 1 and e[0] == name for e in self._entries)

    # ── 页面流 / 包流发现 ──────────────────────────────────────────

    def page_streams(self, design_name: str) -> list[tuple[str, bytes]]:
        """按页面流前缀发现全部页面流。"""
        prefix = ("Views", design_name, "Pages")
        matched = [
            e for e in self._entries
            if len(e) == 4 and e[:3] == prefix
        ]
        matched.sort(key=lambda e: e[3])
        result: list[tuple[str, bytes]] = []
        for entry in matched:
            try:
                result.append((entry[3], self._ole.openstream(entry).read()))
            except OSError as exc:
                logger.warning("Failed to read page stream %s: %s", "/".join(entry), exc)
        return result

    def packages(self) -> list[tuple[str, bytes]]:
        """发现全部 Packages/* 流。"""
        matched = [e for e in self._entries if len(e) == 2 and e[0] == "Packages"]
        matched.sort(key=lambda e: e[1])
        result: list[tuple[str, bytes]] = []
        for entry in matched:
            try:
                result.append((entry[1], self._ole.openstream(entry).read()))
            except OSError as exc:
                logger.warning("Failed to read package stream %s: %s", "/".join(entry), exc)
        return result

    def design_name(self) -> str | None:
        """尝试从流名结构推断设计名（``Views/<design>/Pages``）。"""
        for e in self._entries:
            if len(e) >= 3 and e[0] == "Views" and e[2] == "Pages":
                return e[1]
        return None

    def close(self) -> None:
        """关闭 OLE 文件。"""
        try:
            self._ole.close()
        except Exception:
            pass


class OleReaderProvider:
    """基于项目内自研 ``ole_reader`` 的备选读取器。

    当环境无 ``olefile`` 时使用。内部**只走扁平流名查找**
    （``list_all_entries`` + ``read_stream``），绕开目录树遍历
    （架构设计 Bug A 修复思路）。
    """

    def __init__(self, path: str | Path) -> None:
        from ._fallback_reader import FallbackCfbReader

        self._path = Path(path)
        if not self._path.exists():
            raise FileNotFoundError(f"DSN file not found: {self._path}")
        self._reader = FallbackCfbReader(self._path)
        self._entries: list[tuple[str, ...]] = [
            tuple(e) for e in self._reader.list_entries()
        ]

    # ── 基础流读取 ──────────────────────────────────────────────────

    def read_stream(self, name: str) -> bytes:
        try:
            return self._reader.read_stream(name)
        except KeyError as exc:
            raise KeyError(f"OLE stream not found: {name}") from exc

    def has_stream(self, name: str) -> bool:
        return any(len(e) == 1 and e[0] == name for e in self._entries)

    # ── 页面流 / 包流发现 ──────────────────────────────────────────

    def page_streams(self, design_name: str) -> list[tuple[str, bytes]]:
        prefix = ("Views", design_name, "Pages")
        matched = [e for e in self._entries if len(e) == 4 and e[:3] == prefix]
        matched.sort(key=lambda e: e[3])
        result: list[tuple[str, bytes]] = []
        for entry in matched:
            try:
                result.append((entry[3], self._reader.read_stream_path(entry)))
            except Exception as exc:
                logger.warning("Failed to read page stream %s: %s", "/".join(entry), exc)
        return result

    def packages(self) -> list[tuple[str, bytes]]:
        matched = [e for e in self._entries if len(e) == 2 and e[0] == "Packages"]
        matched.sort(key=lambda e: e[1])
        result: list[tuple[str, bytes]] = []
        for entry in matched:
            try:
                result.append((entry[1], self._reader.read_stream_path(entry)))
            except Exception as exc:
                logger.warning("Failed to read package stream %s: %s", "/".join(entry), exc)
        return result

    def design_name(self) -> str | None:
        for e in self._entries:
            if len(e) >= 3 and e[0] == "Views" and e[2] == "Pages":
                return e[1]
        return None

    def close(self) -> None:
        pass
