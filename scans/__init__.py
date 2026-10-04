"""Scan modules for different arbitrage types."""

from scans.binary import scan_binary_internal, _refine_binary_with_clob
from scans.negrisk import scan_negrisk_internal, _refine_negrisk_with_clob, scan_negrisk_no_side
from scans.cross import (
    scan_cross_platform,
    scan_cross_all,
    _refine_cross_with_clob,
    _refine_cross_all_with_clob,
    _attach_exec_metadata,
    _CROSS_FEE_FUNCS,
)
from scans.kalshi import scan_kalshi_binary, scan_kalshi_multi, _fetch_kalshi_data
from scans.spread import scan_spread_polymarket
from scans.betfair import scan_betfair_backall, scan_betfair_backlay
from scans.smarkets import scan_smarkets_backall, scan_smarkets_backlay
from scans.sxbet import scan_sxbet, scan_sxbet_backall, scan_sxbet_backlay
from scans.matchbook import scan_matchbook_backall, scan_matchbook_backlay
from scans.gemini import scan_gemini_binary, scan_gemini_multi
from scans.ibkr import scan_ibkr_binary
from scans.triangular import scan_triangular, scan_nway_arb
from scans.bracket import scan_bracket_arb, _refine_bracket_with_clob
from scans.multi_cross import scan_multi_cross
from scans.stale import scan_stale_prices
from scans.resolution import scan_resolution_snipes
from scans.convergence import scan_convergence
from scans.rewards import scan_polymarket_rewards, scan_kalshi_rewards, scan_limitless_rewards
from scans.fee_promo import scan_fee_promo
from scans.cross_mm import scan_cross_mm
from scans.lead_lag_mm import scan_lead_lag_mm
from scans.toxic_flow_pause import scan_toxic_flow_pause
from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
from scans.conditional import scan_conditional_arb, _refine_conditional_with_clob
from scans.settlement_timing import scan_settlement_timing, _refine_settlement_with_clob
from scans.new_market import scan_new_market_mispricing
from scans.api_outage import scan_api_outage_arb
from scans.social_sentiment import scan_social_sentiment
from scans.expert_divergence import scan_expert_divergence
from scans.insider_pattern import scan_insider_pattern, get_order_flow_tracker
from scans.cross_category import scan_cross_category, get_signal_fetcher
from scans.frechet import scan_frechet, _refine_frechet_with_clob
from scans.temporal import scan_temporal_arb, _refine_temporal_with_clob
from scans.ctf import scan_ctf, _refine_ctf_with_clob
from scans.helpers import _extract_token_ids, _fetch_clob_for_market, _parallel_fetch_kalshi, capital_efficiency_score

__all__ = [
    "scan_binary_internal",
    "scan_negrisk_internal",
    "scan_negrisk_no_side",
    "scan_cross_platform",
    "scan_cross_all",
    "scan_kalshi_binary",
    "scan_kalshi_multi",
    "scan_spread_polymarket",
    "scan_betfair_backall",
    "scan_betfair_backlay",
    "scan_smarkets_backall",
    "scan_smarkets_backlay",
    "scan_sxbet",
    "scan_sxbet_backall",
    "scan_sxbet_backlay",
    "scan_matchbook_backall",
    "scan_matchbook_backlay",
    "scan_gemini_binary",
    "scan_gemini_multi",
    "scan_ibkr_binary",
    "scan_triangular",
    "scan_nway_arb",
    "scan_bracket_arb",
    "_refine_bracket_with_clob",
    "scan_multi_cross",
    "scan_stale_prices",
    "scan_resolution_snipes",
    "scan_convergence",
    "scan_polymarket_rewards",
    "scan_kalshi_rewards",
    "scan_fee_promo",
    "scan_cross_mm",
    "scan_lead_lag_mm",
    "scan_toxic_flow_pause",
    "scan_volatility_adjusted_mm",
    "scan_conditional_arb",
    "_refine_conditional_with_clob",
    "scan_settlement_timing",
    "_refine_settlement_with_clob",
    "scan_new_market_mispricing",
    "scan_api_outage_arb",
    "_refine_binary_with_clob",
    "_refine_negrisk_with_clob",
    "_refine_cross_with_clob",
    "_refine_cross_all_with_clob",
    "_attach_exec_metadata",
    "_CROSS_FEE_FUNCS",
    "_fetch_kalshi_data",
    "_extract_token_ids",
    "_fetch_clob_for_market",
    "_parallel_fetch_kalshi",
    "capital_efficiency_score",
    "scan_social_sentiment",
    "scan_expert_divergence",
    "scan_insider_pattern",
    "get_order_flow_tracker",
    "scan_cross_category",
    "get_signal_fetcher",
    "scan_frechet",
    "_refine_frechet_with_clob",
    "scan_temporal_arb",
    "_refine_temporal_with_clob",
    "scan_ctf",
    "_refine_ctf_with_clob",
    "scan_limitless_rewards",
]
