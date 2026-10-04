"""Continuous mode: periodic re-scans with WebSocket feeds, settlement, and dashboard updates."""

from sentry_init import init_sentry, capture_scan_heartbeat
init_sentry()

import asyncio
import json
import logging
import math
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from polymarket_api import fetch_all_markets, fetch_events, fetch_reward_markets
from ws_feeds import FeedManager, get_feed_health_tracker
from db import TradeDB
from display import display_results
from dashboard import state as dashboard_state
from recovery import reconcile_orphaned_positions
from scripts.analytics import get_strategy_metrics
from credential_health import CredentialHealthChecker
from kalshi_api import build_client_from_env, kalshi_creds_configured
from fees import (
    net_profit_binary_internal,
    net_profit_negrisk_internal,
    net_profit_cross_platform,
    net_profit_kalshi_binary,
    net_profit_kalshi_multi,
    net_profit_cross_betfair,
    net_profit_cross_generic,
)
import config
from config import (
    RESCAN_INTERVAL as CONFIG_RESCAN_INTERVAL,
    WS_SUBSCRIPTION_LIMIT as CONFIG_WS_SUBSCRIPTION_LIMIT,
    WS_TRIGGER_ENABLED as CONFIG_WS_TRIGGER_ENABLED,
    WS_TRIGGER_THRESHOLD as CONFIG_WS_TRIGGER_THRESHOLD,
    WS_TRIGGER_DEDUPE_SECONDS as CONFIG_WS_TRIGGER_DEDUPE_SECONDS,
    HEDGE_ENABLED as CONFIG_HEDGE_ENABLED,
    SNAPSHOT_ENABLED as CONFIG_SNAPSHOT_ENABLED,
    SNAPSHOT_INTERVAL as CONFIG_SNAPSHOT_INTERVAL,
    MAX_CONCURRENT_WS_EXECUTIONS as CONFIG_MAX_CONCURRENT_WS_EXECUTIONS,
    PRICE_CACHE_EVICTION_AGE as CONFIG_PRICE_CACHE_EVICTION_AGE,
    WS_STALE_FEED_SECONDS as CONFIG_WS_STALE_FEED_SECONDS,
    REWARDS_ENABLED as CONFIG_REWARDS_ENABLED,
    REWARDS_POLL_INTERVAL as CONFIG_REWARDS_POLL_INTERVAL,
    KALSHI_LIP_ENABLED as CONFIG_KALSHI_LIP_ENABLED,
    KALSHI_VIP_TRACK_ENABLED as CONFIG_KALSHI_VIP_TRACK_ENABLED,
    KALSHI_VIP_POLL_INTERVAL as CONFIG_KALSHI_VIP_POLL_INTERVAL,
    IMBALANCE_ENABLED as CONFIG_IMBALANCE_ENABLED,
    NEWS_SNIPE_ENABLED as CONFIG_NEWS_SNIPE_ENABLED,
    CORRELATED_ENABLED as CONFIG_CORRELATED_ENABLED,
    TIME_DECAY_ENABLED as CONFIG_TIME_DECAY_ENABLED,
    LIMITLESS_REWARDS_ENABLED as CONFIG_LIMITLESS_REWARDS_ENABLED,
    polymarket_scan_enabled,
    polymarket_reward_fetch_enabled,
)

# Conditional metrics import — never breaks if metrics.py is missing
try:
    from config import METRICS_ENABLED as _METRICS_ENABLED
    if _METRICS_ENABLED:
        from metrics import metrics as _metrics
    else:
        _metrics = None
except Exception:
    _metrics = None

try:
    from funnel import get_funnel_tracker
except ImportError:
    get_funnel_tracker = None
from scans import (
    scan_binary_internal,
    scan_negrisk_internal,
    scan_cross_platform,
    scan_cross_all,
    scan_kalshi_binary,
    scan_kalshi_multi,
    scan_spread_polymarket,
    scan_betfair_backall,
    scan_betfair_backlay,
    scan_smarkets_backall,
    scan_smarkets_backlay,
    scan_sxbet,
    scan_matchbook_backall,
    scan_matchbook_backlay,
    scan_gemini_binary,
    scan_gemini_multi,
    scan_ibkr_binary,
    scan_triangular,
    scan_nway_arb,
    scan_multi_cross,
    scan_polymarket_rewards,
    scan_kalshi_rewards,
    scan_limitless_rewards,
    scan_lead_lag_mm,
    scan_toxic_flow_pause,
    scan_volatility_adjusted_mm,
    _fetch_kalshi_data,
    capital_efficiency_score,
)
# Layer-4 informed-trading scans — imported at module level (not lazily inside
# the loop) so the continuous-mode wiring is patchable in tests. These power the
# _scan_*_layer4 helpers below.
from scans.imbalance import scan_imbalance
from scans.news_snipe import scan_news_snipe
from scans.correlated import scan_correlated
from scans.time_decay import scan_time_decay

logger = logging.getLogger(__name__)


def _wake_asyncio_selector(loop, event) -> bool:
    """Wake a running asyncio selector without replacing OS signal handlers."""
    if loop is None:
        return False
    try:
        loop.call_soon_threadsafe(event.set)
    except RuntimeError:
        # The loop may already be closed during repeated shutdown signals.
        return False
    return True


def _is_execution_eligible(opp: dict) -> bool:
    """Return whether an opportunity may enter any execution path."""
    return opp.get("_execution_eligible", True) is not False


def _cache_probability(entry: dict | None, *keys: str) -> float | None:
    """Return the first scalar 0..1 probability from a WS cache entry."""
    if not entry:
        return None
    for key in keys:
        value = entry.get(key)
        if value is None or isinstance(value, (dict, list, tuple)):
            continue
        try:
            probability = float(value)
        except (TypeError, ValueError):
            continue
        if 0.0 <= probability <= 1.0:
            return probability
    return None


def _start_kalshi_ws_after_empty_run(feed_manager, ws_task, kalshi_tickers: list[str]) -> bool:
    """Start the Kalshi WS feed when the first scan had nothing to subscribe.

    ``FeedManager.run()`` returns immediately when scan #1 has no tickers. That
    happens in ``--mode mm-pilot`` because the pilot selects its markets after
    the first scan. After that, the per-scan update path, which requires a
    running ``ws_task``, never fires, so pilot tickers never reach the WS feed.
    Only a task that finished cleanly is restarted this way; a crashed or
    cancelled task keeps its existing behaviour. Must run on the event loop.
    """
    if ws_task is None or not ws_task.done() or ws_task.cancelled() or ws_task.exception() is not None:
        return False
    if not kalshi_tickers:
        return False
    feed_manager.update_subscriptions(kalshi_tickers=kalshi_tickers)
    return feed_manager.start_kalshi_feed_late()


def _sync_ws_feeds(feed_manager, ws_task, scan_count: int, poly_sub_ids: list[str],
                   kalshi_sub_tickers: list[str], kalshi_client):
    """Start WS feeds on scan #1, then keep subscriptions current. Returns the WS task."""
    if scan_count == 1 and not ws_task:
        feed_manager.subscribe_polymarket(poly_sub_ids)
        if kalshi_client:
            feed_manager.subscribe_kalshi(kalshi_sub_tickers)
        ws_task = asyncio.create_task(feed_manager.run())
        logger.info(
            "WS feeds started: %d Polymarket tokens, %d Kalshi tickers",
            len(poly_sub_ids), len(kalshi_sub_tickers),
        )
    elif ws_task and not ws_task.done():
        feed_manager.update_subscriptions(
            poly_token_ids=poly_sub_ids,
            kalshi_tickers=kalshi_sub_tickers,
        )
        # If Kalshi healed after a degraded boot, its WS task was
        # never spawned by run() — start it now (idempotent).
        if kalshi_client is not None:
            feed_manager.start_kalshi_feed_late()
    elif ws_task and kalshi_client is not None:
        # run() already returned because scan #1 had nothing to
        # subscribe (mm-pilot before selection); start Kalshi now
        # and queue later re-selections onto the live connection.
        _start_kalshi_ws_after_empty_run(feed_manager, ws_task, kalshi_sub_tickers)
    return ws_task


def _ws_feeds_active(feed_manager, ws_task) -> bool:
    """True while any WS feed runs, including a Kalshi feed started after an empty run."""
    if ws_task is not None and not ws_task.done():
        return True
    return feed_manager.kalshi_late_feed_running()


def _ws_tracking_probability(platform: str, entry: dict | None) -> float | None:
    """Extract one executable scalar probability for WS-driven trackers."""
    if platform == "polymarket":
        return _cache_probability(entry, "best_ask", "ask", "price")
    if platform == "kalshi":
        return _cache_probability(
            entry,
            "yes_ask", "yes_price", "yes",
            "no_ask", "no_price", "no",
            "price",
        )
    return _cache_probability(entry, "price", "yes_price", "yes")


def _ws_opportunity_probability(opp: dict, platform: str, entry: dict | None) -> float | None:
    """Extract the executable WS price for the opportunity's platform side."""
    if platform != "kalshi":
        return _ws_tracking_probability(platform, entry)

    side = None
    if opp.get("_platform_a") == "kalshi":
        side = opp.get("_side_a")
    elif opp.get("_platform_b") == "kalshi":
        side = opp.get("_side_b")

    if side is None:
        opp_type = opp.get("type", "").upper()
        if "K_NO" in opp_type:
            side = "no"
        elif "K_YES" in opp_type:
            side = "yes"

    if side == "no":
        return _cache_probability(entry, "no_ask", "no_price", "no")
    return _cache_probability(entry, "yes_ask", "yes_price", "yes", "price")


def _route_kalshi_ws_to_mm_pilot(pilot, platform: str, ticker: str, data: dict,
                                 tracking_price: float | None) -> None:
    """Route Kalshi WebSocket streaming updates to the MM pilot.

    Prioritizes full streaming orderbooks when available. If the streaming
    orderbook is one-sided (lacks either yes_ask or no_ask) and a tracking
    mid-price is available, also invokes on_ws_price to ensure the midpoint
    is populated. If no orderbook is present, falls back to scalar mid ticks.
    """
    if not pilot or platform != "kalshi":
        return
    if data.get("orderbook"):
        try:
            pilot.update_book_from_ws(ticker, {"orderbook": data["orderbook"]})
        except Exception as exc:
            logger.debug("MM pilot WS orderbook update failed: %s", exc)
        if (tracking_price is not None
                and (data.get("yes_ask") is None
                     or data.get("no_ask") is None)):
            try:
                pilot.on_ws_price(ticker, tracking_price)
            except Exception as exc:
                logger.debug("MM pilot WS feed failed: %s", exc)
    elif tracking_price is not None:
        try:
            pilot.on_ws_price(ticker, tracking_price)
        except Exception as exc:
            logger.debug("MM pilot WS feed failed: %s", exc)


class _WSTriggerDeduper:
    """Thread-safe short cooldown for identical WS-triggered opportunities."""

    def __init__(self, cooldown_seconds: float):
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._last_queued: dict[str, float] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(opp: dict) -> str:
        market_key = (
            opp.get("_market_key")
            or opp.get("_kalshi_ticker")
            or opp.get("market", "?")
        )
        return f"{opp.get('_source', '')}:{opp.get('type', '')}:{market_key}"

    def admit(self, opp: dict, now: float | None = None) -> bool:
        """Claim a cooldown slot, returning False for a recent duplicate."""
        now = time.monotonic() if now is None else now
        key = self._key(opp)
        with self._lock:
            if now - self._last_queued.get(key, float("-inf")) < self.cooldown_seconds:
                return False
            self._last_queued[key] = now
            if len(self._last_queued) > 10_000:
                cutoff = now - max(self.cooldown_seconds * 2, 60.0)
                self._last_queued = {
                    existing_key: queued_at
                    for existing_key, queued_at in self._last_queued.items()
                    if queued_at >= cutoff
                }
            return True

    def forget(self, opp: dict) -> None:
        """Release a claim when queue insertion fails."""
        with self._lock:
            self._last_queued.pop(self._key(opp), None)


class OpportunityIndex:
    """Maps (platform, ticker/token) to opportunities for fast lookup on WS updates."""

    def __init__(self):
        self._index: dict[tuple[str, str], list[dict]] = {}
        self._lock = threading.Lock()

    def rebuild(self, opportunities: list[dict]):
        """Rebuild the index from a list of opportunities."""
        new_index: dict[tuple[str, str], list[dict]] = {}
        for opp in opportunities:
            if not _is_execution_eligible(opp):
                continue
            keys = self._extract_keys(opp)
            for key in keys:
                new_index.setdefault(key, []).append(opp)
        with self._lock:
            self._index = new_index

    def lookup(self, platform: str, ticker: str) -> list[dict]:
        """Look up opportunities affected by a price update for (platform, ticker)."""
        with self._lock:
            return list(self._index.get((platform, ticker), []))

    def get_subscription_tokens(self, limit: int = 500) -> tuple[list[str], list[str]]:
        """Get top token IDs for WS subscription, prioritized by opportunity profit.

        Returns (poly_token_ids, kalshi_tickers).
        """
        poly_tokens = set()
        kalshi_tickers = set()
        with self._lock:
            # Sort by profit descending
            scored = []
            for key, opps in self._index.items():
                best_profit = max(o.get("net_profit", 0) for o in opps)
                scored.append((best_profit, key))
            scored.sort(reverse=True)

            for _, (platform, token) in scored:
                if platform == "polymarket" and len(poly_tokens) < limit:
                    poly_tokens.add(token)
                elif platform == "kalshi" and len(kalshi_tickers) < limit:
                    kalshi_tickers.add(token)
        return list(poly_tokens), list(kalshi_tickers)

    @staticmethod
    def _extract_keys(opp: dict) -> list[tuple[str, str]]:
        """Extract (platform, ticker) keys from an opportunity."""
        keys = []
        opp_type = opp.get("type", "")

        # Polymarket token IDs
        token_ids = opp.get("_token_ids", [])
        for tid in token_ids:
            if tid:
                keys.append(("polymarket", tid))

        # Kalshi tickers
        kalshi_ticker = opp.get("_kalshi_ticker", "")
        if kalshi_ticker:
            keys.append(("kalshi", kalshi_ticker))

        kalshi_tickers = opp.get("_kalshi_tickers", [])
        for t in kalshi_tickers:
            if t:
                keys.append(("kalshi", t))

        # Betfair
        bf_market_id = opp.get("_bf_market_id") or opp.get("_market_id", "")
        if bf_market_id and "betfair" in opp_type.lower():
            keys.append(("betfair", bf_market_id))

        # Smarkets
        sm_market_id = opp.get("_sm_market_id", "")
        if sm_market_id:
            keys.append(("smarkets", sm_market_id))

        # SX Bet
        sx_hash = opp.get("_sx_market_hash", "")
        if sx_hash:
            keys.append(("sxbet", sx_hash))

        # Matchbook
        mb_market_id = opp.get("_mb_market_id", "")
        if mb_market_id:
            keys.append(("matchbook", mb_market_id))

        # Gemini
        gm_event_id = opp.get("_gm_event_id", "")
        if gm_event_id:
            keys.append(("gemini", gm_event_id))

        # IBKR
        ibkr_event_id = opp.get("_ibkr_event_id", "")
        if ibkr_event_id:
            keys.append(("ibkr", ibkr_event_id))

        # EventDivergence: index by platform + metaculus question ID
        if opp_type == "EventDivergence":
            platform = opp.get("_platform", "")
            metaculus_id = opp.get("_metaculus_id")
            if platform and metaculus_id:
                keys.append((platform, f"metaculus_{metaculus_id}"))

        # TriangularCross: index by both platform keys
        if opp_type == "TriangularCross":
            for pkey in ("_platform_a", "_platform_b"):
                pname = opp.get(pkey, "")
                if pname:
                    keys.append((pname, opp.get("market", "")))

        return keys


_WINNING_SIDE_ALIASES = {
    "yes": {"yes", "y", "buy", "back"},
    "no": {"no", "n", "sell", "lay"},
}


def _leg_won(trade_side: str, winning_side: str) -> bool:
    """Return True if a trade's side matches the resolved winning outcome.

    Handles cross-platform side terminology: yes/no, buy/sell, back/lay,
    and free-form outcome names (case-insensitive equality).
    """
    ts = (trade_side or "").lower()
    ws = (winning_side or "").lower()
    if not ts or not ws:
        return False
    if ts == ws:
        return True
    aliases = _WINNING_SIDE_ALIASES.get(ws)
    return aliases is not None and ts in aliases


# Venue sizing semantics, verified against executor._execute_single_leg:
# - SHARES: Polymarket passes `size` straight to PolymarketTrader.place_order,
#   whose `size` parameter is the number of shares.
# - DOLLAR->CONTRACTS: Kalshi (count=max(1,int(size/price))), Smarkets,
#   SX Bet, Gemini, IBKR (quantity=max(1,int(size/price))) convert the
#   requested dollar size to an integer contract count at the order price.
# - STAKE: Betfair (limitOrder size=round(size,2) at decimal odds 1/price)
#   and Matchbook (stake=round(size,2) at decimal odds 1/price) place the
#   dollar STAKE directly; a winning back bet returns stake/price
#   (= stake/price one-dollar contracts), a losing one forfeits the stake.
_SHARE_SIZED_VENUES = frozenset({"polymarket"})
_DOLLAR_CONTRACT_VENUES = frozenset({"kalshi", "smarkets", "sxbet", "gemini", "ibkr"})
_STAKE_SIZED_VENUES = frozenset({"betfair", "matchbook"})


def _money_valid(value) -> bool:
    """Return whether a value is valid positive money data.

    bool is an int subclass, and NaN/inf pass isinstance checks — all three
    must be rejected before a value can price a P&L leg.

    Args:
        value: Candidate numeric value.

    Returns:
        True only for finite, positive, non-boolean numbers.
    """
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _leg_contracts_and_cost(trade: dict) -> tuple[float, float] | None:
    """Return (contracts, dollar_cost) for one trade leg, venue-aware.

    FAIL-CLOSED: this feeds realized P&L and therefore the daily-loss halt.
    An unknown/missing venue or malformed money data (missing, zero,
    negative, NaN or infinite price/fill/size) must never silently produce
    an optimistic number:

    - When the recorded size is valid but the prices are not, we take the
      WORST CASE — zero payout, the venue-consistent worst dollar cost lost
      (shares are capped at $1/share on Polymarket, stake venues lose the
      rounded stake, dollar venues lose the dollar size) — so the halt can
      only over-trigger, never under-trigger.
    - When the size itself is invalid the leg cannot be priced at all;
      return ``None`` so the caller can refuse to produce a realized number
      from garbage instead of falling back to expected profit.

    Args:
        trade: Persisted trade row. Polymarket records shares, Betfair and
            Matchbook record stake, and the remaining supported venues record
            a requested dollar amount converted to integer contracts.

    Returns:
        ``(contracts, dollar_cost)`` when the leg can be conservatively priced,
        otherwise ``None`` when even a safe dollar cost cannot be derived.
    """
    platform = (trade.get("platform") or "").lower()
    price = trade.get("price")
    fill = trade.get("fill_price")
    if fill is None:  # explicit None check — a recorded zero fill is NOT a
        fill = price  # missing fill, it is invalid money data (rejected below)
    size = trade.get("size")

    def _log_fail(reason: str, worst_cost: float | None) -> None:
        logger.error(
            "P&L fail-closed: %s (trade id=%s platform=%r price=%r fill=%r "
            "size=%r) — %s.",
            reason, trade.get("id"), trade.get("platform"), price,
            trade.get("fill_price"), size,
            ("leg unpriceable, refusing realized P&L from garbage data"
             if worst_cost is None
             else f"assuming worst case: ${worst_cost:.2f} lost, zero payout"),
        )

    def _worst_case(reason: str) -> tuple[float, float] | None:
        # Conservative direction: zero payout, full cost lost, in the USD
        # unit the venue actually recorded (`size` is SHARES on Polymarket,
        # a dollar STAKE on Betfair/Matchbook, dollars elsewhere).
        if not _money_valid(size):
            _log_fail(reason, None)
            return None
        safe_fill = fill if _money_valid(fill) else None
        if platform in _SHARE_SIZED_VENUES:
            # Shares cost at most $1/share; use the fill when it's usable.
            worst_cost = (safe_fill * size) if safe_fill is not None else float(size)
        elif platform in _STAKE_SIZED_VENUES:
            worst_cost = round(size, 2)  # mirrors round(size, 2) at placement
        elif platform in _DOLLAR_CONTRACT_VENUES:
            worst_cost = float(size)
        else:
            # Unknown venue: sizing semantics unknown, take the largest
            # plausible interpretation (dollar size vs shares * fill).
            worst_cost = max(float(size), (safe_fill or 0.0) * size)
        _log_fail(reason, worst_cost)
        return 0.0, worst_cost

    if not _money_valid(size):
        _log_fail("missing or invalid trade size", None)
        return None

    if platform in _SHARE_SIZED_VENUES:
        if not _money_valid(fill):
            return _worst_case("missing or invalid fill/order price")
        return float(size), fill * size

    if platform in _DOLLAR_CONTRACT_VENUES:
        if not _money_valid(price) or not _money_valid(fill):
            return _worst_case("missing or invalid fill/order price")
        # Mirror the executor's dollars -> integer-contracts conversion.
        contracts = float(max(1, int(size / price)))
        return contracts, contracts * fill

    if platform in _STAKE_SIZED_VENUES:
        stake = round(size, 2)  # mirrors round(size, 2) at placement
        if not _money_valid(fill):
            return _worst_case("missing or invalid fill/order price")
        # Back bet: stake at decimal odds 1/fill returns stake/fill if it
        # wins — equivalent to stake/fill one-dollar contracts.
        return stake / fill, stake

    return _worst_case(f"unknown venue {platform!r}")


def _calc_realized_pnl(db: TradeDB, pos: dict, winning_side: str | None = None) -> float:
    """Calculate realized P&L from actual fill prices in the trades table.

    Args:
        db: TradeDB instance.
        pos: Position record.
        winning_side: Resolved winning outcome (e.g. "yes", "no", or an outcome
            name). When provided, per-leg payout is computed: winning legs pay
            contracts * $1, losing legs pay $0. Required for directional
            strategies (Imbalance, NewsSnipe, WhaleCopy, TimeDecay, etc.) —
            without it, losing directional bets would falsely appear profitable.

    Returns:
        Realized P&L in USD. Falls back to expected_pnl when no trade data is
        available. When winning_side is None, assumes an arbitrage payout:
        the winning leg pays $1/contract, so guaranteed payout is the minimum
        contract count across legs (correct for Binary/NegRisk/Cross/etc.
        where one side guaranteed wins, INCORRECT for losing directional
        bets — pass winning_side for those).

    Note:
        Contract counts and dollar costs are derived per venue by
        ``_leg_contracts_and_cost`` — Polymarket logs ``size`` in SHARES,
        Betfair and Matchbook log a dollar STAKE, and the remaining supported
        venues log a requested DOLLAR size converted to contracts via
        ``max(1, int(size / price))``.
    """
    trades = db.get_trades_for_opportunity(pos["opportunity_id"])
    if not trades:
        return pos.get("expected_pnl", 0)

    # Realized P&L is based only on confirmed venue fills. Pending, aborted,
    # failed, cancelled, dry-run, and orphaned rows are not executed legs and
    # must not affect settlement. If no confirmed fills remain, preserve the
    # expected-P&L fallback for legacy/incomplete records.
    trades = [t for t in trades if (t.get("status") or "").lower() == "filled"]
    if not trades:
        return pos.get("expected_pnl", 0)

    legs: list[tuple[tuple[float, float], dict]] = []
    invalid = False
    for t in trades:
        # Prefer an explicitly persisted executed size when a venue supports
        # partial fills; otherwise the confirmed row's requested size is the
        # executor's current full-fill representation.
        trade = dict(t)
        if _money_valid(t.get("executed_size")):
            trade["size"] = t["executed_size"]
        priced = _leg_contracts_and_cost(trade)
        if priced is None:
            invalid = True
            continue
        legs.append((priced, trade))

    total_fill_cost = sum(cost for (_, cost), _t in legs)

    if invalid:
        # Garbage money data on at least one live leg: refuse to synthesize
        # a payout, and NEVER fall back to (typically positive) expected
        # profit — report the known cost as lost so the halt over-triggers.
        logger.error(
            "Realized P&L fail-closed for opportunity %s: unpriceable trade "
            "leg(s); reporting -$%.2f (known cost, zero payout).",
            pos.get("opportunity_id"), total_fill_cost,
        )
        expected = pos.get("expected_pnl", 0)
        expected_loss = (
            abs(float(expected))
            if isinstance(expected, (int, float))
            and not isinstance(expected, bool)
            and math.isfinite(expected)
            else 0.0
        )
        return -max(total_fill_cost, expected_loss)

    if winning_side is None:
        # Arbitrage assumption: exactly one leg settles at $1 per contract.
        # Guaranteed payout = min contracts across legs (whichever leg wins,
        # at least that many contracts pay out).
        contracts_per_leg = [contracts for (contracts, _), _t in legs]
        return min(contracts_per_leg) - total_fill_cost

    # Per-leg payout based on resolved outcome. The traded outcome
    # (yes/no/...) is persisted separately from the execution side because
    # Polymarket BUY_NO legs are logged with side="BUY"; fall back to side
    # for legacy rows and venues whose side IS the outcome.
    total_payout = 0.0
    for (contracts, _cost), t in legs:
        trade_side = (
            t.get("outcome")
            if (t.get("platform") or "").lower() == "polymarket"
            else t.get("side")
        )
        if not _leg_won(trade_side or t.get("side") or "", winning_side):
            continue
        total_payout += contracts  # $1 per winning contract
    return total_payout - total_fill_cost


def check_settlements(
    db: TradeDB,
    kalshi_client,
    poly_markets: list[dict] | None,
    betfair_client=None,
    smarkets_client=None,
    sxbet_client=None,
    matchbook_client=None,
    gemini_client=None,
    ibkr_client=None,
):
    """Check open positions for settlement and update realised P&L.

    Iterates all open positions in the database, queries each platform's
    API for resolution status, and settles any positions whose underlying
    market has resolved. Calculates realised P&L from trade history and
    updates the position record. Also processes pending partial fills that
    need hedging.

    Args:
        db: TradeDB instance for reading open positions and writing settlements.
        kalshi_client: Authenticated KalshiClient, or None to skip Kalshi
            settlement checks.
        poly_markets: Latest Polymarket markets list (used to check resolution
            status), or None.
        betfair_client: Optional BetfairClient for Betfair settlement checks.
        smarkets_client: Optional SmarketsClient for Smarkets settlement checks.
        sxbet_client: Optional SXBetClient for SX Bet settlement checks.
        matchbook_client: Optional MatchbookClient for Matchbook settlement
            checks.
        gemini_client: Optional GeminiClient for Gemini settlement checks.
        ibkr_client: Optional IBKRClient for IBKR ForecastEx settlement checks.
    """
    open_positions = db.get_open_positions()
    if not open_positions:
        return

    logger.info("Checking %d open positions for settlement...", len(open_positions))

    # Account-scoped Kalshi reconciliation (June 2026 audit): the account's
    # /portfolio/settlements feed is the authoritative record of what settled —
    # one API call covers every open Kalshi position and cannot miss a
    # resolution the way per-market polling did. Build a ticker → result map
    # up front; per-market /markets/{ticker} lookup remains as fallback for
    # positions not (yet) present in the settlement feed.
    kalshi_settlements: dict[str, str] = {}
    if kalshi_client and any(p["platform"] == "kalshi" for p in open_positions):
        try:
            for s in kalshi_client.get_settlements():
                ticker = s.get("ticker", "")
                result = s.get("market_result", "")
                if ticker and result and ticker not in kalshi_settlements:
                    kalshi_settlements[ticker] = result
        except Exception as e:
            logger.warning("Kalshi settlements fetch failed (falling back to per-market lookups): %s", e)

    settled = 0
    for pos in open_positions:
        platform = pos["platform"]
        # Settlement lookups are keyed by the platform-native ticker, not the
        # human-readable title. Prefer market_ticker; fall back to
        # market_identifier for legacy rows written before that column existed.
        market_id = pos.get("market_ticker") or pos["market_identifier"]

        try:
            if platform == "kalshi" and kalshi_client:
                result = kalshi_settlements.get(market_id, "")
                if not result:
                    resp = kalshi_client._request("GET", f"/markets/{market_id}")
                    if resp and resp.status_code == 200:
                        data = resp.json()
                        market_data = data.get("market", data)
                        result = market_data.get("result", "")
                if result:
                    realized = _calc_realized_pnl(db, pos, winning_side=result)
                    db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                    settled += 1
            elif platform in ("polymarket", "cross"):
                try:
                    from polymarket_api import _get_with_retry, GAMMA_BASE
                    resp = _get_with_retry(f"{GAMMA_BASE}/markets/{market_id}", timeout=15)
                    if resp and resp.status_code == 200:
                        pm_data = resp.json()
                        if pm_data.get("closed") or pm_data.get("resolvedOutcome"):
                            # "cross" positions are arbs (one side wins) — leave winning_side
                            # unset so the legacy 1.0-cost formula applies. For pure-Polymarket
                            # directional trades, pass the resolved outcome.
                            ws = pm_data.get("resolvedOutcome") if platform == "polymarket" else None
                            realized = _calc_realized_pnl(db, pos, winning_side=ws)
                            db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                            settled += 1
                except Exception as e:
                    logger.warning("PM settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "betfair" and betfair_client:
                try:
                    from betfair_api import BETFAIR_EXCHANGE_URL, _rate_limit as bf_rate_limit
                    bf_rate_limit()
                    resp = betfair_client.session.post(
                        f"{BETFAIR_EXCHANGE_URL}/listMarketBook/",
                        json={"marketIds": [market_id]},
                        timeout=15,
                    )
                    if resp and resp.status_code == 200:
                        books = resp.json()
                        if books and isinstance(books, list) and books:
                            mkt_status = books[0].get("status", "")
                            if mkt_status in ("CLOSED", "SETTLED"):
                                realized = _calc_realized_pnl(db, pos)
                                db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                                settled += 1
                except Exception as e:
                    logger.warning("Betfair settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "smarkets" and smarkets_client:
                try:
                    market_data = smarkets_client.get_market_status(market_id) if hasattr(smarkets_client, "get_market_status") else None
                    if market_data and market_data.get("state") in ("settled", "closed"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("Smarkets settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "sxbet" and sxbet_client:
                try:
                    market_data = sxbet_client.get_market_status(market_id) if hasattr(sxbet_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("SETTLED", "CLOSED"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("SX Bet settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "matchbook" and matchbook_client:
                try:
                    market_data = matchbook_client.get_market_status(market_id) if hasattr(matchbook_client, "get_market_status") else None
                    if market_data:
                        event_status = market_data.get("status", "")
                        if event_status in ("settled", "closed", "resulted"):
                            realized = _calc_realized_pnl(db, pos)
                            db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                            settled += 1
                except Exception as e:
                    logger.warning("Matchbook settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "gemini" and gemini_client:
                try:
                    market_data = gemini_client.get_market_status(market_id) if hasattr(gemini_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("settled", "closed", "resolved"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("Gemini settlement check failed for position %s: %s", pos['id'], e)
            elif platform == "ibkr" and ibkr_client:
                try:
                    market_data = ibkr_client.get_market_status(market_id) if hasattr(ibkr_client, "get_market_status") else None
                    if market_data and market_data.get("status") in ("settled", "closed", "expired"):
                        realized = _calc_realized_pnl(db, pos)
                        db.settle_position(pos["id"], realized_pnl=realized, status="settled")
                        settled += 1
                except Exception as e:
                    logger.warning("IBKR settlement check failed for position %s: %s", pos['id'], e)
        except Exception as e:
            logger.warning("Settlement check failed for position %s: %s", pos['id'], e)

    if settled:
        logger.info("Settled %d positions.", settled)


def _recalc_profit(opp: dict, platform: str, ticker: str, new_price: float, price_cache: dict) -> float | None:
    """Recalculate net profit for an opportunity using a fresh WS price.

    Dispatches to the correct fee function based on opportunity type.
    Returns recalculated net profit, or None if unable to compute.
    """
    opp_type = opp.get("type", "")
    try:
        if opp_type == "Binary":
            # Need both YES and NO prices from cache
            token_ids = opp.get("_token_ids", [])
            if len(token_ids) < 2:
                return None
            prices = []
            for tid in token_ids:
                if tid == ticker:
                    prices.append(new_price)
                else:
                    cached = price_cache.get((platform, tid))
                    cached_ask = _cache_probability(cached, "best_ask", "ask", "price")
                    if cached_ask is not None:
                        prices.append(cached_ask)
                    else:
                        return None
            result = net_profit_binary_internal(prices[0], prices[1])
            return result["net_profit"]
        elif opp_type.startswith("NegRisk"):
            token_ids = opp.get("_token_ids", [])
            if not token_ids:
                return None
            prices = []
            for tid in token_ids:
                if tid == ticker:
                    prices.append(new_price)
                else:
                    cached = price_cache.get((platform, tid))
                    cached_ask = _cache_probability(cached, "best_ask", "ask", "price")
                    if cached_ask is not None:
                        prices.append(cached_ask)
                    else:
                        return None
            result = net_profit_negrisk_internal(prices)
            return result["net_profit"]
        elif opp_type == "KalshiBinary":
            k_ticker = opp.get("_kalshi_ticker", "")
            cached = price_cache.get(("kalshi", k_ticker))
            if not cached:
                return None
            k_yes = _cache_probability(cached, "yes_ask", "yes_price", "yes")
            k_no = _cache_probability(cached, "no_ask", "no_price", "no")
            if k_yes is None:
                k_yes = _cache_probability(opp, "_kalshi_yes")
            if k_no is None:
                k_no = _cache_probability(opp, "_kalshi_no")
            if k_yes is None or k_no is None:
                return None
            result = net_profit_kalshi_binary(k_yes, k_no)
            return result["net_profit"]
        elif opp_type.startswith("Cross"):
            # Cross-platform: use the WS-updated price for one side and cached
            # price for the other. Requires _price_a/_price_b/_platform_a/_platform_b
            # metadata attached by the scan (added in cross.py).
            pa = opp.get("_platform_a", "")
            pb = opp.get("_platform_b", "")
            price_a = opp.get("_price_a")
            price_b = opp.get("_price_b")
            side_a = opp.get("_side_a", "yes")
            side_b = opp.get("_side_b", "no")
            if price_a is None or price_b is None or not pa or not pb:
                return None
            price_a = float(price_a)
            price_b = float(price_b)

            # Determine which side the WS update applies to
            if platform == pa:
                price_a = new_price
            elif platform == pb:
                price_b = new_price
            else:
                return None  # Update is for an unrelated platform

            result = net_profit_cross_generic(
                price_a, price_b, side_a, side_b,
                platform_a=pa, platform_b=pb,
            )
            return result["net_profit"]
    except Exception as e:
        logger.debug("Error recalculating profit: %s", e)
        return None
    return None


# Per-market lock management for concurrent WS-triggered execution
_market_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()


def _get_market_lock(market: str) -> threading.Lock:
    """Get or create a lock for a specific market."""
    with _locks_lock:
        if market not in _market_locks:
            _market_locks[market] = threading.Lock()
        return _market_locks[market]


# Priority weights: time-sensitive strategies execute before regular ones.
# Higher weight = execute sooner.
_PRIORITY_WEIGHTS = {
    "StalePriceOpp": 3.0,       # Most time-sensitive: stale prices disappear quickly
    "ResolutionSnipeOpp": 2.5,  # Resolution imminent: price converges fast
    "Binary": 2.0,              # Pure arb: guaranteed profit, execute quickly
    "KalshiBinary": 2.0,
    "Cross": 2.0,
    "TriangularCross": 2.0,
    "MultiCross": 2.0,
    "NegRisk": 1.8,
    "EventDivergence": 1.5,     # Signal-based: less urgent
    "ConvergenceOpp": 1.3,
    "MarketMake": 1.0,          # Lowest priority: always-on, not time-critical
}


def _execution_priority(opp: dict) -> float:
    """Score an opportunity for execution priority ordering.

    Combines time-sensitivity weight with capital efficiency.
    Time-sensitive opportunities (stale prices, resolution snipes) execute
    first regardless of absolute profit size.
    """
    opp_type = opp.get("type", "")
    efficiency = capital_efficiency_score(opp)

    # Find the matching priority weight via prefix
    weight = 1.0
    for prefix, w in _PRIORITY_WEIGHTS.items():
        if opp_type.startswith(prefix):
            weight = w
            break

    return weight * efficiency


class _StageTimer:
    """Context manager that records the elapsed wall-clock time of a scan stage.

    Usage::

        timings: dict[str, float] = {}
        with _StageTimer("fetch", timings):
            ...do work...
        # timings["fetch"] now holds elapsed seconds
    """

    __slots__ = ("name", "timings", "_start")

    def __init__(self, name: str, timings: dict):
        self.name = name
        self.timings = timings
        self._start = 0.0

    def __enter__(self):
        self._start = time.time()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.timings[self.name] = time.time() - self._start
        # Don't suppress exceptions
        return False


def _format_stage_timings(timings: dict, total: float) -> str:
    """Render stage timings as a single sortable summary line."""
    parts = [f"total={total:.1f}s"]
    # Sort by elapsed desc so the bottleneck is first
    for name, elapsed in sorted(timings.items(), key=lambda kv: -kv[1]):
        pct = (elapsed / total * 100) if total > 0 else 0
        parts.append(f"{name}={elapsed:.1f}s({pct:.0f}%)")
    return " ".join(parts)


def _check_platform_balance(executor, opportunities, notifier, scan_count):
    """Check platform capital allocation and alert on imbalance.

    When one platform holds >60% of total capital but generates <30% of
    opportunities, emits a rebalancing alert via the notifier.
    """
    # Count opportunity flow per platform
    platform_opp_counts: dict[str, int] = {}
    for opp in opportunities:
        plat = opp.get("_platform", "")
        if not plat:
            # Infer from type
            opp_type = opp.get("type", "")
            if "Kalshi" in opp_type:
                plat = "kalshi"
            elif "Betfair" in opp_type:
                plat = "betfair"
            elif "Smarkets" in opp_type:
                plat = "smarkets"
            elif "Gemini" in opp_type:
                plat = "gemini"
            elif "IBKR" in opp_type:
                plat = "ibkr"
            else:
                plat = "polymarket"
        platform_opp_counts[plat] = platform_opp_counts.get(plat, 0) + 1

    total_opps = sum(platform_opp_counts.values())
    if total_opps < 5:
        return  # Not enough data to assess

    # Fetch balances (uses executor's cached balance fetch)
    try:
        balances = executor._fetch_balances("Cross")
    except Exception:
        return
    if not balances:
        return

    total_balance = sum(v for v in balances.values() if isinstance(v, (int, float)))
    if total_balance <= 0:
        return

    for platform, balance in balances.items():
        if not isinstance(balance, (int, float)) or balance <= 0:
            continue
        capital_pct = balance / total_balance
        opp_flow = platform_opp_counts.get(platform, 0)
        opp_pct = opp_flow / total_opps if total_opps > 0 else 0

        # Alert if capital is concentrated but opportunity flow is low
        if capital_pct > 0.60 and opp_pct < 0.30:
            msg = (
                f"REBALANCE ALERT: {platform} holds {capital_pct:.0%} of capital "
                f"(${balance:.0f}) but only {opp_pct:.0%} of opportunity flow "
                f"({opp_flow}/{total_opps}). Consider moving funds."
            )
            logger.warning(msg)
            if hasattr(notifier, "notify_text"):
                notifier.notify_text(msg)


def _feed_sprint3_trackers(platform: str, ticker: str, data: dict) -> None:
    """Feed the Sprint 2 VolatilityTracker + LeadLagMM singletons with a WS tick.

    Both trackers short-circuit internally when their respective config flags
    are false; we also short-circuit at this level to avoid the per-tick
    module-singleton lookup when the operator hasn't opted in. Wrapped in
    try/except because ``on_price_update`` is a hot WS path and an unrelated
    tracker bug must never break the feed.
    """
    try:
        if not (config.MM_VOLATILITY_ADJUSTED_ENABLED or config.LEAD_LAG_MM_ENABLED):
            return
        price_val = data.get("price") or data.get("yes") or data.get("yes_price")
        if price_val is None:
            return
        price_f = float(price_val)
        if config.MM_VOLATILITY_ADJUSTED_ENABLED:
            from market_maker import get_volatility_tracker
            get_volatility_tracker().record_price(ticker, price_f)
        if config.LEAD_LAG_MM_ENABLED:
            from market_maker import get_lead_lag_mm
            get_lead_lag_mm().record_price(ticker, platform, price_f)
    except Exception as exc:
        logger.debug("VolatilityTracker/LeadLagMM WS feed failed: %s", exc)


# ---------------------------------------------------------------------------
# Layer 4 informed-trading scans (flag-gated; disabled by default)
#
# These four scans were silently disabled in continuous mode: the inline loop
# blocks called them with stale kwargs (poly_markets=/kalshi_data=/min_profit=)
# and wrong config names, and a broad ``except`` swallowed every resulting
# TypeError/ImportError — so flipping the feature flag did nothing. Extracted
# into helpers with the correct, current signatures so the wiring is unit-
# testable (each scan is patched and asserted invoked).
# ---------------------------------------------------------------------------

def _build_poly_markets_by_key(poly_markets) -> dict[str, dict]:
    """Index Polymarket markets by ``polymarket-<condition_id>``.

    Shared by the Layer-4 scans, which all take a ``markets_by_key`` dict.
    Mirrors the inline pattern the imbalance block used previously; these
    scans do not fetch Kalshi order books, so only Polymarket is indexed.
    """
    markets_by_key: dict[str, dict] = {}
    for mkt in poly_markets or []:
        cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
        if cid:
            markets_by_key[f"polymarket-{cid}"] = mkt
    return markets_by_key


def _parse_correlated_pairs(raw) -> list[tuple[str, str]]:
    """Parse the ``CORRELATED_PAIRS`` config (a JSON string of ``[a, b]``
    pairs) into the ``list[tuple[str, str]]`` ``scan_correlated`` expects.
    Returns ``[]`` on malformed input rather than raising."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    pairs: list[tuple[str, str]] = []
    for item in raw or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            pairs.append((str(item[0]), str(item[1])))
    return pairs


def _scan_imbalance_layer4(poly_markets, price_cache, mode) -> list[dict]:
    """STRAT-01 order-book imbalance. Returns ``[]`` when the flag/mode gate
    is off or there are no Polymarket markets to scan."""
    if mode not in ("all", "imbalance") or not config.IMBALANCE_ENABLED:
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    return scan_imbalance(
        markets_by_key,
        min_ratio=config.IMBALANCE_RATIO,
        price_cache=price_cache,
    )


def _scan_news_snipe_layer4(poly_markets, mode, cooldown_cache=None) -> list[dict]:
    """STRAT-02 news-driven resolution sniping.

    Runs the scan once per enabled news source and merges the results. Two
    independent gates:
      * NEWS_SNIPE_ENABLED + FINNHUB_API_KEY       -> Finnhub headlines
      * FIRECRAWL_NEWS_ENABLED + FIRECRAWL_API_KEY  -> Firecrawl web search
    Either, both, or neither may be active. Returns ``[]`` when no source is
    enabled/configured or there are no markets. A failure building or running
    one client does not disable the other."""
    if mode not in ("all", "news-snipe"):
        return []

    finnhub_on = config.NEWS_SNIPE_ENABLED and bool(config.FINNHUB_API_KEY)
    firecrawl_on = config.FIRECRAWL_NEWS_ENABLED and bool(config.FIRECRAWL_API_KEY)
    if not (finnhub_on or firecrawl_on):
        return []

    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []

    clients: list[object] = []
    if finnhub_on:
        try:
            from finnhub_api import FinnhubNewsClient
            clients.append(FinnhubNewsClient(api_key=config.FINNHUB_API_KEY))
        except ImportError:
            logger.debug("finnhub_api module not available")
        except Exception as exc:  # noqa: BLE001 - one client failing must not kill the other
            logger.warning("Finnhub news client init failed: %s", exc)
    if firecrawl_on:
        try:
            from firecrawl_news_client import FirecrawlNewsClient
            clients.append(FirecrawlNewsClient(api_key=config.FIRECRAWL_API_KEY))
        except ImportError:
            logger.debug("firecrawl_news_client module not available")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Firecrawl news client init failed: %s", exc)

    opportunities: list[dict] = []
    for client in clients:
        try:
            opportunities.extend(
                scan_news_snipe(
                    markets_by_key,
                    client,
                    cooldown_cache=cooldown_cache,
                    fuzzy_threshold=config.FUZZY_MATCH_THRESHOLD,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "News-snipe scan failed for %s: %s", type(client).__name__, exc
            )
    return opportunities


def _scan_correlated_layer4(poly_markets, price_cache, mode) -> list[dict]:
    """STRAT-06 correlated market pairs. Returns ``[]`` when the gate is off,
    no valid pairs are configured, or there are no markets."""
    if mode not in ("all", "correlated") or not config.CORRELATED_ENABLED:
        return []
    pairs = _parse_correlated_pairs(config.CORRELATED_PAIRS)
    if not pairs:
        logger.debug("CORRELATED_ENABLED but CORRELATED_PAIRS has no valid pairs")
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    return scan_correlated(
        markets_by_key,
        pairs,
        min_spread=config.CORRELATION_DIVERGENCE_THRESHOLD,
        price_cache=price_cache,
    )


def _scan_time_decay_layer4(poly_markets, price_cache, mode,
                            signal_aggregator=None) -> list[dict]:
    """STRAT-07 time-decay convergence. Returns ``[]`` when the gate is off or
    there are no markets. ``signal_aggregator`` is injectable for testing; a
    fresh ``SignalAggregator`` is built when not supplied."""
    if mode not in ("all", "time-decay") or not config.TIME_DECAY_ENABLED:
        return []
    markets_by_key = _build_poly_markets_by_key(poly_markets)
    if not markets_by_key:
        return []
    if signal_aggregator is None:
        from signal_aggregator import SignalAggregator
        signal_aggregator = SignalAggregator()
    return scan_time_decay(
        markets_by_key,
        signal_aggregator,
        min_hours_to_expiry=config.TIME_DECAY_MIN_HOURS_EXPIRY,
        min_consensus=config.TIME_DECAY_MIN_CONSENSUS,
        buy_below_price=config.TIME_DECAY_BUY_BELOW_PRICE,
        price_cache=price_cache,
    )


def _log_research_strategy(mode: str, strategy: str, inputs: int, candidates: int | None, surfaced: int) -> None:
    """One INFO line per strategy per cycle in --mode research, for edge measurement."""
    if mode != config.RESEARCH_MODE:
        return
    logger.info(
        "Research %s: %d inputs -> %s candidates -> %d surfaced after CLOB refine",
        strategy, inputs, "n/a" if candidates is None else candidates, surfaced,
    )


def _scan_frechet_layer1(poly_markets, mode, min_profit, price_cache=None, funnel=None) -> list[dict]:
    """Plan 02 Fréchet-bound logical arbitrage. Returns [] when the gate is off or there are no markets."""
    if mode not in ("all", "frechet", config.RESEARCH_MODE):
        return []
    if not (getattr(config, "FRECHET_ARB_ENABLED", False) or config.research_dry_run(mode)):
        return []
    if not poly_markets:
        _log_research_strategy(mode, "frechet", 0, 0, 0)
        return []
    try:
        from scans.frechet import scan_frechet, _refine_frechet_with_clob
        cands = scan_frechet(
            poly_markets,
            min_profit=min_profit,
            min_violation=getattr(config, "FRECHET_MIN_VIOLATION", 0.02),
            funnel=funnel,
            platform="polymarket",
        )
        refined = _refine_frechet_with_clob(cands, min_profit=min_profit, price_cache=price_cache, funnel=funnel)
        _log_research_strategy(mode, "frechet", len(poly_markets), len(cands), len(refined))
        return refined
    except Exception as exc:
        logger.warning("Fréchet arbitrage scan failed: %s", exc)
        return []


def _scan_temporal_layer1(kalshi_markets, mode, min_profit, kalshi_client=None, price_cache=None, funnel=None) -> list[dict]:
    """Plan 03 Cross-date temporal arbitrage. Returns [] when the gate is off or there are no markets."""
    if mode not in ("all", "temporal", config.RESEARCH_MODE):
        return []
    if not (getattr(config, "TEMPORAL_ARB_ENABLED", False) or config.research_dry_run(mode)):
        return []
    if not kalshi_markets:
        _log_research_strategy(mode, "temporal", 0, 0, 0)
        return []
    try:
        from scans.temporal import scan_temporal_arb, _refine_temporal_with_clob
        cands = scan_temporal_arb(
            kalshi_markets,
            min_profit=min_profit,
            min_violation=getattr(config, "TEMPORAL_MIN_VIOLATION", 0.02),
            funnel=funnel,
        )
        refined = _refine_temporal_with_clob(
            cands,
            min_profit=min_profit,
            kalshi_client=kalshi_client,
            price_cache=price_cache,
            funnel=funnel,
        )
        _log_research_strategy(mode, "temporal", len(kalshi_markets), len(cands), len(refined))
        return refined
    except Exception as exc:
        logger.warning("Temporal arbitrage scan failed: %s", exc)
        return []


def _scan_ctf_layer1(poly_markets, mode, min_profit, price_cache=None, funnel=None) -> list[dict]:
    """Plan 04 CTF Primitives arbitrage. Returns [] when disabled or no markets."""
    if mode not in ("all", "ctf", config.RESEARCH_MODE):
        return []
    is_explicit = (mode == "ctf")
    is_dry_run = getattr(config, "DRY_RUN", True)
    enabled = (
        getattr(config, "CTF_ENABLED", False)
        or getattr(config, "CTF_MERGE_ENABLED", False)
        or getattr(config, "CTF_MINT_SELL_ENABLED", False)
    )
    if not enabled and not (is_explicit and is_dry_run) and not config.research_dry_run(mode):
        return []
    if not poly_markets:
        _log_research_strategy(mode, "ctf", 0, None, 0)
        return []
    try:
        from scans.ctf import scan_ctf
        opps = scan_ctf(
            poly_markets,
            min_profit=min_profit,
            price_cache=price_cache,
            funnel=funnel,
        )
        _log_research_strategy(mode, "ctf", len(poly_markets), None, len(opps))
        return opps
    except Exception as exc:
        logger.warning("CTF primitives scan failed: %s", exc)
        return []


def _scan_rewards_continuous(
    mode: str,
    poly_reward_markets: list[dict] | None = None,
    reward_tracker=None,
    kalshi_client=None,
    kalshi_reward_tracker=None,
    kalshi_data=None,
    limitless_client=None,
    price_cache: dict | None = None,
) -> list[dict]:
    """Execute Layer 3 liquidity rewards scanning across configured platforms."""
    rewards_enabled = getattr(config, "REWARDS_ENABLED", CONFIG_REWARDS_ENABLED)
    limitless_rewards_enabled = getattr(config, "LIMITLESS_REWARDS_ENABLED", CONFIG_LIMITLESS_REWARDS_ENABLED)

    if not (
        (mode in ("all", "rewards") and (rewards_enabled or limitless_rewards_enabled))
        or mode == "limitless-rewards"
    ):
        return []

    opps: list[dict] = []
    try:
        pm_reward_opps: list[dict] = []
        k_reward_opps: list[dict] = []
        lim_reward_opps: list[dict] = []
        if mode in ("all", "rewards") and rewards_enabled:
            if poly_reward_markets and reward_tracker:
                pm_reward_opps = scan_polymarket_rewards(
                    markets=poly_reward_markets,
                    reward_tracker=reward_tracker,
                    price_cache=price_cache or {},
                )
                opps.extend(pm_reward_opps)

            if kalshi_client and kalshi_reward_tracker:
                k_reward_opps = scan_kalshi_rewards(
                    kalshi_client=kalshi_client,
                    reward_tracker=kalshi_reward_tracker,
                    kalshi_data=kalshi_data,
                )
                opps.extend(k_reward_opps)

        if (mode in ("all", "rewards", "limitless-rewards")) and (limitless_rewards_enabled or mode == "limitless-rewards") and limitless_client:
            lim_reward_opps = scan_limitless_rewards(
                limitless_client=limitless_client,
                price_cache=price_cache or {},
            )
            opps.extend(lim_reward_opps)

        logger.debug(
            "Rewards scan complete: %d Polymarket + %d Kalshi + %d Limitless opps",
            len(pm_reward_opps),
            len(k_reward_opps),
            len(lim_reward_opps),
        )
    except Exception as exc:
        logger.debug("Rewards scanning error: %s", exc)

    return opps


def heal_kalshi_client(executor, platform_clients, hedger, notifier):
    """Attempt one Kalshi re-auth from env creds and rewire dependents.

    Called from the continuous loop when the run started degraded (boot-time
    auth failure, e.g. during Kalshi's daily maintenance window). On success
    the executor, credential-health platform map, and hedger all receive the
    fresh client so execution paths heal along with scanning.

    Returns the authenticated client, or None if re-auth failed.
    """
    healed = build_client_from_env()
    if healed is None:
        logger.warning("Kalshi re-auth attempt failed — will retry on cooldown.")
        return None
    executor.kalshi_client = healed
    platform_clients["kalshi"] = healed
    if hedger is not None:
        hedger.kalshi_client = healed
    logger.warning("Kalshi authentication RESTORED — resuming Kalshi scans.")
    if notifier:
        try:
            notifier.notify_text(
                "arbgrid: Kalshi authentication restored — Kalshi and "
                "cross-platform scanning resumed.")
        except Exception as e:
            logger.warning("Notifier failed on Kalshi-restore alert: %s", e)
    return healed


def run_continuous(args, min_profit, kalshi_client, kalshi_api_key_id,
                   kalshi_private_key_path, executor, db, price_cache,
                   extra_clients=None, notifier=None, pm_trader=None,
                   event_monitor=None, kalshi_private_key_base64=None):
    """Run the scanner in continuous mode with WebSocket price feeds.

    Sets up WebSocket connections to all configured platforms, runs periodic
    full re-scans at a configurable interval, and optionally triggers
    immediate execution when a WS price update moves a tracked opportunity
    above the profit threshold. Handles graceful shutdown on SIGINT/SIGTERM,
    periodic settlement checks, stale price eviction, and dashboard state
    updates.

    Args:
        args: Parsed CLI argparse namespace (uses mode, continuous, interval,
            min_confidence, min_depth, limit, json, dry_run, exec_mode,
            max_trade, dashboard_port).
        min_profit: Minimum net profit threshold (0-1 float, e.g. 0.01 = 1%).
        kalshi_client: Authenticated KalshiClient instance, or None.
        kalshi_api_key_id: Kalshi API key ID string for WS auth, or None.
        kalshi_private_key_path: Path to Kalshi RSA private key PEM, or None.
        executor: TradeExecutor instance for opportunity execution.
        db: TradeDB instance for logging and settlement tracking.
        price_cache: Shared dict keyed by (platform, ticker) storing latest
            price snapshots from WebSocket feeds.
        extra_clients: Optional dict of additional platform clients keyed by
            platform name (e.g. {"betfair": BetfairClient, ...}).
        notifier: Optional Notifier instance for webhook/Slack alerts.
        pm_trader: Optional PolymarketTrader for on-chain execution.
        event_monitor: Optional EventMonitor for cross-event divergence
            tracking.
    """
    extra_clients = extra_clients or {}
    rescan_interval = getattr(args, 'interval', None) or CONFIG_RESCAN_INTERVAL

    shutdown_event = asyncio.Event()
    _signal_loop = None

    # Plan 10 / Codex round-2 finding #3: declared here (not later, where
    # the MM pilot setup block used to create them) so the signal handler
    # below can reference _mm_pilot_stop unconditionally and safely —
    # these three names always exist in this scope regardless of whether
    # MM_KALSHI_PILOT_ENABLED ever turns them into a running pilot.
    _mm_pilot = None
    _mm_pilot_thread = None
    _mm_pilot_stop = threading.Event()

    def _signal_handler(sig, frame):
        # Signal handlers must only perform async-signal-safe state changes.
        # Logging can re-enter a handler while its buffered stream is flushing,
        # raising RuntimeError during the very shutdown path that must stay safe.
        shutdown_event.set()
        # Signal the MM pilot to stop IMMEDIATELY, not only via the
        # end-of-cycle cleanup path (further down this function, which
        # only runs after the CURRENT scan cycle finishes). A long
        # synchronous scan cycle, or a hard kill before cleanup completes,
        # could otherwise leave live GTC orders resting on Kalshi with
        # nothing cancelling them for however long that cycle takes. The
        # pilot's own run_loop polls this event every ~0.5s and cancels
        # all resting orders via stop() as soon as it notices — idempotent
        # and safe to set here even when the pilot was never enabled.
        _mm_pilot_stop.set()
        # Keep the direct ``signal.signal`` path above so the pilot stop flag
        # is asserted even while the event loop is inside synchronous work.
        # Then wake asyncio's selector through its self-pipe so the outer loop
        # enters cleanup immediately instead of sleeping until its timeout.
        _wake_asyncio_selector(_signal_loop, shutdown_event)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    opp_index = OpportunityIndex()
    ws_trigger_enabled = CONFIG_WS_TRIGGER_ENABLED
    ws_trigger_threshold = CONFIG_WS_TRIGGER_THRESHOLD
    ws_trigger_deduper = _WSTriggerDeduper(CONFIG_WS_TRIGGER_DEDUPE_SECONDS)
    ws_sub_limit = CONFIG_WS_SUBSCRIPTION_LIMIT
    _price_cache_lock = threading.Lock()
    _execution_semaphore = threading.Semaphore(CONFIG_MAX_CONCURRENT_WS_EXECUTIONS)

    # Event-driven Cross detection (Phase 2): persistent pair index that
    # turns every Polymarket / Kalshi WS price tick into an immediate
    # Cross arb evaluation, instead of waiting for the next 16-min scan.
    # Disabled via CROSS_PAIR_WS_ENABLED=false if it ever needs an
    # emergency kill switch in production.
    from cross_pair_index import CrossPairIndex
    cross_pair_index = CrossPairIndex()
    cross_pair_ws_enabled = os.getenv("CROSS_PAIR_WS_ENABLED", "true").lower() == "true"
    _cross_pair_min_profit_factor = float(os.getenv("CROSS_PAIR_WS_MIN_PROFIT_FACTOR", "1.0"))

    # Cross-cycle Polymarket->Kalshi match cache for the convergence scan —
    # titles are static, so fuzzy matching only runs for unseen markets and on
    # a periodic full refresh instead of every cycle.
    from scans.convergence_inputs import ConvergenceMatchCache
    _convergence_match_cache = ConvergenceMatchCache(
        refresh_interval=float(os.getenv("CONVERGENCE_REMATCH_INTERVAL", "1800")))

    # Mirror paper opportunities to the shared Supabase ledger (durable,
    # queryable off-box). Resumes from the remote high-water mark; a Supabase
    # outage falls back to the local max so no historical backfill storms.
    _opp_sync = None
    _opp_sync_hwm = 0
    _opp_sync_inflight = False
    try:
        from config import OPP_SYNC_ENABLED
        if OPP_SYNC_ENABLED:
            from supabase_sync import OpportunitySync, build_client_from_env
            _opp_sync = OpportunitySync(build_client_from_env(), db=db)
            from config import PAPER_WINDOW_START_TS as _pw_ts
            _opp_sync_hwm = _opp_sync.get_remote_high_water_mark(window_start_ts=_pw_ts)
            logger.info("Opportunity Supabase sync active (resuming after id %d)", _opp_sync_hwm)
    except Exception as exc:
        logger.warning("Opportunity Supabase sync init failed: %s", exc)

    # Paper-trading window tracker: daily digest + one-time completion alert.
    _paper_tracker = None
    try:
        from config import PAPER_WINDOW_START, PAPER_WINDOW_START_TS, PAPER_WINDOW_DAYS
        if PAPER_WINDOW_START_TS and notifier:
            from paper_record import PaperRecordTracker
            _paper_tracker = PaperRecordTracker(
                db, notifier, window_start=PAPER_WINDOW_START_TS, window_days=PAPER_WINDOW_DAYS)
            logger.info("Paper-record tracker active: window %s + %d days",
                        PAPER_WINDOW_START, PAPER_WINDOW_DAYS)
    except Exception as exc:
        logger.warning("Paper-record tracker init failed: %s", exc)

    # Initialize PriceTracker for stale price detection (Layer 2)
    _price_tracker = None
    try:
        from price_tracker import PriceTracker
        from config import STALE_PRICE_THRESHOLD, STALE_PRICE_MOVE_PCT
        _price_tracker = PriceTracker(
            stale_threshold_seconds=STALE_PRICE_THRESHOLD,
            move_threshold_pct=STALE_PRICE_MOVE_PCT,
        )
        logger.info("PriceTracker enabled for stale price detection in continuous mode.")
    except Exception as exc:
        logger.debug("PriceTracker not available: %s", exc)

    # Initialize MarketMaker for passive MM (Layer 3)
    _market_maker = None
    try:
        from config import MM_ENABLED, MM_MIN_SPREAD, MM_QUOTE_SIZE, MM_MAX_INVENTORY, MM_MAX_TOTAL_EXPOSURE
        if MM_ENABLED:
            from market_maker import MarketMaker
            _market_maker = MarketMaker(
                min_spread=MM_MIN_SPREAD,
                quote_size=MM_QUOTE_SIZE,
                max_inventory=MM_MAX_INVENTORY,
                max_total_exposure=MM_MAX_TOTAL_EXPOSURE,
                dry_run=executor.dry_run,
            )
            logger.info("MarketMaker enabled in continuous mode (dry_run=%s).", executor.dry_run)
    except Exception as exc:
        logger.debug("MarketMaker not available: %s", exc)

    # Plan 10: Kalshi reward-MM pilot safety layer (docs/plans/10-mm-pilot-prep.md).
    # Independently gated from the legacy MM_ENABLED path above. The pilot
    # runs its own thread so the 2s fill poll / 10s quote refresh cadences
    # are not tied to the scan interval. Fails closed at every gate.
    # _mm_pilot / _mm_pilot_thread / _mm_pilot_stop are declared earlier in
    # this function (with the signal handler) — not re-declared here.
    if (config.MM_KALSHI_PILOT_ENABLED
            and getattr(args, "mode", None) == "mm-pilot"):
        try:
            from mm_pilot import (
                ControlsPoller,
                KalshiMMPilot,
                build_controls_client_from_env,
            )
            try:
                from alerting import alert_manager as _pilot_alerts
            except ImportError as exc:
                logger.debug("MM pilot: alerting unavailable (%s) — "
                             "halts will log but not page.", exc)
                _pilot_alerts = None
            _controls_client = None
            try:
                _controls_client = build_controls_client_from_env()
            except Exception as exc:
                logger.warning(
                    "MM pilot: Supabase controls client unavailable (%s) — "
                    "the kill-switch cache stays stale and the pilot fails "
                    "closed (no quotes).", exc)
            _mm_inv_balancer = None
            if getattr(config, "MM_CROSS_VENUE_SKEW_ENABLED", True):
                try:
                    from inventory_balancer import get_inventory_balancer
                    _mm_inv_balancer = get_inventory_balancer()
                except Exception as exc:
                    logger.debug("MM pilot: inventory balancer unavailable (%s)", exc)
            _mm_pilot = KalshiMMPilot(
                kalshi_client=kalshi_client,
                db=db,
                alert_manager=_pilot_alerts,
                controls=ControlsPoller(supabase_client=_controls_client),
                inventory_balancer=_mm_inv_balancer,
            )
            # Market selection is PR #43's select_lip_markets — not this
            # plan's job. Without it the pilot never receives a selection
            # snapshot and gate G4 fails closed (no quotes are placed).
            _mm_pilot_selection = None
            try:
                from scans.lip_select import select_lip_markets

                def _mm_pilot_selection():
                    return select_lip_markets(kalshi_client) or []
            except ImportError:
                logger.warning(
                    "MM pilot: scans.lip_select not available (PR #43 not "
                    "landed) — no market selection; G4 fails closed.")
            _mm_pilot_thread = threading.Thread(
                target=_mm_pilot.run_loop,
                args=(_mm_pilot_stop, _mm_pilot_selection),
                name="mm-pilot",
                daemon=True,
            )
            _mm_pilot_thread.start()
            dashboard_state.mm_pilot = _mm_pilot
            logger.info("Kalshi MM pilot started (dry_run=%s).",
                        _mm_pilot.dry_run)
        except Exception as exc:
            logger.exception("MM pilot failed to start: %s", exc)
            # If the failure landed after the thread started, signal it now
            # so it cancels any resting orders and exits (fail closed).
            _mm_pilot_stop.set()
            thread_stopped = True
            if _mm_pilot_thread is not None:
                # Thread.start() itself can fail; only join a thread that
                # actually started.
                if _mm_pilot_thread.ident is not None:
                    _mm_pilot_thread.join(timeout=15)
                # Mirror the end-of-run cleanup path's force-stop symmetry
                # (CodeRabbit round-2): a thread that started enough to
                # place live orders but didn't unwind within the join
                # timeout must still have those orders cancelled directly,
                # and the stuck thread logged rather than silently dropped
                # by clearing _mm_pilot/_mm_pilot_thread below.
                if _mm_pilot_thread.is_alive() and _mm_pilot is not None:
                    logger.warning(
                        "MM pilot startup-failure thread did not stop in "
                        "15s; forcing order cancel directly.")
                    _mm_pilot.stop()
                    _mm_pilot_thread.join(timeout=15)
                thread_stopped = not _mm_pilot_thread.is_alive()
            if thread_stopped:
                _mm_pilot = None
                _mm_pilot_thread = None
                dashboard_state.mm_pilot = None
            else:
                logger.critical(
                    "MM pilot startup-failure thread is still alive after "
                    "stop/join; retaining references and keeping the stop "
                    "signal asserted (fail closed).")

    # Initialize reward trackers for liquidity rewards (Layer 3)
    _reward_tracker = None
    _kalshi_reward_tracker = None
    _kalshi_vip_tracker = None
    try:
        if CONFIG_REWARDS_ENABLED:
            from market_maker import RewardTracker, KalshiRewardTracker
            _reward_tracker = RewardTracker()
            _kalshi_reward_tracker = KalshiRewardTracker(db)
            logger.info("Reward trackers enabled in continuous mode.")
        if CONFIG_KALSHI_VIP_TRACK_ENABLED and kalshi_client is not None:
            from kalshi_vip import KalshiVipTracker
            _kalshi_vip_tracker = KalshiVipTracker(kalshi_client)
            logger.info("Kalshi VIP volume tracking enabled (tracking-only).")
        if CONFIG_KALSHI_LIP_ENABLED:
            logger.info("Kalshi LIP scoring enabled; resting-order scores accrue per period.")
    except Exception as exc:
        logger.debug("Reward trackers not available: %s", exc)

    # Feed health tracking: WS messages feed the tracker; outage/recovery
    # transitions alert via the notifier (the 07-23 incident ran 31h silent).
    _feed_health = get_feed_health_tracker()

    def _on_feed_health_change(platform: str, is_healthy: bool):
        if is_healthy:
            msg = "arbgrid: %s feed RECOVERED — full detection resumed." % platform
        else:
            msg = ("arbgrid: %s feed DEGRADED — no WS messages for >%.0fs; "
                   "detection running blind on this venue." % (
                       platform, config.API_OUTAGE_STALE_THRESHOLD))
        logger.warning(msg)
        if notifier:
            # notify_text is a synchronous webhook POST — deliver off-thread
            # so a slow Slack endpoint can't stall WS message processing or
            # the event loop that invoked the health callback.
            def _send(m=msg):
                try:
                    notifier.notify_text(m)
                except Exception as e:
                    logger.warning("Feed-health alert failed to send: %s", e)
            threading.Thread(target=_send, name="feed-health-alert", daemon=True).start()

    _feed_health.register_health_callback(_on_feed_health_change)

    def on_price_update(platform, ticker, data):
        data["_ts"] = time.time()
        _feed_health.record_message(platform)
        with _price_cache_lock:
            price_cache[(platform, ticker)] = data

        tracking_price = _ws_tracking_probability(platform, data)

        # Feed PriceTracker for stale price detection
        if _price_tracker and tracking_price is not None:
            _price_tracker.update(platform, ticker, tracking_price)

        # Update MarketMaker mid-price for registered markets
        if _market_maker and tracking_price is not None:
            _market_maker.update_price(ticker, tracking_price)

        # Plan 10: feed the Kalshi MM pilot's book freshness + VolatilityTracker
        # with orderbook_delta ticks for subscribed pilot tickers.
        _route_kalshi_ws_to_mm_pilot(_mm_pilot, platform, ticker, data, tracking_price)

        # Sprint 3: Feed VolatilityTracker + LeadLagMM with per-tick prices
        _feed_sprint3_trackers(platform, ticker, data)

        nonlocal _seq_counter

        # Event-driven Cross detection (Phase 2): turn this WS tick into
        # an immediate Cross arb evaluation. Unlike opp_index below — which
        # only re-checks opps the slow scan already found — this surfaces
        # *new* Cross opps the moment a price moves into arb territory,
        # bypassing the 16-min scan-cycle latency entirely.
        if cross_pair_ws_enabled and ws_trigger_enabled and args.mode != config.RESEARCH_MODE:
            cross_min_profit = max(min_profit * _cross_pair_min_profit_factor,
                                   ws_trigger_threshold)
            for pair in cross_pair_index.lookup(platform, ticker):
                if _metrics:
                    _metrics.inc("cross_pair_eval_attempts")
                opp = cross_pair_index.evaluate(
                    pair, price_cache, min_profit=cross_min_profit,
                )
                if not opp:
                    continue
                if _metrics:
                    _metrics.inc("cross_pair_eval_hits")
                if not ws_trigger_deduper.admit(opp):
                    if _metrics:
                        _metrics.inc("cross_pair_trigger_duplicates")
                    continue
                market_name = opp.get("market", "?")
                logger.info(
                    "WS Cross trigger: %s profit=$%.4f (%s)",
                    market_name[:50], opp["net_profit"], opp["type"],
                )
                priority = -_execution_priority(opp)
                seq = _seq_counter
                _seq_counter += 1
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(
                        _priority_queue.put((priority, seq, opp)), loop
                    )
                    if _metrics:
                        _metrics.inc("cross_pair_triggers")
                except Exception as exc:
                    ws_trigger_deduper.forget(opp)
                    logger.debug("Cross priority push failed, skipping: %s", exc)

        # Event-driven execution: check if this update affects a tracked opportunity
        if not ws_trigger_enabled:
            return
        affected = opp_index.lookup(platform, ticker)
        if not affected:
            return
        for opp in affected:
            if not _is_execution_eligible(opp):
                continue
            # Recalculate profit using fresh WS price instead of stale value
            with _price_cache_lock:
                cached = price_cache.get((platform, ticker), {})
            new_price = _ws_opportunity_probability(opp, platform, cached)
            if new_price is None:
                continue
            recalculated_profit = _recalc_profit(opp, platform, ticker, new_price, price_cache)
            profit = recalculated_profit if recalculated_profit is not None else opp.get("net_profit", 0)
            if profit >= ws_trigger_threshold:
                market_name = opp.get("market", "?")
                # Push to priority queue for ordered execution (OPTIMIZE-03)
                # Time-sensitive opps (stale, resolution) get higher priority (lower value = dequeues first)
                opp_copy = dict(opp)
                opp_copy["net_profit"] = profit
                if not ws_trigger_deduper.admit(opp_copy):
                    continue
                priority = -_execution_priority(opp_copy)
                seq = _seq_counter
                _seq_counter += 1
                try:
                    loop = asyncio.get_event_loop()
                    asyncio.run_coroutine_threadsafe(
                        _priority_queue.put((priority, seq, opp_copy)), loop
                    )
                except Exception as exc:
                    ws_trigger_deduper.forget(opp_copy)
                    # Fallback: execute directly if queue push fails
                    logger.debug("Priority queue push failed, executing directly: %s", exc)
                    if not _execution_semaphore.acquire(blocking=False):
                        logger.debug("WS trigger: skipping %s — max concurrent executions reached",
                                     market_name[:30])
                        continue
                    lock = _get_market_lock(market_name)
                    if lock.acquire(blocking=False):
                        try:
                            logger.info("WS trigger: executing %s (profit $%.4f)",
                                        market_name[:30], profit)
                            executor.execute(opp_copy)
                        finally:
                            lock.release()
                            _execution_semaphore.release()
                    else:
                        _execution_semaphore.release()

    def _cleanup_price_cache():
        """Evict price cache entries older than the configured max age."""
        now = time.time()
        with _price_cache_lock:
            stale_keys = [k for k, v in price_cache.items()
                          if now - v.get("_ts", 0) > CONFIG_PRICE_CACHE_EVICTION_AGE]
            for k in stale_keys:
                del price_cache[k]
        if stale_keys:
            logger.debug("Evicted %d stale price cache entries.", len(stale_keys))

    # Crash recovery: reconcile orphaned positions from previous session
    reconcile_orphaned_positions(
        db,
        kalshi_client=kalshi_client,
        pm_trader=pm_trader,
        betfair_client=extra_clients.get("betfair"),
        smarkets_client=extra_clients.get("smarkets"),
        sxbet_client=extra_clients.get("sxbet"),
        matchbook_client=extra_clients.get("matchbook"),
        gemini_client=extra_clients.get("gemini"),
        ibkr_client=extra_clients.get("ibkr"),
    )

    # Initialize partial fill hedger for continuous mode
    hedger = None
    if CONFIG_HEDGE_ENABLED:
        from hedger import PartialFillHedger
        hedger = PartialFillHedger(
            pm_trader=pm_trader,
            kalshi_client=kalshi_client,
            betfair_client=extra_clients.get("betfair"),
            smarkets_client=extra_clients.get("smarkets"),
            sxbet_client=extra_clients.get("sxbet"),
            matchbook_client=extra_clients.get("matchbook"),
            gemini_client=extra_clients.get("gemini"),
            limitless_client=extra_clients.get("limitless"),
            db=db,
            price_cache=price_cache,
        )

    # Initialize snapshot recorder for backtesting data collection
    snapshot_recorder = None
    if CONFIG_SNAPSHOT_ENABLED:
        try:
            from snapshot import SnapshotRecorder
            snapshot_recorder = SnapshotRecorder()
            logger.info("Snapshot recording enabled (interval=%ds).", CONFIG_SNAPSHOT_INTERVAL)
        except Exception as e:
            logger.warning("Failed to initialize snapshot recorder: %s", e)

    _last_snapshot_time = 0.0
    _last_bankroll_refresh = 0.0
    _bankroll_refresh_interval = 300.0  # 5 minutes
    _last_daily_reset_date = time.strftime("%Y-%m-%d", time.gmtime())
    _last_fee_refresh = 0.0
    _last_backtest_run = 0.0
    _last_rebalance_digest = 0.0
    _last_correlation_tracker_run = 0.0

    # Monotonic sequence counter for PriorityQueue tie-breaking (thread-safe via GIL for int ops)
    _seq_counter = 0
    # asyncio.PriorityQueue for WS-triggered high-priority execution (OPTIMIZE-03)
    _priority_queue: asyncio.PriorityQueue = asyncio.PriorityQueue()

    # Import alert_manager for daily resets
    try:
        from alerting import alert_manager as _alert_manager
    except Exception:
        _alert_manager = None

    # Strategy #20 (B3): surface backtest-tuning status at startup. The
    # override values already took effect at config import (behind
    # BACKTEST_TUNING_ENABLED + the recommendation-age gate); this makes the
    # shift visible in logs and fires an alert so threshold changes between
    # restarts never pass silently.
    if config.BACKTEST_TUNING_ENABLED:
        _tuning_applied = getattr(config, "BACKTEST_RECOMMENDATIONS_APPLIED", False)
        # recommended_by_strategy_count is bookkeeping, not an override — a
        # dict containing only it means nothing actually changed.
        _overrides = {
            k: v for k, v in _tuning_applied.items()
            if not isinstance(v, (dict, list)) and k != "recommended_by_strategy_count"
        } if isinstance(_tuning_applied, dict) else {}
        _n_strategies = len(getattr(config, "RECOMMENDED_BY_STRATEGY", {}) or {})
        if _overrides or _n_strategies:
            _scalar_summary = ", ".join(f"{k}={v}" for k, v in _overrides.items())
            _tuning_msg = (
                f"Backtest tuning applied at startup: {_scalar_summary or 'no global overrides'}"
                f" ({_n_strategies} per-strategy overrides)"
            )
            logger.info(_tuning_msg)
            if _alert_manager:
                _alert_manager.alert(
                    "BACKTEST_TUNING_APPLIED", "INFO", _tuning_msg,
                    details=_overrides,
                )
        else:
            logger.info(
                "BACKTEST_TUNING_ENABLED is on but no recommendations were "
                "applied (file missing, stale, invalid, or contained no usable "
                "overrides) — running on env/default thresholds.")

    # Initialize credential health checker
    platform_clients = {
        "polymarket": None,  # Will be set from polymarket_api module functions
        "kalshi": kalshi_client,
        "betfair": extra_clients.get("betfair"),
        "smarkets": extra_clients.get("smarkets"),
        "sxbet": extra_clients.get("sxbet"),
        "matchbook": extra_clients.get("matchbook"),
        "gemini": extra_clients.get("gemini"),
        "ibkr": extra_clients.get("ibkr"),
        "limitless": extra_clients.get("limitless"),
    }
    # Remove None clients
    platform_clients = {k: v for k, v in platform_clients.items() if v is not None}

    # Kalshi self-heal state: last re-auth attempt timestamp (cooldown-gated
    # in the scan loop; see heal_kalshi_client). The alias is needed because
    # `platform_clients` is shadowed by a triangular-scan local inside the loop.
    _kalshi_reauth_last = 0.0
    _health_platform_clients = platform_clients
    # Latest measured credential-health results (platform -> bool), filled by
    # _monitor_credential_health; consumed by the /status health publisher.
    _cred_health_state: dict = {}

    health_checker = None
    if _alert_manager and platform_clients:
        from config import CREDENTIAL_HEALTH_CHECK_INTERVAL
        health_checker = CredentialHealthChecker(
            platform_clients=platform_clients,
            alert_manager=_alert_manager,
            interval_seconds=CREDENTIAL_HEALTH_CHECK_INTERVAL,
        )
        logger.info("Credential health checker initialized for %d platforms", len(platform_clients))

    # Initialize WebSocket feed manager
    feed_manager = FeedManager(
        on_price_update=on_price_update,
        kalshi_api_key_id=kalshi_api_key_id,
        kalshi_private_key_path=kalshi_private_key_path,
        kalshi_private_key_base64=kalshi_private_key_base64,
    )
    # Initialize cross-venue delta-neutral inventory balancer
    from inventory_balancer import get_inventory_balancer
    inventory_balancer = get_inventory_balancer()

    if executor is not None:
        executor.feed_manager = feed_manager
        if hasattr(executor, "risk") and executor.risk is not None:
            executor.risk.inventory_balancer = inventory_balancer
    if hedger is not None:
        hedger.feed_manager = feed_manager

    async def _priority_consumer():
        """Drain the priority queue, executing WS-triggered opps in priority order.

        Time-sensitive opportunities (StalePriceOpp, ResolutionSnipeOpp) are
        inserted with a lower queue value and thus execute before lower-priority
        types. Logs a warning if execution latency exceeds 500ms (OPTIMIZE-03).
        """
        while not shutdown_event.is_set():
            try:
                try:
                    item = await asyncio.wait_for(_priority_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                _priority_val, _seq, opp = item
                if not _is_execution_eligible(opp):
                    _priority_queue.task_done()
                    continue
                market_name = opp.get("market", "?")
                profit = opp.get("net_profit", 0)

                if not _execution_semaphore.acquire(blocking=False):
                    logger.debug(
                        "Priority consumer: skipping %s — semaphore full", market_name[:30])
                    _priority_queue.task_done()
                    continue

                lock = _get_market_lock(market_name)
                if lock.acquire(blocking=False):
                    try:
                        _exec_start = time.time()
                        logger.info(
                            "Priority queue execute: %s (profit $%.4f, priority %.3f)",
                            market_name[:30], profit, -_priority_val,
                        )
                        result = executor.execute(opp)
                        _exec_elapsed_ms = (time.time() - _exec_start) * 1000
                        if _exec_elapsed_ms > 500:
                            logger.warning(
                                "Priority execution latency %.0fms exceeded 500ms for %s",
                                _exec_elapsed_ms, market_name[:30],
                            )
                        # Wire loss spike alerting (MONITOR-03)
                        if result is False and _alert_manager:
                            try:
                                loss = abs(profit)
                                _alert_manager.check_loss_spike(loss)
                            except Exception:
                                pass
                    finally:
                        lock.release()
                        _execution_semaphore.release()
                else:
                    _execution_semaphore.release()

                _priority_queue.task_done()
            except Exception as exc:
                logger.debug("Priority consumer error: %s", exc)

    async def _monitor_feed_staleness():
        """Background task: mark stale feeds every 5 seconds.

        Checks if WebSocket feeds have gone silent for 30+ seconds and marks
        all cached prices from stale feeds with _stale: true. When feeds
        recover, clears the stale flag.
        """
        while not shutdown_event.is_set():
            try:
                feed_manager.mark_stale_feeds(stale_threshold_seconds=30.0)
                await asyncio.sleep(5)  # Check every 5 seconds
            except Exception as e:
                logger.warning("Feed staleness check failed: %s", e)
                await asyncio.sleep(5)  # Retry after 5 seconds

    async def _monitor_feed_health():
        """Background task: evaluate feed outages every 30s.

        check_outages() fires the registered degradation/recovery alerts and
        its result is published to /status as per-platform health so the
        external monitor can page on a degraded venue, not just a dead pod.
        """
        while not shutdown_event.is_set():
            try:
                outages = _feed_health.check_outages()
                dashboard_state.platform_health = {
                    "feeds": {
                        p: {
                            "healthy": not info["in_outage"],
                            "last_message_ago_s": round(info["last_message_ago"], 1),
                        }
                        for p, info in outages.items()
                    },
                    # Measured credential health from the 30-min checker;
                    # "unknown" until a platform's first check completes.
                    "clients": {
                        name: _cred_health_state.get(name, "unknown")
                        for name in _health_platform_clients
                    },
                }
            except Exception as e:
                logger.warning("Feed health check failed: %s", e)
            await asyncio.sleep(30)

    async def _monitor_credential_health():
        """Background task: check API credential health every 30 minutes.

        Probes each platform's auth status with a cheap endpoint, detects
        invalid credentials or approaching token expiry, and fires alerts.
        """
        while not shutdown_event.is_set():
            try:
                if health_checker:
                    results = await health_checker.check_all_platforms()
                    logger.info("Credential health check complete: %s", results)
                    if isinstance(results, dict):
                        _cred_health_state.update(results)
                await asyncio.sleep(1800)  # 30 minutes
            except Exception as e:
                logger.warning("Credential health check failed: %s", e)
                await asyncio.sleep(1800)  # Retry after 30 minutes

    async def _continuous_loop():
        # Capture the running loop for the direct OS signal handler.  Its
        # call_soon_threadsafe wakeup preserves immediate pilot cancellation
        # while also interrupting a selector wait on macOS.
        nonlocal _signal_loop, kalshi_client, _kalshi_reauth_last
        _signal_loop = asyncio.get_running_loop()

        ws_task = None
        priority_consumer_task = None
        stale_monitor_task = None
        feed_health_task = None
        health_monitor_task = None
        scan_count = 0

        # Start priority consumer coroutine as a background task
        priority_consumer_task = asyncio.create_task(_priority_consumer())
        logger.info("Priority execution consumer started.")

        # Start feed staleness monitor as a background task
        stale_monitor_task = asyncio.create_task(_monitor_feed_staleness())

        # Start feed health monitor (outage alerts + /status platform health)
        feed_health_task = asyncio.create_task(_monitor_feed_health())
        logger.info("Feed health monitor started.")

        # Start credential health monitor as a background task
        if health_checker:
            health_monitor_task = asyncio.create_task(_monitor_credential_health())
            logger.info("Credential health monitor started.")
        logger.info("Feed staleness monitor started.")

        while not shutdown_event.is_set():
            scan_count += 1
            logger.info("=" * 80)
            logger.info("CONTINUOUS SCAN #%d", scan_count)
            logger.info("=" * 80)

            _scan_start = time.time()
            _stage_timings: dict[str, float] = {}
            _stage_display_start: float | None = None

            # Self-heal a degraded Kalshi start (e.g. boot during the venue's
            # daily maintenance window): re-auth on a cooldown until it works.
            if (kalshi_client is None and kalshi_creds_configured()
                    and _scan_start - _kalshi_reauth_last >= config.KALSHI_REAUTH_INTERVAL):
                _kalshi_reauth_last = _scan_start
                # Synchronous login can block up to ~30s on a dead venue —
                # run it off the event loop so WS execution keeps moving.
                healed = await asyncio.get_running_loop().run_in_executor(
                    None, heal_kalshi_client,
                    executor, _health_platform_clients, hedger, notifier)
                if healed is not None:
                    kalshi_client = healed

            # Daily reset for metrics and alert state
            nonlocal _last_daily_reset_date
            _today = time.strftime("%Y-%m-%d", time.gmtime())
            if _today != _last_daily_reset_date:
                logger.info("Daily reset triggered (new day: %s)", _today)
                if _metrics:
                    _metrics.reset_daily()
                if _alert_manager:
                    _alert_manager.reset_daily()
                _last_daily_reset_date = _today
                if _paper_tracker:
                    _paper_tracker.on_day_boundary(time.time())

            # Funnel telemetry: initialize cycle
            _funnel = get_funnel_tracker() if get_funnel_tracker else None
            if _funnel:
                _funnel.start_cycle()

            try:
                from concurrent.futures import ThreadPoolExecutor

                # Stage 1: Fetch data from all platforms in parallel
                poly_markets = []
                poly_events = None
                poly_reward_markets = []
                kalshi_data = None

                with _StageTimer("fetch", _stage_timings):
                    fetch_futures = {}
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        if polymarket_scan_enabled(args.mode):
                            fetch_futures["poly_markets"] = pool.submit(fetch_all_markets)
                        if polymarket_scan_enabled(args.mode) and args.mode in ("all", "negrisk", "multi-cross"):
                            fetch_futures["poly_events"] = pool.submit(fetch_events)
                        if polymarket_reward_fetch_enabled(args.mode) and CONFIG_REWARDS_ENABLED:
                            fetch_futures["poly_reward_markets"] = pool.submit(fetch_reward_markets)
                        if args.mode in ("all", "kalshi", "cross", "spread", "multi-cross", "rewards", "temporal",
                                         config.RESEARCH_MODE) and kalshi_client:
                            fetch_futures["kalshi_data"] = pool.submit(_fetch_kalshi_data, kalshi_client)

                        for key, future in fetch_futures.items():
                            try:
                                result = future.result()
                                if key == "poly_markets":
                                    poly_markets = result or []
                                elif key == "poly_events":
                                    poly_events = result
                                elif key == "poly_reward_markets":
                                    poly_reward_markets = result or []
                                elif key == "kalshi_data":
                                    kalshi_data = result
                            except Exception as e:
                                logger.error("Failed to fetch %s: %s", key, e)

                if config.DISPUTE_GATE_ENABLED and db and (poly_markets or poly_events):
                    from uma_monitor import fetch_dispute_states
                    try:
                        items = list(poly_markets or []) + list(poly_events or [])
                        db.upsert_dispute_state(fetch_dispute_states(items))
                        if executor and hasattr(executor, "risk_manager"):
                            executor.risk_manager.uma_state_unavailable = False
                    except Exception as e:
                        logger.error("Failed to update UMA dispute states (failing closed): %s", e)
                        if executor and hasattr(executor, "risk_manager"):
                            executor.risk_manager.uma_state_unavailable = True

                all_opportunities = []

                # Stage 2: Run scans in parallel
                with _StageTimer("scan_parallel", _stage_timings):
                    scan_futures = {}
                    with ThreadPoolExecutor(max_workers=4) as pool:
                        if args.mode in ("all", "binary") and poly_markets:
                            scan_futures["binary"] = pool.submit(
                                scan_binary_internal, poly_markets, min_profit,
                                price_cache=price_cache, feed_manager=feed_manager)
                        if args.mode in ("all", "negrisk") and poly_events:
                            scan_futures["negrisk"] = pool.submit(
                                scan_negrisk_internal, poly_events, min_profit,
                                price_cache=price_cache, feed_manager=feed_manager)
                        if args.mode in ("all", "kalshi", config.RESEARCH_MODE) and kalshi_client:
                            scan_futures["kalshi_binary"] = pool.submit(
                                scan_kalshi_binary, kalshi_client, min_profit, kalshi_data=kalshi_data,
                                price_cache=price_cache, feed_manager=feed_manager)
                            # KalshiMulti kill-switch: disable for thin multi-outcome markets
                            # that cause Fill-or-Kill partial fills (no exit liquidity for hedge)
                            if config.KALSHI_MULTI_ENABLED:
                                scan_futures["kalshi_multi"] = pool.submit(
                                    scan_kalshi_multi, kalshi_client, min_profit, kalshi_data=kalshi_data,
                                    price_cache=price_cache, feed_manager=feed_manager)

                        for key, future in scan_futures.items():
                            try:
                                opps = future.result()
                                all_opportunities.extend(opps)
                            except Exception as e:
                                logger.error("Scan %s failed: %s", key, e)

                # Stage 3: Cross-platform scans (need data from above)
                kalshi_events_preloaded = kalshi_data[0] if kalshi_data else None
                _stage3_start = time.time()

                # Rebuild the persistent CrossPairIndex used by on_price_update
                # for event-driven Cross detection. Tying this rebuild to the
                # scan cycle (vs a separate timer) reuses the data we just
                # fetched for free; the WS handler then evaluates pairs on
                # every tick without waiting for the 16-min cycle to find them.
                if (cross_pair_ws_enabled
                        and args.mode != config.RESEARCH_MODE
                        and poly_markets and kalshi_events_preloaded):
                    try:
                        n_pairs = cross_pair_index.rebuild(
                            poly_markets, kalshi_events_preloaded,
                            min_confidence=args.min_confidence,
                        )
                        logger.info("CrossPairIndex active: %d pairs available for WS-driven evaluation", n_pairs)
                        if _metrics:
                            _metrics.set("cross_pair_index_size", value=n_pairs)
                    except Exception as exc:
                        logger.warning("CrossPairIndex rebuild failed (non-fatal): %s", exc, exc_info=True)

                if args.mode in ("all", "cross"):
                    cross_opps = scan_cross_platform(
                        poly_markets, kalshi_client, min_profit,
                        min_confidence=args.min_confidence,
                        kalshi_events_preloaded=kalshi_events_preloaded,
                        price_cache=price_cache,
                        feed_manager=feed_manager,
                    )
                    all_opportunities.extend(cross_opps)

                if args.mode == "cross-all":
                    platform_clients = {}
                    for name, client in extra_clients.items():
                        if client:
                            try:
                                if name == "betfair":
                                    events = client.list_events()
                                    markets = []
                                    for ev in events[:50]:
                                        ev_data = ev.get("event", {})
                                        ev_id = ev_data.get("id", "")
                                        if ev_id:
                                            mkt_list = client.list_markets(ev_id)
                                            markets.extend(mkt_list)
                                elif name in ("smarkets", "sxbet", "matchbook", "gemini", "ibkr"):
                                    markets = client.fetch_all_markets()
                                else:
                                    markets = []
                                if markets:
                                    platform_clients[name] = (client, markets)
                            except Exception as e:
                                logger.warning("Failed to fetch %s markets: %s", name, e)

                    cross_all_opps = scan_cross_all(
                        poly_markets, platform_clients, min_profit,
                        min_confidence=args.min_confidence,
                        price_cache=price_cache,
                    )
                    all_opportunities.extend(cross_all_opps)

                _stage_timings["cross"] = time.time() - _stage3_start
                _stage4_start = time.time()

                # Stage 4: Platform-specific scans (spread, betfair, etc.)
                if args.mode in ("all", "spread"):
                    if poly_markets:
                        spread_pm = scan_spread_polymarket(poly_markets, min_profit)
                        all_opportunities.extend(spread_pm)

                if args.mode in ("all", "betfair"):
                    betfair = extra_clients.get("betfair")
                    if betfair:
                        bf_backall = scan_betfair_backall(betfair, min_profit)
                        all_opportunities.extend(bf_backall)
                        bf_backlay = scan_betfair_backlay(betfair, min_profit)
                        all_opportunities.extend(bf_backlay)

                if args.mode in ("all", "smarkets"):
                    smarkets = extra_clients.get("smarkets")
                    if smarkets:
                        sm_backall = scan_smarkets_backall(smarkets, min_profit)
                        all_opportunities.extend(sm_backall)
                        sm_backlay = scan_smarkets_backlay(smarkets, min_profit)
                        all_opportunities.extend(sm_backlay)

                if args.mode in ("all", "sxbet"):
                    sxbet = extra_clients.get("sxbet")
                    if sxbet:
                        sx_backall, sx_backlay = scan_sxbet(sxbet, min_profit)
                        all_opportunities.extend(sx_backall)
                        all_opportunities.extend(sx_backlay)

                if args.mode in ("all", "matchbook"):
                    matchbook = extra_clients.get("matchbook")
                    if matchbook:
                        mb_backall = scan_matchbook_backall(matchbook, min_profit)
                        all_opportunities.extend(mb_backall)
                        mb_backlay = scan_matchbook_backlay(matchbook, min_profit)
                        all_opportunities.extend(mb_backlay)

                if args.mode in ("all", "gemini"):
                    gemini = extra_clients.get("gemini")
                    if gemini:
                        gm_binary = scan_gemini_binary(gemini, min_profit)
                        all_opportunities.extend(gm_binary)
                        gm_multi = scan_gemini_multi(gemini, min_profit)
                        all_opportunities.extend(gm_multi)

                if args.mode in ("all", "ibkr"):
                    ibkr = extra_clients.get("ibkr")
                    if ibkr:
                        ibkr_binary = scan_ibkr_binary(ibkr, min_profit)
                        all_opportunities.extend(ibkr_binary)

                _stage_timings["per_exchange"] = time.time() - _stage4_start
                _stage5_start = time.time()

                if args.mode in ("all", "event") and event_monitor:
                    platform_markets_for_event = {}
                    if poly_markets:
                        platform_markets_for_event["polymarket"] = poly_markets
                    if kalshi_data and kalshi_data[0]:
                        platform_markets_for_event["kalshi"] = kalshi_data[0]
                    if platform_markets_for_event:
                        event_opps = event_monitor.scan_event_divergences(
                            platform_markets_for_event, min_profit=min_profit)
                        all_opportunities.extend(event_opps)

                if args.mode in ("all", "triangular", "nway"):
                    platform_markets_for_tri = {}
                    platform_clients_for_tri = {}
                    if poly_markets:
                        platform_markets_for_tri["polymarket"] = poly_markets
                    if kalshi_data and kalshi_data[0]:
                        platform_markets_for_tri["kalshi"] = kalshi_data[0]
                        platform_clients_for_tri["kalshi"] = kalshi_client
                    for name, client in extra_clients.items():
                        if client:
                            try:
                                if name == "betfair":
                                    events = client.list_events()
                                    markets = []
                                    for ev in events[:50]:
                                        ev_data = ev.get("event", {})
                                        ev_id = ev_data.get("id", "")
                                        if ev_id:
                                            mkt_list = client.list_markets(ev_id)
                                            markets.extend(mkt_list)
                                elif name in ("smarkets", "sxbet", "matchbook", "gemini", "ibkr"):
                                    markets = client.fetch_all_markets()
                                else:
                                    markets = []
                                if markets:
                                    platform_markets_for_tri[name] = markets
                                    platform_clients_for_tri[name] = client
                            except Exception as e:
                                logger.warning("Triangular: failed to fetch %s: %s", name, e)
                    if args.mode in ("all", "triangular"):
                        tri_opps = scan_triangular(
                            platform_markets_for_tri, platform_clients_for_tri, min_profit,
                            min_confidence=args.min_confidence,
                        )
                        all_opportunities.extend(tri_opps)

                    if args.mode in ("all", "nway"):
                        try:
                            nway_opps = scan_nway_arb(
                                platform_markets_for_tri, platform_clients_for_tri,
                                min_profit, min_confidence=args.min_confidence,
                            )
                            all_opportunities.extend(nway_opps)
                        except Exception as exc:
                            logger.warning("NWayArb scan failed: %s", exc)

                # Sprint 3: LeadLagMM periodic scan. Mirrors the cli.py
                # --mode lead-lag-mm dispatch — uses poly+kalshi matched pairs
                # and asks the LeadLagMM detector which platform is lagging.
                # No-op when LEAD_LAG_MM_ENABLED is false (the scan itself
                # short-circuits on the flag).
                if args.mode in ("all", "lead-lag-mm"):
                    try:
                        from matcher import match_cross_platform
                        kalshi_events_for_ll = kalshi_data[0] if kalshi_data else None
                        if poly_markets and kalshi_events_for_ll:
                            pairs = match_cross_platform(
                                poly_markets, kalshi_events_for_ll,
                                platform_a="polymarket", platform_b="kalshi",
                            )
                            ll_opps = scan_lead_lag_mm(pairs)
                            all_opportunities.extend(ll_opps)
                    except Exception as exc:
                        logger.warning("LeadLagMM scan failed: %s", exc)

                # Sprint 3: ToxicFlowPause + VolatilityAdjustedMM periodic
                # observability scans. Both consume a market_keys list (built
                # once from currently-known poly+kalshi markets) and inspect
                # the respective detector singletons. No-op when their flags
                # are false.
                if args.mode in ("all", "toxic-flow", "vol-mm"):
                    try:
                        observability_market_keys: list[str] = []
                        if poly_markets:
                            for mkt in poly_markets[:50]:
                                cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
                                if cid:
                                    observability_market_keys.append(cid)
                        if kalshi_data and kalshi_data[0]:
                            for evt in kalshi_data[0][:50]:
                                for mkt in evt.get("markets", [evt]):
                                    t = mkt.get("ticker", "")
                                    if t:
                                        observability_market_keys.append(t)
                        # Plan 10: pilot tickers get toxic-flow observability
                        # even when they don't surface in the scan data.
                        if _mm_pilot:
                            for _pt in _mm_pilot.pilot_tickers():
                                if _pt not in observability_market_keys:
                                    observability_market_keys.append(_pt)
                        if args.mode in ("all", "toxic-flow"):
                            tox_opps = scan_toxic_flow_pause(observability_market_keys)
                            all_opportunities.extend(tox_opps)
                        if args.mode in ("all", "vol-mm"):
                            vol_opps = scan_volatility_adjusted_mm(observability_market_keys)
                            all_opportunities.extend(vol_opps)
                    except Exception as exc:
                        logger.warning("ToxicFlow/VolMM scans failed: %s", exc)

                # MultiCross kill-switch: same FOK partial-fill vulnerability
                # as KalshiMulti. Places N legs concurrently on thin Kalshi
                # multi-outcome markets, leaving unhedgeable orphans when legs
                # fail. Disabled until depth gate is added.
                if (args.mode in ("all", "multi-cross")
                        and poly_events and kalshi_client
                        and config.MULTI_CROSS_ENABLED):
                    mc_opps = scan_multi_cross(
                        poly_events, kalshi_client, min_profit,
                        kalshi_data=kalshi_data,
                        price_cache=price_cache,
                    )
                    all_opportunities.extend(mc_opps)

                # Layer 3: Liquidity Rewards
                reward_opps = _scan_rewards_continuous(
                    mode=args.mode,
                    poly_reward_markets=poly_reward_markets,
                    reward_tracker=_reward_tracker,
                    kalshi_client=kalshi_client,
                    kalshi_reward_tracker=_kalshi_reward_tracker,
                    kalshi_data=kalshi_data,
                    limitless_client=extra_clients.get("limitless"),
                    price_cache=price_cache,
                )
                all_opportunities.extend(reward_opps)

                # Kalshi VIP: passive volume-rebate tracking (no execution path).
                if _kalshi_vip_tracker is not None:
                    try:
                        # Monotonic clock: immune to wall-clock jumps over a long run.
                        now_ts = time.monotonic()
                        if now_ts - _kalshi_vip_tracker.last_poll_ts >= CONFIG_KALSHI_VIP_POLL_INTERVAL:
                            _kalshi_vip_tracker.last_poll_ts = now_ts
                            vip_summary = _kalshi_vip_tracker.summarize_since()
                            logger.info(
                                "Kalshi VIP: %d eligible contracts, est. cap $%.4f",
                                vip_summary["eligible_contracts"],
                                vip_summary["reward_cap_usd"],
                            )
                    except Exception as exc:
                        logger.debug("Kalshi VIP tracking error: %s", exc)

                # Layer 4: informed-trading scans (flag-gated; disabled by
                # default). Each helper gates on its own config flag + mode and
                # returns [] when off. Failures log at WARNING (not the old
                # silent debug) so an enabled-but-broken strategy stays visible.
                try:
                    all_opportunities.extend(
                        _scan_imbalance_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Imbalance scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_news_snipe_layer4(poly_markets, args.mode)
                    )
                except Exception as exc:
                    logger.warning("News-snipe scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_correlated_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Correlated-pairs scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_time_decay_layer4(poly_markets, price_cache, args.mode)
                    )
                except Exception as exc:
                    logger.warning("Time-decay scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_frechet_layer1(
                            poly_markets,
                            args.mode,
                            min_profit,
                            price_cache=price_cache,
                            funnel=_funnel,
                        )
                    )
                except Exception as exc:
                    logger.warning("Fréchet scan failed: %s", exc)

                try:
                    kalshi_flat = []
                    if kalshi_data and kalshi_data[0]:
                        for evt in kalshi_data[0]:
                            for mkt in evt.get("markets", [evt]):
                                kalshi_flat.append(mkt)
                    all_opportunities.extend(
                        _scan_temporal_layer1(
                            kalshi_flat,
                            args.mode,
                            min_profit,
                            kalshi_client=kalshi_client,
                            price_cache=price_cache,
                            funnel=_funnel,
                        )
                    )
                except Exception as exc:
                    logger.warning("Temporal scan failed: %s", exc)

                try:
                    all_opportunities.extend(
                        _scan_ctf_layer1(
                            poly_markets,
                            args.mode,
                            min_profit,
                            price_cache=price_cache,
                            funnel=_funnel,
                        )
                    )
                except Exception as exc:
                    logger.warning("CTF primitives scan failed: %s", exc)

                # Structural alpha: Combinatorial logical arbitrage (Phase 9)
                if args.mode in ("all", "logical-arb"):
                    try:
                        from config import LOGICAL_ARB_ENABLED, LOGICAL_ARB_RULES, LOGICAL_ARB_PRICE_THRESHOLD
                        if LOGICAL_ARB_ENABLED and LOGICAL_ARB_RULES:
                            from scans.logical_arb import scan_logical_arb
                            logical_arb_opps = scan_logical_arb(
                                markets_by_key=poly_markets if poly_markets else [],
                                logical_arb_rules=LOGICAL_ARB_RULES,
                                price_threshold=LOGICAL_ARB_PRICE_THRESHOLD,
                            )
                            all_opportunities.extend(logical_arb_opps)
                            logger.info("Logical arb scan: found %d opportunities", len(logical_arb_opps))
                    except Exception as e:
                        logger.debug("Logical arb scan failed: %s", e)

                # Structural alpha: Whale copy trading (Phase 9)
                if args.mode in ("all", "whale-copy"):
                    try:
                        from config import WHALE_COPY_ENABLED, WHALE_WALLETS, POLYGONSCAN_API_KEY
                        if WHALE_COPY_ENABLED and WHALE_WALLETS:
                            from scans.whale_copy import scan_whale_copy
                            from polygonscan_api import PolygonscanClient
                            polygonscan = PolygonscanClient(api_key=POLYGONSCAN_API_KEY)
                            whale_copy_opps = scan_whale_copy(
                                whale_wallets=WHALE_WALLETS,
                                polygonscan_client=polygonscan,
                                last_block_cache=None,
                            )
                            all_opportunities.extend(whale_copy_opps)
                            logger.info("Whale copy scan: found %d opportunities", len(whale_copy_opps))
                    except Exception as e:
                        logger.debug("Whale copy scan failed: %s", e)

                # Seed PriceTracker from REST data (WS only covers subscribed markets)
                if _price_tracker:
                    if poly_markets:
                        for mkt in poly_markets:
                            cid = mkt.get("condition_id", "")
                            tokens = mkt.get("tokens", [])
                            for t in tokens:
                                if t.get("outcome", "").lower() == "yes":
                                    p = t.get("price")
                                    if p and cid:
                                        _price_tracker.update("polymarket", cid, float(p))
                    if kalshi_data and kalshi_data[0]:
                        for evt in kalshi_data[0]:
                            for mkt in evt.get("markets", [evt]):
                                ticker = mkt.get("ticker", "")
                                yp = mkt.get("yes_ask") or mkt.get("yes_price")
                                if ticker and yp:
                                    pv = float(yp)
                                    if pv > 1:
                                        pv /= 100.0
                                    _price_tracker.update("kalshi", ticker, pv)

                # Layer 2: Stale price detection (continuous mode — tracker has WS + REST data)
                if args.mode in ("all", "stale") and _price_tracker:
                    try:
                        from scans.stale import scan_stale_prices
                        from config import STALE_PRICE_MOVE_PCT, STALE_PRICE_THRESHOLD
                        # Build matched_markets from all keys the tracker has across 2+ platforms
                        _all_tracker_keys = set()
                        with _price_tracker._lock:
                            for mkey, plats in _price_tracker._prices.items():
                                if len(plats) >= 2:
                                    _all_tracker_keys.add(mkey)
                        _matched_for_stale = [{"market_key": k} for k in _all_tracker_keys]
                        stale_opps = scan_stale_prices(
                            _price_tracker, _matched_for_stale,
                            min_move_pct=STALE_PRICE_MOVE_PCT,
                            min_stale_seconds=STALE_PRICE_THRESHOLD, min_profit=min_profit,
                        )
                        all_opportunities.extend(stale_opps)
                    except Exception as exc:
                        logger.debug("Stale price scan failed: %s", exc)

                # Layer 2: Resolution sniping
                if args.mode in ("all", "resolution") and poly_markets:
                    try:
                        from scans.resolution import scan_resolution_snipes
                        res_opps = scan_resolution_snipes(
                            poly_markets, platform="polymarket", min_profit=min_profit,
                        )
                        all_opportunities.extend(res_opps)
                    except Exception as exc:
                        logger.debug("Resolution snipe scan failed: %s", exc)

                # Kalshi resolution sniping
                if args.mode in ("all", "resolution") and kalshi_data:
                    try:
                        from scans.resolution import scan_resolution_snipes
                        # kalshi_data is (events, markets_by_event, event_titles)
                        # Flatten markets_by_event dict into a flat list for resolution scan
                        kalshi_flat_markets = []
                        if len(kalshi_data) >= 2 and kalshi_data[1]:
                            for _evt_ticker, _mkts in kalshi_data[1].items():
                                kalshi_flat_markets.extend(_mkts)
                        if kalshi_flat_markets:
                            k_res_opps = scan_resolution_snipes(
                                kalshi_flat_markets, platform="kalshi", min_profit=min_profit,
                            )
                            all_opportunities.extend(k_res_opps)
                    except Exception as exc:
                        logger.debug("Kalshi resolution snipe scan failed: %s", exc)

                # Layer 4: Cross-platform convergence
                if args.mode in ("all", "convergence"):
                    try:
                        from scans.convergence import scan_convergence
                        from config import CONVERGENCE_MIN_DIVERGENCE, CONVERGENCE_MIN_PLATFORMS
                        from matcher import match_cross_platform
                        from scans.convergence_inputs import build_convergence_matched
                        _conv_matched = build_convergence_matched(
                            poly_markets or [],
                            kalshi_data[0] if kalshi_data and kalshi_data[0] else [],
                            _convergence_match_cache,
                            matcher_fn=match_cross_platform,
                            min_confidence=args.min_confidence,
                            min_platforms=CONVERGENCE_MIN_PLATFORMS,
                            now=time.time(),
                        )
                        conv_opps = scan_convergence(
                            _conv_matched, min_divergence=CONVERGENCE_MIN_DIVERGENCE,
                            min_platforms=CONVERGENCE_MIN_PLATFORMS, min_profit=min_profit,
                        )
                        all_opportunities.extend(conv_opps)
                    except Exception as exc:
                        logger.debug("Convergence scan failed: %s", exc)

                # Layer 3: Market making — refresh quotes and generate pseudo-opps
                if args.mode in ("all", "mm") and _market_maker:
                    try:
                        # Register any new liquid markets
                        if poly_markets:
                            for mkt in poly_markets[:20]:
                                tokens = mkt.get("tokens", [])
                                if tokens:
                                    price = tokens[0].get("price")
                                    if price and 0.1 < float(price) < 0.9:
                                        cid = mkt.get("condition_id", "")
                                        if cid:
                                            _market_maker.add_market(cid, "polymarket", float(price))
                        # Refresh quotes
                        _market_maker.refresh_quotes(trader=pm_trader if not executor.dry_run else None)
                        mm_opps = _market_maker.generate_opportunities()
                        all_opportunities.extend(mm_opps)
                    except Exception as exc:
                        logger.debug("Market maker scan failed: %s", exc)

                # Periodic price tracker cleanup
                if _price_tracker and scan_count % 10 == 0:
                    _price_tracker.cleanup(max_age_seconds=300)

                # Platform fund rebalancing check (every 5 scans)
                nonlocal _opp_sync_inflight
                if _opp_sync and scan_count % 5 == 0 and not _opp_sync_inflight:
                    # Offload the Supabase HTTP call — inline it would stall the
                    # event loop (WS handling, priority execution) during a slow
                    # or unreachable remote. Same pattern as the nightly backtest.
                    _opp_sync_inflight = True
                    async def _run_opp_sync():
                        nonlocal _opp_sync_hwm, _opp_sync_inflight
                        try:
                            hwm = _opp_sync_hwm
                            loop = asyncio.get_event_loop()
                            _opp_sync_hwm = await loop.run_in_executor(
                                None, lambda: _opp_sync.sync_opportunities(after_id=hwm))
                        except Exception as exc:
                            logger.warning("Opportunity Supabase sync failed (will retry): %s", exc)
                        finally:
                            _opp_sync_inflight = False
                    asyncio.ensure_future(_run_opp_sync())

                if scan_count % 5 == 0 and notifier:
                    try:
                        _check_platform_balance(
                            executor, all_opportunities, notifier, scan_count)
                    except Exception as exc:
                        logger.debug("Rebalancing check failed: %s", exc)

                # Cross-venue delta-neutral inventory balance evaluation
                if inventory_balancer and inventory_balancer.enabled and executor and hasattr(executor, "db"):
                    try:
                        inventory_balancer.sync_from_db(executor.db)
                        imbalances = inventory_balancer.get_imbalances()
                        if imbalances:
                            logger.info("InventoryBalancer detected %d imbalanced markets", len(imbalances))
                            proposals = inventory_balancer.generate_rebalancing_proposals(
                                imbalances,
                                feed_manager=feed_manager,
                                price_cache=price_cache,
                                kalshi_client=kalshi_client,
                            )
                            for prop in proposals:
                                logger.info("Inventory Rebalance Proposal: %s", prop.get("reason"))

                            # Execute rebalancing proposals if automated rebalance is enabled
                            if getattr(config, "INVENTORY_AUTO_REBALANCE_ENABLED", False):
                                is_dry_run = getattr(args, "dry_run", True) or getattr(executor, "dry_run", True)
                                rebal_results = inventory_balancer.execute_rebalancing_proposals(
                                    proposals,
                                    dry_run=is_dry_run,
                                    kalshi_client=kalshi_client,
                                    polymarket_client=pm_trader,
                                    trade_db=executor.db,
                                )
                                for res in rebal_results:
                                    if res.get("executed"):
                                        logger.info(
                                            "Executed auto-rebalance [%s]: %s %s %.1f @ %.3f on %s",
                                            res.get("status"),
                                            res.get("action", "buy"),
                                            res.get("outcome", "").upper(),
                                            res.get("size", 0.0),
                                            res.get("price", 0.0),
                                            res.get("venue", ""),
                                        )
                    except Exception as exc:
                        logger.debug("InventoryBalancer evaluation failed: %s", exc)


                # Apply filters
                _pre_depth_count = len(all_opportunities)
                if args.min_depth > 0:
                    all_opportunities = [
                        opp for opp in all_opportunities
                        if (opp.get("_clob_depth") or 0) >= args.min_depth
                    ]
                    if _funnel:
                        _funnel.record_depth_dropped(_pre_depth_count - len(all_opportunities))

                all_opportunities.sort(key=_execution_priority, reverse=True)

                _stage_timings["advanced"] = time.time() - _stage5_start
                _stage_display_start = time.time()

                if args.limit:
                    all_opportunities = all_opportunities[:args.limit]

                display_results(all_opportunities, args.json)

                # Send webhook notification
                if notifier and all_opportunities:
                    notifier.notify(all_opportunities)

                # Finalize funnel metrics for the cycle
                if _funnel:
                    _funnel.record_surfaced(len(all_opportunities))
                    _cycle_funnel = _funnel.finish_cycle()
                    logger.info(_funnel.summary_log(scan_count))
                    dashboard_state.funnel_stats = _cycle_funnel
                    if _metrics:
                        for _fk, _fv in _cycle_funnel.items():
                            _metrics.set(f"funnel_{_fk}", value=_fv)

                # Update dashboard state
                dashboard_state.scan_count = scan_count
                dashboard_state.last_scan_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                dashboard_state.opportunities_found += len(all_opportunities)
                dashboard_state.last_opportunities = all_opportunities[:20]
                dashboard_state.open_positions = db.get_open_positions_count()
                dashboard_state.daily_pnl = db.get_daily_pnl()
                dashboard_state.ws_connections = (1 if _ws_feeds_active(feed_manager, ws_task) else 0)
                # Update Layer 2-5 dashboard counters
                dashboard_state.stale_detections = sum(
                    1 for o in all_opportunities if o.get("type") == "StalePriceOpp")
                dashboard_state.resolution_snipes = sum(
                    1 for o in all_opportunities if o.get("type") == "ResolutionSnipeOpp")
                dashboard_state.convergence_signals = sum(
                    1 for o in all_opportunities if o.get("type") == "ConvergenceOpp")
                if _market_maker:
                    mm_status = _market_maker.get_status()
                    dashboard_state.mm_active_markets = mm_status["active_markets"]
                    dashboard_state.mm_active_orders = mm_status["active_orders"]
                    dashboard_state.mm_total_exposure = mm_status["total_exposure"]
                elif _mm_pilot:
                    dashboard_state.mm_pilot = _mm_pilot
                    pilot_status = _mm_pilot.get_status()
                    dashboard_state.mm_active_markets = len(pilot_status.get("selected_markets", []))
                    dashboard_state.mm_active_orders = pilot_status.get("resting_orders", 0)
                    dashboard_state.mm_total_exposure = pilot_status.get("total_inventory_usd", 0.0)

                # Update reward tracker reference for dashboard metrics
                if CONFIG_REWARDS_ENABLED and _reward_tracker:
                    dashboard_state.reward_tracker = _reward_tracker

                # Update strategy metrics (MON-01: per-strategy P&L analytics)
                # Also update leaderboard (MON-02: strategy leaderboard endpoint)
                try:
                    data_dir = config.DATA_DIR if hasattr(config, 'DATA_DIR') else "."
                    db_path = f"{data_dir}/trades.db"
                    metrics = get_strategy_metrics(db_path=db_path, lookback_days=7)
                    dashboard_state.strategy_metrics = metrics
                    dashboard_state.update_strategy_metrics(metrics)
                    if metrics:
                        logger.info("Updated strategy metrics: %d strategies", len(metrics))
                except Exception as e:
                    logger.warning("Failed to update strategy metrics: %s", e)

                # Update metrics
                if _metrics:
                    _scan_duration = time.time() - _scan_start
                    _metrics.inc("scans_total")
                    _metrics.inc("opportunities_found", value=len(all_opportunities))
                    _metrics.observe("scan_duration_seconds", value=_scan_duration)
                    _metrics.set("scan_cycle_duration_seconds", value=_scan_duration)
                    _metrics.set("active_positions", value=dashboard_state.open_positions)
                    _metrics.set("daily_pnl", value=dashboard_state.daily_pnl)
                    if all_opportunities:
                        best_roi_str = all_opportunities[0].get("net_roi", "0%")
                        try:
                            best_roi = float(best_roi_str.replace("%", "")) / 100 if isinstance(best_roi_str, str) else float(best_roi_str)
                        except (ValueError, TypeError):
                            best_roi = 0
                        _metrics.set("best_opportunity_roi", value=best_roi)
                        for opp in all_opportunities:
                            _metrics.observe("opportunity_profit", value=opp.get("net_profit", 0))
                    _metrics.set("ws_connected", {"platform": "combined"},
                                 value=1 if _ws_feeds_active(feed_manager, ws_task) else 0)

                # Check for stale WS feeds (no data received for > 120s)
                stale_feeds = feed_manager.get_stale_feeds(max_silent_seconds=120.0)
                if stale_feeds:
                    logger.warning("Stale WS feeds detected (no data for >120s): %s",
                                   ", ".join(stale_feeds))
                    if _metrics:
                        for sf in stale_feeds:
                            _metrics.set("ws_connected", {"platform": sf}, value=0)

                # Evict stale price cache entries
                _cleanup_price_cache()

                # Check for settled positions
                check_settlements(
                    db, kalshi_client, poly_markets,
                    betfair_client=extra_clients.get("betfair"),
                    smarkets_client=extra_clients.get("smarkets"),
                    sxbet_client=extra_clients.get("sxbet"),
                    matchbook_client=extra_clients.get("matchbook"),
                    gemini_client=extra_clients.get("gemini"),
                    ibkr_client=extra_clients.get("ibkr"),
                )

                # Execute opportunities sequentially (balance must be rechecked between trades)
                if all_opportunities:
                    execution_opportunities = [
                        opp for opp in all_opportunities if _is_execution_eligible(opp)
                    ]
                    # Apply execution budget cap (selectivity control).
                    # Opportunities are already sorted by _execution_priority
                    # (weight * capital_efficiency_score), so slicing [:N]
                    # keeps the top N highest-priority candidates per cycle.
                    budget = getattr(config, "EXECUTION_BUDGET_PER_SCAN", 0)
                    exec_queue = (
                        execution_opportunities[:budget]
                        if budget > 0 else execution_opportunities
                    )
                    if budget > 0 and len(execution_opportunities) > budget:
                        logger.info(
                            "Execution budget: top %d of %d opportunities selected",
                            budget, len(execution_opportunities),
                        )
                    logger.info("--- Execution Pass ---")
                    executed = 0
                    for opp in exec_queue:
                        if shutdown_event.is_set():
                            break
                        try:
                            if executor.execute(opp):
                                executed += 1
                                # Immediate bankroll refresh after trade (per user decision)
                                try:
                                    balances = executor._fetch_balances("Cross")
                                    if balances and executor.position_sizer:
                                        total = sum(
                                            v for v in balances.values()
                                            if isinstance(v, (int, float))
                                        )
                                        if total > 0:
                                            executor.position_sizer.update_bankroll(total)
                                except Exception as exc:
                                    logger.debug("Post-trade bankroll refresh failed: %s", exc)
                        except Exception as e:
                            logger.error("Execution error: %s", e)
                    logger.info("Executed: %d/%d", executed, len(exec_queue))

                # Process any pending hedges from partial fills
                if hedger:
                    try:
                        hedger.process_pending_hedges()
                    except Exception as e:
                        logger.warning("Hedger processing failed: %s", e)

                # Record price snapshots for backtesting
                if snapshot_recorder and all_opportunities:
                    nonlocal _last_snapshot_time
                    now = time.time()
                    if now - _last_snapshot_time >= CONFIG_SNAPSHOT_INTERVAL:
                        try:
                            recorded = snapshot_recorder.record_snapshot(all_opportunities)
                            if recorded:
                                logger.debug("Recorded %d snapshots.", recorded)
                            _last_snapshot_time = now
                        except Exception as e:
                            logger.warning("Snapshot recording failed: %s", e)

                # Timer-based bankroll refresh (every 5 minutes)
                nonlocal _last_bankroll_refresh
                _now = time.time()
                if _now - _last_bankroll_refresh >= _bankroll_refresh_interval:
                    try:
                        balances = executor._fetch_balances("Cross")
                        if balances:
                            from dashboard import state as _ds
                            from datetime import datetime as _dt, timezone as _tz
                            _ds.platform_balances = dict(balances)
                            _ds.last_bankroll_refresh = _dt.now(_tz.utc).isoformat()
                            if executor.position_sizer:
                                total = sum(
                                    v for v in balances.values() if isinstance(v, (int, float))
                                )
                                if total > 0:
                                    executor.position_sizer.update_bankroll(total)
                                    logger.info(
                                        "Bankroll refreshed: $%.2f across %d platforms",
                                        total, len(balances),
                                    )
                                else:
                                    logger.warning(
                                        "Bankroll refresh returned $0 across %d platforms: %s",
                                        len(balances), balances,
                                    )
                        else:
                            logger.warning("Bankroll refresh: _fetch_balances returned no balances")
                        _last_bankroll_refresh = _now
                    except Exception as exc:
                        logger.warning("Bankroll refresh failed: %s", exc, exc_info=True)
                        _last_bankroll_refresh = _now  # Don't retry immediately on failure

                # Hourly fee rate reload (OPTIMIZE-01)
                nonlocal _last_fee_refresh
                if _now - _last_fee_refresh >= config.FEE_REFRESH_INTERVAL:
                    try:
                        fee_changes = config.reload_fee_rates()
                        if fee_changes:
                            logger.info("Fee rates updated: %s", fee_changes)
                    except Exception as exc:
                        logger.debug("Fee rate reload failed: %s", exc)
                    _last_fee_refresh = _now

                # Nightly backtest and threshold recommendations (OPTIMIZE-02)
                nonlocal _last_backtest_run
                if _now - _last_backtest_run >= config.BACKTEST_RUN_INTERVAL:
                    async def _run_nightly_backtest():
                        try:
                            loop = asyncio.get_event_loop()
                            from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                            _end_iso = _dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                            _start_iso = (_dt.now(_tz.utc) - _td(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

                            def _sync_run():
                                from backtest import BacktestEngine, write_recommendations
                                engine = BacktestEngine()
                                result = engine.run(start_time=_start_iso, end_time=_end_iso)
                                write_recommendations(result, config.DATA_DIR)
                                logger.info(
                                    "Nightly backtest complete: %d trades, %.1f%% win rate",
                                    result.total_trades, result.win_rate * 100,
                                )
                            await loop.run_in_executor(None, _sync_run)
                        except Exception:
                            logger.exception("Nightly backtest failed")
                    asyncio.ensure_future(_run_nightly_backtest())
                    _last_backtest_run = _now

                # Weekly rebalance digest (MONITOR-04)
                nonlocal _last_rebalance_digest
                if _now - _last_rebalance_digest >= config.REBALANCE_DIGEST_INTERVAL:
                    try:
                        from dashboard import state as _ds
                        balances = getattr(_ds, "platform_balances", {})
                        opp_flow = getattr(_ds, "platform_opp_flow", {})
                        total = sum(v for v in balances.values() if isinstance(v, (int, float)))
                        if total > 0 and notifier:
                            total_opps = sum(opp_flow.values()) or 1
                            lines = ["Weekly Rebalance Digest:"]
                            for plat in sorted(balances.keys()):
                                bal = balances.get(plat, 0)
                                opps = opp_flow.get(plat, 0)
                                cur_pct = bal / total * 100
                                rec_pct = opps / total_opps * 100
                                lines.append(
                                    "  %s: $%.0f (%.0f%%) -> rec %.0f%%" % (
                                        plat, bal, cur_pct, rec_pct)
                                )
                            if hasattr(notifier, "notify_text"):
                                notifier.notify_text("\n".join(lines))
                            logger.info("Weekly rebalance digest sent.")
                    except Exception as exc:
                        logger.debug("Rebalance digest failed: %s", exc)
                    _last_rebalance_digest = _now

                # PR E: Auto-correlation tracker refresh (default 24h)
                nonlocal _last_correlation_tracker_run
                if (
                    config.CORRELATION_AUTO_DETECT_ENABLED
                    and _now - _last_correlation_tracker_run
                        >= config.CORRELATION_TRACKER_INTERVAL
                ):
                    async def _run_correlation_tracker():
                        try:
                            loop = asyncio.get_event_loop()

                            def _sync_run():
                                from snapshot import SnapshotRecorder
                                from correlation_tracker import (
                                    run_correlation_tracker,
                                )
                                rec = SnapshotRecorder()
                                try:
                                    n = run_correlation_tracker(rec)
                                    logger.info(
                                        "correlation_tracker: cached %d "
                                        "auto-correlated pairs", n,
                                    )
                                finally:
                                    rec.close()
                            await loop.run_in_executor(None, _sync_run)
                        except Exception:
                            logger.exception("correlation_tracker run failed")
                    asyncio.ensure_future(_run_correlation_tracker())
                    _last_correlation_tracker_run = _now

                # MON-03: Per-strategy zero-opportunity period detection (30-minute windows)
                if _alert_manager:
                    try:
                        # Count opportunities per strategy
                        strategy_opp_counts: dict[str, int] = {}
                        for opp in all_opportunities:
                            strategy_type = opp.get("type", "unknown")
                            strategy_opp_counts[strategy_type] = strategy_opp_counts.get(strategy_type, 0) + 1

                        # Check per-strategy zero-opp periods (30-min idle detection)
                        _alert_manager.check_zero_opp_period_per_strategy(strategy_opp_counts)

                        # Record strategy opportunities for tracking
                        for strategy_type in strategy_opp_counts:
                            _alert_manager.record_strategy_opportunity(strategy_type)

                        logger.debug(
                            "Scan cycle: %d opportunities across %d strategies",
                            len(all_opportunities),
                            len(strategy_opp_counts),
                        )
                    except Exception as e:
                        logger.warning("Error in strategy opportunity detection: %s", str(e))

                # Zero-opportunity anomaly detection (MONITOR-03 - overall period)
                if _alert_manager:
                    try:
                        _alert_manager.check_zero_opp_period(len(all_opportunities))
                    except Exception:
                        pass

                # Rebuild opportunity index for WS-triggered execution
                opp_index.rebuild([
                    opp for opp in all_opportunities if _is_execution_eligible(opp)
                ])

                # Subscribe to WebSocket feeds for discovered markets.
                # We subscribe to opportunity tokens AND also to broader
                # market tokens from cross-platform matched pairs so we
                # can detect arbs that appear between scan cycles.
                poly_sub_ids, kalshi_sub_tickers = opp_index.get_subscription_tokens(ws_sub_limit)

                # Broaden subscriptions: include top cross-platform matched
                # Kalshi tickers and Polymarket tokens even if no arb exists
                # yet.  This lets the WS trigger fire when prices move into
                # profitable range between polling scans.
                if poly_markets:
                    import json as _json
                    for pm in poly_markets[:ws_sub_limit]:
                        raw = pm.get("clobTokenIds")
                        if not raw:
                            continue
                        try:
                            tids = _json.loads(raw) if isinstance(raw, str) else raw
                        except Exception:
                            continue
                        if isinstance(tids, list):
                            for tid in tids:
                                if tid and tid not in poly_sub_ids:
                                    poly_sub_ids.append(tid)
                        if len(poly_sub_ids) >= ws_sub_limit:
                            break

                # Extract individual market tickers from Kalshi data.
                # kalshi_data is (events, markets_by_event, event_titles).
                # WS needs market tickers (e.g. KXBTCD-...), not event tickers.
                if kalshi_data and len(kalshi_data) >= 2 and kalshi_data[1]:
                    for _evt_ticker, _markets in kalshi_data[1].items():
                        for km in _markets:
                            kt = km.get("ticker", "")
                            if kt and kt not in kalshi_sub_tickers:
                                kalshi_sub_tickers.append(kt)
                            if len(kalshi_sub_tickers) >= ws_sub_limit:
                                break
                        if len(kalshi_sub_tickers) >= ws_sub_limit:
                            break

                # Plan 10: pilot tickers ride the Kalshi WS subscription set
                # so the pilot's book freshness gate (G6) sees live ticks.
                if _mm_pilot:
                    for _pt in _mm_pilot.pilot_tickers():
                        if _pt and _pt not in kalshi_sub_tickers:
                            kalshi_sub_tickers.append(_pt)

                ws_task = _sync_ws_feeds(
                    feed_manager, ws_task, scan_count, poly_sub_ids, kalshi_sub_tickers, kalshi_client,
                )

                # Sentry Crons heartbeat: emitted as the LAST step of the try
                # block so a failure anywhere in the cycle reports "error",
                # never both. A missed check-in pages (loop hang / death).
                capture_scan_heartbeat("ok")

            except Exception as e:
                import traceback
                logger.error("Scan failed: %s\n%s", e, traceback.format_exc())
                capture_scan_heartbeat("error")
                if _metrics:
                    _metrics.inc("scans_total", {"status": "failed"})

            # Stage timing summary — sorted desc by elapsed so the bottleneck is first.
            # Read this in production logs to identify which stage is dominating
            # the scan cycle (target: total <2 min for arb-quality reaction time).
            try:
                _scan_total = time.time() - _scan_start
                if _stage_display_start is not None:
                    _stage_timings.setdefault("display_exec", time.time() - _stage_display_start)
                logger.info(
                    "Scan #%d stage timings — %s",
                    scan_count,
                    _format_stage_timings(_stage_timings, _scan_total),
                )
            except Exception:
                # Never let instrumentation break the scan loop
                logger.exception("Stage-timing summary failed (non-fatal)")

            # Wait for next scan interval or shutdown
            logger.info("Next scan in %ds (Ctrl+C to stop)...", rescan_interval)
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=rescan_interval)
            except asyncio.TimeoutError:
                pass

        # Cleanup
        # Plan 10: stop the MM pilot first — run_loop's exit path cancels
        # every resting pilot order before the process dies (SIGTERM rule,
        # spec section 7). Gate on the THREAD, not just _mm_pilot, so a
        # late startup failure that reset _mm_pilot still signals the
        # thread it left running.
        if _mm_pilot or _mm_pilot_thread is not None:
            logger.info("Stopping Kalshi MM pilot...")
            _mm_pilot_stop.set()
            if _mm_pilot_thread is not None:
                _mm_pilot_thread.join(timeout=15)
                if _mm_pilot_thread.is_alive() and _mm_pilot:
                    logger.warning("MM pilot thread did not stop in 15s; "
                                   "forcing order cancel directly.")
                    _mm_pilot.stop()
                    _mm_pilot_thread.join(timeout=15)
                    if _mm_pilot_thread.is_alive():
                        logger.critical(
                            "MM pilot thread remains alive after forced "
                            "stop; cancellation retries exhausted or venue "
                            "call still blocked.")

        logger.info("Stopping WebSocket feeds...")
        feed_manager.stop()
        if ws_task:
            ws_task.cancel()
            try:
                await ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if priority_consumer_task:
            priority_consumer_task.cancel()
            try:
                await priority_consumer_task
            except (asyncio.CancelledError, Exception):
                pass
        if stale_monitor_task:
            stale_monitor_task.cancel()
            try:
                await stale_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        if feed_health_task:
            feed_health_task.cancel()
            try:
                await feed_health_task
            except (asyncio.CancelledError, Exception):
                pass
        if health_monitor_task:
            health_monitor_task.cancel()
            try:
                await health_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Shutdown complete.")

    asyncio.run(_continuous_loop())
