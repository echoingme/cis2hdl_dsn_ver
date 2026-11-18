"""信息继承与数据合并（A2/A3）。

R1/R2 命中后：把 DSN 解析到的、hdl_lib 元件缺失的信息写回实例（描述/
额定功率/电压/偏差/引脚序号名称等），hdl_lib 文件零改动；多实例共享
库元件时以众数聚合 user_attrs（哨兵剔除）。

纯函数设计（高内聚低耦合）：不触碰文件系统、不依赖匹配器内部状态；
输入 DesignIR/ComponentDef/实例，输出继承清单或聚合字典。
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Optional

from cis2hdl.core.ir.component import ComponentDef, ComponentInstanceIR

#: 哨兵值：EXP 描述用 ``-`` 占位（无真实信息），众数聚合时剔除。
_SENTINEL_DESC = {"-", "", "<null>"}

#: 继承优先级：DSN IR 真值 > hdl_lib 元件默认（hdl_lib 缺失才写回）。
_INHERITABLE_KEYS = (
    "描述",
    "额定功率(W)",
    "额定工作电压(V)",
    "阻值允许偏差",
)


def aggregate_user_attrs(
    instances: Iterable[ComponentInstanceIR], library_id: str
) -> dict[str, str]:
    """同 library_id 实例的 user_attrs 众数合并（A3）。

    - 逐键取众数（并列取首个）；哨兵值（``-``/``<null>``/空）剔除；
    - 4 个 GBK 属性键 + 其它非空键全部纳入；
    - 纯函数，不改实例。

    Args:
        instances: 同库元件实例（可跨页）。
        library_id: 库元件 id（仅用于日志/过滤，不强制过滤）。

    Returns:
        ``{键: 众数值}``。
    """
    key_values: dict[str, list[str]] = {}
    for inst in instances:
        for k, v in inst.user_attrs.items():
            if v in _SENTINEL_DESC:
                continue
            key_values.setdefault(k, []).append(v)
    merged: dict[str, str] = {}
    for k, vals in key_values.items():
        if vals:
            merged[k] = Counter(vals).most_common(1)[0][0]
    return merged


def inherit_into_instance(
    inst: ComponentInstanceIR,
    hdl_comp: ComponentDef,
    match_confidence: float = 1.0,
) -> list[str]:
    """R1/R2 信息继承：DSN 有、hdl_lib 缺失的信息写回实例（A2）。

    写回规则（hdl_lib 文件零改动，只写 IR 实例）：
    1. ``user_attrs``：4 个 GBK 继承键，仅当实例缺失/空时写回
       （inst 真值优先，hdl_comp.user_attrs 兜底）；
    2. ``value_override``：实例空且 hdl 元件 value 非空时写回；
    3. 返回继承动作清单（供 QA 统计/审计）。

    Args:
        inst: DSN 实例（将被原位修改）。
        hdl_comp: 命中的 hdl_lib 元件（只读）。
        match_confidence: 匹配置信度（记录用）。

    Returns:
        继承动作列表，如 ``["user_attrs.描述=片式厚膜电阻器"]``。
    """
    actions: list[str] = []
    for key in _INHERITABLE_KEYS:
        cur = inst.user_attrs.get(key)
        if cur and cur not in _SENTINEL_DESC:
            continue  # 实例已有真值，不覆盖
        lib_val = hdl_comp.user_attrs.get(key)
        if lib_val and lib_val not in _SENTINEL_DESC:
            inst.user_attrs[key] = lib_val
            actions.append(f"user_attrs.{key}={lib_val}")
    if not inst.value_override and hdl_comp.value:
        inst.value_override = hdl_comp.value
        actions.append(f"value_override={hdl_comp.value}")
    if actions:
        inst.extra_data["inherit_source"] = {
            "hdl_cell": hdl_comp.part_name,
            "confidence": match_confidence,
            "actions": actions,
        }
    return actions


def pin_number_label_map(
    inst: ComponentInstanceIR, hdl_comp: ComponentDef
) -> dict[str, str]:
    """引脚序号→名称映射（继承链补充：实例 pin_labels 优先，库次之）。"""
    merged: dict[str, str] = {}
    for pd in hdl_comp.pins:
        if pd.number:
            merged.setdefault(pd.number, pd.name or "")
    for k, v in inst.pin_labels.items():
        merged[k] = v
    return merged
