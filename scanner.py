#!/usr/bin/env python3
"""Polymarket Arbitrage Scanner.

Slim orchestrator — re-exports scan functions, display, and CLI for backward
compatibility.  Actual implementations live in:

    scans/binary.py     — Binary internal scan + CLOB refinement
    scans/negrisk.py    — NegRisk internal scan + CLOB refinement
    scans/cross.py      — Cross-platform and cross-all scans
    scans/kalshi.py     — Kalshi binary + multi-outcome scans
    scans/helpers.py    — Shared helpers (_extract_token_ids, _parallel_fetch_kalshi)
    display.py          — Table / JSON output formatting
    continuous.py       — Continuous mode loop, settlement, WS management
    cli.py              — Argument parsing and main() entry point
"""

import argparse  # noqa: F401 — tests access scanner.argparse
import logging
import os
import sys  # noqa: F401 — tests access scanner.sys.modules

# CLI --dry-run must win over DRY_RUN=false in the environment before config
# import (validate_config runs at import and requires a live envelope).
if "--dry-run" in sys.argv:
    os.environ["DRY_RUN"] = "true"

# Re-export scan functions so existing imports (e.g. ``import scanner``) keep working.
from scans.helpers import _extract_token_ids, _fetch_clob_for_market, _parallel_fetch_kalshi  # noqa: F401
from scans.binary import scan_binary_internal, _refine_binary_with_clob  # noqa: F401
from scans.negrisk import scan_negrisk_internal, _refine_negrisk_with_clob, scan_negrisk_no_side  # noqa: F401
from scans.cross import (  # noqa: F401
    scan_cross_platform,
    scan_cross_all,
    _refine_cross_with_clob,
    _refine_cross_all_with_clob,
    _attach_exec_metadata,
    _CROSS_FEE_FUNCS,
)
from scans.kalshi import scan_kalshi_binary, scan_kalshi_multi, _fetch_kalshi_data  # noqa: F401
from scans.spread import scan_spread_polymarket  # noqa: F401
from scans.betfair import scan_betfair_backall, scan_betfair_backlay  # noqa: F401
from scans.smarkets import scan_smarkets_backall, scan_smarkets_backlay  # noqa: F401
from scans.sxbet import scan_sxbet, scan_sxbet_backall, scan_sxbet_backlay  # noqa: F401
from scans.matchbook import scan_matchbook_backall, scan_matchbook_backlay  # noqa: F401
from scans.gemini import scan_gemini_binary, scan_gemini_multi  # noqa: F401
from scans.ibkr import scan_ibkr_binary  # noqa: F401
from scans.triangular import scan_triangular  # noqa: F401
from scans.multi_cross import scan_multi_cross  # noqa: F401
from scans.stale import scan_stale_prices  # noqa: F401
from scans.resolution import scan_resolution_snipes  # noqa: F401
from scans.convergence import scan_convergence  # noqa: F401
from scans.fee_promo import scan_fee_promo  # noqa: F401
from scans.cross_mm import scan_cross_mm  # noqa: F401
from scans.frechet import scan_frechet, _refine_frechet_with_clob  # noqa: F401
from scans.temporal import scan_temporal_arb, _refine_temporal_with_clob  # noqa: F401
from scans.ctf import scan_ctf, _refine_ctf_with_clob  # noqa: F401
from scans.rewards import scan_limitless_rewards  # noqa: F401
from display import display_results as _display_results  # noqa: F401
from continuous import check_settlements as _check_settlements, run_continuous as _run_continuous  # noqa: F401
from cli import main, _run_oneshot  # noqa: F401

# Re-export names that tests patch on the ``scanner`` module.
from polymarket_api import get_clob_prices  # noqa: F401
from matcher import match_cross_platform, match_cross_platform_semantic, match_markets_to_events_semantic  # noqa: F401
from fees import net_profit_binary_internal  # noqa: F401

__all__ = [
    "main",
    "_run_oneshot",
    "_run_continuous",
    "_check_settlements",
    "_display_results",
    "scan_binary_internal",
    "_refine_binary_with_clob",
    "scan_negrisk_internal",
    "_refine_negrisk_with_clob",
    "scan_negrisk_no_side",
    "scan_cross_platform",
    "scan_cross_all",
    "_refine_cross_with_clob",
    "_refine_cross_all_with_clob",
    "_attach_exec_metadata",
    "_CROSS_FEE_FUNCS",
    "scan_kalshi_binary",
    "scan_kalshi_multi",
    "_fetch_kalshi_data",
    "scan_spread_polymarket",
    "scan_betfair_backall",
    "scan_betfair_backlay",
    "scan_smarkets_backall",
    "scan_smarkets_backlay",
    "scan_sxbet_backall",
    "scan_sxbet_backlay",
    "scan_matchbook_backall",
    "scan_matchbook_backlay",
    "scan_gemini_binary",
    "scan_gemini_multi",
    "scan_ibkr_binary",
    "scan_triangular",
    "scan_multi_cross",
    "scan_stale_prices",
    "scan_resolution_snipes",
    "scan_convergence",
    "scan_fee_promo",
    "scan_cross_mm",
    "scan_frechet",
    "_refine_frechet_with_clob",
    "scan_temporal_arb",
    "_refine_temporal_with_clob",
    "scan_ctf",
    "_refine_ctf_with_clob",
    "scan_limitless_rewards",
    "get_clob_prices",
    "match_cross_platform",
    "match_cross_platform_semantic",
    "match_markets_to_events_semantic",
    "net_profit_binary_internal",
    "_extract_token_ids",
    "_fetch_clob_for_market",
    "_parallel_fetch_kalshi",
]

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    main()
