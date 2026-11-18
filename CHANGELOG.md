# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] — 2026-09-03

### Added

- **Golden comparison layer** (`cis2hdl/core/qa/`): diffs the emitted `components.csv` /
  `pins.csv` / `nets.csv` against independent EXP exports and reports mismatch, missing and
  extra rows per level, including multi-section parts (one symbol split across several part
  references in the export).
- **Six-way match routing (R1–R6)**: exact library match, fuzzy match, mock symbol,
  non-electrical element, GND symbol and connector, each with its own placement and wiring
  rules. Reference-board distribution: R1 504, R2 48, R3 185, R4 6, GND 675, R6 321.
- **Mock cell generation**: parts without a real library symbol get a generated cell that
  reproduces the original geometry, with a single-pin-name template and zero degenerate
  segments. 191 cells on the reference board, zero empty pins.
- **Label handling**: rotation follows pin orientation; label templates fall back when a
  prefixed placeholder is not available.
- **Page-sheet compaction**: bounding-box outlier clamping prevents a single stray element from
  inflating the sheet, which previously produced unreadable pages.
- **Portable test fixtures**: reference-design paths are resolved through
  `tests/support.py` and the `CIS2HDL_DSN_ROOT` environment variable, so the suite runs from a
  fresh clone and skips (rather than fails) without the third-party design.

### Changed

- Package version unified: runtime `__version__` and distribution metadata now agree
  (previously `0.5.0` at runtime versus `1.1.0` in metadata).
- Console entry point restored: `cis2hdl` now resolves to a real callable, so the documented
  command works after `pip install`.
- Package discovery restricted to `cis2hdl*`; `tests`, `docs` and `scripts` are no longer
  installed as importable packages.

### Fixed

- Zero-length pin segments in generated symbol libraries.
- GND aggregation on pages with multiple ground symbols.
- MOCK-marked cells were indistinguishable from real cells in the mapping table.

### Known issues

- `SPCOCD-553`: symbol/cell naming consistency for part of the placeholder cells — affects
  Cadence-side loading of those cells, not the emitted structure. Under investigation.
- The GUI workbench has not been migrated to the current module layout.

## [0.5.0] — 2026-08-19

Initial public release: DSN binary parsing (OLE container, frames, symbols), intermediate
representation with archiving, two-stage component matching, Cadence-compatible writers
(`cpm`, `csa`, `con`, `xcon`, `cds.lib`), declarative `pipeline.yaml` with profiles, CLI with
`convert` / `verify` / `profile` / `gui`, and the staged diagnostics framework.

[1.1.0]: https://github.com/echoingme/cis2hdl_dsn_ver/releases/tag/v1.1.0
