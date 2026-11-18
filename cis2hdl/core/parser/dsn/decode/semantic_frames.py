"""语义帧解码器（架构设计 §2 L3 + §5.1/5.2）。

实证帧布局：
- ``0x85`` 位号帧（strLst[133]='VALUE' 属性归属）：
  ``[8B DELIM][type:u16][00 00][len32][04 00 30|04 40 30][x:u16][rot:u8][font:u8][len16][refdes][00][u32 valueIdx]``
  valueIdx = 该实例真实 Value 的 strLst 索引（QA 证据 1，643/643 可读）。
- ``0x02`` 属性帧：内含 ``[0x0d][count][nameIdx:u32][valueIdx:u32]`` 属性对，
  ``nameIdx==25('PCB Footprint') → valueIdx → strLst``（QA 证据 2，665 对）。
- ``0x01`` GND 帧：引脚/连接引用帧（strLst[1]='GND'）。
- ``0x22`` Part Reference 帧：位号显示帧（隐藏 VALUE 实例的位号来源）。
- ``0x083f`` Name / ``0x0842`` Number：名称/编号文本帧。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..frame.splitter import Frame
from .registry import DecodeContext

#: PCB Footprint 属性名索引（strLst[25]）
IDX_PCB_FOOTPRINT = 25
#: PKG_GRP 属性名索引（strLst[29]，U7 的 BGA409-26-2323 在此属性）
IDX_PKG_GRP = 29
#: Value 属性名索引（strLst[632]）
IDX_VALUE = 632

#: 0x85/0x22 帧内坐标标记（实际字节 0x03/0x04 变体）
_MARKERS = (b"\x04\x00\x30", b"\x04\x40\x30", b"\x03\x00\x30", b"\x03\x40\x30")

#: 位号帧允许类型（0x85=133 'VALUE' 主；0x22=34 'Part Reference' 次；
#: 0x0278=632 'Value'、0x0005=5 'DEVICE'、0x0001=1 'GND' 为实例显示帧补充）
REFDES_FRAME_TYPES = frozenset(
    (0x85, 0x22, 0x0278, 0x0005, 0x0001)
)

#: 位号形态（R83 / C66 / U12 …）
_REFDES_RE = re.compile(r"^[A-Za-z]{1,3}[0-9]+$")


@dataclass
class RefdesFrame:
    """0x85/0x22 位号帧解码结果。"""

    offset: int
    refdes: str
    x: int | None
    value_idx: int | None
    value: str | None
    frame_type: int
    #: 旋转角度（P1-4；0x85 marker 后 rot 字节）
    rot: int = 0
    #: 位号字号（P1-5；0x85 marker 后 font 字节）
    font: int = 0
    #: 镜像标志（P1-4；0=无、1=X 镜像、2=Y 镜像）
    mirror: int = 0
    #: 旋转方向标志（08-20：marker `04 00 30` vs `04 40 30`——
    #: 0x40 位区分 R90/R270，驱动 cache 引脚线旋转方向）
    rot_marker40: bool = False
    #: 位号文本颜色（P1-6；attr 高位字节 → 颜色名）
    color: str | None = None


@dataclass
class PropertyFrame:
    """0x02 属性帧解码结果。"""

    offset: int
    props: dict[str, str] = field(default_factory=dict)
    footprint: str | None = None
    footprint_idx: int | None = None
    #: PKG_GRP 值（strLst[29]，如 'BGA409-26-2323'；作为 footprint 候选）
    pkg_grp: str | None = None
    pkg_grp_idx: int | None = None
    prop_pairs: list[tuple[int, int]] = field(default_factory=list)


@dataclass
class GndFrame:
    """0x01 GND/引脚引用帧。"""

    offset: int
    refs: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class TextFrame:
    """0x083f Name / 0x0842 Number 文本帧。"""

    offset: int
    text: str | None
    value_idx: int | None
    value: str | None


def _u16(data: bytes, off: int) -> int | None:
    return int.from_bytes(data[off:off + 2], "little") if off + 2 <= len(data) else None


def _u32(data: bytes, off: int) -> int | None:
    return int.from_bytes(data[off:off + 4], "little") if off + 4 <= len(data) else None


def _is_printable_ascii(data: bytes) -> bool:
    return all(32 <= b < 127 for b in data)


def _filter_value(value: str | None) -> str | None:
    """过滤路径/超大串等噪声 value。"""
    if not value:
        return None
    if "\\" in value or "/" in value or len(value) > 40:
        return None
    return value


def parse_refdes_frame(frame: Frame, ctx: DecodeContext) -> RefdesFrame | None:
    """解析 0x85/0x22 位号帧。

    Args:
        frame: 输入帧。
        ctx: 解码上下文。

    Returns:
        RefdesFrame；无效帧返回 None（不抛异常）。
    """
    rec = frame.raw
    if frame.frame_type not in REFDES_FRAME_TYPES:
        return None
    if len(rec) < 30:
        return None

    # ── 方案 1：坐标标记定位（04 00 30 / 04 40 30 变体） ──
    idx = -1
    marker40 = False
    for marker in _MARKERS:
        i = rec.find(marker, 10)
        if i != -1:
            idx = i
            # marker 第 2 字节 0x40 = 旋转方向标志（08-20 攻坚实证：
            # C188 旋转用 `04 00 30`、RD8/RD5 用 `04 40 30`——方向相反）
            marker40 = marker[1] == 0x40
            break
    if idx != -1:
        x = _u16(rec, idx + 3)
        # P1-4/P1-5：marker 后 [x u16][rot u8][font u8][len16]
        rot = rec[idx + 5] if idx + 5 < len(rec) else 0
        font = rec[idx + 6] if idx + 6 < len(rec) else 0
        tlen = _u16(rec, idx + 7)
        if tlen is not None and 1 <= tlen <= 30:
            toff = idx + 9
            if toff + tlen + 1 <= len(rec):
                text = rec[toff:toff + tlen]
                if rec[toff + tlen] == 0 and _is_printable_ascii(text):
                    refdes = text.decode("latin1")
                    if _REFDES_RE.match(refdes):
                        vidx = _u32(rec, toff + tlen + 1)
                        value = (
                            ctx.strlst[vidx]
                            if vidx is not None and vidx < len(ctx.strlst)
                            else None
                        )
                        value = _filter_value(value)
                        if value is None:
                            vidx = None
                        return RefdesFrame(
                            offset=frame.offset,
                            refdes=refdes,
                            x=x,
                            value_idx=vidx,
                            value=value,
                            frame_type=frame.frame_type,
                            rot=rot,
                            font=font,
                            rot_marker40=marker40,
                        )

    # ── 方案 2（QA 精确法）：扫描 0x18 前缀 + len16 文本 ──
    for off in range(10, len(rec) - 4):
        if rec[off] != 0x18:
            continue
        l = int.from_bytes(rec[off + 1:off + 3], "little")
        if l < 1 or l > 30 or off + 3 + l + 1 > len(rec):
            continue
        if rec[off + 3 + l] != 0:
            continue
        txt = rec[off + 3:off + 3 + l]
        if not _is_printable_ascii(txt):
            continue
        refdes = txt.decode("latin1")
        if not _REFDES_RE.match(refdes):
            continue
        # x 坐标：0x18 前缀前的 u16（若存在）
        x = _u16(rec, off - 2)
        vidx = _u32(rec, off + 3 + l + 1)
        value = ctx.strlst[vidx] if vidx is not None and vidx < len(ctx.strlst) else None
        value = _filter_value(value)
        if value is None:
            vidx = None
        return RefdesFrame(
            offset=frame.offset,
            refdes=refdes,
            x=x,
            value_idx=vidx,
            value=value,
            frame_type=frame.frame_type,
        )
    return None


#: 属性帧 0x0d 标记最小扫描起点（跳过帧头，避开分隔符/类型区）
_PROP_MARK_SCAN_START = 10
#: 属性对最大数量（防御上限，超出视为非法候选）
_PROP_MAX_COUNT = 64


def _is_valid_prop_marker(
    rec: bytes, j: int, strlst_len: int,
) -> int | None:
    """判断 ``rec[j] == 0x0D`` 是否为真正的属性对计数标记。

    属性对编码（QA 实证）：``[0x0d][count:u16][nameIdx:u32][valueIdx:u32]*count``。
    帧头部可能出现假 0x0d（如 R83 帧 offset 30/39/48/57，count 读出
    340/325/204/67 明显超界或配对越界），必须通过以下校验过滤：

    1. count ∈ [1, 64]；
    2. 8*count 对全部落在帧内；
    3. 每个 nameIdx/valueIdx 都是合法 strLst 下标。

    Args:
        rec: 帧原始字节。
        j: 0x0d 位置。
        strlst_len: strLst 长度（用于下标合法性校验）。

    Returns:
        合法则返回 count，否则 None。
    """
    if rec[j] != 0x0D:
        return None
    count = int.from_bytes(rec[j + 1:j + 3], "little")
    if not (1 <= count <= _PROP_MAX_COUNT):
        return None
    if j + 3 + 8 * count > len(rec):
        return None
    for n in range(count):
        k = j + 3 + 8 * n
        name_idx = int.from_bytes(rec[k:k + 4], "little")
        val_idx = int.from_bytes(rec[k + 4:k + 8], "little")
        if name_idx >= strlst_len or val_idx >= strlst_len:
            return None
    return count


def _find_prop_marker(rec: bytes, strlst_len: int) -> tuple[int, int] | None:
    """定位 0x02 属性帧真正的 0x0d 标记。

    策略：扫描全部合法候选，取 count 最大者（真正的属性块通常含最多属性对；
    QA 统计 643/643 帧 max-count 候选唯一）。若并列则取帧内最靠后者
    （属性块常位于帧尾）。

    Args:
        rec: 帧原始字节。
        strlst_len: strLst 长度。

    Returns:
        (marker_j, count)；无合法候选返回 None。
    """
    candidates: list[tuple[int, int]] = []
    for j in range(_PROP_MARK_SCAN_START, len(rec) - 3):
        count = _is_valid_prop_marker(rec, j, strlst_len)
        if count is not None:
            candidates.append((j, count))
    if not candidates:
        return None
    max_count = max(c for _j, c in candidates)
    best = [item for item in candidates if item[1] == max_count]
    # 并列时取帧内最靠后的候选（属性块通常在帧尾）
    best.sort(key=lambda item: item[0])
    return best[-1]


def parse_property_frame(frame: Frame, ctx: DecodeContext) -> PropertyFrame:
    """解析 0x02 属性帧。

    属性对编码（QA 实证）：``[0x0d][count:u16][nameIdx:u32][valueIdx:u32]*count``。
    帧头部存在假 0x0d（计数虚高导致 8 字节错位），本函数先定位真正的
    0x0d 标记（max-valid-count 规则），再顺序读取属性对。

    R83（offset 25605）验收：首对 ``(25, 161)`` → 'PCB Footprint'='HSC0201-HDTA'，
    末对 ``(5, 6)`` → 'DEVICE'='-'。
    """
    rec = frame.raw
    props: dict[str, str] = {}
    footprint: str | None = None
    footprint_idx: int | None = None
    pkg_grp: str | None = None
    pkg_grp_idx: int | None = None
    prop_pairs: list[tuple[int, int]] = []
    strlst_len = len(ctx.strlst)

    # 1) 定位真正的 0x0d 标记（max-valid-count 规则）
    marker = _find_prop_marker(rec, strlst_len)
    if marker is not None:
        j, count = marker
        k = j + 3
        for _ in range(count):
            if k + 8 > len(rec):
                break
            name_idx = _u32(rec, k)
            val_idx = _u32(rec, k + 4)
            if (
                name_idx is not None
                and val_idx is not None
                and name_idx < strlst_len
                and val_idx < strlst_len
            ):
                name = ctx.strlst[name_idx]
                val = ctx.strlst[val_idx]
                prop_pairs.append((name_idx, val_idx))
                if name and val:
                    props[name] = val
                if name_idx == IDX_PCB_FOOTPRINT:
                    clean = _filter_value(val)
                    if clean is not None:
                        footprint = clean
                        footprint_idx = val_idx
                elif name_idx == IDX_PKG_GRP:
                    # PKG_GRP（如 'BGA409-26-2323'）作为 footprint 候选
                    clean = _filter_value(val)
                    if clean is not None:
                        pkg_grp = clean
                        pkg_grp_idx = val_idx
                        if footprint is None:
                            footprint = clean
                            footprint_idx = val_idx
            k += 8

    # 2) 兜底：扫描所有 u32==25 位置（QA qa_fp_verify 精确方法）
    #    注意：帧头部可能有假 25（u16 拼出的 u32），必须继续扫到真值。
    if footprint is None:
        for j in range(10, len(rec) - 7):
            if _u32(rec, j) == IDX_PCB_FOOTPRINT:
                val_idx = _u32(rec, j + 4)
                if val_idx is not None and val_idx < strlst_len:
                    clean = _filter_value(ctx.strlst[val_idx])
                    if clean is not None:
                        footprint = clean
                        footprint_idx = val_idx
                        break

    return PropertyFrame(
        offset=frame.offset,
        props=props,
        footprint=footprint,
        footprint_idx=footprint_idx,
        pkg_grp=pkg_grp,
        pkg_grp_idx=pkg_grp_idx,
        prop_pairs=prop_pairs,
    )


def parse_gnd_frame(frame: Frame, ctx: DecodeContext) -> GndFrame:
    """解析 0x01 GND 帧（引脚/连接引用）。"""
    rec = frame.raw
    refs: list[tuple[int, str]] = []
    for j in range(10, len(rec) - 4):
        if rec[j] == 0x0D:
            v = _u32(rec, j + 1)
            if v is not None and v < len(ctx.strlst) and ctx.strlst[v]:
                refs.append((v, ctx.strlst[v]))
    return GndFrame(offset=frame.offset, refs=refs)


def parse_text_frame(frame: Frame, ctx: DecodeContext) -> TextFrame:
    """解析 0x083f Name / 0x0842 Number 文本帧。"""
    rec = frame.raw
    text: str | None = None
    for off in range(10, len(rec) - 4):
        if rec[off] != 0x18:
            continue
        l = int.from_bytes(rec[off + 1:off + 3], "little")
        if l < 1 or l > 40 or off + 3 + l + 1 > len(rec):
            continue
        if rec[off + 3 + l] != 0:
            continue
        txt = rec[off + 3:off + 3 + l]
        if _is_printable_ascii(txt):
            text = txt.decode("latin1")
            vidx = _u32(rec, off + 3 + l + 1)
            value = ctx.strlst[vidx] if vidx is not None and vidx < len(ctx.strlst) else None
            return TextFrame(
                offset=frame.offset, text=text, value_idx=vidx, value=value,
            )
    return TextFrame(offset=frame.offset, text=None, value_idx=None, value=None)


def register_semantic_decoders() -> None:
    """注册全部语义帧解码器。"""
    from .registry import register_semantic

    register_semantic(0x85, parse_refdes_frame)
    register_semantic(0x22, parse_refdes_frame)
    register_semantic(0x02, parse_property_frame)
    register_semantic(0x01, parse_gnd_frame)
    register_semantic(0x083F, parse_text_frame)
    register_semantic(0x0842, parse_text_frame)
