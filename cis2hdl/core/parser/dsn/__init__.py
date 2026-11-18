"""DSN 解析器包（架构设计 §2 五层管道 + L6 顶层调度）。

层结构：
- L1 ``io``：OLE 流读取（IOleProvider / OlefileProvider / OleReaderProvider）
- L2 ``frame``：8B 分隔符切帧（Frame / split_frames）
- L2.5 ``strlst``：Library 流 strLst（2170 项双策略解析）
- L3 ``decode``：语义帧 + 结构帧解码器（注册表模式）
- L4 ``cache`` + ``bind``：Cache/Packages 解析 + 实例绑定
- L5 ``ir``：DSN 记录 → Pydantic IR
- L6 ``dsn_parser``：顶层调度（新帧管道 + RTL 回退）

入口：``DSNParser``（FORMAT_NAME='CIS_DSN'，.dsn）。
"""

from __future__ import annotations

from .dsn_parser import DSNParser
from .legacy_dsn_parser import LegacyDSNParser

__all__ = [
    "DSNParser",
    "LegacyDSNParser",
]
