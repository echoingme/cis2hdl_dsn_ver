"""中间文件层（dsn_ver 消费侧 + 下游视图层）。

归属（v2.1 用户裁决）：IR 权威归档 schema 归上游 parser（R-IRJSON，
``core/writer/ir_archive_writer.py``）；本包只做**消费侧适配**与
**视图层派生**，不自定义序列化定义。

模块划分（高内聚低耦合）：
- ``ir_load.py``   —— IR JSON 消费侧（load_ir_json 薄封装 + 版本校验）
- ``ir_access.py`` —— IR 只读访问 helper（A1，纯函数）
- ``inherit.py``   —— 信息继承与数据合并（A2/A3，纯函数）
- ``view_layer.py``—— 三 CSV 视图层（B1，对齐黄金列）
"""
