"""OLE 复合文档最小 CFB 读取器（无第三方依赖备选）。

实现自研 CFB（Compound File Binary）读取器的最小子集：
- 魔数校验
- 目录项遍历（扁平列表，不做树路径重建 —— 绕开 OrCAD sibling/child 指针损坏问题）
- 按顶层流名 / 路径读流

仅用于 ``OleReaderProvider`` 备选路径；主路径使用 ``olefile``。
"""

from __future__ import annotations

import struct
from pathlib import Path

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SECTOR_SIZE = 512
_DIR_ENTRY_SIZE = 128
_FREE = 0xFFFFFFFF
_ENDOFCHAIN = 0xFFFFFFFE

# 目录项字段偏移
_DE_NAME = 0x00        # 64B UTF-16
_DE_NAME_LEN = 0x40    # u16
_DE_TYPE = 0x42        # u8
_DE_START = 0x74       # u32 (sect)
_DE_SIZE = 0x78        # u64


class _DirEntry:
    __slots__ = ("name", "type", "start", "size", "path")

    def __init__(self, name: str, type_: int, start: int, size: int) -> None:
        self.name = name
        self.type = type_  # 1=storage, 2=stream, 5=root
        self.start = start
        self.size = size
        self.path: tuple[str, ...] = ()


class FallbackCfbReader:
    """最小 CFB 读取器（扁平目录 + 直接流读取）。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._data = self._path.read_bytes()
        if not self._data.startswith(_CFB_MAGIC):
            raise ValueError(f"Not a CFB compound document: {self._path}")
        self._sector_shift = struct.unpack_from("<H", self._data, 0x1E)[0]
        self._sector_size = 1 << self._sector_shift
        self._mini_sector_shift = struct.unpack_from("<H", self._data, 0x20)[0]
        self._mini_sector_size = 1 << self._mini_sector_shift
        self._num_dir_sectors = struct.unpack_from("<I", self._data, 0x28)[0]
        self._num_fat_sectors = struct.unpack_from("<I", self._data, 0x2C)[0]
        self._first_dir_sector = struct.unpack_from("<I", self._data, 0x30)[0]
        self._mini_stream_cutoff = struct.unpack_from("<I", self._data, 0x38)[0]
        self._first_mini_fat_sector = struct.unpack_from("<I", self._data, 0x3C)[0]
        self._num_mini_fat_sectors = struct.unpack_from("<I", self._data, 0x40)[0]
        self._first_difat_sector = struct.unpack_from("<I", self._data, 0x44)[0]
        self._num_difat_sectors = struct.unpack_from("<I", self._data, 0x48)[0]

        self._fat: list[int] = self._read_fat()
        self._mini_fat: list[int] = self._read_mini_fat()
        self._directory: list[_DirEntry] = self._read_directory()
        self._mini_stream: bytes = self._read_mini_stream()

    # ── sector helpers ──────────────────────────────────────────────

    def _sector_offset(self, sector: int) -> int:
        return (sector + 1) * self._sector_size

    def _read_sector(self, sector: int) -> bytes:
        off = self._sector_offset(sector)
        return self._data[off:off + self._sector_size]

    def _read_fat(self) -> list[int]:
        """从 DIFAT（FAT 扇区链）读取完整 FAT。"""
        fat: list[int] = []
        # 前 109 个 FAT 扇区号在 header
        sector_ids: list[int] = list(
            struct.unpack_from("<109I", self._data, 0x4C)
        )
        sector_ids = [s for s in sector_ids if s not in (_FREE, _ENDOFCHAIN)]
        # 扩展 DIFAT
        difat = self._first_difat_sector
        for _ in range(self._num_difat_sectors):
            if difat in (_FREE, _ENDOFCHAIN):
                break
            sec = self._read_sector(difat)
            per = self._sector_size // 4 - 1
            for i in range(per):
                nxt = struct.unpack_from("<I", sec, i * 4)[0]
                if nxt not in (_FREE, _ENDOFCHAIN):
                    sector_ids.append(nxt)
            difat = struct.unpack_from("<I", sec, per * 4)[0]

        for sid in sector_ids:
            sec = self._read_sector(sid)
            per = self._sector_size // 4
            for i in range(per):
                fat.append(struct.unpack_from("<I", sec, i * 4)[0])
        return fat

    def _read_mini_fat(self) -> list[int]:
        mini_fat: list[int] = []
        sect = self._first_mini_fat_sector
        for _ in range(max(self._num_mini_fat_sectors, 0)):
            if sect in (_FREE, _ENDOFCHAIN):
                break
            sec = self._read_sector(sect)
            per = self._sector_size // 4
            for i in range(per):
                mini_fat.append(struct.unpack_from("<I", sec, i * 4)[0])
            sect = self._fat[sect] if sect < len(self._fat) else _ENDOFCHAIN
        return mini_fat

    def _read_directory(self) -> list[_DirEntry]:
        entries: list[_DirEntry] = []
        sect = self._first_dir_sector
        visited: set[int] = set()
        while sect not in (_FREE, _ENDOFCHAIN) and sect not in visited:
            visited.add(sect)
            sec = self._read_sector(sect)
            per = self._sector_size // _DIR_ENTRY_SIZE
            for i in range(per):
                off = i * _DIR_ENTRY_SIZE
                name_raw = sec[off + _DE_NAME: off + _DE_NAME + 64]
                name_len = struct.unpack_from("<H", sec, off + _DE_NAME_LEN)[0]
                type_ = sec[off + _DE_TYPE]
                start = struct.unpack_from("<I", sec, off + _DE_START)[0]
                size = struct.unpack_from("<Q", sec, off + _DE_SIZE)[0]
                if name_len >= 2:
                    name = name_raw[: name_len - 2].decode("utf-16-le", errors="replace")
                else:
                    name = ""
                if type_ in (1, 2, 5):
                    entries.append(_DirEntry(name, type_, start, size))
            sect = self._fat[sect] if sect < len(self._fat) else _ENDOFCHAIN

        # 扁平路径重建（按父子关系，只读不用做树遍历）
        by_start: dict[int, _DirEntry] = {}
        for e in entries:
            if e.type == 5:
                e.path = ()
        # 建立 parent 映射：root(start 为特殊) → children
        # CFB 目录树通过 sibling/child 指针组织，但我们只需要名字列表，
        # 因此直接按 root 目录顺序 + 已知顶层名推断路径。
        root = next((e for e in entries if e.type == 5), None)
        if root is not None:
            by_start[root.start] = root
        # 顶层流（目录顺序中紧随 root 的 storage 项）：
        # 无法可靠重建树，退化为"顶层名 = 目录名"。
        return entries

    def _read_mini_stream(self) -> bytes:
        root = next((e for e in self._directory if e.type == 5), None)
        if root is None or root.start in (_FREE, _ENDOFCHAIN):
            return b""
        chain = self._fat_chain(root.start, use_mini=False)
        out = bytearray()
        for s in chain:
            out += self._read_sector(s)
        return bytes(out[: root.size])

    def _fat_chain(self, start: int, use_mini: bool) -> list[int]:
        chain: list[int] = []
        s = start
        table = self._mini_fat if use_mini else self._fat
        visited: set[int] = set()
        while s not in (_FREE, _ENDOFCHAIN) and s not in visited:
            visited.add(s)
            chain.append(s)
            s = table[s] if s < len(table) else _ENDOFCHAIN
        return chain

    # ── public API ──────────────────────────────────────────────────

    def list_entries(self) -> list[tuple[str, ...]]:
        """返回扁平条目路径列表（尽力而为）。"""
        # 由于 CFB 树指针损坏风险，这里返回"扁平名 + 推断路径"。
        # 页面流前缀匹配在 OleReaderProvider 中完成，因此这里只需
        # 给出可用的路径形式。为简化，直接把目录名作为单元素路径。
        result: list[tuple[str, ...]] = []
        for e in self._directory:
            if e.type in (1, 2):
                result.append((e.name,))
        # 补充常见顶层流名（避免目录解析遗漏）
        for top in ("Cache", "Library", "Packages", "Schematic", "AdminData", "DsnStream"):
            if top not in {r[0] for r in result}:
                # 尝试按名字查找任意深度条目
                found = next((e for e in self._directory if e.name == top), None)
                if found is not None:
                    result.append((top,))
        return result

    def read_stream(self, name: str) -> bytes:
        """按顶层流名读取流（Packages 作为前缀时返回拼接？—— 不，
        这里只支持单流名；Packages 用 read_stream_path）。"""
        entry = next(
            (e for e in self._directory if e.type == 2 and e.name == name),
            None,
        )
        if entry is None:
            raise KeyError(f"stream not found: {name}")
        return self._read_stream_data(entry)

    def read_stream_path(self, path: tuple[str, ...]) -> bytes:
        """按路径读取流。实现为：路径最后一段名匹配（扁平查找）。"""
        if not path:
            raise KeyError("empty path")
        name = path[-1]
        entry = next(
            (e for e in self._directory if e.type == 2 and e.name == name),
            None,
        )
        if entry is None:
            raise KeyError(f"stream not found: {'/'.join(path)}")
        return self._read_stream_data(entry)

    def _read_stream_data(self, entry: _DirEntry) -> bytes:
        size = entry.size
        if size < self._mini_stream_cutoff:
            # mini stream
            if entry.start in (_FREE, _ENDOFCHAIN):
                return b""
            chain = self._fat_chain(entry.start, use_mini=True)
            out = bytearray()
            for s in chain:
                off = s * self._mini_sector_size
                out += self._mini_stream[off: off + self._mini_sector_size]
            return bytes(out[:size])
        # regular stream
        if entry.start in (_FREE, _ENDOFCHAIN):
            return b""
        chain = self._fat_chain(entry.start, use_mini=False)
        out = bytearray()
        for s in chain:
            out += self._read_sector(s)
        return bytes(out[:size])
