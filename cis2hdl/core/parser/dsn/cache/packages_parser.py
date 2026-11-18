"""Packages 流 pinMap/pinNames 解析器（架构设计 §2 cache）。

实证（QA package_full_decode.json 字节级验证，11 包）：
    Packages/<流名> 流本身也是 8B 分隔符帧结构（``FF E4 5C 39 00 00 00 00``）。
    pinMap 位于**最后一帧**（type=0 或 type=0x0001 主单元帧）：
    ``[len16][包名][00][pinCount u16][(len16)(pinNum)(00)(bitmap u8)]*pinCount``
    包名 = 流名（单单元）或 流名+单元字母（多单元，如 Hi5662YV305_DDR_GE_0F）。
    pinNames 位于 pinMap 帧之前的各**单串帧**（每帧一个 len16 串，帧类型恰为
    串长），按顺序收集即引脚名序列（多单元包为全部单元引脚名）。

    11 包：DDR3=96、Hi5662=178、2P5GEPHY=49、USB=8、NPN=3、LC4=4、
    LB=2、LC_J=4、PNP=4、SLC NAND FLASH_0/1=9。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.types import DELIMITER

logger = logging.getLogger(__name__)

_DELIM_RE = re.compile(re.escape(DELIMITER))

#: 单元名后缀（多单元包：流名 + 单字母，如 Hi5662YV305_DDR_GE_0A~F）
_UNIT_SUFFIX_RE = re.compile(r"^[A-Za-z]$")

#: 过滤 pinNames 时的噪声串
_NOISE_NAMES = frozenset({"*", "U", "Q", "T", "J", "LB", "F"})
#: 明显不是引脚名的结尾
_NOISE_ENDS = (".Normal", ".Convert")


@dataclass
class PinEntry:
    """一个引脚编号。"""

    number: str
    bitmap: int = 0

    @property
    def is_ignore(self) -> bool:
        return bool(self.bitmap & 0x80)

    @property
    def group(self) -> int | None:
        g = self.bitmap & 0x7F
        return None if g == 0x7F else g


@dataclass
class PackageData:
    """一个 Package 流解析结果。"""

    stream_name: str
    name: str | None = None
    refdes: str | None = None
    unit: str = ""
    pin_count: int | None = None
    pin_map: list[str] = field(default_factory=list)
    pin_names: list[str] = field(default_factory=list)
    pcb_footprint: str | None = None


def _u16(data: bytes, off: int) -> int | None:
    return int.from_bytes(data[off:off + 2], "little") if off + 2 <= len(data) else None


def _lstr_at(data: bytes, off: int, limit: int) -> tuple[str | None, int]:
    """读 u16 len + bytes + 0x00；成功返回 (str, 下一偏移)。"""
    if off + 2 > limit:
        return None, off
    ln = int.from_bytes(data[off:off + 2], "little")
    if ln < 1 or ln > 128 or off + 2 + ln + 1 > limit:
        return None, off
    if data[off + 2 + ln] != 0:
        return None, off
    try:
        return data[off + 2:off + 2 + ln].decode("latin1"), off + 2 + ln + 1
    except Exception:
        return None, off


def _split_frames(data: bytes) -> list[int]:
    """按 8B 分隔符切帧，返回各帧起始偏移（含文件尾哨兵）。"""
    frames = [m.start() for m in _DELIM_RE.finditer(data)]
    frames.append(len(data))
    return frames


def _parse_pinmap_at(data: bytes, name_off: int, name: str, fend: int) -> tuple[int | None, list[str]]:
    """在帧内 ``name`` 串之后解析 pinCount + pinMap。

    Args:
        data: 包流。
        name_off: 包名字符串偏移（len16 前缀之后）。
        name: 包名。
        fend: 帧尾。

    Returns:
        (pinCount, pinMap)。失败返回 (None, [])。
    """
    p = name_off + len(name) + 1
    if p + 2 > fend:
        return None, []
    pc = int.from_bytes(data[p:p + 2], "little")
    if not (1 <= pc <= 1000):
        return None, []
    pins: list[str] = []
    q = p + 2
    for _ in range(pc):
        if q + 2 > fend:
            return None, []
        sl = int.from_bytes(data[q:q + 2], "little")
        if sl < 1 or sl > 8 or q + 2 + sl + 1 + 1 > fend:
            return None, []
        if data[q + 2 + sl] != 0:
            return None, []
        try:
            num = data[q + 2:q + 2 + sl].decode("latin1")
        except Exception:
            return None, []
        pins.append(num)
        q += 2 + sl + 1 + 1
    if len(pins) != pc:
        return None, []
    return pc, pins


def _candidate_names(data: bytes, stream_name: str) -> list[tuple[int, str]]:
    """候选包名列表：流名本身 + 多单元后缀（流名+字母）。

    返回 [(name_off, name)]，其中 name_off 指向 len16 前缀之后。
    """
    cands: list[tuple[int, str]] = []
    for m in _DELIM_RE.finditer(data):
        p = m.end()
        # 帧内第一个 lstr
        s, _ = _lstr_at(data, p, len(data))
        if s is None:
            continue
        if s == stream_name:
            cands.append((p + 2, s))
        elif s.startswith(stream_name) and len(s) == len(stream_name) + 1:
            suffix = s[len(stream_name):]
            if _UNIT_SUFFIX_RE.match(suffix):
                cands.append((p + 2, s))
    return cands


def _first_lstr(data: bytes, fo: int, fe: int) -> tuple[str | None, int]:
    """帧内第一个合法 len16 串（跳过 type u16 头；从 fo+8 逐字节扫描）。

    Returns:
        (串, 串内容之后偏移)；无则 (None, fo+8)。
    """
    j = fo + 8
    while j + 2 <= fe:
        ln = int.from_bytes(data[j:j + 2], "little")
        if 1 <= ln <= 128 and j + 2 + ln + 1 <= fe and data[j + 2 + ln] == 0:
            try:
                s = data[j + 2:j + 2 + ln].decode("latin1")
            except Exception:
                s = None
            if s is not None:
                return s, j + 2 + ln + 1
        j += 1
    return None, fo + 8


def _find_pinmap(data: bytes, stream_name: str) -> tuple[str | None, str | None, int | None, list[str], int]:
    """定位 pinMap：在所有候选包名帧中找可完整解析的最后一帧。

    两种帧结构：
    - 单单元：``[DELIM][type][len16][包名][00][pinCount u16][pinMap...]``
    - 多单元：``[DELIM][type][len16][单元字母][00][len16][包名][00][pinCount u16][pinMap...]``
      （如 Hi5662 frame 679：'F' + 'Hi5662YV305_DDR_GE_0F'）

    Returns:
        (包名, 单元, pinCount, pinMap, pinMap帧索引)。
    """
    frames = _split_frames(data)
    best: tuple[str, str, int, list[str], int] | None = None
    for fi in range(len(frames) - 1):
        fo, fe = frames[fi], frames[fi + 1]
        s, p2 = _first_lstr(data, fo, fe)
        if s is None:
            continue
        # 情形 A：帧内首串 = 包名（单单元）
        if s == stream_name:
            name_off = p2 - len(s) - 1
            pc, pins = _parse_pinmap_at(data, name_off, s, fe)
            if pc is not None and pins:
                best = (s, "", pc, pins, fi)
                continue
        # 情形 B：帧内首串 = 单元字母，次串 = 包名（多单元主单元）
        if (
            len(s) == 1
            and _UNIT_SUFFIX_RE.match(s)
            and p2 < fe
        ):
            # 从单元字母串尾（p2）开始读第二个 lstr = 包名
            s2, p3 = _lstr_at(data, p2, fe)
            if s2 is not None and (
                s2 == stream_name
                or (
                    s2.startswith(stream_name)
                    and len(s2) == len(stream_name) + 1
                    and _UNIT_SUFFIX_RE.match(s2[len(stream_name):])
                )
            ):
                # p2 指向第二个 lstr 的 len16 前缀；内容起始 = p2 + 2
                pc, pins = _parse_pinmap_at(data, p2 + 2, s2, fe)
                if pc is not None and pins:
                    unit = s2[len(stream_name):] if len(s2) > len(stream_name) else s
                    best = (s2, unit, pc, pins, fi)
    if best is None:
        return None, None, None, [], -1
    name, unit, pc, pins, fi = best
    return name, unit, pc, pins, fi


def _collect_pin_names(data: bytes, pinmap_frame_idx: int, frames: list[int], pkg_name: str, stream_name: str) -> list[str]:
    """收集 pinMap 帧之前所有单串帧的 len16 串（引脚名序列）。

    Args:
        data: 包流。
        pinmap_frame_idx: pinMap 所在帧索引。
        frames: 帧起始偏移列表。
        pkg_name: 主单元包名（如 Hi5662YV305_DDR_GE_0F）。
        stream_name: 流名（如 Hi5662YV305_DDR_GE_0）。

    Returns:
        引脚名列表（保序）。
    """
    # 多单元包（pkg_name 比流名多单元字母）才过滤单字母单元名
    is_multi_unit = len(pkg_name) > len(stream_name)
    names: list[str] = []
    for fi in range(pinmap_frame_idx):
        fo, fe = frames[fi], frames[fi + 1]
        # 帧类型 == 串长（经验规律：引脚名帧 type 恰为 len16 串长度）
        ftype = int.from_bytes(data[fo + 8:fo + 10], "little")
        if not (1 <= ftype <= 64):
            continue
        s, _ = _first_lstr(data, fo, fe)
        if s is None:
            continue
        # 过滤噪声
        if s.endswith(_NOISE_ENDS):
            continue
        if s == pkg_name or s == stream_name:
            continue
        # 单元名（多单元包：流名+单字母 或 流名_0+单字母）
        if s.startswith(stream_name) and len(s) == len(stream_name) + 1 and s[-1].isalpha():
            continue
        if s.startswith(stream_name + "_0") and len(s) == len(stream_name) + 2:
            continue
        if s in _NOISE_NAMES:
            continue
        # 单字母（多单元包的单元字母；单单元包保留如 NPN 的 B/E/C 引脚名）
        if len(s) == 1 and s.isalpha() and is_multi_unit:
            continue
        if " " in s:
            continue
        # 仅接受纯可打印 ASCII（防二进制误读）
        if not s.isprintable() or any(ord(c) < 32 or ord(c) > 126 for c in s):
            continue
        names.append(s)
    return names


def parse_package_stream(data: bytes, stream_name: str) -> PackageData:
    """解析单个 Packages/<流名> 流。

    Args:
        data: 包流原始字节。
        stream_name: 流名（如 ``2P5GEPHY_0``）。

    Returns:
        PackageData。
    """
    result = PackageData(stream_name=stream_name)

    # ── 1. pinMap ──────────────────────────────────────────────
    pkg_name, unit, pc, pins, pinmap_fi = _find_pinmap(data, stream_name)
    if pkg_name is None:
        logger.warning("Package %s: no pinMap frame found", stream_name)
        return result
    result.name = pkg_name
    result.refdes = pkg_name
    result.unit = unit or ""
    result.pin_count = pc
    result.pin_map = pins

    # ── 2. pinNames ────────────────────────────────────────────
    frames = _split_frames(data)
    if pinmap_fi != -1:
        names = _collect_pin_names(data, pinmap_fi, frames, pkg_name, stream_name)
        # 多单元包（Hi5662）：pinNames 为全部单元共 409 个；单单元包取 pinCount 个
        if len(names) >= pc:
            result.pin_names = names[:pc] if not result.unit else names
        else:
            result.pin_names = names
    else:
        result.pin_names = []

    # ── 3. pcbFootprint（尽力提取） ────────────────────────────
    # 注：Package 流内无 footprint 串（实证），保持 None；真实 footprint
    # 由 Cache/页面 0x02 帧提供。此处保留字段用于未来扩展。
    result.pcb_footprint = None

    logger.debug(
        "Package %s: name=%s pc=%s pins=%d names=%d",
        stream_name, pkg_name, pc, len(pins), len(result.pin_names),
    )
    return result


def parse_packages(package_streams: list[tuple[str, bytes]]) -> list[PackageData]:
    """解析全部 Packages/* 流。

    Args:
        package_streams: [(流名, bytes)]。

    Returns:
        PackageData 列表（11 包）。
    """
    result: list[PackageData] = []
    for stream_name, data in package_streams:
        pkg = parse_package_stream(data, stream_name)
        result.append(pkg)
    ok = sum(1 for p in result if p.pin_count is not None and p.pin_map)
    logger.info("Packages parsed: %d packages (%d with pinMap)", len(result), ok)
    return result
