# Documentation

Technical documentation for the DSN edition of CIS2HDL. Usage and configuration live in the
top-level [README](../README.md).

| Document | What it answers |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the converter is put together: DSN parsing model, intermediate representation, match routing R1–R6, writer stages, diagnostics, and where the golden-comparison layer sits. Start here. |
| [MATCHING.md](MATCHING.md) | How components are matched and routed: the two-stage matcher, the six routing outcomes, thresholds, edge cases, and the current reference-board numbers. |

## Where to look for what

| Question | Document, section |
|---|---|
| How is a DSN file structured and parsed? | ARCHITECTURE — parsing layer |
| What is in the intermediate representation? | ARCHITECTURE — IR |
| Why did a component take the mock route? | MATCHING — routing outcomes |
| How are thresholds and weights configured? | README — configuration, plus `pipeline.yaml` |
| How do I prove the output is correct? | README — reference-board results; `cis2hdl/core/qa/` |

## Note on references

Some sections cite earlier working records — development logs, phase plans, archived
research — that are kept outside this repository. Where a citation points to a file that is
not present, the surrounding text is the authoritative description; no behaviour depends on
those records.
