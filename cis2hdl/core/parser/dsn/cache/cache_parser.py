"""Cache 流解析器（架构设计 §2 cache）。

实证（QA B.2）：Cache 流含 44 个 LibraryPart：
``[DELIM][len16][name.Normal][00][len16][sourceLib][00]...``
每个 part 区域尾部含 StructGeneralProperties：
``implementationPath | implementation | refDes | partValue | ...``
- ``refDes`` = 位号前缀（R/C/U…）
- ``partValue`` = 库默认 Value（仅在无实例覆盖时兜底）

与实证脚本 ``cache_general_props2.py`` 保持一致。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from ..frame.types import DELIMITER
from .pin_labels_parser import PinLabelMap, scan_pin_label_maps

logger = logging.getLogger(__name__)

_DELIM_RE = re.compile(re.escape(DELIMITER))
_REFDES_PREFIX_RE = re.compile(r"^[A-Za-z]{1,3}$")


@dataclass
class LibraryPart:
    """Cache 中的一个 LibraryPart。"""

    name: str
    source_library: str
    preamble: int
    ref_des: str | None = None
    part_value: str | None = None
    implementation_path: str = ""
    implementation: str = ""


@dataclass
class CacheData:
    """Cache 流解析结果。"""

    parts: list[LibraryPart] = field(default_factory=list)
    #: P0-3：引脚标号映射（U7 六段 0A~0F）
    pin_label_maps: list[PinLabelMap] = field(default_factory=list)
    #: P1-1：SymbolPin type-4 几何（80 个有效）
    pin_geometries: list = field(default_factory=list)
    #: P1-12：符号文本/头属性（Cache 0x2e2e 长帧 + 0x001c 子块）
    symbol_texts: list = field(default_factory=list)
    #: T04：命名引脚定义（914+，含 3A BUCK/LDO_1/PSW6M2 等小 IC 引脚名+几何）
    pin_defs: list = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.parts)

    def by_shape(self, shape: str) -> LibraryPart | None:
        """按外形名精确匹配。"""
        for p in self.parts:
            if p.name == shape:
                return p
        return None

    def by_base_name(self, base: str) -> LibraryPart | None:
        """按基础名（去 .Normal）匹配。"""
        for p in self.parts:
            if p.name.rsplit(".", 1)[0] == base:
                return p
        return None


def _read_lstr(data: bytes, off: int) -> tuple[str | None, int]:
    """u16 len + bytes + 0x00。"""
    if off + 2 > len(data):
        return None, off
    l = int.from_bytes(data[off:off + 2], "little")
    if l == 0xFFFF or l > 500:
        return None, off
    if off + 2 + l + 1 > len(data):
        return None, off
    if data[off + 2 + l] != 0:
        return None, off
    try:
        return data[off + 2:off + 2 + l].decode("latin1"), off + 2 + l + 1
    except Exception:
        return None, off


def find_library_parts(data: bytes) -> list[dict]:
    """扫描 Cache 流找 LibraryPart 起点（name.Normal + sourceLib）。"""
    parts: list[dict] = []
    for m in _DELIM_RE.finditer(data):
        p = m.end()
        name, p2 = _read_lstr(data, p)
        if name is None or not (name.endswith(".Normal") or name.endswith(".Convert")):
            continue
        src, p3 = _read_lstr(data, p2)
        if src is None:
            continue
        if not (src.endswith(".OLB") or src.endswith(".DSN") or "\\" in src or "/" in src):
            continue
        parts.append({
            "name": name, "source_library": src,
            "preamble": m.start(), "after_src": p3,
        })
    return parts


def find_general_props(region: bytes, base_off: int) -> dict | None:
    """在 region 内扫描 4 连续 lstr：3rd=refDes(^[A-Za-z]{1,3}$), 4th=partValue。"""
    best: dict | None = None
    for p in range(0, len(region) - 8):
        s1, p1 = _read_lstr(region, p)
        if s1 is None:
            continue
        s2, p2 = _read_lstr(region, p1)
        if s2 is None:
            continue
        s3, p3 = _read_lstr(region, p2)
        if s3 is None:
            continue
        s4, p4 = _read_lstr(region, p3)
        if s4 is None:
            continue
        if _REFDES_PREFIX_RE.match(s3) and len(s4) <= 40 and "\\" not in s4 and "/" not in s4:
            score = 0
            if s1 == "":
                score += 1
            if s2 == "":
                score += 1
            if s4 == "":
                score += 0.5
            if re.match(r"^[0-9]", s4) or s4 in ("LED", "LED*"):
                score += 2
            if s4 and not s4.startswith("$") and not s4.startswith("INS"):
                score += 1
            cand = {
                "offset": base_off + p,
                "implementation_path": s1,
                "implementation": s2,
                "ref_des": s3,
                "part_value": s4,
                "score": score,
            }
            if best is None or score > best["score"]:
                best = cand
    return best


def parse_cache_stream(data: bytes) -> CacheData:
    """解析 Cache 流。

    Args:
        data: Cache 流原始字节。

    Returns:
        CacheData（44 个 LibraryPart）。
    """
    parts_raw = find_library_parts(data)
    result = CacheData()
    for i, pt in enumerate(parts_raw):
        start = pt["after_src"]
        if i + 1 < len(parts_raw):
            end = parts_raw[i + 1]["preamble"]
        else:
            end = len(data)
        region = data[start:end]
        gp = find_general_props(region, start)
        part = LibraryPart(
            name=pt["name"],
            source_library=pt["source_library"],
            preamble=pt["preamble"],
            ref_des=gp["ref_des"] if gp else None,
            part_value=gp["part_value"] if gp else None,
            implementation_path=gp["implementation_path"] if gp else "",
            implementation=gp["implementation"] if gp else "",
        )
        result.parts.append(part)
    # P0-3：引脚标号映射（防御：失败仅 warn，不中断）
    try:
        result.pin_label_maps = scan_pin_label_maps(data)
    except Exception as exc:
        logger.warning("Pin label map scan failed (continue): %s", exc)
    # P1-1：SymbolPin type-4 几何（防御：失败仅 warn，不中断）
    try:
        from .pin_geometry_parser import scan_cache_pin_geometries

        result.pin_geometries = scan_cache_pin_geometries(data)
    except Exception as exc:
        logger.warning("Pin geometry scan failed (continue): %s", exc)
    # P1-12：符号文本/头属性（防御：失败仅 warn，不中断）
    try:
        from ..decode.symbol_geometry import scan_symbol_texts

        result.symbol_texts = scan_symbol_texts(data)
    except Exception as exc:
        logger.warning("Symbol text scan failed (continue): %s", exc)
    # T04：命名引脚定义（Cache [DELIM][len][name][00][坐标] 帧族，
    # 覆盖 Packages 流缺失的小 IC 引脚——3A BUCK/LDO_1/PSW6M2 等）
    try:
        from .cache_pin_defs import scan_cache_pin_defs

        result.pin_defs = scan_cache_pin_defs(data)
    except Exception as exc:
        logger.warning("Cache pin defs scan failed (continue): %s", exc)
    logger.info(
        "Cache parsed: %d LibraryPart, %d pin label maps, %d pin geometries, "
        "%d symbol texts, %d pin defs",
        result.count, len(result.pin_label_maps), len(result.pin_geometries),
        len(result.symbol_texts), len(result.pin_defs),
    )
    return result
