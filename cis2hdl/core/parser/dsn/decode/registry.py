"""解码器注册表（架构设计 §2 L3）。

双注册表设计：
- ``SEMANTIC_FRAME_DECODERS``：frame_type(strLst 索引) → 语义帧解码器
- ``STRUCT_DECODERS``：结构类型(u8 枚举) → 结构帧解码器

分派策略：先查语义表 → 命中即语义解码；未命中但类型在标准结构枚举 →
结构解码；两者皆无 → UnknownFrame 记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol

from ..frame.splitter import Frame


@dataclass
class DecodeContext:
    """解码上下文（携带 strLst 与页面信息）。"""

    strlst: list[str] = field(default_factory=list)
    page_name: str = ""
    design_name: str = ""


@dataclass
class UnknownFrame:
    """无法解码的帧（诊断保留）。"""

    frame_type: int
    offset: int
    length: int


#: 语义帧解码器签名：Frame + DecodeContext → 记录对象（dataclass/dict）
FrameDecoder = Callable[[Frame, DecodeContext], object]
#: 结构帧解码器签名：Frame + DecodeContext → 记录对象
StructDecoder = Callable[[Frame, DecodeContext], object]

#: 表一：语义帧注册表（frame_type → 解码器）
SEMANTIC_FRAME_DECODERS: dict[int, FrameDecoder] = {}

#: 表二：结构类型注册表（structure_type → 解码器）
STRUCT_DECODERS: dict[int, StructDecoder] = {}


def register_semantic(frame_type: int, decoder: FrameDecoder) -> None:
    """注册语义帧解码器。

    Args:
        frame_type: 帧类型（strLst 索引语义）。
        decoder: 解码函数。
    """
    SEMANTIC_FRAME_DECODERS[frame_type] = decoder


def register_struct(struct_type: int, decoder: StructDecoder) -> None:
    """注册结构类型解码器。

    Args:
        struct_type: 结构类型（u8 枚举）。
        decoder: 解码函数。
    """
    STRUCT_DECODERS[struct_type] = decoder


def _content_probe_named_geometry(frame: Frame) -> object | None:
    """内容探测：命名几何帧（T04，帧覆盖率 63.9%→92.4% 突破口）。

    背景：页面流 3970 unknown 帧中 3154 帧为**同构命名几何帧**——
    ``[DELIM][type:u16][dbId][30 00 00 00][x:u32][y:u32][x2][y2]...``
    （marker30 @ offset 16），长度 60/70B 为主，部分含名称 lstr
    （'R.Normal' 等）。类型号 = strLst 索引（数据相关）逐个注册不现实，
    故在 dispatch 兜底做**内容探测**（marker30 + 合法坐标 + 长度范围）。

    T04-2 扩展：另有 796 帧为**名称 lstr @ offset 16** 的 GraphicInst
    几何帧（``[DELIM][type][dbId][len:u16][name][00][...坐标...]``，
    如 C.Normal/R.Normal/ONSHEET2/BISHEET），与 T01 shape 帧同构但
    名称位置不同 → 一并内容探测（坐标经名称后 i16/u16 扫描，合法域校验）。

    铁律（BISHEET 教训）：仅 marker30 或名称 + 坐标合法性双条件命中才
    接受，无名称不拒绝（几何帧本就无名称），但坐标必须落在页面 mil
    合法域。

    Returns:
        NamedGeometryRecord dict；不匹配返回 None。
    """
    rec = frame.raw
    if len(rec) < 24 or len(rec) > 200:
        return None
    # 形态一：marker30 @ offset 8/12/16（4×u32 坐标；T04-3 扩 8/12 位）
    for mpos in (16, 12, 8):
        if rec[mpos:mpos + 4] == b"\x30\x00\x00\x00":
            try:
                vals = [
                    int.from_bytes(rec[mpos + 4 + k * 4:mpos + 8 + k * 4], "little")
                    for k in range(4)
                ]
            except Exception:
                return None
            if any(v > 40000 for v in vals):
                continue
            return {
                "kind": "named_geometry",
                "frame_type": frame.frame_type,
                "offset": frame.offset,
                "x": vals[0], "y": vals[1], "x2": vals[2], "y2": vals[3],
                "length": len(rec),
            }
    # 形态二：名称 lstr @ offset 16（[len:u16][name][00] + 坐标）
    ln = int.from_bytes(rec[16:18], "little")
    if not (1 <= ln <= 24) or 18 + ln + 1 > len(rec):
        return None
    name_b = rec[18:18 + ln]
    if not all(32 <= b < 127 for b in name_b):
        return None
    if rec[18 + ln:18 + ln + 1] != b"\x00":
        return None
    # 名称后扫描坐标（i16 对，best-effort；至少 2 对合法）
    pts: list[tuple[int, int]] = []
    start = 18 + ln + 1
    for off in range(start, max(start, len(rec) - 3)):
        x = int.from_bytes(rec[off:off + 2], "little", signed=True)
        y = int.from_bytes(rec[off + 2:off + 4], "little", signed=True)
        if 0 < x < 40000 and 0 < y < 40000:
            pts.append((x, y))
            if len(pts) >= 4:
                break
    if len(pts) < 2:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {
        "kind": "named_geometry",
        "frame_type": frame.frame_type,
        "offset": frame.offset,
        "name": name_b.decode("latin1"),
        "x": min(xs), "y": min(ys), "x2": max(xs), "y2": max(ys),
        "length": len(rec),
    }


def dispatch(frame: Frame, ctx: DecodeContext) -> object:
    """按帧类型分派解码。

    Args:
        frame: 输入帧。
        ctx: 解码上下文。

    Returns:
        解码记录；无法识别时返回 UnknownFrame。
    """
    if frame.frame_type in SEMANTIC_FRAME_DECODERS:
        return SEMANTIC_FRAME_DECODERS[frame.frame_type](frame, ctx)
    if frame.frame_type in STRUCT_DECODERS:
        return STRUCT_DECODERS[frame.frame_type](frame, ctx)
    # T04：内容探测兜底（命名几何帧族，不依赖类型号）
    probed = _content_probe_named_geometry(frame)
    if probed is not None:
        return probed
    return UnknownFrame(
        frame_type=frame.frame_type,
        offset=frame.offset,
        length=len(frame.raw),
    )


def clear_registries() -> None:
    """清空两个注册表（测试用）。"""
    SEMANTIC_FRAME_DECODERS.clear()
    STRUCT_DECODERS.clear()
