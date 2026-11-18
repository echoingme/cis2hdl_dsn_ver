"""Library 流用户属性表解析（P1-11）。

DSN ``Library`` 流在偏移 0x64f 起是一张连续字符串表
``[len:u16][字符串][00]*N``。实测 2169 条 lstr（文档称 2880，
可能为去重口径差异）。关键属性名（实测偏移）：
- ``描述`` @0x8af
- ``COMPONENT_HIGH_MAX`` @0xc62（后接 '0.4mm'）
- ``额定功率(W)`` @0x13c2（后接 '0.06'）
- ``阻值允许偏差`` @0x13d7（后接 '+/-1%'）
- ``额定工作电压(V)`` @0x164a（后接 '6.3'）

INS 编号：实测 743 个（INS5/INS1608/INS1620...），与 EXP 元件 743 对齐。

输出 ``ComponentUserAttr``：按 INS 编号（或库元件名）聚合用户属性：
comp_high_max / rated_voltage / rated_power / description /
resistance_tolerance。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

#: Library 流字符串表起始偏移（实测 0x64f）
TABLE_START = 0x64F
#: 单条 lstr 最大长度
_MAX_LEN = 200
#: 解析上限（防御）
_MAX_ITEMS = 5000
#: INS 行后续字段最大收集条数（关联 ComponentDef 用）
_ROW_FIELD_MAX = 24

#: 关键属性名（按出现顺序；对应 strLst 索引）
_ATTR_NAMES = (
    "描述",
    "COMPONENT_HIGH_MAX",
    "额定功率(W)",
    "阻值允许偏差",
    "额定工作电压(V)",
    # R-MFG：产地（MFGREGION 键后跟 GBK 值如 '日本;台湾'/'中国'）
    "MFGREGION",
)

#: INS 编号前缀（实测 'INS5' 等）
_INS_RE = re.compile(r"^INS\d+$")

#: R-CAT：器件分类名集合（EXP CATEGORY_NAME 19 类，全部在 DSN 字符串表；
#: 用于从 INS 行字段识别 CATEGORY_NAME 值——DSN 无 CATEGORY_NAME 键前缀，
#: 分类名直接出现在 INS 行 lstr 字段中）
CATEGORY_NAMES = frozenset({
    "片式厚膜电阻器", "低压低容陶瓷电容(<=100V,<1uf)",
    "高Q射频陶瓷电容", "大容量陶瓷电容(>=1uf)",
    "大容量陶瓷电容(&gt;=1uf)", "SMD陶瓷电容", "片式电感",
    "贴片电感器", "高频电感器", "功率电感", "终端功率电感", "EMI磁珠",
    "终端EMI磁珠", "EMI共模电感", "发光二极管", "开关二极管",
    "快恢复二极管", "瞬态抑制二极管", "三极管", "机械器件",
    "IO连接器", "DC座连接器", "光模块组件", "晶体谐振器",
    "DDR3 DRAM", "SLC NAND FLASH", "功率MOSFET",
})


@dataclass
class ComponentUserAttr:
    """一个 INS 元件的用户属性。

    Attributes:
        ins_id: INS 编号（如 'INS5'）。
        name: 元件名（如 '17N16918'，尽力关联）。
        comp_high_max: COMPONENT_HIGH_MAX 值（如 '0.4mm'）。
        rated_voltage: 额定工作电压(V)（如 '6.3'）。
        rated_power: 额定功率(W)（如 '0.06'）。
        description: 描述。
        resistance_tolerance: 阻值允许偏差（如 '+/-1%'）。
        attrs: 全部属性键值（保序 dict）。
    """

    ins_id: str
    name: str = ""
    comp_high_max: str | None = None
    rated_voltage: str | None = None
    rated_power: str | None = None
    description: str | None = None
    resistance_tolerance: str | None = None
    #: R-MFG：产地（如 '日本;台湾'/'中国'；MFGREGION 键后值）
    mfg_region: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    #: R-CAT：器件分类名（如 '片式厚膜电阻器'；INS 行字段识别）
    category: str | None = None
    #: INS 行后续 lstr 字段（≤24 条；用于关联 ComponentDef）
    row_fields: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ins_id": self.ins_id,
            "name": self.name,
            "comp_high_max": self.comp_high_max,
            "rated_voltage": self.rated_voltage,
            "rated_power": self.rated_power,
            "description": self.description,
            "resistance_tolerance": self.resistance_tolerance,
            "mfg_region": self.mfg_region,
            "category": self.category,
            "attrs": dict(self.attrs),
            "row_fields": list(self.row_fields),
        }


def _read_lstr(data: bytes, off: int, limit: int) -> tuple[str | None, int]:
    """读 u16 len + bytes + 0x00（支持 GBK 中文属性名）。

    Args:
        data: 缓冲。
        off: 起始偏移。
        limit: 上限。

    Returns:
        (串, 下一偏移)；失败返回 (None, off)。
    """
    if off + 2 > limit:
        return None, off
    ln = int.from_bytes(data[off:off + 2], "little")
    if ln == 0xFFFF or ln > _MAX_LEN or off + 2 + ln + 1 > limit:
        return None, off
    if data[off + 2 + ln] != 0:
        return None, off
    raw = data[off + 2:off + 2 + ln]
    # 全 ASCII → latin1；含高位字节 → GBK（中文属性名）
    if all(32 <= b < 127 for b in raw):
        return raw.decode("latin1"), off + 2 + ln + 1
    try:
        return raw.decode("gbk"), off + 2 + ln + 1
    except UnicodeDecodeError:
        return None, off


def scan_string_table(data: bytes) -> list[str]:
    """从 0x64f 起扫描连续字符串表。

    Args:
        data: Library 流原始字节。

    Returns:
        字符串列表（按出现顺序）。
    """
    result: list[str] = []
    off = TABLE_START
    guard = 0
    while off + 2 <= len(data) and guard < _MAX_ITEMS:
        guard += 1
        s, next_off = _read_lstr(data, off, len(data))
        if s is None:
            break
        result.append(s)
        off = next_off
    logger.info("Library string table parsed: %d strings", len(result))
    return result


def _is_attr_name(s: str) -> bool:
    """判断字符串是否为已知属性名。"""
    return s in _ATTR_NAMES


def _normalize_attr_value(value: str) -> str:
    """归一化属性值（过滤路径/噪声）。"""
    if not value:
        return value
    if len(value) > 60 or "\\" in value or "/" in value:
        return ""
    return value


def _apply_attr_value(current: ComponentUserAttr, name: str, value: str) -> None:
    """把属性名/值写入当前 INS 记录（P1-11 关键属性分发）。"""
    current.attrs[name] = value
    if name == "COMPONENT_HIGH_MAX":
        current.comp_high_max = value
    elif name == "额定工作电压(V)":
        current.rated_voltage = value
    elif name == "额定功率(W)":
        current.rated_power = value
    elif name == "描述":
        current.description = value
    elif name == "阻值允许偏差":
        current.resistance_tolerance = value
    elif name == "MFGREGION":
        current.mfg_region = value


def _find_attr_value(data: bytes, next_off: int) -> str | None:
    """在属性名后找第一个非空且非属性名/非 INS 的字符串作为值。

    Args:
        data: Library 流原始字节。
        next_off: 属性名 lstr 结束偏移。

    Returns:
        属性值；无命中返回 None。
    """
    for j in range(next_off, min(next_off + 40, len(data) - 2)):
        v, _ = _read_lstr(data, j, len(data))
        if v is None or not v:
            continue
        if _INS_RE.match(v) or _is_attr_name(v):
            return None
        clean = _normalize_attr_value(v)
        if clean:
            return clean
        return None
    return None


def parse_component_attrs(data: bytes) -> list[ComponentUserAttr]:
    """解析 Library 流用户属性表。

    策略（P1-11 全流扫描）：
    1. 全流逐偏移扫描 lstr（0x64f 起连续表之外还有 GBK 中文属性名，
       如 ``额定工作电压(V)``@0x164a——不在连续表内）；
    2. 遇到 ``INS<digits>`` 编号时新建记录，并把其后连续 lstr（≤24 条）
       存入 ``row_fields``（用于与 ComponentDef 关联，见 mapper）；
    3. 遇到已知属性名时，把其后第一个非空、非属性名的字符串作为值。

    Args:
        data: Library 流原始字节。

    Returns:
        ComponentUserAttr 列表（按 INS 出现顺序）。
    """
    result: list[ComponentUserAttr] = []
    current: ComponentUserAttr | None = None

    # 全流扫描 lstr（0x64f 起 + 尾部二进制区的中文属性名）
    off = TABLE_START
    guard = 0
    while off + 2 <= len(data) and guard < _MAX_ITEMS * 4:
        guard += 1
        s, next_off = _read_lstr(data, off, len(data))
        if s is None:
            # 跳过 1 字节继续扫描（防御二进制区）
            off += 1
            continue
        if _INS_RE.match(s):
            current = ComponentUserAttr(ins_id=s)
            result.append(current)
            # P1-11：收集 INS 行后续 lstr 字段（关联 ComponentDef 用）
            o2 = next_off
            for _ in range(_ROW_FIELD_MAX):
                f, o3 = _read_lstr(data, o2, len(data))
                if f is None:
                    break
                current.row_fields.append(f)
                # R-CAT：分类名识别（值 ∈ CATEGORY_NAMES）
                if current.category is None and f in CATEGORY_NAMES:
                    current.category = f
                o2 = o3
        elif current is not None and _is_attr_name(s):
            value = _find_attr_value(data, next_off)
            if value is not None:
                _apply_attr_value(current, s, value)
        off = next_off

    logger.info("Component user attrs parsed: %d INS records", len(result))
    return result


def register_library_attr_decoder() -> None:
    """Library 流属性解码器注册（占位；Library 非帧结构，无帧解码器）。"""
    # Library 流为字符串表，不走帧分派；此处保留注册入口便于统一调用。
    return None
