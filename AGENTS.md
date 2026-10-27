# AGENTS.md

Instructions for AI coding agents operating in this repository.

This repository supports autonomous trading services, but execution remains governed by current runtime configuration, venue eligibility, operator-owned risk limits, and kill-switch controls. Repository documentation is not transaction authority.

## Project Overview

Python CLI tool that scans for arbitrage opportunities across prediction markets. Supports one-shot scans, continuous mode with WebSocket feeds, and automated trade execution. 

**Platforms**: Polymarket, Kalshi, Betfair, Smarkets, SX Bet, Matchbook, Gemini Predictions, IBKR ForecastEx (+ Metaculus as read-only signal source)

## Commands

```bash
# Install dependencies
pip install -r requirements.txt
pip install -r requirements-dev.txt   # pytest (dev only)

# Run one-shot scan (all arb types)
python scanner.py

# Continuous mode
python scanner.py --continuous --interval 60

# Specific modes
python scanner.py --mode kalshi
python scanner.py --mode cross-all

# Execution controls
python scanner.py --dry-run                         # detect only (default)
# Live Kalshi D0: scripts/launch-kalshi-d0-live.sh
# DRY_RUN=false requires artifacts/live-envelope.json (five operator fields).
# Do not invent venue, pair, max_notional_usd, max_daily_loss_usd, or kill_switch.
```

## Testing

Tests use `pytest` with `unittest.mock`. All tests are methods inside classes (no module-level test functions). No `conftest.py` exists; shared setup uses per-file `autouse` fixtures.

```bash
# Run all tests
python -m pytest tests/ -v                      # full suite

# Run a single test file
python -m pytest tests/test_fees.py -v          # single file

# Run a single class
python -m pytest tests/test_executor.py::TestExecutor -v

# Run a single test
python -m pytest tests/test_fees.py::TestPolymarketFee::test_zero_when_sell_equals_buy -v

# Options
python -m pytest tests/ -v --tb=short           # short traceback
python -m pytest tests/ -v -x                   # stop on first failure
```

**Testing Patterns**:
- External SDKs are mocked via `sys.modules` stubs before importing the module under test.
- Tests add the project root to `sys.path`.
- `autouse` fixtures that clean `sys.modules` must only remove the specific scan module under test (e.g. `scans.gemini`), **never** `scans.helpers` or `scans.__init__` to prevent cross-test pollution.

## Code Style

No linter or formatter is configured. Style is enforced by convention only.

### Python Version & Typing
- Target **Python 3.11+**.
- Use modern union syntax: `X | None`, `list[float]`, `tuple[bool, str]`.
- **Never** use `Optional`, `List`, `Dict`, `Tuple` from `typing`.

### Naming Conventions
- Functions/methods: `snake_case` (`scan_binary_internal`, `_refine_cross_with_clob`).
- Classes: `PascalCase` (`ArbitrageExecutor`, `TradeDB`, `RiskManager`).
- Module-level constants: `UPPER_CASE` (`MIN_NET_ROI`, `MAX_TRADE_SIZE`).
- Private constants: `_UPPER_CASE` (`_CROSS_FEE_FUNCS`, `_VALID_PLATFORMS`).
- Internal dict keys: `_`-prefixed (`_token_ids`, `_clob_depth`, `_market_key`).

### Formatting & Layout
- ~120-character soft line limit; no hard enforcement.
- 4-space indentation, no tabs.
- Double quotes for strings (dominant convention).
- Section separators: Use `# ---------------------------------------------------------------------------` (75 dashes) to separate logical sections. 

### Imports
- Standard library → third-party → local, separated by blank lines.
- Relative imports within `scans/` package (`from .helpers import ...`).
- Absolute imports everywhere else (`from fees import net_profit_binary`).

### Logging & Error Handling
- Use `logging` with `%`-style formatting: `logger.info("Found %d opps in %s", count, market)`.
- `executor.py` is the sole exception (uses f-strings in log calls).
- Every module creates its own logger: `logger = logging.getLogger(__name__)`.
- **Never** add bare `except: pass`. Always log at `logger.debug()` minimum.
- Custom exceptions: `ConfigError(ValueError)` in `config.py`, `_RateLimitError(Exception)` in `kalshi_api.py` and `polymarket_api.py`.
- Guard optional dependency imports with `except ImportError`.

### Docstrings
- Google-style docstrings with `Args:` / `Returns:` sections.

### Data Structures
- Opportunities flow as **plain dicts** (not dataclass/TypedDict).
- Internal keys are `_`-prefixed.
- Public keys: `type`, `market`, `prices`, `total_cost`, `net_profit`, `net_roi`.

## Key Conventions

### scanner.py is a facade
Never add logic to `scanner.py`. It re-exports names from `scans/`, `cli.py`, `continuous.py`, and `display.py`. Tests patch `scanner.<name>` which hits these re-exports.

### Two-stage detection
All scan modules follow: mid-price scan (fast) → CLOB refinement (`_refine_*_with_clob`). The refine step drops candidates that aren't profitable at real ask prices.

### Thread safety
- `TradeDB`: threading lock + SQLite WAL mode.
- Price cache: plain dict updated from WS threads.
- Per-market locks in continuous mode prevent double execution.

### Config
`config.py` uses typed env helpers (`_env_float`, `_env_int`, `_env_bool`) that raise `ConfigError` on invalid input. Precedence: CLI args > env vars > defaults in `config.py`.

### Live Kalshi D0
The currently verified operating target is live Kalshi D0 via `scripts/launch-kalshi-d0-live.sh`. `DRY_RUN=false` fail-closes unless `artifacts/live-envelope.json` contains venue, pair, max_notional_usd, max_daily_loss_usd, and kill_switch from one operator message. Agents do not invent those fields. Other adapter-backed venues need their own validated authority, account, eligibility, limit, emergency-stop, and action-time approval gates. User-reported access is not independent eligibility verification. `kalshi_policy.live_kalshi_submit_allowed` guards every Kalshi submit path. LLM agents do not pick quotes.

## Adding a New Opportunity Type
1. Create the scan in `scans/<name>.py` following the two-stage pattern.
2. Add the fee function in `fees.py`.
3. Add a branch in `executor.py:_build_legs()` and a matching `_revalidate` case.
4. Wire into `cli.py:_run_oneshot()` and `continuous.py` if applicable.
5. Add the mode string to `cli.py` argparse choices.

## Adding a New Cross-Platform Pair
Add entries to `_CROSS_FEE_FUNCS` in `scans/cross.py` using `functools.partial(net_profit_cross_generic, buy_fee, sell_fee)`.

## Agent Delivery Workflow

### Isolate and scope the work
- Refresh `origin/master`, then inspect `git status`, `git worktree list`, and open PRs before editing. Work from a dedicated task branch and isolated worktree based on the current `origin/master`; never edit, clean, stash, or reset the dirty canonical checkout.
- Use one branch per objective. If another branch, worktree, or PR overlaps the same files or subsystem, coordinate or serialize the work instead of racing it.
- Keep one PR to one purpose. Before commit and again before the PR, inspect `git status`, `git diff --stat`, and the complete diff. Exclude unrelated code, formatting, generated artifacts, dependencies, and secrets.

### Verify, review, and hand off
- For every change, run `git diff --check`. For `.coderabbit.yaml`, also run `ruby -e 'require "yaml"; YAML.parse_file(".coderabbit.yaml")'`.
- For Python or CI behavior changes, use Python 3.12 and run the same correctness gates as `.github/workflows/test.yml`: `ruff check . --select E9,F63,F7,F82` and `pytest tests/ -v --tb=short`. Add the narrowest relevant regression test first when practical; do not weaken, skip, or relabel checks to obtain green status.
- Write an imperative, outcome-specific PR title. The PR body must explain why, what changed, exact verification and results, deployment or trading risk, and deferred work. State every unrun check.
- CodeRabbit reviews every non-draft PR incrementally. Address actionable findings with narrow fixes, push, and wait for CodeRabbit and `test` again. Do not resolve a thread without fixing it or recording why it is non-actionable.
- Before handoff, fetch `origin/master`, confirm the branch is current, inspect the final diff, and rerun affected verification after any branch update or conflict resolution. Required `test` and `CodeRabbit` checks, conversation resolution, and a mergeable state are mandatory; never bypass protection or force-push.

### Depot, merge, and production boundaries
- Depot supplies the managed runner only for `.github/workflows/test.yml`. Keep scheduled monitoring, market scans, digests, communications, and any deployment workflow off Depot unless a separate, explicit infrastructure task authorizes the change. Depot account settings, runner groups, cache or network policy, and usage caps are external account changes, not part of repository implementation.
- Never exercise a write-capable trading path during validation. Do not run with `DRY_RUN=false`, `--exec-mode full-auto`, a live launcher, order submission, fund movement, or production credentials. Tests must use mocks or read-only/dry-run paths.
- Stop at a review-ready PR for changes involving live order placement, risk or loss limits, funds, credentials or secrets, permissions or authentication, account or provider configuration, migrations, production infrastructure or configuration, scheduled external communications, or other destructive or hard-to-reverse effects. Obtain explicit action-time confirmation for the exact effect.
- Low-risk changes may use GitHub-native auto-merge only when the final diff is limited to `docs/**`, `tests/**`, and Markdown files other than `AGENTS.md` or `CLAUDE.md`; the branch is current with `master`; required `test` and `CodeRabbit` checks pass on the latest commit; every actionable review finding is resolved; and GitHub reports the PR mergeable. Changes to `AGENTS.md`, `CLAUDE.md`, `.coderabbit.yaml`, or any other delivery or review control require explicit action-time approval. Immediately before arming auto-merge, use read-only operating evidence to verify the Railway worker is dry-run and no opportunity is mid-execution; if that quiescent state cannot be verified, stop at the PR. Do not change trading state to manufacture a merge window. The ordinary Railway restart triggered by a qualifying merge is authorized as part of this repository's autonomous lane; runtime, dependency, workflow, launcher, broker, trading, risk, Docker, and Railway changes remain outside it. Do not create a workflow that bypasses GitHub branch protection or polls around a missing required check.
- After an autonomous or explicitly authorized merge, verify the remote result with `gh pr view <PR> --json state,mergedAt,mergeCommit,statusCheckRollup`, fetch `origin/master`, confirm the merge commit is an ancestor, inspect the latest `Tests` run on `master`, and read back `https://arb-scanner-production.up.railway.app/healthz`. A merged commit or green PR checks alone do not prove a healthy deployment. If the deployment or health check fails, stop further autonomous delivery, report the failure, and follow only a repository-documented rollback path; never exercise a live trading path as a deployment test.

## Files to Never Read or Commit
`.env`, `.env.*`, `*.pem`, `*.key`, `*credential*`, `secrets/*`

## CI / CD & Deployment
- `.github/workflows/test.yml` — correctness lint plus pytest on PRs to `master` (Python 3.12); any failure or collection crash fails CI.
- Railway auto-deploys on push to `master` via GitHub integration. Dockerfile-based build (`python:3.12-slim`).

## Local Firecrawl KBs

Sticky venue/docs mirrors: `~/Financial Markets with AI/.firecrawl/_kb/` (Kalshi API,
Polymarket public docs — shadow/data only). Registry:
`~/.claude/references/firecrawl-kb-registry.md`. Read the corpus `index.md`, then only
the pages you need. Do not preload whole trees. Polymarket international remains
non-executable per portfolio allowlist.

## Mem0 (cross-tool)

Mem0 `app_id=johnsnow92-polymarket-arb-scanner`. Search this app_id plus `personal-prefs` at session start before rediscovering durable decisions.
Personal prefs → `personal-prefs`. Open items → Linear. Human notes → Obsidian. Claude Code session learnings → native MEMORY.md.
Policy: `~/Dev/mem0-knowledge-base/FULL-LEVERAGE.md`.
