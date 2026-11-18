## What this changes

<!-- One paragraph. What behaviour changes, and why. -->

## Area

- [ ] parser (DSN frames, symbols, side files)
- [ ] intermediate representation
- [ ] matcher and R1-R6 routing
- [ ] writer (layout and wiring)
- [ ] mock library and symbol assembly
- [ ] QA and golden comparison
- [ ] configuration, profiles, CLI
- [ ] GUI
- [ ] tests
- [ ] documentation

## Verification

<!-- Paste the commands you ran and their result. -->

```bash
ruff check .
mypy cis2hdl
pytest -q
```

## Output impact

- [ ] Default output is byte-identical to the previous commit
- [ ] Default output changes — described below, with the reason it is more correct

<!-- If output changes, state which files differ and why. -->

## Related issues

<!-- Fixes #123 -->
