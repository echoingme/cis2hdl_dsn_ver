"""SymbolPinScalar(26)/SymbolPinBus(27) 解码器（P1-1/P1-2）。

双上下文语义（实证）：
- **页面级**：26/27 帧为 45B 短引用
  ``[8B DELIM][type:u16][x:u16][y:u16][dbId:u32][trailer]``，
  表示一个引脚点引用（引脚坐标 + 对象 id）。TG0C0D2 共 9 页各 1×26 + 1×27
  （06/07/08/10/11/12/13/16/18），每帧 45B。
- **Cache 级**：type-4 帧为完整几何（见 ``cache/pin_geometry_parser.py``），
  含引脚名、3 坐标（startX/startY/hotptX）、pinShape、portType。

portType → ElectricalType 映射（P1-2）：
    替换硬编码 PASSIVE。OrCAD SymbolPin portType 语义（OpenOrCadParser）：
    1=INPUT、2=OUTPUT、3=BIDIR、4=POWER、5=GROUND、6=PASSIVE、7=NC。
    TG0C0D2 Cache 实测 portType 恒 1（INPUT）——诚实标注：U7 六段
    0A~0F 几何均为输入引脚；P1 用合成样本验证位域（见测试）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..frame.splitter import Frame
from .registry import DecodeContext

if TYPE_CHECKING:
    from ....ir.component import ElectricalType

logger = logging.getLogger(__name__)

#: 页面级 26/27 短引用最大长度（45B 实测 + 容差）
_PIN_REF_MAX_LEN = 60
#: 坐标上限（mil 页面坐标）
_MAX_COORD = 30000

#: 页面级引脚引用帧类型
PIN_REF_FRAME_TYPES = frozenset((26, 27))

#: portType → ElectricalType（P1-2；OrCAD SymbolPin 语义）。
#: 值引用 IR 枚举字符串，避免运行时导入 IR（零循环依赖）。
PORT_TYPE_TO_ELECTRICAL: dict[int, str] = {
    0: "PASSIVE",
    1: "INPUT",
    2: "OUTPUT",
    3: "BIDIR",
    4: "POWER",
    5: "GROUND",
    6: "PASSIVE",
    7: "NC",
}


@dataclass
class PinRefRecord:
    """页面级 26/27 短引用（引脚点引用）。

    Attributes:
        offset: 帧页内偏移。
        frame_type: 26=SymbolPinScalar、27=SymbolPinBus。
        x, y: 引脚坐标（mil）。
        object_id: 对象 id（dbId，关联 Cache 几何）。
        pin_name: 引脚名（经 object_id → Cache 几何关联后回填，默认空）。
        electrical_type: 电气类型（P1-2 映射；默认 PASSIVE）。
    """

    offset: int
    frame_type: int
    x: int
    y: int
    object_id: int
    pin_name: str = ""
    electrical_type: str = "PASSIVE"

    def to_dict(self) -> dict:
        return {
            "offset": self.offset,
            "frame_type": f"0x{self.frame_type:04x}",
            "x": self.x,
            "y": self.y,
            "object_id": self.object_id,
            "pin_name": self.pin_name,
            "electrical_type": self.electrical_type,
        }


def parse_symbol_pin_ref(frame: Frame, ctx: DecodeContext) -> PinRefRecord | None:
    """解析页面级 26/27 短引用帧。

    Args:
        frame: 输入帧（frame_type ∈ {26, 27}）。
        ctx: 解码上下文（未使用，保留签名一致性）。

    Returns:
        PinRefRecord；非引脚引用帧或坐标非法返回 None。
    """
    if frame.frame_type not in PIN_REF_FRAME_TYPES:
        return None
    rec = frame.raw
    if len(rec) > _PIN_REF_MAX_LEN:
        return None
    if len(rec) < 18:
        return None
    x = int.from_bytes(rec[10:12], "little")
    y = int.from_bytes(rec[12:14], "little")
    object_id = int.from_bytes(rec[14:18], "little")
    if not (0 < x < _MAX_COORD and 0 < y < _MAX_COORD):
        return None
    return PinRefRecord(
        offset=frame.offset,
        frame_type=frame.frame_type,
        x=x,
        y=y,
        object_id=object_id,
    )


def scan_symbol_pin_refs(frames: list[Frame], ctx: DecodeContext) -> list[PinRefRecord]:
    """扫描一页全部 26/27 引脚引用。

    Args:
        frames: 页面帧列表。
        ctx: 解码上下文。

    Returns:
        PinRefRecord 列表（按帧偏移升序）。
    """
    result: list[PinRefRecord] = []
    for frame in frames:
        rec = parse_symbol_pin_ref(frame, ctx)
        if rec is not None:
            result.append(rec)
    return result


def port_type_to_electrical(port_type: int | None) -> str:
    """portType → ElectricalType 字符串（P1-2）。

    Args:
        port_type: Cache type-4 几何的 portType 字段。

    Returns:
        ElectricalType 字符串；未知值返回 PASSIVE（防御不抛异常）。
    """
    if port_type is None:
        return "PASSIVE"
    return PORT_TYPE_TO_ELECTRICAL.get(port_type, "PASSIVE")


#: 电源引脚名特征（P2-D 回退链）
_POWER_PIN_RE = re.compile(
    r"^(VCC|VDD|VSS|AVCC|AVDD|DVDD|PVDD|VBAT|VIN|VPH|VBUS|5V|3V3|1V8|1V2|0V9|12V0?)\d*$",
    re.IGNORECASE,
)
#: 接地引脚名特征（P2-D 回退链）
_GND_PIN_RE = re.compile(r"^(GND|AGND|DGND|PGND|GNDA|GNDD|VSS|VSSA|VSSD)\d*$", re.IGNORECASE)


def infer_pin_type_from_name(pin_name: str) -> str | None:
    """按引脚名推断电气类型（P2-D 回退链；Cache 几何缺失时）。

    仅对**语义明确**的电源/接地引脚推断（POWER/GROUND），其余返回
    None（保持 PASSIVE 诚实标注，不硬编码）。

    Args:
        pin_name: 引脚名（如 VCC1 / GND / DVDD09_1）。

    Returns:
        ElectricalType 字符串（POWER/GROUND）；无法推断返回 None。
    """
    if not pin_name:
        return None
    up = pin_name.strip().upper()
    if _GND_PIN_RE.match(up):
        return "GROUND"
    if _POWER_PIN_RE.match(up):
        return "POWER"
    # 含电源/接地子串（如 DVDD09_1、VSSA、AVDD33）
    if up.startswith(("VCC", "VDD", "AVDD", "DVDD", "PVDD", "VIN", "VBAT")):
        return "POWER"
    if up.startswith(("GND", "VSS")):
        return "GROUND"
    return None


def register_symbol_pin_decoders() -> None:
    """注册 26/27 结构帧解码器。"""
    from .registry import register_struct

    register_struct(26, parse_symbol_pin_ref)
    register_struct(27, parse_symbol_pin_ref)
