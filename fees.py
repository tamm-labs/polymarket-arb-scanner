"""Fee calculators for Polymarket and Kalshi."""

import logging
import math
import os

from config import (
    CTF_GAS_ESTIMATE,
    FEE_MODEL,
    KALSHI_FEE_CAP_CENTS,
    POLYGON_GAS_ESTIMATE,
    POLYMARKET_DEFAULT_TAKER_RATE,
    GEMINI_TAKER_RATE,
    GEMINI_MAKER_RATE,
    KALSHI_MAKER_MULTIPLIER,
)

logger = logging.getLogger(__name__)


def _select_fees(case_a_fees: float, case_b_fees: float, price_a: float) -> float:
    """Select fee estimate based on the configured FEE_MODEL.

    Args:
        case_a_fees: Fees when side A wins.
        case_b_fees: Fees when side B wins.
        price_a: Buy price on side A, used as probability proxy for EV model.

    Returns:
        Fee estimate (worst-case or expected-value depending on FEE_MODEL).
    """
    if FEE_MODEL == "expected_value":
        # Use price as proxy for probability of that side winning
        prob_a = max(0.0, min(1.0, price_a))
        return prob_a * case_a_fees + (1.0 - prob_a) * case_b_fees
    return max(case_a_fees, case_b_fees)


# Polymarket taker fee rates by market category, verified against
# docs.polymarket.com on 2026-06-10. Geopolitics is fee-free; the rest use
# rate * C * P * (1 - P). Categories not listed fall back to
# POLYMARKET_DEFAULT_TAKER_RATE.
POLYMARKET_CATEGORY_TAKER_RATES = {
    "crypto": 0.07,
    "sports": 0.03,
    "finance": 0.04,
    "politics": 0.04,
    "mentions": 0.04,
    "tech": 0.04,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "geopolitics": 0.0,
}


def polymarket_rate_for_category(category: str | None) -> float:
    """Resolve the Polymarket taker fee rate for a market category.

    Matches case-insensitively against POLYMARKET_CATEGORY_TAKER_RATES;
    unknown or missing categories use POLYMARKET_DEFAULT_TAKER_RATE.
    """
    if category:
        rate = POLYMARKET_CATEGORY_TAKER_RATES.get(category.strip().lower())
        if rate is not None:
            return rate
    return POLYMARKET_DEFAULT_TAKER_RATE


def polymarket_taker_fee(price: float, contracts: int = 1,
                         fee_rate: float | None = None,
                         category: str | None = None) -> float:
    """Polymarket dynamic taker fee (March 2026 model).

    Formula: fee_rate * C * P * (1 - P).
    Makers pay 0%. Rate is category-dependent (see
    POLYMARKET_CATEGORY_TAKER_RATES); geopolitics is fee-free.
    Fee is charged at trade entry, not at settlement.

    Args:
        price: Trade price in [0, 1].
        contracts: Number of contracts (default 1).
        fee_rate: Explicit rate override; wins over category.
        category: Market category for rate lookup; falls back to
            POLYMARKET_DEFAULT_TAKER_RATE when unknown.

    Returns:
        Total fee in dollars.
    """
    if price <= 0 or price >= 1:
        return 0.0
    rate = fee_rate if fee_rate is not None else polymarket_rate_for_category(category)
    return rate * contracts * price * (1.0 - price)


# Polymarket maker rebate program (docs.polymarket.com, verified 2026-06-10):
# makers receive back a share of the taker fees their resting orders generate,
# paid daily in USDC — 25% of the taker fee in most categories, 20% in crypto.
# Geopolitics has a 0% taker fee, so its rebate is also 0.
POLYMARKET_MAKER_REBATE_SHARE = {
    "crypto": 0.20,
}
POLYMARKET_DEFAULT_MAKER_REBATE_SHARE = 0.25


def polymarket_maker_rebate(price: float, contracts: int = 1,
                            category: str | None = None) -> float:
    """Expected maker-rebate income when a resting order fills.

    The rebate is a share of the taker fee the counterparty pays against the
    maker's resting order: rebate_share * taker_rate * C * P * (1 - P).
    This is income (positive), to be netted against MM costs in yield models —
    it stacks with liquidity rewards, which pay for resting presence whether
    or not the order fills.

    Args:
        price: Fill price in [0, 1].
        contracts: Number of contracts filled.
        category: Market category (drives both the taker rate and the
            rebate share; crypto rebates at 20%, everything else at 25%).

    Returns:
        Rebate income in dollars (0.0 for fee-free categories).
    """
    taker = polymarket_taker_fee(price, contracts, category=category)
    if taker <= 0:
        return 0.0
    share = POLYMARKET_MAKER_REBATE_SHARE.get(
        (category or "").strip().lower(), POLYMARKET_DEFAULT_MAKER_REBATE_SHARE)
    return share * taker


def polymarket_fee(buy_price: float, sell_price: float = 1.0) -> float:
    """DEPRECATED: Use polymarket_taker_fee() instead.

    Legacy settlement-fee model (2% on net winnings). Kept as alias for
    backward compatibility with cross-platform helpers that still use it.
    New code should call polymarket_taker_fee() directly.
    """
    if sell_price <= buy_price:
        return 0.0
    net_winnings = sell_price - buy_price
    return 0.02 * net_winnings


# Kalshi series with non-standard fees, verified against the official
# schedule (kalshi.com/fee-schedule, effective Feb 5, 2026) on 2026-06-10:
# - Most markets: taker fee only (0.07 coefficient); makers pay $0.
# - ~93 flagged series additionally charge makers the 0.0175 coefficient.
# - S&P 500 (INX*) and Nasdaq-100 (NASDAQ100*) series use a HALVED taker
#   coefficient (0.035).
# Both lists are env-overridable; the maker list ships with the known
# flagged examples and should be refreshed from the fee-schedule page.
KALSHI_MAKER_FEE_SERIES = tuple(
    p.strip().upper()
    for p in os.getenv(
        "KALSHI_MAKER_FEE_SERIES",
        "KXAAAGASM,KXATPMATCH,KXCPI,KXCPIYOY,KXBALLONDOR,KXCONNSMYTHE,KXBTCMAX150",
    ).split(",")
    if p.strip()
)
KALSHI_HALF_TAKER_SERIES = tuple(
    p.strip().upper()
    for p in os.getenv("KALSHI_HALF_TAKER_SERIES", "INX,NASDAQ100").split(",")
    if p.strip()
)


def _kalshi_series_matches(ticker: str | None, prefixes: tuple[str, ...]) -> bool:
    if not ticker:
        return False
    t = ticker.upper()
    return any(t.startswith(p) for p in prefixes)


def kalshi_taker_fee(price: float, contracts: int = 1,
                     ticker: str | None = None) -> float:
    """Calculate Kalshi taker fee.

    Verified 2026-06-10 against the official schedule (effective Feb 5,
    2026): fees = roundup(0.07 * C * P * (1 - P)), rounded up to the next
    cent ONCE PER ORDER — not per contract. The previous per-contract
    ceil + 2-cent floor overstated the real fee by up to 6x at price
    extremes (e.g. P=0.05, C=100: real $0.33 vs charged $2.00),
    suppressing legitimate long-tail detections.

    S&P 500 / Nasdaq-100 series (KALSHI_HALF_TAKER_SERIES) use a halved
    coefficient (0.035). Capped at KALSHI_FEE_CAP_CENTS per contract.

    Returns total fee in dollars.
    """
    if price <= 0 or price >= 1:
        return 0.0
    rate = 0.035 if _kalshi_series_matches(ticker, KALSHI_HALF_TAKER_SERIES) else 0.07
    # Round up to the next cent on the order total, per the schedule.
    # Epsilon guards against float artifacts (175.0000000003 -> 176).
    fee_cents = math.ceil(rate * contracts * price * (1 - price) * 100 - 1e-9)
    # Per-contract cap retained as a safety bound (cannot bind in practice).
    fee_cents = min(fee_cents, KALSHI_FEE_CAP_CENTS * contracts)
    return fee_cents / 100.0


def kalshi_maker_fee(price: float, contracts: int = 1,
                     ticker: str | None = None) -> float:
    """Kalshi maker fee. Returns total in dollars.

    Verified 2026-06-10 against the official schedule (effective Feb 5,
    2026): makers pay $0 on most markets. The
    ceil(KALSHI_MAKER_MULTIPLIER * P * (1 - P)) formula applies ONLY to the
    series explicitly flagged on kalshi.com/fee-schedule
    (KALSHI_MAKER_FEE_SERIES). Unknown/unflagged tickers — including calls
    that pass no ticker — pay $0, matching the schedule's default.

    Args:
        price: Trade price in [0, 1].
        contracts: Number of contracts (default 1).
        ticker: Market ticker; flagged series are charged the maker formula.

    Returns:
        Total fee in dollars.
    """
    if price <= 0 or price >= 1:
        return 0.0
    if not _kalshi_series_matches(ticker, KALSHI_MAKER_FEE_SERIES):
        return 0.0
    # Round up to the next cent on the order total, per the schedule.
    fee_cents = math.ceil(KALSHI_MAKER_MULTIPLIER * contracts * price * (1 - price) - 1e-9)
    fee_cents = min(fee_cents, KALSHI_FEE_CAP_CENTS * contracts)
    return fee_cents / 100.0


def net_profit_binary_internal(yes_price: float, no_price: float,
                               category: str | None = None) -> dict:
    """Calculate net profit for a Polymarket binary arbitrage.

    Buy YES + NO. One always pays $1.00.
    Profit = $1.00 - (yes_price + no_price) - fees.

    March 2026 model: fee is charged at entry (rate * P * (1-P)), not on winnings.
    Both legs pay the taker entry fee. The rate is category-dependent
    (geopolitics 0% ... crypto 7%); pass the Gamma market category.
    """
    total_cost = yes_price + no_price
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    # Entry fees: both YES and NO legs pay taker fee at trade time
    fee = (polymarket_taker_fee(yes_price, category=category)
           + polymarket_taker_fee(no_price, category=category))

    # Gas cost: two Polygon transactions (buy YES + buy NO)
    gas = POLYGON_GAS_ESTIMATE * 2

    return {
        "gross_spread": gross_spread,
        "fees": fee + gas,
        "net_profit": gross_spread - fee - gas,
    }


def net_profit_negrisk_internal(yes_prices: list[float],
                                category: str | None = None) -> dict:
    """Calculate net profit for a NegRisk (multi-outcome) arbitrage.

    Buy one YES share of every outcome. Exactly one pays $1.00.
    Profit = $1.00 - sum(prices) - fees.

    March 2026 model: all legs pay taker entry fee at trade time. The rate
    is category-dependent (geopolitics 0% ... crypto 7%).
    """
    total_cost = sum(yes_prices)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    # Entry fees: each outcome leg pays taker fee at trade time
    fee = sum(polymarket_taker_fee(p, category=category) for p in yes_prices)

    # Gas cost: one Polygon transaction per outcome
    gas = POLYGON_GAS_ESTIMATE * len(yes_prices)

    return {
        "gross_spread": gross_spread,
        "fees": fee + gas,
        "net_profit": gross_spread - fee - gas,
    }


def net_profit_negrisk_no_side(no_prices: list[float]) -> dict:
    """Calculate net profit for a NegRisk NO-side arbitrage.

    Buy one NO share of every outcome in a mutually-exclusive N-outcome event.
    Exactly one outcome resolves YES, so exactly (N-1) of the NO shares pay $1.00.
    Guaranteed payout = (N - 1). Profit = (N - 1) - sum(no_prices) - fees.

    March 2026 model: every leg pays the Polymarket taker entry fee at trade time.
    """
    n = len(no_prices)
    if n < 2:
        return {"gross_spread": 0.0, "fees": 0, "net_profit": 0.0}

    total_cost = sum(no_prices)
    gross_spread = float(n - 1) - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    # Entry fees: each NO leg pays taker fee at trade time
    fee = sum(polymarket_taker_fee(p) for p in no_prices)

    # Gas cost: one Polygon transaction per outcome
    gas = POLYGON_GAS_ESTIMATE * n

    return {
        "gross_spread": gross_spread,
        "fees": fee + gas,
        "net_profit": gross_spread - fee - gas,
    }


def net_profit_kalshi_binary(yes_price: float, no_price: float) -> dict:
    """Calculate net profit for a Kalshi binary arbitrage.

    Buy YES + NO on the same market. One always pays $1.00.
    Both legs pay Kalshi taker fee at entry.
    """
    total_cost = yes_price + no_price
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    fees = kalshi_taker_fee(yes_price) + kalshi_taker_fee(no_price)
    return {
        "gross_spread": gross_spread,
        "fees": fees,
        "net_profit": gross_spread - fees,
    }


def net_profit_kalshi_multi(yes_prices: list[float]) -> dict:
    """Calculate net profit for a Kalshi multi-outcome arbitrage.

    Buy YES on each outcome in an event. Exactly one pays $1.00.
    Each leg pays Kalshi taker fee at entry.
    """
    total_cost = sum(yes_prices)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    fees = sum(kalshi_taker_fee(p) for p in yes_prices)
    return {
        "gross_spread": gross_spread,
        "fees": fees,
        "net_profit": gross_spread - fees,
    }


def net_profit_cross_platform(
    poly_price: float,
    kalshi_price: float,
    poly_side: str,
    kalshi_side: str,
) -> dict:
    """Calculate net profit for cross-platform arbitrage.

    poly_side/kalshi_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    """
    poly_cost = poly_price
    kalshi_cost = kalshi_price
    total_cost = poly_cost + kalshi_cost

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Both legs pay entry fees at trade time (March 2026 model)
    pm_entry_fee = polymarket_taker_fee(poly_price)
    kalshi_entry_fee = kalshi_taker_fee(kalshi_price, 1)

    # Both fees are charged at entry regardless of outcome — no case distinction needed
    fees = pm_entry_fee + kalshi_entry_fee
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# Betfair fee calculations
# ---------------------------------------------------------------------------

def betfair_commission(net_winnings: float, commission_rate: float = 0.05) -> float:
    """Betfair charges 2-5% commission on net winnings per market.

    The rate depends on the user's discount rate (based on activity).
    Default 5% is the standard rate for new/low-volume users.
    """
    if net_winnings <= 0:
        return 0.0
    return net_winnings * commission_rate


def net_profit_cross_betfair(
    poly_price: float,
    bf_price: float,
    poly_side: str,
    bf_side: str,
    commission_rate: float = 0.05,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs Betfair.

    poly_side/bf_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    """
    total_cost = poly_price + bf_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Polymarket leg pays entry fee at trade time (March 2026 model)
    pm_entry_fee = polymarket_taker_fee(poly_price)

    # Case 1 (Poly wins): PM entry fee + no Betfair commission (BF side lost)
    case1_fees = pm_entry_fee

    # Case 2 (Betfair wins): PM entry fee + Betfair commission on net winnings
    bf_win_profit = 1.0 - bf_price
    case2_fees = pm_entry_fee + betfair_commission(bf_win_profit, commission_rate)

    # Fee estimate + Polygon gas for the PM leg
    fees = _select_fees(case1_fees, case2_fees, poly_price)
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# Spread (intra-platform) fee calculations
# ---------------------------------------------------------------------------

def net_profit_spread_polymarket(ask: float, bid: float) -> dict:
    """Calculate net profit for a Polymarket spread capture (buy at ask, sell at bid).

    Round-trip on same token — no CLOB fee, only Polygon gas for 2 txns.
    """
    if bid <= ask:
        return {"gross_spread": bid - ask, "fees": 0, "net_profit": bid - ask}

    gross = bid - ask
    gas = POLYGON_GAS_ESTIMATE * 2
    return {
        "gross_spread": gross,
        "fees": gas,
        "net_profit": gross - gas,
    }


# ---------------------------------------------------------------------------
# Betfair standalone fee calculations
# ---------------------------------------------------------------------------

def net_profit_betfair_backall(implied_probs: list[float], commission_rate: float = 0.05) -> dict:
    """Calculate net profit for a Betfair back-all arbitrage.

    Back all runners. Sum of implied probabilities < 1.0 means under-round book.
    Exactly one runner wins; commission on net winnings.
    """
    total_cost = sum(implied_probs)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    # Winner pays out $1. Commission on net winnings (1 - cost of winning bet).
    cheapest = min(implied_probs)
    net_winnings = 1.0 - cheapest
    fee = betfair_commission(net_winnings, commission_rate)

    return {
        "gross_spread": gross_spread,
        "fees": fee,
        "net_profit": gross_spread - fee,
    }


def net_profit_betfair_backlay(back_price: float, lay_price: float, commission_rate: float = 0.05) -> dict:
    """Calculate net profit for a Betfair back-lay arbitrage on same runner.

    Back at back_price, lay at lay_price. Profit when back < lay (crossed book).
    back_price and lay_price are in implied probability (0-1) terms.
    """
    if lay_price <= back_price:
        return {"gross_spread": 0, "fees": 0, "net_profit": 0}

    # Gross profit from the spread
    gross = lay_price - back_price

    # Commission applies to net market profit
    fee = betfair_commission(gross, commission_rate)

    return {
        "gross_spread": gross,
        "fees": fee,
        "net_profit": gross - fee,
    }


# ---------------------------------------------------------------------------
# Smarkets fee calculations
# ---------------------------------------------------------------------------

def smarkets_commission(net_winnings: float, commission_rate: float = 0.02) -> float:
    """Smarkets charges 2% commission on net winnings.

    The rate is fixed at 2% for most users (lower than Betfair's 5% default).
    """
    if net_winnings <= 0:
        return 0.0
    return net_winnings * commission_rate


def net_profit_smarkets_backall(implied_probs: list[float], commission_rate: float = 0.02) -> dict:
    """Calculate net profit for a Smarkets back-all arbitrage.

    Back all runners. Sum of implied probabilities < 1.0 means under-round book.
    Exactly one runner wins; commission on net winnings.
    """
    total_cost = sum(implied_probs)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    # Winner pays out $1. Commission on net winnings (1 - cost of winning bet).
    cheapest = min(implied_probs)
    net_winnings = 1.0 - cheapest
    fee = smarkets_commission(net_winnings, commission_rate)

    return {
        "gross_spread": gross_spread,
        "fees": fee,
        "net_profit": gross_spread - fee,
    }


def net_profit_smarkets_backlay(back_price: float, lay_price: float, commission_rate: float = 0.02) -> dict:
    """Calculate net profit for a Smarkets back-lay arbitrage on same runner.

    Back at back_price, lay at lay_price. Profit when back < lay (crossed book).
    back_price and lay_price are in implied probability (0-1) terms.
    """
    if lay_price <= back_price:
        return {"gross_spread": 0, "fees": 0, "net_profit": 0}

    gross = lay_price - back_price
    fee = smarkets_commission(gross, commission_rate)

    return {
        "gross_spread": gross,
        "fees": fee,
        "net_profit": gross - fee,
    }


def net_profit_cross_smarkets(
    poly_price: float,
    sm_price: float,
    poly_side: str,
    sm_side: str,
    commission_rate: float = 0.02,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs Smarkets.

    poly_side/sm_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    """
    total_cost = poly_price + sm_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Polymarket leg pays entry fee at trade time (March 2026 model)
    pm_entry_fee = polymarket_taker_fee(poly_price)

    # Case 1 (Poly wins): PM entry fee + no Smarkets commission (SM side lost)
    case1_fees = pm_entry_fee

    # Case 2 (Smarkets wins): PM entry fee + Smarkets commission on net winnings
    sm_win_profit = 1.0 - sm_price
    case2_fees = pm_entry_fee + smarkets_commission(sm_win_profit, commission_rate)

    # Fee estimate + Polygon gas for the PM leg
    fees = _select_fees(case1_fees, case2_fees, poly_price)
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# SX Bet fee calculations
# ---------------------------------------------------------------------------

def net_profit_sxbet_backall(implied_probs: list[float]) -> dict:
    """Calculate net profit for SX Bet back-all arbitrage.

    SX Bet has 0% commission on API trades -- no commission on winnings.
    """
    total_cost = sum(implied_probs)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    return {
        "gross_spread": gross_spread,
        "fees": 0,
        "net_profit": gross_spread,
    }


def net_profit_sxbet_backlay(back_price: float, lay_price: float) -> dict:
    """Calculate net profit for SX Bet back-lay arbitrage. 0% fees."""
    if lay_price <= back_price:
        return {"gross_spread": 0, "fees": 0, "net_profit": 0}

    gross = lay_price - back_price
    return {
        "gross_spread": gross,
        "fees": 0,
        "net_profit": gross,
    }


# ---------------------------------------------------------------------------
# Matchbook fee calculations (0% commission on prediction markets)
# ---------------------------------------------------------------------------

def net_profit_matchbook_backall(implied_probs: list[float]) -> dict:
    """Calculate net profit for Matchbook back-all arbitrage.

    Matchbook has 0% commission on prediction markets — pure overround arb.
    """
    total_cost = sum(implied_probs)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    return {
        "gross_spread": gross_spread,
        "fees": 0,
        "net_profit": gross_spread,
    }


def net_profit_matchbook_backlay(back_price: float, lay_price: float) -> dict:
    """Calculate net profit for Matchbook back-lay arbitrage. 0% commission."""
    if lay_price <= back_price:
        return {"gross_spread": 0, "fees": 0, "net_profit": 0}

    gross = lay_price - back_price
    return {
        "gross_spread": gross,
        "fees": 0,
        "net_profit": gross,
    }


def net_profit_cross_matchbook(
    poly_price: float,
    mb_price: float,
    poly_side: str,
    mb_side: str,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs Matchbook.

    poly_side/mb_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    Matchbook has 0% commission.
    """
    total_cost = poly_price + mb_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Polymarket leg pays entry fee at trade time (March 2026 model); Matchbook 0% commission
    fees = polymarket_taker_fee(poly_price)
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# Gemini Predictions fee calculations
# Fee formula (CFTC 40.6 filing, eff. 2026-03-09): roundup(rate * C * P * (1-P)) per contract
# Rates: 7% taker (GEMINI_TAKER_RATE), 1.75% maker (GEMINI_MAKER_RATE)
# ---------------------------------------------------------------------------

def gemini_fee(price: float, fee_rate: float | None = None, contracts: int = 1) -> float:
    """Calculate Gemini fee for a single contract.

    Formula (March 18, 2026): fee_rate * C * P * (1 - P). Rounded up to next cent.
    Default taker rate 0.07 (7%).
    Old formula was min(P, 1-P) * fee_rate — replaced by P*(1-P)*rate.

    Args:
        price: Trade price in [0, 1].
        fee_rate: Override fee rate; uses GEMINI_TAKER_RATE if None.
        contracts: Number of contracts (default 1).

    Returns:
        Total fee in dollars, rounded up to next cent.
    """
    if price <= 0 or price >= 1:
        return 0.0
    rate = fee_rate if fee_rate is not None else GEMINI_TAKER_RATE
    raw = rate * contracts * price * (1.0 - price)
    return math.ceil(raw * 100) / 100


def net_profit_gemini_binary(yes_price: float, no_price: float,
                             fee_rate: float | None = None) -> dict:
    """Calculate net profit for a Gemini binary arbitrage.

    Buy YES + NO. One always pays $1.00.
    Each leg pays Gemini fee at entry using the 2026 formula: rate * P * (1-P).
    """
    total_cost = yes_price + no_price
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    fees = gemini_fee(yes_price, fee_rate) + gemini_fee(no_price, fee_rate)

    return {
        "gross_spread": gross_spread,
        "fees": fees,
        "net_profit": gross_spread - fees,
    }


def net_profit_gemini_multi(yes_prices: list[float], fee_rate: float | None = None) -> dict:
    """Calculate net profit for a Gemini categorical (multi-outcome) arbitrage.

    Buy YES on each outcome. Exactly one pays $1.00.
    Each leg pays Gemini fee at entry using the 2026 formula: rate * P * (1-P).
    """
    total_cost = sum(yes_prices)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    fees = sum(gemini_fee(p, fee_rate) for p in yes_prices)

    return {
        "gross_spread": gross_spread,
        "fees": fees,
        "net_profit": gross_spread - fees,
    }


def net_profit_cross_gemini(
    poly_price: float,
    gm_price: float,
    poly_side: str,
    gm_side: str,
    fee_rate: float | None = None,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs Gemini.

    poly_side/gm_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    Both legs pay entry-time fees using the 2026 formula.
    """
    total_cost = poly_price + gm_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Both legs pay entry fees at trade time (March 2026 model)
    pm_entry_fee = polymarket_taker_fee(poly_price)
    gm_entry_fee = gemini_fee(gm_price, fee_rate)

    # Both fees are charged at entry regardless of outcome
    fees = pm_entry_fee + gm_entry_fee
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# IBKR ForecastEx fee calculations ($0.00 commission)
# ---------------------------------------------------------------------------

def net_profit_ibkr_binary(yes_price: float, no_price: float) -> dict:
    """Calculate net profit for an IBKR ForecastEx binary arbitrage.

    BUY YES + BUY NO (both are buy orders). One pays $1, other $0.
    IBKR has $0.00 commission on ForecastEx.
    """
    total_cost = yes_price + no_price
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {"gross_spread": gross_spread, "fees": 0, "net_profit": gross_spread}

    return {
        "gross_spread": gross_spread,
        "fees": 0,
        "net_profit": gross_spread,
    }


def net_profit_cross_ibkr(
    poly_price: float,
    ibkr_price: float,
    poly_side: str,
    ibkr_side: str,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs IBKR.

    poly_side/ibkr_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    IBKR has $0.00 commission.
    """
    total_cost = poly_price + ibkr_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Polymarket leg pays entry fee at trade time; IBKR has $0.00 commission
    fees = polymarket_taker_fee(poly_price)
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


def net_profit_triangular(
    yes_price: float,
    no_price: float,
    yes_platform: str,
    no_platform: str,
) -> dict:
    """Calculate net profit for a triangular (3+ platform) cross-platform arbitrage.

    Buys YES on yes_platform and NO on no_platform. One position always wins $1.00.
    Fee calculation picks the worst-case fee scenario for each platform's fee model.

    Args:
        yes_price: Price of the YES contract on the best-YES platform.
        no_price: Price of the NO contract on the best-NO platform.
        yes_platform: Name of the platform where YES is bought.
        no_platform: Name of the platform where NO is bought.

    Returns:
        Dict with gross_spread, fees, and net_profit.
    """
    total_cost = yes_price + no_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Compute worst-case fees for the YES-side platform
    yes_fee = _platform_win_fee(yes_price, yes_platform)
    # Compute worst-case fees for the NO-side platform
    no_fee = _platform_win_fee(no_price, no_platform)

    # Entry fees (paid regardless of outcome)
    yes_entry = _platform_entry_fee(yes_price, yes_platform)
    no_entry = _platform_entry_fee(no_price, no_platform)
    entry_fees = yes_entry + no_entry

    # If YES wins: YES-side win fee + NO-side entry fee
    case_yes_wins = yes_fee + entry_fees
    # If NO wins: NO-side win fee + YES-side entry fee
    case_no_wins = no_fee + entry_fees

    fees = _select_fees(case_yes_wins, case_no_wins, yes_price)

    # Gas: Polygon gas for any Polymarket leg
    gas = 0.0
    if yes_platform == "polymarket" or no_platform == "polymarket":
        gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


def _platform_win_fee(price: float, platform: str) -> float:
    """Calculate the winner fee for a platform (fee charged when position pays out $1).

    March 2026: Polymarket no longer charges on winnings — fee is at entry.
    """
    if platform == "polymarket":
        # Polymarket switched to entry-time fee in March 2026; no settlement fee
        return 0.0
    elif platform == "kalshi":
        # Kalshi taker fee is an entry fee, not a win fee
        return 0.0
    elif platform == "betfair":
        return betfair_commission(1.0 - price)
    elif platform == "smarkets":
        return smarkets_commission(1.0 - price)
    # sxbet, matchbook, gemini (entry fee), ibkr: no win fee
    return 0.0


def _platform_entry_fee(price: float, platform: str) -> float:
    """Calculate the entry fee for a platform (fee charged when placing the trade)."""
    if platform == "polymarket":
        return polymarket_taker_fee(price)
    elif platform == "kalshi":
        return kalshi_taker_fee(price)
    elif platform == "gemini":
        return gemini_fee(price)
    # betfair, smarkets: charge on winnings (win fee), not at entry
    # sxbet, matchbook, ibkr: 0% fees
    return 0.0


def net_profit_cross_generic(
    price_a: float,
    price_b: float,
    side_a: str,
    side_b: str,
    platform_a: str = "",
    platform_b: str = "",
) -> dict:
    """Calculate net profit for a cross-platform arbitrage between any two platforms.

    Uses _platform_win_fee and _platform_entry_fee to compute worst-case fees
    for the given platform pair. Includes Polygon gas when either platform is
    Polymarket.

    Args:
        price_a: Price on platform A.
        price_b: Price on platform B.
        side_a: 'yes' or 'no' — what we're buying on platform A.
        side_b: 'yes' or 'no' — what we're buying on platform B.
        platform_a: Name of the first platform.
        platform_b: Name of the second platform.

    Returns:
        Dict with gross_spread, fees, and net_profit.
    """
    total_cost = price_a + price_b

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Entry fees (paid regardless of outcome)
    entry_a = _platform_entry_fee(price_a, platform_a)
    entry_b = _platform_entry_fee(price_b, platform_b)
    entry_fees = entry_a + entry_b

    # Case 1 (A wins): A's win fee + B's entry fee + A's entry fee
    case1_fees = _platform_win_fee(price_a, platform_a) + entry_fees
    # Case 2 (B wins): B's win fee + A's entry fee + B's entry fee
    case2_fees = _platform_win_fee(price_b, platform_b) + entry_fees

    fees = _select_fees(case1_fees, case2_fees, price_a)

    # Polygon gas when one side is Polymarket
    gas = 0.0
    if platform_a == "polymarket" or platform_b == "polymarket":
        gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


def net_profit_cross_sxbet(
    poly_price: float,
    sx_price: float,
    poly_side: str,
    sx_side: str,
) -> dict:
    """Calculate net profit for cross-platform arbitrage: Polymarket vs SX Bet.

    poly_side/sx_side: 'yes' or 'no' -- what we're buying on each platform.
    One of the two positions will win $1.00, the other $0.00.
    """
    total_cost = poly_price + sx_price

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Polymarket leg pays entry fee at trade time; SX Bet has 0% fees
    fees = polymarket_taker_fee(poly_price)
    gas = POLYGON_GAS_ESTIMATE

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# Multi-outcome cross-platform fee calculation
# ---------------------------------------------------------------------------

def net_profit_multi_cross(
    outcome_prices: list[float],
    outcome_platforms: list[str],
) -> dict:
    """Calculate net profit for a multi-outcome cross-platform arbitrage.

    Buys YES on each outcome on potentially different platforms.  Exactly one
    outcome wins and pays $1.  The arb is profitable when the total cost of
    buying YES on every outcome (across the cheapest platforms) is less than
    $1 minus all fees.

    Fees are computed per-outcome:
    - Entry fees are always charged (platform-specific).
    - Win fee is charged only on the outcome that settles YES.  Since we
      don't know which one wins, we take the worst-case (highest) win fee
      across all outcomes.

    Gas: one POLYGON_GAS_ESTIMATE is charged if any leg is on Polymarket.

    Args:
        outcome_prices: YES price for each outcome (one per outcome).
        outcome_platforms: Platform name for each outcome (parallel list).

    Returns:
        Dict with gross_spread, fees, and net_profit.
    """
    total_cost = sum(outcome_prices)

    if total_cost >= 1.0:
        return {"gross_spread": 1.0 - total_cost, "fees": 0, "net_profit": 1.0 - total_cost}

    gross_spread = 1.0 - total_cost

    # Entry fees: always paid on every leg regardless of outcome
    entry_fees = sum(
        _platform_entry_fee(p, plat)
        for p, plat in zip(outcome_prices, outcome_platforms)
    )

    # Win fee: depends on which outcome wins — take worst case
    win_fees_per_outcome = [
        _platform_win_fee(p, plat)
        for p, plat in zip(outcome_prices, outcome_platforms)
    ]
    worst_win_fee = max(win_fees_per_outcome) if win_fees_per_outcome else 0.0

    fees = entry_fees + worst_win_fee

    # Gas: one charge if any leg touches Polymarket
    gas = POLYGON_GAS_ESTIMATE if "polymarket" in outcome_platforms else 0.0

    return {
        "gross_spread": gross_spread,
        "fees": fees + gas,
        "net_profit": gross_spread - fees - gas,
    }


# ---------------------------------------------------------------------------
# Dynamic fee routing — pick lowest-fee path for cross-platform opportunities
# ---------------------------------------------------------------------------

# Real-time fee schedule per platform (can be updated at runtime for promos)
# Real-time fee schedule per platform (2026 rates; can be updated at runtime for promos).
# taker/maker values are fee RATES used by estimate_total_fee() for routing decisions.
# Actual fee amounts use the dedicated fee functions (polymarket_taker_fee, etc.).
PLATFORM_FEE_SCHEDULE: dict[str, dict[str, float]] = {
    "polymarket": {"taker": 0.04, "maker": 0.00, "gas": POLYGON_GAS_ESTIMATE},
    "kalshi": {"taker": 0.07, "maker": 0.0175, "gas": 0.0},
    "betfair": {"taker": 0.05, "maker": 0.05, "gas": 0.0},
    "smarkets": {"taker": 0.02, "maker": 0.02, "gas": 0.0},
    "sxbet": {"taker": 0.00, "maker": 0.00, "gas": 0.0},
    "matchbook": {"taker": 0.00, "maker": 0.00, "gas": 0.0},
    "gemini": {"taker": 0.07, "maker": 0.0175, "gas": 0.0},
    "ibkr": {"taker": 0.00, "maker": 0.00, "gas": 0.0},
}


def estimate_total_fee(platform: str, price: float, order_type: str = "taker") -> float:
    """Estimate total fee for a trade on a platform.

    Args:
        platform: Platform name.
        price: Trade price (0-1).
        order_type: "taker" or "maker".

    Returns:
        Estimated fee in dollars per contract.
    """
    schedule = PLATFORM_FEE_SCHEDULE.get(platform, {})
    fee_rate = schedule.get(order_type, 0.0)
    gas = schedule.get("gas", 0.0)

    if platform == "polymarket":
        # March 2026: entry fee only (no settlement fee)
        if order_type == "taker":
            return polymarket_taker_fee(price) + gas
        else:
            return gas  # Maker pays 0%
    elif platform == "kalshi":
        if order_type == "taker":
            return kalshi_taker_fee(price)
        else:
            return kalshi_maker_fee(price)
    elif platform == "gemini":
        if order_type == "taker":
            return gemini_fee(price, GEMINI_TAKER_RATE) + gas
        else:
            return gemini_fee(price, GEMINI_MAKER_RATE) + gas
    elif platform in ("betfair", "smarkets"):
        # Commission on winnings
        return (1.0 - price) * fee_rate
    else:
        return gas


def find_lowest_fee_path(
    platforms: list[str],
    yes_prices: dict[str, float],
    no_prices: dict[str, float],
) -> dict | None:
    """Find the lowest-fee cross-platform path for an arb opportunity.

    Given YES and NO prices across multiple platforms, finds the pair
    (buy YES on platform A, buy NO on platform B) that minimizes total fees.

    Args:
        platforms: List of platform names with prices.
        yes_prices: {platform: yes_ask_price} for each platform.
        no_prices: {platform: no_ask_price} for each platform.

    Returns:
        Dict with best_yes_platform, best_no_platform, total_cost,
        estimated_fees, net_profit, or None if no profitable path exists.
    """
    best = None

    for yes_plat in platforms:
        yes_p = yes_prices.get(yes_plat)
        if yes_p is None or yes_p <= 0 or yes_p >= 1:
            continue
        for no_plat in platforms:
            if no_plat == yes_plat:
                continue
            no_p = no_prices.get(no_plat)
            if no_p is None or no_p <= 0 or no_p >= 1:
                continue

            total_cost = yes_p + no_p
            if total_cost >= 1.0:
                continue

            # Estimate fees for this path
            yes_fee = estimate_total_fee(yes_plat, yes_p)
            no_fee = estimate_total_fee(no_plat, no_p)
            total_fees = yes_fee + no_fee
            net_profit = 1.0 - total_cost - total_fees

            if net_profit <= 0:
                continue

            if best is None or net_profit > best["net_profit"]:
                best = {
                    "best_yes_platform": yes_plat,
                    "best_no_platform": no_plat,
                    "yes_price": yes_p,
                    "no_price": no_p,
                    "total_cost": total_cost,
                    "estimated_fees": total_fees,
                    "net_profit": net_profit,
                }

    return best


def net_profit_rewards(bid_price: float, ask_price: float, size: float = 1.0,
                       platform: str = "polymarket") -> dict:
    """Calculate net profit for reward resting orders.

    For rewards strategy, profit comes from two sources:
    1. Spread capture (bid/ask spread on fills)
    2. Reward payout (tracked separately in database)

    This function calculates the spread profit per fill. Actual reward yield
    is tracked separately via RewardTracker/KalshiRewardTracker in database.

    Args:
        bid_price: Resting bid price (0-1).
        ask_price: Resting ask price (0-1).
        size: Order size in dollars.
        platform: "polymarket" or "kalshi".

    Returns:
        Dict with net_profit, spread, fees, net_roi, bid, ask keys.
    """
    mid = (bid_price + ask_price) / 2
    spread = ask_price - bid_price

    # Platform fees for resting limit orders (makers)
    if platform == "polymarket":
        # Polymarket maker fee: 0% for most cases, but conservative estimate of 0.5%
        # in case of fee structures we're not aware of
        fee_rate = 0.005
    elif platform == "kalshi":
        # Kalshi maker fee: lower than taker; use conservative 0.5% estimate
        # Actual: ceil(KALSHI_MAKER_MULTIPLIER * P * (1 - P)) in cents
        fee_rate = 0.005
    else:
        # Default conservative fee rate
        fee_rate = 0.01

    fees = spread * size * fee_rate
    net_profit = spread * size - fees
    net_roi = (net_profit / size) * 100 if size > 0 else 0.0

    return {
        "net_profit": net_profit,
        "spread": spread,
        "fees": fees,
        "net_roi": net_roi,
        "bid": bid_price,
        "ask": ask_price,
    }


# ---------------------------------------------------------------------------
# Order book imbalance fee calculations (Layer 4 - Informed Trading)
# ---------------------------------------------------------------------------

def net_profit_imbalance(
    entry_price: float,
    exit_price: float,
    size: float,
    platform: str = "polymarket",
) -> float:
    """Calculate net profit for an order book imbalance signal execution.

    Imbalance trades are Layer 4 (informed trading) based on directional signals
    from bid/ask volume ratios. Execution uses taker orders (time-sensitive) because
    the signal may decay quickly. Entry and exit both incur taker fees.

    Args:
        entry_price: Entry price in [0, 1] (where we buy the predicted direction).
        exit_price: Exit price in [0, 1] (where we sell to lock in profit).
        size: Trade size in dollars.
        platform: Platform for fee calculation ("polymarket", "kalshi", "gemini", etc.).

    Returns:
        Net profit in USD after fees. May be negative if signal was wrong.
    """
    if size <= 0:
        return 0.0

    # `size` is dollars; one contract pays $1 and costs `price` dollars,
    # so contracts = size / price. Fee functions expect contract counts.
    contracts = max(1, int(size / entry_price)) if entry_price > 0 else 1

    if platform == "polymarket":
        entry_fee = polymarket_taker_fee(entry_price, contracts=contracts)
        exit_fee = polymarket_taker_fee(exit_price, contracts=contracts)
        gas = POLYGON_GAS_ESTIMATE * 2  # Two Polygon transactions
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee - gas

    elif platform == "kalshi":
        entry_fee = kalshi_taker_fee(entry_price, contracts=contracts)
        exit_fee = kalshi_taker_fee(exit_price, contracts=contracts)
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    elif platform == "gemini":
        # Use canonical gemini_fee (rate * P * (1-P) per contract, ceiled to cent),
        # NOT the legacy min(P, 1-P) formula.
        entry_fee = gemini_fee(entry_price, contracts=contracts)
        exit_fee = gemini_fee(exit_price, contracts=contracts)
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    else:
        # Default: conservative taker fee estimate (1% of notional per side)
        fee_rate = 0.01
        entry_fee = size * fee_rate
        exit_fee = size * fee_rate
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    return net_profit


# ---------------------------------------------------------------------------
# News-driven resolution sniping fee calculations (Layer 2 - Near-Arb)
# ---------------------------------------------------------------------------

def net_profit_news_snipe(
    entry_price: float,
    exit_price: float,
    size: float,
    platform: str = "polymarket",
) -> float:
    """Calculate net profit for a news-driven resolution snipe execution.

    News sniping is a Layer 2 (near-arbitrage) strategy exploiting time-sensitive
    event signals from Finnhub news headlines. Execution uses taker orders because
    speed is critical — latency determines profitability. Both entry and exit incur
    taker fees to ensure fast fills.

    Args:
        entry_price: Entry price in [0, 1] (where we buy the predicted outcome).
        exit_price: Exit price in [0, 1] (where we sell at market rate).
        size: Trade size in dollars.
        platform: Platform for fee calculation ("polymarket", "kalshi", "gemini", etc.).

    Returns:
        Net profit in USD after taker fees. May be negative if signal was incorrect.
    """
    if size <= 0:
        return 0.0

    contracts = max(1, int(size / entry_price)) if entry_price > 0 else 1

    if platform == "polymarket":
        entry_fee = polymarket_taker_fee(entry_price, contracts=contracts)
        exit_fee = polymarket_taker_fee(exit_price, contracts=contracts)
        gas = POLYGON_GAS_ESTIMATE * 2  # Two Polygon transactions (buy + sell)
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee - gas

    elif platform == "kalshi":
        entry_fee = kalshi_taker_fee(entry_price, contracts=contracts)
        exit_fee = kalshi_taker_fee(exit_price, contracts=contracts)
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    elif platform == "gemini":
        entry_fee = gemini_fee(entry_price, contracts=contracts)
        exit_fee = gemini_fee(exit_price, contracts=contracts)
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    else:
        # Default: conservative taker fee estimate (2% of notional per side)
        fee_rate = 0.02
        entry_fee = size * fee_rate
        exit_fee = size * fee_rate
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    return net_profit


def net_profit_correlated(
    long_entry_price: float,
    long_exit_price: float,
    short_entry_price: float,
    short_exit_price: float,
    size: float,
    platform_long: str = "polymarket",
    platform_short: str = "polymarket",
) -> float:
    """Calculate net profit for a correlated pair convergence trade.

    Correlated pair trades match-size both legs (long the underpriced outcome,
    short the overpriced outcome) to capture spread convergence with minimal
    directional exposure. Both legs are Layer 4 (informed trading) based on
    correlation signals, typically executed with taker orders (time-sensitive).

    Args:
        long_entry_price: Entry price for the long leg (underpriced outcome).
        long_exit_price: Exit price for the long leg.
        short_entry_price: Entry price for the short leg (overpriced outcome).
        short_exit_price: Exit price for the short leg.
        size: Trade size in dollars (same for both legs to maintain correlation hedge).
        platform_long: Platform for long leg ("polymarket", "kalshi", etc.).
        platform_short: Platform for short leg (can differ from long leg).

    Returns:
        Net profit in USD after fees on both legs. May be negative if convergence
        did not occur as expected.
    """
    if size <= 0:
        return 0.0

    # Long leg profit: buy at entry, sell at exit
    long_gross = size * (long_exit_price - long_entry_price)
    long_entry_fee = 0.0
    long_exit_fee = 0.0
    long_contracts = max(1, int(size / long_entry_price)) if long_entry_price > 0 else 1

    if platform_long == "polymarket":
        long_entry_fee = polymarket_taker_fee(long_entry_price, contracts=long_contracts)
        long_exit_fee = polymarket_taker_fee(long_exit_price, contracts=long_contracts)
    elif platform_long == "kalshi":
        long_entry_fee = kalshi_taker_fee(long_entry_price, contracts=long_contracts)
        long_exit_fee = kalshi_taker_fee(long_exit_price, contracts=long_contracts)
    elif platform_long == "gemini":
        long_entry_fee = gemini_fee(long_entry_price, contracts=long_contracts)
        long_exit_fee = gemini_fee(long_exit_price, contracts=long_contracts)
    else:
        long_entry_fee = size * 0.02
        long_exit_fee = size * 0.02

    long_net = long_gross - long_entry_fee - long_exit_fee

    # Short leg profit: sell at entry, buy back at exit (reversed)
    short_gross = size * (short_entry_price - short_exit_price)
    short_entry_fee = 0.0
    short_exit_fee = 0.0
    short_contracts = max(1, int(size / short_entry_price)) if short_entry_price > 0 else 1

    if platform_short == "polymarket":
        short_entry_fee = polymarket_taker_fee(short_entry_price, contracts=short_contracts)
        short_exit_fee = polymarket_taker_fee(short_exit_price, contracts=short_contracts)
    elif platform_short == "kalshi":
        short_entry_fee = kalshi_taker_fee(short_entry_price, contracts=short_contracts)
        short_exit_fee = kalshi_taker_fee(short_exit_price, contracts=short_contracts)
    elif platform_short == "gemini":
        short_entry_fee = gemini_fee(short_entry_price, contracts=short_contracts)
        short_exit_fee = gemini_fee(short_exit_price, contracts=short_contracts)
    else:
        short_entry_fee = size * 0.02
        short_exit_fee = size * 0.02

    short_net = short_gross - short_entry_fee - short_exit_fee

    # Total net profit from both legs
    return long_net + short_net


# ---------------------------------------------------------------------------
# Time decay convergence fee calculations (Layer 2 - Near-Arb)
# ---------------------------------------------------------------------------

def net_profit_time_decay(
    entry_price: float,
    exit_price: float,
    size: float,
    platform: str = "polymarket",
) -> float:
    """Calculate net profit for a time decay convergence position.

    Time decay trades hold near-certain outcomes to market resolution for
    guaranteed profit. Entry price is <0.95 (buy at discount), exit price
    is typically 1.0 (correct outcome at settlement) or 0.0 (wrong outcome).
    Layer 2 (near-arbitrage) strategy.

    Entry pays taker fee; exit at settlement may incur platform settlement fees
    (e.g., Polymarket settlement fee on net winnings, Kalshi taker on close-out).

    Args:
        entry_price: Entry price in [0, 1] (typically 0.90-0.95 for <0.95 buy).
        exit_price: Exit price in [0, 1] (typically 1.0 if correct, 0.0 if wrong).
        size: Trade size in dollars.
        platform: Platform for fee calculation ("polymarket", "kalshi", "gemini", etc.).

    Returns:
        Net profit in USD after entry and exit fees. Typically +1-5% if consensus
        correct (entry 0.90, exit 1.0), negative if consensus wrong.
    """
    if size <= 0:
        return 0.0

    contracts = max(1, int(size / entry_price)) if entry_price > 0 else 1

    if platform == "polymarket":
        # Entry-time taker fee only; settlement has no additional fee in March 2026 model.
        entry_fee = polymarket_taker_fee(entry_price, contracts=contracts)
        exit_fee = 0.0
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    elif platform == "kalshi":
        # Conservative: charge taker fee at entry only; settlement is automatic.
        entry_fee = kalshi_taker_fee(entry_price, contracts=contracts)
        exit_fee = 0.0
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    elif platform == "gemini":
        # Entry pays taker fee; exit is automatic settlement.
        entry_fee = gemini_fee(entry_price, contracts=contracts)
        exit_fee = 0.0
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    else:
        # Default: conservative 1% notional taker fee on entry only.
        fee_rate = 0.01
        entry_fee = size * fee_rate
        exit_fee = 0.0
        gross_profit = size * (exit_price - entry_price)
        net_profit = gross_profit - entry_fee - exit_fee

    return net_profit


def net_profit_logical_arb(price_if_yes: float, price_then_yes: float) -> float:
    """Calculate net profit for logical arbitrage trade.

    Buys the underpriced implied outcome (then_yes) and sells the implying outcome
    (if_yes) to capitalize on semantic inconsistencies across related markets.

    Layer 4: Informed trading with rapid execution at taker rates.

    Formula: The trade structure is:
    - BUY: then_yes at taker fee
    - SELL: if_yes at taker fee (hedge)
    - Profit: (price_if_yes - TAKER_FEE) - (price_then_yes + TAKER_FEE)

    Example: Bitcoin >$100k trading at 0.50, Bitcoin >$90k trading at 0.45.
    If P(>$100k) should be <= P(>$90k) logically, buy >$90k, sell >$100k.
    Net profit = (0.50 - 0.04*0.5*(1-0.5)) - (0.45 + 0.04*0.45*(1-0.45)) ≈ 0.045

    Args:
        price_if_yes: Price of the implying outcome (e.g., Bitcoin >$100k).
        price_then_yes: Price of the implied outcome (e.g., Bitcoin >$90k).

    Returns:
        Net profit in USD after both entry fees. Can be negative if trade is unfavorable.
    """
    # Entry fees: both legs pay Polymarket taker fee at trade entry
    # Taker fee = POLYMARKET_DEFAULT_TAKER_RATE * P * (1 - P) per contract
    fee_sell_if_yes = polymarket_taker_fee(price_if_yes)
    fee_buy_then_yes = polymarket_taker_fee(price_then_yes)

    # Net profit: receive if_yes price minus both entry fees, minus cost of then_yes
    # Assuming 1 contract ($1 stake)
    net_profit = price_if_yes - price_then_yes - fee_sell_if_yes - fee_buy_then_yes

    return net_profit


# ---------------------------------------------------------------------------
# Fréchet-Bound Logical Arbitrage fee calculator
# ---------------------------------------------------------------------------


def net_profit_frechet_implication(
    p_a: float,
    p_b: float,
    platform: str = "polymarket",
    category: str | None = None,
) -> dict:
    """Calculate net profit for an implication-violation lock on A ⊆ B.

    Coherence requires P(A) <= P(B). When P(A) > P(B), buy YES on B and NO on A.
    Payoffs across reachable states (A ⊆ B forbids A=1, B=0):
      - A=1, B=1: YES_B pays $1, NO_A pays $0 -> payout $1.00
      - A=0, B=1: YES_B pays $1, NO_A pays $1 -> payout $2.00
      - A=0, B=0: YES_B pays $0, NO_A pays $1 -> payout $1.00
    Minimum guaranteed payoff = $1.00.
    Total cost = P(YES_B) + P(NO_A) = P(B) + (1.0 - P(A)) = 1.0 + P(B) - P(A).
    Gross spread = 1.0 - Total cost = P(A) - P(B).
    Profit = (P(A) - P(B)) - fees.

    Args:
        p_a: Probability/price of subset event A.
        p_b: Probability/price of superset event B.
        platform: Execution platform ("polymarket" or "kalshi").
        category: Market category for Polymarket dynamic taker fee.

    Returns:
        Dict with keys: gross_spread, fees, net_profit, net_roi, total_cost.
    """
    gross_spread = p_a - p_b
    total_cost = 1.0 + p_b - p_a
    if gross_spread <= 0:
        return {
            "gross_spread": gross_spread,
            "fees": 0.0,
            "net_profit": gross_spread,
            "net_roi": 0.0,
            "total_cost": total_cost,
        }

    if platform == "kalshi":
        fee = kalshi_taker_fee(p_b) + kalshi_taker_fee(1.0 - p_a)
        gas = 0.0
    else:
        fee = (polymarket_taker_fee(p_b, category=category)
               + polymarket_taker_fee(1.0 - p_a, category=category))
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fee + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / total_cost if total_cost > 0 else 0.0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
        "total_cost": total_cost,
    }


# Plan 03: Cross-date temporal arbitrage shares identical Dutch book implication payoff economics
net_profit_temporal_implication = net_profit_frechet_implication


# ---------------------------------------------------------------------------
# Whale Copy Trading fee calculator
# ---------------------------------------------------------------------------


def net_profit_whale_copy(entry_price: float, exit_price: float) -> float:
    """Calculate net profit for whale copy mirror trade.

    Mirrors a profitable whale trader's position on Polymarket. We're buying
    at entry_price (taker fee) and selling at exit_price (taker fee).

    Layer 4: Rapid execution at taker rates on both legs.

    Formula:
    - BUY at entry_price: pay taker fee
    - SELL at exit_price: pay taker fee
    - Profit = (exit_price - entry_price) - fee_buy - fee_sell

    Args:
        entry_price: Price we buy at (copying whale's entry).
        exit_price: Expected exit price (whale's target or current market).

    Returns:
        Net profit in USD after taker fees on both legs.
    """
    fee_buy = polymarket_taker_fee(entry_price)
    fee_sell = polymarket_taker_fee(exit_price)
    return (exit_price - entry_price) - fee_buy - fee_sell


# ---------------------------------------------------------------------------
# Strategy #30: Conditional Market Arbitrage
# ---------------------------------------------------------------------------


def net_profit_conditional(
    p_x_given_y: float,
    p_y: float,
    p_x: float,
    direction: str = "BUY_CONDITIONAL",
) -> dict:
    """Calculate net profit for conditional market arbitrage.

    Exploits mispricings between conditional and unconditional markets:
        P(X|Y) × P(Y) should equal P(X)

    Strategy:
    - BUY_CONDITIONAL: Buy P(X|Y) and P(Y), sell P(X)
    - BUY_UNCONDITIONAL: Buy P(X), sell P(X|Y) and P(Y)

    Layer 1: Pure arbitrage when the combined probability diverges.

    Args:
        p_x_given_y: Price of conditional market P(X|Y).
        p_y: Price of condition market P(Y).
        p_x: Price of unconditional market P(X).
        direction: "BUY_CONDITIONAL" or "BUY_UNCONDITIONAL".

    Returns:
        Dict with gross_spread, fees, net_profit, total_cost, net_roi.
    """
    combined = p_x_given_y * p_y

    if direction == "BUY_CONDITIONAL":
        total_cost = p_x_given_y + p_y
        gross_spread = p_x - combined
    else:
        total_cost = p_x
        gross_spread = combined - p_x

    if gross_spread <= 0:
        return {
            "gross_spread": gross_spread,
            "fees": 0,
            "net_profit": gross_spread,
            "total_cost": total_cost,
            "net_roi": 0,
        }

    if direction == "BUY_CONDITIONAL":
        fee_cond = polymarket_taker_fee(p_x_given_y)
        fee_y = polymarket_taker_fee(p_y)
        fee_x = polymarket_taker_fee(p_x)
        fees = fee_cond + fee_y + fee_x
        gas = POLYGON_GAS_ESTIMATE * 3
    else:
        fee_x = polymarket_taker_fee(p_x)
        fee_cond = polymarket_taker_fee(p_x_given_y)
        fee_y = polymarket_taker_fee(p_y)
        fees = fee_x + fee_cond + fee_y
        gas = POLYGON_GAS_ESTIMATE * 3

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / total_cost if total_cost > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "total_cost": total_cost,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #31: Bracket/Range Market Arbitrage
# ---------------------------------------------------------------------------


def net_profit_bracket(
    bracket_prices: list[float],
    platform: str = "kalshi",
) -> dict:
    """Calculate net profit for bracket/range market arbitrage.

    Exploits when the sum of mutually exclusive range brackets is below 1.0:
        Σ(P(range_i)) < 1.0

    Example: "BTC $60k-70k" + "BTC $70k-80k" + "BTC $80k+" = 0.95 → 5% profit

    Layer 1: Pure arbitrage — buy all brackets for guaranteed payout.

    Args:
        bracket_prices: List of YES prices for each bracket.
        platform: Platform for fee calculation ("kalshi" or "polymarket").

    Returns:
        Dict with gross_spread, fees, net_profit, total_cost, net_roi.
    """
    total_cost = sum(bracket_prices)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {
            "gross_spread": gross_spread,
            "fees": 0,
            "net_profit": gross_spread,
            "total_cost": total_cost,
            "net_roi": 0,
        }

    if platform == "kalshi":
        fees = sum(kalshi_taker_fee(p) for p in bracket_prices)
        gas = 0
    else:
        fees = sum(polymarket_taker_fee(p) for p in bracket_prices)
        gas = POLYGON_GAS_ESTIMATE * len(bracket_prices)

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / total_cost if total_cost > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "total_cost": total_cost,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #32: N-Way Multi-Leg Exotic Arbitrage
# ---------------------------------------------------------------------------


def net_profit_nway(
    platform_prices: list[tuple[str, float]],
) -> dict:
    """Calculate net profit for N-way cross-platform arbitrage.

    Extends triangular (3-way) to N-way cycles across 4+ platforms.

    Args:
        platform_prices: List of (platform, price) tuples for each leg.

    Returns:
        Dict with gross_spread, fees, net_profit, total_cost, net_roi.
    """
    total_cost = sum(price for _, price in platform_prices)
    gross_spread = 1.0 - total_cost

    if gross_spread <= 0:
        return {
            "gross_spread": gross_spread,
            "fees": 0,
            "net_profit": gross_spread,
            "total_cost": total_cost,
            "net_roi": 0,
        }

    fees = 0.0
    gas = 0.0

    for platform, price in platform_prices:
        if platform == "polymarket":
            fees += polymarket_taker_fee(price)
            gas += POLYGON_GAS_ESTIMATE
        elif platform == "kalshi":
            fees += kalshi_taker_fee(price)
        elif platform in ("betfair", "smarkets"):
            pass
        elif platform == "gemini":
            fees += gemini_fee(price)
        else:
            fees += polymarket_taker_fee(price)

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / total_cost if total_cost > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "total_cost": total_cost,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #33: Settlement Timing Arbitrage
# ---------------------------------------------------------------------------


def net_profit_settlement_timing(
    current_price: float,
    expected_payout: float = 1.0,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for settlement timing arbitrage.

    Buy winning outcome on slow-settling platform before settlement propagates.

    Layer 2: Near-arbitrage — outcome is known but not yet settled.

    Args:
        current_price: Current trading price on the slow platform.
        expected_payout: Expected payout (usually 1.0 for binary YES).
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = expected_payout - current_price

    if gross_spread <= 0:
        return {
            "gross_spread": gross_spread,
            "fees": 0,
            "net_profit": gross_spread,
            "net_roi": 0,
        }

    if platform == "polymarket":
        fees = polymarket_taker_fee(current_price)
        gas = POLYGON_GAS_ESTIMATE
    elif platform == "kalshi":
        fees = kalshi_taker_fee(current_price)
        gas = 0
    else:
        fees = polymarket_taker_fee(current_price)
        gas = POLYGON_GAS_ESTIMATE

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / current_price if current_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #34: New Market Mispricing
# ---------------------------------------------------------------------------


def net_profit_new_market(
    market_price: float,
    fair_value: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for new market mispricing.

    Exploit price inefficiency in first 24-48h of new markets.

    Layer 2: Near-arbitrage — directional bet on price convergence.

    Args:
        market_price: Current market price.
        fair_value: Estimated fair value from multi-source consensus.
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = abs(fair_value - market_price)

    if gross_spread <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = market_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #35: API Outage Arbitrage
# ---------------------------------------------------------------------------


def net_profit_stale_price(
    stale_price: float,
    fresh_price: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for API outage / stale price arbitrage.

    Exploit stale prices during platform API outages.

    Layer 2: Near-arbitrage — exploits temporary information asymmetry.

    Args:
        stale_price: Stale price on the outage platform.
        fresh_price: Fresh price on healthy platform(s).
        platform: Platform with stale prices for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = abs(fresh_price - stale_price)

    if gross_spread <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = stale_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #39: Social Sentiment Signals
# ---------------------------------------------------------------------------


def net_profit_social_sentiment(
    market_price: float,
    implied_prob: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for social sentiment divergence trade.

    Trade when social media sentiment diverges from market price.

    Layer 4: Informed trading — directional bet based on sentiment edge.

    Args:
        market_price: Current market price (entry price).
        implied_prob: Implied probability from sentiment analysis.
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = abs(implied_prob - market_price)

    if gross_spread <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = market_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #40: Expert Forecaster Divergence
# ---------------------------------------------------------------------------


def net_profit_expert_divergence(
    market_price: float,
    expert_prob: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for expert forecaster divergence trade.

    Trade when superforecaster predictions diverge from market prices.

    Layer 4: Informed trading — directional bet based on expert edge.

    Args:
        market_price: Current market price (entry price).
        expert_prob: Expert/superforecaster probability estimate.
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = abs(expert_prob - market_price)

    if gross_spread <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = market_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #42: Insider Pattern Detection
# ---------------------------------------------------------------------------


def net_profit_insider_pattern(
    market_price: float,
    expected_edge: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for insider pattern detection trade.

    Follow unusual order flow that may indicate informed trading.

    Layer 4: Informed trading — follow informed money.

    Args:
        market_price: Current market price (entry price).
        expected_edge: Expected edge based on flow analysis.
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    if expected_edge <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = market_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = expected_edge - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": expected_edge,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Strategy #43: Cross-Category Correlation Signals
# ---------------------------------------------------------------------------


def net_profit_cross_category(
    market_price: float,
    implied_prob: float,
    platform: str = "polymarket",
) -> dict:
    """Calculate net profit for cross-category correlation trade.

    Trade when external signals (BTC price, etc.) imply different probability.

    Layer 4: Informed trading — exploit slow price discovery.

    Args:
        market_price: Current market price (entry price).
        implied_prob: Implied probability from external signals.
        platform: Platform for fee calculation.

    Returns:
        Dict with gross_spread, fees, net_profit, net_roi.
    """
    gross_spread = abs(implied_prob - market_price)

    if gross_spread <= 0:
        return {
            "gross_spread": 0,
            "fees": 0,
            "net_profit": 0,
            "net_roi": 0,
        }

    entry_price = market_price
    if platform == "polymarket":
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2
    elif platform == "kalshi":
        fees = kalshi_taker_fee(entry_price) * 2
        gas = 0
    else:
        fees = polymarket_taker_fee(entry_price) * 2
        gas = POLYGON_GAS_ESTIMATE * 2

    total_fees = fees + gas
    net_profit = gross_spread - total_fees
    net_roi = net_profit / entry_price if entry_price > 0 else 0

    return {
        "gross_spread": gross_spread,
        "fees": total_fees,
        "net_profit": net_profit,
        "net_roi": net_roi,
    }


# ---------------------------------------------------------------------------
# Plan 04: CTF Primitives Fee Calculators
# ---------------------------------------------------------------------------

def net_profit_ctf_merge(
    yes_ask: float,
    no_ask: float,
    category: str | None = None,
    gas_cost: float | None = None,
) -> dict[str, float]:
    """Net profit calculator for buy-and-merge CTF arbitrage (YES_ask + NO_ask < $1.00).

    Buys both YES and NO on the order book and merges them into $1.00 collateral
    via ConditionalTokens.mergePositions(), avoiding resolution holding period.

    Args:
        yes_ask: Best ask price for YES token.
        no_ask: Best ask price for NO token.
        category: Market category for Polymarket taker fee rate lookup.
        gas_cost: Optional gas cost override in USD. If None, uses
            CTF_GAS_ESTIMATE + 2 * POLYGON_GAS_ESTIMATE.

    Returns:
        Dict with gross_spread, fees, net_profit.
    """
    gross = 1.0 - (yes_ask + no_ask)
    if gross <= 0.0:
        return {"gross_spread": gross, "fees": 0.0, "net_profit": gross}

    fee_yes = polymarket_taker_fee(yes_ask, category=category)
    fee_no = polymarket_taker_fee(no_ask, category=category)
    gas = gas_cost if gas_cost is not None else (CTF_GAS_ESTIMATE + POLYGON_GAS_ESTIMATE * 2)
    total_fees = fee_yes + fee_no + gas

    return {
        "gross_spread": gross,
        "fees": total_fees,
        "net_profit": gross - total_fees,
    }


def net_profit_ctf_mint(
    yes_bid: float,
    no_bid: float,
    category: str | None = None,
    gas_cost: float | None = None,
) -> dict[str, float]:
    """Net profit calculator for mint-and-sell CTF arbitrage (YES_bid + NO_bid > $1.00).

    Mints 1 YES + 1 NO from $1.00 collateral via ConditionalTokens.splitPosition(),
    then sells both legs into resting bids for > $1.00.

    Args:
        yes_bid: Best bid price for YES token.
        no_bid: Best bid price for NO token.
        category: Market category for Polymarket taker fee rate lookup.
        gas_cost: Optional gas cost override in USD. If None, uses
            CTF_GAS_ESTIMATE + 2 * POLYGON_GAS_ESTIMATE.

    Returns:
        Dict with gross_spread, fees, net_profit.
    """
    gross = (yes_bid + no_bid) - 1.0
    if gross <= 0.0:
        return {"gross_spread": gross, "fees": 0.0, "net_profit": gross}

    fee_yes = polymarket_taker_fee(yes_bid, category=category)
    fee_no = polymarket_taker_fee(no_bid, category=category)
    gas = gas_cost if gas_cost is not None else (CTF_GAS_ESTIMATE + POLYGON_GAS_ESTIMATE * 2)
    total_fees = fee_yes + fee_no + gas

    return {
        "gross_spread": gross,
        "fees": total_fees,
        "net_profit": gross - total_fees,
    }
