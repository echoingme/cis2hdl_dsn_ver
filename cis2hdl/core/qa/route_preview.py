"""路由结果预览（§11.4，调试用，可执行入口）。

把 source_builder + MatcherPipeline.run_batch + result_router 串起来，
输出路由结果的文本表格（refdes → 路由类 → 置信度 → hdl_cell）——
Stage 4 调试的快捷视图，不落盘、不改 IR。

用法（调试）::

    python -m cis2hdl.core.qa.route_preview <design.dsn> [--exp <partandpin.EXP>]
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def preview(
    design_ir,
    sources,
    plan,
    max_rows: int = 40,
) -> str:
    """路由预览 → 文本表格。

    Args:
        design_ir: DesignIR（用于 R6 页面统计）。
        sources: source_builder 输出（源 ComponentDef 列表）。
        plan: result_router 输出（RoutedPlan）。
        max_rows: 每类最多显示行数。

    Returns:
        文本表格（含各类计数 + 抽样行）。
    """
    lines: list[str] = []
    counts = plan.counts
    lines.append("═══ 路由预览 ═══")
    lines.append(
        f"总源 {len(sources)} | R1 精确 {counts['R1_exact']} | "
        f"R2 近似 {counts['R2_approx']} | R3 mock {counts['R3_unmatched']} | "
        f"R4 非电气 {counts['R4_non_electrical']} | R5 GND {counts['R5_gnd']} | "
        f"R6 连接符 {counts['R6_off_pages']} off-page + {counts['R6_ports']} port"
    )
    lines.append("")

    sections = [
        ("R1 精确", plan.exact),
        ("R2 近似", plan.approx),
        ("R3 未命中(mock)", plan.unmatched),
        ("R4 非电气", plan.non_electrical),
        ("R5 GND", plan.gnd),
    ]
    for label, items in sections:
        lines.append(f"── {label} ({len(items)}) ──")
        for src, res in items[:max_rows]:
            ref = (src.extra_data.get("refdes") if src else "?") or "?"
            conf = f"{res.confidence:.2f}" if res.confidence is not None else "?"
            cell = res.target_library_id or res.error_note or "-"
            lines.append(f"  {ref:<10} conf={conf:<6} → {cell}")
        if len(items) > max_rows:
            lines.append(f"  … 共 {len(items)} 行")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：``python -m cis2hdl.core.qa.route_preview``。"""
    parser = argparse.ArgumentParser(description="路由结果预览（调试）")
    parser.add_argument("dsn", type=Path, help="DSN 文件路径")
    parser.add_argument("--exp", type=Path, default=None,
                        help="partandpin.EXP（可选，EXP 辅助对齐）")
    parser.add_argument("--rows", type=int, default=40, help="每类最大行数")
    args = parser.parse_args(argv)

    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    ir = DSNParser().parse(args.dsn, exp_path=args.exp)
    # 匹配预览（不要求 hdl_lib，未匹配全部 R3 展示）。
    from cis2hdl.core.matcher.source_builder import build_source_components
    from cis2hdl.core.matcher.result_router import route
    from cis2hdl.core.ir.match import MatchResult, MatchStrategy

    sources = build_source_components(ir)
    results = [
        MatchResult(confidence=0.0, strategy=MatchStrategy.NEEDS_REVIEW,
                    source_library_id=s.library_id,
                    error_note="preview(未运行 run_batch)")
        for s in sources
    ]
    plan = route(results, sources, ir)
    print(preview(ir, sources, plan, max_rows=args.rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
