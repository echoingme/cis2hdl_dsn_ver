"""DSN 输入插件（FR1，可选）。

S3 真实现：直接 DSN 解析编排器（不走 P0-D2 EDIF 优先——用户显式启用
``dsn`` 即选择 DSN 元件源；配合 ``cfg.app.use_dsn_components`` 使用）。

流程与 ``edif`` 插件一致（同批引擎子步骤编排，FR9 等价），仅解析路径
决策不同：直接 ``engine._stage_parse(input_path)``（ParserRegistry 按
扩展名选 DSNParser）。

cross_ref / pst 子步骤按"增量插件是否启用"委托或内联（同 edif）：
- ``cross_ref`` 未启用 → 内联 ``_load_cross_ref_csv``。
- ``pstxnet``/``pstchip`` 未启用 → 内联 ``_load_pst_files``（只加载未覆盖
  文件，``log_summary=False``）。

返回 True 表示接管解析（ctx.ir 已写）；返回 False → 引擎回退 legacy 全链。
"""

from __future__ import annotations

from typing import Any

from ..hookspecs import hookimpl
from ..context import ConversionContext
from ..spec import PluginSpec

__all__ = ["DsnInputPlugin", "PLUGIN"]


class DsnInputPlugin:
    """DSN 输入插件（可选，S3 真实现）。"""

    def __init__(self, engine: Any = None, **kwargs: Any) -> None:
        self.name = "dsn"
        self.engine = engine
        self.params = kwargs

    @hookimpl
    def load_input(self, ctx: ConversionContext) -> bool | None:
        if ctx is None or ctx.ir is not None:
            return False  # 已被其他解析插件接管（edif/dsn 互斥）
        engine = self.engine
        if engine is None:
            return False
        input_path = ctx.input_files[0] if ctx.input_files else None
        if input_path is None:
            return False

        design = engine._stage_parse(input_path, ctx.report, None)
        if design is None:
            return False
        ctx.ir = design
        engine._log_parse_statistics(design)

        # DSN 唯一输入（v2 收口）：cross_ref/pst 子步骤已随 EDIF/PST
        # 路径归档（_retired/）；DSN 为唯一信息源，无附加注入。
        return True

    def cleanup(self) -> None:
        self.engine = None
        self.params = {}


PLUGIN = PluginSpec(
    name="dsn",
    stage="input",
    description="DSN 解析（可选；直接 DSN 元件源，不经 EDIF 优先）",
    cls=DsnInputPlugin,
    module=__name__,
    writes_keys=("ir",),
    requires=(),
    builtin=True,
)
