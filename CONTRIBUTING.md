# Contributing

> **Owner:** Jonathon Tamm · **Review cadence:** as conventions change.

## Branching & PRs
- Work in branches, never on `master`. Naming: `type/short-description` (e.g. `feat/sxbet-eip712`, `fix/kalshi-cache-race`, `docs/...`).
- Open a PR to `master`. CI runs `pytest` on Python 3.12 and **fails on any test failure or error** (zero tolerance).
- A push to `master` auto-deploys to Railway and restarts the live worker — see `RUNBOOK.md` for the controlled-merge protocol.
- Never `--no-verify`; never force-push `master`.

## Code style (see CLAUDE.md "Code Style" for the full list)
- Python 3.11+; modern unions (`X | None`, `list[float]`) — never `Optional`/`List`/`Dict`/`Tuple` from `typing`.
- Double quotes; ~120-char soft limit; `%`-style logging (`executor.py` excepted, uses f-strings).
- 75-dash section separators between logical sections.
- Relative imports within `scans/`; absolute elsewhere.

## Testing
- `pytest` + `unittest.mock`. All tests are methods inside classes (no module-level test functions).
- No `conftest.py`; shared setup via per-file `autouse` fixtures.
- External SDKs are mocked via `sys.modules` stubs before importing the module under test (see `tests/test_executor.py`).
- `autouse` cleanup must remove only the specific scan module under test (e.g. `scans.gemini`) — never `scans.helpers`/`scans.__init__` (cross-test pollution).
- Known-flaky: `tests/test_helpers.py::TestWithinResolutionWindow::test_uses_config_default` (time-sensitive).
- Run one: `pytest tests/test_fees.py::TestPolymarketFee::test_zero_when_sell_equals_buy -v`.

## Adding a new opportunity type
1. Create the scan in `scans/<name>.py` (two-stage: mid-price → CLOB refine).
2. Add the fee function in `fees.py`.
3. Add a branch in `executor.py:_build_legs()` + a matching `_revalidate` case.
4. Wire into `cli.py:_run_oneshot()` and `continuous.py` if applicable.
5. Add the mode string to `cli.py` `--mode` argparse choices.
6. Default the feature flag to `false`.

## Adding a cross-platform pair
Add to `_CROSS_FEE_FUNCS` in `scans/cross.py` via `functools.partial(net_profit_cross_generic, buy_fee, sell_fee)`. All 28 pairs of the 8 trading platforms are already covered.

## Definition of done
See `TASK_CONTRACT.md` — compiles, tests pass, behavior verified, no stubs.
