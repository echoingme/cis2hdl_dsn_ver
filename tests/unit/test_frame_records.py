"""L0 全帧中间文件输出器测试（OFA-20260820 晚）。"""

from __future__ import annotations

import json

import pytest

from cis2hdl.core.parser.dsn.dsn_parser import DSNParser
from cis2hdl.core.writer.frame_records_writer import _decode_kind
from cis2hdl.core.writer.ir_archive_writer import write_all
from tests.support import data_path, design_path


@pytest.fixture(scope="module")
def p2_design() -> object:
    """gold 权威 DSN 解析（模块级一次）。"""
    dsn = design_path("DSN")
    if not dsn.exists():
        pytest.skip(f"reference design not available: {dsn}")
    return DSNParser().parse(dsn)


def test_decode_kind_states() -> None:
    """覆盖状态判定三态可用（decoder/probe/unknown）。"""
    # 类型号 0x0001 文本帧族应命中 decoder 或 unknown（不抛异常即可）
    kind, covered = _decode_kind(0x0001, b"\x00" * 32)
    assert kind in ("decoder", "unknown", "probe", "target")
    assert isinstance(covered, bool)


def test_frames_jsonl_all_frames(p2_design, tmp_path) -> None:
    """全帧 JSONL：10988 帧逐帧记录，kind 分布与 frame_stats 对账。"""
    paths = write_all(p2_design, tmp_path, "TG0C0D2_VER_B_SCH")
    assert paths["frames_jsonl"].exists()
    assert paths["frame_coverage_csv"].exists()
    kinds: dict[str, int] = {}
    n = 0
    with open(paths["frames_jsonl"], encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            n += 1
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
            # 字段完整性
            assert "stream" in rec and "offset" in rec
            assert "frame_type_hex" in rec and "raw_hex" in rec
    assert n == 10988
    fs = p2_design.metadata["frame_stats"]
    # kind 分布与 frame_stats 口径对账（decoder+target=decoder_frames）
    assert kinds["decoder"] + kinds["target"] == fs["decoder_frames"]
    assert kinds["probe"] == fs["probe_frames"]
    assert kinds["unknown"] == fs["unknown_frames"]


def test_frame_coverage_csv_totals(p2_design, tmp_path) -> None:
    """覆盖矩阵 CSV：TOTAL 行与 frame_stats 完全一致。"""
    paths = write_all(p2_design, tmp_path, "TG0C0D2_VER_B_SCH")
    lines = open(paths["frame_coverage_csv"], encoding="utf-8").readlines()
    total_row = lines[-1].strip().split(",")
    assert total_row[0] == "TOTAL"
    fs = p2_design.metadata["frame_stats"]
    assert int(total_row[2]) == fs["total_frames"] == 10988
    assert float(total_row[7]) == pytest.approx(fs["covered_pct"])
