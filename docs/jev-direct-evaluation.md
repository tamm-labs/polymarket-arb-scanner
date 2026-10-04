# Direct TypeSafe integration and research evidence

Status: 2026-09-20 America/Detroit (2026-09-21 UTC). Review branch only;
no production configuration, deployment, live-trading flags, or capital changed.

## What is implemented

Jev uses `https://api.typesafe.ai/v1/systemone` and `jev-1.13.0` directly.
`TYPESAFE_MODEL` overrides the model. `OPENROUTER_API_KEY` is never used by Jev.
Set `TYPESAFE_API_KEY` through a secret manager, or set `TYPESAFE_API_KEY_FILE`
to an existing owner-only local file. Symlinks, public files, endpoint overrides,
redirects and malformed typed answers fail closed. No key belongs in this repo,
command-line arguments, reports, or logs. Availability means configured, not authenticated;
the live synthetic test below independently verified authentication.

News and equivalence judgments share frozen questions and an uncertainty option.
Operational acceptance requires confidence >=0.90; identical-contract acceptance
also requires Noul >=0.85 and full rules on both sides. These thresholds are
research settings, not measured guarantees. Discovery propagates rules and keys
Jev's cache by IDs, rules, model and prompt version. Human promotion of discovered
pairs remains required. Missing Kalshi event-level rules suppress Jev acceptance;
contract-level hydration is future work, not guessed from an event title.

The Jev-directed crypto strike scanner (`scans/jev_crypto.py`), its research
monitor (`scripts/jev_crypto_monitor.py`) and the single-contract demo were
removed on 2026-09-28: Jev may not decide trades, so no scan may emit a
Jev-forecast opportunity. The executor still refuses any `_research_only` or
Jev-sourced opportunity in live mode. Existing `jev_decisions` rows remain
readable by the resolution-sync and calibration tooling below.

The paper probe exposed a shared order-book parser assumption: arrays were assumed
best-first. The parser now selects minimum asks / maximum bids across valid levels
and sums size at that price. This affects all users of that helper and requires
review before deployment. The first probe's four records used the old parser and
are excluded because they lack the new quote-method provenance.

Reports use one earliest eligible observation per market within the latest 2,000
resolved rows, not lifetime totals or independent economic events. They require
pre-expiry timestamps, model/prompt/rule hashes, quote provenance, and final UMA
resolution evidence. Resolution lookup uses exact condition ID, checks final
`umaResolutionStatus`, explicit YES/NO labels and exact 1/0 settlement prices;
closed, proposed, disputed, 0.99-price and split-settlement records are excluded.
The recorded resolution time is when finality was observed, not the event time.
Legacy/manual labels without that provenance do not enter evaluation.

Hypothetical returns use actual side asks, dollar budget / entry price for contract
quantity, fees and explicitly supplied slippage. Missing inputs are excluded, never
imputed as zero costs. Fees remain configured assumptions, not verified current
venue charges. Top-level depth is recorded but fills, market impact, queue position
and volume capacity are not established. No report can set `live_ready=true`.

## Verified results

The frozen 24-case synthetic fixture set contains 12 news and 12 equivalence cases.
The direct API completed 24/24 with zero errors: 23/24 labels correct (95.8%) versus
14/24 (58.3%) for the simple frozen keyword/exact-rules baseline. Nine cases met the
acceptance rules (37.5% coverage), all correctly. There were zero false-equivalence
and zero false-resolution labels in this small set. The remaining error was a
conservative uncertain judgment instead of divergent. p95 request latency was
338.38 ms; reported usage was 13,338 input and 1,501 output tokens. The provider
response did not establish billed dollar cost; no cost saving is claimed.

These fixtures were authored alongside this implementation: they are smoke tests,
not an independent holdout, a stronger-model comparison, or profit evidence.

A bounded public-data paper scan after the quote correction made five decisions
and persisted five observations across BTC and ETH. No orders were submitted.
The earlier probe rejected one invalid decision distribution rather than accepting
it. The corrected run had no such errors. No observations have eligible settled
outcomes yet, so calibration and return evidence remain pending. Slippage was not
invented; those records remain cost-incomplete.

Operating truth: master started at `a79ff82fe96620a47087aebb0df0741a4a7c7732`;
GitHub development is active while both Linear projects were still Paused during
this session's readback. The public health endpoint returned OK, which does not
identify deployed revision, enabled flags, account state, balances or P&L. Those
production facts remain unverified. Existing command-center July status must not
be treated as current operating evidence.

## Reproduce safely

Run with Python 3.12 from the repository root. No scheduler is installed.

```bash
# Offline fixture validation; no provider call.
python scripts/jev_semantic_benchmark.py --output /tmp/jev-offline.json

# Paid direct-provider smoke test, using an explicitly configured private key file.
TYPESAFE_API_KEY_FILE=/absolute/private/key-file \
  python scripts/jev_semantic_benchmark.py --live --max-cases 24 --output /tmp/jev-live.json

# Read public finality and update only that explicitly selected research database.
python scripts/sync_jev_resolutions.py --db /absolute/research/jev.db --limit 100
python scripts/jev_calibration_report.py --db /absolute/research/jev.db --json
```

Supply `JEV_PAPER_SLIPPAGE_BPS` only when an operator has selected an explicit
simulation assumption. Its omission intentionally prevents cost-complete returns.
Do not treat that assumption or model-implied EV as an executable edge.

## Remaining evidence gates

Freeze the prompts and build an independently labeled, chronological prospective
set with event grouping. Compare with existing rules, a stronger reasoning model,
and separately validated statistical forecasts; include uncertainty and ablations.
The current descriptive report does not perform that inference. At least 300
settled distinct markets remains a sample floor, not statistical independence or
permission for capital. Verify current venue fees, liquidity and realistic fills
before economic conclusions. Record actual review effort and billed costs before
claiming research savings. Production reconciliation and exact merge/deployment
approval remain separate from this local implementation.

References: [TypeSafe API](https://docs.typesafe.ai/api),
[confidence](https://docs.typesafe.ai/confidence),
[model limitations](https://docs.typesafe.ai/model-jaggedness/jev-1.13),
[Polymarket market schema](https://github.com/Polymarket/py-sdk/blob/main/src/polymarket/models/gamma/market.py),
[resolution process](https://help.polymarket.com/en/articles/13364518-how-are-prediction-markets-resolved).
