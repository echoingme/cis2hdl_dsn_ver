"""DSNParser — Binary DSN 顶层调度器（架构设计 §2 L6）。

新帧语义管道（TG0C0D2 平铺页面，18 页）：

    OlefileProvider → Library strLst(2170) → Cache/Packages
    → 页面 8B 分隔符切帧 → 网名表/走线 → InstanceBinder
    → ir.mapper → DesignIR

RTL8367RB 变体回退：
    当 Library strLst 数量 ≠ 2170（该变体无 0x85 位号帧 / 0x22 文本位号）
    或新帧管道任一步失败时，回退到 ``LegacyDSNParser``（旧 preamble
    扫描 + 结构体模型），保证既有测试与下游不变。

对外契约（与旧版一致）：
    ``DSNParser.FORMAT_NAME == 'CIS_DSN'``
    ``DSNParser.FILE_EXTENSIONS == ['.dsn']``
    ``parser.parse(path) -> DesignIR``
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING

from ..base import ParserBase
from ...ir.design import DesignIR

if TYPE_CHECKING:
    from ..bind.binder import InstanceBind
    from ..cache.cache_parser import CacheData
    from ..cache.packages_parser import PackageData
    from ..decode.net_table import NetTable
    from ..decode.wire_frames import WireRecord
    from ..frame.splitter import Frame
    from ..io.ole_provider import OlefileProvider

logger = logging.getLogger(__name__)


class DSNParser(ParserBase):
    """Binary DSN 解析器 — 将 .dsn 文件解析为完整 DesignIR（含坐标）。

    Usage:
        parser = DSNParser()
        ir = parser.parse(Path("project.dsn"))
    """

    FORMAT_NAME = "CIS_DSN"
    FILE_EXTENSIONS = [".dsn"]

    def parse(self, dsn_path: Path, exp_path: Path | None = None) -> DesignIR:
        """解析 DSN 二进制文件为 DesignIR。

        优先走新帧语义管道；不适用（RTL 变体）或失败时回退旧路径。

        Args:
            dsn_path: .dsn 文件路径。
            exp_path: 可选黄金 partandpin.EXP（part+pin 型）路径——传入时
                对 IR 的 pin_connections 应用 EXP 键名对齐（L1）+ 补键/
                修正（L2），使键名与 EXP 库序完全一致（匹配率 100%）。
                不传时保持纯 DSN 解析（诚实基线，无 EXP 依赖）。

        Returns:
            包含坐标和逻辑数据的完整 DesignIR。

        Raises:
            FileNotFoundError: 文件不存在。
            ValueError: 文件内容无法解析。
        """
        logger.info("Parsing DSN: %s", dsn_path)
        result = self._try_parse_new_path(dsn_path)
        if result is not None:
            if exp_path is not None:
                from .exp.exp_dir_assist import align

                align_stats = align(result, Path(exp_path))
                result.metadata["exp_source"] = align_stats["exp_type"]
                result.metadata["exp_align"] = {
                    k: v for k, v in align_stats.items()
                    if k != "exp_type"
                }
                logger.info(                    "EXP 辅助键名对齐: %s (L1=%d L2a=%d L2b=%d L2c=%d)",
                    align_stats["exp_type"], align_stats["l1_instances"],
                    align_stats["l2a_filled"], align_stats["l2b_fixed"],
                    align_stats["l2c_removed"],
                )
            # 2026-08-21：U7 六段独立芯片引脚对象偏移注入（exp 对齐
            # 后的全量 pin_connections 作为网名↔引脚名 join 基准；
            # u7_pin_order 供无网名对象（U7F）按物理序分配坐标）
            self._inject_pin_object_offsets(
                result,
                result.metadata.get("pin_objects_by_page") or {},
                exp_pin_order=(
                    (align_stats or {}).get("u7_pin_order")
                    if exp_path is not None else None
                ),
            )
            return result

        logger.warning(
            "New frame-semantic path not applicable for %s; "
            "falling back to legacy RTL path", dsn_path,
        )
        from .legacy_dsn_parser import LegacyDSNParser

        return LegacyDSNParser().parse(dsn_path)

    # ── 新帧语义管道 ────────────────────────────────────────────────

    def _try_parse_new_path(self, dsn_path: Path) -> DesignIR | None:
        """尝试新帧语义管道；失败返回 None（触发回退）。"""
        from .cache.cache_parser import parse_cache_stream
        from .cache.packages_parser import parse_packages
        from .decode.net_table import parse_net_table
        from .decode.wire_frames import scan_wire_frames
        from .decode.registry import DecodeContext
        from .frame.splitter import split_frames
        from .io.ole_provider import OlefileProvider
        from .strlst.strlst import parse_strlst

        provider: OlefileProvider | None = None
        try:
            provider = OlefileProvider(dsn_path)
            design_name = provider.design_name()

            # ── 1. Library strLst ─────────────────────────────────
            lib_bytes = provider.read_stream("Library")
            strlst = parse_strlst(lib_bytes)
            page_streams_all = provider.page_streams(design_name)
            # P1-9：启发式判断（替代 2170 硬门槛）
            from .decode.text_style import should_use_new_path

            if not should_use_new_path(strlst, len(page_streams_all)):
                logger.info(
                    "strLst=%d pages=%d → 启发式不适用，回退旧路径",
                    len(strlst), len(page_streams_all),
                )
                return None

            # ── 2. Cache / Packages ───────────────────────────────
            cache: "CacheData | None" = None
            try:
                cache = parse_cache_stream(provider.read_stream("Cache"))
            except Exception as exc:
                logger.warning("Cache 解析失败（继续）：%s", exc)

            packages: list["PackageData"] = []
            try:
                packages = parse_packages(provider.packages())
            except Exception as exc:
                logger.warning("Packages 解析失败（继续）：%s", exc)

            # ── 3. 页面流切帧 + 网名表 + 走线 + P0 拓扑 ───────────
            from .bind.binder import InstanceBinder
            from .decode.connectivity_points import scan_connection_points
            from .decode.page_text_frames import (
                scan_page_elements_defensive,
                scan_page_graphic_frames,
            )
            from .decode.pin_table_frames import scan_pin_partition_frames
            from .decode.source_symbols import scan_source_symbols
            from .decode.symbol_pin_frames import scan_symbol_pin_refs
            from .decode.wire_topology import (
                ConnectivityBuilder,
                build_wire_polylines,
                collect_pin_points,
            )

            page_streams = provider.page_streams(design_name)
            binder = InstanceBinder(strlst, cache, packages)
            # P2-B：网名帧扫描 + 类型发现（frame_stats 覆盖率口径）
            from .decode.net_name_frames import (
                discover_net_name_types,
                register_net_name_decoders,
                scan_net_name_frames,
            )

            pages_frames: list[tuple[str, list["Frame"], "NetTable"]] = []
            # 新 P1 12 元组：页面名/绑定/网表/走线/pin_partitions/
            #   text_elements/graphic_elements/polylines/connectivity/
            #   source_symbols/connection_points/pin_refs
            pages_data: list[tuple] = []
            binds_by_page: list[list["InstanceBind"]] = []
            pin_objects_by_page: dict[str, list[tuple]] = {}
            ctx = DecodeContext(strlst=strlst)
            all_frames_for_discovery: list["Frame"] = []
            for page_name, data in page_streams:
                frames = split_frames(data)
                net_table = parse_net_table(data)
                ctx.page_name = page_name
                pages_frames.append((page_name, frames, net_table))
                all_frames_for_discovery.extend(frames)
                # 走线扫描与 QA 一致：仅统计网名表之后的结构区帧
                after = net_table.offset if net_table.offset is not None else 0x400
                struct_frames = [f for f in frames if f.offset >= after]
                wire_scan = scan_wire_frames(struct_frames, ctx)

                # ── P0-1：0x0867 引脚分区表（全帧含信息页兜底）──
                pin_partitions = scan_pin_partition_frames(frames, ctx)
                # ── P0-2：0x0000 文字元素（含 GBK）+ 图形元素 ─────
                text_elements = scan_page_elements_defensive(frames, ctx)
                graphic_elements = scan_page_graphic_frames(frames, ctx)
                # ── P0-4：走线折线拼接 ────────────────────────────
                polylines = build_wire_polylines(wire_scan.wires, net_table)
                # ── P0-5：引脚点 + 连接关系 ───────────────────────
                pin_points = collect_pin_points(struct_frames)
                # 绑定该页（含 pin_label_map）用于连接关系实例关联
                page_binds = binder.bind_page(frames, net_table, page_name)
                # P2-B：网名帧（诊断透传 + 覆盖率）
                net_name_records = scan_net_name_frames(struct_frames, ctx)
                connectivity = ConnectivityBuilder(net_table).build(
                    wire_scan.wires, pin_points, page_binds,
                    net_name_records=net_name_records,
                )
                wire_scan.polylines = polylines
                wire_scan.pin_points = pin_points

                # ── P1-1/P1-7/P1-10：引脚引用/连接点/源符号 ──────
                pin_refs = scan_symbol_pin_refs(struct_frames, ctx)
                source_symbols = scan_source_symbols(struct_frames, ctx)
                connection_points = scan_connection_points(struct_frames, ctx)

                # ── T04 P2-1..7：展示维度 ────────────────────────
                from .decode.page_size import parse_page_size
                from .decode.graphic_shapes import scan_graphic_shape_frames
                from .decode.symbol_bbox import scan_symbol_bboxes
                from .decode.offpage_connectors import (
                    associate_offpage_nets,
                    scan_offpage_connectors,
                )

                page_size = parse_page_size(data)
                graphic_shapes = scan_graphic_shape_frames(struct_frames, ctx)
                symbol_bboxes = scan_symbol_bboxes(struct_frames, ctx)
                offpage = scan_offpage_connectors(struct_frames, ctx)
                # 2026-08-21：内容探测补 ONSHEET2/OFFSHEET2 连接符帧
                # （帧类型=strLst 索引数据相关，类型 38 仅覆盖部分）
                from .decode.offpage_connectors import (
                    scan_offpage_connectors_content,
                )
                offpage += scan_offpage_connectors_content(data, strlst)
                # OffPage 跨页 net 关联（坐标 ∈ 走线端点 → 网名表）
                ep_net_map: dict[tuple[int, int], str] = {}
                for w in wire_scan.wires:
                    nm = net_table.name_of(w.net_id)
                    if nm:
                        ep_net_map[(w.x1, w.y1)] = nm
                        ep_net_map[(w.x2, w.y2)] = nm
                associate_offpage_nets(offpage, ep_net_map)

                binds_by_page.append(page_binds)
                # 2026-08-21：页面流"引脚对象区"扫描（物理坐标+网名，
                # U7 六段独立芯片的引脚标签坐标来源）
                pin_objects_by_page[page_name] = self._scan_pin_objects(data)
                pages_data.append((
                    page_name, page_binds, net_table, wire_scan.wires,
                    pin_partitions, text_elements, graphic_elements,
                    polylines, connectivity,
                    source_symbols, connection_points, pin_refs,
                    page_size.to_dict(), graphic_shapes, symbol_bboxes, offpage,
                ))

            # ── 4. 实例绑定（含 U7 多单元合并）───────────────────
            all_binds: list["InstanceBind"] = []
            for binds in binds_by_page:
                all_binds.extend(binds)
            merged_binds = binder._merge_multi_unit(all_binds)

            # P2-B：发现并注册网名帧类型（frame_stats 覆盖率口径）
            discover_net_name_types(all_frames_for_discovery)
            register_net_name_decoders()
            # T04 P2-7：短坐标引用帧类型（覆盖率剩余）。先注册基础
            # 解码器（0x01/0x02/0x22 等已有专用语义），discovery 只收集
            # **未注册**的小类型，避免覆盖语义/虚高覆盖率。
            from .decode import register_all_decoders

            register_all_decoders()
            from .decode.coord_ref_frames import (
                discover_coord_ref_types,
                register_coord_ref_decoders,
            )

            discover_coord_ref_types(all_frames_for_discovery)
            register_coord_ref_decoders()

            # ── 5. IR 映射 ────────────────────────────────────────
            from .ir.mapper import map_to_design_ir

            # P1-11：Library 流 INS 属性（解析一次，供 metadata + user_attrs 回填）
            library_attrs = self._parse_library_attrs(provider)

            # P2-A：frame_stats 帧覆盖率统计 + 解析警告
            frame_stats = self._compute_frame_stats(pages_frames)
            warnings = self._collect_parse_warnings(merged_binds)

            design = map_to_design_ir(
                project_name=dsn_path.stem,
                pages_data=pages_data,
                cache=cache,
                packages=packages,
                library_attrs=library_attrs,
                frame_stats=frame_stats,
                warnings=warnings,
            )
            # 跨页合并多单元 → IR 实例（按 refdes 合并，section=单元数）
            self._merge_ir_multi_unit(design, merged_binds, cache=cache)
            # 2026-08-21：U7 六段独立芯片引脚对象偏移注入
            # （mock 全量引脚线 + LASTPIN 标签坐标来源）
            design.metadata["pin_objects_by_page"] = pin_objects_by_page

            design.metadata["strlst_count"] = len(strlst)
            design.metadata["merged_instances"] = len(merged_binds)
            design.metadata["unique_refdes"] = len(
                {b.refdes for b in merged_binds}
            )
            design.metadata["value_bound"] = sum(
                1 for b in merged_binds if b.value
            )
            design.metadata["footprint_bound"] = sum(
                1 for b in merged_binds if b.footprint
            )
            # ── P1 metadata 统计键 ────────────────────────────────
            def _p1_count(index: int) -> int:
                """统计 pages_data 第 index 个 P1 字段（防御旧元组）。"""
                return sum(
                    len(p[index]) if index < len(p) else 0
                    for p in pages_data
                )

            design.metadata["source_symbol_count"] = _p1_count(9)
            design.metadata["connection_point_count"] = _p1_count(10)
            design.metadata["pin_ref_count"] = _p1_count(11)
            design.metadata["symbol_text_count"] = (
                len(cache.symbol_texts) if cache is not None else 0
            )
            design.metadata["library_attr_count"] = len(library_attrs)
            design.metadata["cache_pin_geometry_count"] = (
                len(cache.pin_geometries) if cache is not None else 0
            )
            # ── P0 帧覆盖率（0x0867/0x0000 不再 UnknownFrame）─────
            frame_coverage = self._compute_frame_coverage(pages_frames)
            design.metadata["frame_coverage"] = frame_coverage
            # ── L0 证据层：保留全部帧供全帧中间文件输出（OFA-20260820
            #    晚，用户要求"99.7% 覆盖的每一帧都要可追溯"）。只读引用，
            #    不复制（省内存）；输出器按需消费。──
            design.metadata["_pages_frames_ref"] = pages_frames
            logger.info(
                "DSN new-path parse complete: %d pages, %d merged instances, "
                "%d unique refdes, %d wires, frame_coverage=%s",
                len(pages_frames), len(merged_binds),
                design.metadata["unique_refdes"],
                design.metadata["total_wires"],
                frame_coverage,
            )
            return design
        except Exception as exc:
            logger.warning("New-path parse failed: %s", exc)
            return None
        finally:
            if provider is not None:
                provider.close()

    @staticmethod
    def _parse_library_attrs(provider: "OlefileProvider") -> list:
        """解析 Library 流用户属性表（P1-11；防御：失败仅 warn）。

        Args:
            provider: 已打开的 OLE provider。

        Returns:
            ComponentUserAttr 列表；解析失败返回 []。
        """
        from .decode.library_attrs import parse_component_attrs

        try:
            return parse_component_attrs(provider.read_stream("Library"))
        except Exception as exc:
            logger.warning("Library attrs parse failed (continue): %s", exc)
            return []

    @staticmethod
    def _compute_frame_coverage(
        pages_frames: list[tuple[str, list["Frame"], "NetTable"]],
    ) -> dict:
        """计算 P0 帧覆盖率（0x0867/0x0000 是否被解码器接管）。

        对比口径：旧解码器集合（语义+结构，不含 P0）vs 完整集合
        （含 0x0867/0x0000 新解码器）。覆盖率 = 可解码帧数 / 总帧数。

        P1 增强：26/27（symbol_pin）、0x03/0x05/0x06（连接点）、
        source symbol 11 类型（0x46/0x50/...）均注册进完整集合，
        coverage_before 从 30.6 升至 30.8（18 个 26/27 帧不再 UnknownFrame）。

        Args:
            pages_frames: [(页面名, 帧列表, 网名表)]。

        Returns:
            dict: {total, decoded_p0, unknown_before_p0, unknown_after_p0,
                   coverage_before, coverage_after, p0_frame_types}。
        """
        from .decode.registry import (
            SEMANTIC_FRAME_DECODERS,
            STRUCT_DECODERS,
            clear_registries,
        )
        from .decode.semantic_frames import register_semantic_decoders
        from .decode.struct_frames import register_struct_decoders
        from .decode.pin_table_frames import register_pin_partition_decoder
        from .decode.page_text_frames import register_page_text_decoder
        from .decode.connectivity_points import register_connection_point_decoders
        from .decode.source_symbols import register_source_symbol_decoders

        total = 0
        unknown_before = 0
        unknown_after = 0
        p0_types: dict[int, int] = {}
        try:
            # 1) 旧解码器集合（语义 + 结构）
            clear_registries()
            register_semantic_decoders()
            register_struct_decoders()
            for _name, frames, _net_table in pages_frames:
                total += len(frames)
                for f in frames:
                    if f.frame_type not in SEMANTIC_FRAME_DECODERS and \
                       f.frame_type not in STRUCT_DECODERS:
                        unknown_before += 1
            # 2) 完整集合（+ P0 新解码器 + P1 新解码器）
            register_pin_partition_decoder()
            register_page_text_decoder()
            register_connection_point_decoders()
            register_source_symbol_decoders()
            for _name, frames, _net_table in pages_frames:
                for f in frames:
                    if f.frame_type in (0x0867, 0x0000):
                        p0_types[f.frame_type] = p0_types.get(f.frame_type, 0) + 1
                    if f.frame_type not in SEMANTIC_FRAME_DECODERS and \
                       f.frame_type not in STRUCT_DECODERS:
                        unknown_after += 1
        finally:
            clear_registries()
            from .decode import register_all_decoders
            register_all_decoders()
        return {
            "total": total,
            "decoded_p0": sum(p0_types.values()),
            "unknown_before_p0": unknown_before,
            "unknown_after_p0": unknown_after,
            "coverage_before": round(
                (total - unknown_before) / max(total, 1) * 100, 1
            ),
            "coverage_after": round(
                (total - unknown_after) / max(total, 1) * 100, 1
            ),
            "p0_frame_types": p0_types,
        }

    @staticmethod
    def _compute_frame_stats(
        pages_frames: list[tuple[str, list["Frame"], "NetTable"]],
    ) -> dict:
        """计算 P2 frame_stats 帧覆盖率统计（metadata.frame_stats）。

        口径（与审计「帧覆盖率」一致）：
        - total_frames：页面流帧总数（TG0C0D2 = 10995）；
        - covered_frames：类型号 ∈ 完整解码器集合（语义+结构+P0+P1）的帧数；
        - unknown_frames：无解码器类型号帧数（UnknownFrame）；
        - covered_pct：``covered / total * 100``（round 1 位）；
        - top_unknown_types：未覆盖类型号 Top 10（strLst 索引 → 帧数，
          数据相关，供后续补全定位最高价值帧族）。

        防御：解码器注册失败不抛异常（返回全 0 结构）。

        Args:
            pages_frames: [(页面名, 帧列表, 网名表)]。

        Returns:
            dict: frame_stats 结构。
        """
        from .decode.registry import (
            SEMANTIC_FRAME_DECODERS,
            STRUCT_DECODERS,
            _content_probe_named_geometry,
            clear_registries,
        )
        from .decode import register_all_decoders
        from .decode.source_symbols import TARGET_FRAME_TYPES

        total = 0
        unknown_types: dict[int, int] = {}
        probe_count = 0
        try:
            # 先清空注册表，再按当前帧流内容探测发现 coord_ref/net_name
            # 类型号（数据相关；discover 只收集"未注册"类型，若在 clear 前
            # 调用会把已注册类型跳过 → 发现集被清空 → 覆盖率口径漂移）
            clear_registries()
            from .decode.coord_ref_frames import discover_coord_ref_types
            from .decode.net_name_frames import discover_net_name_types

            all_frames = [f for _, frames, _ in pages_frames for f in frames]
            discover_net_name_types(all_frames)
            discover_coord_ref_types(all_frames)
            register_all_decoders()
            for _name, frames, _net_table in pages_frames:
                total += len(frames)
                for f in frames:
                    if f.frame_type not in SEMANTIC_FRAME_DECODERS and \
                       f.frame_type not in STRUCT_DECODERS and \
                       f.frame_type not in TARGET_FRAME_TYPES:
                        # T04：内容探测兜底（命名几何帧族）——命中即覆盖。
                        # **诚实口径**：probe 仅"识别为命名几何帧"，坐标可能
                        # 为宽松扫描的 best-effort 值（QA P2#13 已抽查 10 帧
                        # 中 ≥6 帧坐标乱码）→ 单独计数，不混入 decoder。
                        if _content_probe_named_geometry(f) is None:
                            unknown_types[f.frame_type] = \
                                unknown_types.get(f.frame_type, 0) + 1
                        else:
                            probe_count += 1
        except Exception:
            # 防御：统计失败不阻塞主流程
            return {
                "total_frames": total,
                "covered_frames": 0,
                "unknown_frames": 0,
                "covered_pct": 0.0,
                "top_unknown_types": {},
            }
        finally:
            clear_registries()
            register_all_decoders()

        unknown = sum(unknown_types.values())
        covered = max(total - unknown, 0)
        top_unknown = sorted(unknown_types.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        # decoder_count：注册表解码器实际解码帧数（含 TARGET 白名单）；
        # probe_count：内容探测兜底帧数（best-effort 坐标，单独计数）。
        decoder_count = max(covered - probe_count, 0)
        return {
            "total_frames": total,
            "covered_frames": covered,
            "unknown_frames": unknown,
            "covered_pct": round(covered / max(total, 1) * 100, 1),
            "decoder_frames": decoder_count,
            "decoder_pct": round(decoder_count / max(total, 1) * 100, 1),
            "probe_frames": probe_count,
            "probe_pct": round(probe_count / max(total, 1) * 100, 1),
            "top_unknown_types": {
                f"0x{tid:04x}": cnt for tid, cnt in top_unknown
            },
        }

    @staticmethod
    def _collect_parse_warnings(
        merged_binds: list["InstanceBind"],
    ) -> list[dict]:
        """收集解析警告（P2 metadata.warnings）。

        当前警告项：
        - COORD_PLACED_FALLBACK：实例 loc 走 0x0D PlacedInstance 兜底
          （shape 帧坐标缺失，防御性替代）。
        格式：``{"severity": "WARN", "code": "...", "msg": "...",
        "page_id": "...", "refdes": "..."}``。

        Args:
            merged_binds: 合并后实例绑定列表。

        Returns:
            警告 dict 列表（无警告返回 []）。
        """
        warnings: list[dict] = []
        for b in merged_binds:
            if b.shape == "GND_SYMBOL":
                continue
            if b.loc_x is None or b.loc_y is None:
                warnings.append({
                    "severity": "WARN",
                    "code": "COORD_MISSING",
                    "msg": f"实例 {b.refdes} 坐标缺失（shape/0x0D 均无合法值）",
                    "page_id": b.page,
                    "refdes": b.refdes,
                })
                continue
            # 0x0D 兜底无法从 bind 直接判断（坐标已归一），此处仅记录
            # 真实缺失项，避免误报。
        return warnings

    @staticmethod
    def _merge_ir_multi_unit(
        design: DesignIR,
        merged_binds: list["InstanceBind"],
        cache: "CacheData | None" = None,
    ) -> None:
        """按 refdes 合并 IR 实例（保留首个页面位置，section=单元数）。

        P2-B/P2-E 增强：跨单元合并 **pin_connections**（各单元页的
        连接按单元前缀归并）与 **pin_labels**（六单元标号映射聚合到
        主实例，U7 共 203 对），修复"只保留首页单元导致 U7 连接
        全丢 / 主单元标号被 _merge_multi_unit 覆盖"的旧缺陷。
        """
        from ...ir.component import ComponentInstanceIR

        def _seg_key(refdes: str, lib: str) -> object:
            """2026-08-21 通用化：多段实例（同 refdes 多库名）归并键
            用 (refdes, 库名段) 保留各自独立；单段实例保持 refdes 键。
            对任意多 section 芯片通用（U7A~U7F 六段、其他多单元）。"""
            if lib:
                return (refdes, lib)
            return refdes

        by_refdes: dict = {}
        for page in design.pages:
            for inst in page.instances:
                key = _seg_key(inst.refdes, inst.library_id)
                if key not in by_refdes:
                    by_refdes[key] = inst

        # 各单元实例（IR 中未合并前的页面级实例）→ 单元连接/标号聚合
        unit_instances: dict = defaultdict(list)
        for page in design.pages:
            for inst in page.instances:
                key = _seg_key(inst.refdes, inst.library_id)
                unit_instances[key].append((page.page_id, inst))

        for b in merged_binds:
            inst = by_refdes.get(_seg_key(b.refdes, b.shape))
            if inst is None:
                continue
            inst.section = b.section
            if b.value:
                inst.value_override = b.value
                inst.properties.setdefault("Value", b.value)
            if b.footprint:
                inst.properties.setdefault("PCB Footprint", b.footprint)
            # P2-E：六单元标号映射聚合（U7 共 203 对：43+11+24+18+18+89）。
            # 优先从 Cache 权威源聚合（避免 binder._merge_multi_unit 把主
            # 单元 map 覆盖为 0F 导致 0D 丢失）；缺失时用各页实例 map。
            merged_labels: dict[str, str] = {}
            bind_base = (b.shape or "").rsplit(".", 1)[0]
            unit_prefix = bind_base[:-1] if bind_base and len(bind_base) > 3 else bind_base
            if cache is not None and cache.pin_label_maps:
                for pm in cache.pin_label_maps:
                    if unit_prefix and pm.unit_name.startswith(unit_prefix):
                        merged_labels.update(pm.by_label)
            for _page_id, unit in unit_instances.get(b.refdes, []):
                if unit.pin_labels:
                    merged_labels.update(unit.pin_labels)
            if b.pin_label_map:
                merged_labels.update(b.pin_label_map)
            if merged_labels:
                inst.pin_labels = merged_labels
            # P2-B：跨单元 pin_connections 聚合。各单元页位置键（P{idx}）
            # 会跨单元冲突，冲突时加页面后缀保证唯一（诚实唯一键）。
            # 注意：主实例自身也出现在 unit_instances，跳过避免自我复制。
            for page_id, unit in unit_instances.get(
                _seg_key(b.refdes, b.shape), []
            ):
                if unit is inst:
                    continue
                for pin_key, net_name in list(unit.pin_connections.items()):
                    if pin_key not in inst.pin_connections:
                        inst.pin_connections[pin_key] = net_name
                    else:
                        alt = f"{pin_key}@{page_id}"
                        if alt not in inst.pin_connections:
                            inst.pin_connections[alt] = net_name
            inst.extra_data["value_source"] = b.value_source
            inst.extra_data["footprint_source"] = b.footprint_source

        # 移除跨页重复（保留首个出现页；Hi5662 六段按 (refdes, 段库名)
        # 键保留各自独立）
        seen: set = set()
        for page in design.pages:
            kept: list[ComponentInstanceIR] = []
            for inst in page.instances:
                key = _seg_key(inst.refdes, inst.library_id)
                if key in seen:
                    continue
                seen.add(key)
                kept.append(inst)
            page.instances = kept

    # ── U7 引脚对象区（页面流物理坐标+网名 → 实例偏移）────────────

    @staticmethod
    def _scan_pin_objects(data: bytes) -> list[tuple]:
        """页面流"引脚对象区"扫描（handoff 附录 C，字节级实证）：
        ``[8B DELIM][object_id:u32][?][30 00 00 00][x1][y1][x2][y2]``
        + 网名对象（``[x][y][30 00 00 00][0][4][len][name]``）。

        Returns:
            [(object_id, x, y, 网名)]（3170 全设计 / 1085 带网名）。
        """
        import re as _re

        pins: list[tuple] = []
        start = 0
        while True:
            i = data.find(b"\xff\xe4\x5c\x39\x00\x00\x00\x00", start)
            if i < 0:
                break
            if i + 36 <= len(data):
                oid = int.from_bytes(data[i + 8:i + 12], "little")
                if (data[i + 16:i + 20] == b"\x30\x00\x00\x00"
                        and 0 < oid < 200000):
                    x1 = int.from_bytes(data[i + 20:i + 24], "little",
                                        signed=True)
                    y1 = int.from_bytes(data[i + 24:i + 28], "little",
                                        signed=True)
                    if 0 < x1 < 3000 and 0 < y1 < 3000:
                        seg = data[i + 36:i + 200]
                        m = _re.search(rb"[A-Z_$][A-Z0-9_$]{2,30}\x00", seg)
                        nm = (m.group(0)[:-1].decode("latin1")
                              if m else "")
                        x2 = int.from_bytes(data[i + 28:i + 32], "little",
                                            signed=True)
                        y2 = int.from_bytes(data[i + 32:i + 36], "little",
                                            signed=True)
                        pins.append((oid, x1, y1, x2, y2, nm))
            start = i + 8
        return pins

    def _inject_pin_object_offsets(
        self,
        design: "DesignIR",
        pin_objects_by_page: dict[str, list[tuple]],
        exp_pin_order: "dict[str, list[str]] | None" = None,
    ) -> None:
        """U7 六段独立芯片：页面流引脚对象（物理坐标+网名）→ 实例
        符号局部偏移（extra_data['pin_object_offsets']），mock 符号
        生成全量引脚 C 命令 + LASTPIN 标签发射的坐标来源。

        - 对象网名 ∈ 实例 pin_connections（exp 对齐后全量）→
          引脚名 = pc 键；偏移 = 对象坐标 - 实例 loc（符号局部）。
        - 无网名对象（U7F GND 页等）：引脚对象是**折线分段**
          （终点=下一起点），按 oid 序合并折线 → 折线数 = 引脚数
          （U7F 实测 178），按 EXP 段引脚序（文件序=物理序，
          J1704 实证）逐一分配坐标。
        - 2026-08-21 通用化：对**任意元件**（非仅 U7）注入——仅当
          库符号引脚线不足以覆盖连接（mock 缺口补全），小件
          （R/C/L cache 引脚线完整）不注入，避免重复。
        """
        for pg in design.pages:
            objs = pin_objects_by_page.get(
                pg.page_id or pg.page_name
            ) or pin_objects_by_page.get(pg.page_name) or []
            for inst in pg.instances:
                pc = inst.pin_connections or {}
                if not pc:
                    continue
                # 通用缺口判定：库符号引脚线 < 连接数 → 注入对象偏移
                _lib = (inst.library_id or "")
                _pin_line_cnt = 0
                _db = getattr(design, "component_db", None)
                if _db is not None:
                    try:
                        _c = _db.get_by_library_id(_lib)
                        if _c is not None:
                            _pin_line_cnt = len(
                                (_c.symbol_geometry or {}).get("pin_lines") or []
                            )
                    except Exception:
                        _pin_line_cnt = 0
                if _pin_line_cnt >= len(pc):
                    continue  # 库引脚线已覆盖，无需对象注入
                # （无 hdl_db 时退化：对 mock 缺口无法判断，仍注入——
                #  mock 生成侧按"已有 pin_lines 不重复"去重）
                loc_x = float(getattr(inst, "loc_x", 0) or 0)
                loc_y = float(getattr(inst, "loc_y", 0) or 0)
                net_to_pins: dict[str, list[str]] = {}
                for pin, net in pc.items():
                    if net:
                        net_to_pins.setdefault(net, []).append(str(pin))
                # 2026-08-21：同网名多引脚（AVDD18_PON ×3 等）↔
                # 多对象坐标按序分配（每对象取一个未分配引脚）
                net_used: dict[str, int] = {}
                offsets: dict[str, tuple[int, int]] = {}
                for _oid, ox, oy, _x2, _y2, nm in objs:
                    if not nm or nm not in net_to_pins:
                        continue
                    pins_ = net_to_pins[nm]
                    i = net_used.get(nm, 0)
                    if i >= len(pins_):
                        continue
                    pin = pins_[i]
                    net_used[nm] = i + 1
                    if pin in offsets:
                        continue  # 每引脚一个偏移
                    offsets[pin] = (int(ox - loc_x), int(oy - loc_y))
                if not offsets and exp_pin_order:
                    # 2026-08-21：无网名对象（U7F GND 页）→ 折线合并
                    # 按 oid 序 ↔ EXP 段引脚序分配
                    order = exp_pin_order.get(inst.refdes) or []
                    if order:
                        lines = self._merge_pin_object_polylines(objs)
                        if len(lines) == len(order):
                            for pin, (sx, sy) in zip(order, lines):
                                offsets[pin] = (int(sx - loc_x),
                                                int(sy - loc_y))
                if offsets:
                    ed = inst.extra_data or {}
                    ed["pin_object_offsets"] = offsets
                    inst.extra_data = ed

    @staticmethod
    def _merge_pin_object_polylines(
        objs: list[tuple],
    ) -> list[tuple[int, int]]:
        """引脚对象折线合并：按 oid 序，终点==下一起点则同线；
        返回每条折线**起点**（物理序，与 EXP 引脚序配对）。"""
        objs = sorted(objs, key=lambda o: o[0])
        lines: list[tuple[int, int]] = []
        cur_end: "tuple[int, int] | None" = None
        for o in objs:
            sx, sy = o[1], o[2]
            if cur_end is None:
                lines.append((sx, sy))
            elif (sx, sy) != cur_end:
                lines.append((sx, sy))
            cur_end = (o[3], o[4])
        return lines

    # ── 绑定导出（full_bind_v3 生成用）──────────────────────────────

    def extract_binds(
        self, dsn_path: Path,
    ) -> dict:
        """解析并返回全部绑定数据（用于 full_bind JSON 导出）。

        Returns:
            dict: {pages: [...], instances: [...], summary: {...}}
        """
        from .cache.cache_parser import parse_cache_stream
        from .cache.packages_parser import parse_packages
        from .decode.net_table import parse_net_table
        from .decode.registry import DecodeContext
        from .decode.wire_frames import scan_wire_frames
        from .frame.splitter import split_frames
        from .io.ole_provider import OlefileProvider
        from .strlst.strlst import parse_strlst

        provider = OlefileProvider(dsn_path)
        try:
            design_name = provider.design_name()
            strlst = parse_strlst(provider.read_stream("Library"))
            cache = parse_cache_stream(provider.read_stream("Cache"))
            packages = parse_packages(provider.packages())

            from .bind.binder import InstanceBinder

            binder = InstanceBinder(strlst, cache, packages)
            ctx = DecodeContext(strlst=strlst)
            pages_out: list[dict] = []
            all_binds: list["InstanceBind"] = []
            for page_name, data in provider.page_streams(design_name):
                frames = split_frames(data)
                net_table = parse_net_table(data)
                ctx.page_name = page_name
                after = net_table.offset if net_table.offset is not None else 0x400
                struct_frames = [f for f in frames if f.offset >= after]
                wire_scan = scan_wire_frames(struct_frames, ctx)
                binds = binder.bind_page(frames, net_table, page_name)
                all_binds.extend(binds)
                pages_out.append({
                    "page": page_name,
                    "frames": len(frames),
                    "nets": len(net_table.names),
                    "wires": len(wire_scan.wires),
                    "instances": len(binds),
                    "refdes_0x85": sum(
                        1 for f in struct_frames if f.frame_type == 0x85
                    ),
                    "refdes_0x22": sum(
                        1 for f in struct_frames if f.frame_type == 0x22
                    ),
                })

            merged = binder._merge_multi_unit(all_binds)
            instances = [b.to_dict() for b in merged]
            summary = {
                "total": len(merged),
                "unique_refdes": len({b.refdes for b in merged}),
                "shape_bound": sum(1 for b in merged if b.shape),
                "value_bound": sum(1 for b in merged if b.value),
                "value_pct": round(
                    sum(1 for b in merged if b.value) / max(len(merged), 1) * 100, 1
                ),
                "footprint_bound": sum(1 for b in merged if b.footprint),
                "footprint_pct": round(
                    sum(1 for b in merged if b.footprint) / max(len(merged), 1) * 100, 1
                ),
                "pin_map_bound": sum(1 for b in merged if b.pin_map),
                "pin_map_pct": round(
                    sum(1 for b in merged if b.pin_map) / max(len(merged), 1) * 100, 1
                ),
                "pin_names_bound": sum(1 for b in merged if b.pin_names),
                "loc_y_bound": sum(1 for b in merged if b.loc_y is not None),
                "strlst_count": len(strlst),
                "cache_parts": cache.count,
                "packages": len(packages),
                "shapes": len({b.shape for b in merged if b.shape}),
            }
            return {
                "pages": pages_out,
                "instances": instances,
                "summary": summary,
            }
        finally:
            provider.close()
