"""T05 三层归档 round-trip 保真测试（test_ir_archive.py）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support import data_path, design_path

DSN_PATH = design_path("DSN")


@pytest.fixture(scope="module")
def design() -> object:
    from cis2hdl.core.parser.dsn.dsn_parser import DSNParser

    if not Path(DSN_PATH).exists():
        pytest.skip(f"Test file not found: {DSN_PATH}")
    return DSNParser().parse(Path(DSN_PATH))


@pytest.fixture(scope="module")
def archive(tmp_path_factory, design) -> dict:
    from cis2hdl.core.writer.ir_archive_writer import write_all

    out = tmp_path_factory.mktemp("ir_archive")
    return write_all(design, out, "TG0C0D2_VER_B_SCH")


class TestIRJson:
    def test_ir_json_exists(self, archive) -> None:
        assert archive["ir_json"].exists()
        assert archive["ir_json"].stat().st_size > 100_000

    def test_roundtrip_pages(self, archive, design) -> None:
        data = json.loads(archive["ir_json"].read_text(encoding="utf-8"))
        assert len(data["pages"]) == len(design.pages)

    def test_roundtrip_instances(self, archive, design) -> None:
        data = json.loads(archive["ir_json"].read_text(encoding="utf-8"))
        ir_inst = sum(len(p["instances"]) for p in data["pages"])
        design_inst = sum(len(p.instances) for p in design.pages)
        assert ir_inst == design_inst

    def test_roundtrip_component_db(self, archive, design) -> None:
        data = json.loads(archive["ir_json"].read_text(encoding="utf-8"))
        assert len(data["component_db"]) == design.component_db.count

    def test_roundtrip_metadata(self, archive) -> None:
        data = json.loads(archive["ir_json"].read_text(encoding="utf-8"))
        fs = data.get("metadata", {}).get("frame_stats", {})
        assert fs.get("covered_pct", 0) >= 90


class TestSummaryMd:
    def test_summary_exists(self, archive) -> None:
        assert archive["summary_md"].exists()
        text = archive["summary_md"].read_text(encoding="utf-8")
        assert "TG0C0D2" in text or "解析摘要" in text

    def test_summary_has_pages(self, archive) -> None:
        text = archive["summary_md"].read_text(encoding="utf-8")
        assert "页面" in text


class TestBomCsv:
    def test_bom_exists(self, archive) -> None:
        assert archive["bom_csv"].exists()

    def test_bom_rows(self, archive) -> None:
        import csv

        with archive["bom_csv"].open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) >= 700  # 743 元件口径（含 GND 符号则更多）

    def test_bom_columns(self, archive) -> None:
        import csv

        with archive["bom_csv"].open(encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = reader.fieldnames or []
        for col in ("Reference", "Value", "X", "Y", "Page"):
            assert col in fieldnames, f"missing BOM column: {col}"


class TestNetConnectionsCsv:
    def test_net_connections_exists(self, archive) -> None:
        assert archive["net_connections_csv"].exists()

    def test_net_connections_rows(self, archive) -> None:
        import csv

        with archive["net_connections_csv"].open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) >= 2000  # ≥2189 目标（gold 口径 1515+ 待补）
