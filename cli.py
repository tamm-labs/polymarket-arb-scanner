"""CLI entry point — argument parsing and initialization."""

from sentry_init import init_sentry
init_sentry()

import argparse
import io
import logging
import os
import sys
import time
from pathlib import Path

# Fix Windows console encoding for Unicode market names
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from dotenv import load_dotenv

# CLI --dry-run must win over DRY_RUN=false before any config import.
if "--dry-run" in sys.argv:
    os.environ["DRY_RUN"] = "true"

logger = logging.getLogger(__name__)

_TRUTHY = ("1", "true", "yes")


def require_kalshi_or_exit(kalshi_client) -> None:
    """Abort at boot when REQUIRE_KALSHI is set but Kalshi auth failed.

    Kalshi-only runs (D0 soaks, kalshi-only production) must not degrade to a
    Polymarket-only scan when Kalshi is unreachable — a 48h soak that silently
    drops its subject exchange produces void evidence (2026-07-23 incident).
    """
    if os.getenv("REQUIRE_KALSHI", "").strip().lower() not in _TRUTHY:
        return
    if kalshi_client is not None:
        return
    logger.error(
        "REQUIRE_KALSHI is set but Kalshi auth failed or credentials are missing — "
        "aborting instead of degrading (exchange may be in its maintenance window; "
        "check GET /trade-api/v2/exchange/status before relaunching)."
    )
    sys.exit(1)


from polymarket_api import (
    fetch_all_markets,
    fetch_events,
    fetch_reward_markets,
    PolymarketTrader,
)
from kalshi_api import build_client_from_env, kalshi_creds_configured
from betfair_api import BetfairClient
from smarkets_api import SmarketsClient
from sxbet_api import SXBetClient
from matchbook_api import MatchbookClient
from gemini_api import GeminiClient
from ibkr_api import IBKRClient
from metaculus_api import MetaculusClient
from gas_monitor import GasMonitor
from event_monitor import EventMonitor
from db import TradeDB
from risk_manager import RiskManager
from executor import ArbitrageExecutor
from notifier import WebhookNotifier
from dashboard import start_dashboard, state as dashboard_state
from display import display_results
from continuous import run_continuous, check_settlements
from scans import (
    scan_binary_internal,
    scan_negrisk_internal,
    scan_negrisk_no_side,
    scan_cross_platform,
    scan_cross_all,
    scan_kalshi_binary,
    scan_kalshi_multi,
    _fetch_kalshi_data,
    capital_efficiency_score,
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
    scan_stale_prices,
    scan_resolution_snipes,
    scan_convergence,
    scan_polymarket_rewards,
    scan_kalshi_rewards,
    scan_limitless_rewards,
    scan_frechet,
    _refine_frechet_with_clob,
    scan_temporal_arb,
    _refine_temporal_with_clob,
    scan_ctf,
    _refine_ctf_with_clob,
)
import config
from config import (
    polymarket_scan_enabled,
    polymarket_reward_fetch_enabled,
    DEFAULT_MIN_PROFIT,
    MAX_TRADE_SIZE as CONFIG_MAX_TRADE_SIZE,
    DAILY_LOSS_LIMIT as CONFIG_DAILY_LOSS_LIMIT,
    MAX_OPEN_POSITIONS as CONFIG_MAX_OPEN_POSITIONS,
    MAX_DAILY_TRADES as CONFIG_MAX_DAILY_TRADES,
    MIN_LIQUIDITY as CONFIG_MIN_LIQUIDITY,
    MIN_LIQUIDITY_HIGH_ROI as CONFIG_MIN_LIQUIDITY_HIGH_ROI,
    MIN_NET_ROI as CONFIG_MIN_NET_ROI,
    ALLOW_BETTER_REENTRY as CONFIG_ALLOW_BETTER_REENTRY,
    REENTRY_IMPROVEMENT_THRESHOLD as CONFIG_REENTRY_IMPROVEMENT_THRESHOLD,
    RESCAN_INTERVAL as CONFIG_RESCAN_INTERVAL,
    WEBHOOK_URL as CONFIG_WEBHOOK_URL,
    WEBHOOK_MIN_PROFIT as CONFIG_WEBHOOK_MIN_PROFIT,
    DASHBOARD_PORT as CONFIG_DASHBOARD_PORT,
    REVALIDATION_MIN_FLOOR as CONFIG_REVALIDATION_MIN_FLOOR,
    REVALIDATION_ADAPTIVE as CONFIG_REVALIDATION_ADAPTIVE,
    DYNAMIC_SIZING_ENABLED as CONFIG_DYNAMIC_SIZING,
    SIZING_AGGRESSIVENESS as CONFIG_SIZING_AGGRESSIVENESS,
    setup_logging,
    POLYGON_RPC_URL as CONFIG_POLYGON_RPC_URL,
    DYNAMIC_FEE_ENABLED as CONFIG_DYNAMIC_FEE,
    GAS_PRICE_CACHE_TTL as CONFIG_GAS_CACHE_TTL,
    EVENT_DIVERGENCE_THRESHOLD as CONFIG_EVENT_DIVERGENCE,
    EVENT_MONITOR_ENABLED as CONFIG_EVENT_MONITOR,
    METACULUS_COMMERCIAL_USE_APPROVED as CONFIG_METACULUS_COMMERCIAL_USE_APPROVED,
    CONCURRENT_EXECUTION as CONFIG_CONCURRENT_EXECUTION,
    REWARDS_ENABLED as CONFIG_REWARDS_ENABLED,
)

# Project-local .env only — never merge personal/global env files (e.g.
# ~/.claude/.env) into the bot environment.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")


def _initialize_ibkr_client(mode: str):
    """Initialize IBKR only when its gateway host is explicitly configured."""
    if mode not in ("all", "cross-all", "ibkr"):
        return None

    ibkr_host = os.getenv("IBKR_HOST")
    if not ibkr_host:
        log = logger.warning if mode == "ibkr" else logger.info
        log("IBKR disabled: set IBKR_HOST when an IB Gateway or TWS endpoint is available.")
        return None

    ibkr_port = int(os.getenv("IBKR_PORT", "4001"))
    ibkr_cid = int(os.getenv("IBKR_CLIENT_ID", "1"))
    client = IBKRClient()
    if not client.login(ibkr_host, ibkr_port, ibkr_cid):
        logger.warning(
            "IBKR connection failed (is IB Gateway running at %s:%d?).",
            ibkr_host,
            ibkr_port,
        )
        return None

    logger.info("IBKR connected successfully.")
    return client


def _initialize_metaculus_client():
    """Initialize Metaculus only after its access and commercial-use gates."""
    if not CONFIG_EVENT_MONITOR:
        return None

    api_key = os.getenv("METACULUS_API_KEY")
    if not api_key:
        logger.warning(
            "Metaculus disabled: EVENT_MONITOR_ENABLED requires METACULUS_API_KEY."
        )
        return None
    if not CONFIG_METACULUS_COMMERCIAL_USE_APPROVED:
        logger.warning(
            "Metaculus disabled: set METACULUS_COMMERCIAL_USE_APPROVED=true only after "
            "Metaculus grants written commercial API/data permission."
        )
        return None

    client = MetaculusClient()
    if not client.login(api_key=api_key):
        logger.warning("Metaculus authentication or API-access verification failed.")
        return None

    logger.info("Metaculus client initialized with approved API access.")
    return client


def _run_oneshot(args, min_profit, kalshi_client, executor, db, extra_clients=None,
                 notifier=None, event_monitor=None):
    """One-shot scan mode with parallel data fetching and optional execution."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    extra_clients = extra_clients or {}
    all_opportunities = []

    # Stage 1: Fetch data from all platforms in parallel
    poly_markets = None
    poly_events = None
    poly_reward_markets = None
    kalshi_data = None

    fetch_futures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if polymarket_scan_enabled(args.mode):
            fetch_futures["poly_markets"] = pool.submit(fetch_all_markets)
        if polymarket_scan_enabled(args.mode) and args.mode in ("all", "negrisk", "negrisk-no"):
            fetch_futures["poly_events"] = pool.submit(fetch_events)
        if polymarket_reward_fetch_enabled(args.mode) and CONFIG_REWARDS_ENABLED:
            fetch_futures["poly_reward_markets"] = pool.submit(fetch_reward_markets)
        if args.mode in ("all", "kalshi", "cross", "spread", "rewards", "temporal") and kalshi_client:
            fetch_futures["kalshi_data"] = pool.submit(_fetch_kalshi_data, kalshi_client)

        for key, future in fetch_futures.items():
            try:
                result = future.result()
                if key == "poly_markets":
                    poly_markets = result
                    if poly_markets:
                        logger.info("Fetched %d Polymarket markets.", len(poly_markets))
                    else:
                        logger.warning("Failed to fetch Polymarket markets.")
                elif key == "poly_events":
                    poly_events = result
                    if poly_events:
                        logger.info("Fetched %d events.", len(poly_events))
                elif key == "poly_reward_markets":
                    poly_reward_markets = result
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

    # Stage 2: Run scans in parallel (binary, negrisk, kalshi_binary, kalshi_multi)
    scan_futures = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        if args.mode in ("all", "binary") and poly_markets:
            scan_futures["binary"] = pool.submit(scan_binary_internal, poly_markets, min_profit)
        if args.mode in ("all", "negrisk") and poly_events:
            scan_futures["negrisk"] = pool.submit(scan_negrisk_internal, poly_events, min_profit)
        if args.mode in ("all", "negrisk-no") and poly_events:
            scan_futures["negrisk_no"] = pool.submit(scan_negrisk_no_side, poly_events, min_profit)
        if args.mode in ("all", "kalshi") and kalshi_client:
            scan_futures["kalshi_binary"] = pool.submit(
                scan_kalshi_binary, kalshi_client, min_profit, kalshi_data=kalshi_data)
            # KalshiMulti kill-switch: disable for thin multi-outcome markets
            # that cause Fill-or-Kill partial fills (no exit liquidity for hedge)
            if config.KALSHI_MULTI_ENABLED:
                scan_futures["kalshi_multi"] = pool.submit(
                    scan_kalshi_multi, kalshi_client, min_profit, kalshi_data=kalshi_data)

        for key, future in scan_futures.items():
            try:
                opps = future.result()
                all_opportunities.extend(opps)
                logger.info("Found %d %s opportunities.", len(opps), key)
            except Exception as e:
                logger.error("Scan %s failed: %s", key, e)

    # Stage 3: Cross-platform scans (need data from stages above)
    kalshi_events_preloaded = kalshi_data[0] if kalshi_data else None
    kalshi_markets_preloaded = kalshi_data[1] if kalshi_data else None

    if args.mode in ("all", "cross"):
        logger.info("--- Cross-Platform Scan (Polymarket vs Kalshi) ---")
        cross_opps = scan_cross_platform(
            poly_markets, kalshi_client, min_profit,
            min_confidence=args.min_confidence,
            kalshi_events_preloaded=kalshi_events_preloaded,
            kalshi_markets_by_event=kalshi_markets_preloaded,
        )
        all_opportunities.extend(cross_opps)
        logger.info("Found %d cross-platform opportunities.", len(cross_opps))

    # Scan cross-all (all platform pairs)
    if args.mode == "cross-all":
        logger.info("--- Cross-All Platform Scan ---")
        platform_clients = {}
        for name, client in extra_clients.items():
            if client:
                logger.info("Fetching %s markets...", name)
                if name == "betfair":
                    events = client.list_events()
                    markets = []
                    for ev in events[:50]:  # Limit for performance
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
                    logger.info("Fetched %d %s markets.", len(markets), name)
                    platform_clients[name] = (client, markets)

        cross_all_opps = scan_cross_all(
            poly_markets, platform_clients, min_profit,
            min_confidence=args.min_confidence,
        )
        all_opportunities.extend(cross_all_opps)
        logger.info("Found %d cross-all opportunities.", len(cross_all_opps))

    # Stage 4: Platform-specific scans
    if args.mode in ("all", "spread"):
        logger.info("--- Spread Scan ---")
        if poly_markets:
            spread_pm = scan_spread_polymarket(poly_markets, min_profit)
            all_opportunities.extend(spread_pm)
            logger.info("Found %d Polymarket spread opportunities.", len(spread_pm))

    if args.mode in ("all", "betfair"):
        betfair = extra_clients.get("betfair")
        if betfair:
            logger.info("--- Betfair Scan ---")
            bf_backall = scan_betfair_backall(betfair, min_profit)
            all_opportunities.extend(bf_backall)
            logger.info("Found %d Betfair back-all opportunities.", len(bf_backall))
            bf_backlay = scan_betfair_backlay(betfair, min_profit)
            all_opportunities.extend(bf_backlay)
            logger.info("Found %d Betfair back-lay opportunities.", len(bf_backlay))

    if args.mode in ("all", "smarkets"):
        smarkets = extra_clients.get("smarkets")
        if smarkets:
            logger.info("--- Smarkets Scan ---")
            sm_backall = scan_smarkets_backall(smarkets, min_profit)
            all_opportunities.extend(sm_backall)
            logger.info("Found %d Smarkets back-all opportunities.", len(sm_backall))
            sm_backlay = scan_smarkets_backlay(smarkets, min_profit)
            all_opportunities.extend(sm_backlay)
            logger.info("Found %d Smarkets back-lay opportunities.", len(sm_backlay))

    if args.mode in ("all", "sxbet"):
        sxbet = extra_clients.get("sxbet")
        if sxbet:
            logger.info("--- SX Bet Scan ---")
            sx_backall, sx_backlay = scan_sxbet(sxbet, min_profit)
            all_opportunities.extend(sx_backall)
            logger.info("Found %d SX Bet back-all opportunities.", len(sx_backall))
            all_opportunities.extend(sx_backlay)
            logger.info("Found %d SX Bet back-lay opportunities.", len(sx_backlay))

    if args.mode in ("all", "matchbook"):
        matchbook = extra_clients.get("matchbook")
        if matchbook:
            logger.info("--- Matchbook Scan ---")
            mb_backall = scan_matchbook_backall(matchbook, min_profit)
            all_opportunities.extend(mb_backall)
            logger.info("Found %d Matchbook back-all opportunities.", len(mb_backall))
            mb_backlay = scan_matchbook_backlay(matchbook, min_profit)
            all_opportunities.extend(mb_backlay)
            logger.info("Found %d Matchbook back-lay opportunities.", len(mb_backlay))

    if args.mode in ("all", "gemini"):
        gemini = extra_clients.get("gemini")
        if gemini:
            logger.info("--- Gemini Scan ---")
            gm_binary = scan_gemini_binary(gemini, min_profit)
            all_opportunities.extend(gm_binary)
            logger.info("Found %d Gemini binary opportunities.", len(gm_binary))
            gm_multi = scan_gemini_multi(gemini, min_profit)
            all_opportunities.extend(gm_multi)
            logger.info("Found %d Gemini multi-outcome opportunities.", len(gm_multi))

    if args.mode in ("all", "ibkr"):
        ibkr = extra_clients.get("ibkr")
        if ibkr:
            logger.info("--- IBKR Scan ---")
            ibkr_binary = scan_ibkr_binary(ibkr, min_profit)
            all_opportunities.extend(ibkr_binary)
            logger.info("Found %d IBKR binary opportunities.", len(ibkr_binary))

    if args.mode in ("all", "event") and event_monitor:
        logger.info("--- Event Divergence Scan ---")
        platform_markets_for_event = {}
        if poly_markets:
            platform_markets_for_event["polymarket"] = poly_markets
        if kalshi_data and kalshi_data[0]:
            platform_markets_for_event["kalshi"] = kalshi_data[0]
        if platform_markets_for_event:
            event_opps = event_monitor.scan_event_divergences(
                platform_markets_for_event, min_profit=min_profit)
            all_opportunities.extend(event_opps)
            logger.info("Found %d event divergence opportunities.", len(event_opps))

    if args.mode in ("all", "triangular", "nway"):
        logger.info("--- Triangular / N-way Cross-Platform Scan ---")
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
                    logger.warning("Triangular: failed to fetch %s markets: %s", name, e)
        if args.mode in ("all", "triangular"):
            tri_opps = scan_triangular(
                platform_markets_for_tri, platform_clients_for_tri, min_profit,
                min_confidence=args.min_confidence,
            )
            all_opportunities.extend(tri_opps)
            logger.info("Found %d triangular opportunities.", len(tri_opps))

        if args.mode in ("all", "nway"):
            nway_opps = scan_nway_arb(
                platform_markets_for_tri, platform_clients_for_tri, min_profit,
                min_confidence=args.min_confidence,
            )
            all_opportunities.extend(nway_opps)
            logger.info("Found %d N-way arb opportunities.", len(nway_opps))

    # Multi-outcome cross-platform scan
    if args.mode in ("all", "multi-cross") and poly_events and kalshi_client:
        logger.info("--- Multi-outcome cross-platform scan ---")
        mc_opps = scan_multi_cross(
            poly_events, kalshi_client, min_profit,
            kalshi_data=kalshi_data,
        )
        all_opportunities.extend(mc_opps)
        logger.info("Found %d multi-cross opportunities.", len(mc_opps))

    # Stale price scan (Layer 2)
    # Note: In one-shot mode, stale detection is limited since we only have
    # a single snapshot. This scan is most effective in continuous mode where
    # the PriceTracker accumulates data over time via WebSocket feeds.
    if args.mode == "stale":
        logger.info("--- Stale price scan ---")
        try:
            from price_tracker import PriceTracker
            from config import STALE_PRICE_THRESHOLD, STALE_PRICE_MOVE_PCT
            tracker = PriceTracker(
                stale_threshold_seconds=STALE_PRICE_THRESHOLD,
                move_threshold_pct=STALE_PRICE_MOVE_PCT,
            )
            # Seed tracker with current prices from all available platforms
            if poly_markets:
                for mkt in poly_markets:
                    cid = mkt.get("condition_id", "")
                    tokens = mkt.get("tokens", [])
                    for token in tokens:
                        if token.get("outcome", "").lower() == "yes":
                            price = token.get("price")
                            if price and cid:
                                tracker.update("polymarket", cid, float(price))
            if kalshi_data and kalshi_data[0]:
                for evt in kalshi_data[0]:
                    for mkt in evt.get("markets", [evt]):
                        ticker = mkt.get("ticker", "")
                        yes_price = mkt.get("yes_ask") or mkt.get("yes_price")
                        if ticker and yes_price:
                            price_val = float(yes_price)
                            if price_val > 1:
                                price_val /= 100.0
                            tracker.update("kalshi", ticker, price_val)
            # In one-shot mode, all prices are fresh so stale detection yields nothing.
            # Log this for the user's awareness.
            stale_opps = scan_stale_prices(
                tracker, [], min_move_pct=STALE_PRICE_MOVE_PCT,
                min_stale_seconds=STALE_PRICE_THRESHOLD, min_profit=min_profit,
            )
            all_opportunities.extend(stale_opps)
            logger.info("Found %d stale price opportunities (one-shot: use --continuous for real-time detection).", len(stale_opps))
        except Exception as exc:
            logger.warning("Stale price scan failed: %s", exc)

    # Resolution sniping scan (Layer 2)
    if args.mode in ("all", "resolution"):
        logger.info("--- Resolution sniping scan ---")
        try:
            # Scan Polymarket markets
            if poly_markets:
                res_opps_pm = scan_resolution_snipes(
                    poly_markets, platform="polymarket", min_profit=min_profit,
                )
                all_opportunities.extend(res_opps_pm)
                logger.info("Found %d Polymarket resolution snipe opportunities.", len(res_opps_pm))
            # Scan Kalshi markets
            if kalshi_data and kalshi_data[0]:
                kalshi_markets_flat = []
                for evt in kalshi_data[0]:
                    for mkt in evt.get("markets", [evt]):
                        kalshi_markets_flat.append(mkt)
                if kalshi_markets_flat:
                    res_opps_k = scan_resolution_snipes(
                        kalshi_markets_flat, platform="kalshi", min_profit=min_profit,
                    )
                    all_opportunities.extend(res_opps_k)
                    logger.info("Found %d Kalshi resolution snipe opportunities.", len(res_opps_k))
        except Exception as exc:
            logger.warning("Resolution sniping scan failed: %s", exc)

    # Convergence scan (Layer 4)
    if args.mode in ("all", "convergence"):
        logger.info("--- Cross-platform convergence scan ---")
        try:
            from config import CONVERGENCE_MIN_DIVERGENCE, CONVERGENCE_MIN_PLATFORMS
            from matcher import match_cross_platform
            # Build cross-platform matched market data with prices from all platforms
            matched_for_convergence = []
            platform_prices_map: dict[str, dict[str, dict]] = {}  # {market_key: {platform: {yes, no}}}

            # Collect Polymarket prices
            if poly_markets:
                for mkt in poly_markets:
                    cid = mkt.get("condition_id", "")
                    title = mkt.get("question") or mkt.get("title", "")
                    tokens = mkt.get("tokens", [])
                    yes_p = no_p = None
                    for t in tokens:
                        if t.get("outcome", "").lower() == "yes":
                            yes_p = t.get("price")
                        elif t.get("outcome", "").lower() == "no":
                            no_p = t.get("price")
                    if cid and yes_p:
                        platform_prices_map.setdefault(cid, {})["polymarket"] = {
                            "yes": float(yes_p), "no": float(no_p) if no_p else 1.0 - float(yes_p),
                        }
                        platform_prices_map[cid]["_title"] = title

            # Collect Kalshi prices (match by title to Polymarket condition IDs)
            if kalshi_data and kalshi_data[0] and poly_markets:
                kalshi_flat = []
                for evt in kalshi_data[0]:
                    for mkt in evt.get("markets", [evt]):
                        kalshi_flat.append(mkt)
                matches = match_cross_platform(
                    poly_markets, kalshi_flat, "polymarket", "kalshi",
                    threshold=72, min_confidence=args.min_confidence,
                )
                for m in matches:
                    pm_mkt = m.get("market_a", {})
                    k_mkt = m.get("market_b", {})
                    cid = pm_mkt.get("condition_id", "")
                    yes_ask = k_mkt.get("yes_ask") or k_mkt.get("yes_price")
                    if cid and yes_ask:
                        price_val = float(yes_ask)
                        if price_val > 1:
                            price_val /= 100.0
                        platform_prices_map.setdefault(cid, {})["kalshi"] = {
                            "yes": price_val, "no": 1.0 - price_val,
                        }

            # Build matched_markets list for convergence scan
            for market_key, data in platform_prices_map.items():
                title = data.pop("_title", market_key)
                platform_prices = {k: v for k, v in data.items() if isinstance(v, dict)}
                if len(platform_prices) >= 2:
                    matched_for_convergence.append({
                        "market_key": market_key,
                        "title": title,
                        "platform_prices": platform_prices,
                    })

            conv_opps = scan_convergence(
                matched_for_convergence, min_divergence=CONVERGENCE_MIN_DIVERGENCE,
                min_platforms=CONVERGENCE_MIN_PLATFORMS, min_profit=min_profit,
            )
            all_opportunities.extend(conv_opps)
            logger.info("Found %d convergence opportunities.", len(conv_opps))
        except Exception as exc:
            logger.warning("Convergence scan failed: %s", exc)

    # Market making (Layer 3) — generate pseudo-opportunities for display
    # Strategy #9: re-score cached cross near-misses against current fees.
    if args.mode == "fee-promo":
        logger.info("--- Fee promotional arbitrage scan ---")
        try:
            from scans.fee_promo import scan_fee_promo
            from near_miss_cache import get_global_cache
            from config import reload_fee_rates, MIN_PROFIT_AMOUNT
            changes = reload_fee_rates()
            if changes:
                logger.info("Fee rate deltas detected: %s", changes)
            promo_opps = scan_fee_promo(
                cache=get_global_cache(),
                min_profit=MIN_PROFIT_AMOUNT,
            )
            all_opportunities.extend(promo_opps)
            logger.info("Found %d fee-promo opportunities.", len(promo_opps))
        except Exception as exc:
            logger.warning("Fee-promo scan failed: %s", exc)

    # Strategy #11: paired bid/ask quotes across two platforms.
    if args.mode == "cross-mm":
        logger.info("--- Cross-platform market making scan ---")
        try:
            from scans.cross_mm import scan_cross_mm
            from config import (
                CROSS_MM_MIN_SPREAD, CROSS_MM_QUOTE_SIZE, CROSS_MM_PLATFORMS,
            )
            from matcher import match_cross_platform
            whitelist = tuple(p.strip() for p in CROSS_MM_PLATFORMS.split(",") if p.strip())
            # Pair Polymarket binaries with Kalshi events (same input the
            # cross-platform arb scan uses) and hand them to scan_cross_mm.
            if poly_markets and kalshi_client and "polymarket" in whitelist and "kalshi" in whitelist:
                kalshi_events = kalshi_client.fetch_all_events()
                pairs = match_cross_platform(
                    poly_markets, kalshi_events,
                    platform_a="polymarket", platform_b="kalshi",
                ) if kalshi_events else []
                cross_mm_opps = scan_cross_mm(
                    pairs,
                    min_spread=CROSS_MM_MIN_SPREAD,
                    quote_size=CROSS_MM_QUOTE_SIZE,
                    platforms_whitelist=whitelist,
                )
                all_opportunities.extend(cross_mm_opps)
                logger.info("Found %d cross-platform MM opportunities.",
                            len(cross_mm_opps))
            else:
                logger.info("Cross-MM requires both Polymarket + Kalshi credentials")
        except Exception as exc:
            logger.warning("Cross-MM scan failed: %s", exc)

    # VolatilityAdjustedMM observability scan.
    if args.mode == "vol-mm":
        logger.info("--- Volatility-Adjusted MM scan ---")
        try:
            from scans.volatility_adjusted_mm import scan_volatility_adjusted_mm
            market_keys = []
            if poly_markets:
                for mkt in poly_markets[:50]:
                    cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
                    if cid:
                        market_keys.append(cid)
            if kalshi_data and kalshi_data[0]:
                for evt in kalshi_data[0][:50]:
                    for mkt in evt.get("markets", [evt]):
                        t = mkt.get("ticker", "")
                        if t:
                            market_keys.append(t)
            vol_opps = scan_volatility_adjusted_mm(market_keys)
            all_opportunities.extend(vol_opps)
            logger.info("Found %d VolatilityAdjustedMM opportunities.", len(vol_opps))
        except Exception as exc:
            logger.warning("VolatilityAdjustedMM scan failed: %s", exc)

    # ToxicFlowPause observability scan.
    if args.mode == "toxic-flow":
        logger.info("--- Toxic Flow Pause scan ---")
        try:
            from scans.toxic_flow_pause import scan_toxic_flow_pause
            market_keys = []
            if poly_markets:
                for mkt in poly_markets[:50]:
                    cid = mkt.get("condition_id") or mkt.get("conditionId") or ""
                    if cid:
                        market_keys.append(cid)
            if kalshi_data and kalshi_data[0]:
                for evt in kalshi_data[0][:50]:
                    for mkt in evt.get("markets", [evt]):
                        t = mkt.get("ticker", "")
                        if t:
                            market_keys.append(t)
            tox_opps = scan_toxic_flow_pause(market_keys)
            all_opportunities.extend(tox_opps)
            logger.info("Found %d ToxicFlowPause opportunities.", len(tox_opps))
        except Exception as exc:
            logger.warning("ToxicFlowPause scan failed: %s", exc)

    # LeadLagMM scan — surfaces lagging platforms anchored to leader fair value.
    if args.mode == "lead-lag-mm":
        logger.info("--- Lead-Lag MM scan ---")
        try:
            from scans.lead_lag_mm import scan_lead_lag_mm
            from matcher import match_cross_platform
            pairs = []
            if poly_markets and kalshi_client:
                kalshi_events = kalshi_client.fetch_all_events()
                if kalshi_events:
                    pairs = match_cross_platform(
                        poly_markets, kalshi_events,
                        platform_a="polymarket", platform_b="kalshi",
                    )
            ll_opps = scan_lead_lag_mm(pairs)
            all_opportunities.extend(ll_opps)
            logger.info("Found %d LeadLagMM opportunities.", len(ll_opps))
        except Exception as exc:
            logger.warning("LeadLagMM scan failed: %s", exc)

    if args.mode == "mm":
        logger.info("--- Market making scan ---")
        try:
            from market_maker import MarketMaker
            from config import MM_MIN_SPREAD, MM_QUOTE_SIZE, MM_MAX_INVENTORY, MM_MAX_TOTAL_EXPOSURE
            mm = MarketMaker(
                min_spread=MM_MIN_SPREAD,
                quote_size=MM_QUOTE_SIZE,
                max_inventory=MM_MAX_INVENTORY,
                max_total_exposure=MM_MAX_TOTAL_EXPOSURE,
                dry_run=executor.dry_run,
            )
            # Register liquid markets for MM
            if poly_markets:
                for mkt in poly_markets[:20]:
                    title = mkt.get("question") or mkt.get("title", "")
                    tokens = mkt.get("tokens", [])
                    if tokens:
                        price = tokens[0].get("price")
                        if price and 0.1 < float(price) < 0.9:
                            cid = mkt.get("condition_id", title[:30])
                            mm.add_market(cid, "polymarket", float(price))
            mm_opps = mm.generate_opportunities()
            all_opportunities.extend(mm_opps)
            logger.info("Found %d market making opportunities.", len(mm_opps))
        except Exception as exc:
            logger.warning("Market making scan failed: %s", exc)

    # STRAT-01: Order Book Imbalance
    if args.mode in ("all", "imbalance"):
        from config import IMBALANCE_ENABLED, IMBALANCE_RATIO
        if IMBALANCE_ENABLED:
            logger.info("--- Order Book Imbalance Scan ---")
            try:
                from scans.imbalance import scan_imbalance
                markets_by_key = {}
                if poly_markets:
                    for mkt in poly_markets:
                        cid = mkt.get("condition_id", "")
                        if cid:
                            markets_by_key[cid] = mkt
                imbalance_opps = scan_imbalance(markets_by_key, min_ratio=IMBALANCE_RATIO, price_cache={})
                all_opportunities.extend(imbalance_opps)
                logger.info("Found %d imbalance opportunities.", len(imbalance_opps))
            except Exception as e:
                logger.error("Imbalance scan failed: %s", e)

    # STRAT-02: News-Driven Resolution Sniping (Finnhub and/or Firecrawl sources)
    if args.mode in ("all", "news-snipe"):
        from config import (
            NEWS_SNIPE_ENABLED,
            FINNHUB_API_KEY,
            FIRECRAWL_NEWS_ENABLED,
            FIRECRAWL_API_KEY,
        )
        finnhub_on = NEWS_SNIPE_ENABLED and FINNHUB_API_KEY
        firecrawl_on = FIRECRAWL_NEWS_ENABLED and FIRECRAWL_API_KEY
        if finnhub_on or firecrawl_on:
            logger.info("--- News-Driven Sniping Scan ---")
            try:
                from scans.news_snipe import scan_news_snipe
                markets_by_key = {}
                if poly_markets:
                    for mkt in poly_markets:
                        cid = mkt.get("condition_id", "")
                        if cid:
                            markets_by_key[cid] = mkt
                news_clients = []
                if finnhub_on:
                    from finnhub_api import FinnhubNewsClient
                    news_clients.append(FinnhubNewsClient(FINNHUB_API_KEY))
                if firecrawl_on:
                    from firecrawl_news_client import FirecrawlNewsClient
                    news_clients.append(FirecrawlNewsClient(FIRECRAWL_API_KEY))
                news_opps = []
                for client in news_clients:
                    try:
                        news_opps.extend(
                            scan_news_snipe(markets_by_key, client, cooldown_cache={})
                        )
                    except Exception as e:
                        logger.error(
                            "News snipe scan failed for %s: %s",
                            type(client).__name__,
                            e,
                        )
                all_opportunities.extend(news_opps)
                logger.info("Found %d news snipe opportunities.", len(news_opps))
            except Exception as e:
                logger.error("News snipe scan failed: %s", e)

    # STRAT-06: Correlated Market Pairs (manual seeds + PR E auto-detected)
    if args.mode in ("all", "correlated"):
        from config import (
            CORRELATED_ENABLED, CORRELATED_PAIRS,
            CORRELATION_DIVERGENCE_THRESHOLD,
            CORRELATION_AUTO_DETECT_ENABLED,
        )
        # Manual pairs OR auto-detection enabled — either is enough to scan.
        if CORRELATED_ENABLED and (
            CORRELATED_PAIRS != "[]" or CORRELATION_AUTO_DETECT_ENABLED
        ):
            logger.info("--- Correlated Market Pairs Scan ---")
            try:
                from scans.correlated import (
                    scan_correlated, _load_correlated_pairs,
                    _refine_correlated_with_depth,
                )
                pairs = _load_correlated_pairs(CORRELATED_PAIRS)
                auto_pairs: list[tuple[str, str]] = []
                if CORRELATION_AUTO_DETECT_ENABLED:
                    try:
                        from correlation_tracker import load_auto_correlated_pairs
                        from snapshot import SnapshotRecorder
                        rec = SnapshotRecorder()
                        auto_pairs = load_auto_correlated_pairs(rec)
                        rec.close()
                        logger.info(
                            "Loaded %d auto-correlated pairs from cache",
                            len(auto_pairs),
                        )
                    except Exception as e:
                        logger.warning("Auto-correlation cache load failed: %s", e)
                markets_by_key = {}
                if poly_markets:
                    for mkt in poly_markets:
                        cid = mkt.get("condition_id", "")
                        if cid:
                            markets_by_key[cid] = mkt
                corr_opps = scan_correlated(
                    markets_by_key, pairs,
                    min_spread=CORRELATION_DIVERGENCE_THRESHOLD,
                    auto_pairs=auto_pairs,
                )
                # Stage 2: live CLOB validation.
                corr_opps = _refine_correlated_with_depth(corr_opps)
                all_opportunities.extend(corr_opps)
                logger.info("Found %d correlated opportunities.", len(corr_opps))
            except Exception as e:
                logger.error("Correlated pairs scan failed: %s", e)

    # Plan 02: Fréchet-Bound Logical Arbitrage
    if args.mode in ("all", "frechet"):
        from config import FRECHET_ARB_ENABLED, FRECHET_MIN_VIOLATION
        is_dry_run = getattr(args, "dry_run", None)
        if is_dry_run is None:
            is_dry_run = getattr(executor, "dry_run", True)
        if args.mode == "frechet" and not FRECHET_ARB_ENABLED and not is_dry_run:
            logger.error(
                "Fréchet arbitrage mode requested but FRECHET_ARB_ENABLED=false in non-dry-run execution. "
                "Refusing to scan without explicit enablement."
            )
        elif FRECHET_ARB_ENABLED or (args.mode == "frechet" and is_dry_run):
            logger.info("--- Fréchet-Bound Logical Arbitrage Scan ---")
            try:
                frechet_opps = scan_frechet(
                    poly_markets or [],
                    min_profit=min_profit,
                    min_violation=FRECHET_MIN_VIOLATION,
                    platform="polymarket",
                )
                frechet_opps = _refine_frechet_with_clob(frechet_opps, min_profit=min_profit)
                all_opportunities.extend(frechet_opps)
                logger.info("Found %d Fréchet arbitrage opportunities.", len(frechet_opps))
            except Exception as e:
                logger.error("Fréchet arbitrage scan failed: %s", e)

    # Plan 03: Cross-Date / Nested Temporal Arbitrage
    if args.mode in ("all", "temporal"):
        from config import TEMPORAL_ARB_ENABLED, TEMPORAL_MIN_VIOLATION
        is_dry_run = getattr(args, "dry_run", None)
        if is_dry_run is None:
            is_dry_run = getattr(executor, "dry_run", True)
        if args.mode == "temporal" and not TEMPORAL_ARB_ENABLED and not is_dry_run:
            logger.error(
                "Temporal arbitrage mode requested but TEMPORAL_ARB_ENABLED=false in non-dry-run execution. "
                "Refusing to scan without explicit enablement."
            )
        elif TEMPORAL_ARB_ENABLED or (args.mode == "temporal" and is_dry_run):
            logger.info("--- Cross-Date / Nested Temporal Arbitrage Scan ---")
            try:
                kalshi_markets_flat = []
                if kalshi_data and kalshi_data[0]:
                    for evt in kalshi_data[0]:
                        for mkt in evt.get("markets", [evt]):
                            kalshi_markets_flat.append(mkt)
                if kalshi_markets_flat:
                    temporal_opps = scan_temporal_arb(
                        kalshi_markets_flat,
                        min_profit=min_profit,
                        min_violation=TEMPORAL_MIN_VIOLATION,
                    )
                    temporal_opps = _refine_temporal_with_clob(
                        temporal_opps,
                        min_profit=min_profit,
                        kalshi_client=kalshi_client,
                    )
                    all_opportunities.extend(temporal_opps)
                    logger.info("Found %d temporal arbitrage opportunities.", len(temporal_opps))
            except Exception as e:
                logger.error("Temporal arbitrage scan failed: %s", e)

    # Plan 04: CTF Primitives (Merge / Split Arbitrage)
    if args.mode in ("all", "ctf"):
        from config import CTF_ENABLED, CTF_MERGE_ENABLED, CTF_MINT_SELL_ENABLED
        is_dry_run = getattr(args, "dry_run", None)
        if is_dry_run is None:
            is_dry_run = getattr(executor, "dry_run", True)
        if args.mode == "ctf" and not (CTF_ENABLED or CTF_MERGE_ENABLED or CTF_MINT_SELL_ENABLED) and not is_dry_run:
            logger.error(
                "CTF mode requested but CTF_ENABLED=false in non-dry-run execution. "
                "Refusing to scan without explicit enablement."
            )
            sys.exit(1)
        elif (CTF_ENABLED or CTF_MERGE_ENABLED or CTF_MINT_SELL_ENABLED) or (args.mode == "ctf" and is_dry_run):
            logger.info("--- CTF Primitives Scan (Polymarket Merge / Split) ---")
            try:
                ctf_opps = scan_ctf(
                    poly_markets or [],
                    min_profit=min_profit,
                )
                all_opportunities.extend(ctf_opps)
                logger.info("Found %d CTF primitive opportunities.", len(ctf_opps))
            except Exception as e:
                logger.error("CTF primitives scan failed: %s", e)

    # STRAT-07: Time Decay Convergence
    if args.mode in ("all", "time-decay"):
        from config import TIME_DECAY_ENABLED
        if TIME_DECAY_ENABLED:
            logger.info("--- Time Decay Convergence Scan ---")
            try:
                from scans.time_decay import scan_time_decay
                from config import (
                    TIME_DECAY_MIN_HOURS_EXPIRY, TIME_DECAY_MIN_CONSENSUS,
                    TIME_DECAY_BUY_BELOW_PRICE
                )
                # Initialize signal aggregator if not already available
                signal_agg = extra_clients.get("signal_aggregator")
                if not signal_agg:
                    from signal_aggregator import SignalAggregator
                    signal_agg = SignalAggregator()
                markets_by_key = {}
                if poly_markets:
                    for mkt in poly_markets:
                        cid = mkt.get("condition_id", "")
                        if cid:
                            markets_by_key[cid] = mkt
                decay_opps = scan_time_decay(
                    markets_by_key, signal_agg,
                    min_hours_to_expiry=TIME_DECAY_MIN_HOURS_EXPIRY,
                    min_consensus=TIME_DECAY_MIN_CONSENSUS,
                    buy_below_price=TIME_DECAY_BUY_BELOW_PRICE,
                )
                all_opportunities.extend(decay_opps)
                logger.info("Found %d time decay opportunities.", len(decay_opps))
            except Exception as e:
                logger.error("Time decay scan failed: %s", e)

    # STRAT-08: Whale Copy Trading (one-shot path; continuous.py has its own
    # invocation that runs each scan cycle).
    if args.mode in ("all", "whale-copy"):
        from config import WHALE_COPY_ENABLED, WHALE_WALLETS, POLYGONSCAN_API_KEY
        if WHALE_COPY_ENABLED and WHALE_WALLETS:
            logger.info("--- Whale Copy Scan ---")
            try:
                from scans.whale_copy import scan_whale_copy
                from polygonscan_api import PolygonscanClient
                polygonscan = PolygonscanClient(api_key=POLYGONSCAN_API_KEY)
                whale_opps = scan_whale_copy(
                    whale_wallets=WHALE_WALLETS,
                    polygonscan_client=polygonscan,
                    last_block_cache=None,
                )
                all_opportunities.extend(whale_opps)
                logger.info("Found %d whale copy opportunities.", len(whale_opps))
            except Exception as e:
                logger.error("Whale copy scan failed: %s", e)
        else:
            logger.debug(
                "Whale copy: skipped (enabled=%s, wallets=%d)",
                WHALE_COPY_ENABLED, len(WHALE_WALLETS),
            )

    # Rewards scanning (Layer 3: liquidity rewards)
    limitless_rewards_active = getattr(config, "LIMITLESS_REWARDS_ENABLED", False) or args.mode == "limitless-rewards"
    if args.mode in ("all", "rewards", "limitless-rewards") and (CONFIG_REWARDS_ENABLED or limitless_rewards_active):
        logger.info("--- Rewards Scan ---")
        if args.mode in ("all", "rewards") and CONFIG_REWARDS_ENABLED:
            try:
                # Polymarket rewards scan
                if poly_reward_markets:
                    from market_maker import RewardTracker
                    reward_tracker = RewardTracker()
                    pm_reward_opps = scan_polymarket_rewards(
                        poly_reward_markets, reward_tracker, min_pool_usdc=10.0
                    )
                    all_opportunities.extend(pm_reward_opps)
                    logger.info("Found %d Polymarket reward opportunities.", len(pm_reward_opps))
            except Exception as e:
                logger.error("Polymarket rewards scan failed: %s", e)

            try:
                # Kalshi rewards scan
                if kalshi_client:
                    from market_maker import KalshiRewardTracker
                    kalshi_reward_tracker = KalshiRewardTracker()
                    k_reward_opps = scan_kalshi_rewards(
                        kalshi_client, kalshi_reward_tracker, min_pool_usdc=10.0,
                        kalshi_data=kalshi_data,
                    )
                    all_opportunities.extend(k_reward_opps)
                    logger.info("Found %d Kalshi reward opportunities.", len(k_reward_opps))
            except Exception as e:
                logger.error("Kalshi rewards scan failed: %s", e)

        if limitless_rewards_active:
            try:
                # Limitless rewards scan
                limitless_client = extra_clients.get("limitless") if extra_clients else None
                if not limitless_client:
                    from limitless_api import LimitlessClient
                    limitless_client = LimitlessClient()
                    api_key = getattr(config, "LIMITLESS_API_KEY", "")
                    pk = getattr(config, "LIMITLESS_PRIVATE_KEY", "")
                    if api_key:
                        limitless_client.login(api_key=api_key, private_key=pk)
                l_reward_opps = scan_limitless_rewards(limitless_client, min_pool_usdc=10.0)
                all_opportunities.extend(l_reward_opps)
                logger.info("Found %d Limitless reward opportunities.", len(l_reward_opps))
            except Exception as e:
                logger.error("Limitless rewards scan failed: %s", e)

    # Filter by minimum depth if specified
    if args.min_depth > 0:
        before = len(all_opportunities)
        all_opportunities = [
            opp for opp in all_opportunities
            if (opp.get("_clob_depth") or 0) >= args.min_depth
        ]
        filtered = before - len(all_opportunities)
        if filtered:
            logger.info("Filtered out %d opportunities below min depth %.0f", filtered, args.min_depth)

    # Sort by capital efficiency (ROI * depth) descending
    all_opportunities.sort(key=capital_efficiency_score, reverse=True)

    if args.limit:
        all_opportunities = all_opportunities[:args.limit]

    # Display results
    display_results(all_opportunities, args.json)

    # Send webhook notification
    if notifier and all_opportunities:
        notifier.notify(all_opportunities)

    # Update dashboard state
    dashboard_state.opportunities_found += len(all_opportunities)
    dashboard_state.last_opportunities = all_opportunities[:20]
    dashboard_state.open_positions = db.get_open_positions_count()
    dashboard_state.daily_pnl = db.get_daily_pnl()

    # Execute opportunities if not display-only
    execution_opportunities = [
        opp for opp in all_opportunities if opp.get("_execution_eligible", True)
    ]
    if execution_opportunities and (executor.dry_run or executor.exec_mode in ("semi-auto", "full-auto")):
        logger.info("--- Execution Pass ---")
        executed = 0
        for opp in execution_opportunities:
            if executor.execute(opp):
                executed += 1
        logger.info("Executed: %d/%d", executed, len(execution_opportunities))


def _run_report(json_output: bool = False):
    """Print a P&L report from the trade database and exit.

    Displays cumulative P&L, daily P&L, open positions, per-strategy stats,
    and recent trade history.

    Args:
        json_output: If True, print raw JSON instead of formatted tables.
    """
    import json as json_mod

    db = TradeDB()
    try:
        cumulative = db.get_cumulative_pnl()
        daily = db.get_daily_pnl()
        daily_history = db.get_daily_pnl_history(days=30)
        open_count = db.get_open_positions_count()
        positions = db.get_open_positions()
        strategy_stats = db.get_opportunity_stats_by_type()
        recent_trades = db.get_recent_trades(limit=20)
        avg_slippage = db.get_avg_slippage()
        db_stats = db.get_db_stats()
    finally:
        db.close()

    if json_output:
        report = {
            "cumulative_pnl": cumulative,
            "daily_pnl": daily,
            "open_positions_count": open_count,
            "open_positions": positions,
            "daily_history": daily_history,
            "strategy_stats": strategy_stats,
            "recent_trades": recent_trades,
            "avg_slippage": avg_slippage,
            "db_stats": db_stats,
        }
        print(json_mod.dumps(report, indent=2, default=str))
        return

    print("=" * 72)
    print("  POLYMARKET ARBITRAGE SCANNER — P&L REPORT")
    print("=" * 72)
    print()

    # Summary
    print("  Cumulative P&L (settled):  ${:.4f}".format(cumulative))
    print("  Today's P&L (est):        ${:.4f}".format(daily))
    print("  Open positions:            {}".format(open_count))
    print("  Avg slippage:              ${:.6f}".format(avg_slippage))
    print()

    # Database stats
    if db_stats:
        print("  Database:")
        for table, count in db_stats.items():
            print("    {:20s} {:>6d} rows".format(table, count))
        print()

    # Strategy breakdown
    if strategy_stats:
        print("-" * 72)
        print("  STRATEGY BREAKDOWN")
        print("-" * 72)
        fmt = "  {:20s}  {:>6s}  {:>10s}  {:>10s}  {:>10s}"
        print(fmt.format("Type", "Count", "Avg ROI", "Total $", "Avg $"))
        print(fmt.format("-" * 20, "-" * 6, "-" * 10, "-" * 10, "-" * 10))
        for s in strategy_stats:
            print(fmt.format(
                str(s.get("type", "?"))[:20],
                str(s.get("count", 0)),
                "{:.2f}%".format(s.get("avg_roi", 0) * 100),
                "${:.4f}".format(s.get("total_profit", 0)),
                "${:.4f}".format(s.get("avg_profit", 0)),
            ))
        print()

    # Daily history (last 30 days)
    if daily_history:
        print("-" * 72)
        print("  DAILY P&L (last 30 days)")
        print("-" * 72)
        for day in daily_history:
            pnl = day.get("pnl", 0)
            marker = "+" if pnl >= 0 else ""
            print("  {}  {}${:.4f}".format(day.get("date", "?"), marker, pnl))
        print()

    # Open positions
    if positions:
        print("-" * 72)
        print("  OPEN POSITIONS ({})".format(open_count))
        print("-" * 72)
        for p in positions[:20]:
            print("  {} | {} | expected ${:.4f}".format(
                str(p.get("market", "?"))[:40],
                p.get("platform", "?"),
                p.get("expected_pnl", 0),
            ))
        if open_count > 20:
            print("  ... and {} more".format(open_count - 20))
        print()

    # Recent trades
    if recent_trades:
        print("-" * 72)
        print("  RECENT TRADES (last 20)")
        print("-" * 72)
        for t in recent_trades:
            slip = t.get("slippage")
            slip_str = " slip=${:.6f}".format(slip) if slip else ""
            print("  {} | {} {} @ ${:.4f} | {}{}".format(
                str(t.get("timestamp", "?"))[:19],
                t.get("platform", "?"),
                t.get("side", "?"),
                t.get("price", 0),
                t.get("status", "?"),
                slip_str,
            ))
        print()

    print("=" * 72)


def _run_analyze(json_output: bool = False):
    """Print historical performance analysis from the trade database.

    Computes win rate, Sharpe ratio, max drawdown, average hold time,
    and per-strategy performance breakdown.

    Args:
        json_output: If True, print raw JSON instead of formatted report.
    """
    import json as json_mod

    db = TradeDB()
    try:
        stats = db.get_performance_stats()
        daily_history = db.get_daily_pnl_history(days=90)
    finally:
        db.close()

    if json_output:
        report = {"performance": stats, "daily_history_90d": daily_history}
        print(json_mod.dumps(report, indent=2, default=str))
        return

    print("=" * 72)
    print("  POLYMARKET ARBITRAGE SCANNER — PERFORMANCE ANALYSIS")
    print("=" * 72)
    print()

    total = stats.get("total_settled", 0)
    if total == 0:
        print("  No settled positions found. Run the scanner first.")
        print("=" * 72)
        return

    print("  Total settled positions:  {}".format(total))
    print("  Win rate:                 {:.1f}%".format(stats.get("win_rate", 0) * 100))
    print("  Total P&L:               ${:.4f}".format(stats.get("total_pnl", 0)))
    print("  Average P&L per trade:   ${:.4f}".format(stats.get("avg_pnl", 0)))
    print("  Max win:                 ${:.4f}".format(stats.get("max_win", 0)))
    print("  Max loss:                ${:.4f}".format(stats.get("max_loss", 0)))
    print("  Sharpe ratio:            {:.4f}".format(stats.get("sharpe_ratio", 0)))

    avg_hold = stats.get("avg_hold_seconds", 0)
    if avg_hold > 86400:
        print("  Avg hold time:           {:.1f} days".format(avg_hold / 86400))
    elif avg_hold > 3600:
        print("  Avg hold time:           {:.1f} hours".format(avg_hold / 3600))
    elif avg_hold > 0:
        print("  Avg hold time:           {:.0f} seconds".format(avg_hold))
    print()

    # Strategy breakdown
    breakdown = stats.get("strategy_breakdown", [])
    if breakdown:
        print("-" * 72)
        print("  STRATEGY PERFORMANCE")
        print("-" * 72)
        fmt = "  {:22s}  {:>5s}  {:>8s}  {:>10s}  {:>10s}"
        print(fmt.format("Strategy", "Count", "Win %", "Total $", "Avg $"))
        print(fmt.format("-" * 22, "-" * 5, "-" * 8, "-" * 10, "-" * 10))
        for s in sorted(breakdown, key=lambda x: x.get("total_pnl", 0), reverse=True):
            print(fmt.format(
                str(s.get("type", "?"))[:22],
                str(s.get("count", 0)),
                "{:.1f}%".format(s.get("win_rate", 0) * 100),
                "${:.4f}".format(s.get("total_pnl", 0)),
                "${:.4f}".format(s.get("avg_pnl", 0)),
            ))
        print()

    # Daily P&L chart (last 90 days, text-based)
    if daily_history:
        print("-" * 72)
        print("  DAILY P&L (last 90 days)")
        print("-" * 72)
        max_pnl = max(abs(d.get("pnl", 0)) for d in daily_history) or 1
        bar_width = 40
        for day in daily_history[-30:]:  # Show last 30 for readability
            pnl = day.get("pnl", 0)
            bar_len = int(abs(pnl) / max_pnl * bar_width) if max_pnl > 0 else 0
            if pnl >= 0:
                bar = " " * bar_width + "|" + "#" * bar_len
            else:
                bar = " " * (bar_width - bar_len) + "#" * bar_len + "|"
            marker = "+" if pnl >= 0 else ""
            print("  {} {:>+9.4f} {}".format(day.get("date", "?"), pnl, bar.rstrip()))
        if len(daily_history) > 30:
            print("  ... ({} more days)".format(len(daily_history) - 30))
        print()

    print("=" * 72)


def main():
    """CLI entry point: parse arguments, initialise clients, and run scans.

    Parses command-line flags (mode, thresholds, execution settings, logging),
    resolves config precedence (CLI > env > defaults), initialises platform
    API clients (Polymarket, Kalshi, Betfair, Smarkets, SX Bet, Matchbook,
    Gemini, IBKR, Metaculus), sets up the trade executor, risk manager, and
    database, then dispatches to either one-shot scanning or continuous mode
    with WebSocket price feeds.

    Exits with code 0 on clean shutdown, 1 on fatal initialisation errors.
    """
    parser = argparse.ArgumentParser(description="Polymarket Arbitrage Scanner")
    parser.add_argument(
        "--mode",
        choices=["all", "binary", "negrisk", "negrisk-no", "cross", "kalshi", "cross-all",
                 "spread", "betfair", "smarkets", "sxbet", "matchbook",
                 "gemini", "ibkr", "event", "triangular", "nway",
                 "multi-cross",
                 "stale", "resolution", "convergence", "mm", "rewards", "limitless-rewards",
                 "imbalance", "news-snipe", "correlated", "time-decay",
                 "logical-arb", "whale-copy",
                 "fee-promo", "cross-mm",
                 "lead-lag-mm", "toxic-flow", "vol-mm", "mm-pilot", "frechet", "temporal", "ctf",
                 "research"],
        default="all",
        help="Scan mode: all, binary, negrisk, negrisk-no, cross, kalshi, cross-all, spread, betfair, smarkets, sxbet, matchbook, gemini, ibkr, event, triangular, stale, resolution, convergence, mm, mm-pilot, rewards, limitless-rewards, imbalance, news-snipe, correlated, time-decay, fee-promo, cross-mm, frechet, temporal, ctf, research (dry-run Kalshi + Fréchet + temporal + CTF)",
    )
    parser.add_argument(
        "--min-profit",
        type=float,
        default=None,
        help="Minimum net profit threshold (0-1, e.g., 0.01 = 1%%)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max number of results to display",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output results as JSON",
    )
    parser.add_argument(
        "--min-confidence",
        choices=["HIGH", "MEDIUM", "LOW"],
        default="LOW",
        help="Minimum cross-platform match confidence (default: LOW)",
    )
    parser.add_argument(
        "--min-depth",
        type=float,
        default=0,
        help="Minimum order book depth to display (default: 0 = no filter)",
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run persistently with WebSocket feeds and periodic re-scans",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=None,
        help="Seconds between re-scans in continuous mode (default: from RESCAN_INTERVAL env/config)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=None,
        help="Detect and log opportunities without executing trades",
    )
    parser.add_argument(
        "--exec-mode",
        choices=["semi-auto", "full-auto"],
        default=None,
        help="Execution mode (default: from .env or semi-auto)",
    )
    parser.add_argument(
        "--max-trade",
        type=float,
        default=None,
        help="Maximum dollar amount per trade (default: from .env or 5.00)",
    )
    parser.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Webhook URL for opportunity notifications (Slack/Discord/generic)",
    )
    parser.add_argument(
        "--dashboard-port",
        type=int,
        default=None,
        help="Port for HTTP status dashboard (0 = disabled)",
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default=None,
        help="Logging level (default: from .env or INFO)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Log file path (default: from .env or none)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="Print P&L report and exit (no scanning or trading)",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print historical performance analysis and exit",
    )
    parser.add_argument(
        "--auto-rebalance",
        action="store_true",
        help="Enable automated execution of inventory rebalancing proposals",
    )
    args = parser.parse_args()


    # Configure logging first (before any logger calls)
    setup_logging(level=args.log_level, log_file=args.log_file)

    # Report mode: print P&L and exit (no platform auth needed)
    if args.report:
        _run_report(json_output=args.json)
        return

    # Analysis mode: print historical performance and exit
    if args.analyze:
        _run_analyze(json_output=args.json)
        return

    min_profit = args.min_profit or float(os.getenv("MIN_PROFIT_THRESHOLD", DEFAULT_MIN_PROFIT))

    # Resolve execution settings from CLI > .env > defaults
    dry_run = args.dry_run if args.dry_run is not None else os.getenv("DRY_RUN", "true").lower() == "true"
    exec_mode = args.exec_mode or os.getenv("EXECUTION_MODE", "semi-auto")
    max_trade = args.max_trade or float(os.getenv("MAX_TRADE_SIZE", "5.0"))

    if args.auto_rebalance:
        config.INVENTORY_AUTO_REBALANCE_ENABLED = True


    if args.mode == config.RESEARCH_MODE and not (dry_run and config.DRY_RUN and args.continuous):
        logger.error("--mode research is observational: it requires --continuous and DRY_RUN=true.")
        sys.exit(2)

    logger.info("=" * 80)
    logger.info("POLYMARKET ARBITRAGE SCANNER v2")
    logger.info("Min profit threshold: %.2f%%", min_profit * 100)
    if args.continuous:
        logger.info("Mode: CONTINUOUS | Exec: %s | Dry-run: %s | Max trade: $%.2f", exec_mode, dry_run, max_trade)
    logger.info("=" * 80)

    # Initialize execution components
    db = TradeDB()
    risk_config = {
        "max_trade_size": max_trade,
        "daily_loss_limit": CONFIG_DAILY_LOSS_LIMIT,
        "max_open_positions": CONFIG_MAX_OPEN_POSITIONS,
        "max_daily_trades": CONFIG_MAX_DAILY_TRADES,
        "min_liquidity": CONFIG_MIN_LIQUIDITY,
        "min_liquidity_high_roi": CONFIG_MIN_LIQUIDITY_HIGH_ROI,
        "min_net_roi": CONFIG_MIN_NET_ROI,
        "allow_better_reentry": CONFIG_ALLOW_BETTER_REENTRY,
        "reentry_improvement_threshold": CONFIG_REENTRY_IMPROVEMENT_THRESHOLD,
        "dispute_gate_enabled": config.DISPUTE_GATE_ENABLED,
    }
    from inventory_balancer import InventoryBalancer
    inventory_balancer = InventoryBalancer()
    risk_manager = RiskManager(risk_config, inventory_balancer=inventory_balancer)

    # Initialize platform clients
    kalshi_client = None
    kalshi_api_key_id = os.getenv("KALSHI_API_KEY_ID")
    kalshi_private_key_path = os.getenv("KALSHI_PRIVATE_KEY_PATH")
    kalshi_private_key_b64 = os.getenv("KALSHI_PRIVATE_KEY_BASE64")
    if kalshi_private_key_path:
        kalshi_private_key_path = os.path.expanduser(kalshi_private_key_path)
    if kalshi_creds_configured():
        logger.info("Authenticating with Kalshi (API key)...")
        kalshi_client = build_client_from_env(
            attempts=config.KALSHI_AUTH_BOOT_ATTEMPTS,
            retry_wait=config.KALSHI_AUTH_BOOT_RETRY_WAIT,
        )
        if kalshi_client is None:
            logger.warning(
                "Kalshi auth failed after %d attempts — continuing without "
                "Kalshi; continuous mode will keep retrying every %.0fs.",
                config.KALSHI_AUTH_BOOT_ATTEMPTS, config.KALSHI_REAUTH_INTERVAL,
            )
        else:
            logger.info("Kalshi authenticated successfully.")
    else:
        logger.info("KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH (or KALSHI_PRIVATE_KEY_BASE64) not set — Kalshi disabled")

    # Kalshi-only runs (D0 soaks, kalshi-only production) must not degrade to a
    # Polymarket-only scan when Kalshi is unreachable — a 48h soak that silently
    # drops its subject exchange produces void evidence (2026-07-23 incident).
    require_kalshi_or_exit(kalshi_client)

    pm_trader = None
    pm_private_key = os.getenv("POLYMARKET_PRIVATE_KEY")
    if pm_private_key and not dry_run:
        pm_chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        pm_funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        pm_sig_type = int(os.getenv("POLYMARKET_SIGNATURE_TYPE", "0"))
        try:
            pm_trader = PolymarketTrader(
                pm_private_key, pm_chain_id,
                funder=pm_funder, signature_type=pm_sig_type,
            )
            logger.info(
                "Polymarket trader initialized (sig_type=%d, funder=%s).",
                pm_sig_type, pm_funder or "none",
            )
        except Exception as e:
            logger.warning("Polymarket trader init failed: %s", e)

    # Initialize additional platform clients
    betfair_client = None
    smarkets_client = None
    sxbet_client = None

    if args.mode in ("all", "cross-all", "betfair"):
        bf_api_key = os.getenv("BETFAIR_APP_KEY") or os.getenv("BETFAIR_API_KEY")
        bf_user = os.getenv("BETFAIR_USERNAME")
        if bf_api_key and bf_user:
            betfair_client = BetfairClient()
            if not betfair_client.login():
                betfair_client = None
                logger.warning("Betfair auth failed.")
            else:
                logger.info("Betfair authenticated successfully.")

    if args.mode in ("all", "cross-all", "smarkets"):
        sm_api_key = os.getenv("SMARKETS_API_KEY")
        if sm_api_key:
            smarkets_client = SmarketsClient()
            if not smarkets_client.login():
                smarkets_client = None
                logger.warning("Smarkets auth failed.")
            else:
                logger.info("Smarkets authenticated successfully.")

    if args.mode in ("all", "cross-all", "sxbet"):
        sx_api_key = os.getenv("SXBET_API_KEY")
        if sx_api_key:
            sxbet_client = SXBetClient()
            if not sxbet_client.login():
                sxbet_client = None
                logger.warning("SX Bet auth failed.")
            else:
                logger.info("SX Bet authenticated successfully.")

    matchbook_client = None
    if args.mode in ("all", "cross-all", "matchbook"):
        mb_user = os.getenv("MATCHBOOK_USERNAME")
        mb_pass = os.getenv("MATCHBOOK_PASSWORD")
        if mb_user and mb_pass:
            matchbook_client = MatchbookClient()
            if not matchbook_client.login():
                matchbook_client = None
                logger.warning("Matchbook auth failed.")
            else:
                logger.info("Matchbook authenticated successfully.")

    gemini_client = None
    if args.mode in ("all", "cross-all", "gemini"):
        gm_key = os.getenv("GEMINI_API_KEY")
        gm_secret = os.getenv("GEMINI_API_SECRET")
        if gm_key and gm_secret:
            gemini_client = GeminiClient()
            if not gemini_client.login(gm_key, gm_secret):
                gemini_client = None
                logger.warning("Gemini auth failed.")
            else:
                logger.info("Gemini authenticated successfully.")

    ibkr_client = _initialize_ibkr_client(args.mode)

    # Metaculus is read-only, but authentication and written commercial API
    # permission are both required by its current terms.
    metaculus_client = _initialize_metaculus_client()

    # Initialize GasMonitor for dynamic fee thresholds
    gas_monitor = None
    if CONFIG_DYNAMIC_FEE:
        gas_monitor = GasMonitor(
            polygon_rpc_url=CONFIG_POLYGON_RPC_URL,
            cache_ttl=CONFIG_GAS_CACHE_TTL,
        )
        logger.info("Dynamic fee arbitrage enabled (GasMonitor active).")

    # Initialize SignalAggregator for multi-source probability consensus
    sig_aggregator = None
    try:
        from signal_aggregator import SignalAggregator
        from manifold_api import ManifoldClient
        from config import SIGNAL_CACHE_TTL
        manifold_client = ManifoldClient()
        sig_aggregator = SignalAggregator(
            cache_ttl=SIGNAL_CACHE_TTL,
            metaculus_client=metaculus_client,
            manifold_client=manifold_client,
        )
        sources = "Metaculus + Manifold" if metaculus_client else "Manifold"
        logger.info("Signal aggregator enabled (%s).", sources)
    except Exception as exc:
        logger.debug("Signal aggregator not available: %s", exc)

    # Initialize EventMonitor for divergence signals (with multi-source when available)
    event_monitor = None
    if CONFIG_EVENT_MONITOR and metaculus_client:
        event_monitor = EventMonitor(
            metaculus_client=metaculus_client,
            divergence_threshold=CONFIG_EVENT_DIVERGENCE,
            signal_aggregator=sig_aggregator,
        )
        logger.info("Event-driven speed trading enabled (EventMonitor active).")
    elif CONFIG_EVENT_MONITOR:
        logger.warning("EventMonitor disabled because approved Metaculus access is unavailable.")

    # Price cache updated by WebSocket feeds (shared with executor for revalidation)
    price_cache = {}

    # Kelly criterion position sizer (optional — falls back to static/dynamic sizing)
    pos_sizer = None
    try:
        from position_sizer import PositionSizer
        from config import KELLY_FRACTION, KELLY_MAX_FRACTION
        pos_sizer = PositionSizer(
            bankroll=max_trade * 100,  # Approximate bankroll from max trade * 100
            kelly_fraction=KELLY_FRACTION,
            max_fraction=KELLY_MAX_FRACTION,
        )
        logger.info("Kelly position sizer enabled (fraction=%.2f, max=%.2f).",
                     KELLY_FRACTION, KELLY_MAX_FRACTION)
    except Exception as exc:
        logger.debug("Position sizer not available: %s", exc)

    ctf_client = None
    try:
        from ctf_api import CTFClient
        ctf_client = CTFClient(dry_run=dry_run)
    except Exception as exc:
        logger.debug("CTFClient not initialized: %s", exc)

    limitless_client = None
    try:
        from config import (
            LIMITLESS_API_KEY as CONFIG_LIMITLESS_API_KEY,
            LIMITLESS_PRIVATE_KEY as CONFIG_LIMITLESS_PRIVATE_KEY,
            LIMITLESS_REWARDS_ENABLED as CONFIG_LIMITLESS_REWARDS_ENABLED,
        )
        if CONFIG_LIMITLESS_REWARDS_ENABLED or args.mode in ("all", "rewards", "limitless-rewards"):
            from limitless_api import LimitlessClient
            limitless_client = LimitlessClient()
            limitless_client.dry_run = dry_run
            if CONFIG_LIMITLESS_API_KEY:
                limitless_client.login(api_key=CONFIG_LIMITLESS_API_KEY, private_key=CONFIG_LIMITLESS_PRIVATE_KEY)
    except Exception as exc:
        logger.debug("LimitlessClient not initialized: %s", exc)

    executor = ArbitrageExecutor(
        pm_trader=pm_trader,
        kalshi_client=kalshi_client,
        db=db,
        risk_manager=risk_manager,
        dry_run=dry_run,
        exec_mode=exec_mode,
        max_trade_size=max_trade,
        price_cache=price_cache,
        betfair_client=betfair_client,
        smarkets_client=smarkets_client,
        sxbet_client=sxbet_client,
        matchbook_client=matchbook_client,
        gemini_client=gemini_client,
        ibkr_client=ibkr_client,
        gas_monitor=gas_monitor,
        revalidation_adaptive=CONFIG_REVALIDATION_ADAPTIVE,
        revalidation_min_floor=CONFIG_REVALIDATION_MIN_FLOOR,
        dynamic_sizing=CONFIG_DYNAMIC_SIZING,
        sizing_aggressiveness=CONFIG_SIZING_AGGRESSIVENESS,
        concurrent_execution=CONFIG_CONCURRENT_EXECUTION,
        position_sizer=pos_sizer,
        ctf_client=ctf_client,
    )

    extra_clients = {
        "betfair": betfair_client,
        "smarkets": smarkets_client,
        "sxbet": sxbet_client,
        "matchbook": matchbook_client,
        "gemini": gemini_client,
        "ibkr": ibkr_client,
        "limitless": limitless_client,
    }

    # Initialize webhook notifier
    webhook_url = args.webhook or CONFIG_WEBHOOK_URL
    notifier = None
    if webhook_url:
        notifier = WebhookNotifier(webhook_url, min_profit=CONFIG_WEBHOOK_MIN_PROFIT)
        executor.notifier = notifier
        logger.info("Webhook notifications enabled.")

    # Start dashboard
    dashboard_port = args.dashboard_port if args.dashboard_port is not None else CONFIG_DASHBOARD_PORT
    dashboard_server = start_dashboard(dashboard_port)

    if args.continuous:
        run_continuous(args, min_profit, kalshi_client, kalshi_api_key_id,
                       kalshi_private_key_path, executor, db, price_cache,
                       extra_clients, notifier=notifier, pm_trader=pm_trader,
                       event_monitor=event_monitor,
                       kalshi_private_key_base64=kalshi_private_key_b64)
    else:
        _run_oneshot(args, min_profit, kalshi_client, executor, db, extra_clients,
                     notifier=notifier, event_monitor=event_monitor)

    if dashboard_server:
        dashboard_server.shutdown()
    db.close()


if __name__ == "__main__":
    main()
