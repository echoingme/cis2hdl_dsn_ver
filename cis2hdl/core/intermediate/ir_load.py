"""中间文件消费侧：IR 权威归档加载（A5/B1）。

上游 ``core/writer/ir_archive_writer.py::load_ir_json`` 已交付反序列化
（component_db dict → ComponentDB + ``DesignIR.model_validate``）。本模块
是**消费侧薄封装**：路径校验 + schema 版本宽容校验（strict/auto 两档）
+ 统一的 ``IntermediateSchemaError``。不实现任何序列化定义（schema 归
上游，见附录-P R-IRJSON）。

设计原则（高内聚低耦合）：
- 只依赖上游 ``load_ir_json`` 公开 API，不触碰 parser 内部；
- 版本校验对**已归档文件**宽容（旧归档无 schema_version 字段 → auto 放行）；
- 返回上游 DesignIR 原对象，不拷贝（下游只读消费）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cis2hdl.core.writer.ir_archive_writer import load_ir_json

logger = logging.getLogger(__name__)

#: 下游仅维护版本常量（不定义 schema；schema 归上游 R-IRJSON）。
INTERMEDIATE_SCHEMA_VERSION = "1.0"


class IntermediateSchemaError(Exception):
    """IR 归档 schema 版本不兼容（strict 模式）。"""


def load(ir_json: Path, strict: bool = False) -> "object":
    """加载上游 IR 权威归档 → DesignIR。

    Args:
        ir_json: ``<project>.dsn.ir.json`` 路径。
        strict: ``True`` 时 schema_version 显式不匹配即抛
            ``IntermediateSchemaError``；``False``（默认）仅警告，
            兼容无 schema_version 字段的旧归档。

    Returns:
        DesignIR（round-trip 与上游 ``write_ir_json`` 输入等价）。

    Raises:
        FileNotFoundError: 归档不存在。
        IntermediateSchemaError: strict 且 schema_version 不匹配。
        ValueError: JSON 损坏或无法反序列化。
    """
    path = Path(ir_json)
    if not path.exists():
        raise FileNotFoundError(f"IR 归档不存在: {path}")

    # 版本校验（宽容：读 schema_version，无/不匹配时按 strict 档处理）。
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"IR 归档 JSON 损坏: {path}: {exc}") from exc
    schema_ver = doc.get("schema_version")
    if schema_ver is not None and schema_ver != INTERMEDIATE_SCHEMA_VERSION:
        msg = (
            f"IR 归档 schema_version={schema_ver!r} 与下游期望 "
            f"{INTERMEDIATE_SCHEMA_VERSION!r} 不匹配: {path}"
        )
        if strict:
            raise IntermediateSchemaError(msg)
        logger.warning("%s（strict=False 放行）", msg)

    design = load_ir_json(path)
    logger.info(
        "IR 归档加载成功: %s（%d 页，schema_version=%s）",
        path.name,
        len(design.pages),
        schema_ver or "（无字段，旧归档）",
    )
    return design


#: 兼容别名（消费侧统一入口：``from ...intermediate.ir_load import load``）。
load_ir = load
