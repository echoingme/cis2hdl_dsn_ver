"""IR 只读访问 helper（A1）。

纯函数集合：对 DesignIR 提供只读查询（实例/网络/元件/页/网名→net_id），
**不修改任何 IR 对象**。供下游匹配器、视图层、几何复刻共用，避免各处
重复遍历 ``ir.pages``（高内聚：访问逻辑唯一收敛点；低耦合：无状态）。

匹配规则（与 mapper 一致）：
- ``library_id`` 精确匹配优先；失败回退 ``<基础名>.Normal``（多单元符号
  ``U7A.Normal`` 等基础名 = 段名，库表按基础名注册）。
"""

from __future__ import annotations

from typing import Iterator, Optional

from cis2hdl.core.ir.component import ComponentDef, ComponentInstanceIR
from cis2hdl.core.ir.design import DesignIR, NetIR, PageIR


def iter_pages(design: DesignIR) -> Iterator[PageIR]:
    """遍历全部页面。"""
    yield from design.pages


def iter_instances(design: DesignIR) -> Iterator[ComponentInstanceIR]:
    """遍历全部实例（跨页，保持页序）。"""
    for page in design.pages:
        yield from page.instances


def get_page(design: DesignIR, page_id: str) -> Optional[PageIR]:
    """按 page_id 取页（如 ``'1.1'``）。"""
    for page in design.pages:
        if page.page_id == page_id:
            return page
    return None


def get_instance(design: DesignIR, refdes: str) -> Optional[ComponentInstanceIR]:
    """按位号取实例（U7A-F 分段实例以自身 refdes 为键）。"""
    for inst in iter_instances(design):
        if inst.refdes == refdes:
            return inst
    return None


def get_net(design: DesignIR, name: str) -> Optional[NetIR]:
    """按网名取网络（跨页；同网名跨页取首页）。"""
    for page in design.pages:
        for net in page.nets:
            if net.name == name:
                return net
    return None


def net_id_of(design: DesignIR, name: str) -> Optional[int]:
    """网名 → net_id（R-NETID 显式字段；无则 None）。"""
    net = get_net(design, name)
    return net.net_id if net is not None else None


def get_component(design: DesignIR, library_id: str) -> Optional[ComponentDef]:
    """按 library_id 取库元件（``.Normal`` 回退）。"""
    db = design.component_db
    comp = db.get_by_library_id(library_id)
    if comp is not None:
        return comp
    base = library_id.rsplit(".", 1)[0] if "." in library_id else library_id
    return db.get_by_library_id(f"{base}.Normal") or db.get_by_library_id(base)


def component_of(
    design: DesignIR, inst: ComponentInstanceIR
) -> Optional[ComponentDef]:
    """实例 → 库元件（经 library_id，含多单元回退）。"""
    return get_component(design, inst.library_id)


def page_of(design: DesignIR, refdes: str) -> Optional[PageIR]:
    """位号 → 所在页。"""
    for page in design.pages:
        for inst in page.instances:
            if inst.refdes == refdes:
                return page
    return None


def global_nets(design: DesignIR) -> set[str]:
    """全局网名集合（IR global_nets 字段）。"""
    return set(design.global_nets)


def all_net_names(design: DesignIR) -> set[str]:
    """全部网名（跨页，含空网排除）。"""
    return {net.name for page in design.pages for net in page.nets if net.name}
