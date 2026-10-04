"""Centralized configuration — all constants backed by environment variables."""

import logging
import math
import os
import re
import sys
from pathlib import Path
from dotenv import load_dotenv

# Project-local .env only. Never merge personal/global env files (e.g.
# ~/.claude/.env) into the bot environment — that leaks unrelated personal
# credentials into the trading process.
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Environment variable helpers (safe parsing with clear error messages)
# ---------------------------------------------------------------------------

class ConfigError(ValueError):
    """Raised when an environment variable has an invalid value."""


def _env_float(name: str, default: str) -> float:
    """Read an env var as float, raising ConfigError on bad values."""
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (ValueError, TypeError):
        raise ConfigError(
            f"Environment variable {name}={raw!r} is not a valid float"
        )


def _env_non_negative_float(name: str, default: str) -> float:
    """Read a finite non-negative float, raising ConfigError otherwise."""
    value = _env_float(name, default)
    if not math.isfinite(value) or value < 0:
        raise ConfigError(
            f"Environment variable {name}={value!r} must be finite and >= 0"
        )
    return value


_ZERO_ETH_ADDRESS = "0x" + "0" * 40
_ETH_ADDRESS_PATTERN = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _is_valid_eth_address(address: str | None) -> bool:
    """Validate that an address is a non-empty, non-zero 20-byte hexadecimal Ethereum address."""
    if not address or not isinstance(address, str):
        return False
    if not _ETH_ADDRESS_PATTERN.match(address):
        return False
    if address.lower() == _ZERO_ETH_ADDRESS.lower():
        return False
    return True


def _env_int(name: str, default: str) -> int:
    """Read an env var as int, raising ConfigError on bad values."""
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (ValueError, TypeError):
        raise ConfigError(
            f"Environment variable {name}={raw!r} is not a valid integer"
        )


def _env_bool(name: str, default: str) -> bool:
    """Read an env var as bool (true/false), raising ConfigError on bad values."""
    raw = os.getenv(name, default).lower().strip()
    if raw in ("true", "1", "yes"):
        return True
    if raw in ("false", "0", "no"):
        return False
    raise ConfigError(
        f"Environment variable {name}={raw!r} is not a valid boolean "
        f"(expected true/false, 1/0, or yes/no)"
    )


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("LOG_FILE", "")  # Empty = no file logging


def setup_logging(level: str | None = None, log_file: str | None = None):
    """Configure root logger with console and optional file handlers."""
    lvl = getattr(logging, (level or LOG_LEVEL), logging.INFO)
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [
        logging.StreamHandler(sys.stdout),
    ]
    handlers[0].setLevel(lvl)

    file_path = log_file if log_file is not None else LOG_FILE
    if file_path:
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(fmt, datefmt=datefmt))
        handlers.append(fh)

    logging.basicConfig(level=logging.DEBUG, format=fmt, datefmt=datefmt,
                        handlers=handlers, force=True)


# Scanner defaults
DEFAULT_MIN_PROFIT = _env_float("MIN_PROFIT_THRESHOLD", "0.005")
FUZZY_MATCH_THRESHOLD = _env_int("FUZZY_MATCH_THRESHOLD", "72")
WS_SUBSCRIPTION_LIMIT = _env_int("WS_SUBSCRIPTION_LIMIT", "2000")
WS_TRIGGER_ENABLED = _env_bool("WS_TRIGGER_ENABLED", "true")
WS_TRIGGER_THRESHOLD = _env_float("WS_TRIGGER_THRESHOLD", "0.03")
WS_TRIGGER_DEDUPE_SECONDS = _env_non_negative_float("WS_TRIGGER_DEDUPE_SECONDS", "5.0")
PARALLEL_WORKERS = _env_int("PARALLEL_WORKERS", "4")
RESCAN_INTERVAL = _env_int("RESCAN_INTERVAL", "30")
MAX_RESOLUTION_DAYS = _env_int("MAX_RESOLUTION_DAYS", "7")

# Kalshi fee parameters
KALSHI_FEE_CAP_CENTS = _env_int("KALSHI_FEE_CAP_CENTS", "175")

# Risk management
BASE_TRADE_SIZE = _env_float("BASE_TRADE_SIZE", "1.0")
MAX_TRADE_SIZE = _env_float("MAX_TRADE_SIZE", "5.0")
DAILY_LOSS_LIMIT = _env_float("DAILY_LOSS_LIMIT", "25.0")
MAX_OPEN_POSITIONS = _env_int("MAX_OPEN_POSITIONS", "10")
MAX_DAILY_TRADES = _env_int("MAX_DAILY_TRADES", "0")  # 0 = unlimited
MIN_LIQUIDITY = _env_float("MIN_LIQUIDITY", "10.0")
MIN_LIQUIDITY_HIGH_ROI = _env_float("MIN_LIQUIDITY_HIGH_ROI", "5.0")
MIN_NET_ROI = _env_float("MIN_NET_ROI", "0")
# Entry discipline: refuse taker/arb entries below this per-contract price.
# Penny longshots (e.g. $0.01 sports outcomes) have no resting bids to exit
# into — the hedger cannot save a position the market won't buy back.
# MarketMake legs are exempt (resting cheap quotes is how rewards are farmed).
MIN_ENTRY_PRICE = _env_float("MIN_ENTRY_PRICE", "0.05")
# Entry discipline, part 2: require a live exit bid on every buy leg before
# placing orders. MIN_ENTRY_PRICE blocks penny longshots; this blocks any
# market (at any price) whose book is one-sided, where a partial fill could
# not be hedged or unwound. Depth is contracts resting at the best exit bid.
# Checked at execution time against the live order book; fails closed when
# the book cannot be fetched. MarketMake legs are exempt.
EXIT_LIQUIDITY_GATE_ENABLED = _env_bool("EXIT_LIQUIDITY_GATE_ENABLED", "true")
MIN_EXIT_BID_DEPTH = _env_int("MIN_EXIT_BID_DEPTH", "10")
ALLOW_BETTER_REENTRY = _env_bool("ALLOW_BETTER_REENTRY", "true")
REENTRY_IMPROVEMENT_THRESHOLD = _env_float("REENTRY_IMPROVEMENT_THRESHOLD", "0.20")

# Dynamic sizing
DYNAMIC_SIZING_ENABLED = _env_bool("DYNAMIC_SIZING_ENABLED", "true")
SIZING_AGGRESSIVENESS = _env_float("SIZING_AGGRESSIVENESS", "0.5")

# Kelly criterion position sizing
KELLY_FRACTION = _env_float("KELLY_FRACTION", "0.5")
KELLY_MAX_FRACTION = _env_float("KELLY_MAX_FRACTION", "0.25")

# Execution
DRY_RUN = _env_bool("DRY_RUN", "true")

# ``--mode research`` runs the Layer 1 structural strategies (Kalshi binary/multi,
# Fréchet, temporal, CTF) observationally, without flipping their execution flags.
# It only activates while DRY_RUN is true, so a live flip cannot turn it into trading.
RESEARCH_MODE = "research"


def research_dry_run(mode: str) -> bool:
    """True when ``mode`` is the observational research mode and DRY_RUN is on."""
    return mode == RESEARCH_MODE and DRY_RUN
_raw_exec_mode = os.getenv("EXECUTION_MODE", "semi-auto")
EXECUTION_MODE = "full-auto" if _raw_exec_mode == "auto" else _raw_exec_mode

# Canary / paper gates (used by scripts/canary_trade.py and optional CLI)
# CANARY_MODE=paper  → scan real matched pairs, build legs, log only (no orders)
# CANARY_MODE=live   → place real orders with hard size/count caps below
CANARY_MODE = os.getenv("CANARY_MODE", "").strip().lower()  # "", "paper", "live"
CANARY_MAX_TRADE_SIZE = _env_float("CANARY_MAX_TRADE_SIZE", "1.0")
CANARY_MAX_TRADES = _env_int("CANARY_MAX_TRADES", "1")
CANARY_MIN_NET_ROI = _env_float("CANARY_MIN_NET_ROI", "0.01")  # 1%
CANARY_PLATFORMS = frozenset(
    p.strip().lower()
    for p in os.getenv("CANARY_PLATFORMS", "polymarket,kalshi").split(",")
    if p.strip()
)
# Live canary requires explicit acknowledgement to prevent accidental live runs.
CANARY_LIVE_ACK = os.getenv("CANARY_LIVE_ACK", "").strip()

# Platform execution whitelist — only these platforms can place live orders.
# Comma-separated list of platform names. Platforms not listed here will still
# be scanned for price data but will never execute trades.
_VALID_PLATFORMS = frozenset([
    "polymarket", "polymarket_ctf", "kalshi", "betfair", "smarkets",
    "sxbet", "matchbook", "gemini", "ibkr", "limitless",
])
_raw_enabled = os.getenv("ENABLED_EXECUTION_PLATFORMS", "kalshi")
if _raw_enabled.strip().lower() in ("all", "all platforms", "*"):
    ENABLED_EXECUTION_PLATFORMS: frozenset[str] = _VALID_PLATFORMS
else:
    ENABLED_EXECUTION_PLATFORMS: frozenset[str] = frozenset(
        p.strip().lower() for p in _raw_enabled.split(",") if p.strip()
    )

# Scan-venue pin (SCAN_VENUES preferred; PAPER_SCAN_VENUES is the legacy name).
# When set to kalshi / kalshi-only, skip Polymarket fetches even if --mode all.
# International Polymarket is forbidden to execute from this book.
_PAPER_SCAN_VENUES = (
    os.getenv("SCAN_VENUES") or os.getenv("PAPER_SCAN_VENUES") or ""
).strip().lower()
_POLYMARKET_SCAN_SKIP_MODES = frozenset({
    "kalshi", "betfair", "smarkets", "sxbet", "matchbook",
    "gemini", "ibkr", "triangular", "mm-pilot", "rewards",
})


def polymarket_scan_enabled(mode: str) -> bool:
    """Whether this run should fetch or scan Polymarket markets."""
    if _PAPER_SCAN_VENUES in {"kalshi", "kalshi-only"}:
        return False
    return mode not in _POLYMARKET_SCAN_SKIP_MODES


def polymarket_reward_fetch_enabled(mode: str) -> bool:
    """Whether this run should hit the Polymarket rewards endpoint.

    ``rewards`` is in the market-scan skip set (it does not fetch the CLOB
    universe), but it still needs the dedicated reward feed unless the run is
    pinned to Kalshi-only.
    """
    if _PAPER_SCAN_VENUES in {"kalshi", "kalshi-only"}:
        return False
    return mode in ("all", "rewards")

# Platform minimum order sizes (USD). Orders below these are rejected
# client-side to prevent API rejections and costly partial-fill hedging.
PLATFORM_MIN_ORDER_SIZE: dict[str, float] = {
    "polymarket": 0.01,
    "polymarket_ctf": 0.01,
    "kalshi": 0.01,
    "sxbet": 1.00,
    "gemini": 0.01,
    "ibkr": 0.01,
    "betfair": 2.50,
    "smarkets": 6.25,
    "matchbook": 5.50,
    "limitless": 0.01,
}

# Polygon gas cost estimate (per transaction, in dollars)
POLYGON_GAS_ESTIMATE = _env_float("POLYGON_GAS_ESTIMATE", "0.005")

# Polymarket fee model (March 2026: dynamic taker, zero maker)
POLYMARKET_DEFAULT_TAKER_RATE = _env_float("POLYMARKET_TAKER_FEE_RATE", "0.04")
POLYMARKET_MAKER_FEE_RATE = _env_float("POLYMARKET_MAKER_FEE_RATE", "0.0")

# Gemini fee model (March 18, 2026: P*(1-P)*rate)
GEMINI_TAKER_RATE = _env_float("GEMINI_TAKER_FEE_RATE", "0.07")
GEMINI_MAKER_RATE = _env_float("GEMINI_MAKER_FEE_RATE", "0.0175")

# Kalshi maker fee multiplier (per D-07, env-var override for hotfixing)
KALSHI_MAKER_MULTIPLIER = _env_float("KALSHI_MAKER_FEE_MULTIPLIER", "1.75")

# Matchbook prediction market commission (per Pitfall 4, promo may expire)
MATCHBOOK_PREDICTION_COMMISSION = _env_float("MATCHBOOK_PREDICTION_COMMISSION", "0.0")

# Revalidation
REVALIDATION_MIN_FLOOR = _env_float("REVALIDATION_MIN_FLOOR", "0.001")
REVALIDATION_ADAPTIVE = _env_bool("REVALIDATION_ADAPTIVE", "true")

# ---------------------------------------------------------------------------
# Strategy layers and layer-specific revalidation floors (per D-02, D-03)
# ---------------------------------------------------------------------------
STRATEGY_LAYERS: dict[str, int] = {
    # Layer 1 — Pure Arbitrage
    "Binary": 1, "KalshiBinary": 1, "Cross": 1, "NegRisk": 1,
    "MultiCross": 1, "TriangularCross": 1,
    "BetfairBackAll": 1, "BetfairBackLay": 1,
    "SmarketsBackAll": 1, "SmarketsBackLay": 1,
    "SXBetBackAll": 1, "SXBetBackLay": 1,
    "MatchbookBackAll": 1, "MatchbookBackLay": 1,
    "GeminiBinary": 1, "GeminiMulti": 1,
    "IBKRBinary": 1,
    "Spread": 1,
    # #30-#32: New Layer 1 strategies
    "ConditionalArb": 1, "BracketArb": 1, "NWayArb": 1, "FrechetArb": 1, "TemporalArb": 1,
    # Layer 2 — Near-Arbitrage
    "StalePriceOpp": 2, "ResolutionSnipeOpp": 2, "FeePromo": 2,
    # #33-#35: New Layer 2 strategies
    "SettlementTimingArb": 2, "NewMarketMispricing": 2, "APIOutageArb": 2,
    # Layer 3 — Market Making
    "MarketMake": 3, "CrossPlatformMM": 3,
    # #36-#38: New Layer 3 strategies
    "VolatilityAdjustedMM": 3, "LeadLagMM": 3, "ToxicFlowPause": 3,
    # Layer 4 — Informed Trading
    "EventDivergence": 4, "ConvergenceOpp": 4,
    # #39-#43: New Layer 4 strategies
    "SocialSentiment": 4, "ExpertDivergence": 4, "CalibratedSignal": 4,
    "InsiderPattern": 4, "CrossCategoryCorrelation": 4,
    # Layer 5 — Capital Optimization (scoring adjustments, not opps)
    "OpportunityCostScore": 5, "MarginOptimization": 5,
    "TaxAwarePosition": 5, "WithdrawalTiming": 5,
}

REVAL_FLOOR_L1 = _env_float("REVAL_FLOOR_L1", "0.02")   # 2% pure arb
REVAL_FLOOR_L2 = _env_float("REVAL_FLOOR_L2", "0.05")   # 5% near-arb
REVAL_FLOOR_L3 = _env_float("REVAL_FLOOR_L3", "0.03")   # 3% market making
REVAL_FLOOR_L4 = _env_float("REVAL_FLOOR_L4", "0.10")   # 10% informed
REVAL_FLOORS: dict[int, float] = {
    1: REVAL_FLOOR_L1, 2: REVAL_FLOOR_L2,
    3: REVAL_FLOOR_L3, 4: REVAL_FLOOR_L4,
}


def get_layer(opp_type: str) -> int:
    """Look up strategy layer for an opportunity type string.

    Checks exact match first, then prefix match for parameterized types
    like 'NegRisk(5)' or 'Cross(PM_YES + K_NO)'.
    Returns 0 for unknown types.
    """
    if opp_type in STRATEGY_LAYERS:
        return STRATEGY_LAYERS[opp_type]
    for prefix, layer in STRATEGY_LAYERS.items():
        if opp_type.startswith(prefix):
            return layer
    return 0

# API rate limits (seconds between requests)
PM_RATE_LIMIT = _env_float("PM_RATE_LIMIT", "0.01")
KALSHI_RATE_LIMIT = _env_float("KALSHI_RATE_LIMIT", "0.05")

# Dust trade filter — minimum profit to execute (avoids wasting gas)
MIN_PROFIT_AMOUNT = _env_float("MIN_PROFIT_AMOUNT", "0.02")

# Fill polling (Polymarket only; Kalshi FOK fills instantly)
FILL_POLL_INTERVAL = _env_float("FILL_POLL_INTERVAL", "0.1")
FILL_POLL_TIMEOUT = _env_float("FILL_POLL_TIMEOUT", "5.0")

# Partial fill hedging
HEDGE_ENABLED = _env_bool("HEDGE_ENABLED", "true")
HEDGE_MAX_ATTEMPTS = _env_int("HEDGE_MAX_ATTEMPTS", "5")
HEDGE_MAX_SPREAD_LOSS_PCT = _env_float("HEDGE_MAX_SPREAD_LOSS_PCT", "0.15")

# Inventory-hedged market making (Strategy #12).
# When MM inventory on a market exceeds MM_HEDGE_THRESHOLD * max_inventory,
# automatically attempt to sell back at the current best bid via the
# existing PartialFillHedger platform dispatch.
MM_AUTO_HEDGE_ENABLED = _env_bool("MM_AUTO_HEDGE_ENABLED", "false")
MM_HEDGE_THRESHOLD = _env_float("MM_HEDGE_THRESHOLD", "0.8")

# Auto-rebalancing / treasury (Strategy #18).
# Programmatic transfers between Gemini and Polymarket (USDC on Polygon).
# All other platforms are read-only and remain on the manual-rebalance path.
AUTO_REBALANCE_ENABLED = _env_bool("AUTO_REBALANCE_ENABLED", "false")
MAX_AUTO_TRANSFER_PER_DAY = _env_float("MAX_AUTO_TRANSFER_PER_DAY", "500.0")
MIN_TRANSFER_AMOUNT = _env_float("MIN_TRANSFER_AMOUNT", "50.0")
POLYMARKET_DEPOSIT_ADDRESS = os.getenv("POLYMARKET_DEPOSIT_ADDRESS", "")

# Cross-platform market making (Strategy #11).
# Posts opposing limit orders on two platforms for the same matched event.
# When the spread `ask_high - bid_low - sum_fees` exceeds CROSS_MM_MIN_SPREAD,
# the scan emits a CrossPlatformMM opp with both legs pre-built.
CROSS_MM_ENABLED = _env_bool("CROSS_MM_ENABLED", "false")
CROSS_MM_MIN_SPREAD = _env_float("CROSS_MM_MIN_SPREAD", "0.04")
CROSS_MM_MAX_INVENTORY = _env_float("CROSS_MM_MAX_INVENTORY", "200.0")
CROSS_MM_QUOTE_SIZE = _env_float("CROSS_MM_QUOTE_SIZE", "5.0")
CROSS_MM_PLATFORMS = os.getenv("CROSS_MM_PLATFORMS", "polymarket,kalshi")

# Cross-venue delta-neutral inventory balancer.
# Tracks inventory delta across Polymarket and Kalshi and generates
# rebalancing proposals to maintain delta-neutral positions (|Delta| -> 0).
INVENTORY_BALANCER_ENABLED = _env_bool("INVENTORY_BALANCER_ENABLED", "true")
INVENTORY_MAX_DELTA_CONTRACTS = _env_float("INVENTORY_MAX_DELTA_CONTRACTS", "50.0")
INVENTORY_MAX_IMBALANCE_RATIO = _env_float("INVENTORY_MAX_IMBALANCE_RATIO", "0.5")
INVENTORY_REBALANCE_MAX_COST = _env_float("INVENTORY_REBALANCE_MAX_COST", "25.0")
INVENTORY_AUTO_REBALANCE_ENABLED = _env_bool("INVENTORY_AUTO_REBALANCE_ENABLED", "false")
INVENTORY_REBALANCE_COOLDOWN_SEC = _env_float("INVENTORY_REBALANCE_COOLDOWN_SEC", "60.0")
INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO = _env_float("INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO", "0.4")


# Fee promotional arbitrage (Strategy #9).
# When enabled, cross-platform near-misses (within PROMO_NEAR_MISS_BAND of
# MIN_NET_ROI) are captured into NearMissCache and re-scored when fee rates
# drop. Calendar tracking warns when known promo windows are about to expire.
FEE_PROMO_ENABLED = _env_bool("FEE_PROMO_ENABLED", "false")
PROMO_NEAR_MISS_BAND = _env_float("PROMO_NEAR_MISS_BAND", "0.05")
PROMO_WARNING_DAYS = _env_int("PROMO_WARNING_DAYS", "7")

# Optional ISO-8601 dates marking the day after which each platform's
# current promotional fee rate expires. Empty string = no known expiry.
MATCHBOOK_PROMO_EXPIRES = os.getenv("MATCHBOOK_PROMO_EXPIRES", "")
GEMINI_PROMO_EXPIRES = os.getenv("GEMINI_PROMO_EXPIRES", "")
POLYMARKET_PROMO_EXPIRES = os.getenv("POLYMARKET_PROMO_EXPIRES", "")


def get_promo_expiry(platform: str):
    """Return the configured promo expiry date for a platform, or None.

    Args:
        platform: Platform name (matchbook, gemini, polymarket).

    Returns:
        A ``datetime.date`` if the env var is set and parses cleanly,
        otherwise ``None``. Unknown platforms always return ``None``.
    """
    from datetime import date
    raw = {
        "matchbook": MATCHBOOK_PROMO_EXPIRES,
        "gemini": GEMINI_PROMO_EXPIRES,
        "polymarket": POLYMARKET_PROMO_EXPIRES,
    }.get(platform.lower(), "")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None

# Betfair commission rate (2-5%, default 3% for moderate-volume users)
BETFAIR_COMMISSION_RATE = _env_float("BETFAIR_COMMISSION_RATE", "0.03")

# Betfair Streaming API (TLS TCP socket)
BETFAIR_STREAM_HOST = os.getenv("BETFAIR_STREAM_HOST", "stream-api.betfair.com")
BETFAIR_STREAM_PORT = _env_int("BETFAIR_STREAM_PORT", "443")

# Smarkets commission rate (fixed 2% for most users)
SMARKETS_COMMISSION_RATE = _env_float("SMARKETS_COMMISSION_RATE", "0.02")

# Proxy configuration
POLYMARKET_PROXY_URL = os.getenv("POLYMARKET_PROXY_URL")
KALSHI_PROXY_URL = os.getenv("KALSHI_PROXY_URL")
BETFAIR_PROXY_URL = os.getenv("BETFAIR_PROXY_URL")
SMARKETS_PROXY_URL = os.getenv("SMARKETS_PROXY_URL")
SXBET_PROXY_URL = os.getenv("SXBET_PROXY_URL")
MATCHBOOK_PROXY_URL = os.getenv("MATCHBOOK_PROXY_URL")
GEMINI_PROXY_URL = os.getenv("GEMINI_PROXY_URL")

# Platform credentials (presence-checked, not stored)
POLYMARKET_PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")
POLYMARKET_CHAIN_ID = _env_int("POLYMARKET_CHAIN_ID", "137")
POLYMARKET_FUNDER_ADDRESS = os.getenv("POLYMARKET_FUNDER_ADDRESS")
POLYMARKET_SIGNATURE_TYPE = _env_int("POLYMARKET_SIGNATURE_TYPE", "0")
KALSHI_API_KEY_ID = os.getenv("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PATH = os.getenv("KALSHI_PRIVATE_KEY_PATH")
# Self-heal: a boot during Kalshi's daily maintenance window must not
# permanently degrade the run (2026-07-23 incident — 31h Polymarket-only).
KALSHI_AUTH_BOOT_ATTEMPTS = _env_int("KALSHI_AUTH_BOOT_ATTEMPTS", "3")
KALSHI_AUTH_BOOT_RETRY_WAIT = _env_float("KALSHI_AUTH_BOOT_RETRY_WAIT", "20")
KALSHI_REAUTH_INTERVAL = _env_float("KALSHI_REAUTH_INTERVAL", "120")
BETFAIR_USERNAME = os.getenv("BETFAIR_USERNAME")
BETFAIR_PASSWORD = os.getenv("BETFAIR_PASSWORD")
BETFAIR_APP_KEY = os.getenv("BETFAIR_APP_KEY") or os.getenv("BETFAIR_API_KEY")
BETFAIR_API_KEY = BETFAIR_APP_KEY  # backward-compat alias

# Smarkets
SMARKETS_API_KEY = os.getenv("SMARKETS_API_KEY")

# SX Bet
SXBET_API_KEY = os.getenv("SXBET_API_KEY")
SXBET_PRIVATE_KEY = os.getenv("SXBET_PRIVATE_KEY")

# Matchbook
MATCHBOOK_USERNAME = os.getenv("MATCHBOOK_USERNAME")
MATCHBOOK_PASSWORD = os.getenv("MATCHBOOK_PASSWORD")

# Gemini Predictions
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_SECRET = os.getenv("GEMINI_API_SECRET")
GEMINI_BASE_URL = os.getenv("GEMINI_BASE_URL", "https://api.gemini.com")
GEMINI_FEE_RATE = _env_float("GEMINI_FEE_RATE", "0.05")  # 5% taker / 1% maker
GEMINI_ORDER_TYPE = os.getenv("GEMINI_ORDER_TYPE", "ioc")  # "ioc" or "gtc"
GEMINI_RATE_LIMIT = _env_float("GEMINI_RATE_LIMIT", "0.1")

# Exchange API rate limits (seconds between requests)
BETFAIR_RATE_LIMIT = _env_float("BETFAIR_RATE_LIMIT", "0.2")    # 5/s
SMARKETS_RATE_LIMIT = _env_float("SMARKETS_RATE_LIMIT", "0.2")  # 5/s
SXBET_RATE_LIMIT = _env_float("SXBET_RATE_LIMIT", "0.2")        # 5/s
MATCHBOOK_RATE_LIMIT = _env_float("MATCHBOOK_RATE_LIMIT", "0.2")  # 5/s

# IBKR ForecastEx (via IB Gateway / TWS socket).  Host is intentionally
# opt-in: a cloud deployment must not probe its own localhost on every boot.
IBKR_HOST = os.getenv("IBKR_HOST")
IBKR_PORT = _env_int("IBKR_PORT", "4001")
IBKR_CLIENT_ID = _env_int("IBKR_CLIENT_ID", "1")
IBKR_ORDER_RATE_LIMIT = _env_float("IBKR_ORDER_RATE_LIMIT", "5.0")

# Metaculus (read-only signal source).  The current API requires a token, and
# any commercial use requires a separate written agreement.  Both gates must
# be satisfied explicitly before the client is initialized.
METACULUS_API_KEY = os.getenv("METACULUS_API_KEY")
METACULUS_COMMERCIAL_USE_APPROVED = _env_bool(
    "METACULUS_COMMERCIAL_USE_APPROVED", "false"
)
METACULUS_CACHE_TTL = _env_float("METACULUS_CACHE_TTL", "300")

# ---------------------------------------------------------------------------
# Feature flags — defaults are false for local dev safety.
# Enable in production via Railway env vars:
#   MM_ENABLED=true              — Market making engine
#   SNAPSHOT_ENABLED=true        — Price snapshot recording for backtesting
#   DYNAMIC_FEE_ENABLED=true     — Real-time Polygon gas price monitoring
#   EVENT_MONITOR_ENABLED=true   — Metaculus/Manifold signal aggregation
# ---------------------------------------------------------------------------

# Dynamic fee arbitrage (GasMonitor)
POLYGON_RPC_URL = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
DYNAMIC_FEE_ENABLED = _env_bool("DYNAMIC_FEE_ENABLED", "false")
GAS_PRICE_CACHE_TTL = _env_float("GAS_PRICE_CACHE_TTL", "15.0")

# Event-driven trading (Metaculus divergence signals)
EVENT_DIVERGENCE_THRESHOLD = _env_float("EVENT_DIVERGENCE_THRESHOLD", "0.10")
EVENT_MONITOR_ENABLED = _env_bool("EVENT_MONITOR_ENABLED", "false")

# Stale price detection
STALE_PRICE_THRESHOLD = _env_float("STALE_PRICE_THRESHOLD", "30.0")
STALE_PRICE_MOVE_PCT = _env_float("STALE_PRICE_MOVE_PCT", "0.03")

# Market making
MM_ENABLED = _env_bool("MM_ENABLED", "false")
MM_MIN_SPREAD = _env_float("MM_MIN_SPREAD", "0.02")  # 2% minimum spread width
MM_QUOTE_SIZE = _env_float("MM_QUOTE_SIZE", "5.0")
MM_MAX_INVENTORY = _env_float("MM_MAX_INVENTORY", "500.0")  # $500 per market cap
MM_MAX_TOTAL_EXPOSURE = _env_float("MM_MAX_TOTAL_EXPOSURE", "500.0")
MM_REFRESH_INTERVAL = _env_float("MM_REFRESH_INTERVAL", "10.0")

# Kalshi Liquidity Incentive Program (LIP) market making — the MM lead
# strategy per docs/plans/02-kalshi-lip-mm-scope.md. Retail LIP ends
# 2027-01-01 (help centre, verified 2026-08-17); pools come from
# GET /incentive_programs.
LIP_MIN_POOL = _env_float("LIP_MIN_POOL", "10.0")  # ignore pools under $10/period
LIP_MAX_MARKETS = _env_int("LIP_MAX_MARKETS", "5")
LIP_SELECT_INTERVAL = _env_float("LIP_SELECT_INTERVAL", "3600.0")  # re-rank hourly
LIP_EXCLUDED_CATEGORIES = tuple(
    c.strip() for c in os.getenv("LIP_EXCLUDED_CATEGORIES", "Sports").split(",") if c.strip()
)
LIP_PRICE_BAND_LOW = _env_float("LIP_PRICE_BAND_LOW", "0.10")
LIP_PRICE_BAND_HIGH = _env_float("LIP_PRICE_BAND_HIGH", "0.90")
LIP_MIN_HOURS_REMAINING = _env_float("LIP_MIN_HOURS_REMAINING", "24.0")
LIP_DEPTH_PROBE_LIMIT = _env_int("LIP_DEPTH_PROBE_LIMIT", "25")  # max book fetches per selection pass

# Liquidity rewards (Polymarket + Kalshi)
REWARDS_ENABLED = _env_bool("REWARDS_ENABLED", "false")
REWARDS_MAX_EXPOSURE = _env_float("REWARDS_MAX_EXPOSURE", "200.0")
REWARDS_MIN_SIZE = _env_float("REWARDS_MIN_SIZE", "5.0")
REWARDS_MAX_SPREAD = _env_float("REWARDS_MAX_SPREAD", "0.05")
REWARDS_POLL_INTERVAL = _env_int("REWARDS_POLL_INTERVAL", "60")
REWARDS_MIN_RESTING_TIME = _env_int("REWARDS_MIN_RESTING_TIME", "300")
REWARDS_MAX_MARKETS = _env_int("REWARDS_MAX_MARKETS", "100")

# Limitless Exchange (Base CLOB) & delta-neutral reward farming
LIMITLESS_API_KEY = os.getenv("LIMITLESS_API_KEY", "")
LIMITLESS_PRIVATE_KEY = os.getenv("LIMITLESS_PRIVATE_KEY", "")
LIMITLESS_BASE_URL = os.getenv("LIMITLESS_BASE_URL", "https://api.limitless.exchange")
LIMITLESS_EXCHANGE_CONTRACT = os.getenv("LIMITLESS_EXCHANGE_CONTRACT", "")
LIMITLESS_REWARDS_ENABLED = _env_bool("LIMITLESS_REWARDS_ENABLED", "false")
LIMITLESS_MAX_INVENTORY = _env_float("LIMITLESS_MAX_INVENTORY", "200.0")
LIMITLESS_RATE_LIMIT = _env_float("LIMITLESS_RATE_LIMIT", "0.2")

# Kalshi Liquidity Incentive Program (LIP) — snapshot scoring of resting orders.
KALSHI_LIP_ENABLED = _env_bool("KALSHI_LIP_ENABLED", "false")
# Kalshi Volume Incentive Program (VIP) — passive volume-rebate tracking.
KALSHI_VIP_TRACK_ENABLED = _env_bool("KALSHI_VIP_TRACK_ENABLED", "false")
# VIP fills polling interval (seconds); tracking-only, never an execution path.
KALSHI_VIP_POLL_INTERVAL = _env_int("KALSHI_VIP_POLL_INTERVAL", "1800")

# Kalshi multi-outcome execution gating (kill-switch + depth check)
# Set to false to disable KalshiMulti scanning/execution entirely.
KALSHI_MULTI_ENABLED = _env_bool("KALSHI_MULTI_ENABLED", "true")

# S1: NegRisk NO-side arbitrage (buy all NO when Σ NO < N-1). Layer 1, default off.
NEGRISK_NO_SIDE_ENABLED = _env_bool("NEGRISK_NO_SIDE_ENABLED", "false")
# Minimum resting yes-side contracts required at the best ask on EACH leg
# before the executor will even attempt a KalshiMulti trade. Prevents the
# Fill-or-Kill partial-fill trap on thin multi-outcome markets.
KALSHI_MULTI_MIN_DEPTH = _env_int("KALSHI_MULTI_MIN_DEPTH", "10")
# Completeness floor for multi-outcome scans: a true single-winner event's
# YES asks sum to just under/over 1.0. A sum well below this means missing,
# closed, or stale legs — not a real arb.
KALSHI_MULTI_MIN_SUM = _env_float("KALSHI_MULTI_MIN_SUM", "0.85")
# Same implausible-sum defense for Gemini categorical events: point-spread
# events list alternative lines that are neither exclusive nor exhaustive,
# so summing their prices fakes riskless 90%+ ROI (2026-07-24 paper-window
# false-positive class — sibling of the Kalshi strike-ladder gate, PR #100).
GEMINI_MULTI_MIN_SUM = _env_float("GEMINI_MULTI_MIN_SUM", "0.85")

# Multi-outcome cross-platform execution gating (kill-switch + depth check)
# MultiCross places N legs concurrently across Polymarket + Kalshi. Same
# Fill-or-Kill partial-fill vulnerability as KalshiMulti. Set to false to
# disable MultiCross scanning/execution entirely.
MULTI_CROSS_ENABLED = _env_bool("MULTI_CROSS_ENABLED", "true")
# Minimum resting contracts required at the best ask on EACH Kalshi leg
# of a MultiCross arb before the executor will attempt the trade.
MULTI_CROSS_MIN_DEPTH = _env_int("MULTI_CROSS_MIN_DEPTH", "10")

# Execution budget per scan cycle. After sorting opportunities by
# capital_efficiency_score, only the top N are attempted in each scan.
# 0 = unlimited (current behavior). Set to a small integer (3-5) to
# force the bot to be selective and only execute the highest-priority
# opportunities each cycle, preserving capital for the best trades.
EXECUTION_BUDGET_PER_SCAN = _env_int("EXECUTION_BUDGET_PER_SCAN", "0")

# STRAT-01: Order Book Imbalance
IMBALANCE_ENABLED = _env_bool("IMBALANCE_ENABLED", "false")
IMBALANCE_RATIO = _env_float("IMBALANCE_RATIO", "3.0")
IMBALANCE_MAX_TRADE_SIZE = _env_float("IMBALANCE_MAX_TRADE_SIZE", "10.0")
IMBALANCE_MAX_CONCURRENT_POSITIONS = int(os.getenv("IMBALANCE_MAX_CONCURRENT_POSITIONS", "5"))

# STRAT-02: News-Driven Resolution Sniping
NEWS_SNIPE_ENABLED = _env_bool("NEWS_SNIPE_ENABLED", "false")
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_REQUEST_TIMEOUT = _env_float("FINNHUB_REQUEST_TIMEOUT", "10.0")
NEWS_SNIPE_MAX_TRADE_SIZE = _env_float("NEWS_SNIPE_MAX_TRADE_SIZE", "25.0")
NEWS_SNIPE_COOLDOWN = _env_float("NEWS_SNIPE_COOLDOWN", "30.0")
NEWS_SNIPE_CONFIDENCE_THRESHOLD = _env_float("NEWS_SNIPE_CONFIDENCE_THRESHOLD", "0.5")
# Stage 2 refiner: drop signals where the headline is older than this window.
NEWS_SNIPE_MAX_AGE_MINUTES = _env_int("NEWS_SNIPE_MAX_AGE_MINUTES", "60")

# Firecrawl web-search news source — alternative/supplemental to Finnhub for
# STRAT-02. Fills the specced-but-unbuilt news_monitor.py slot
# (.planning/research/ARCHITECTURE.md Pattern 6). Disabled by default; when
# enabled it is passed into scan_news_snipe() as an alternate client, NOT
# auto-wired into signal_aggregator.py's live source weights — that wiring
# is a separate, explicit decision (see DEFAULT_SOURCE_WEIGHTS).
FIRECRAWL_NEWS_ENABLED = _env_bool("FIRECRAWL_NEWS_ENABLED", "false")
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "")
FIRECRAWL_NEWS_REQUEST_TIMEOUT = _env_float("FIRECRAWL_NEWS_REQUEST_TIMEOUT", "15.0")

# STRAT-06: Correlated Market Pairs
CORRELATED_ENABLED = _env_bool("CORRELATED_ENABLED", "false")
CORRELATED_PAIRS = os.getenv("CORRELATED_PAIRS", "[]")  # JSON list of [market_a, market_b] pairs
CORRELATION_DIVERGENCE_THRESHOLD = _env_float("CORRELATION_DIVERGENCE_THRESHOLD", "0.10")
CORRELATED_MAX_TRADE_SIZE = _env_float("CORRELATED_MAX_TRADE_SIZE", "20.0")
CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD = _env_float("CORRELATED_MIN_SPREAD_COLLAPSE_THRESHOLD", "0.20")

# PR E — auto-correlation detection (correlation_tracker.py)
# When enabled, the tracker runs nightly over the last
# CORRELATION_LOOKBACK_DAYS of snapshots, computes Pearson r between
# every pair of markets, and caches pairs with |r| >=
# CORRELATION_PEARSON_THRESHOLD for scan_correlated() to consume
# alongside the manually-configured CORRELATED_PAIRS.
CORRELATION_AUTO_DETECT_ENABLED = _env_bool("CORRELATION_AUTO_DETECT_ENABLED", "false")
CORRELATION_PEARSON_THRESHOLD = _env_float("CORRELATION_PEARSON_THRESHOLD", "0.85")
CORRELATION_LOOKBACK_DAYS = _env_int("CORRELATION_LOOKBACK_DAYS", "30")
CORRELATION_MIN_SAMPLES = _env_int("CORRELATION_MIN_SAMPLES", "24")
# Tracker refresh interval — default 24h, mirrors BACKTEST_RUN_INTERVAL.
CORRELATION_TRACKER_INTERVAL = _env_float("CORRELATION_TRACKER_INTERVAL", "86400")
# Bucket width (seconds) for the Pearson time-series grid. 1h default.
CORRELATION_BUCKET_SECONDS = _env_int("CORRELATION_BUCKET_SECONDS", "3600")

# STRAT-07: Time Decay Convergence
TIME_DECAY_ENABLED = _env_bool("TIME_DECAY_ENABLED", "false")
TIME_DECAY_MIN_HOURS_EXPIRY = int(os.getenv("TIME_DECAY_MIN_HOURS_EXPIRY", "48"))
TIME_DECAY_MIN_CONSENSUS = _env_float("TIME_DECAY_MIN_CONSENSUS", "0.90")
TIME_DECAY_BUY_BELOW_PRICE = _env_float("TIME_DECAY_BUY_BELOW_PRICE", "0.95")
TIME_DECAY_MAX_TRADE_SIZE = _env_float("TIME_DECAY_MAX_TRADE_SIZE", "50.0")
TIME_DECAY_MIN_CONSENSUS_SAMPLE_SIZE = int(os.getenv("TIME_DECAY_MIN_CONSENSUS_SAMPLE_SIZE", "30"))

# STRAT-04: Logical Arbitrage
LOGICAL_ARB_ENABLED = _env_bool("LOGICAL_ARB_ENABLED", "false")
LOGICAL_ARB_PRICE_THRESHOLD = _env_float("LOGICAL_ARB_PRICE_THRESHOLD", "0.05")
LOGICAL_ARB_MAX_TRADE_SIZE = _env_float("LOGICAL_ARB_MAX_TRADE_SIZE", "20.0")

# Load logical arb rules from env var (JSON string) or file (logical_arb_rules.json)
import json
LOGICAL_ARB_RULES = []
if LOGICAL_ARB_ENABLED:
    rules_str = os.getenv("LOGICAL_ARB_RULES", "")
    if rules_str:
        try:
            LOGICAL_ARB_RULES = json.loads(rules_str)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid LOGICAL_ARB_RULES JSON: {e}")
    else:
        # Fallback to file
        rules_file = "logical_arb_rules.json"
        if os.path.exists(rules_file):
            with open(rules_file) as f:
                LOGICAL_ARB_RULES = json.load(f)
        else:
            logger.warning("LOGICAL_ARB_ENABLED but no rules provided; feature disabled")
            LOGICAL_ARB_ENABLED = False

# STRAT-05: Whale Copy Trading
WHALE_COPY_ENABLED = _env_bool("WHALE_COPY_ENABLED", "false")
WHALE_COPY_MAX_TRADE_SIZE = _env_float("WHALE_COPY_MAX_TRADE_SIZE", "15.0")
WHALE_COPY_MAX_POSITIONS = _env_int("WHALE_COPY_MAX_POSITIONS", "5")
WHALE_COPY_POLL_INTERVAL = _env_int("WHALE_COPY_POLL_INTERVAL", "10")

# Whale wallet addresses: comma-separated list of profitable Polymarket wallets to track
WHALE_WALLETS = []
whale_wallets_str = os.getenv("WHALE_WALLETS", "").strip()
if whale_wallets_str:
    WHALE_WALLETS = [w.strip() for w in whale_wallets_str.split(",") if w.strip()]
if WHALE_COPY_ENABLED and not WHALE_WALLETS:
    logger.warning("WHALE_COPY_ENABLED but WHALE_WALLETS empty; feature disabled")
    WHALE_COPY_ENABLED = False

# Polygonscan API for on-chain monitoring
POLYGONSCAN_API_KEY = os.getenv("POLYGONSCAN_API_KEY", "")

# ---------------------------------------------------------------------------
# Extended Strategy Flags (#30-#49) — Phase 2 expansion, all default false
# ---------------------------------------------------------------------------

# Layer 1 — Pure Arbitrage (New)
# Plan 02: Fréchet-Bound Logical Arbitrage — A ⊆ B coherence bounds P(A) <= P(B)
FRECHET_ARB_ENABLED = _env_bool("FRECHET_ARB_ENABLED", "false")
FRECHET_MIN_VIOLATION = _env_float("FRECHET_MIN_VIOLATION", "0.02")
FRECHET_ARB_MAX_TRADE_SIZE = _env_float("FRECHET_ARB_MAX_TRADE_SIZE", "25.0")

# Plan 03: Cross-Date / Nested Temporal Arbitrage — D_early < D_late monotonicity bounds P(early) <= P(late)
TEMPORAL_ARB_ENABLED = _env_bool("TEMPORAL_ARB_ENABLED", "false")
TEMPORAL_MIN_VIOLATION = _env_float("TEMPORAL_MIN_VIOLATION", "0.02")
TEMPORAL_ARB_MAX_TRADE_SIZE = _env_float("TEMPORAL_ARB_MAX_TRADE_SIZE", "25.0")

# Plan 04: CTF mint/split & merge/redeem primitives (Polymarket on-chain CTF)
CTF_ENABLED = _env_bool("CTF_ENABLED", "false")
CTF_MERGE_ENABLED = _env_bool("CTF_MERGE_ENABLED", "false")
CTF_MINT_SELL_ENABLED = _env_bool("CTF_MINT_SELL_ENABLED", "false")
CTF_CONVERT_ENABLED = _env_bool("CTF_CONVERT_ENABLED", "false")
CTF_MIN_PROFIT = _env_float("CTF_MIN_PROFIT", "0.005")
CTF_MAX_TRADE_SIZE = _env_float("CTF_MAX_TRADE_SIZE", "25.0")
CTF_GAS_ESTIMATE = _env_non_negative_float("CTF_GAS_ESTIMATE", "0.01")
CTF_MAX_RESOLUTION_DAYS = _env_int("CTF_MAX_RESOLUTION_DAYS", "0")
CONDITIONAL_TOKENS_ADDRESS = os.getenv("CONDITIONAL_TOKENS_ADDRESS", "0x4d97dcd9" "7ec945f40cf65f87097ace5ea0476045")
NEG_RISK_ADAPTER_ADDRESS = os.getenv("NEG_RISK_ADAPTER_ADDRESS", "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296")
COLLATERAL_TOKEN_ADDRESS = os.getenv("COLLATERAL_TOKEN_ADDRESS", "0xc011a7e1" "2a19f7b1f670d46f03b03f3342e82dfb")

# Plan 05: UMA oracle dispute-risk gate (defensive) — block resolution-held Polymarket arbs in proposal/dispute window
DISPUTE_GATE_ENABLED = _env_bool("DISPUTE_GATE_ENABLED", "false")

# #30: Conditional Market Arbitrage — P(X|Y) × P(Y) ≠ P(X) detection
CONDITIONAL_ARB_ENABLED = _env_bool("CONDITIONAL_ARB_ENABLED", "false")
CONDITIONAL_ARB_MIN_DIVERGENCE = _env_float("CONDITIONAL_ARB_MIN_DIVERGENCE", "0.05")
CONDITIONAL_ARB_MAX_TRADE_SIZE = _env_float("CONDITIONAL_ARB_MAX_TRADE_SIZE", "25.0")

# #31: Bracket/Range Market Arbitrage — Σ(range brackets) > 1.0
BRACKET_ARB_ENABLED = _env_bool("BRACKET_ARB_ENABLED", "false")
BRACKET_ARB_MIN_SPREAD = _env_float("BRACKET_ARB_MIN_SPREAD", "0.02")
BRACKET_ARB_MAX_TRADE_SIZE = _env_float("BRACKET_ARB_MAX_TRADE_SIZE", "25.0")

# #32: Multi-Leg Exotic Arb — N-way extension of triangular (4+ platforms)
NWAY_ARB_ENABLED = _env_bool("NWAY_ARB_ENABLED", "false")
NWAY_ARB_MAX_LEGS = _env_int("NWAY_ARB_MAX_LEGS", "5")
NWAY_ARB_MAX_TRADE_SIZE = _env_float("NWAY_ARB_MAX_TRADE_SIZE", "20.0")

# Layer 2 — Near-Arbitrage (New)
# #33: Settlement Timing Arb — buy winning outcome on slow-settling platform
SETTLEMENT_TIMING_ENABLED = _env_bool("SETTLEMENT_TIMING_ENABLED", "false")
SETTLEMENT_TIMING_MIN_DISCOUNT = _env_float("SETTLEMENT_TIMING_MIN_DISCOUNT", "0.005")
SETTLEMENT_TIMING_MAX_TRADE_SIZE = _env_float("SETTLEMENT_TIMING_MAX_TRADE_SIZE", "50.0")

# #34: New Market Mispricing — first 24-48h price inefficiency
NEW_MARKET_MISPRICING_ENABLED = _env_bool("NEW_MARKET_MISPRICING_ENABLED", "false")
NEW_MARKET_AGE_HOURS = _env_float("NEW_MARKET_AGE_HOURS", "48.0")
NEW_MARKET_MIN_DIVERGENCE = _env_float("NEW_MARKET_MIN_DIVERGENCE", "0.10")
NEW_MARKET_MAX_TRADE_SIZE = _env_float("NEW_MARKET_MAX_TRADE_SIZE", "15.0")

# #35: API Outage Arbitrage — exploit stale prices during platform outages
API_OUTAGE_ARB_ENABLED = _env_bool("API_OUTAGE_ARB_ENABLED", "false")
API_OUTAGE_STALE_THRESHOLD = _env_float("API_OUTAGE_STALE_THRESHOLD", "120.0")
API_OUTAGE_MIN_DIVERGENCE = _env_float("API_OUTAGE_MIN_DIVERGENCE", "0.05")

# Layer 3 — Market Making (New)
# #36: Volatility-Adjusted Spread Quoting — dynamic spread widening
MM_VOLATILITY_ADJUSTED_ENABLED = _env_bool("MM_VOLATILITY_ADJUSTED_ENABLED", "false")
MM_VOLATILITY_LOOKBACK_SECONDS = _env_float("MM_VOLATILITY_LOOKBACK_SECONDS", "300.0")
MM_VOLATILITY_SPREAD_MULTIPLIER = _env_float("MM_VOLATILITY_SPREAD_MULTIPLIER", "2.0")

# #37: Lead-Lag Market Making — quote lagging platforms using leader's price
LEAD_LAG_MM_ENABLED = _env_bool("LEAD_LAG_MM_ENABLED", "false")
LEAD_LAG_MIN_DELAY_MS = _env_float("LEAD_LAG_MIN_DELAY_MS", "500.0")
LEAD_LAG_PLATFORMS = os.getenv("LEAD_LAG_PLATFORMS", "polymarket,kalshi")

# #38: Toxic Flow Detection & Adverse Selection Tuning
MM_TOXIC_FLOW_ENABLED = _env_bool("MM_TOXIC_FLOW_ENABLED", "false")
MM_TOXIC_FLOW_THRESHOLD = _env_float("MM_TOXIC_FLOW_THRESHOLD", "0.60")
MM_TOXIC_FLOW_PAUSE_SECONDS = _env_float("MM_TOXIC_FLOW_PAUSE_SECONDS", "60.0")
MM_TOXIC_FLOW_HALF_LIFE_SECONDS = _env_float("MM_TOXIC_FLOW_HALF_LIFE_SECONDS", "60.0")
MM_TOXIC_SPREAD_FACTOR = _env_float("MM_TOXIC_SPREAD_FACTOR", "1.5")
MM_TOXIC_SIZE_TAPER_FACTOR = _env_float("MM_TOXIC_SIZE_TAPER_FACTOR", "0.8")
MM_TOXIC_MIN_SIZE_FRACTION = _env_float("MM_TOXIC_MIN_SIZE_FRACTION", "0.2")
MM_FILL_VELOCITY_WINDOW_SECONDS = _env_float("MM_FILL_VELOCITY_WINDOW_SECONDS", "30.0")
MM_FILL_VELOCITY_BURST_THRESHOLD = _env_int("MM_FILL_VELOCITY_BURST_THRESHOLD", "3")

# ---------------------------------------------------------------------------
# Kalshi reward-MM pilot (plan 10 — docs/plans/10-mm-pilot-prep.md).
# Independently gated from the legacy Polymarket MM_ENABLED path. Venue is
# Kalshi ONLY: every pilot order is hard-checked platform == "kalshi" at the
# authorize_order choke point in mm_pilot.py, on top of the
# ENABLED_EXECUTION_PLATFORMS allowlist. All defaults are the conservative
# tranche-1 pilot values from the spec ($2-3K bankroll, <=$300 gross/market).
# ---------------------------------------------------------------------------
MM_KALSHI_PILOT_ENABLED = _env_bool("MM_KALSHI_PILOT_ENABLED", "false")
LIVE_ENVELOPE: dict | None = None

# Fill detection (spec section 3) — REST polling of /portfolio/fills.
MM_FILL_POLL_SECONDS = _env_float("MM_FILL_POLL_SECONDS", "2.0")

# Auto-hedge (spec section 4).
MM_INVENTORY_TARGET_USD = _env_float("MM_INVENTORY_TARGET_USD", "0.0")
MM_HEDGE_DEADBAND_USD = _env_float("MM_HEDGE_DEADBAND_USD", "5.0")
MM_HEDGE_MAX_LATENCY_SECONDS = _env_float("MM_HEDGE_MAX_LATENCY_SECONDS", "10.0")
MM_HALT_WINDOW_SECONDS = _env_float("MM_HALT_WINDOW_SECONDS", "3600.0")

# Inventory caps — hard, enforced in the order path (spec section 5).
# Both USD and contract units are enforced; the most restrictive wins.
# The legacy MM_MAX_INVENTORY / MM_MAX_TOTAL_EXPOSURE stay untouched for the
# old path; the pilot reads only these MM_MAX_* keys.
MM_MAX_INVENTORY_USD = _env_float("MM_MAX_INVENTORY_USD", "100.0")
MM_MAX_INVENTORY_CONTRACTS = _env_int("MM_MAX_INVENTORY_CONTRACTS", "250")
MM_MAX_TOTAL_INVENTORY_USD = _env_float("MM_MAX_TOTAL_INVENTORY_USD", "250.0")
MM_MAX_GROSS_PER_MARKET_USD = _env_float("MM_MAX_GROSS_PER_MARKET_USD", "300.0")
MM_QUOTE_SIZE_USD = _env_float("MM_QUOTE_SIZE_USD", "10.0")
MM_PILOT_BANKROLL_USD = _env_float("MM_PILOT_BANKROLL_USD", "2000.0")

# Pre-quote gate thresholds (spec section 6).
MM_BOOK_MAX_STALE_SECONDS = _env_float("MM_BOOK_MAX_STALE_SECONDS", "30.0")
MM_VOL_PULL_MULTIPLIER = _env_float("MM_VOL_PULL_MULTIPLIER", "2.5")
MM_MAX_BOOK_DEPTH_FRACTION = _env_float("MM_MAX_BOOK_DEPTH_FRACTION", "0.25")

# LIP Target Size Balancer — dynamically size quoting volume to marginal LIP target
MM_LIP_BALANCER_ENABLED = _env_bool("MM_LIP_BALANCER_ENABLED", "true")
MM_LIP_BALANCER_MAX_SHARE = _env_float("MM_LIP_BALANCER_MAX_SHARE", "0.25")
MM_LIP_BALANCER_SCALE_UP = _env_bool("MM_LIP_BALANCER_SCALE_UP", "true")
MM_LIP_BALANCER_MIN_EFFICIENCY = _env_float("MM_LIP_BALANCER_MIN_EFFICIENCY", "0.50")

# Sub-minute WebSocket Orderbook Streaming for MM Pilot
MM_WS_ORDERBOOK_STREAMING_ENABLED = _env_bool("MM_WS_ORDERBOOK_STREAMING_ENABLED", "true")
MM_WS_BOOK_MAX_AGE_SECONDS = _env_float("MM_WS_BOOK_MAX_AGE_SECONDS", "15.0")

# Inventory Skew Quoting & Spread Widening
MM_SKEW_SPREAD_ENABLED = _env_bool("MM_SKEW_SPREAD_ENABLED", "true")
MM_SKEW_SPREAD_FACTOR = _env_float("MM_SKEW_SPREAD_FACTOR", "1.0")
MM_SKEW_SPREAD_MAX_MULTIPLIER = _env_float("MM_SKEW_SPREAD_MAX_MULTIPLIER", "3.0")
MM_CROSS_VENUE_SKEW_ENABLED = _env_bool("MM_CROSS_VENUE_SKEW_ENABLED", "true")

# Portfolio-Level Margin & Aggregate Exposure Guard
MM_PORTFOLIO_GUARD_ENABLED = _env_bool("MM_PORTFOLIO_GUARD_ENABLED", "true")
MM_MAX_PORTFOLIO_NOTIONAL_USD = _env_float("MM_MAX_PORTFOLIO_NOTIONAL_USD", "500.0")
MM_MAX_PORTFOLIO_MARGIN_UTILIZATION = _env_float("MM_MAX_PORTFOLIO_MARGIN_UTILIZATION", "0.80")

# Dynamic Market Selection for MM Pilot via LIP Rewards & Volume
MM_DYNAMIC_SELECTION_ENABLED = _env_bool("MM_DYNAMIC_SELECTION_ENABLED", "true")
MM_SELECTION_REFRESH_INTERVAL_SEC = _env_float("MM_SELECTION_REFRESH_INTERVAL_SEC", "1800.0")
MM_MIN_24H_VOLUME = _env_float("MM_MIN_24H_VOLUME", "0.0")
MM_MAX_SPREAD_CENTS = _env_float("MM_MAX_SPREAD_CENTS", "0.0")
MM_VOLUME_WEIGHT = _env_float("MM_VOLUME_WEIGHT", "0.20")


# Kill switch / control plane (spec section 7). Fail closed: a cache older
# than MM_CONTROLS_MAX_STALE_SECONDS means unknown operator intent = off.
MM_CONTROLS_POLL_SECONDS = _env_float("MM_CONTROLS_POLL_SECONDS", "60.0")
MM_CONTROLS_MAX_STALE_SECONDS = _env_float("MM_CONTROLS_MAX_STALE_SECONDS", "300.0")

# Canary phase (spec section 8). Live trading always begins in canary mode.
MM_CANARY_FILLS = _env_int("MM_CANARY_FILLS", "10")
MM_CANARY_QUOTE_SIZE_USD = _env_float("MM_CANARY_QUOTE_SIZE_USD", "2.0")
MM_CANARY_MAX_LOSS_USD = _env_float("MM_CANARY_MAX_LOSS_USD", "10.0")
MM_CANARY_MIN_HOURS = _env_float("MM_CANARY_MIN_HOURS", "24.0")

# Layer 4 — Informed Trading (New)
# #39: Social Sentiment Signals — Twitter/Reddit sentiment vs price
SOCIAL_SENTIMENT_ENABLED = _env_bool("SOCIAL_SENTIMENT_ENABLED", "false")
TWITTER_API_KEY = os.getenv("TWITTER_API_KEY", "")
TWITTER_API_SECRET = os.getenv("TWITTER_API_SECRET", "")
TWITTER_BEARER_TOKEN = os.getenv("TWITTER_BEARER_TOKEN", "")
REDDIT_CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
SOCIAL_SENTIMENT_MIN_DIVERGENCE = _env_float("SOCIAL_SENTIMENT_MIN_DIVERGENCE", "0.15")
SOCIAL_SENTIMENT_MIN_SAMPLE_SIZE = _env_int("SOCIAL_SENTIMENT_MIN_SAMPLE_SIZE", "10")
SOCIAL_SENTIMENT_WEIGHT_TWITTER = _env_float("SOCIAL_SENTIMENT_WEIGHT_TWITTER", "1.5")
SOCIAL_SENTIMENT_WEIGHT_REDDIT = _env_float("SOCIAL_SENTIMENT_WEIGHT_REDDIT", "1.0")
SOCIAL_SENTIMENT_MAX_TRADE_SIZE = _env_float("SOCIAL_SENTIMENT_MAX_TRADE_SIZE", "20.0")

# #40: Expert Individual Forecaster Divergence — superforecaster predictions
EXPERT_DIVERGENCE_ENABLED = _env_bool("EXPERT_DIVERGENCE_ENABLED", "false")
EXPERT_DIVERGENCE_MIN_DIVERGENCE = _env_float("EXPERT_DIVERGENCE_MIN_DIVERGENCE", "0.15")
EXPERT_DIVERGENCE_MIN_FORECASTERS = _env_int("EXPERT_DIVERGENCE_MIN_FORECASTERS", "3")
EXPERT_DIVERGENCE_MIN_ACCURACY = _env_float("EXPERT_DIVERGENCE_MIN_ACCURACY", "0.70")
EXPERT_DIVERGENCE_MAX_TRADE_SIZE = _env_float("EXPERT_DIVERGENCE_MAX_TRADE_SIZE", "25.0")

# #41: Historical Platform Calibration Weighting — bias-adjusted weights
CALIBRATION_WEIGHTING_ENABLED = _env_bool("CALIBRATION_WEIGHTING_ENABLED", "false")
CALIBRATION_LOOKBACK_DAYS = _env_int("CALIBRATION_LOOKBACK_DAYS", "90")
CALIBRATION_MIN_SAMPLES = _env_int("CALIBRATION_MIN_SAMPLES", "50")

# #42: Insider Pattern Detection — unusual order flow before events
INSIDER_PATTERN_ENABLED = _env_bool("INSIDER_PATTERN_ENABLED", "false")
INSIDER_PATTERN_VOLUME_THRESHOLD = _env_float("INSIDER_PATTERN_VOLUME_THRESHOLD", "3.0")
INSIDER_PATTERN_IMBALANCE_THRESHOLD = _env_float("INSIDER_PATTERN_IMBALANCE_THRESHOLD", "0.70")
INSIDER_PATTERN_LOOKBACK_HOURS = _env_float("INSIDER_PATTERN_LOOKBACK_HOURS", "24.0")
INSIDER_PATTERN_MAX_TRADE_SIZE = _env_float("INSIDER_PATTERN_MAX_TRADE_SIZE", "15.0")

# #43: Cross-Category Correlation Signals — BTC price vs prediction markets
CROSS_CATEGORY_ENABLED = _env_bool("CROSS_CATEGORY_ENABLED", "false")
CROSS_CATEGORY_MIN_DIVERGENCE = _env_float("CROSS_CATEGORY_MIN_DIVERGENCE", "0.10")
CROSS_CATEGORY_MAX_TRADE_SIZE = _env_float("CROSS_CATEGORY_MAX_TRADE_SIZE", "20.0")
# External price feeds for correlation tracking
CRYPTO_PRICE_API_URL = os.getenv("CRYPTO_PRICE_API_URL", "https://api.coingecko.com/api/v3")

# Layer 5 — Capital Optimization (New)
# #44: Opportunity Cost Scoring — time-weighted ROI
OPPORTUNITY_COST_SCORING_ENABLED = _env_bool("OPPORTUNITY_COST_SCORING_ENABLED", "false")
OPPORTUNITY_COST_MIN_ANNUALIZED_ROI = _env_float("OPPORTUNITY_COST_MIN_ANNUALIZED_ROI", "0.10")

# #45: Margin Efficiency Optimization — route collateral optimally
MARGIN_EFFICIENCY_ENABLED = _env_bool("MARGIN_EFFICIENCY_ENABLED", "false")
MARGIN_REBALANCE_THRESHOLD = _env_float("MARGIN_REBALANCE_THRESHOLD", "0.20")

# #46: Tax-Aware Position Management — harvest losses, defer gains
TAX_AWARE_ENABLED = _env_bool("TAX_AWARE_ENABLED", "false")
TAX_LOSS_HARVEST_THRESHOLD = _env_float("TAX_LOSS_HARVEST_THRESHOLD", "0.10")
TAX_SHORT_TERM_RATE = _env_float("TAX_SHORT_TERM_RATE", "0.37")
TAX_LONG_TERM_RATE = _env_float("TAX_LONG_TERM_RATE", "0.20")

# #47: Withdrawal Timing Optimization — factor withdrawal delays
WITHDRAWAL_TIMING_ENABLED = _env_bool("WITHDRAWAL_TIMING_ENABLED", "false")
# Platform withdrawal delays in hours (used by treasury.py rebalancing)
PLATFORM_WITHDRAWAL_DELAYS: dict[str, float] = {
    "polymarket": 0.5,     # ~30 min (Polygon L2)
    "kalshi": 24.0,        # 1 business day
    "betfair": 24.0,       # 1 business day
    "smarkets": 24.0,      # 1 business day
    "sxbet": 1.0,          # ~1 hour (Polygon L2)
    "matchbook": 48.0,     # 2 business days
    "gemini": 0.5,         # ~30 min (crypto)
    "ibkr": 72.0,          # 3 business days
}

# Infrastructure (New)
# #48: Redundant Data Feed Arbitrage — parallel feeds, trust fastest
REDUNDANT_FEEDS_ENABLED = _env_bool("REDUNDANT_FEEDS_ENABLED", "false")
REDUNDANT_FEEDS_STALENESS_MS = _env_float("REDUNDANT_FEEDS_STALENESS_MS", "500.0")

# #49: Geographic Latency Optimization — multi-region deployment
GEOGRAPHIC_LATENCY_ENABLED = _env_bool("GEOGRAPHIC_LATENCY_ENABLED", "false")
LATENCY_MONITOR_INTERVAL = _env_float("LATENCY_MONITOR_INTERVAL", "60.0")
DEPLOYMENT_REGION = os.getenv("DEPLOYMENT_REGION", "us-east-1")

# Convergence detection
CONVERGENCE_MIN_DIVERGENCE = _env_float("CONVERGENCE_MIN_DIVERGENCE", "0.05")
CONVERGENCE_MIN_PLATFORMS = _env_int("CONVERGENCE_MIN_PLATFORMS", "3")

# Signal aggregation
SIGNAL_CACHE_TTL = _env_float("SIGNAL_CACHE_TTL", "300.0")

# Notifications
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")  # Slack/Discord/generic URL
WEBHOOK_MIN_PROFIT = _env_float("WEBHOOK_MIN_PROFIT", "0.01")

# Data directory (for EFS mount in Fargate)
DATA_DIR = os.getenv("DATA_DIR", ".")

# Concurrent execution (submit both legs simultaneously for supported platforms)
CONCURRENT_EXECUTION = _env_bool("CONCURRENT_EXECUTION", "true")

# Order time-in-force strategy.
# "fok" = Fill-or-Kill (default): taker orders, immediate fill or cancel.
#   Safest for arb — you know instantly if you got filled.
# "gtc" = Good-Til-Cancelled: maker/limit orders that rest on the book.
#   Saves taker fees (e.g. Kalshi 7%), but risk of partial/no fill.
# "gtc_first_leg" = Use GTC for the first leg only (capture maker pricing),
#   then FOK for the hedge leg (guarantee execution).
ORDER_TIME_IN_FORCE = os.getenv("ORDER_TIME_IN_FORCE", "fok").lower()

# Maximum seconds to wait for a GTC order to fill before cancelling.
# Only applies when ORDER_TIME_IN_FORCE is "gtc" or "gtc_first_leg".
GTC_ORDER_TIMEOUT = _env_float("GTC_ORDER_TIMEOUT", "30.0")

# Balance caching (avoids redundant balance API calls within a scan cycle)
BALANCE_CACHE_TTL = _env_float("BALANCE_CACHE_TTL", "10.0")

# Semantic matching (embedding-based cross-platform market matching)
SEMANTIC_MATCHING_ENABLED = _env_bool("SEMANTIC_MATCHING_ENABLED", "true")
SEMANTIC_MATCH_THRESHOLD = _env_float("SEMANTIC_MATCH_THRESHOLD", "0.70")

# Cross-venue market discovery (LLM equivalence judge — Polymarket x Kalshi).
# Detection/curation ONLY: a Jaccard pre-filter proposes candidate pairs, Claude
# judges true resolution-equivalence, and accepted pairs are written to
# DISCOVERY_CANDIDATES_PATH as status: candidate for HUMAN REVIEW. Discovered
# pairs are never auto-traded — only human-promoted status: verified pairs in
# DISCOVERY_VERIFIED_PATH are consumed downstream. Requires ANTHROPIC_API_KEY
# when enabled. Ported from the pmarb scaffold during the 2026-06 consolidation.
DISCOVERY_ENABLED = _env_bool("DISCOVERY_ENABLED", "false")
DISCOVERY_MODEL = os.getenv("DISCOVERY_MODEL", "claude-haiku-4-5")
DISCOVERY_PREFILTER_THRESHOLD = _env_float("DISCOVERY_PREFILTER_THRESHOLD", "0.10")
DISCOVERY_ACCEPT_CONFIDENCE = _env_float("DISCOVERY_ACCEPT_CONFIDENCE", "0.85")
DISCOVERY_MAX_CANDIDATES = _env_int("DISCOVERY_MAX_CANDIDATES", "200")
DISCOVERY_CACHE_PATH = os.getenv(
    "DISCOVERY_CACHE_PATH", os.path.join(DATA_DIR, "discovery", "discovery_cache.json")
)
DISCOVERY_CANDIDATES_PATH = os.getenv(
    "DISCOVERY_CANDIDATES_PATH", os.path.join(DATA_DIR, "discovery", "candidate_pairs.yaml")
)
DISCOVERY_VERIFIED_PATH = os.getenv(
    "DISCOVERY_VERIFIED_PATH", os.path.join(DATA_DIR, "discovery", "verified_pairs.yaml")
)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# ---------------------------------------------------------------------------
# Jev System One Decision Engine
# ---------------------------------------------------------------------------
JEV_MODEL = os.getenv("TYPESAFE_MODEL", "jev-1.13.0")
JEV_CONFIDENCE_THRESHOLD = _env_float("JEV_CONFIDENCE_THRESHOLD", "0.70")
JEV_CROSS_EQUIVALENCE_ENABLED = _env_bool("JEV_CROSS_EQUIVALENCE_ENABLED", "false")

# Fee model: "expected_value" uses probability-weighted average fees,
# "worst_case" uses max(case1, case2) — more conservative but overfilters.
FEE_MODEL = os.getenv("FEE_MODEL", "expected_value")

# Snapshot recording (historical price data for backtesting)
SNAPSHOT_ENABLED = _env_bool("SNAPSHOT_ENABLED", "false")
SNAPSHOT_INTERVAL = _env_int("SNAPSHOT_INTERVAL", "60")

# Backtesting
BACKTEST_INITIAL_BALANCE = _env_float("BACKTEST_INITIAL_BALANCE", "1000.0")

# Dashboard
# Railway injects PORT; fall back to it when DASHBOARD_PORT is not set.
_dashboard_port_default = os.getenv("PORT", "0")
DASHBOARD_PORT = _env_int("DASHBOARD_PORT", _dashboard_port_default)
DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")  # empty = no auth (loopback only)
# Bind interface for the dashboard HTTP server. Default is loopback so
# accidentally leaving DASHBOARD_PASS empty does not expose an unauth
# server. Production deploys (Railway, Docker) must set both
# DASHBOARD_HOST=0.0.0.0 and DASHBOARD_PASS=<secret>.
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_REFRESH_SECONDS = _env_int("DASHBOARD_REFRESH_SECONDS", "15")

# ---------------------------------------------------------------------------
# Previously hardcoded constants — extracted for tunability
# ---------------------------------------------------------------------------

# Executor: cooldown (seconds) after a failed trade before retrying same opp
FAILED_TRADE_COOLDOWN = _env_float("FAILED_TRADE_COOLDOWN", "300")

# Continuous mode: max concurrent WS-triggered executions (semaphore count)
MAX_CONCURRENT_WS_EXECUTIONS = _env_int("MAX_CONCURRENT_WS_EXECUTIONS", "5")

# Price cache staleness (seconds) — different thresholds per use-case
PRICE_CACHE_EVICTION_AGE = _env_float("PRICE_CACHE_EVICTION_AGE", "60")
WS_CACHE_MAX_AGE_SCAN = _env_float("WS_CACHE_MAX_AGE_SCAN", "30")
WS_CACHE_MAX_AGE_REVALIDATION = _env_float("WS_CACHE_MAX_AGE_REVALIDATION", "15")

# Cross-venue WebSocket Orderbook Streaming for Hedger and Arbitrage Executor
WS_ORDERBOOK_STREAMING_ENABLED = _env_bool("WS_ORDERBOOK_STREAMING_ENABLED", "true")
WS_ORDERBOOK_MAX_AGE_SECONDS = _env_float("WS_ORDERBOOK_MAX_AGE_SECONDS", "15.0")

# WS feed stale detection threshold (seconds without any message)
WS_STALE_FEED_SECONDS = _env_float("WS_STALE_FEED_SECONDS", "120")

# Parallel workers for depth/order book fetches (separate from scan workers)
DEPTH_FETCH_WORKERS = _env_int("DEPTH_FETCH_WORKERS", "8")

# Market title truncation length (for display and DB storage)
MARKET_TITLE_MAX_LEN = _env_int("MARKET_TITLE_MAX_LEN", "60")

# Resolution sniping — how close to settlement a market must be (in hours)
# before _is_near_resolution() flags it as a candidate. Default 48h.
RESOLUTION_SNIPE_WINDOW_HOURS = _env_float("RESOLUTION_SNIPE_WINDOW_HOURS", "48")

# Mirror paper opportunities to Supabase (supabase_sync.OpportunitySync).
OPP_SYNC_ENABLED = _env_bool("OPP_SYNC_ENABLED", "false")

# Paper-trading window tracker (paper_record.py). PAPER_WINDOW_START is an ISO
# UTC timestamp (e.g. "2026-07-21T20:30:00Z"); empty disables the tracker.
# Validated here at import so a malformed timestamp fails loudly instead of
# silently disabling the tracker mid-deploy.
PAPER_WINDOW_START = os.getenv("PAPER_WINDOW_START", "")
PAPER_WINDOW_START_TS = 0.0
if PAPER_WINDOW_START:
    try:
        from datetime import datetime as _pw_dt, timezone as _pw_tz
        _pw_parsed = _pw_dt.fromisoformat(PAPER_WINDOW_START.replace("Z", "+00:00"))
        if _pw_parsed.tzinfo is None:
            # A naive timestamp would be read as deploy-host local time,
            # making the window deployment-dependent; the config contract is
            # UTC, so pin it explicitly.
            _pw_parsed = _pw_parsed.replace(tzinfo=_pw_tz.utc)
        PAPER_WINDOW_START_TS = _pw_parsed.timestamp()
    except ValueError as _pw_exc:
        raise ConfigError(
            f"PAPER_WINDOW_START is not a valid ISO timestamp: {PAPER_WINDOW_START!r}"
        ) from _pw_exc
PAPER_WINDOW_DAYS = max(1, _env_int("PAPER_WINDOW_DAYS", "7"))

# Dashboard query limits
DASHBOARD_RECENT_TRADES_LIMIT = _env_int("DASHBOARD_RECENT_TRADES_LIMIT", "100")
DASHBOARD_PNL_HISTORY_DAYS = _env_int("DASHBOARD_PNL_HISTORY_DAYS", "30")

# Metrics & Alerting
METRICS_ENABLED = _env_bool("METRICS_ENABLED", "true")
ALERT_RATE_LIMIT_SECONDS = _env_float("ALERT_RATE_LIMIT_SECONDS", "300")
ALERT_LOSS_STREAK_THRESHOLD = _env_int("ALERT_LOSS_STREAK_THRESHOLD", "5")
ALERT_BALANCE_LOW_THRESHOLD = _env_float("ALERT_BALANCE_LOW_THRESHOLD", "10.0")

# Credential health checks (HARD-03)
CREDENTIAL_HEALTH_CHECK_INTERVAL = _env_int("CREDENTIAL_HEALTH_CHECK_INTERVAL", "1800")  # 30 min
CREDENTIAL_HEALTH_CHECK_TIMEOUT = _env_int("CREDENTIAL_HEALTH_CHECK_TIMEOUT", "10")  # per-probe
CREDENTIAL_FAILURE_THRESHOLD = _env_int("CREDENTIAL_FAILURE_THRESHOLD", "3")  # consecutive
CREDENTIAL_EXPIRY_WINDOW = _env_int("CREDENTIAL_EXPIRY_WINDOW", "86400")  # 24 hours

# Optional: time-limited token expiry timestamps (Unix time)
BETFAIR_TOKEN_EXPIRY_TIMESTAMP = _env_int("BETFAIR_TOKEN_EXPIRY_TIMESTAMP", "0")
SMARKETS_SESSION_EXPIRY_TIMESTAMP = _env_int("SMARKETS_SESSION_EXPIRY_TIMESTAMP", "0")

# ---------------------------------------------------------------------------
# Dynamic fee reload intervals and backtest scheduling
# ---------------------------------------------------------------------------

# How often (seconds) to re-read fee-related env vars and update globals.
# Allows commission rate changes on Railway to take effect without restart.
FEE_REFRESH_INTERVAL = _env_float("FEE_REFRESH_INTERVAL", "3600")  # 1 hour

# How often (seconds) to run the nightly backtest and write recommendations.
BACKTEST_RUN_INTERVAL = _env_float("BACKTEST_RUN_INTERVAL", "86400")  # 24 hours

# ---------------------------------------------------------------------------
# Backtest-driven threshold tuning (OPTIMIZE-02 follow-up)
# ---------------------------------------------------------------------------
# When BACKTEST_TUNING_ENABLED=true, config reads
# backtest_recommendations.json at import time and overrides MIN_NET_ROI /
# FUZZY_MATCH_THRESHOLD with the recommended values, and populates
# RECOMMENDED_BY_STRATEGY with per-strategy overrides for any strategy that
# had at least 10 backtested trades. Off by default — existing env-var /
# CLI / default precedence is unchanged when this flag is false, even if
# the recommendations file exists on disk.
BACKTEST_TUNING_ENABLED = _env_bool("BACKTEST_TUNING_ENABLED", "false")
BACKTEST_RECOMMENDATIONS_PATH = os.getenv(
    "BACKTEST_RECOMMENDATIONS_PATH",
    os.path.join(DATA_DIR, "backtest_recommendations.json"),
)
# Age sanity gate: recommendations older than this are NOT applied — a tuning
# file from weeks ago reflects a different fee/market regime. Files with a
# missing or unparseable generated_at are treated as stale (cannot verify
# freshness → do not auto-apply).
BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS = _env_float(
    "BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS", "72")
# Populated by apply_backtest_recommendations() when the flag is on.
# Keys are strategy names (e.g. "Binary"); values are dicts with optional
# "MIN_NET_ROI" / "FUZZY_MATCH_THRESHOLD" overrides. Strategies not in this
# dict fall back to the top-level MIN_NET_ROI / FUZZY_MATCH_THRESHOLD.
RECOMMENDED_BY_STRATEGY: dict[str, dict] = {}


def load_backtest_recommendations(path: str | None = None) -> dict | None:
    """Read backtest_recommendations.json and return the parsed dict.

    Defensive: missing file, malformed JSON, or non-object payloads all
    log a warning and return None — this function never raises.
    """
    target = path or BACKTEST_RECOMMENDATIONS_PATH
    if not os.path.exists(target):
        logger.warning(
            "Backtest recommendations file not found at %s — using existing defaults",
            target,
        )
        return None
    try:
        with open(target, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Backtest recommendations file at %s is unreadable (%s) — using existing defaults",
            target, exc,
        )
        return None
    if not isinstance(data, dict):
        logger.warning(
            "Backtest recommendations payload at %s is not a JSON object — using existing defaults",
            target,
        )
        return None
    return data


def _recommendation_age_hours(generated_at: str) -> float | None:
    """Hours elapsed since a recommendation file's generated_at timestamp.

    Accepts the ISO-8601 UTC format written by backtest.build_recommendations
    (naive isoformat + 'Z'). Returns None when the timestamp is missing or
    unparseable — callers treat that as stale.
    """
    if not generated_at or not isinstance(generated_at, str):
        return None
    from datetime import datetime, timezone
    raw = generated_at.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0


def apply_backtest_recommendations(path: str | None = None) -> dict:
    """Load backtest recommendations and surface them as module globals.

    Updates MIN_NET_ROI, FUZZY_MATCH_THRESHOLD, and RECOMMENDED_BY_STRATEGY
    in-place. Defensive — out-of-range or missing values fall back to the
    existing defaults, and recommendations older than
    BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS (or with no verifiable timestamp)
    are skipped entirely. Returns the applied recommendations dict (empty
    when no file, stale file, or no usable values were loaded).
    """
    global MIN_NET_ROI, FUZZY_MATCH_THRESHOLD, RECOMMENDED_BY_STRATEGY
    data = load_backtest_recommendations(path)
    if data is None:
        return {}

    age_hours = _recommendation_age_hours(data.get("generated_at", ""))
    if age_hours is None or age_hours < 0:
        # A negative age means generated_at is in the future (clock skew or a
        # malformed file) — that must not bypass the freshness gate.
        logger.warning(
            "Backtest recommendations at %s have invalid generated_at=%r "
            "(unparseable or in the future) — cannot verify freshness, not applying",
            path or BACKTEST_RECOMMENDATIONS_PATH,
            data.get("generated_at"),
        )
        return {}
    if age_hours > BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS:
        logger.warning(
            "Backtest recommendations are %.1fh old (max %.0fh) — stale tuning "
            "not applied; re-run scripts/tune.py to refresh",
            age_hours, BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS,
        )
        return {}

    applied: dict = {}
    recommended = data.get("recommended")
    if not isinstance(recommended, dict):
        recommended = {}

    candidate_roi = recommended.get("MIN_NET_ROI")
    if isinstance(candidate_roi, (int, float)) and 0.0 <= float(candidate_roi) <= 1.0:
        MIN_NET_ROI = float(candidate_roi)
        applied["MIN_NET_ROI"] = MIN_NET_ROI
    elif candidate_roi is not None:
        logger.warning(
            "Recommended MIN_NET_ROI=%r is out of range; keeping existing default",
            candidate_roi,
        )

    candidate_fuzzy = recommended.get("FUZZY_MATCH_THRESHOLD")
    if isinstance(candidate_fuzzy, (int, float)) and 0 < int(candidate_fuzzy) <= 100:
        FUZZY_MATCH_THRESHOLD = int(candidate_fuzzy)
        applied["FUZZY_MATCH_THRESHOLD"] = FUZZY_MATCH_THRESHOLD
    elif candidate_fuzzy is not None:
        logger.warning(
            "Recommended FUZZY_MATCH_THRESHOLD=%r is out of range; keeping existing default",
            candidate_fuzzy,
        )

    per_strategy = data.get("recommended_by_strategy")
    sanitized: dict[str, dict] = {}
    if isinstance(per_strategy, dict):
        for strat, overrides in per_strategy.items():
            if not isinstance(strat, str) or not isinstance(overrides, dict):
                continue
            entry: dict = {}
            cand = overrides.get("MIN_NET_ROI")
            if isinstance(cand, (int, float)) and 0.0 <= float(cand) <= 1.0:
                entry["MIN_NET_ROI"] = float(cand)
            cand = overrides.get("FUZZY_MATCH_THRESHOLD")
            if isinstance(cand, (int, float)) and 0 < int(cand) <= 100:
                entry["FUZZY_MATCH_THRESHOLD"] = int(cand)
            if entry:
                sanitized[strat] = entry
    elif per_strategy is not None:
        logger.warning(
            "recommended_by_strategy in %s is not a JSON object; ignoring per-strategy overrides",
            path or BACKTEST_RECOMMENDATIONS_PATH,
        )
    RECOMMENDED_BY_STRATEGY = sanitized
    applied["recommended_by_strategy_count"] = len(sanitized)

    logger.warning(
        "Loaded backtest recommendations from %s — applied MIN_NET_ROI=%s, FUZZY_MATCH_THRESHOLD=%s (%d per-strategy overrides)",
        path or BACKTEST_RECOMMENDATIONS_PATH,
        MIN_NET_ROI,
        FUZZY_MATCH_THRESHOLD,
        len(sanitized),
    )
    return applied

# How often (seconds) to emit the weekly platform-rebalance digest.
REBALANCE_DIGEST_INTERVAL = _env_float("REBALANCE_DIGEST_INTERVAL", "604800")  # 7 days


def reload_fee_rates() -> dict[str, tuple[float, float]]:
    """Re-read fee-related env vars and update module globals.

    Returns a dict of {var_name: (old_value, new_value)} for vars that changed.
    IMPORTANT: Only updates fee rate globals. Does NOT touch DRY_RUN,
    EXECUTION_MODE, API keys, or any non-fee configuration.
    """
    global BETFAIR_COMMISSION_RATE, SMARKETS_COMMISSION_RATE, GEMINI_FEE_RATE
    global POLYMARKET_DEFAULT_TAKER_RATE, POLYMARKET_MAKER_FEE_RATE
    global GEMINI_TAKER_RATE, GEMINI_MAKER_RATE
    global KALSHI_MAKER_MULTIPLIER, MATCHBOOK_PREDICTION_COMMISSION
    # (env_var_name, global_var_name, default_str)
    _fee_vars = [
        ("BETFAIR_COMMISSION_RATE", "BETFAIR_COMMISSION_RATE", "0.03"),
        ("SMARKETS_COMMISSION_RATE", "SMARKETS_COMMISSION_RATE", "0.02"),
        ("GEMINI_FEE_RATE", "GEMINI_FEE_RATE", "0.05"),
        ("POLYMARKET_TAKER_FEE_RATE", "POLYMARKET_DEFAULT_TAKER_RATE", "0.04"),
        ("POLYMARKET_MAKER_FEE_RATE", "POLYMARKET_MAKER_FEE_RATE", "0.0"),
        ("GEMINI_TAKER_FEE_RATE", "GEMINI_TAKER_RATE", "0.07"),
        ("GEMINI_MAKER_FEE_RATE", "GEMINI_MAKER_RATE", "0.0175"),
        ("KALSHI_MAKER_FEE_MULTIPLIER", "KALSHI_MAKER_MULTIPLIER", "1.75"),
        ("MATCHBOOK_PREDICTION_COMMISSION", "MATCHBOOK_PREDICTION_COMMISSION", "0.0"),
    ]
    changes: dict[str, tuple[float, float]] = {}
    for env_name, global_name, default in _fee_vars:
        old_val = globals()[global_name]
        new_val = _env_float(env_name, default)
        if abs(new_val - old_val) > 1e-9:
            changes[global_name] = (old_val, new_val)
            globals()[global_name] = new_val
    return changes


# ---------------------------------------------------------------------------
# Validation — called at module load to catch bad configuration early
# ---------------------------------------------------------------------------

_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_VALID_EXECUTION_MODES = {"semi-auto", "full-auto"}
_VALID_FEE_MODELS = {"expected_value", "worst_case"}
_VALID_GEMINI_ORDER_TYPES = {"ioc", "gtc"}
_VALID_CANARY_MODES = {"", "paper", "live"}
_CANARY_LIVE_ACK_TOKEN = "I_ACCEPT_LIVE_CANARY"


def validate_config() -> list[str]:
    """Validate all configuration values and return a list of warnings.

    Raises:
        ConfigError: If a value is invalid and cannot be safely ignored.

    Returns:
        List of non-fatal warning messages (logged but not raised).
    """
    global LIVE_ENVELOPE, MM_MAX_GROSS_PER_MARKET_USD, MM_MAX_INVENTORY_USD, MM_CANARY_MAX_LOSS_USD
    warnings: list[str] = []

    # --- Enum checks ---
    if EXECUTION_MODE not in _VALID_EXECUTION_MODES:
        raise ConfigError(
            f"EXECUTION_MODE={EXECUTION_MODE!r} is not valid "
            f"(expected one of {_VALID_EXECUTION_MODES})"
        )

    if FEE_MODEL not in _VALID_FEE_MODELS:
        raise ConfigError(
            f"FEE_MODEL={FEE_MODEL!r} is not valid "
            f"(expected one of {_VALID_FEE_MODELS})"
        )

    if GEMINI_ORDER_TYPE not in _VALID_GEMINI_ORDER_TYPES:
        raise ConfigError(
            f"GEMINI_ORDER_TYPE={GEMINI_ORDER_TYPE!r} is not valid "
            f"(expected one of {_VALID_GEMINI_ORDER_TYPES})"
        )

    if LOG_LEVEL not in _VALID_LOG_LEVELS:
        warnings.append(
            f"LOG_LEVEL={LOG_LEVEL!r} is not a standard level "
            f"({_VALID_LOG_LEVELS}); defaulting to INFO"
        )

    # --- Positive-value checks ---
    _positive = {
        "BASE_TRADE_SIZE": BASE_TRADE_SIZE,
        "MAX_TRADE_SIZE": MAX_TRADE_SIZE,
        "DAILY_LOSS_LIMIT": DAILY_LOSS_LIMIT,
        "FILL_POLL_INTERVAL": FILL_POLL_INTERVAL,
        "FILL_POLL_TIMEOUT": FILL_POLL_TIMEOUT,
        "PARALLEL_WORKERS": PARALLEL_WORKERS,
        "HEDGE_MAX_ATTEMPTS": HEDGE_MAX_ATTEMPTS,
        "RESCAN_INTERVAL": RESCAN_INTERVAL,
        "BACKTEST_INITIAL_BALANCE": BACKTEST_INITIAL_BALANCE,
        "GAS_PRICE_CACHE_TTL": GAS_PRICE_CACHE_TTL,
        "IBKR_ORDER_RATE_LIMIT": IBKR_ORDER_RATE_LIMIT,
        "SNAPSHOT_INTERVAL": SNAPSHOT_INTERVAL,
        "BALANCE_CACHE_TTL": BALANCE_CACHE_TTL,
        "ALERT_RATE_LIMIT_SECONDS": ALERT_RATE_LIMIT_SECONDS,
        "ALERT_LOSS_STREAK_THRESHOLD": ALERT_LOSS_STREAK_THRESHOLD,
        "STALE_PRICE_THRESHOLD": STALE_PRICE_THRESHOLD,
        "BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS": BACKTEST_RECOMMENDATIONS_MAX_AGE_HOURS,
        "KALSHI_VIP_POLL_INTERVAL": KALSHI_VIP_POLL_INTERVAL,
        "KALSHI_AUTH_BOOT_ATTEMPTS": KALSHI_AUTH_BOOT_ATTEMPTS,
        "KALSHI_REAUTH_INTERVAL": KALSHI_REAUTH_INTERVAL,
        "LIP_MAX_MARKETS": LIP_MAX_MARKETS,
        "LIP_SELECT_INTERVAL": LIP_SELECT_INTERVAL,
        "LIP_DEPTH_PROBE_LIMIT": LIP_DEPTH_PROBE_LIMIT,
        # Plan 10 — Kalshi MM pilot keys
        "MM_FILL_POLL_SECONDS": MM_FILL_POLL_SECONDS,
        "MM_HEDGE_MAX_LATENCY_SECONDS": MM_HEDGE_MAX_LATENCY_SECONDS,
        "MM_HALT_WINDOW_SECONDS": MM_HALT_WINDOW_SECONDS,
        "MM_MAX_INVENTORY_USD": MM_MAX_INVENTORY_USD,
        "MM_MAX_INVENTORY_CONTRACTS": MM_MAX_INVENTORY_CONTRACTS,
        "MM_MAX_TOTAL_INVENTORY_USD": MM_MAX_TOTAL_INVENTORY_USD,
        "MM_MAX_GROSS_PER_MARKET_USD": MM_MAX_GROSS_PER_MARKET_USD,
        "MM_QUOTE_SIZE_USD": MM_QUOTE_SIZE_USD,
        "MM_PILOT_BANKROLL_USD": MM_PILOT_BANKROLL_USD,
        "MM_BOOK_MAX_STALE_SECONDS": MM_BOOK_MAX_STALE_SECONDS,
        "MM_VOL_PULL_MULTIPLIER": MM_VOL_PULL_MULTIPLIER,
        "MM_MAX_BOOK_DEPTH_FRACTION": MM_MAX_BOOK_DEPTH_FRACTION,
        "MM_CONTROLS_POLL_SECONDS": MM_CONTROLS_POLL_SECONDS,
        "MM_CONTROLS_MAX_STALE_SECONDS": MM_CONTROLS_MAX_STALE_SECONDS,
        "MM_CANARY_FILLS": MM_CANARY_FILLS,
        "MM_CANARY_QUOTE_SIZE_USD": MM_CANARY_QUOTE_SIZE_USD,
        "MM_CANARY_MAX_LOSS_USD": MM_CANARY_MAX_LOSS_USD,
        "MM_CANARY_MIN_HOURS": MM_CANARY_MIN_HOURS,
        "MM_WS_BOOK_MAX_AGE_SECONDS": MM_WS_BOOK_MAX_AGE_SECONDS,
        "WS_ORDERBOOK_MAX_AGE_SECONDS": WS_ORDERBOOK_MAX_AGE_SECONDS,
        "CTF_MAX_TRADE_SIZE": CTF_MAX_TRADE_SIZE,
        "INVENTORY_MAX_DELTA_CONTRACTS": INVENTORY_MAX_DELTA_CONTRACTS,
        "INVENTORY_REBALANCE_MAX_COST": INVENTORY_REBALANCE_MAX_COST,
        "MM_MAX_PORTFOLIO_NOTIONAL_USD": MM_MAX_PORTFOLIO_NOTIONAL_USD,
    }
    for name, val in _positive.items():
        if not math.isfinite(val) or val <= 0:
            raise ConfigError(f"{name}={val} must be > 0")

    if not (0 < INVENTORY_MAX_IMBALANCE_RATIO <= 1):
        raise ConfigError(
            f"INVENTORY_MAX_IMBALANCE_RATIO={INVENTORY_MAX_IMBALANCE_RATIO} must be in (0, 1]")

    if INVENTORY_REBALANCE_COOLDOWN_SEC < 0:
        raise ConfigError(
            f"INVENTORY_REBALANCE_COOLDOWN_SEC={INVENTORY_REBALANCE_COOLDOWN_SEC} must be >= 0")

    if not (0 < INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO <= 1):
        raise ConfigError(
            f"INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO={INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO} must be in (0, 1]")

    if not (0 < MM_MAX_PORTFOLIO_MARGIN_UTILIZATION <= 1):
        raise ConfigError(
            f"MM_MAX_PORTFOLIO_MARGIN_UTILIZATION={MM_MAX_PORTFOLIO_MARGIN_UTILIZATION} must be in (0, 1]")

    if MM_SELECTION_REFRESH_INTERVAL_SEC < 10.0:
        raise ConfigError(
            f"MM_SELECTION_REFRESH_INTERVAL_SEC={MM_SELECTION_REFRESH_INTERVAL_SEC} must be >= 10.0")

    if MM_MIN_24H_VOLUME < 0:
        raise ConfigError(
            f"MM_MIN_24H_VOLUME={MM_MIN_24H_VOLUME} must be >= 0")

    if MM_MAX_SPREAD_CENTS < 0:
        raise ConfigError(
            f"MM_MAX_SPREAD_CENTS={MM_MAX_SPREAD_CENTS} must be >= 0")

    if MM_VOLUME_WEIGHT < 0:
        raise ConfigError(
            f"MM_VOLUME_WEIGHT={MM_VOLUME_WEIGHT} must be >= 0")



    # Plan 10 non-negative keys (zero is a valid value for these)
    if MM_INVENTORY_TARGET_USD < 0:
        raise ConfigError(
            f"MM_INVENTORY_TARGET_USD={MM_INVENTORY_TARGET_USD} must be >= 0")
    if MM_HEDGE_DEADBAND_USD < 0:
        raise ConfigError(
            f"MM_HEDGE_DEADBAND_USD={MM_HEDGE_DEADBAND_USD} must be >= 0")
    if not (0 < LIP_PRICE_BAND_LOW < LIP_PRICE_BAND_HIGH < 1):
        raise ConfigError(
            f"LIP_PRICE_BAND_HIGH ({LIP_PRICE_BAND_HIGH}) must be > "
            f"LIP_PRICE_BAND_LOW ({LIP_PRICE_BAND_LOW}), with both in (0, 1)")
    if not (0 < MM_MAX_BOOK_DEPTH_FRACTION <= 1):
        raise ConfigError(
            f"MM_MAX_BOOK_DEPTH_FRACTION={MM_MAX_BOOK_DEPTH_FRACTION} "
            f"must be in (0, 1]")
    if not (0 < MM_LIP_BALANCER_MAX_SHARE <= 1):
        raise ConfigError(
            f"MM_LIP_BALANCER_MAX_SHARE={MM_LIP_BALANCER_MAX_SHARE} "
            f"must be in (0, 1]")
    if not (0 <= MM_LIP_BALANCER_MIN_EFFICIENCY <= 1):
        raise ConfigError(
            f"MM_LIP_BALANCER_MIN_EFFICIENCY={MM_LIP_BALANCER_MIN_EFFICIENCY} "
            f"must be in [0, 1]")
    if MM_SKEW_SPREAD_FACTOR < 0:
        raise ConfigError(
            f"MM_SKEW_SPREAD_FACTOR={MM_SKEW_SPREAD_FACTOR} must be >= 0")
    if MM_SKEW_SPREAD_MAX_MULTIPLIER < 1.0:
        raise ConfigError(
            f"MM_SKEW_SPREAD_MAX_MULTIPLIER={MM_SKEW_SPREAD_MAX_MULTIPLIER} must be >= 1.0")

    # --- Non-negative checks ---
    _non_negative = {
        "MIN_LIQUIDITY": MIN_LIQUIDITY,
        "MIN_NET_ROI": MIN_NET_ROI,
        "MIN_PROFIT_AMOUNT": MIN_PROFIT_AMOUNT,
        "DEFAULT_MIN_PROFIT": DEFAULT_MIN_PROFIT,
        "PM_RATE_LIMIT": PM_RATE_LIMIT,
        "KALSHI_RATE_LIMIT": KALSHI_RATE_LIMIT,
        "KALSHI_AUTH_BOOT_RETRY_WAIT": KALSHI_AUTH_BOOT_RETRY_WAIT,
        "POLYGON_GAS_ESTIMATE": POLYGON_GAS_ESTIMATE,
        "WEBHOOK_MIN_PROFIT": WEBHOOK_MIN_PROFIT,
        "ALERT_BALANCE_LOW_THRESHOLD": ALERT_BALANCE_LOW_THRESHOLD,
        "LIP_MIN_POOL": LIP_MIN_POOL,
        "LIP_MIN_HOURS_REMAINING": LIP_MIN_HOURS_REMAINING,
    }
    for name, val in _non_negative.items():
        if val < 0:
            raise ConfigError(f"{name}={val} must be >= 0")

    # --- Range checks ---
    if not (0 <= SIZING_AGGRESSIVENESS <= 1):
        raise ConfigError(
            f"SIZING_AGGRESSIVENESS={SIZING_AGGRESSIVENESS} must be in [0, 1]"
        )
    if not (0 < KELLY_FRACTION <= 1):
        raise ConfigError(
            f"KELLY_FRACTION={KELLY_FRACTION} must be in (0, 1]"
        )
    if not (0 < KELLY_MAX_FRACTION <= 1):
        raise ConfigError(
            f"KELLY_MAX_FRACTION={KELLY_MAX_FRACTION} must be in (0, 1]"
        )
    if not (0 <= BETFAIR_COMMISSION_RATE < 1):
        raise ConfigError(
            f"BETFAIR_COMMISSION_RATE={BETFAIR_COMMISSION_RATE} must be in [0, 1)"
        )
    if not (0 <= SMARKETS_COMMISSION_RATE < 1):
        raise ConfigError(
            f"SMARKETS_COMMISSION_RATE={SMARKETS_COMMISSION_RATE} must be in [0, 1)"
        )
    if not (0 <= GEMINI_FEE_RATE < 1):
        raise ConfigError(
            f"GEMINI_FEE_RATE={GEMINI_FEE_RATE} must be in [0, 1)"
        )
    if not (0 <= DASHBOARD_PORT <= 65535):
        raise ConfigError(
            f"DASHBOARD_PORT={DASHBOARD_PORT} must be in [0, 65535]"
        )
    if not (0 < FUZZY_MATCH_THRESHOLD <= 100):
        raise ConfigError(
            f"FUZZY_MATCH_THRESHOLD={FUZZY_MATCH_THRESHOLD} must be in (0, 100]"
        )
    if not (0 <= EVENT_DIVERGENCE_THRESHOLD <= 1):
        raise ConfigError(
            f"EVENT_DIVERGENCE_THRESHOLD={EVENT_DIVERGENCE_THRESHOLD} "
            f"must be in [0, 1]"
        )
    if not (0 <= HEDGE_MAX_SPREAD_LOSS_PCT <= 1):
        raise ConfigError(
            f"HEDGE_MAX_SPREAD_LOSS_PCT={HEDGE_MAX_SPREAD_LOSS_PCT} "
            f"must be in [0, 1]"
        )
    if not (0 <= REENTRY_IMPROVEMENT_THRESHOLD <= 1):
        raise ConfigError(
            f"REENTRY_IMPROVEMENT_THRESHOLD={REENTRY_IMPROVEMENT_THRESHOLD} "
            f"must be in [0, 1]"
        )
    if not (0 <= MIN_ENTRY_PRICE < 1):
        raise ConfigError(
            f"MIN_ENTRY_PRICE={MIN_ENTRY_PRICE} must be in [0, 1)"
        )
    if MIN_EXIT_BID_DEPTH < 0:
        raise ConfigError(
            f"MIN_EXIT_BID_DEPTH={MIN_EXIT_BID_DEPTH} must be >= 0"
        )
    if not (0 < SEMANTIC_MATCH_THRESHOLD <= 1):
        raise ConfigError(
            f"SEMANTIC_MATCH_THRESHOLD={SEMANTIC_MATCH_THRESHOLD} "
            f"must be in (0, 1]"
        )
    if not (0 < STALE_PRICE_MOVE_PCT < 1):
        raise ConfigError(
            f"STALE_PRICE_MOVE_PCT={STALE_PRICE_MOVE_PCT} "
            f"must be in (0, 1)"
        )
    # --- Relationship checks ---
    if BASE_TRADE_SIZE > MAX_TRADE_SIZE:
        raise ConfigError(
            f"BASE_TRADE_SIZE ({BASE_TRADE_SIZE}) must be <= "
            f"MAX_TRADE_SIZE ({MAX_TRADE_SIZE})"
        )

    if FILL_POLL_TIMEOUT < FILL_POLL_INTERVAL:
        warnings.append(
            f"FILL_POLL_TIMEOUT ({FILL_POLL_TIMEOUT}) < "
            f"FILL_POLL_INTERVAL ({FILL_POLL_INTERVAL}); "
            f"polls may never complete"
        )

    # --- Dashboard checks ---
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    if DASHBOARD_PORT > 0 and not DASHBOARD_PASS:
        if DASHBOARD_HOST not in _LOOPBACK_HOSTS:
            raise ConfigError(
                f"DASHBOARD_HOST={DASHBOARD_HOST!r} binds to a non-loopback "
                f"interface but DASHBOARD_PASS is empty. Either set "
                f"DASHBOARD_PASS to a strong secret or set DASHBOARD_HOST "
                f"to 127.0.0.1 / localhost / ::1 for loopback-only access."
            )
        warnings.append(
            "DASHBOARD_PORT is set but DASHBOARD_PASS is empty — "
            "dashboard is loopback-only with no authentication"
        )

    # --- Platform whitelist validation ---
    if not ENABLED_EXECUTION_PLATFORMS:
        warnings.append(
            "ENABLED_EXECUTION_PLATFORMS is empty — no platforms will execute trades"
        )
    unknown = ENABLED_EXECUTION_PLATFORMS - _VALID_PLATFORMS
    if unknown:
        raise ConfigError(
            f"ENABLED_EXECUTION_PLATFORMS contains unknown platforms: "
            f"{', '.join(sorted(unknown))}. "
            f"Valid: {', '.join(sorted(_VALID_PLATFORMS))}"
        )

    if "polymarket" in ENABLED_EXECUTION_PLATFORMS and not DRY_RUN:
        raise ConfigError(
            "Polymarket is public-data/shadow-only and cannot be included in "
            "ENABLED_EXECUTION_PLATFORMS when DRY_RUN=false. Remove polymarket "
            "from the execution whitelist."
        )

    # SX Bet quarantine — place_order() sends unsigned JSON and is rejected by
    # the API. Detection-only (DRY_RUN=true) is fine; live trading is not.
    # Real fix is implementing EIP-712 signing — separate future PR.
    if "sxbet" in ENABLED_EXECUTION_PLATFORMS and not DRY_RUN:
        raise ConfigError(
            "SX Bet is in ENABLED_EXECUTION_PLATFORMS with DRY_RUN=false, but "
            "SX Bet trading is not yet implemented (place_order() sends "
            "unsigned JSON; orders are rejected by the API). Either set "
            "DRY_RUN=true for detection-only, or remove sxbet from "
            "ENABLED_EXECUTION_PLATFORMS."
        )

    # Limitless reward farming validation
    if LIMITLESS_REWARDS_ENABLED and not DRY_RUN:
        if not LIMITLESS_API_KEY:
            raise ConfigError(
                "LIMITLESS_REWARDS_ENABLED=true and DRY_RUN=false requires LIMITLESS_API_KEY"
            )
        if not LIMITLESS_PRIVATE_KEY:
            raise ConfigError(
                "LIMITLESS_REWARDS_ENABLED=true and DRY_RUN=false requires LIMITLESS_PRIVATE_KEY"
            )
        if not _is_valid_eth_address(LIMITLESS_EXCHANGE_CONTRACT):
            raise ConfigError(
                f"LIMITLESS_REWARDS_ENABLED=true and DRY_RUN=false requires valid non-zero "
                f"LIMITLESS_EXCHANGE_CONTRACT address, got {LIMITLESS_EXCHANGE_CONTRACT!r}"
            )
        if "limitless" not in ENABLED_EXECUTION_PLATFORMS:
            raise ConfigError(
                "LIMITLESS_REWARDS_ENABLED=true and DRY_RUN=false requires 'limitless' in "
                "ENABLED_EXECUTION_PLATFORMS. Add limitless to the execution whitelist."
            )

    # Plan 04: CTF validation — require valid non-zero 20-byte addresses if CTF features are enabled
    if CTF_ENABLED or CTF_MERGE_ENABLED or CTF_MINT_SELL_ENABLED or CTF_CONVERT_ENABLED:
        if not _is_valid_eth_address(CONDITIONAL_TOKENS_ADDRESS):
            raise ConfigError(
                f"CONDITIONAL_TOKENS_ADDRESS must be a valid non-zero 20-byte hexadecimal address, got {CONDITIONAL_TOKENS_ADDRESS!r}"
            )
        if not _is_valid_eth_address(COLLATERAL_TOKEN_ADDRESS):
            raise ConfigError(
                f"COLLATERAL_TOKEN_ADDRESS must be a valid non-zero 20-byte hexadecimal address, got {COLLATERAL_TOKEN_ADDRESS!r}"
            )
        if CTF_CONVERT_ENABLED and not _is_valid_eth_address(NEG_RISK_ADAPTER_ADDRESS):
            raise ConfigError(
                f"NEG_RISK_ADAPTER_ADDRESS must be a valid non-zero 20-byte hexadecimal address when CTF_CONVERT_ENABLED=true, got {NEG_RISK_ADAPTER_ADDRESS!r}"
            )
    if CTF_MAX_RESOLUTION_DAYS < 0:
        raise ConfigError(
            f"CTF_MAX_RESOLUTION_DAYS={CTF_MAX_RESOLUTION_DAYS} must be >= 0"
        )

    # --- Strategy-specific validation (Phase 8) ---

    # STRAT-01: Order Book Imbalance
    if IMBALANCE_ENABLED and IMBALANCE_RATIO <= 1.0:
        raise ConfigError(
            f"IMBALANCE_RATIO={IMBALANCE_RATIO} must be > 1.0"
        )

    # STRAT-02: News-Driven Sniping
    if NEWS_SNIPE_ENABLED and not FINNHUB_API_KEY:
        warnings.append(
            "News sniping enabled (NEWS_SNIPE_ENABLED=true) but "
            "FINNHUB_API_KEY not set; disabling news sniping"
        )

    if NEWS_SNIPE_ENABLED and not (0 < NEWS_SNIPE_CONFIDENCE_THRESHOLD <= 1):
        raise ConfigError(
            f"NEWS_SNIPE_CONFIDENCE_THRESHOLD={NEWS_SNIPE_CONFIDENCE_THRESHOLD} "
            f"must be in (0, 1]"
        )

    # Firecrawl news source (alternative/supplemental to Finnhub for STRAT-02)
    if FIRECRAWL_NEWS_ENABLED and not FIRECRAWL_API_KEY:
        warnings.append(
            "Firecrawl news enabled (FIRECRAWL_NEWS_ENABLED=true) but "
            "FIRECRAWL_API_KEY not set; disabling Firecrawl news"
        )

    # STRAT-06: Correlated Market Pairs
    if CORRELATED_ENABLED and CORRELATED_PAIRS == "[]":
        warnings.append(
            "Correlated pairs enabled (CORRELATED_ENABLED=true) but no pairs "
            "configured (CORRELATED_PAIRS='[]'); disabling correlated pairs"
        )

    if CORRELATED_ENABLED and not (0 < CORRELATION_DIVERGENCE_THRESHOLD < 1):
        raise ConfigError(
            f"CORRELATION_DIVERGENCE_THRESHOLD={CORRELATION_DIVERGENCE_THRESHOLD} "
            f"must be in (0, 1)"
        )

    # STRAT-07: Time Decay Convergence
    if TIME_DECAY_ENABLED and not (0 < TIME_DECAY_MIN_CONSENSUS <= 1):
        raise ConfigError(
            f"TIME_DECAY_MIN_CONSENSUS={TIME_DECAY_MIN_CONSENSUS} "
            f"must be in (0, 1]"
        )

    if TIME_DECAY_ENABLED and not (0 < TIME_DECAY_BUY_BELOW_PRICE <= 1):
        raise ConfigError(
            f"TIME_DECAY_BUY_BELOW_PRICE={TIME_DECAY_BUY_BELOW_PRICE} "
            f"must be in (0, 1]"
        )

    # --- Contradiction warnings ---
    if EXECUTION_MODE == "full-auto" and DRY_RUN:
        warnings.append(
            "EXECUTION_MODE=full-auto but DRY_RUN=true — "
            "no trades will be executed"
        )

    if CANARY_MODE and CANARY_MODE not in _VALID_CANARY_MODES:
        raise ConfigError(
            f"CANARY_MODE={CANARY_MODE!r} is not valid "
            f"(expected one of {{'paper', 'live'}} or empty)"
        )

    if CANARY_MODE == "live" and CANARY_LIVE_ACK != _CANARY_LIVE_ACK_TOKEN:
        raise ConfigError(
            "CANARY_MODE=live requires "
            f"CANARY_LIVE_ACK={_CANARY_LIVE_ACK_TOKEN!r} "
            "(refusing accidental live canary)"
        )

    if CANARY_MODE == "live" and CANARY_MAX_TRADE_SIZE > 5.0:
        raise ConfigError(
            f"CANARY_MAX_TRADE_SIZE={CANARY_MAX_TRADE_SIZE} exceeds hard cap $5.00"
        )

    if CANARY_MODE == "live" and CANARY_MAX_TRADES > 3:
        raise ConfigError(
            f"CANARY_MAX_TRADES={CANARY_MAX_TRADES} exceeds hard cap 3"
        )

    # --- Live envelope: DRY_RUN=false requires an operator five-item file ---
    if not DRY_RUN:
        from live_envelope import EnvelopeError, require_live_envelope
        try:
            envelope = require_live_envelope()
        except EnvelopeError as exc:
            raise ConfigError(str(exc)) from exc
        if envelope["venue"] != "kalshi-d0":
            raise ConfigError(
                "This scanner live path is Kalshi D0 only; envelope venue is "
                f"{envelope['venue']!r}. Use the matching launcher for other venues."
            )
        LIVE_ENVELOPE = envelope
        MM_MAX_GROSS_PER_MARKET_USD = min(
            MM_MAX_GROSS_PER_MARKET_USD, envelope["max_notional_usd"]
        )
        MM_MAX_INVENTORY_USD = min(MM_MAX_INVENTORY_USD, envelope["max_notional_usd"])
        MM_CANARY_MAX_LOSS_USD = min(
            MM_CANARY_MAX_LOSS_USD, envelope["max_daily_loss_usd"]
        )

    # --- Kalshi MM pilot invariants (plan 10, spec sections 4-6) ---
    if MM_KALSHI_PILOT_ENABLED:
        # Forced preconditions: hedging and both hot-path gates are NOT
        # optional for the pilot. Refuse a live (non-dry-run) start outright.
        if not DRY_RUN:
            missing = [
                name for name, enabled in (
                    ("MM_AUTO_HEDGE_ENABLED", MM_AUTO_HEDGE_ENABLED),
                    ("MM_TOXIC_FLOW_ENABLED", MM_TOXIC_FLOW_ENABLED),
                    ("MM_VOLATILITY_ADJUSTED_ENABLED", MM_VOLATILITY_ADJUSTED_ENABLED),
                ) if not enabled
            ]
            if missing:
                raise ConfigError(
                    "MM_KALSHI_PILOT_ENABLED=true with DRY_RUN=false requires "
                    f"{', '.join(missing)} — the pilot refuses to start live "
                    "without auto-hedge and the toxic-flow/volatility gates."
                )
        if "kalshi" not in ENABLED_EXECUTION_PLATFORMS:
            raise ConfigError(
                "MM_KALSHI_PILOT_ENABLED=true but 'kalshi' is not in "
                "ENABLED_EXECUTION_PLATFORMS — the pilot is Kalshi-only."
            )
        if not (MM_MAX_INVENTORY_USD <= MM_MAX_GROSS_PER_MARKET_USD <= 300.0):
            warnings.append(
                f"MM pilot cap sanity: expected MM_MAX_INVENTORY_USD "
                f"({MM_MAX_INVENTORY_USD}) <= MM_MAX_GROSS_PER_MARKET_USD "
                f"({MM_MAX_GROSS_PER_MARKET_USD}) <= 300 (pre-registered "
                f"per-market ceiling)"
            )
        if MM_MAX_TOTAL_INVENTORY_USD > 0.15 * MM_PILOT_BANKROLL_USD:
            warnings.append(
                f"MM pilot cap sanity: MM_MAX_TOTAL_INVENTORY_USD "
                f"({MM_MAX_TOTAL_INVENTORY_USD}) > 15% of pilot bankroll "
                f"(${MM_PILOT_BANKROLL_USD})"
            )

    # --- Startup summary for Phase 8 strategies ---
    strategy_status = []
    strategy_status.append(
        f"Imbalance: {'ENABLED' if IMBALANCE_ENABLED else 'DISABLED'}"
        + (f" (ratio {IMBALANCE_RATIO}, max ${IMBALANCE_MAX_TRADE_SIZE})" if IMBALANCE_ENABLED else "")
    )
    strategy_status.append(
        f"NewsSnipe: {'ENABLED' if NEWS_SNIPE_ENABLED else 'DISABLED'}"
        + (f" (confidence {NEWS_SNIPE_CONFIDENCE_THRESHOLD}, max ${NEWS_SNIPE_MAX_TRADE_SIZE})" if NEWS_SNIPE_ENABLED else "")
    )
    # Count correlated pairs if enabled
    if CORRELATED_ENABLED and CORRELATED_PAIRS != "[]":
        import json
        try:
            pairs = json.loads(CORRELATED_PAIRS)
            num_pairs = len(pairs) if isinstance(pairs, list) else 0
            strategy_status.append(f"Correlated: ENABLED ({num_pairs} pairs, threshold {CORRELATION_DIVERGENCE_THRESHOLD})")
        except (json.JSONDecodeError, TypeError):
            strategy_status.append("Correlated: ENABLED (invalid pairs format)")
    else:
        strategy_status.append("Correlated: DISABLED")

    strategy_status.append(
        f"TimeDecay: {'ENABLED' if TIME_DECAY_ENABLED else 'DISABLED'}"
        + (f" ({TIME_DECAY_MIN_HOURS_EXPIRY}h, {int(TIME_DECAY_MIN_CONSENSUS*100)}% consensus)" if TIME_DECAY_ENABLED else "")
    )

    if any(IMBALANCE_ENABLED or NEWS_SNIPE_ENABLED or CORRELATED_ENABLED or TIME_DECAY_ENABLED for _ in [1]):
        logger.info("Phase 8 Market Signal Strategies: %s", " | ".join(strategy_status))

    return warnings


# Run validation at import time; log warnings but don't suppress them
_config_warnings = validate_config()

# Apply backtest recommendations after validate_config so env-var values
# are validated first. When the flag is off the loader never runs and the
# existing env-var / default precedence is preserved verbatim.
# BACKTEST_RECOMMENDATIONS_APPLIED is unconditionally defined so callers
# (and the evidence harness) can introspect whether tuning was applied:
# `False` when the flag is off or apply failed, the returned dict when
# the apply pass ran (may be empty if the file was missing/invalid).
BACKTEST_RECOMMENDATIONS_APPLIED: dict | bool = False
if BACKTEST_TUNING_ENABLED:
    try:
        BACKTEST_RECOMMENDATIONS_APPLIED = apply_backtest_recommendations()
    except Exception as _tune_exc:  # never raise at import
        logger.warning(
            "apply_backtest_recommendations() failed (%s) — using existing defaults",
            _tune_exc,
        )
        BACKTEST_RECOMMENDATIONS_APPLIED = False
