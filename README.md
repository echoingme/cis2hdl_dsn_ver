# CIS2HDL — DSN edition

**Turn an OrCAD Capture CIS schematic into a Cadence Design Entry HDL project, treating the
binary `.dsn` as the single source of truth.**

[![Python](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-544%20passing-brightgreen.svg)](tests/)
[![Status](https://img.shields.io/badge/status-v1.1.0-orange.svg)](CHANGELOG.md)

CIS2HDL reads a Capture CIS design, resolves every component against a Cadence Design Entry
HDL symbol library, reproduces the schematic geometry, and emits a project that opens in
Cadence SPB 16.6+ for editing.

The design principle that shapes everything here: **nothing is inferred that can be read.**
Component values, footprints, attributes, coordinates, wires, text and graphics all come from
the DSN database. Exported side files (EXP / CSV) are used only to *check* the result, never to
substitute for it — which is why the converter carries a golden-comparison layer that can diff
its own output against an independently exported reference and report mismatches per
component, per pin and per net.

---

## What it does

```
DSN ──► binary parse ──► IR ──► match routing R1–R6 ──► symbol assembly ──► DEHDL project
                                   │                          │
                         component → library cell      1:1 geometry replay;
                         exact / fuzzy / mock /        mock cells for parts
                         non-electrical / GND /        with no real symbol
                         connector
```

**Parsing.** The DSN container (an OLE compound file) is read frame by frame: pages, instances,
properties, pin labels, net tables, wires, text blocks and library symbols. The parser is
covered by frame-level tests, so a format difference in another Capture release surfaces as a
failing assertion rather than a silently wrong schematic.

**Matching and routing.** Every instance lands in one of six outcomes — exact library match,
fuzzy match, mock symbol, non-electrical element, GND symbol, connector — and each route has
its own placement and wiring rules. Parts without a real library symbol get a generated mock
cell that reproduces the original geometry rather than an approximation.

**Layout and wiring.** Placement from DSN coordinates, orthogonal wiring with lane allocation,
stub generation, overlap detection and resolution, GND clustering and distribution, off-page
connector edge layout, and label placement with collision handling.

**Output.** A Cadence Project Manager tree — `<cell>.cpm`, `cds.lib`, `temp/`,
`worklib/<cell>/sch_1/pageN.csa`, `.con`, `.xcon` — plus `mapping.csv`, error logs, aesthetic
and IOPORT audit reports, and a generated symbol library carrying every mock cell.

**Verification.** A golden-comparison module (`cis2hdl/core/qa/`) diffs the emitted
`components.csv` / `pins.csv` / `nets.csv` against an independent EXP export and reports
mismatch, missing and extra rows at all three levels.

## Reference-board results

Measured against a 24-page industrial board (the design itself is not redistributed):

| Metric | Result |
|---|---|
| Component routing | R1 504 exact · R2 48 fuzzy · R3 185 mock · R4 6 non-electrical · GND 675 · R6 321 connector |
| Exact conversion | 1,500 nets (86.26%) |
| Golden comparison (components / pins / nets) | all passed |
| Emitted project | 83 files, 0 errors |
| Generated mock library | 191 cells, 0 empty pins |
| Local test run | 544 passing; 185 reference-board tests skip without the design |

## Installation

Python 3.12+ is required; Cadence is only needed to open the generated project.

```bash
git clone https://github.com/echoingme/cis2hdl_dsn_ver.git
cd cis2hdl_dsn_ver
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## Usage

```bash
# convert with the default profile
cis2hdl convert board.DSN --output out/ --hdl-lib /path/to/hdl_library

# use a profile, or an explicit pipeline configuration
cis2hdl convert board.DSN --profile max-beauty --output out/
cis2hdl convert board.DSN --pipeline my-project.yaml --output out/

# inspect, create and share profiles
cis2hdl profile list
cis2hdl profile show debug
cis2hdl profile create my-board --from-file pipeline.yaml
cis2hdl profile export my-board -o my-board.yaml

# run the verification suites
cis2hdl verify --suite unit --suite e2e

# open the GUI workbench
cis2hdl gui
```

Configuration is declarative: `pipeline.yaml` describes the input plugins, match weights and
thresholds, beautification passes, output file set and test suites. A profile overlays a
partial configuration on top of it, and path-level flags (`--output`, `--hdl-lib`,
`--extra-hdl-lib`) override both for a single run.

| Profile | Intent |
|---|---|
| `default` | Behaviour-equivalent to the validated baseline |
| `fast` | Skips reports and verification for quick iteration |
| `match-only` | Matching and export only — no schematic is emitted |
| `max-beauty` | `detour` routing with wire simplification and text layout |
| `debug` | Verbose diagnostics and intermediate artefacts, for tracing a conversion |

## Repository layout

```
cis2hdl/
├── cis2hdl/
│   ├── cli.py · __main__.py · verify.py
│   ├── core/
│   │   ├── parser/          # DSN container, frames, symbols, EXP/CSV side files
│   │   ├── intermediate/    # IR construction and archiving
│   │   ├── matcher/         # two-stage matching, R1–R6 routing
│   │   ├── writer/          # cpm · csa · con · xcon · router · wire layout · mock library
│   │   ├── diagnostics/     # staged diagnostics and error codes
│   │   ├── qa/              # golden comparison against reference exports
│   │   ├── validator/       # net · pin · power checks
│   │   └── engine/          # conversion pipeline
│   ├── plugins/             # input · match · beautify · output · test stages
│   └── gui/                 # PySide6 workbench
├── profiles/                # default · fast · match-only · max-beauty · debug
├── tests/                   # unit · e2e · fixtures · support helpers
├── docs/                    # architecture and matching documentation
└── pipeline.yaml            # authoritative configuration
```

## Testing

```bash
pytest tests -q                               # 544 tests, no external data required
CIS2HDL_DSN_ROOT=/path/to/design pytest -q    # adds the 185 reference-board tests
```

The suite splits by what it needs:

- **Design-independent tests** run anywhere: parsers against synthetic frames, matchers,
  writers, configuration, CLI and plugin contracts.
- **Reference-board tests** parse a real 24-page design and assert frame counts, geometry and
  golden-comparison results. They skip unless `CIS2HDL_DSN_ROOT` points at a directory
  containing `<design>.DSN` together with its `.EXP` / `.CSV` exports — that board is
  third-party data and is not redistributed here.

## Compatibility and limitations

- **Python 3.12+**, developed on Windows where Cadence lives. The converter is
  platform-independent; path defaults are not.
- **Cadence SPB 16.6+** is required to open the output. Projects are checked by opening them in
  16.6 and comparing page and primitive counts against the CIS original.
- DSN coverage depends on the Capture version that wrote the file; unsupported records are
  reported as diagnostics rather than dropped.
- One item is still open: symbol/cell naming consistency for a subset of placeholder cells
  (`SPCOCD-553`). It affects how Cadence loads those cells, not the emitted structure.
- The GUI workbench predates the current module layout and is being updated; the CLI and the
  pipeline configuration are the supported interface.

## Documentation

| Document | Contents |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Layer diagram, DSN parsing model, IR, matching routes, writer stages |
| [docs/MATCHING.md](docs/MATCHING.md) | Two-stage matching, the six routing outcomes, thresholds, edge cases |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development loop, standards, what a change must demonstrate |

## Related

- **[CIS2HDL_plugin_ver](https://github.com/echoingme/CIS2HDL_plugin_ver)** — the upstream
  plugin-architecture line: the same conversion engine organised as a composable stage
  pipeline, focused on the DSN parser and the reference-comparison tooling.

## License

[MIT](LICENSE) © 2026 Zhichen Rao. Citation metadata in [CITATION.cff](CITATION.cff).
