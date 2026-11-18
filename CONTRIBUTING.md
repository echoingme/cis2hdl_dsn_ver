# Contributing to CIS2HDL

## Development setup

```bash
git clone https://github.com/echoingme/cis2hdl_dsn_ver.git
cd cis2hdl_dsn_ver
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

Cadence is not required for development. The parser, matcher and writers are exercised against
synthetic frames and the fixtures in `tests/fixtures/`.

## Before opening a pull request

```bash
pytest tests/unit -q
python scripts/check_config.py
ruff check cis2hdl                # lint (not yet clean repository-wide)
mypy cis2hdl                      # strict typing on the package
```

The test suite and the configuration check must pass; lint and typing are expected to be clean
for the files you touch.

If your change touches output formats or wiring, also run the end-to-end layer, and if you
have a reference design available, run with `CIS2HDL_DSN_ROOT` set to include the
reference-board and golden-comparison tests.

## What a change is expected to include

| Change type | Expected evidence |
|---|---|
| Bug fix | A regression test that fails before the fix and passes after it |
| Parser / frame decoding | A frame-level unit test plus the effect on the golden comparison |
| Match routing change | Tests covering the route's positive and negative cases, plus the effect on `mapping.csv` |
| Layout or wiring change | Before/after description of the emitted geometry; byte-level comparison where possible |
| Output format change | Comparison against the reference output, documented in the PR |
| New configuration field | A default that preserves existing behaviour, plus documentation in the README profile table |

Existing output is treated as a compatibility contract: a change that alters default output
must say so explicitly and justify why the new result is more correct.

## Ground rules for this converter

- **Do not substitute inferred data for parsed data.** Values, footprints, attributes and
  coordinates come from the DSN. Side files (EXP / CSV) may validate, never invent.
- **A mock cell must reproduce the original geometry**, not approximate it.
- Diagnostics are part of the product: an unsupported record is reported, never silently
  dropped.

## Coding standards

- Python 3.12+, `from __future__ import annotations` in new modules.
- Ruff (`E,F,W,I,N,UP,B,C4,SIM`), line length 100 — see `pyproject.toml`.
- `mypy --strict` on `cis2hdl`; public functions fully annotated.
- Configuration belongs in `pipeline.yaml` and `profiles/`, not in code paths.
- Tests must not depend on absolute machine paths; use `tests/support.py` helpers.

## Commit messages

Imperative subject line, body describing motivation and the verification performed. Name the
affected area (`parser`, `IR`, `matcher`, `writer`, `cli`, `docs`) when it is not obvious.

## Documentation

Documents under `docs/` are part of the deliverable. If a change invalidates documented
behaviour, update the document in the same pull request.

## License

Contributions are accepted under the [MIT License](LICENSE).
