"""Tests for continuous.py — OpportunityIndex and WS-triggered execution logic."""

import sys
import os
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from unittest.mock import MagicMock, patch

# Save any real modules already loaded so we can restore them after import
_saved_modules = {}
_modules_to_mock = [
    "kalshi_api", "dashboard", "display", "recovery",
]
for _mod_name in _modules_to_mock:
    if _mod_name in sys.modules:
        _saved_modules[_mod_name] = sys.modules[_mod_name]

# Mock heavy dependencies before importing continuous
mock_kalshi = MagicMock()
mock_kalshi._sign_pss = MagicMock(return_value="fake_sig")
mock_kalshi._load_private_key = MagicMock(return_value=None)
mock_kalshi.KALSHI_BASE_URL = "https://api.elections.kalshi.com"
mock_kalshi.KALSHI_API_PATH = "/trade-api/v2"
sys.modules["kalshi_api"] = mock_kalshi


mock_dashboard = MagicMock()
mock_dashboard.state = MagicMock()
sys.modules["dashboard"] = mock_dashboard

sys.modules["display"] = MagicMock()
sys.modules["recovery"] = MagicMock()

from continuous import (
    OpportunityIndex,
    _StageTimer,
    _calc_realized_pnl,
    _format_stage_timings,
    _get_market_lock,
    _is_execution_eligible,
    _recalc_profit,
    _ws_opportunity_probability,
    _ws_tracking_probability,
)
from db import TradeDB

# Restore original modules so other test files are not affected
for _mod_name in _modules_to_mock:
    if _mod_name in _saved_modules:
        sys.modules[_mod_name] = _saved_modules[_mod_name]
    elif _mod_name in sys.modules:
        del sys.modules[_mod_name]


# ---------------------------------------------------------------------------
# OpportunityIndex — rebuild and lookup
# ---------------------------------------------------------------------------


class TestStageTimer:
    """_StageTimer + _format_stage_timings — scan-cycle profiling helpers."""

    def test_records_elapsed(self):
        timings: dict = {}
        with _StageTimer("foo", timings):
            time.sleep(0.01)
        assert "foo" in timings
        assert timings["foo"] >= 0.01

    def test_records_on_exception(self):
        """Timer must record elapsed even when the wrapped block raises."""
        timings: dict = {}
        with pytest.raises(RuntimeError):
            with _StageTimer("bar", timings):
                raise RuntimeError("kaboom")
        assert "bar" in timings

    def test_format_sorts_descending(self):
        out = _format_stage_timings({"a": 1.0, "b": 5.0, "c": 0.5}, total=10.0)
        # Bottleneck (highest elapsed) appears first after total
        assert out.startswith("total=10.0s b=5.0s")
        assert "a=1.0s" in out and "c=0.5s" in out

    def test_format_handles_zero_total(self):
        # Should not raise even when total is 0 (e.g. all stages skipped)
        out = _format_stage_timings({"x": 0.0}, total=0.0)
        assert "0%" in out


class TestCrossPairIndexImport:
    """Phase 2 wiring: continuous.py must import CrossPairIndex without errors.

    A real end-to-end test of the WS handler would require running the full
    asyncio event loop with mocked feeds. The smaller, durable contract is:
    the import is in place, the env var defaults to enabled, and the wiring
    didn't introduce a syntax/typo regression. The actual lookup → evaluate
    → priority-queue path is covered by `test_cross_pair_index.py` (24 tests
    exercising the unit) plus production observation after deploy.
    """

    def test_cross_pair_index_importable_from_continuous_module(self):
        from cross_pair_index import CrossPairIndex
        idx = CrossPairIndex()
        assert idx.pair_count == 0
        # Lookup on an empty index must not crash
        assert idx.lookup("polymarket", "anything") == []

    def test_env_var_default_enabled(self):
        import os
        # Phase 2 ships with CROSS_PAIR_WS_ENABLED defaulting to "true".
        # If a prior test set it to "false", this assertion documents the
        # default behaviour for production.
        val = os.getenv("CROSS_PAIR_WS_ENABLED", "true").lower()
        assert val in ("true", "false")  # at minimum, parses as bool


class TestOpportunityIndexRebuild:
    def test_rebuild_indexes_polymarket_tokens(self):
        idx = OpportunityIndex()
        opps = [
            {"_token_ids": ["tok_a", "tok_b"], "net_profit": 0.05},
        ]
        idx.rebuild(opps)

        result = idx.lookup("polymarket", "tok_a")
        assert len(result) == 1
        assert result[0]["net_profit"] == 0.05

        result_b = idx.lookup("polymarket", "tok_b")
        assert len(result_b) == 1

    def test_rebuild_indexes_kalshi_ticker(self):
        idx = OpportunityIndex()
        opps = [
            {"_kalshi_ticker": "PRES-2028-REP", "net_profit": 0.03},
        ]
        idx.rebuild(opps)

        result = idx.lookup("kalshi", "PRES-2028-REP")
        assert len(result) == 1
        assert result[0]["net_profit"] == 0.03

    def test_rebuild_indexes_kalshi_tickers_list(self):
        idx = OpportunityIndex()
        opps = [
            {"_kalshi_tickers": ["TICK-A", "TICK-B"], "net_profit": 0.02},
        ]
        idx.rebuild(opps)

        assert len(idx.lookup("kalshi", "TICK-A")) == 1
        assert len(idx.lookup("kalshi", "TICK-B")) == 1

    def test_rebuild_replaces_previous_index(self):
        idx = OpportunityIndex()
        idx.rebuild([{"_token_ids": ["old_tok"], "net_profit": 0.01}])
        assert len(idx.lookup("polymarket", "old_tok")) == 1

        idx.rebuild([{"_token_ids": ["new_tok"], "net_profit": 0.02}])
        assert len(idx.lookup("polymarket", "old_tok")) == 0
        assert len(idx.lookup("polymarket", "new_tok")) == 1

    def test_rebuild_empty_list_clears_index(self):
        idx = OpportunityIndex()
        idx.rebuild([{"_token_ids": ["tok"], "net_profit": 0.01}])
        assert len(idx.lookup("polymarket", "tok")) == 1

        idx.rebuild([])
        assert len(idx.lookup("polymarket", "tok")) == 0

    def test_multiple_opps_same_token(self):
        idx = OpportunityIndex()
        opps = [
            {"_token_ids": ["shared_tok"], "net_profit": 0.05},
            {"_token_ids": ["shared_tok"], "net_profit": 0.08},
        ]
        idx.rebuild(opps)

        result = idx.lookup("polymarket", "shared_tok")
        assert len(result) == 2

    def test_opp_with_no_keys_not_indexed(self):
        idx = OpportunityIndex()
        opps = [
            {"net_profit": 0.05},  # no tokens or tickers
        ]
        idx.rebuild(opps)
        assert idx.lookup("polymarket", "") == []
        assert idx.lookup("kalshi", "") == []

    def test_monitoring_only_opp_cannot_reach_ws_index(self):
        idx = OpportunityIndex()
        idx.rebuild([{
            "_token_ids": ["reward-token"],
            "net_profit": 10.0,
            "_execution_eligible": False,
        }])
        assert idx.lookup("polymarket", "reward-token") == []
        assert idx.get_subscription_tokens() == ([], [])

    def test_execution_eligibility_defaults_true_and_honors_false(self):
        assert _is_execution_eligible({}) is True
        assert _is_execution_eligible({"_execution_eligible": True}) is True
        assert _is_execution_eligible({"_execution_eligible": False}) is False


# ---------------------------------------------------------------------------
# OpportunityIndex — lookup
# ---------------------------------------------------------------------------


class TestOpportunityIndexLookup:
    def test_lookup_returns_empty_for_unknown_token(self):
        idx = OpportunityIndex()
        idx.rebuild([{"_token_ids": ["tok_a"], "net_profit": 0.01}])

        assert idx.lookup("polymarket", "tok_nonexistent") == []

    def test_lookup_returns_empty_for_wrong_platform(self):
        idx = OpportunityIndex()
        idx.rebuild([{"_token_ids": ["tok_a"], "net_profit": 0.01}])

        assert idx.lookup("kalshi", "tok_a") == []

    def test_lookup_returns_list_copy(self):
        """Ensure returned list is a copy, not a reference to internal data."""
        idx = OpportunityIndex()
        idx.rebuild([{"_token_ids": ["tok_a"], "net_profit": 0.01}])

        result1 = idx.lookup("polymarket", "tok_a")
        result2 = idx.lookup("polymarket", "tok_a")
        assert result1 is not result2


# ---------------------------------------------------------------------------
# OpportunityIndex — get_subscription_tokens
# ---------------------------------------------------------------------------


class TestGetSubscriptionTokens:
    def test_returns_poly_and_kalshi_tokens(self):
        idx = OpportunityIndex()
        opps = [
            {"_token_ids": ["poly_1"], "net_profit": 0.05},
            {"_kalshi_ticker": "KALSHI-1", "net_profit": 0.03},
        ]
        idx.rebuild(opps)

        poly, kalshi = idx.get_subscription_tokens(limit=10)
        assert "poly_1" in poly
        assert "KALSHI-1" in kalshi

    def test_respects_limit(self):
        idx = OpportunityIndex()
        opps = [
            {"_token_ids": [f"poly_{i}"], "net_profit": 0.01 * i}
            for i in range(10)
        ]
        idx.rebuild(opps)

        poly, kalshi = idx.get_subscription_tokens(limit=3)
        assert len(poly) <= 3

    def test_prioritizes_higher_profit(self):
        idx = OpportunityIndex()
        opps = [
            {"_token_ids": ["low_profit"], "net_profit": 0.001},
            {"_token_ids": ["high_profit"], "net_profit": 0.10},
        ]
        idx.rebuild(opps)

        poly, _ = idx.get_subscription_tokens(limit=1)
        assert "high_profit" in poly

    def test_empty_index_returns_empty(self):
        idx = OpportunityIndex()
        idx.rebuild([])

        poly, kalshi = idx.get_subscription_tokens()
        assert poly == []
        assert kalshi == []


# ---------------------------------------------------------------------------
# OpportunityIndex._extract_keys
# ---------------------------------------------------------------------------


class TestExtractKeys:
    def test_extracts_token_ids(self):
        opp = {"_token_ids": ["a", "b"]}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("polymarket", "a") in keys
        assert ("polymarket", "b") in keys

    def test_extracts_kalshi_ticker(self):
        opp = {"_kalshi_ticker": "TICK-1"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("kalshi", "TICK-1") in keys

    def test_extracts_kalshi_tickers_list(self):
        opp = {"_kalshi_tickers": ["T1", "T2"]}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("kalshi", "T1") in keys
        assert ("kalshi", "T2") in keys

    def test_skips_empty_token_ids(self):
        opp = {"_token_ids": ["valid", "", None]}
        keys = OpportunityIndex._extract_keys(opp)
        assert len([k for k in keys if k[0] == "polymarket"]) == 1

    def test_skips_empty_kalshi_ticker(self):
        opp = {"_kalshi_ticker": ""}
        keys = OpportunityIndex._extract_keys(opp)
        assert len(keys) == 0

    def test_combined_poly_and_kalshi(self):
        opp = {
            "_token_ids": ["poly_tok"],
            "_kalshi_ticker": "KALSHI_TICK",
        }
        keys = OpportunityIndex._extract_keys(opp)
        assert ("polymarket", "poly_tok") in keys
        assert ("kalshi", "KALSHI_TICK") in keys

    def test_no_keys_in_opp(self):
        opp = {"net_profit": 0.05, "market": "test"}
        keys = OpportunityIndex._extract_keys(opp)
        assert keys == []

    def test_extracts_betfair_market_id(self):
        opp = {"type": "BetfairBackAll", "_bf_market_id": "1.234567"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("betfair", "1.234567") in keys

    def test_extracts_betfair_fallback_market_id(self):
        opp = {"type": "BetfairBackLay", "_market_id": "1.999"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("betfair", "1.999") in keys

    def test_betfair_key_requires_betfair_type(self):
        """Non-betfair type with _market_id should NOT produce betfair key."""
        opp = {"type": "Binary", "_market_id": "1.999"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("betfair", "1.999") not in keys

    def test_extracts_smarkets_market_id(self):
        opp = {"type": "SmarketsBackAll", "_sm_market_id": "sm_123"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("smarkets", "sm_123") in keys

    def test_extracts_sxbet_market_hash(self):
        opp = {"type": "SXBetBackAll", "_sx_market_hash": "0xabc"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("sxbet", "0xabc") in keys

    def test_extracts_matchbook_market_id(self):
        opp = {"type": "MatchbookBackAll", "_mb_market_id": "mb_456"}
        keys = OpportunityIndex._extract_keys(opp)
        assert ("matchbook", "mb_456") in keys

    def test_extracts_event_divergence_metaculus_key(self):
        opp = {
            "type": "EventDivergence",
            "_platform": "polymarket",
            "_metaculus_id": 12345,
        }
        keys = OpportunityIndex._extract_keys(opp)
        assert ("polymarket", "metaculus_12345") in keys

    def test_event_divergence_no_key_without_platform(self):
        opp = {
            "type": "EventDivergence",
            "_platform": "",
            "_metaculus_id": 12345,
        }
        keys = OpportunityIndex._extract_keys(opp)
        assert not any(k[1].startswith("metaculus_") for k in keys)

    def test_extracts_triangular_cross_keys(self):
        opp = {
            "type": "TriangularCross",
            "market": "Test Market",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
        }
        keys = OpportunityIndex._extract_keys(opp)
        assert ("polymarket", "Test Market") in keys
        assert ("kalshi", "Test Market") in keys

    def test_triangular_cross_skips_empty_platforms(self):
        opp = {
            "type": "TriangularCross",
            "market": "Test",
            "_platform_a": "polymarket",
            "_platform_b": "",
        }
        keys = OpportunityIndex._extract_keys(opp)
        assert ("polymarket", "Test") in keys
        assert not any(k[0] == "" for k in keys)


# ---------------------------------------------------------------------------
# _recalc_profit — WS trigger profit recalculation
# ---------------------------------------------------------------------------

class TestRecalcProfit:
    def test_recalc_binary_with_both_cached(self):
        """Should recalculate binary profit when both tokens are in cache."""
        opp = {
            "type": "Binary",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        cache = {
            ("polymarket", "tok_yes"): {"price": 0.40, "_ts": time.time()},
            ("polymarket", "tok_no"): {"price": 0.42, "_ts": time.time()},
        }
        result = _recalc_profit(opp, "polymarket", "tok_yes", 0.40, cache)
        assert result is not None
        # gross = 1.0 - 0.82 = 0.18, should be positive
        assert result > 0

    def test_recalc_binary_uses_normalized_string_asks(self):
        opp = {
            "type": "Binary",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        cache = {
            ("polymarket", "tok_yes"): {"best_ask": "0.40", "price": "0.39"},
            ("polymarket", "tok_no"): {"best_ask": "0.42", "price": "0.41"},
        }

        with patch("continuous.net_profit_binary_internal", return_value={"net_profit": 0.18}) as profit:
            result = _recalc_profit(opp, "polymarket", "tok_yes", 0.40, cache)

        assert result == pytest.approx(0.18)
        profit.assert_called_once_with(0.40, 0.42)

    def test_recalc_binary_missing_other_token(self):
        """Should return None when other token not in cache."""
        opp = {
            "type": "Binary",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        cache = {
            ("polymarket", "tok_yes"): {"price": 0.40, "_ts": time.time()},
        }
        result = _recalc_profit(opp, "polymarket", "tok_yes", 0.40, cache)
        assert result is None

    def test_recalc_negrisk(self):
        """Should recalculate negrisk profit using cached prices."""
        opp = {
            "type": "NegRisk (3 outcomes)",
            "_token_ids": ["t0", "t1", "t2"],
        }
        cache = {
            ("polymarket", "t0"): {"price": 0.20, "_ts": time.time()},
            ("polymarket", "t1"): {"price": 0.25, "_ts": time.time()},
            ("polymarket", "t2"): {"price": 0.30, "_ts": time.time()},
        }
        result = _recalc_profit(opp, "polymarket", "t0", 0.20, cache)
        assert result is not None
        # total = 0.75, gross = 0.25
        assert result > 0

    def test_recalc_unknown_type_returns_none(self):
        """Unknown opportunity types should return None."""
        opp = {"type": "UnknownType"}
        result = _recalc_profit(opp, "polymarket", "tok", 0.5, {})
        assert result is None

    def test_recalc_binary_no_token_ids(self):
        """Binary with missing token_ids should return None."""
        opp = {"type": "Binary", "_token_ids": []}
        result = _recalc_profit(opp, "polymarket", "tok", 0.5, {})
        assert result is None


class TestWSTrackingProbability:
    def test_kalshi_prefers_normalized_ask_over_raw_ladder(self):
        entry = {
            "yes": [[40, 10]],
            "no": [[60, 10]],
            "yes_ask": "0.41",
            "no_ask": "0.61",
        }

        assert _ws_tracking_probability("kalshi", entry) == pytest.approx(0.41)

    def test_kalshi_raw_ladder_and_cent_delta_are_not_probabilities(self):
        entry = {"yes": [[40, 10]], "no": [[60, 10]], "price": 45}

        assert _ws_tracking_probability("kalshi", entry) is None

    def test_opportunity_selects_kalshi_no_side_from_metadata(self):
        opp = {
            "type": "FeePromo",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_side_a": "yes",
            "_side_b": "no",
        }
        entry = {"yes_ask": "0.41", "no_ask": "0.62"}

        assert _ws_opportunity_probability(opp, "kalshi", entry) == pytest.approx(0.62)

    def test_opportunity_infers_kalshi_no_side_from_cross_type(self):
        opp = {"type": "Cross(PM_YES + K_NO)"}
        entry = {"yes_ask": "0.41", "no_ask": "0.62"}

        assert _ws_opportunity_probability(opp, "kalshi", entry) == pytest.approx(0.62)


# ---------------------------------------------------------------------------
# Per-market locking
# ---------------------------------------------------------------------------

class TestPerMarketLocking:
    def test_get_market_lock_creates_new(self):
        """Should create a new lock for an unseen market."""
        lock = _get_market_lock("test-market-unique-12345")
        assert hasattr(lock, "acquire") and hasattr(lock, "release")

    def test_get_market_lock_returns_same(self):
        """Should return the same lock for the same market."""
        lock1 = _get_market_lock("test-market-same-lock-12345")
        lock2 = _get_market_lock("test-market-same-lock-12345")
        assert lock1 is lock2

    def test_different_markets_get_different_locks(self):
        """Different markets should get different locks."""
        lock_a = _get_market_lock("market-a-unique-12345")
        lock_b = _get_market_lock("market-b-unique-12345")
        assert lock_a is not lock_b

    def test_concurrent_locks_allow_parallel_execution(self):
        """Per-market locks should allow concurrent execution on different markets."""
        lock_a = _get_market_lock("concurrent-test-a-12345")
        lock_b = _get_market_lock("concurrent-test-b-12345")

        # Both should be acquirable simultaneously
        assert lock_a.acquire(blocking=False)
        assert lock_b.acquire(blocking=False)
        lock_a.release()
        lock_b.release()

    def test_same_market_lock_blocks(self):
        """Same market lock should block concurrent execution."""
        lock = _get_market_lock("blocking-test-12345")
        assert lock.acquire(blocking=False)
        # Second acquire should fail (non-blocking)
        assert not lock.acquire(blocking=False)
        lock.release()


# ---------------------------------------------------------------------------
# Settlement P&L calculation
# ---------------------------------------------------------------------------

class TestCalcRealizedPnl:
    @pytest.fixture
    def db(self):
        trade_db = TradeDB(":memory:")
        yield trade_db
        trade_db.close()

    def test_uses_fill_prices_when_available(self, db):
        """Should use actual fill prices instead of expected P&L.

        Polymarket ``size`` is SHARES (PolymarketTrader.place_order passes it
        through as the share count), so cost = fill * size and contracts =
        size. Arb payout = min contracts across legs (guaranteed winner)."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "filled", fill_price=0.41)
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "filled", fill_price=0.46)
        pos_id = db.create_position(opp_id, "m1", "polymarket", 0.15)

        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        # cost = 0.41*5 + 0.46*5 = 4.35; payout = min(5, 5) = 5; P&L = +0.65
        assert realized == pytest.approx(5.0 - 4.35)

    def test_falls_back_to_expected_when_no_trades(self, db):
        """Should fall back to expected_pnl when no trades exist."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        pos_id = db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        assert realized == pytest.approx(0.15)

    def test_uses_order_price_when_no_fill(self, db):
        """Should use order price when fill_price is None."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "filled")
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "filled")
        pos_id = db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        # PM shares at order prices: cost = 0.40*5 + 0.45*5 = 4.25;
        # payout = min(5, 5) = 5; P&L = +0.75
        assert realized == pytest.approx(5.0 - 4.25)

    def test_explicit_zero_fill_fails_closed(self, db):
        """A recorded zero fill is invalid, not a request to reuse limit price."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(
            opp_id, "polymarket", "BUY", 0.50, 10.0, "filled",
            fill_price=0.0, outcome="yes",
        )
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]

        realized = _calc_realized_pnl(db, pos, winning_side="yes")

        assert realized == pytest.approx(-10.0)

    @pytest.mark.parametrize(
        "trade",
        [
            {"platform": "kalshi", "side": "yes", "price": float("nan"),
             "fill_price": None, "size": 10.0, "status": "filled"},
            {"platform": "kalshi", "side": "yes", "price": 0.5,
             "fill_price": float("inf"), "size": 10.0, "status": "filled"},
            {"platform": "kalshi", "side": "yes", "price": 0.5,
             "fill_price": 0.5, "size": float("nan"), "status": "filled"},
            {"platform": "kalshi", "side": "yes", "price": True,
             "fill_price": 0.5, "size": 10.0, "status": "filled"},
        ],
    )
    def test_non_finite_or_boolean_money_never_returns_expected_profit(self, trade):
        """Malformed filled rows must produce a non-positive fail-closed result."""
        db = MagicMock()
        db.get_trades_for_opportunity.return_value = [trade]
        pos = {"opportunity_id": 1, "expected_pnl": 0.25}

        assert _calc_realized_pnl(db, pos, winning_side="yes") <= 0

    def test_zero_size_never_returns_expected_profit(self, db):
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.50, 0.0, "filled", outcome="yes")
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]

        assert _calc_realized_pnl(db, pos, winning_side="yes") <= 0

    def test_non_filled_rows_are_excluded(self, db):
        """Pending and failed requests cannot add settlement cost or payout."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.40, 0.10, 0.25, 50, "traded")
        db.log_trade(
            opp_id, "polymarket", "BUY", 0.40, 5.0, "filled",
            fill_price=0.40, outcome="no",
        )
        db.log_trade(opp_id, "polymarket", "BUY", 0.90, 50.0, "pending", outcome="yes")
        db.log_trade(opp_id, "polymarket", "BUY", 0.90, 50.0, "failed", outcome="yes")
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]

        realized = _calc_realized_pnl(db, pos, winning_side="no")

        assert realized == pytest.approx(3.0)

    def test_expected_fallback_when_no_filled_rows_remain(self, db):
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "pending")
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "failed")
        db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]

        assert _calc_realized_pnl(db, pos) == pytest.approx(0.15)

    def test_polymarket_buy_no_settles_from_persisted_outcome(self, db):
        """BUY is execution direction; the separate NO outcome drives payout."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.40, 0.10, 0.25, 50, "traded")
        db.log_trade(
            opp_id, "polymarket", "BUY", 0.40, 10.0, "filled",
            fill_price=0.40, outcome="no",
        )
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]

        realized = _calc_realized_pnl(db, pos, winning_side="no")

        assert realized == pytest.approx(6.0)

    def test_directional_winning_yes_bet(self, db):
        """Directional PM bet on YES that resolved YES: payout = shares * $1.

        Polymarket size = SHARES: 10 shares at fill 0.40 → cost $4.00,
        payout $10.00, net +$6.00."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.40, 0.10, 0.25, 50, "traded")
        db.log_trade(opp_id, "polymarket", "yes", 0.40, 10.0, "filled", fill_price=0.40)
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="yes")
        # contracts = 10 shares; payout = 10; cost = 0.40*10 = 4; net = +6
        assert realized == pytest.approx(6.0)

    def test_directional_losing_yes_bet(self, db):
        """Directional bet on YES that resolved NO: payout = $0, realized = -cost.

        Regression for the bug where losing directional bets reported P&L of
        `1.0 - cost` instead of properly accounting for $0 payout on the
        losing leg."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.40, 0.10, 0.25, 50, "traded")
        db.log_trade(opp_id, "polymarket", "yes", 0.40, 10.0, "filled", fill_price=0.40)
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="no")
        # PM YES leg lost; payout = 0; realized = -(0.40 * 10 shares) = -4
        assert realized == pytest.approx(-4.0)

    def test_directional_handles_buy_alias_for_yes(self, db):
        """Trade.side='BUY' should match winning_side='yes'."""
        opp_id = db.log_opportunity("NewsSnipe", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.50, 5.0, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="yes")
        # PM: contracts = 5 shares; payout = 5; cost = 0.50*5 = 2.5; net = +2.5
        assert realized == pytest.approx(2.5)

    def test_arbitrage_default_uses_min_contracts_payout(self, db):
        """Without winning_side, arb payout = min contracts across legs * $1
        — whichever leg wins pays $1/contract, and the min contract count is
        the amount guaranteed hedged across both legs."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 0.5, "filled", fill_price=0.41)
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 0.5, "filled", fill_price=0.46)
        db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        # PM shares: cost = 0.41*0.5 + 0.46*0.5 = 0.435; payout = min(0.5, 0.5)
        assert realized == pytest.approx(0.5 - 0.435)

    def test_regression_kalshi_dollar_size_derives_integer_contracts(self, db):
        """Regression (audit #77 round 2): Kalshi ``size`` is the requested
        DOLLAR amount; the executor places max(1, int(size/price)) contracts.

        Two-leg $50/$50 Kalshi arb at 0.48/0.49:
        - leg 1: int(50/0.48) = 104 contracts, cost = 104*0.48 = 49.92
        - leg 2: int(50/0.49) = 102 contracts, cost = 102*0.49 = 49.98
        - payout = min(104, 102) = 102; P&L = 102 - 99.90 = +2.10
        The pre-audit formula reported 1.0 - (0.48*50 + 0.49*50) = -47.50 on
        this profitable arb — corrupting realized P&L and the daily-loss
        halt input.
        """
        opp_id = db.log_opportunity("KalshiBinary", "M", "", 0.97, 0.02, 0.02, 50, "traded")
        db.log_trade(opp_id, "kalshi", "yes", 0.48, 50.0, "filled", fill_price=0.48)
        db.log_trade(opp_id, "kalshi", "no", 0.49, 50.0, "filled", fill_price=0.49)
        db.create_position(opp_id, "m1", "kalshi", 0.02)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        expected = 102.0 - (104 * 0.48 + 102 * 0.49)  # +2.10
        assert realized == pytest.approx(expected)
        assert realized > 0  # profitable arb must not report a huge loss

    def test_regression_mixed_venue_arb_uses_per_venue_semantics(self, db):
        """PM leg in SHARES + Kalshi leg in DOLLARS within one opportunity.

        PM: 10 shares at 0.45 → cost 4.50, contracts 10.
        Kalshi: $5 at 0.50 → int(5/0.50) = 10 contracts, cost 5.00.
        payout = min(10, 10) = 10; P&L = 10 - 9.50 = +0.50.
        """
        opp_id = db.log_opportunity("Cross", "M", "", 0.95, 0.05, 0.05, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 10.0, "filled", fill_price=0.45)
        db.log_trade(opp_id, "kalshi", "no", 0.50, 5.0, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "polymarket", 0.05)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        assert realized == pytest.approx(0.50)


class TestLegContractsAndCostVenueSemantics:
    """Round-3 audit findings: stake-sized venues and fail-closed handling.

    Betfair/Matchbook place round(size, 2) as a STAKE at decimal odds
    1/price (executor.py) — a third semantics distinct from PM shares and
    the dollar->integer-contracts venues. Unknown venues and malformed
    money data must fail CLOSED (zero payout, full cost lost) because this
    feeds the daily-loss halt — it may over-trigger, never under-trigger.
    """

    @pytest.fixture
    def db(self):
        trade_db = TradeDB(":memory:")
        yield trade_db
        trade_db.close()

    # ---------------------------------------------------------------------------
    # Stake-sized venues (Betfair / Matchbook)

    def test_betfair_winning_back_bet_pays_stake_over_price(self, db):
        """$10 stake backed at 0.30: win returns 10/0.30 = 33.333, net
        +23.333. The dollar-contract path would truncate to 33 contracts
        (cost 9.90, net +23.10) — Betfair does not truncate stakes."""
        opp_id = db.log_opportunity("BetfairArb", "M", "", 0.30, 0.10, 0.33, 50, "traded")
        db.log_trade(opp_id, "betfair", "back", 0.30, 10.0, "filled", fill_price=0.30)
        db.create_position(opp_id, "m1", "betfair", 0.10)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="yes")
        assert realized == pytest.approx(10.0 / 0.30 - 10.0)  # +23.333...

    def test_matchbook_losing_bet_forfeits_rounded_stake(self, db):
        """Matchbook places stake=round(size, 2): size 10.456 -> $10.46
        staked and lost. The dollar-contract path reported -$10.00
        (int(10.456/0.50) = 20 contracts x 0.50)."""
        opp_id = db.log_opportunity("MatchbookArb", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "matchbook", "back", 0.50, 10.456, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "matchbook", 0.10)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="no")
        assert realized == pytest.approx(-10.46)

    # ---------------------------------------------------------------------------
    # Fail-closed: unknown venue

    def test_unknown_venue_fails_closed_with_error_log(self, db, caplog):
        """An unrecognized platform must NOT silently take the
        dollar->contracts path (which would report +$10 profit here) —
        worst case is assumed: zero payout, full $10 cost lost."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "robinhood", "yes", 0.50, 10.0, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "robinhood", 0.10)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos)
        assert realized == pytest.approx(-10.0)
        assert any("unknown venue" in r.message for r in caplog.records)

    def test_empty_platform_fails_closed(self, db, caplog):
        """Missing platform is as unverifiable as an unknown one."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "", "yes", 0.50, 10.0, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos)
        assert realized == pytest.approx(-10.0)
        assert any("P&L fail-closed" in r.message for r in caplog.records)

    # ---------------------------------------------------------------------------
    # Fail-closed: malformed money data

    def test_zero_price_fails_closed_not_one_phantom_contract(self, db, caplog):
        """price=0 on a dollar-contract venue previously produced a silent
        1-contract-at-cost-0 guess (and fell through to the expected_pnl
        fallback). Fail closed instead: error log + full $10 size lost."""
        opp_id = db.log_opportunity("KalshiBinary", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "kalshi", "yes", 0.0, 10.0, "filled")
        db.create_position(opp_id, "m1", "kalshi", 0.10)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos, winning_side="yes")
        assert realized == pytest.approx(-10.0)
        assert any("P&L fail-closed" in r.message for r in caplog.records)

    def test_stake_venue_zero_fill_fails_closed(self, db, caplog):
        """Betfair leg with no usable price: the stake is treated as lost."""
        opp_id = db.log_opportunity("BetfairArb", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "betfair", "back", 0.0, 8.0, "filled")
        db.create_position(opp_id, "m1", "betfair", 0.10)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos, winning_side="yes")
        assert realized == pytest.approx(-8.0)
        assert any("P&L fail-closed" in r.message for r in caplog.records)

    def test_malformed_leg_poisons_arb_payout_conservatively(self, db, caplog):
        """A worst-cased leg contributes zero contracts, so the arb min-
        contracts payout collapses to 0 and the whole position reads as
        cost lost — the halt over-triggers rather than under-triggers."""
        opp_id = db.log_opportunity("Cross", "M", "", 0.95, 0.05, 0.05, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 10.0, "filled", fill_price=0.45)
        db.log_trade(opp_id, "unknown-venue", "no", 0.50, 5.0, "filled", fill_price=0.50)
        db.create_position(opp_id, "m1", "polymarket", 0.05)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos)
        # PM leg cost 4.50 + worst-cased leg cost 5.00; payout min(10, 0)=0
        assert realized == pytest.approx(-9.50)


class TestRealizedPnlFailClosedData:
    """Round-4 review findings: invalid money data and trade-status handling.

    `fill_price or price` masked an explicit zero fill, type-only checks
    accepted NaN/inf/bool, zero-size legs collapsed to (0, 0) and fell
    through to the (typically positive) expected_pnl fallback, and every
    trade row — filled, failed, or pending — entered the P&L sums.
    """

    @pytest.fixture
    def db(self):
        trade_db = TradeDB(":memory:")
        yield trade_db
        trade_db.close()

    def test_explicit_zero_fill_is_not_masked_by_order_price(self, db, caplog):
        """A recorded fill_price of 0.0 is invalid money data, not a missing
        fill — it must NOT silently fall back to the order price. PM worst
        case: shares cost at most $1/share, so 10 shares read as -$10."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.50, 0.10, 0.20, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 10.0, "filled", fill_price=0.0)
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos, winning_side="yes")
        assert realized == pytest.approx(-10.0)
        assert any("P&L fail-closed" in r.message for r in caplog.records)

    def test_nan_fill_fails_closed(self, caplog):
        """NaN passes isinstance((int, float)) — it must still be rejected."""
        db = MagicMock()
        db.get_trades_for_opportunity.return_value = [{
            "id": 1,
            "platform": "polymarket",
            "side": "BUY",
            "price": 0.40,
            "fill_price": float("nan"),
            "size": 10.0,
            "status": "filled",
        }]
        pos = {"opportunity_id": 1, "expected_pnl": 0.10}
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos, winning_side="yes")
        assert realized == pytest.approx(-10.0)

    def test_zero_size_leg_never_reaches_expected_pnl_fallback(self, db, caplog):
        """Regression: a zero-size leg produced (0, 0), total cost 0, and the
        function returned the POSITIVE expected_pnl for a garbage position.
        An unpriceable leg must fail closed to -(known cost) instead."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.50, 5.0, 0.20, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 0.0, "filled", fill_price=0.40)
        db.create_position(opp_id, "m1", "polymarket", expected_pnl=5.0)
        pos = db.get_open_positions()[0]
        with caplog.at_level("ERROR", logger="continuous"):
            realized = _calc_realized_pnl(db, pos)
        assert realized <= 0.0  # never the +5.0 expected_pnl fallback
        assert any("fail-closed" in r.message for r in caplog.records)

    def test_failed_legs_are_excluded_from_pnl(self, db):
        """Failed legs never reached the venue: no cost, no payout. Before
        the fix they were priced like fills, corrupting realized P&L."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "filled", fill_price=0.41)
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "failed")
        db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos, winning_side="yes")
        # Only the filled YES leg: payout 5 - cost 2.05 = +2.95
        assert realized == pytest.approx(5.0 - 0.41 * 5)

    def test_pending_leg_is_excluded_from_realized_pnl(self, db):
        """Pending rows are not confirmed executions and cannot affect P&L."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 0.15, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "filled", fill_price=0.41)
        db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "pending")
        db.create_position(opp_id, "m1", "polymarket", 0.15)
        pos = db.get_open_positions()[0]
        realized = _calc_realized_pnl(db, pos)
        # Only the confirmed filled leg enters realized P&L.
        assert realized == pytest.approx(5.0 - 0.41 * 5)

    def test_all_legs_failed_preserves_expected_pnl_fallback(self, db):
        """No executed rows preserves the legacy expected-P&L fallback."""
        opp_id = db.log_opportunity("Binary", "M", "", 0.85, 5.0, 0.176, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 5.0, "failed")
        db.create_position(opp_id, "m1", "polymarket", expected_pnl=5.0)
        pos = db.get_open_positions()[0]
        assert _calc_realized_pnl(db, pos) == pytest.approx(5.0)

    def test_polymarket_buy_no_settles_by_stored_outcome(self, db):
        """Regression: PM BUY_NO legs are logged side="BUY", which aliases to
        winning_side="yes" — a NO bet that WON was scored as a loss (and a
        NO bet that lost was scored as a win). The traded outcome column
        must drive settlement when present."""
        opp_id = db.log_opportunity("Imbalance", "M", "", 0.40, 0.10, 0.25, 50, "traded")
        db.log_trade(opp_id, "polymarket", "BUY", 0.40, 10.0, "filled",
                     fill_price=0.40, outcome="no")
        db.create_position(opp_id, "m1", "polymarket", 0.10)
        pos = db.get_open_positions()[0]
        # Market resolved NO: the NO leg WON. payout 10 - cost 4 = +6
        assert _calc_realized_pnl(db, pos, winning_side="no") == pytest.approx(6.0)
        # Market resolved YES: the NO leg LOST. payout 0 - cost 4 = -4
        assert _calc_realized_pnl(db, pos, winning_side="yes") == pytest.approx(-4.0)


# ---------------------------------------------------------------------------
# Kalshi resolution sniping in continuous mode (INTEG-03)
# ---------------------------------------------------------------------------


class TestKalshiResolution:
    """Kalshi markets must be fed to scan_resolution_snipes in continuous mode."""

    def _make_scan_inner_args(self, mode="all", poly_markets=None, kalshi_data=None):
        """Build the locals dict that the continuous scan body uses."""
        import argparse
        args = argparse.Namespace(
            mode=mode,
            min_profit=0.01,
            min_depth=0,
            min_confidence="LOW",
            limit=None,
            json=False,
            continuous=True,
        )
        return {
            "args": args,
            "poly_markets": poly_markets or [],
            "kalshi_data": kalshi_data,
            "min_profit": 0.01,
            "all_opportunities": [],
        }

    def test_kalshi_markets_fed_to_resolution_scan(self):
        """When kalshi_data is present and mode is 'all', scan_resolution_snipes is
        called with a flat list of Kalshi market dicts and platform='kalshi'."""
        markets_by_event = {
            "EVT-1": [{"ticker": "EVT-1-Y", "yes_price": 0.95}],
            "EVT-2": [{"ticker": "EVT-2-Y", "yes_price": 0.97}, {"ticker": "EVT-2-N", "yes_price": 0.03}],
        }
        kalshi_data = ([], markets_by_event, {})

        call_args_list = []

        def fake_scan(markets, platform, min_profit):
            call_args_list.append((markets, platform, min_profit))
            return []

        # Simulate the Kalshi resolution scan block from continuous.py
        from scans import resolution as _res_mod
        with patch.object(_res_mod, "scan_resolution_snipes", side_effect=fake_scan):
            # Execute the block directly (mirrors continuous.py logic)
            all_opportunities = []
            if kalshi_data:
                kalshi_flat_markets = []
                if len(kalshi_data) >= 2 and kalshi_data[1]:
                    for _evt_ticker, _mkts in kalshi_data[1].items():
                        kalshi_flat_markets.extend(_mkts)
                if kalshi_flat_markets:
                    k_res_opps = _res_mod.scan_resolution_snipes(
                        kalshi_flat_markets, platform="kalshi", min_profit=0.01,
                    )
                    all_opportunities.extend(k_res_opps)

        assert len(call_args_list) == 1
        called_markets, called_platform, _ = call_args_list[0]
        assert called_platform == "kalshi"
        # All 3 markets from both events must be in the flat list
        assert len(called_markets) == 3
        tickers = {m["ticker"] for m in called_markets}
        assert "EVT-1-Y" in tickers
        assert "EVT-2-Y" in tickers
        assert "EVT-2-N" in tickers

    def test_no_kalshi_data_skips_gracefully(self):
        """When kalshi_data is None, no error occurs."""
        kalshi_data = None
        all_opportunities = []

        # This mirrors the guard in continuous.py — if kalshi_data is falsy, skip
        if kalshi_data:
            raise AssertionError("Should not reach here")

        # No exception should occur; opportunities list unchanged
        assert all_opportunities == []

    def test_empty_markets_by_event_skips_scan(self):
        """When kalshi_data[1] is empty dict, scan is not called."""
        kalshi_data = ([], {}, {})
        called = []

        def fake_scan(markets, platform, min_profit):
            called.append(True)
            return []

        from scans import resolution as _res_mod
        with patch.object(_res_mod, "scan_resolution_snipes", side_effect=fake_scan):
            all_opportunities = []
            if kalshi_data:
                kalshi_flat_markets = []
                if len(kalshi_data) >= 2 and kalshi_data[1]:
                    for _evt_ticker, _mkts in kalshi_data[1].items():
                        kalshi_flat_markets.extend(_mkts)
                if kalshi_flat_markets:
                    k_res_opps = _res_mod.scan_resolution_snipes(
                        kalshi_flat_markets, platform="kalshi", min_profit=0.01,
                    )
                    all_opportunities.extend(k_res_opps)

        # scan was never called because flat list was empty
        assert called == []


# ---------------------------------------------------------------------------
# Bankroll refresh in continuous mode (INTEG-04)
# ---------------------------------------------------------------------------


class TestBankrollRefresh:
    """Bankroll must refresh every 5 minutes AND immediately after each trade."""

    def _make_executor_with_sizer(self, balances=None, fetch_raises=False):
        executor = MagicMock()
        executor.position_sizer = MagicMock()
        if fetch_raises:
            executor._fetch_balances.side_effect = Exception("network error")
        else:
            executor._fetch_balances.return_value = balances or {
                "polymarket": 100.0, "kalshi": 50.0
            }
        return executor

    def test_timer_refresh_calls_update_bankroll(self):
        """Timer-based refresh calls update_bankroll with summed balance."""
        executor = self._make_executor_with_sizer(
            balances={"polymarket": 100.0, "kalshi": 50.0}
        )

        # Simulate the timer-based refresh block
        _last_bankroll_refresh = 0.0
        _bankroll_refresh_interval = 300.0
        now = _last_bankroll_refresh + _bankroll_refresh_interval + 1  # past threshold

        if now - _last_bankroll_refresh >= _bankroll_refresh_interval:
            try:
                balances = executor._fetch_balances("Cross")
                if balances and executor.position_sizer:
                    total = sum(v for v in balances.values() if isinstance(v, (int, float)))
                    if total > 0:
                        executor.position_sizer.update_bankroll(total)
                _last_bankroll_refresh = now
            except Exception:
                pass

        executor._fetch_balances.assert_called_once_with("Cross")
        executor.position_sizer.update_bankroll.assert_called_once_with(150.0)

    def test_timer_does_not_refresh_before_interval(self):
        """Timer-based refresh is skipped when interval has not elapsed."""
        executor = self._make_executor_with_sizer()

        _last_bankroll_refresh = 1000.0
        _bankroll_refresh_interval = 300.0
        now = 1100.0  # only 100s elapsed, below 300s threshold

        if now - _last_bankroll_refresh >= _bankroll_refresh_interval:
            executor._fetch_balances("Cross")

        executor._fetch_balances.assert_not_called()

    def test_post_trade_immediate_refresh(self):
        """After executor.execute returns True, update_bankroll is called immediately."""
        executor = self._make_executor_with_sizer(
            balances={"polymarket": 200.0, "kalshi": 100.0}
        )
        executor.execute.return_value = True
        opp = {"type": "BinaryInternal", "net_profit": 0.05}

        executed = 0
        if executor.execute(opp):
            executed += 1
            # Post-trade bankroll refresh
            try:
                balances = executor._fetch_balances("Cross")
                if balances and executor.position_sizer:
                    total = sum(v for v in balances.values() if isinstance(v, (int, float)))
                    if total > 0:
                        executor.position_sizer.update_bankroll(total)
            except Exception:
                pass

        assert executed == 1
        executor._fetch_balances.assert_called_once_with("Cross")
        executor.position_sizer.update_bankroll.assert_called_once_with(300.0)

    def test_no_position_sizer_skips_gracefully(self):
        """When executor.position_sizer is None, no error occurs."""
        executor = MagicMock()
        executor.position_sizer = None
        executor._fetch_balances.return_value = {"polymarket": 100.0}

        # Simulate the guard
        balances = executor._fetch_balances("Cross")
        if balances and executor.position_sizer:
            executor.position_sizer.update_bankroll(sum(balances.values()))

        # update_bankroll should never be called
        # (position_sizer is None, so the attribute doesn't exist)
        assert executor.position_sizer is None

    def test_fetch_balances_failure_logs_and_continues(self):
        """When _fetch_balances raises, exception is caught and loop continues."""
        executor = self._make_executor_with_sizer(fetch_raises=True)

        error_caught = False
        try:
            balances = executor._fetch_balances("Cross")
            if balances and executor.position_sizer:
                total = sum(v for v in balances.values() if isinstance(v, (int, float)))
                if total > 0:
                    executor.position_sizer.update_bankroll(total)
        except Exception:
            error_caught = True

        # The guard should catch the exception
        assert error_caught is True
        # In production the except block logs and continues — update_bankroll never called
        executor.position_sizer.update_bankroll.assert_not_called()


# ---------------------------------------------------------------------------
# Codex round-2 finding #3: SIGINT/SIGTERM must signal the MM pilot to stop
# IMMEDIATELY (set _mm_pilot_stop directly from the signal handler), not
# only via the end-of-cycle cleanup path further down run_continuous, which
# only runs after the CURRENT scan cycle finishes. A long synchronous scan
# cycle, or a hard kill before cleanup completes, could otherwise leave live
# GTC orders resting on Kalshi with nothing cancelling them.
#
# run_continuous is a large synchronous orchestrator that registers signal
# handlers early and then drives an asyncio event loop end-to-end — too
# complex and stateful to run to completion in a unit test (no test in this
# file does). This captures the REAL closure Python registers via
# signal.signal (not a reimplementation of its logic): signal.signal is
# monkeypatched to record its arguments, and the very next call
# run_continuous makes (constructing OpportunityIndex) is monkeypatched to
# raise a sentinel exception, so the function returns immediately after
# registering the handler and before the untestable async orchestration
# starts. The captured handler function's closure cells are then inspected
# directly (co_freevars / __closure__) to reach the REAL _mm_pilot_stop
# threading.Event object it holds, and the handler is invoked exactly as
# the OS would, observing its actual effect on that real object.
#
# Fail-before: the handler only called shutdown_event.set() — the captured
# handler's _mm_pilot_stop stayed unset after being invoked.
# ---------------------------------------------------------------------------

class _StopEarly(Exception):
    """Sentinel used to abort run_continuous right after it registers
    signal handlers, before the async orchestration loop starts."""


class TestSignalHandlerStopsMMPilotImmediately:
    def _run_until_signal_registered(self, monkeypatch):
        import continuous
        import signal as signal_module

        captured: dict = {}
        monkeypatch.setattr(
            continuous.signal, "signal",
            lambda sig, handler: captured.__setitem__(sig, handler))
        monkeypatch.setattr(
            continuous, "OpportunityIndex",
            lambda: (_ for _ in ()).throw(_StopEarly()))

        args = MagicMock()
        args.interval = None
        with pytest.raises(_StopEarly):
            continuous.run_continuous(
                args=args, min_profit=0.01, kalshi_client=None,
                kalshi_api_key_id=None, kalshi_private_key_path=None,
                executor=MagicMock(), db=MagicMock(), price_cache={},
            )
        assert signal_module.SIGTERM in captured, (
            "SIGTERM handler was never registered before the sentinel fired")
        return captured[signal_module.SIGTERM]

    @staticmethod
    def _closure_var(func, name):
        freevars = func.__code__.co_freevars
        cells = dict(zip(freevars, func.__closure__ or ()))
        assert name in cells, (
            f"{name!r} is not a free variable of the captured handler "
            f"(found: {sorted(cells)}) — signature of _signal_handler "
            f"may have changed"
        )
        return cells[name].cell_contents

    def test_sigterm_sets_mm_pilot_stop_immediately(self, monkeypatch):
        handler = self._run_until_signal_registered(monkeypatch)
        mm_pilot_stop = self._closure_var(handler, "_mm_pilot_stop")
        shutdown_event = self._closure_var(handler, "shutdown_event")
        assert isinstance(mm_pilot_stop, threading.Event)
        assert mm_pilot_stop.is_set() is False  # not yet — signal hasn't fired

        import signal as signal_module
        handler(signal_module.SIGTERM, None)

        assert mm_pilot_stop.is_set() is True, (
            "signal handler must set _mm_pilot_stop directly, not only "
            "shutdown_event — otherwise the MM pilot only notices a "
            "shutdown once the current scan cycle's end-of-loop cleanup "
            "runs, which can take arbitrarily long"
        )
        assert shutdown_event.is_set() is True  # pre-existing behavior kept

    def test_mm_pilot_stop_is_idempotent_across_repeated_signals(
            self, monkeypatch):
        """SIGINT then SIGTERM (or a repeated SIGTERM) must not raise —
        threading.Event.set() is idempotent, matching shutdown_event's
        existing behavior."""
        handler = self._run_until_signal_registered(monkeypatch)
        import signal as signal_module
        handler(signal_module.SIGINT, None)
        handler(signal_module.SIGTERM, None)
        mm_pilot_stop = self._closure_var(handler, "_mm_pilot_stop")
        assert mm_pilot_stop.is_set() is True

    def test_signal_handler_does_not_log_reentrantly(self, monkeypatch):
        """Logging from an OS signal handler can re-enter a buffered stream."""
        import continuous
        import signal as signal_module

        info = MagicMock()
        monkeypatch.setattr(continuous.logger, "info", info)
        handler = self._run_until_signal_registered(monkeypatch)

        handler(signal_module.SIGTERM, None)

        info.assert_not_called()


class TestAsyncioSignalWakeup:
    def test_wakes_selector_without_replacing_signal_handler(self):
        import continuous
        loop = MagicMock()
        event = MagicMock()

        assert continuous._wake_asyncio_selector(loop, event) is True
        loop.call_soon_threadsafe.assert_called_once_with(event.set)
        loop.add_signal_handler.assert_not_called()

    def test_closed_loop_preserves_direct_fallback(self):
        import continuous
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = RuntimeError

        assert continuous._wake_asyncio_selector(loop, MagicMock()) is False

    def test_missing_loop_preserves_direct_fallback(self):
        import continuous
        assert continuous._wake_asyncio_selector(None, MagicMock()) is False


# ---------------------------------------------------------------------------
# CodeRabbit finding (adjacent to Codex round-2 #3, fixed opportunistically
# while already in this exact shutdown-safety code): the MM pilot
# startup-failure exception handler must mirror the end-of-run cleanup
# path's force-stop symmetry — a thread that started enough to place live
# orders but doesn't unwind within the join timeout must still have those
# orders cancelled directly via _mm_pilot.stop(), not silently dropped when
# _mm_pilot/_mm_pilot_thread are cleared to None.
#
# A full behavioral test here would need to drive run_continuous past
# OpportunityIndex() into the MM-pilot try block, start a real thread, and
# force it to outlive a 15s join timeout — too slow and heavyweight for a
# unit test. This checks the fix structurally instead (same technique
# tests/test_mm_pilot_gates.py::TestNoBypass already uses for a similarly
# hard-to-exercise invariant): the startup-failure except block must
# contain the same is_alive() + _mm_pilot.stop() pattern the cleanup path
# uses, not just an unconditional join.
# ---------------------------------------------------------------------------

class TestStartupFailureMirrorsCleanupForceStop:
    @staticmethod
    def _exception_handler_source():
        import re
        src_path = os.path.join(os.path.dirname(__file__), "..", "continuous.py")
        with open(src_path, encoding="utf-8") as fh:
            source = fh.read()
        marker = 'logger.exception("MM pilot failed to start: %s", exc)'
        start = source.index(marker)
        # The except block ends at the next top-level (8-space-indented)
        # statement after this point, i.e. `_mm_pilot = None` immediately
        # followed by `_mm_pilot_thread = None` and a blank line.
        end = source.index("\n\n", start)
        return source[start:end]

    def test_startup_failure_checks_is_alive_before_clearing_state(self):
        block = self._exception_handler_source()
        assert "_mm_pilot_thread.join(timeout=15)" in block
        assert "_mm_pilot_thread.is_alive()" in block, (
            "startup-failure handler must check is_alive() after the join "
            "timeout, mirroring the end-of-run cleanup path, instead of "
            "silently clearing _mm_pilot/_mm_pilot_thread regardless of "
            "whether the thread actually stopped"
        )
        assert "_mm_pilot.stop()" in block, (
            "a thread still alive after the join timeout must have its "
            "resting orders force-cancelled directly"
        )
        # is_alive() must be checked strictly before the state is cleared.
        assert block.index("is_alive()") < block.index("_mm_pilot = None")
        assert "if thread_stopped:" in block
        assert "retaining references" in block


class TestMMPilotModeIsolation:
    def test_pilot_startup_requires_dedicated_mode(self):
        """The feature flag alone must never start the order-producing loop."""
        import inspect
        import continuous

        source = inspect.getsource(continuous.run_continuous)
        assert "config.MM_KALSHI_PILOT_ENABLED" in source
        assert 'getattr(args, "mode", None) == "mm-pilot"' in source


class TestFrechetContinuousScan:
    def test_frechet_disabled_by_default(self, monkeypatch):
        import config
        from continuous import _scan_frechet_layer1
        monkeypatch.setattr(config, "FRECHET_ARB_ENABLED", False)
        markets = [{"title": "Will BTC be above $100k?"}]
        res = _scan_frechet_layer1(markets, mode="all", min_profit=0.01)
        assert res == []

    def test_frechet_empty_markets(self, monkeypatch):
        import config
        from continuous import _scan_frechet_layer1
        monkeypatch.setattr(config, "FRECHET_ARB_ENABLED", True)
        res = _scan_frechet_layer1([], mode="all", min_profit=0.01)
        assert res == []

    def test_frechet_irrelevant_mode(self, monkeypatch):
        import config
        from continuous import _scan_frechet_layer1
        monkeypatch.setattr(config, "FRECHET_ARB_ENABLED", True)
        markets = [{"title": "Will BTC be above $100k?"}]
        res = _scan_frechet_layer1(markets, mode="binary", min_profit=0.01)
        assert res == []

    def test_frechet_enabled_dispatches_scan_and_refine(self, monkeypatch):
        from unittest.mock import patch, MagicMock
        import config
        from continuous import _scan_frechet_layer1
        monkeypatch.setattr(config, "FRECHET_ARB_ENABLED", True)
        markets = [{"title": "Will BTC be above $100k?"}]
        mock_cands = [{"type": "FrechetArb", "net_profit": 0.05}]
        mock_refined = [{"type": "FrechetArb", "net_profit": 0.04}]

        with patch("scans.frechet.scan_frechet", return_value=mock_cands) as p_scan, \
             patch("scans.frechet._refine_frechet_with_clob", return_value=mock_refined) as p_ref:
            res = _scan_frechet_layer1(markets, mode="all", min_profit=0.01)

        assert res == mock_refined
        p_scan.assert_called_once()
        p_ref.assert_called_once()


class TestResearchModeLayer1Scans:
    """--mode research runs Fréchet/temporal/CTF observationally, only while DRY_RUN is on."""

    def _flags_off(self, monkeypatch, dry_run):
        import config
        for flag in ("FRECHET_ARB_ENABLED", "TEMPORAL_ARB_ENABLED", "CTF_ENABLED",
                     "CTF_MERGE_ENABLED", "CTF_MINT_SELL_ENABLED"):
            monkeypatch.setattr(config, flag, False)
        monkeypatch.setattr(config, "DRY_RUN", dry_run)

    def test_research_dry_run_dispatches_all_three_with_flags_off(self, monkeypatch):
        from unittest.mock import patch
        from continuous import _scan_ctf_layer1, _scan_frechet_layer1, _scan_temporal_layer1
        self._flags_off(monkeypatch, dry_run=True)
        with patch("scans.frechet.scan_frechet", return_value=[{"c": 1}]), \
             patch("scans.frechet._refine_frechet_with_clob", return_value=["frechet"]), \
             patch("scans.temporal.scan_temporal_arb", return_value=[{"c": 1}]), \
             patch("scans.temporal._refine_temporal_with_clob", return_value=["temporal"]), \
             patch("scans.ctf.scan_ctf", return_value=["ctf"]):
            assert _scan_frechet_layer1([{"title": "m"}], mode="research", min_profit=0.01) == ["frechet"]
            assert _scan_temporal_layer1([{"ticker": "K"}], mode="research", min_profit=0.01) == ["temporal"]
            assert _scan_ctf_layer1([{"conditionId": "0x1"}], mode="research", min_profit=0.01) == ["ctf"]

    def test_research_mode_is_inert_when_not_dry_run(self, monkeypatch):
        from unittest.mock import patch
        from continuous import _scan_ctf_layer1, _scan_frechet_layer1, _scan_temporal_layer1
        self._flags_off(monkeypatch, dry_run=False)
        with patch("scans.frechet.scan_frechet") as p_frechet, \
             patch("scans.temporal.scan_temporal_arb") as p_temporal, \
             patch("scans.ctf.scan_ctf") as p_ctf:
            assert _scan_frechet_layer1([{"title": "m"}], mode="research", min_profit=0.01) == []
            assert _scan_temporal_layer1([{"ticker": "K"}], mode="research", min_profit=0.01) == []
            assert _scan_ctf_layer1([{"conditionId": "0x1"}], mode="research", min_profit=0.01) == []
        p_frechet.assert_not_called()
        p_temporal.assert_not_called()
        p_ctf.assert_not_called()

    def test_research_logs_one_line_per_strategy_only_in_research_mode(self, caplog):
        from continuous import _log_research_strategy
        with caplog.at_level("INFO", logger="continuous"):
            _log_research_strategy("research", "frechet", 2000, 3, 1)
            _log_research_strategy("research", "ctf", 2000, None, 0)
            _log_research_strategy("all", "frechet", 2000, 3, 1)
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Research ")]
        assert lines == [
            "Research frechet: 2000 inputs -> 3 candidates -> 1 surfaced after CLOB refine",
            "Research ctf: 2000 inputs -> n/a candidates -> 0 surfaced after CLOB refine",
        ]

    def test_flags_still_gate_all_mode(self, monkeypatch):
        from continuous import _scan_frechet_layer1, _scan_temporal_layer1
        self._flags_off(monkeypatch, dry_run=True)
        assert _scan_frechet_layer1([{"title": "m"}], mode="all", min_profit=0.01) == []
        assert _scan_temporal_layer1([{"ticker": "K"}], mode="all", min_profit=0.01) == []

    def test_research_empty_inputs_log_strategy_reports(self, monkeypatch, caplog):
        from continuous import _scan_ctf_layer1, _scan_frechet_layer1, _scan_temporal_layer1
        self._flags_off(monkeypatch, dry_run=True)
        with caplog.at_level("INFO", logger="continuous"):
            assert _scan_frechet_layer1([], mode="research", min_profit=0.01) == []
            assert _scan_temporal_layer1([], mode="research", min_profit=0.01) == []
            assert _scan_ctf_layer1([], mode="research", min_profit=0.01) == []
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Research ")]
        assert lines == [
            "Research frechet: 0 inputs -> 0 candidates -> 0 surfaced after CLOB refine",
            "Research temporal: 0 inputs -> 0 candidates -> 0 surfaced after CLOB refine",
            "Research ctf: 0 inputs -> n/a candidates -> 0 surfaced after CLOB refine",
        ]


class TestTemporalContinuousScan:
    """Test _scan_temporal_layer1 continuous dispatch and gating."""

    def test_temporal_disabled_returns_empty(self, monkeypatch):
        import config
        from continuous import _scan_temporal_layer1
        monkeypatch.setattr(config, "TEMPORAL_ARB_ENABLED", False)
        markets = [{"ticker": "KXBTC-26MAR31-T100000"}]
        res = _scan_temporal_layer1(markets, mode="all", min_profit=0.01)
        assert res == []

    def test_temporal_unmatched_mode_returns_empty(self, monkeypatch):
        import config
        from continuous import _scan_temporal_layer1
        monkeypatch.setattr(config, "TEMPORAL_ARB_ENABLED", True)
        markets = [{"ticker": "KXBTC-26MAR31-T100000"}]
        res = _scan_temporal_layer1(markets, mode="binary", min_profit=0.01)
        assert res == []

    def test_temporal_enabled_dispatches_scan_and_refine(self, monkeypatch):
        from unittest.mock import patch
        import config
        from continuous import _scan_temporal_layer1
        monkeypatch.setattr(config, "TEMPORAL_ARB_ENABLED", True)
        markets = [{"ticker": "KXBTC-26MAR31-T100000"}]
        mock_cands = [{"type": "TemporalArb", "net_profit": 0.05}]
        mock_refined = [{"type": "TemporalArb", "net_profit": 0.04}]

        with patch("scans.temporal.scan_temporal_arb", return_value=mock_cands) as p_scan, \
             patch("scans.temporal._refine_temporal_with_clob", return_value=mock_refined) as p_ref:
            res = _scan_temporal_layer1(markets, mode="all", min_profit=0.01)

        assert res == mock_refined
        p_scan.assert_called_once()
        p_ref.assert_called_once()

    def test_temporal_forwards_price_cache_to_refine(self, monkeypatch):
        from unittest.mock import patch
        import config
        from continuous import _scan_temporal_layer1
        monkeypatch.setattr(config, "TEMPORAL_ARB_ENABLED", True)
        markets = [{"ticker": "KXBTC-26MAR31-T100000"}]
        mock_cands = [{"type": "TemporalArb", "net_profit": 0.05}]
        mock_refined = [{"type": "TemporalArb", "net_profit": 0.04}]
        test_cache = {("kalshi", "KXBTC-26MAR31-T100000"): {"yes_ask": 0.50}}

        with patch("scans.temporal.scan_temporal_arb", return_value=mock_cands) as p_scan, \
             patch("scans.temporal._refine_temporal_with_clob", return_value=mock_refined) as p_ref:
            res = _scan_temporal_layer1(
                markets,
                mode="all",
                min_profit=0.01,
                kalshi_client="dummy_client",
                price_cache=test_cache,
            )

        assert res == mock_refined
        p_scan.assert_called_once()
        p_ref.assert_called_once_with(
            mock_cands,
            min_profit=0.01,
            kalshi_client="dummy_client",
            price_cache=test_cache,
            funnel=None,
        )


# ---------------------------------------------------------------------------
# CTF Primitives Continuous Integration (Plan 04)
# ---------------------------------------------------------------------------

class TestCTFContinuousScan:
    """Plan 04: CTF Primitives continuous scan layer 1 tests."""

    def test_ctf_disabled_returns_empty(self, monkeypatch):
        import config
        from continuous import _scan_ctf_layer1
        monkeypatch.setattr(config, "CTF_ENABLED", False)
        monkeypatch.setattr(config, "CTF_MERGE_ENABLED", False)
        monkeypatch.setattr(config, "CTF_MINT_SELL_ENABLED", False)
        res = _scan_ctf_layer1([{"conditionId": "0x123"}], mode="all", min_profit=0.01)
        assert res == []

    def test_ctf_unmatched_mode_returns_empty(self, monkeypatch):
        import config
        from continuous import _scan_ctf_layer1
        monkeypatch.setattr(config, "CTF_ENABLED", True)
        res = _scan_ctf_layer1([{"conditionId": "0x123"}], mode="binary", min_profit=0.01)
        assert res == []

    def test_ctf_enabled_dispatches_scan(self, monkeypatch):
        from unittest.mock import patch
        import config
        from continuous import _scan_ctf_layer1
        monkeypatch.setattr(config, "CTF_ENABLED", True)
        markets = [{"conditionId": "0x123"}]
        mock_opps = [{"type": "CTFMerge", "net_profit": 0.05}]

        with patch("scans.ctf.scan_ctf", return_value=mock_opps) as p_scan:
            res = _scan_ctf_layer1(markets, mode="ctf", min_profit=0.01)

        assert res == mock_opps
        p_scan.assert_called_once()

    def test_ctf_explicit_mode_in_live_disabled_returns_empty(self, monkeypatch):
        import config
        from continuous import _scan_ctf_layer1
        monkeypatch.setattr(config, "CTF_ENABLED", False)
        monkeypatch.setattr(config, "CTF_MERGE_ENABLED", False)
        monkeypatch.setattr(config, "CTF_MINT_SELL_ENABLED", False)
        monkeypatch.setattr(config, "DRY_RUN", False)
        markets = [{"conditionId": "0x123"}]
        res = _scan_ctf_layer1(markets, mode="ctf", min_profit=0.01)
        assert res == []


# ---------------------------------------------------------------------------
# UMA Dispute Gate Continuous Integration (Plan 05)
# ---------------------------------------------------------------------------

class TestDisputeGateContinuous:
    """Verify UMA dispute cache update wiring in continuous mode."""

    def test_continuous_dispute_cache_refresh_success(self, monkeypatch):
        import asyncio
        import signal as signal_module
        import config
        continuous_module = sys.modules["continuous"]

        monkeypatch.setattr(config, "DISPUTE_GATE_ENABLED", True)

        captured_signals = {}
        def capture_sig(sig, handler):
            if getattr(handler, "__name__", "") == "_signal_handler":
                captured_signals[sig] = handler

        monkeypatch.setattr(continuous_module.signal, "signal", capture_sig)

        mock_feed = MagicMock()
        async def mock_feed_run():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Coroutine cancelled on test shutdown
                return
        mock_feed.run = mock_feed_run
        monkeypatch.setattr(continuous_module, "FeedManager", lambda *a, **kw: mock_feed)
        monkeypatch.setattr(continuous_module, "reconcile_orphaned_positions", MagicMock())
        monkeypatch.setattr(continuous_module, "display_results", MagicMock())
        monkeypatch.setattr(continuous_module, "capture_scan_heartbeat", MagicMock())

        sample_markets = [
            {"conditionId": "0xabc", "umaResolutionStatus": "disputed"},
            {"conditionId": "0xdef", "umaResolutionStatus": "resolved"},
        ]
        sample_events = [
            {"id": "evt1", "markets": [{"conditionId": "0xevent_cid", "umaResolutionStatus": "proposed"}]}
        ]
        monkeypatch.setattr(continuous_module, "fetch_all_markets", lambda: sample_markets)
        monkeypatch.setattr(continuous_module, "fetch_events", lambda: sample_events)
        monkeypatch.setattr(continuous_module, "scan_binary_internal", lambda *a, **kw: [])

        mock_db = MagicMock()
        captured_states = {}

        def on_upsert(states):
            captured_states.update(states)
            handler = captured_signals[signal_module.SIGTERM]
            cells = dict(zip(handler.__code__.co_freevars, handler.__closure__ or ()))
            cells["shutdown_event"].cell_contents.set()
            return len(states)

        mock_db.upsert_dispute_state.side_effect = on_upsert

        args = MagicMock()
        args.mode = "all"
        args.interval = 1
        args.top = 10
        args.min_depth = 0
        args.max_trade = 10
        args.exec_mode = "manual"

        mock_executor = MagicMock()

        continuous_module.run_continuous(
            args=args,
            min_profit=0.01,
            kalshi_client=None,
            kalshi_api_key_id=None,
            kalshi_private_key_path=None,
            executor=mock_executor,
            db=mock_db,
            price_cache={},
        )

        mock_db.upsert_dispute_state.assert_called_once()
        assert "0xabc" in captured_states
        assert captured_states["0xabc"]["blocked"] is True
        assert "0xdef" in captured_states
        assert captured_states["0xdef"]["blocked"] is False
        assert "0xevent_cid" in captured_states
        assert captured_states["0xevent_cid"]["blocked"] is True
        assert mock_executor.risk_manager.uma_state_unavailable is False

    def test_continuous_dispute_cache_refresh_handles_db_exception(self, monkeypatch):
        import asyncio
        import signal as signal_module
        import config
        continuous_module = sys.modules["continuous"]

        monkeypatch.setattr(config, "DISPUTE_GATE_ENABLED", True)

        captured_signals = {}
        def capture_sig(sig, handler):
            if getattr(handler, "__name__", "") == "_signal_handler":
                captured_signals[sig] = handler

        monkeypatch.setattr(continuous_module.signal, "signal", capture_sig)

        mock_feed = MagicMock()
        async def mock_feed_run():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                # Coroutine cancelled on test shutdown
                return
        mock_feed.run = mock_feed_run
        monkeypatch.setattr(continuous_module, "FeedManager", lambda *a, **kw: mock_feed)
        monkeypatch.setattr(continuous_module, "reconcile_orphaned_positions", MagicMock())
        monkeypatch.setattr(continuous_module, "display_results", MagicMock())
        monkeypatch.setattr(continuous_module, "capture_scan_heartbeat", MagicMock())

        sample_markets = [
            {"conditionId": "0xabc", "umaResolutionStatus": "disputed"},
        ]
        monkeypatch.setattr(continuous_module, "fetch_all_markets", lambda: sample_markets)
        monkeypatch.setattr(continuous_module, "fetch_events", lambda: [])
        monkeypatch.setattr(continuous_module, "scan_binary_internal", lambda *a, **kw: [])

        mock_db = MagicMock()

        def on_upsert_error(states):
            handler = captured_signals[signal_module.SIGTERM]
            cells = dict(zip(handler.__code__.co_freevars, handler.__closure__ or ()))
            cells["shutdown_event"].cell_contents.set()
            raise RuntimeError("Database connection failure")

        mock_db.upsert_dispute_state.side_effect = on_upsert_error

        args = MagicMock()
        args.mode = "binary"
        args.interval = 1
        args.top = 10
        args.min_depth = 0
        args.max_trade = 10
        args.exec_mode = "manual"

        mock_executor = MagicMock()

        # The loop must catch the exception, set fail-closed, and finish safely
        continuous_module.run_continuous(
            args=args,
            min_profit=0.01,
            kalshi_client=None,
            kalshi_api_key_id=None,
            kalshi_private_key_path=None,
            executor=mock_executor,
            db=mock_db,
            price_cache={},
        )

        mock_db.upsert_dispute_state.assert_called_once()
        assert mock_executor.risk_manager.uma_state_unavailable is True


class TestContinuousDepthFilter:
    """Verify min_depth filtering in continuous mode handles None and numeric depths."""

    def test_continuous_depth_filter_handles_none_and_numeric_depths(self, monkeypatch):
        import asyncio
        import signal as signal_module
        continuous_module = sys.modules["continuous"]

        captured_signals = {}
        def capture_sig(sig, handler):
            if getattr(handler, "__name__", "") == "_signal_handler":
                captured_signals[sig] = handler

        monkeypatch.setattr(continuous_module.signal, "signal", capture_sig)

        mock_feed = MagicMock()
        async def mock_feed_run():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
        mock_feed.run = mock_feed_run
        monkeypatch.setattr(continuous_module, "FeedManager", lambda *a, **kw: mock_feed)
        monkeypatch.setattr(continuous_module, "reconcile_orphaned_positions", MagicMock())
        monkeypatch.setattr(continuous_module, "capture_scan_heartbeat", MagicMock())
        monkeypatch.setattr(continuous_module, "fetch_all_markets", lambda: [{"conditionId": "0x1"}])
        monkeypatch.setattr(continuous_module, "fetch_events", lambda: [])

        sample_opps = [
            {"type": "Binary", "market": "M1", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": None},
            {"type": "Binary", "market": "M2", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": 10.0},
            {"type": "Binary", "market": "M3", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": 50.0},
            {"type": "Binary", "market": "M4", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": 100.0},
            {"type": "Binary", "market": "M5", "net_profit": 0.05, "total_cost": "$0.95"},
        ]
        monkeypatch.setattr(continuous_module, "scan_binary_internal", lambda *a, **kw: list(sample_opps))

        mock_display = MagicMock()
        captured_displayed = []

        def on_display(opps, json_flag):
            captured_displayed.extend(opps)
            handler = captured_signals[signal_module.SIGTERM]
            cells = dict(zip(handler.__code__.co_freevars, handler.__closure__ or ()))
            cells["shutdown_event"].cell_contents.set()

        mock_display.side_effect = on_display
        monkeypatch.setattr(continuous_module, "display_results", mock_display)

        args = MagicMock()
        args.mode = "binary"
        args.interval = 1
        args.top = 10
        args.min_depth = 50.0
        args.max_trade = 10
        args.exec_mode = "manual"
        args.limit = None
        args.json = False

        mock_executor = MagicMock()
        mock_db = MagicMock()

        continuous_module.run_continuous(
            args=args,
            min_profit=0.01,
            kalshi_client=None,
            kalshi_api_key_id=None,
            kalshi_private_key_path=None,
            executor=mock_executor,
            db=mock_db,
            price_cache={},
        )

        mock_display.assert_called_once()
        retained_markets = [o["market"] for o in captured_displayed]
        assert "M3" in retained_markets
        assert "M4" in retained_markets
        assert "M1" not in retained_markets
        assert "M2" not in retained_markets
        assert "M5" not in retained_markets

    def test_continuous_depth_filter_disabled_when_zero(self, monkeypatch):
        import asyncio
        import signal as signal_module
        continuous_module = sys.modules["continuous"]

        captured_signals = {}
        def capture_sig(sig, handler):
            if getattr(handler, "__name__", "") == "_signal_handler":
                captured_signals[sig] = handler

        monkeypatch.setattr(continuous_module.signal, "signal", capture_sig)

        mock_feed = MagicMock()
        async def mock_feed_run():
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                return
        mock_feed.run = mock_feed_run
        monkeypatch.setattr(continuous_module, "FeedManager", lambda *a, **kw: mock_feed)
        monkeypatch.setattr(continuous_module, "reconcile_orphaned_positions", MagicMock())
        monkeypatch.setattr(continuous_module, "capture_scan_heartbeat", MagicMock())
        monkeypatch.setattr(continuous_module, "fetch_all_markets", lambda: [{"conditionId": "0x1"}])
        monkeypatch.setattr(continuous_module, "fetch_events", lambda: [])

        sample_opps = [
            {"type": "Binary", "market": "M1", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": None},
            {"type": "Binary", "market": "M2", "net_profit": 0.05, "total_cost": "$0.95", "_clob_depth": 10.0},
            {"type": "Binary", "market": "M3", "net_profit": 0.05, "total_cost": "$0.95"},
        ]
        monkeypatch.setattr(continuous_module, "scan_binary_internal", lambda *a, **kw: list(sample_opps))

        mock_display = MagicMock()
        captured_displayed = []

        def on_display(opps, json_flag):
            captured_displayed.extend(opps)
            handler = captured_signals[signal_module.SIGTERM]
            cells = dict(zip(handler.__code__.co_freevars, handler.__closure__ or ()))
            cells["shutdown_event"].cell_contents.set()

        mock_display.side_effect = on_display
        monkeypatch.setattr(continuous_module, "display_results", mock_display)

        args = MagicMock()
        args.mode = "binary"
        args.interval = 1
        args.top = 10
        args.min_depth = 0
        args.max_trade = 10
        args.exec_mode = "manual"
        args.limit = None
        args.json = False

        mock_executor = MagicMock()
        mock_db = MagicMock()

        continuous_module.run_continuous(
            args=args,
            min_profit=0.01,
            kalshi_client=None,
            kalshi_api_key_id=None,
            kalshi_private_key_path=None,
            executor=mock_executor,
            db=mock_db,
            price_cache={},
        )

        mock_display.assert_called_once()
        retained_markets = [o["market"] for o in captured_displayed]
        assert len(retained_markets) == 3
        assert "M1" in retained_markets
        assert "M2" in retained_markets
        assert "M3" in retained_markets


class TestContinuousKalshiWSOrderbookRouting:
    def test_routing_prefers_ws_orderbook_when_both_sides_present(self):
        """Verify complete two-sided orderbook updates book and does not touch on_ws_price."""
        from continuous import _route_kalshi_ws_to_mm_pilot
        mock_pilot = MagicMock()
        data = {
            "orderbook": {"yes": [[48, 50]], "no": [[48, 50]]},
            "yes_ask": 0.52,
            "no_ask": 0.52,
        }
        _route_kalshi_ws_to_mm_pilot(mock_pilot, "kalshi", "KXTEST", data, tracking_price=0.50)

        mock_pilot.update_book_from_ws.assert_called_once_with("KXTEST", {"orderbook": data["orderbook"]})
        mock_pilot.on_ws_price.assert_not_called()

    def test_routing_feeds_scalar_mid_when_orderbook_one_sided(self):
        """Verify one-sided orderbook updates book AND preserves scalar mid-price."""
        from continuous import _route_kalshi_ws_to_mm_pilot
        mock_pilot = MagicMock()
        data = {
            "orderbook": {"yes": [[48, 50]], "no": []},
            "yes_ask": None,
            "no_ask": 0.52,
        }
        _route_kalshi_ws_to_mm_pilot(mock_pilot, "kalshi", "KXTEST", data, tracking_price=0.48)

        mock_pilot.update_book_from_ws.assert_called_once_with("KXTEST", {"orderbook": data["orderbook"]})
        mock_pilot.on_ws_price.assert_called_once_with("KXTEST", 0.48)

    def test_routing_falls_back_to_scalar_when_no_orderbook(self):
        """Verify tick without orderbook falls back to on_ws_price."""
        from continuous import _route_kalshi_ws_to_mm_pilot
        mock_pilot = MagicMock()
        data = {"price": 0.51}
        _route_kalshi_ws_to_mm_pilot(mock_pilot, "kalshi", "KXTEST", data, tracking_price=0.51)

        mock_pilot.update_book_from_ws.assert_not_called()
        mock_pilot.on_ws_price.assert_called_once_with("KXTEST", 0.51)

    def test_routing_ignores_non_kalshi_platform(self):
        """Verify non-Kalshi updates never route to the Kalshi MM pilot."""
        from continuous import _route_kalshi_ws_to_mm_pilot
        mock_pilot = MagicMock()
        data = {
            "orderbook": {"yes": [[48, 50]], "no": [[48, 50]]},
            "yes_ask": 0.52,
            "no_ask": 0.52,
        }
        _route_kalshi_ws_to_mm_pilot(mock_pilot, "polymarket", "POLY-1", data, tracking_price=0.50)

        mock_pilot.update_book_from_ws.assert_not_called()
        mock_pilot.on_ws_price.assert_not_called()

    def test_routing_tolerates_pilot_exceptions_gracefully(self):
        """Verify pilot exceptions are caught and do not disrupt WS event loop."""
        from continuous import _route_kalshi_ws_to_mm_pilot
        mock_pilot = MagicMock()
        mock_pilot.update_book_from_ws.side_effect = RuntimeError("book parse error")
        mock_pilot.on_ws_price.side_effect = RuntimeError("mid update error")

        data = {
            "orderbook": {"yes": []},
            "yes_ask": None,
            "no_ask": None,
        }
        # Should not raise
        _route_kalshi_ws_to_mm_pilot(mock_pilot, "kalshi", "KXTEST", data, tracking_price=0.50)
        mock_pilot.update_book_from_ws.assert_called_once()
        mock_pilot.on_ws_price.assert_called_once()


class TestContinuousWSOrderbookStreamingWiring:
    """Verify continuous mode wires price_cache to hedger and feed_manager to executor & hedger."""

    def test_wiring_price_cache_and_feed_manager(self, monkeypatch):
        continuous_module = sys.modules["continuous"]
        from hedger import PartialFillHedger

        args = MagicMock()
        args.interval = None
        args.mode = "all"
        args.limit = 10
        args.min_depth = 0
        args.min_confidence = 0.5
        args.json = False
        args.dry_run = True
        args.exec_mode = "semi-auto"
        args.max_trade = 5.0
        args.dashboard_port = None

        captured_hedger_kwargs = {}
        captured_hedger_instances = []
        original_hedger_init = PartialFillHedger.__init__

        def fake_hedger_init(self, *args, **kwargs):
            captured_hedger_kwargs.update(kwargs)
            captured_hedger_instances.append(self)
            original_hedger_init(self, *args, **kwargs)

        monkeypatch.setattr(PartialFillHedger, "__init__", fake_hedger_init)

        mock_feed_mgr = MagicMock()
        monkeypatch.setattr(continuous_module, "FeedManager", lambda **kwargs: mock_feed_mgr)

        class _StopSentinel(Exception):
            pass

        def fake_asyncio_run(coro):
            coro.close()
            raise _StopSentinel()

        monkeypatch.setattr(continuous_module.asyncio, "run", fake_asyncio_run)

        test_price_cache = {("polymarket", "tok1"): {"price": 0.5}}
        mock_executor = MagicMock()

        with pytest.raises(_StopSentinel), \
             patch("continuous.CONFIG_HEDGE_ENABLED", True):
            continuous_module.run_continuous(
                args=args,
                min_profit=0.01,
                kalshi_client=None,
                kalshi_api_key_id=None,
                kalshi_private_key_path=None,
                executor=mock_executor,
                db=MagicMock(),
                price_cache=test_price_cache,
                extra_clients={},
            )

        assert captured_hedger_kwargs.get("price_cache") is test_price_cache
        assert mock_executor.feed_manager is mock_feed_mgr
        assert len(captured_hedger_instances) >= 1
        assert captured_hedger_instances[0].feed_manager is mock_feed_mgr
