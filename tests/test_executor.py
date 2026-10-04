"""Tests for executor.py — arbitrage trade execution engine."""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
import time

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import TradeDB
from risk_manager import RiskManager


# We need to mock the imports that executor.py uses before importing it
# because some modules (betfair_api, etc.) may not exist.
# Patch them in sys.modules before importing executor.

@pytest.fixture(autouse=True)
def mock_external_modules():
    """Use real platform-API modules when importable; mock only the ones that fail.

    The historical version of this fixture unconditionally replaced these
    modules with MagicMocks if they were absent from ``sys.modules``. That
    broke isolation runs where ``test_executor.py`` was the first file to
    load — ``_revalidate_cross`` would then call MagicMock'd
    ``kalshi_api.parse_orderbook`` / ``best_yes_ask`` and fail when
    formatting a MagicMock as a float. The full test suite hid the bug
    because earlier files always imported the real modules first.
    """
    mock_modules = {}
    for mod_name in [
        "polymarket_api", "kalshi_api",
        "betfair_api", "smarkets_api", "sxbet_api",
    ]:
        if mod_name in sys.modules:
            continue
        try:
            __import__(mod_name)
        except ImportError:
            mock_modules[mod_name] = MagicMock()
            sys.modules[mod_name] = mock_modules[mod_name]
    yield
    for mod_name in mock_modules:
        del sys.modules[mod_name]


# Import after mocking external modules
def _import_executor():
    # Force reimport to pick up mocked modules
    if "executor" in sys.modules:
        del sys.modules["executor"]
    from executor import ArbitrageExecutor
    return ArbitrageExecutor


@pytest.fixture
def ArbitrageExecutor():
    return _import_executor()


@pytest.fixture
def db():
    trade_db = TradeDB(":memory:")
    yield trade_db
    trade_db.close()


@pytest.fixture
def risk_manager():
    return RiskManager({
        "max_trade_size": 5.0,
        "daily_loss_limit": 25.0,
        "max_open_positions": 25,
        "min_liquidity": 25.0,
        "min_liquidity_high_roi": 10.0,
        "min_net_roi": 0,
        "allow_better_reentry": True,
        "reentry_improvement_threshold": 0.20,
    })


@pytest.fixture
def executor(ArbitrageExecutor, db, risk_manager):
    pm_trader = MagicMock()
    kalshi_client = MagicMock()
    return ArbitrageExecutor(
        pm_trader=pm_trader,
        kalshi_client=kalshi_client,
        db=db,
        risk_manager=risk_manager,
        dry_run=True,
        max_trade_size=5.0,
    )


# ---------------------------------------------------------------------------
# _build_legs for each opportunity type
# ---------------------------------------------------------------------------

class TestBuildLegs:
    def test_binary_legs(self, executor):
        opp = {
            "type": "Binary",
            "prices": "Y=0.400 N=0.450",
            "_token_ids": ["token_yes", "token_no"],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.400)
        assert legs[0]["_token_id"] == "token_yes"
        assert legs[1]["price"] == pytest.approx(0.450)
        assert legs[1]["_token_id"] == "token_no"

    def test_negrisk_legs(self, executor):
        opp = {
            "type": "NegRisk (4 outcomes)",
            "prices": "0.20, 0.25, 0.30, 0.15",
            "_token_ids": ["t0", "t1", "t2", "t3"],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 4
        assert legs[0]["price"] == pytest.approx(0.20)
        assert legs[3]["price"] == pytest.approx(0.15)
        for i, leg in enumerate(legs):
            assert leg["_token_id"] == f"t{i}"

    def test_kalshi_binary_legs(self, executor):
        opp = {
            "type": "KalshiBinary",
            "_kalshi_yes": 0.40,
            "_kalshi_no": 0.40,
            "_kalshi_ticker": "TICKER-ABC",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "kalshi"
        assert legs[0]["side"] == "yes"
        assert legs[0]["_ticker"] == "TICKER-ABC"
        assert legs[1]["side"] == "no"

    def test_kalshi_multi_legs(self, executor):
        opp = {
            "type": "KalshiMulti (3 outcomes)",
            "_kalshi_tickers": ["T1", "T2", "T3"],
            "_kalshi_prices": [0.20, 0.30, 0.25],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 3
        for i, leg in enumerate(legs):
            assert leg["platform"] == "kalshi"
            assert leg["_ticker"] == opp["_kalshi_tickers"][i]

    def test_cross_pm_yes_kalshi_no(self, executor):
        opp = {
            "type": "Cross",
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.300)
        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "no"
        assert legs[1]["_ticker"] == "TICKER-XYZ"

    def test_cross_pm_no_kalshi_yes(self, executor):
        opp = {
            "type": "Cross",
            "prices": "PM_N=0.350 K_Y=0.300",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.350)
        assert legs[0]["_token_id"] == "token_no"
        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "yes"

    def test_binary_no_token_ids(self, executor):
        opp = {
            "type": "Binary",
            "prices": "Y=0.400 N=0.450",
            "_token_ids": [],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["_token_id"] == ""
        assert legs[1]["_token_id"] == ""


# ---------------------------------------------------------------------------
# Token ID propagation
# ---------------------------------------------------------------------------

class TestTokenIdPropagation:
    def test_negrisk_token_ids_propagated(self, executor):
        opp = {
            "type": "NegRisk (3 outcomes)",
            "prices": "0.20, 0.30, 0.25",
            "_token_ids": ["tok_a", "tok_b", "tok_c"],
        }
        legs = executor._build_legs(opp, 5.0)
        assert legs[0]["_token_id"] == "tok_a"
        assert legs[1]["_token_id"] == "tok_b"
        assert legs[2]["_token_id"] == "tok_c"

    def test_negrisk_missing_token_ids(self, executor):
        opp = {
            "type": "NegRisk (3 outcomes)",
            "prices": "0.20, 0.30, 0.25",
            "_token_ids": ["tok_a"],  # Only 1 token ID for 3 outcomes
        }
        legs = executor._build_legs(opp, 5.0)
        # Price count (3) != token ID count (1) → returns empty legs
        assert legs == []


# ---------------------------------------------------------------------------
# _parse_price
# ---------------------------------------------------------------------------

class TestParsePrice:
    def test_extracts_yes_price(self, executor):
        opp = {"prices": "Y=0.400 N=0.450"}
        assert executor._parse_price(opp, "Y=") == pytest.approx(0.400)

    def test_extracts_no_price(self, executor):
        opp = {"prices": "Y=0.400 N=0.450"}
        assert executor._parse_price(opp, "N=") == pytest.approx(0.450)

    def test_extracts_cross_prices(self, executor):
        opp = {"prices": "PM_Y=0.300 K_N=0.350"}
        assert executor._parse_price(opp, "PM_Y=") == pytest.approx(0.300)
        assert executor._parse_price(opp, "K_N=") == pytest.approx(0.350)

    def test_returns_none_on_missing_prefix(self, executor):
        opp = {"prices": "Y=0.400 N=0.450"}
        assert executor._parse_price(opp, "X=") is None

    def test_returns_none_on_invalid_value(self, executor):
        opp = {"prices": "Y=abc N=0.450"}
        assert executor._parse_price(opp, "Y=") is None

    def test_returns_none_for_zero_price(self, executor):
        opp = {"prices": "Y=0.000 N=0.450"}
        assert executor._parse_price(opp, "Y=") is None

    def test_returns_none_for_price_at_one(self, executor):
        opp = {"prices": "Y=1.000 N=0.450"}
        assert executor._parse_price(opp, "Y=") is None

    def test_returns_none_on_empty_prices(self, executor):
        opp = {"prices": ""}
        assert executor._parse_price(opp, "Y=") is None


# ---------------------------------------------------------------------------
# Revalidation (mock API calls, test 90% degradation threshold)
# ---------------------------------------------------------------------------

class TestRevalidation:
    def test_revalidate_binary_passes(self, executor):
        from unittest.mock import patch as mpatch
        opp = {
            "type": "Binary",
            "net_profit": 0.10,
            "total_cost": "$0.8500",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        mock_book = {"asks": [{"price": "0.40", "size": "100"}]}
        mock_bid_ask = {"bid": 0.39, "ask": 0.40}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_binary_internal", return_value={"net_profit": 0.095}):
            result = executor._revalidate(opp, None)
            # ROI = 0.10/0.85 = 11.8% (>5%) -> strict 90%: 0.095 >= 0.09 -> pass
            assert result is True

    def test_revalidate_binary_degraded(self, executor):
        from unittest.mock import patch as mpatch
        opp = {
            "type": "Binary",
            "net_profit": 0.10,
            "total_cost": "$0.8500",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        mock_book = {"asks": [{"price": "0.40", "size": "100"}]}
        mock_bid_ask = {"bid": 0.39, "ask": 0.40}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_binary_internal", return_value={"net_profit": 0.05}):
            result = executor._revalidate(opp, None)
            # ROI = 0.10/0.85 = 11.8% (>5%) -> strict 90%: 0.05 < 0.09 -> fail
            assert result is False

    def test_revalidate_returns_false_on_zero_profit(self, executor):
        opp = {"type": "Binary", "net_profit": 0, "_token_ids": ["a", "b"]}
        result = executor._revalidate(opp, None)
        assert result is False

    def test_revalidate_returns_false_on_negative_profit(self, executor):
        opp = {"type": "Binary", "net_profit": -0.01, "_token_ids": ["a", "b"]}
        result = executor._revalidate(opp, None)
        assert result is False

    def test_revalidate_binary_missing_token_ids(self, executor):
        from unittest.mock import patch as mpatch
        opp = {"type": "Binary", "net_profit": 0.10, "_token_ids": ["a"]}
        result = executor._revalidate(opp, None)
        assert result is False

    def test_revalidate_unknown_type_proceeds(self, executor):
        opp = {"type": "UnknownType", "net_profit": 0.10}
        result = executor._revalidate(opp, None)
        assert result is True

    def test_revalidate_handles_exception(self, executor):
        from unittest.mock import patch as mpatch
        opp = {"type": "Binary", "net_profit": 0.10, "_token_ids": ["a", "b"]}
        with mpatch("executor.fetch_order_book", side_effect=Exception("API down")):
            result = executor._revalidate(opp, None)
            assert result is False


# ---------------------------------------------------------------------------
# Dry run logging
# ---------------------------------------------------------------------------

class TestDryRunLog:
    def test_dry_run_logs_opportunity_and_trades(self, executor, db):
        opp = {
            "type": "Binary",
            "market": "Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.138,
            "net_roi": "16.2%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        legs = [
            {"platform": "polymarket", "side": "BUY", "token": "yes", "price": 0.40, "_token_id": "tok_yes"},
            {"platform": "polymarket", "side": "BUY", "token": "no", "price": 0.45, "_token_id": "tok_no"},
        ]
        result = executor._dry_run_log(opp, legs, 5.0)
        assert result is True

        opps = db.get_recent_opportunities()
        assert len(opps) == 1
        assert opps[0]["action"] == "dry_run"

        trades = db.get_trades_for_opportunity(opps[0]["id"])
        assert len(trades) == 2
        assert all(t["status"] == "dry_run" for t in trades)
        assert [t["outcome"] for t in trades] == ["yes", "no"]


# ---------------------------------------------------------------------------
# Position creation after successful execution
# ---------------------------------------------------------------------------

class TestPositionCreation:
    def test_execute_dry_run_full_pipeline(self, executor, db):
        # Configure mock traders to return numeric balances
        executor.pm_trader.get_balance.return_value = 100.0
        opp = {
            "type": "Binary",
            "market": "Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.138,
            "net_roi": "16.2%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        result = executor.execute(opp)
        assert result is True

        opps = db.get_recent_opportunities()
        assert len(opps) == 1
        assert opps[0]["action"] == "dry_run"


# ---------------------------------------------------------------------------
# Orphaned trade marking on partial fill failure
# ---------------------------------------------------------------------------

class TestOrphanedTrades:
    def test_execute_legs_partial_fill_marks_orphaned(self, executor, db):
        executor.dry_run = False
        opp = {
            "type": "Binary",
            "market": "Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.138,
            "net_roi": "16.2%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }

        legs = [
            {"platform": "polymarket", "side": "BUY", "token": "yes",
             "price": 0.40, "_token_id": "tok_yes"},
            {"platform": "polymarket", "side": "BUY", "token": "no",
             "price": 0.45, "_token_id": "tok_no"},
        ]

        # Mock _execute_single_leg: first leg succeeds, second fails
        call_count = [0]
        def mock_execute_single_leg(leg, size, opp_arg):
            call_count[0] += 1
            if leg["token"] == "yes":
                leg["_order_id"] = "order_123"
                return (True, "order_123", 0.40)
            else:
                return (False, None, None)

        executor._execute_single_leg = mock_execute_single_leg

        # Mock cancel to fail so it gets marked orphaned
        executor._cancel_leg = lambda leg: False

        result = executor._execute_legs(opp, legs, 5.0)
        assert result is False

        trades = db.get_trades_for_opportunity(1)
        statuses = [t["status"] for t in trades]
        assert "orphaned" in statuses or "filled" in statuses


# ---------------------------------------------------------------------------
# _check_ws_cache
# ---------------------------------------------------------------------------

class TestCheckWsCache:
    def test_returns_none_when_no_cache(self, executor):
        result = executor._check_ws_cache(None, "polymarket", "tok1")
        assert result is None

    def test_returns_none_when_stale(self, executor):
        cache = {("polymarket", "tok1"): {"price": 0.50, "_ts": time.time() - 20}}
        result = executor._check_ws_cache(cache, "polymarket", "tok1")
        assert result is None

    def test_returns_entry_when_fresh(self, executor):
        cache = {("polymarket", "tok1"): {"price": 0.50, "_ts": time.time()}}
        result = executor._check_ws_cache(cache, "polymarket", "tok1")
        assert result is not None
        assert result["price"] == 0.50

    def test_returns_none_when_key_missing(self, executor):
        cache = {("polymarket", "tok1"): {"price": 0.50, "_ts": time.time()}}
        result = executor._check_ws_cache(cache, "polymarket", "tok2")
        assert result is None


# ---------------------------------------------------------------------------
# _revalidate_cross — strategy flip detection
# ---------------------------------------------------------------------------

class TestCrossRevalidationStrategy:
    def test_uses_normalized_ws_asks_and_coerces_strings(self, executor):
        """Production WS payloads contain string prices and raw Kalshi ladders."""
        from unittest.mock import call, patch as mpatch

        opp = {
            "type": "Cross(PM_NO + K_YES)",
            "net_profit": 0.10,
            "prices": "PM_N=0.690 K_Y=0.410",
            "total_cost": "$1.1000",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }
        now = time.time()
        price_cache = {
            ("polymarket", "tok_yes"): {
                "best_ask": "0.310", "price": "0.300", "_ts": now,
            },
            ("polymarket", "tok_no"): {
                "best_ask": "0.690", "price": "0.680", "_ts": now,
            },
            ("kalshi", "TICKER-XYZ"): {
                "yes": [[40, 10]],
                "no": [[60, 10]],
                "yes_ask": "0.410",
                "no_ask": "0.360",
                "_ts": now,
            },
        }

        with mpatch(
            "executor.net_profit_cross_platform",
            side_effect=[{"net_profit": 0.05}, {"net_profit": 0.11}],
        ) as profit:
            passed, reval_profit, reason = executor._revalidate_cross(
                opp, 0.10, price_cache,
            )

        assert passed is True
        assert reval_profit == pytest.approx(0.11)
        assert reason == "passed"
        assert profit.call_args_list == [
            call(0.31, 0.36, "yes", "no"),
            call(0.69, 0.41, "no", "yes"),
        ]

    def test_strategy1_remains_best(self, executor):
        """When strategy 1 (PM_YES + K_NO) stays best, prices should contain PM_Y= and K_N=."""
        from unittest.mock import patch as mpatch

        opp = {
            "type": "Cross",
            "net_profit": 0.10,
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }

        mock_book = {"asks": [{"price": "0.30", "size": "100"}]}
        mock_bid_ask = {"bid": 0.29, "ask": 0.30}
        kalshi_book = {
            "orderbook": {
                "yes": [[40, 10]],
                "no": [[35, 10]],
            }
        }
        executor.kalshi_client.fetch_order_book.return_value = kalshi_book

        # Strategy 1 wins: net_profit=0.12; Strategy 2 loses: net_profit=0.05
        strat1_result = {"net_profit": 0.12}
        strat2_result = {"net_profit": 0.05}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_cross_platform", side_effect=[strat1_result, strat2_result]):
            passed, reval_profit, reason = executor._revalidate_cross(opp, 0.10, None)

        assert passed is True
        assert "PM_Y=" in opp["prices"]
        assert "K_N=" in opp["prices"]
        assert opp["net_profit"] == pytest.approx(0.12)

    def test_strategy2_becomes_best(self, executor):
        """When strategy 2 (PM_NO + K_YES) becomes best, prices should flip to PM_N= and K_Y=."""
        from unittest.mock import patch as mpatch

        opp = {
            "type": "Cross",
            "net_profit": 0.10,
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }

        mock_book = {"asks": [{"price": "0.30", "size": "100"}]}
        mock_bid_ask = {"bid": 0.29, "ask": 0.30}
        kalshi_book = {
            "orderbook": {
                "yes": [[35, 10]],
                "no": [[40, 10]],
            }
        }
        executor.kalshi_client.fetch_order_book.return_value = kalshi_book

        # Strategy 2 wins: net_profit=0.14; Strategy 1 loses: net_profit=0.04
        strat1_result = {"net_profit": 0.04}
        strat2_result = {"net_profit": 0.14}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_cross_platform", side_effect=[strat1_result, strat2_result]):
            passed, reval_profit, reason = executor._revalidate_cross(opp, 0.10, None)

        assert passed is True
        assert "PM_N=" in opp["prices"]
        assert "K_Y=" in opp["prices"]
        assert opp["net_profit"] == pytest.approx(0.14)

    def test_net_profit_updated_to_winning_strategy(self, executor):
        """Verify net_profit is set to the winning strategy's value, not left at original."""
        from unittest.mock import patch as mpatch

        opp = {
            "type": "Cross",
            "net_profit": 0.10,
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }

        mock_book = {"asks": [{"price": "0.30", "size": "100"}]}
        mock_bid_ask = {"bid": 0.29, "ask": 0.30}
        kalshi_book = {
            "orderbook": {
                "yes": [[30, 10]],
                "no": [[30, 10]],
            }
        }
        executor.kalshi_client.fetch_order_book.return_value = kalshi_book

        # Both strategies above threshold; strategy 1 wins with 0.15
        strat1_result = {"net_profit": 0.15}
        strat2_result = {"net_profit": 0.11}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_cross_platform", side_effect=[strat1_result, strat2_result]):
            passed, reval_profit, reason = executor._revalidate_cross(opp, 0.10, None)

        assert passed is True
        assert opp["net_profit"] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# _build_cross_all_legs — generic cross-platform leg builder
# ---------------------------------------------------------------------------

class TestBuildCrossAllLegs:
    def _patch_all_platforms(self):
        """Return a combined context manager enabling all platforms."""
        all_plats = frozenset([
            "polymarket", "kalshi", "betfair", "smarkets",
            "sxbet", "matchbook", "gemini", "ibkr",
        ])
        no_mins = {p: 0.01 for p in all_plats}
        import executor as _ex
        return (
            patch.object(_ex, "ENABLED_EXECUTION_PLATFORMS", all_plats),
            patch.object(_ex, "PLATFORM_MIN_ORDER_SIZE", no_mins),
        )

    def test_polymarket_smarkets_legs(self, executor):
        """Polymarket + Smarkets: should produce 2 legs with correct platforms."""
        opp = {
            "type": "Cross",
            "prices": "polymarket_Y=0.400 smarkets_N=0.300",
            "_token_ids": ["tok_yes", "tok_no"],
            "_platform_a": "polymarket",
            "_platform_b": "smarkets",
            "_sm_market_id": "sm_123",
        }
        p1, p2 = self._patch_all_platforms()
        with p1, p2:
            legs = executor._build_cross_all_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.400)
        assert legs[0]["_token_id"] == "tok_yes"
        assert legs[1]["platform"] == "smarkets"
        assert legs[1]["price"] == pytest.approx(0.300)
        assert legs[1]["side"] == "no"

    def test_polymarket_betfair_legs(self, executor):
        """Polymarket + Betfair: should include _market_id and _selection_id."""
        opp = {
            "type": "Cross",
            "prices": "polymarket_Y=0.450 betfair_N=0.250",
            "_token_ids": ["tok_yes", "tok_no"],
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "_market_id": "1.234567",
            "_selection_id": 98765,
        }
        p1, p2 = self._patch_all_platforms()
        with p1, p2:
            legs = executor._build_cross_all_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.450)
        assert legs[1]["platform"] == "betfair"
        assert legs[1]["price"] == pytest.approx(0.250)
        assert legs[1]["_market_id"] == "1.234567"
        assert legs[1]["_selection_id"] == 98765

    def test_polymarket_sxbet_legs(self, executor):
        """Polymarket + SX Bet: should use _sx_market_hash."""
        opp = {
            "type": "Cross",
            "prices": "polymarket_N=0.350 sxbet_Y=0.400",
            "_token_ids": ["tok_yes", "tok_no"],
            "_platform_a": "polymarket",
            "_platform_b": "sxbet",
            "_sx_market_hash": "0xabc123",
        }
        p1, p2 = self._patch_all_platforms()
        with p1, p2:
            legs = executor._build_cross_all_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.350)
        assert legs[0]["token"] == "no"
        assert legs[0]["_token_id"] == "tok_no"
        assert legs[1]["platform"] == "sxbet"
        assert legs[1]["price"] == pytest.approx(0.400)
        assert legs[1]["_market_hash"] == "0xabc123"

    def test_malformed_prices_returns_empty(self, executor):
        """Malformed prices string should return empty legs."""
        opp = {
            "type": "Cross",
            "prices": "garbage data here with no equals",
            "_token_ids": ["tok_yes", "tok_no"],
            "_platform_a": "polymarket",
            "_platform_b": "smarkets",
        }
        legs = executor._build_cross_all_legs(opp, 5.0)
        assert legs == []

    def test_platform_a_and_b_must_be_present(self, executor):
        """Without _platform_a/_platform_b, _build_legs falls through to standard Cross handler."""
        opp = {
            "type": "Cross",
            "prices": "polymarket_Y=0.400 smarkets_N=0.300",
            "_token_ids": ["tok_yes", "tok_no"],
            # No _platform_a or _platform_b
        }
        # _build_legs without _platform_a won't reach _build_cross_all_legs;
        # the standard Cross handler won't match PM_Y= or PM_N= either, so empty.
        legs = executor._build_legs(opp, 5.0)
        assert legs == []


# ---------------------------------------------------------------------------
# _fetch_balances — multi-platform balance fetching for Cross opps
# ---------------------------------------------------------------------------

class TestFetchBalances:
    def test_cross_fetches_smarkets_balance(self, executor):
        """Cross-type opp should call get_balance on smarkets client."""
        mock_smarkets = MagicMock()
        mock_smarkets.get_balance.return_value = 500.0
        executor.smarkets_client = mock_smarkets

        balances = executor._fetch_balances("Cross")
        assert "smarkets" in balances
        assert balances["smarkets"] == 500.0
        mock_smarkets.get_balance.assert_called_once()

    def test_cross_fetches_betfair_balance(self, executor):
        """Cross-type opp should call get_balance on betfair client."""
        mock_betfair = MagicMock()
        mock_betfair.get_balance.return_value = 1000.0
        executor.betfair_client = mock_betfair

        balances = executor._fetch_balances("Cross")
        assert "betfair" in balances
        assert balances["betfair"] == 1000.0
        mock_betfair.get_balance.assert_called_once()

    def test_cross_fetches_sxbet_balance(self, executor):
        """Cross-type opp should call get_balance on sxbet client."""
        mock_sxbet = MagicMock()
        mock_sxbet.get_balance.return_value = 250.0
        executor.sxbet_client = mock_sxbet

        balances = executor._fetch_balances("Cross")
        assert "sxbet" in balances
        assert balances["sxbet"] == 250.0
        mock_sxbet.get_balance.assert_called_once()

    def test_cross_fetches_all_platform_balances(self, executor):
        """Cross-type should fetch from all available platform clients."""
        mock_betfair = MagicMock()
        mock_betfair.get_balance.return_value = 1000.0
        mock_smarkets = MagicMock()
        mock_smarkets.get_balance.return_value = 500.0
        mock_sxbet = MagicMock()
        mock_sxbet.get_balance.return_value = 250.0

        executor.betfair_client = mock_betfair
        executor.smarkets_client = mock_smarkets
        executor.sxbet_client = mock_sxbet

        balances = executor._fetch_balances("Cross")
        assert balances["betfair"] == 1000.0
        assert balances["smarkets"] == 500.0
        assert balances["sxbet"] == 250.0

    def test_non_cross_does_not_fetch_extra_platforms(self, executor):
        """Non-Cross opp types should not fetch betfair/smarkets/sxbet balances."""
        mock_betfair = MagicMock()
        mock_betfair.get_balance.return_value = 500.0
        executor.betfair_client = mock_betfair

        balances = executor._fetch_balances("Binary")
        # Binary only fetches polymarket
        mock_betfair.get_balance.assert_not_called()
        if balances:
            assert "betfair" not in balances


# ---------------------------------------------------------------------------
# Fee path re-validation and routing in _build_legs for Cross types
# ---------------------------------------------------------------------------

class TestFeePathExecution:
    def test_build_legs_routes_using_fee_path(self, executor):
        """When Cross opp has _fee_path, legs are built using fee_path platform routing."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-FP",
            "_fee_path": {
                "best_yes_platform": "polymarket",
                "best_no_platform": "kalshi",
                "yes_price": 0.305,
                "no_price": 0.355,
                "total_cost": 0.660,
                "estimated_fees": 0.010,
                "net_profit": 0.040,
            },
        }
        fresh_path = {
            "best_yes_platform": "polymarket",
            "best_no_platform": "kalshi",
            "yes_price": 0.305,
            "no_price": 0.355,
            "total_cost": 0.660,
            "estimated_fees": 0.010,
            "net_profit": 0.040,
        }
        with mpatch("executor.find_lowest_fee_path", return_value=fresh_path):
            legs = executor._build_legs(opp, 5.0)

        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[1]["platform"] == "kalshi"

    def test_build_legs_no_fee_path_uses_default(self, executor):
        """When Cross opp has no _fee_path, executor builds legs using default prices_str parsing."""
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-DEF",
            # No _fee_path key
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.300)
        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "no"
        assert legs[1]["_ticker"] == "TICKER-DEF"

    def test_revalidation_calls_find_lowest_fee_path(self, executor):
        """When _fee_path is present, executor calls find_lowest_fee_path for re-validation."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-RV",
            "_fee_path": {
                "best_yes_platform": "polymarket",
                "best_no_platform": "kalshi",
                "yes_price": 0.305,
                "no_price": 0.355,
                "total_cost": 0.660,
                "estimated_fees": 0.010,
                "net_profit": 0.040,
            },
        }
        fresh_path = {
            "best_yes_platform": "polymarket",
            "best_no_platform": "kalshi",
            "yes_price": 0.305,
            "no_price": 0.355,
            "total_cost": 0.660,
            "estimated_fees": 0.010,
            "net_profit": 0.040,
        }
        with mpatch("executor.find_lowest_fee_path", return_value=fresh_path) as mock_flp:
            executor._build_legs(opp, 5.0)
        mock_flp.assert_called_once()

    def test_stale_fee_path_falls_back_to_default(self, executor):
        """When _fee_path re-validation returns None (stale), executor falls back to default routing."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "prices": "PM_Y=0.300 K_N=0.350",
            "_token_ids": ["token_yes", "token_no"],
            "_kalshi_ticker": "TICKER-STALE",
            "_fee_path": {
                "best_yes_platform": "polymarket",
                "best_no_platform": "kalshi",
                "yes_price": 0.305,
                "no_price": 0.355,
                "total_cost": 0.660,
                "estimated_fees": 0.010,
                "net_profit": 0.040,
            },
        }
        # Re-validation returns None — fee path no longer profitable
        with mpatch("executor.find_lowest_fee_path", return_value=None):
            legs = executor._build_legs(opp, 5.0)

        # Falls back to default prices_str routing — PM_Y=0.300 K_N=0.350
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.300)
        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "no"
        assert legs[1]["_ticker"] == "TICKER-STALE"


# ---------------------------------------------------------------------------
# Adaptive revalidation threshold
# ---------------------------------------------------------------------------

class TestAdaptiveRevalidation:
    def test_high_roi_uses_strict_threshold(self, executor):
        """ROI >= 5% uses strict 90% threshold."""
        opp = {"total_cost": "$0.9000"}
        # net_profit=0.10 on cost=0.90 => ROI=11.1% (>5%)
        threshold = executor._get_revalidation_threshold(0.10, opp)
        assert threshold == pytest.approx(0.10 * 0.9)

    def test_medium_roi_uses_moderate_threshold(self, executor):
        """ROI 2-5% uses 80% threshold."""
        opp = {"total_cost": "$0.9500"}
        # net_profit=0.03 on cost=0.95 => ROI=3.16% (2-5%)
        threshold = executor._get_revalidation_threshold(0.03, opp)
        assert threshold == pytest.approx(0.03 * 0.8)

    def test_low_roi_uses_layer_floor(self, executor):
        """ROI < 2% uses layer-specific floor (L1=0.02 for Binary/unknown)."""
        opp = {"total_cost": "$0.9900", "type": "Binary", "_layer": 1}
        # net_profit=0.005 on cost=0.99 => ROI=0.51% (<2%)
        threshold = executor._get_revalidation_threshold(0.005, opp)
        assert threshold == pytest.approx(0.02)  # Layer 1 floor

    def test_below_floor_still_rejected(self, executor):
        """Profits below the layer floor are rejected (threshold > original)."""
        opp = {"total_cost": "$0.9900", "type": "Binary", "_layer": 1}
        threshold = executor._get_revalidation_threshold(0.001, opp)
        # Layer 1 floor is 0.02, so even though original is 0.001, threshold is 0.02
        assert threshold == pytest.approx(0.02)

    def test_adaptive_disabled_uses_strict(self, ArbitrageExecutor, db, risk_manager):
        """When revalidation_adaptive=False, always use 90%."""
        ex = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=MagicMock(),
            db=db, risk_manager=risk_manager, dry_run=True,
            revalidation_adaptive=False, revalidation_min_floor=0.003,
        )
        opp = {"total_cost": "$0.9900"}
        # ROI < 2% but adaptive disabled -> strict 90%
        threshold = ex._get_revalidation_threshold(0.005, opp)
        assert threshold == pytest.approx(0.005 * 0.9)

    def test_zero_cost_uses_layer_floor(self, executor):
        """Zero cost defaults to 0 ROI -> layer floor (L1=0.02 for unknown type)."""
        opp = {"total_cost": "$0", "type": "Binary", "_layer": 1}
        threshold = executor._get_revalidation_threshold(0.01, opp)
        assert threshold == pytest.approx(0.02)  # Layer 1 floor

    def test_numeric_total_cost(self, executor):
        """Handles total_cost as numeric value."""
        opp = {"total_cost": 0.80}
        # ROI = 0.10/0.80 = 12.5% (>5%)
        threshold = executor._get_revalidation_threshold(0.10, opp)
        assert threshold == pytest.approx(0.10 * 0.9)


# ---------------------------------------------------------------------------
# Dynamic sizing in executor
# ---------------------------------------------------------------------------

class TestDynamicSizingExecutor:
    def test_dynamic_sizing_disabled_uses_fixed(self, ArbitrageExecutor, db, risk_manager):
        """When dynamic_sizing=False, uses fixed max_trade_size."""
        ex = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=MagicMock(),
            db=db, risk_manager=risk_manager, dry_run=True,
            max_trade_size=5.0, dynamic_sizing=False,
        )
        ex.pm_trader.get_balance.return_value = 100.0
        opp = {
            "type": "Binary",
            "market": "Test",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.10,
            "net_roi": "11.8%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        result = ex.execute(opp)
        assert result is True

    def test_dynamic_sizing_enabled_executes(self, ArbitrageExecutor, db, risk_manager):
        """When dynamic_sizing=True, should still execute successfully."""
        ex = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=MagicMock(),
            db=db, risk_manager=risk_manager, dry_run=True,
            max_trade_size=50.0, dynamic_sizing=True, sizing_aggressiveness=0.5,
        )
        ex.pm_trader.get_balance.return_value = 100.0
        opp = {
            "type": "Binary",
            "market": "Test Dynamic",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.10,
            "net_roi": "11.8%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        result = ex.execute(opp)
        assert result is True


# ---------------------------------------------------------------------------
# Partial CLOB revalidation
# ---------------------------------------------------------------------------

class TestPartialClobRevalidation:
    def test_partial_clob_uses_wider_threshold_strict(self, executor):
        """Partial CLOB opportunities use 80% instead of 90% for high ROI."""
        opp = {"total_cost": "$0.8500", "_partial_clob": True}
        # ROI = 0.10/0.85 = 11.8% (>5%) -> partial uses 80%
        threshold = executor._get_revalidation_threshold(0.10, opp)
        assert threshold == pytest.approx(0.10 * 0.8)

    def test_non_partial_uses_standard_threshold(self, executor):
        """Non-partial opportunities use standard 90% threshold."""
        opp = {"total_cost": "$0.8500"}
        threshold = executor._get_revalidation_threshold(0.10, opp)
        assert threshold == pytest.approx(0.10 * 0.9)

    def test_partial_clob_medium_roi(self, executor):
        """Partial CLOB with medium ROI uses 70% instead of 80%."""
        opp = {"total_cost": "$0.9500", "_partial_clob": True}
        # ROI = 0.03/0.95 = 3.16% (2-5%) -> partial uses 70%
        threshold = executor._get_revalidation_threshold(0.03, opp)
        assert threshold == pytest.approx(0.03 * 0.7)

    def test_partial_clob_low_roi_uses_layer_floor(self, executor):
        """Partial CLOB with low ROI uses layer floor (L1=0.02 for Binary)."""
        opp = {"total_cost": "$0.9900", "_partial_clob": True, "type": "Binary", "_layer": 1}
        threshold = executor._get_revalidation_threshold(0.005, opp)
        assert threshold == pytest.approx(0.02)  # Layer 1 floor


# ---------------------------------------------------------------------------
# Slippage tracking in executor
# ---------------------------------------------------------------------------

class TestSlippageTracking:
    def test_slippage_stored_after_fill(self, executor, db):
        """After a successful fill, slippage should be recorded in the trades table."""
        executor.dry_run = False
        opp = {
            "type": "Binary",
            "market": "Test Slippage",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.10,
            "net_roi": "11.8%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        legs = [
            {"platform": "polymarket", "side": "BUY", "token": "yes",
             "price": 0.40, "_token_id": "tok_yes"},
            {"platform": "polymarket", "side": "BUY", "token": "no",
             "price": 0.45, "_token_id": "tok_no"},
        ]

        # Mock _execute_single_leg to return fills with slight slippage
        def mock_execute(leg, size, opp_arg):
            leg["_order_id"] = "order_mock"
            # Simulate fill at slightly worse price
            fill = leg["price"] + 0.005
            return (True, "order_mock", fill)

        executor._execute_single_leg = mock_execute

        result = executor._execute_legs(opp, legs, 5.0)
        assert result is True

        trades = db.get_trades_for_opportunity(1)
        assert len(trades) == 2
        for t in trades:
            assert t["slippage"] is not None
            assert t["slippage"] == pytest.approx(0.005)

    def test_zero_slippage_on_exact_fill(self, executor, db):
        """Zero slippage when fill price matches expected price."""
        executor.dry_run = False
        opp = {
            "type": "Binary",
            "market": "Test Zero Slippage",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.10,
            "net_roi": "11.8%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        legs = [
            {"platform": "polymarket", "side": "BUY", "token": "yes",
             "price": 0.40, "_token_id": "tok_yes"},
            {"platform": "polymarket", "side": "BUY", "token": "no",
             "price": 0.45, "_token_id": "tok_no"},
        ]

        def mock_execute(leg, size, opp_arg):
            leg["_order_id"] = "order_mock"
            return (True, "order_mock", leg["price"])  # Exact fill

        executor._execute_single_leg = mock_execute

        result = executor._execute_legs(opp, legs, 5.0)
        assert result is True

        trades = db.get_trades_for_opportunity(1)
        for t in trades:
            assert t["slippage"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _per_leg_budget — correct platform balance and leg division
# ---------------------------------------------------------------------------

class TestPerLegBudget:
    def test_kalshi_binary_uses_kalshi_balance(self, executor):
        """KalshiBinary divides Kalshi balance by 2 legs."""
        balances = {"polymarket": 100.0, "kalshi": 20.0}
        budget = executor._per_leg_budget("KalshiBinary", {}, balances)
        assert budget == pytest.approx(10.0)

    def test_kalshi_multi_divides_by_leg_count(self, executor):
        """KalshiMulti(3) divides Kalshi balance by 3 legs."""
        balances = {"kalshi": 30.0}
        budget = executor._per_leg_budget("KalshiMulti(3)", {}, balances)
        assert budget == pytest.approx(10.0)

    def test_kalshi_multi_parses_count_from_type(self, executor):
        """Parses leg count from type string like 'KalshiMulti(5)'."""
        balances = {"kalshi": 50.0}
        budget = executor._per_leg_budget("KalshiMulti(5)", {}, balances)
        assert budget == pytest.approx(10.0)

    def test_binary_uses_polymarket_balance(self, executor):
        """Binary uses Polymarket balance divided by 2."""
        balances = {"polymarket": 40.0, "kalshi": 100.0}
        budget = executor._per_leg_budget("Binary", {}, balances)
        assert budget == pytest.approx(20.0)

    def test_cross_uses_minimum_balance(self, executor):
        """Cross uses minimum of all platform balances (1 leg per platform)."""
        balances = {"polymarket": 40.0, "kalshi": 15.0}
        budget = executor._per_leg_budget("Cross(PM_YES + K_NO)", {}, balances)
        assert budget == pytest.approx(15.0)

    def test_no_balances_returns_none(self, executor):
        """Returns None when no balances available."""
        budget = executor._per_leg_budget("Binary", {}, None)
        assert budget is None

    def test_zero_balance_returns_zero(self, executor):
        """Returns 0.0 when platform balance is zero."""
        balances = {"kalshi": 0.0}
        budget = executor._per_leg_budget("KalshiBinary", {}, balances)
        assert budget == pytest.approx(0.0)

    def test_negrisk_parses_count(self, executor):
        """NegRisk(4) divides Polymarket balance by 4."""
        balances = {"polymarket": 40.0}
        budget = executor._per_leg_budget("NegRisk(4)", {}, balances)
        assert budget == pytest.approx(10.0)

    def test_negrisk_fallback_to_token_ids(self, executor):
        """NegRisk without count in type falls back to _token_ids length."""
        balances = {"polymarket": 30.0}
        opp = {"_token_ids": ["t1", "t2", "t3"]}
        budget = executor._per_leg_budget("NegRisk", opp, balances)
        assert budget == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Sequential leg execution for same-platform arbs
# ---------------------------------------------------------------------------

class TestSequentialLegExecution:
    def test_same_platform_legs_abort_on_failure(self, executor, db):
        """Same-platform legs should abort remaining legs after first failure."""
        executor.dry_run = False
        opp = {
            "type": "KalshiMulti(3)",
            "market": "Test Sequential",
            "prices": "0.20, 0.30, 0.40",
            "total_cost": "$0.9000",
            "net_profit": 0.10,
            "net_roi": "11.1%",
            "_clob_depth": 100.0,
            "_kalshi_tickers": ["T1", "T2", "T3"],
            "_kalshi_prices": [0.20, 0.30, 0.40],
        }
        legs = [
            {"platform": "kalshi", "side": "yes", "price": 0.20, "_ticker": "T1"},
            {"platform": "kalshi", "side": "yes", "price": 0.30, "_ticker": "T2"},
            {"platform": "kalshi", "side": "yes", "price": 0.40, "_ticker": "T3"},
        ]

        call_count = [0]
        def mock_execute(leg, size, opp_arg):
            call_count[0] += 1
            if call_count[0] == 1:
                # First leg fails
                return (False, None, None)
            # Should not reach here
            return (True, "order_mock", leg["price"])

        executor._execute_single_leg = mock_execute

        result = executor._execute_legs(opp, legs, 5.0)
        assert result is False
        # Only 1 leg should have been attempted (abort after failure)
        assert call_count[0] == 1

        trades = db.get_trades_for_opportunity(1)
        statuses = [t["status"] for t in trades]
        assert statuses[0] == "failed"
        # Remaining legs should be aborted
        assert statuses[1] == "aborted"
        assert statuses[2] == "aborted"

    def test_cross_platform_legs_execute_concurrently(self, executor, db):
        """Cross-platform legs (different exchanges) should both execute."""
        executor.dry_run = False
        opp = {
            "type": "Cross(PM_YES + K_NO)",
            "market": "Test Concurrent",
            "prices": "PM_Y=0.300 K_N=0.350",
            "total_cost": "$0.6500",
            "net_profit": 0.10,
            "net_roi": "15.4%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-XYZ",
        }
        legs = [
            {"platform": "polymarket", "side": "BUY", "price": 0.30, "_token_id": "tok_yes"},
            {"platform": "kalshi", "side": "no", "price": 0.35, "_ticker": "TICKER-XYZ"},
        ]

        def mock_execute(leg, size, opp_arg):
            leg["_order_id"] = "order_mock"
            return (True, "order_mock", leg["price"])

        executor._execute_single_leg = mock_execute

        result = executor._execute_legs(opp, legs, 5.0)
        assert result is True

        trades = db.get_trades_for_opportunity(1)
        assert len(trades) == 2
        assert all(t["status"] == "filled" for t in trades)


# ---------------------------------------------------------------------------
# _build_legs for TriangularCross
# ---------------------------------------------------------------------------

class TestBuildLegsTriangularCross:
    def _patch_all_platforms(self):
        """Return context managers enabling all platforms."""
        all_plats = frozenset([
            "polymarket", "kalshi", "betfair", "smarkets",
            "sxbet", "matchbook", "gemini", "ibkr",
        ])
        no_mins = {p: 0.01 for p in all_plats}
        import executor as _ex
        return (
            patch.object(_ex, "ENABLED_EXECUTION_PLATFORMS", all_plats),
            patch.object(_ex, "PLATFORM_MIN_ORDER_SIZE", no_mins),
        )

    def test_triangular_cross_routes_to_cross_all(self, executor):
        """TriangularCross should use _build_cross_all_legs via same price format."""
        opp = {
            "type": "TriangularCross",
            "prices": "polymarket_Y=0.350 kalshi_N=0.300",
            "_token_ids": ["tok_yes", "tok_no"],
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_kalshi_ticker": "TICKER-TRI",
        }
        p1, p2 = self._patch_all_platforms()
        with p1, p2:
            legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.350)
        assert legs[0]["_token_id"] == "tok_yes"
        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["price"] == pytest.approx(0.300)
        assert legs[1]["_ticker"] == "TICKER-TRI"

    def test_triangular_cross_betfair_smarkets(self, executor):
        """TriangularCross with betfair YES + smarkets NO."""
        opp = {
            "type": "TriangularCross",
            "prices": "betfair_Y=0.400 smarkets_N=0.250",
            "_platform_a": "betfair",
            "_platform_b": "smarkets",
            "_market_id": "1.234567",
            "_selection_id": 99999,
            "_sm_market_id": "sm_456",
            "_token_ids": [],
        }
        p1, p2 = self._patch_all_platforms()
        with p1, p2:
            legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "betfair"
        assert legs[0]["price"] == pytest.approx(0.400)
        assert legs[1]["platform"] == "smarkets"
        assert legs[1]["price"] == pytest.approx(0.250)

    def test_triangular_cross_empty_on_bad_prices(self, executor):
        """TriangularCross with malformed prices returns empty legs."""
        opp = {
            "type": "TriangularCross",
            "prices": "garbage",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": [],
        }
        legs = executor._build_legs(opp, 5.0)
        assert legs == []


# ---------------------------------------------------------------------------
# _build_legs for EventDivergence
# ---------------------------------------------------------------------------

class TestBuildLegsEventDivergence:
    def test_event_divergence_buy_yes_polymarket(self, executor):
        """EventDivergence BUY_YES on Polymarket produces one leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.400 metaculus=0.600",
            "_platform": "polymarket",
            "_direction": "BUY_YES",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.400)
        assert legs[0]["token"] == "yes"
        assert legs[0]["_token_id"] == "tok_yes"

    def test_event_divergence_buy_no_polymarket(self, executor):
        """EventDivergence BUY_NO on Polymarket produces one leg at NO price."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.700 metaculus=0.500",
            "_platform": "polymarket",
            "_direction": "BUY_NO",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["price"] == pytest.approx(0.300)  # 1.0 - 0.7
        assert legs[0]["token"] == "no"
        assert legs[0]["_token_id"] == "tok_no"

    def test_event_divergence_buy_yes_kalshi(self, executor):
        """EventDivergence BUY_YES on Kalshi produces one leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.350 metaculus=0.550",
            "_platform": "kalshi",
            "_direction": "BUY_YES",
            "_kalshi_ticker": "TICKER-ED",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "kalshi"
        assert legs[0]["price"] == pytest.approx(0.350)
        assert legs[0]["side"] == "yes"
        assert legs[0]["action"] == "buy"
        assert legs[0]["_ticker"] == "TICKER-ED"

    def test_event_divergence_missing_platform(self, executor):
        """EventDivergence with missing _platform returns empty legs."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.400 metaculus=0.600",
            "_platform": "",
            "_direction": "BUY_YES",
        }
        legs = executor._build_legs(opp, 5.0)
        assert legs == []

    def test_event_divergence_unsupported_platform(self, executor):
        """EventDivergence on truly unsupported platform returns empty legs."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.400 metaculus=0.600",
            "_platform": "some_unknown_platform",
            "_direction": "BUY_YES",
        }
        legs = executor._build_legs(opp, 5.0)
        assert legs == []


# ---------------------------------------------------------------------------
# _revalidate_triangular
# ---------------------------------------------------------------------------

class TestRevalidateTriangular:
    def test_revalidate_triangular_passes(self, executor):
        """TriangularCross revalidation passes when profit stays above threshold."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "TriangularCross",
            "net_profit": 0.08,
            "total_cost": "$0.7000",
            "prices": "polymarket_Y=0.350 kalshi_N=0.350",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-TRI",
        }
        mock_book = {"asks": [{"price": "0.35", "size": "100"}]}
        mock_bid_ask = {"bid": 0.34, "ask": 0.35}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_triangular", return_value={"net_profit": 0.075, "gross_spread": 0.30, "fees": 0.01}):
            result = executor._revalidate(opp, None)
            # ROI = 0.08/0.70 = 11.4% (>5%) -> strict 90%: 0.075 >= 0.072 -> pass
            assert result is True

    def test_revalidate_triangular_degraded(self, executor):
        """TriangularCross revalidation fails when profit drops too much."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "TriangularCross",
            "net_profit": 0.10,
            "total_cost": "$0.7000",
            "prices": "polymarket_Y=0.350 kalshi_N=0.350",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "TICKER-TRI",
        }
        mock_book = {"asks": [{"price": "0.45", "size": "100"}]}
        mock_bid_ask = {"bid": 0.44, "ask": 0.45}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_triangular", return_value={"net_profit": 0.02, "gross_spread": 0.10, "fees": 0.08}):
            result = executor._revalidate(opp, None)
            # ROI = 0.10/0.70 = 14.3% (>5%) -> strict 90%: 0.02 < 0.09 -> fail
            assert result is False

    def test_revalidate_triangular_missing_platforms_high_roi(self, executor):
        """TriangularCross with missing platform info fails closed at any ROI."""
        opp = {
            "type": "TriangularCross",
            "net_profit": 0.10,
            "total_cost": "$0.7000",
            "prices": "polymarket_Y=0.350 kalshi_N=0.350",
            "_platform_a": "",
            "_platform_b": "",
        }
        result = executor._revalidate(opp, None)
        assert result is False

    def test_revalidate_triangular_missing_platforms_low_roi(self, executor):
        """TriangularCross with missing platform info and low ROI: rejected."""
        opp = {
            "type": "TriangularCross",
            "net_profit": 0.005,
            "total_cost": "$1.0000",
            "prices": "polymarket_Y=0.500 kalshi_N=0.500",
            "_platform_a": "",
            "_platform_b": "",
        }
        result = executor._revalidate(opp, None)
        assert result is False  # ROI 0.5% < 2%, so API error is rejected

    def test_revalidate_event_divergence_passes(self, executor):
        """EventDivergence should always pass revalidation (signal-based)."""
        opp = {
            "type": "EventDivergence",
            "net_profit": 0.05,
        }
        result = executor._revalidate(opp, None)
        assert result is True


class TestRevalidateLogicalArb:
    def test_invalid_executable_ask_fails_closed(self, executor):
        opp = {
            "type": "LogicalArb",
            "net_profit": 0.10,
            "total_cost": "$0.7000",
            "_then_price": 0.40,
            "_token_ids": ["then-yes", "then-no"],
            "_if_token_ids": ["if-yes", "if-no"],
        }
        books = [
            {"asks": [{"price": "nan", "size": "100"}]},
            {"asks": [{"price": "0.35", "size": "100"}]},
        ]

        with patch("executor.fetch_order_book", side_effect=books):
            assert executor._revalidate(opp, None) is False


# ---------------------------------------------------------------------------
# _revalidate_negrisk
# ---------------------------------------------------------------------------

class TestRevalidateNegRisk:
    def test_passes_when_profit_above_threshold(self, executor):
        """NegRisk revalidation passes when profit stays above 90% of original."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.10,
            "total_cost": "$0.9000",
            "prices": "Y1=0.45 Y2=0.45",
            "_token_ids": ["tok_y1", "tok_y2"],
        }
        mock_book = {"asks": [{"price": "0.45", "size": "100"}]}
        mock_bid_ask = {"bid": 0.44, "ask": 0.45}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_negrisk_internal", return_value={"net_profit": 0.095}):
            result = executor._revalidate(opp, None)
            assert result is True

    def test_fails_when_no_token_ids(self, executor):
        """NegRisk revalidation fails with empty _token_ids."""
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.10,
            "_token_ids": [],
        }
        result = executor._revalidate(opp, None)
        assert result is False

    def test_api_error_rejected_when_high_roi(self, executor):
        """NegRisk: API errors fail closed even when scan ROI is high."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.10,
            "total_cost": "$0.9000",
            "_token_ids": ["tok_y1"],
        }
        with mpatch("executor.fetch_order_book", return_value=None):
            result = executor._revalidate(opp, None)
            assert result is False

    def test_api_error_rejected_when_low_roi(self, executor):
        """NegRisk: API error rejected when ROI < 2%."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.005,
            "total_cost": "$1.0000",
            "_token_ids": ["tok_y1"],
        }
        with mpatch("executor.fetch_order_book", return_value=None):
            result = executor._revalidate(opp, None)
            assert result is False  # ROI 0.5% < 2%, API error rejected

    def test_fails_when_profit_degrades(self, executor):
        """NegRisk revalidation fails when new profit < 90% of original."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.10,
            "total_cost": "$0.9000",
            "prices": "Y1=0.45 Y2=0.45",
            "_token_ids": ["tok_y1", "tok_y2"],
        }
        mock_book = {"asks": [{"price": "0.50", "size": "100"}]}
        mock_bid_ask = {"bid": 0.49, "ask": 0.50}

        with mpatch("executor.fetch_order_book", return_value=mock_book), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_negrisk_internal", return_value={"net_profit": 0.02}):
            result = executor._revalidate(opp, None)
            assert result is False

    def test_uses_ws_cache_when_available(self, executor):
        """NegRisk revalidation uses WS price cache, skipping fetch_order_book."""
        from unittest.mock import patch as mpatch
        opp = {
            "type": "NegRiskInternal",
            "net_profit": 0.10,
            "total_cost": "$0.9000",
            "prices": "Y1=0.45 Y2=0.45",
            "_token_ids": ["tok_y1", "tok_y2"],
        }
        price_cache = {
            ("polymarket", "tok_y1"): {"price": 0.45, "_ts": time.time()},
            ("polymarket", "tok_y2"): {"price": 0.46, "_ts": time.time()},
        }
        with mpatch("executor.fetch_order_book") as mock_fetch, \
             mpatch("executor.net_profit_negrisk_internal", return_value={"net_profit": 0.095}):
            result = executor._revalidate(opp, price_cache)
            assert result is True
            mock_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# _revalidate_kalshi_multi
# ---------------------------------------------------------------------------

class TestRevalidateKalshiMulti:
    def test_passes_when_profit_above_threshold(self, executor):
        """KalshiMulti revalidation passes when profit stays above threshold."""
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.10,
            "total_cost": "$0.8000",
            "_kalshi_tickers": ["TICKER-A", "TICKER-B"],
        }
        mock_book = {
            "orderbook": {
                "yes": [],
                "no": [["55", "100"]],
            }
        }
        executor.kalshi_client.fetch_order_book.return_value = mock_book
        with patch("executor.net_profit_kalshi_multi", return_value={"net_profit": 0.095}):
            result = executor._revalidate(opp, None)
            assert result is True

    def test_fails_when_no_tickers(self, executor):
        """KalshiMulti revalidation fails with empty _kalshi_tickers."""
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.10,
            "_kalshi_tickers": [],
        }
        result = executor._revalidate(opp, None)
        assert result is False

    def test_fails_when_no_kalshi_client(self, ArbitrageExecutor, db, risk_manager):
        """KalshiMulti revalidation fails when kalshi_client is None."""
        ex = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=None,
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.10,
            "_kalshi_tickers": ["TICKER-A"],
        }
        result = ex._revalidate(opp, None)
        assert result is False

    def test_api_error_rejected_when_high_roi(self, executor):
        """KalshiMulti: API errors fail closed even when scan ROI is high."""
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.10,
            "total_cost": "$0.8000",
            "_kalshi_tickers": ["TICKER-A", "TICKER-B"],
        }
        executor.kalshi_client.fetch_order_book.return_value = None
        result = executor._revalidate(opp, None)
        assert result is False

    def test_api_error_rejected_when_low_roi(self, executor):
        """KalshiMulti: API error rejected when ROI < 2%."""
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.005,
            "total_cost": "$1.0000",
            "_kalshi_tickers": ["TICKER-A", "TICKER-B"],
        }
        executor.kalshi_client.fetch_order_book.return_value = None
        result = executor._revalidate(opp, None)
        assert result is False  # ROI 0.5% < 2%, API error rejected

    def test_fails_when_profit_degrades(self, executor):
        """KalshiMulti revalidation fails when profit drops below threshold.

        Schema note: YES ask = 1 - best_no_bid_price. To simulate a YES ask
        of $0.50 with depth 100, the NO side needs a bid at $0.50 with size 100.
        """
        opp = {
            "type": "KalshiMultiOutcome",
            "net_profit": 0.10,
            "total_cost": "$0.8000",
            "_kalshi_tickers": ["TICKER-A", "TICKER-B"],
        }
        mock_book = {
            "orderbook": {
                "yes": [],
                "no": [[50, 100]],  # NO bid @ $0.50 -> YES ask = $0.50, depth 100
            }
        }
        executor.kalshi_client.fetch_order_book.return_value = mock_book
        with patch("executor.net_profit_kalshi_multi", return_value={"net_profit": 0.02}):
            result = executor._revalidate(opp, None)
            assert result is False


# ---------------------------------------------------------------------------
# Fill confirmation for exchange platforms
# ---------------------------------------------------------------------------

class TestFillConfirmationExchanges:
    def test_confirm_fill_betfair_filled(self, executor):
        """Betfair fill confirm returns price when EXECUTION_COMPLETE."""
        mock_bf = MagicMock()
        mock_bf.get_order_status.return_value = {
            "status": "EXECUTION_COMPLETE",
            "averagePriceMatched": 2.5,  # decimal odds
        }
        executor.betfair_client = mock_bf
        result = executor._confirm_fill_betfair("bet123", 0.40)
        assert result == pytest.approx(1.0 / 2.5)

    def test_confirm_fill_betfair_timeout(self, ArbitrageExecutor, db, risk_manager):
        """Betfair fill confirm returns expected_price on timeout."""
        from unittest.mock import patch as mpatch
        ex = ArbitrageExecutor(
            pm_trader=MagicMock(), kalshi_client=MagicMock(),
            db=db, risk_manager=risk_manager, dry_run=True,
        )
        mock_bf = MagicMock()
        mock_bf.get_order_status.return_value = {"status": "EXECUTABLE"}
        ex.betfair_client = mock_bf
        with mpatch("executor.FILL_POLL_TIMEOUT", 0.01), \
             mpatch("executor.FILL_POLL_INTERVAL", 0.005):
            result = ex._confirm_fill_betfair("bet123", 0.40)
        assert result is None  # timeout returns None, not expected_price

    def test_confirm_fill_betfair_no_client(self, executor):
        """Returns None when no betfair client (cannot confirm = treat as not filled)."""
        executor.betfair_client = None
        result = executor._confirm_fill_betfair("bet123", 0.40)
        assert result is None

    def test_confirm_fill_smarkets_filled(self, executor):
        """Smarkets fill confirm returns price when matched."""
        mock_sm = MagicMock()
        mock_sm.get_order_status.return_value = {
            "state": "matched",
            "avg_price": 4000,  # basis points (40%)
        }
        executor.smarkets_client = mock_sm
        result = executor._confirm_fill_smarkets("order456", 0.40)
        assert result == pytest.approx(0.40)

    def test_confirm_fill_smarkets_cancelled(self, executor):
        """Smarkets fill confirm returns None on cancel (cancelled order is not a fill)."""
        mock_sm = MagicMock()
        mock_sm.get_order_status.return_value = {"state": "cancelled"}
        executor.smarkets_client = mock_sm
        result = executor._confirm_fill_smarkets("order456", 0.40)
        assert result is None

    def test_confirm_fill_sxbet_filled(self, executor):
        """SX Bet fill confirm returns price when FILLED."""
        mock_sx = MagicMock()
        mock_sx.get_order_status.return_value = {
            "status": "FILLED",
            "avgPrice": 0.42,
        }
        executor.sxbet_client = mock_sx
        result = executor._confirm_fill_sxbet("order789", 0.40)
        assert result == pytest.approx(0.42)

    def test_confirm_fill_sxbet_no_client(self, executor):
        """Returns None when no sxbet client (cannot confirm = treat as not filled)."""
        executor.sxbet_client = None
        result = executor._confirm_fill_sxbet("order789", 0.40)
        assert result is None

    def test_confirm_fill_matchbook_filled(self, executor):
        """Matchbook fill confirm returns price when matched."""
        mock_mb = MagicMock()
        mock_mb.get_order_status.return_value = {
            "status": "matched",
            "matched-odds": 2.5,  # decimal odds
        }
        executor.matchbook_client = mock_mb
        result = executor._confirm_fill_matchbook("offer123", 0.40)
        assert result == pytest.approx(1.0 / 2.5)

    def test_confirm_fill_matchbook_expired(self, executor):
        """Matchbook fill confirm returns None on expired (expired order is not a fill)."""
        mock_mb = MagicMock()
        mock_mb.get_order_status.return_value = {"status": "expired"}
        executor.matchbook_client = mock_mb
        result = executor._confirm_fill_matchbook("offer123", 0.40)
        assert result is None


# ---------------------------------------------------------------------------
# Cancel leg for exchange platforms
# ---------------------------------------------------------------------------

class TestCancelLegExchanges:
    def test_cancel_betfair(self, executor):
        """Cancel leg on Betfair calls cancel_orders with market_id and bet_id."""
        mock_bf = MagicMock()
        mock_bf.cancel_orders.return_value = True
        executor.betfair_client = mock_bf
        leg = {"platform": "betfair", "_order_id": "bet123", "_market_id": "1.234"}
        assert executor._cancel_leg(leg) is True
        mock_bf.cancel_orders.assert_called_once_with("1.234", ["bet123"])

    def test_cancel_smarkets(self, executor):
        """Cancel leg on Smarkets calls cancel_order."""
        mock_sm = MagicMock()
        mock_sm.cancel_order.return_value = True
        executor.smarkets_client = mock_sm
        leg = {"platform": "smarkets", "_order_id": "order456"}
        assert executor._cancel_leg(leg) is True
        mock_sm.cancel_order.assert_called_once_with("order456")

    def test_cancel_sxbet(self, executor):
        """Cancel leg on SX Bet calls cancel_order."""
        mock_sx = MagicMock()
        mock_sx.cancel_order.return_value = True
        executor.sxbet_client = mock_sx
        leg = {"platform": "sxbet", "_order_id": "order789"}
        assert executor._cancel_leg(leg) is True
        mock_sx.cancel_order.assert_called_once_with("order789")

    def test_cancel_matchbook(self, executor):
        """Cancel leg on Matchbook calls cancel_order."""
        mock_mb = MagicMock()
        mock_mb.cancel_order.return_value = True
        executor.matchbook_client = mock_mb
        leg = {"platform": "matchbook", "_order_id": "offer123"}
        assert executor._cancel_leg(leg) is True
        mock_mb.cancel_order.assert_called_once_with("offer123")

    def test_cancel_unknown_platform(self, executor):
        """Cancel on unknown platform returns False."""
        leg = {"platform": "unknown", "_order_id": "xxx"}
        assert executor._cancel_leg(leg) is False

    def test_cancel_no_order_id(self, executor):
        """Cancel with no order_id returns False."""
        leg = {"platform": "betfair"}
        assert executor._cancel_leg(leg) is False


# ---------------------------------------------------------------------------
# _per_leg_budget for exchange platforms
# ---------------------------------------------------------------------------

class TestPerLegBudgetExchanges:
    def test_betfair_back_all_budget(self, executor):
        """BetfairBackAll divides betfair balance by number of selections."""
        balances = {"betfair": 100.0}
        opp = {"_bf_selection_ids": [1, 2, 3, 4]}
        budget = executor._per_leg_budget("BetfairBackAll", opp, balances)
        assert budget == pytest.approx(25.0)

    def test_betfair_backlay_budget(self, executor):
        """BetfairBackLay divides betfair balance by 2."""
        balances = {"betfair": 50.0}
        budget = executor._per_leg_budget("BetfairBackLay", {}, balances)
        assert budget == pytest.approx(25.0)

    def test_smarkets_backall_budget(self, executor):
        """SmarketsBackAll uses smarkets balance."""
        balances = {"smarkets": 60.0}
        opp = {"_sm_contract_ids": ["c1", "c2", "c3"]}
        budget = executor._per_leg_budget("SmarketsBackAll", opp, balances)
        assert budget == pytest.approx(20.0)

    def test_sxbet_backlay_budget(self, executor):
        """SXBetBackLay divides sxbet balance by 2."""
        balances = {"sxbet": 40.0}
        budget = executor._per_leg_budget("SXBetBackLay", {}, balances)
        assert budget == pytest.approx(20.0)

    def test_matchbook_budget(self, executor):
        """MatchbookBackAll uses matchbook balance."""
        balances = {"matchbook": 80.0}
        opp = {"_mb_runner_ids": [1, 2]}
        budget = executor._per_leg_budget("MatchbookBackAll", opp, balances)
        assert budget == pytest.approx(40.0)

    def test_missing_exchange_balance_returns_none(self, executor):
        """Returns 0.0 when exchange balance not available."""
        balances = {"polymarket": 100.0}
        budget = executor._per_leg_budget("BetfairBackAll", {}, balances)
        # betfair not in balances -> balance is None -> returns 0.0
        assert budget == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# _fetch_balances for exchange-specific arb types
# ---------------------------------------------------------------------------

class TestFetchBalancesExchangeTypes:
    def test_betfair_type_fetches_betfair_balance(self, executor):
        """BetfairBackAll fetches betfair balance."""
        mock_bf = MagicMock()
        mock_bf.get_balance.return_value = 200.0
        executor.betfair_client = mock_bf
        balances = executor._fetch_balances("BetfairBackAll")
        assert "betfair" in balances
        assert balances["betfair"] == 200.0

    def test_smarkets_type_fetches_smarkets_balance(self, executor):
        """SmarketsBackLay fetches smarkets balance."""
        mock_sm = MagicMock()
        mock_sm.get_balance.return_value = 150.0
        executor.smarkets_client = mock_sm
        balances = executor._fetch_balances("SmarketsBackLay")
        assert "smarkets" in balances
        assert balances["smarkets"] == 150.0

    def test_sxbet_type_fetches_sxbet_balance(self, executor):
        """SXBetBackAll fetches sxbet balance."""
        mock_sx = MagicMock()
        mock_sx.get_balance.return_value = 300.0
        executor.sxbet_client = mock_sx
        balances = executor._fetch_balances("SXBetBackAll")
        assert "sxbet" in balances

    def test_matchbook_type_fetches_matchbook_balance(self, executor):
        """MatchbookBackAll fetches matchbook balance."""
        mock_mb = MagicMock()
        mock_mb.get_balance.return_value = 400.0
        executor.matchbook_client = mock_mb
        balances = executor._fetch_balances("MatchbookBackAll")
        assert "matchbook" in balances
        assert balances["matchbook"] == 400.0

    def test_event_divergence_fetches_exchange_balances(self, executor):
        """EventDivergence fetches all exchange balances."""
        mock_bf = MagicMock()
        mock_bf.get_balance.return_value = 100.0
        executor.betfair_client = mock_bf
        balances = executor._fetch_balances("EventDivergence")
        assert "betfair" in balances


# ---------------------------------------------------------------------------
# EventDivergence legs for exchange platforms
# ---------------------------------------------------------------------------

class TestBuildLegsEventDivergenceExchanges:
    def test_event_divergence_buy_yes_betfair(self, executor):
        """EventDivergence BUY_YES on Betfair produces one BACK leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.400 metaculus=0.600",
            "_platform": "betfair",
            "_direction": "BUY_YES",
            "_market_id": "1.234567",
            "_selection_id": 99999,
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "betfair"
        assert legs[0]["side"] == "BACK"
        assert legs[0]["_market_id"] == "1.234567"
        assert legs[0]["_selection_id"] == 99999

    def test_event_divergence_buy_no_betfair(self, executor):
        """EventDivergence BUY_NO on Betfair produces one LAY leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.700 metaculus=0.500",
            "_platform": "betfair",
            "_direction": "BUY_NO",
            "_market_id": "1.234567",
            "_selection_id": 99999,
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "betfair"
        assert legs[0]["side"] == "LAY"

    def test_event_divergence_buy_yes_smarkets(self, executor):
        """EventDivergence BUY_YES on Smarkets produces one BACK leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.350 metaculus=0.550",
            "_platform": "smarkets",
            "_direction": "BUY_YES",
            "_sm_market_id": "sm_123",
            "_sm_contract_id": "c_456",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "smarkets"
        assert legs[0]["side"] == "BACK"
        assert legs[0]["_market_id"] == "sm_123"
        assert legs[0]["_contract_id"] == "c_456"

    def test_event_divergence_buy_no_sxbet(self, executor):
        """EventDivergence BUY_NO on SX Bet produces one LAY leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.700 metaculus=0.500",
            "_platform": "sxbet",
            "_direction": "BUY_NO",
            "_sx_market_hash": "0xabc",
            "_sx_outcome_id": "out1",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "sxbet"
        assert legs[0]["side"] == "LAY"
        assert legs[0]["_market_hash"] == "0xabc"
        assert legs[0]["_outcome_id"] == "out1"

    def test_event_divergence_buy_yes_matchbook(self, executor):
        """EventDivergence BUY_YES on Matchbook produces one back leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.400 metaculus=0.600",
            "_platform": "matchbook",
            "_direction": "BUY_YES",
            "_mb_market_id": "mb_123",
            "_mb_runner_id": "r_456",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "matchbook"
        assert legs[0]["side"] == "back"
        assert legs[0]["_market_id"] == "mb_123"
        assert legs[0]["_runner_id"] == "r_456"

    def test_event_divergence_buy_no_matchbook(self, executor):
        """EventDivergence BUY_NO on Matchbook produces one lay leg."""
        opp = {
            "type": "EventDivergence",
            "prices": "platform=0.700 metaculus=0.500",
            "_platform": "matchbook",
            "_direction": "BUY_NO",
            "_mb_market_id": "mb_123",
            "_mb_runner_id": "r_456",
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 1
        assert legs[0]["platform"] == "matchbook"
        assert legs[0]["side"] == "lay"


# ---------------------------------------------------------------------------
# Platform execution whitelist
# ---------------------------------------------------------------------------

class TestPlatformWhitelist:

    def test_execute_single_leg_rejects_non_whitelisted_platform(self, executor):
        """Leg on a platform not in ENABLED_EXECUTION_PLATFORMS returns False."""
        leg = {"platform": "betfair", "side": "BACK", "price": 0.50}
        opp = {"type": "BetfairBackLay"}
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset(["polymarket", "kalshi"])):
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        assert success is False
        assert order_id is None

    def test_execute_single_leg_allows_whitelisted_platform(self, executor):
        """Leg on a whitelisted platform proceeds past the guard."""
        leg = {"platform": "polymarket", "side": "BUY", "price": 0.50,
               "_token_id": "tok123"}
        opp = {"type": "Binary"}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "ord1"
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset(["polymarket", "kalshi"])):
            with patch.object(executor, "_confirm_fill_pm", return_value=0.50):
                success, order_id, fill_price = executor._execute_single_leg(
                    leg, 5.0, opp)
        assert success is True
        assert order_id == "ord1"

    def test_persist_order_id_before_fill_poll(self, executor):
        """Order ID must be written to DB before fill confirmation completes."""
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "price": 0.50,
            "_token_id": "tok123",
            "_trade_id": 42,
            "_idempotency_key": "idem-xyz",
        }
        opp = {"type": "Binary"}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "ord-persist-1",
        }
        call_order: list[str] = []
        update_mock = MagicMock()

        def _track_update(*a, **kw):
            call_order.append("update")
            update_mock(*a, **kw)

        def _track_confirm(*a, **kw):
            call_order.append("confirm")
            return 0.50

        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset(["polymarket", "kalshi"])), \
             patch.object(executor.db, "update_trade_status", side_effect=_track_update), \
             patch.object(executor, "_confirm_fill_pm", side_effect=_track_confirm):
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        assert success is True
        assert order_id == "ord-persist-1"
        assert "update" in call_order and "confirm" in call_order
        assert call_order.index("update") < call_order.index("confirm")
        # First update must carry the order_id while still pending
        first_kw = update_mock.call_args_list[0].kwargs
        assert first_kw.get("order_id") == "ord-persist-1"

    def test_cross_all_legs_rejected_when_platform_not_whitelisted(self, executor):
        """Cross-all with a non-whitelisted platform returns empty legs."""
        opp = {
            "type": "CrossAll",
            "prices": "polymarket_Y=0.45 smarkets_N=0.50",
            "_platform_a": "polymarket",
            "_platform_b": "smarkets",
            "_token_ids": ["tok1"],
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS", frozenset(["polymarket", "kalshi"])):
            legs = executor._build_cross_all_legs(opp, 5.0)
        assert legs == []

    def test_cross_all_legs_allowed_when_both_whitelisted(self, executor):
        """Cross-all with both platforms whitelisted builds legs normally."""
        opp = {
            "type": "CrossAll",
            "prices": "polymarket_Y=0.45 kalshi_N=0.50",
            "_platform_a": "polymarket",
            "_platform_b": "kalshi",
            "_token_ids": ["tok1"],
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])):
            legs = executor._build_cross_all_legs(opp, 5.0)
        assert len(legs) == 2


# ---------------------------------------------------------------------------
# Minimum order size enforcement
# ---------------------------------------------------------------------------

class TestMinOrderSize:

    def test_rejects_order_below_platform_minimum(self, executor):
        """Order below platform min returns False without calling API."""
        leg = {"platform": "smarkets", "side": "BACK", "price": 0.50}
        opp = {"type": "SmarketsBackAll"}
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi", "smarkets"])):
            with patch("executor.PLATFORM_MIN_ORDER_SIZE",
                       {"smarkets": 6.25, "polymarket": 0.01, "kalshi": 0.01}):
                success, order_id, fill_price = executor._execute_single_leg(
                    leg, 3.0, opp)
        assert success is False
        assert order_id is None

    def test_allows_order_above_platform_minimum(self, executor):
        """Order above platform min proceeds past the guard."""
        leg = {"platform": "kalshi", "side": "yes", "action": "buy",
               "price": 0.50, "_ticker": "TICK-1"}
        opp = {"type": "KalshiBinary"}
        executor.kalshi_client.place_order.return_value = {
            "order": {"order_id": "k1", "status": "executed"},
        }
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])):
            with patch("executor.PLATFORM_MIN_ORDER_SIZE",
                       {"kalshi": 0.01, "polymarket": 0.01}):
                success, order_id, fill_price = executor._execute_single_leg(
                    leg, 3.0, opp)
        assert success is True

    def test_kalshi_sports_ticker_does_not_place(self, executor):
        leg = {"platform": "kalshi", "side": "yes", "action": "buy",
               "price": 0.50, "_ticker": "KXNFLGAME-26AUG18"}
        opp = {"type": "KalshiBinary"}
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                   frozenset(["polymarket", "kalshi"])):
            success, _, _ = executor._execute_single_leg(leg, 3.0, opp)
        assert success is False
        executor.kalshi_client.place_order.assert_not_called()

    def test_cross_all_rejects_when_per_leg_size_below_minimum(self, executor):
        """Cross-all with per-leg size below platform min returns empty."""
        opp = {
            "type": "CrossAll",
            "prices": "polymarket_Y=0.45 betfair_N=0.50",
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "_token_ids": ["tok1"],
        }
        # size=4.0, per-leg=2.0, betfair min=2.50 -> rejected
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "betfair"])):
            with patch("executor.PLATFORM_MIN_ORDER_SIZE",
                       {"polymarket": 0.01, "betfair": 2.50}):
                legs = executor._build_cross_all_legs(opp, 4.0)
        assert legs == []

    def test_cross_all_allows_when_per_leg_size_above_minimum(self, executor):
        """Cross-all with per-leg size above platform min builds legs."""
        opp = {
            "type": "CrossAll",
            "prices": "polymarket_Y=0.45 betfair_N=0.50",
            "_platform_a": "polymarket",
            "_platform_b": "betfair",
            "_token_ids": ["tok1"],
        }
        # size=6.0, per-leg=3.0, betfair min=2.50 -> allowed
        with patch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "betfair"])):
            with patch("executor.PLATFORM_MIN_ORDER_SIZE",
                       {"polymarket": 0.01, "betfair": 2.50}):
                legs = executor._build_cross_all_legs(opp, 6.0)
        assert len(legs) == 2


# ---------------------------------------------------------------------------
# Layer-aware revalidation floors (Plan 05-02, D-02)
# ---------------------------------------------------------------------------

class TestLayerAwareRevalidation:
    """Tests that _get_revalidation_threshold uses layer-specific floors from REVAL_FLOORS."""

    def test_layer1_floor_applied(self, executor):
        """Layer 1 opp with low ROI uses 2% (0.02) floor, not global minimum."""
        opp = {
            "type": "Binary",
            "_layer": 1,
            "net_profit": 0.005,
            "total_cost": "$1.0000",
        }
        # ROI = 0.5% (< 2%) → layer 1 floor = 0.02
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.02), (
            f"Expected Layer 1 floor 0.02, got {threshold}"
        )

    def test_layer2_floor_applied(self, executor):
        """Layer 2 opp with low ROI uses 5% (0.05) floor."""
        opp = {
            "type": "StalePriceOpp",
            "_layer": 2,
            "net_profit": 0.005,
            "total_cost": "$1.0000",
        }
        # ROI = 0.5% (< 2%) → layer 2 floor = 0.05
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.05), (
            f"Expected Layer 2 floor 0.05, got {threshold}"
        )

    def test_layer3_floor_applied(self, executor):
        """Layer 3 opp with low ROI uses 3% (0.03) floor."""
        opp = {
            "type": "MarketMake",
            "_layer": 3,
            "net_profit": 0.005,
            "total_cost": "$1.0000",
        }
        # ROI = 0.5% (< 2%) → layer 3 floor = 0.03
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.03), (
            f"Expected Layer 3 floor 0.03, got {threshold}"
        )

    def test_layer4_floor_applied(self, executor):
        """Layer 4 opp with low ROI uses 10% (0.10) floor."""
        opp = {
            "type": "ConvergenceOpp",
            "_layer": 4,
            "net_profit": 0.005,
            "total_cost": "$1.0000",
        }
        # ROI = 0.5% (< 2%) → layer 4 floor = 0.10
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.10), (
            f"Expected Layer 4 floor 0.10, got {threshold}"
        )

    def test_missing_layer_falls_back_to_get_layer(self, executor):
        """Opp without _layer key falls back to get_layer(opp['type'])."""
        opp = {
            "type": "Binary",
            # No _layer key
            "net_profit": 0.005,
            "total_cost": "$1.0000",
        }
        # get_layer("Binary") returns 1 → floor 0.02
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.02), (
            f"Expected Layer 1 floor 0.02 via get_layer fallback, got {threshold}"
        )

    def test_high_roi_not_affected_by_layer(self, executor):
        """High-ROI opps use percentage-based threshold regardless of layer."""
        opp = {
            "type": "Binary",
            "_layer": 1,
            "net_profit": 0.10,
            "total_cost": "$0.85",  # ROI = 11.8% >= 5%
        }
        # High ROI path: 90% of original profit (not layer floor)
        threshold = executor._get_revalidation_threshold(opp["net_profit"], opp)
        assert threshold == pytest.approx(0.09), (
            f"Expected 90% of 0.10 = 0.09 for high-ROI path, got {threshold}"
        )


# ---------------------------------------------------------------------------
# Calibration logging — REVAL| structured format (Plan 05-02, D-01)
# ---------------------------------------------------------------------------

class TestCalibrationLogging:
    """Tests that every revalidation decision emits a structured REVAL| log line."""

    def test_reval_logs_structured_format_on_pass(self, executor):
        """Verify REVAL| log line emitted when revalidation passes."""
        from unittest.mock import patch as mpatch
        import logging
        opp = {
            "type": "Binary",
            "_layer": 1,
            "net_profit": 0.10,
            "total_cost": "$0.8500",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        mock_bid_ask = {"bid": 0.39, "ask": 0.40}
        with mpatch("executor.fetch_order_book", return_value={}), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_binary_internal",
                    return_value={"net_profit": 0.095}), \
             mpatch("executor.logger") as mock_logger:
            executor._revalidate(opp, None)

        # Check that info was called with a REVAL| pattern
        calls = [str(c) for c in mock_logger.info.call_args_list]
        reval_calls = [c for c in calls if "REVAL|" in c]
        assert len(reval_calls) >= 1, (
            f"Expected at least one REVAL| log call, got: {calls}"
        )

    def test_reval_log_contains_required_fields(self, executor):
        """REVAL| log contains layer=, type=, scan_roi=, reval_roi=, passed=, floor=."""
        from unittest.mock import patch as mpatch, call
        opp = {
            "type": "Binary",
            "_layer": 1,
            "net_profit": 0.10,
            "total_cost": "$0.8500",
            "_token_ids": ["tok_yes", "tok_no"],
        }
        mock_bid_ask = {"bid": 0.39, "ask": 0.40}
        captured_args = []

        def capture_info(fmt, *args, **kwargs):
            captured_args.append((fmt, args))

        with mpatch("executor.fetch_order_book", return_value={}), \
             mpatch("executor.get_best_bid_ask", return_value=mock_bid_ask), \
             mpatch("executor.net_profit_binary_internal",
                    return_value={"net_profit": 0.095}), \
             mpatch("executor.logger") as mock_logger:
            mock_logger.info.side_effect = capture_info
            executor._revalidate(opp, None)

        # Find the REVAL| call
        reval_fmt = next(
            (fmt for fmt, args in captured_args if "REVAL|" in fmt),
            None
        )
        assert reval_fmt is not None, "No REVAL| log call found"
        assert "layer=" in reval_fmt
        assert "type=" in reval_fmt
        assert "scan_roi=" in reval_fmt
        assert "reval_roi=" in reval_fmt
        assert "passed=" in reval_fmt
        assert "floor=" in reval_fmt


# ---------------------------------------------------------------------------
# Maker routing — GTC order placement (Plan 05-02, D-05)
# ---------------------------------------------------------------------------

class TestMakerRouting:
    """Tests that Polymarket and Kalshi legs use GTC (maker) order routing."""

    def test_gtc_order_placed_when_configured(self, executor):
        """When ORDER_TIME_IN_FORCE is 'gtc', Polymarket leg uses order_type=GTC."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1}
        executor.pm_trader.place_order.return_value = {"success": True, "orderID": "order_gtc_123"}
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=0.45):
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        # Verify place_order was called with order_type="GTC" and share qty
        assert executor.pm_trader.place_order.called
        call_kwargs = executor.pm_trader.place_order.call_args
        order_type = (call_kwargs.kwargs or {}).get("order_type")
        assert order_type == "GTC"
        # $5 @ $0.45 → floor(5/0.45) = 11 shares (not dollar size)
        assert call_kwargs.kwargs.get("size") == 11.0
        assert success is True
        assert order_id == "order_gtc_123"

    def test_unfilled_maker_cancelled_after_timeout(self, executor):
        """GTC order not filled within timeout is cancelled (no taker fallback)."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_timeout_456"
        }
        executor.pm_trader.cancel_order = MagicMock(return_value=True)
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None):
            # _confirm_fill_pm returning None simulates fill timeout
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        # Cancel must have been called after timeout
        assert executor.pm_trader.cancel_order.called, (
            "Expected cancel_order to be called for unfilled GTC order"
        )

    def test_no_taker_fallback_after_maker_timeout(self, executor):
        """After GTC maker timeout and cancel, execution returns (False, ...) — no taker retry."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_timeout_789"
        }
        executor.pm_trader.cancel_order = MagicMock(return_value=True)
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None):
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        # Should fail (no taker fallback)
        assert success is False, (
            "Expected failure after GTC timeout — no taker fallback per D-05"
        )
        # place_order should only have been called once (maker attempt only)
        call_count = executor.pm_trader.place_order.call_count
        assert call_count == 1, (
            f"Expected exactly 1 order attempt (maker only), got {call_count}"
        )

    def test_unconfirmed_cancel_flags_leg_for_reconciliation(self, executor, caplog):
        """cancel_order returning False must flag the leg (_cancel_unconfirmed),
        log an ERROR, and preserve the order ID for recovery/reconciliation."""
        import logging as _logging
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1, "market": "Test Market?"}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_unconfirmed_1"
        }
        executor.pm_trader.cancel_order = MagicMock(return_value=False)
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None), \
             caplog.at_level(_logging.ERROR, logger="executor"):
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        assert success is False
        # Order ID preserved on both the return value and the leg
        assert order_id == "order_unconfirmed_1"
        assert leg["_order_id"] == "order_unconfirmed_1"
        # Leg carries the unconfirmed-cancel marker
        assert leg.get("_cancel_unconfirmed") is True
        # An ERROR mentioning the order ID was logged
        assert any(
            r.levelno == _logging.ERROR and "order_unconfirmed_1" in r.getMessage()
            for r in caplog.records
        )

    def test_confirmed_cancel_does_not_flag_leg(self, executor):
        """cancel_order returning True must NOT set _cancel_unconfirmed."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_confirmed_1"
        }
        executor.pm_trader.cancel_order = MagicMock(return_value=True)
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None):
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        assert success is False
        assert "_cancel_unconfirmed" not in leg

    def test_cancel_raising_flags_leg_and_preserves_order_id(self, executor):
        """cancel_order RAISING must still flag the leg and preserve the order ID."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1, "market": "Test Market?"}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_raise_1"
        }
        executor.pm_trader.cancel_order = MagicMock(side_effect=RuntimeError("venue down"))
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None):
            executor.dry_run = False
            success, order_id, fill_price = executor._execute_single_leg(leg, 5.0, opp)
        assert success is False
        assert order_id == "order_raise_1"
        assert leg["_order_id"] == "order_raise_1"
        assert leg.get("_cancel_unconfirmed") is True

    def test_unconfirmed_cancel_fires_alert_when_manager_present(self, executor):
        """An unconfirmed cancel must route through alerting when available."""
        from unittest.mock import patch as mpatch
        leg = {
            "platform": "polymarket",
            "side": "BUY",
            "token": "yes",
            "price": 0.45,
            "_token_id": "tok_yes",
        }
        opp = {"type": "Binary", "_layer": 1, "market": "Alert Market?"}
        executor.pm_trader.place_order.return_value = {
            "success": True, "orderID": "order_alert_1"
        }
        executor.pm_trader.cancel_order = MagicMock(return_value=False)
        mock_alert_manager = MagicMock()
        with mpatch("executor.ORDER_TIME_IN_FORCE", "gtc"), \
             mpatch("executor.GTC_ORDER_TIMEOUT", 0.01), \
             mpatch("executor.ENABLED_EXECUTION_PLATFORMS",
                    frozenset(["polymarket", "kalshi"])), \
             mpatch("executor._alert_manager", mock_alert_manager), \
             mpatch.object(executor, "_confirm_fill_pm", return_value=None):
            executor.dry_run = False
            executor._execute_single_leg(leg, 5.0, opp)
        assert mock_alert_manager.alert.called
        args = mock_alert_manager.alert.call_args
        assert args.args[0] == "cancel_unconfirmed"
        # Must be a supported AlertManager severity ("error" under-routes to
        # WARNING); an untracked-exposure event is high-severity.
        assert args.args[1] == "CRITICAL"
        assert args.kwargs["details"]["order_id"] == "order_alert_1"


class TestRecordFailedLeg:
    """Unconfirmed cancels must survive restart via a pending DB row + order_id."""

    def _make_trade(self, db):
        opp_id = db.log_opportunity(
            "Binary", "M?", "Y=0.4 N=0.5", 0.9, 0.1, 0.11, 100.0, "detect")
        return db.log_trade(opp_id, "polymarket", "BUY", 0.45, 5.0, "pending")

    def test_unconfirmed_cancel_row_is_pending_with_order_id(self, executor, db):
        trade_id = self._make_trade(db)
        leg = {
            "platform": "polymarket",
            "_trade_id": trade_id,
            "_order_id": "order_live_1",
            "_cancel_unconfirmed": True,
        }
        executor._record_failed_leg(trade_id, leg)
        row = [t for t in db.get_pending_trades() if t["id"] == trade_id]
        assert len(row) == 1, "unconfirmed-cancel trade must stay visible to recovery"
        assert row[0]["status"] == "pending"
        assert row[0]["order_id"] == "order_live_1"

    def test_confirmed_cancel_row_is_failed(self, executor, db):
        trade_id = self._make_trade(db)
        leg = {
            "platform": "polymarket",
            "_trade_id": trade_id,
            "_order_id": "order_dead_1",
        }
        executor._record_failed_leg(trade_id, leg)
        assert all(t["id"] != trade_id for t in db.get_pending_trades())
        rows = db.conn.execute(
            "SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchall()
        assert rows[0][0] == "failed"

    def test_exception_after_placement_row_is_pending_with_order_id(self, executor, db):
        """unknown_state=True + an assigned order_id: the venue-side state is
        unknown (order may be live) — must stay visible to recovery."""
        trade_id = self._make_trade(db)
        leg = {
            "platform": "polymarket",
            "_trade_id": trade_id,
            "_order_id": "order_maybe_live_1",
        }
        executor._record_failed_leg(trade_id, leg, unknown_state=True)
        row = [t for t in db.get_pending_trades() if t["id"] == trade_id]
        assert len(row) == 1, "exception-path trade with an order_id must stay pending"
        assert row[0]["status"] == "pending"
        assert row[0]["order_id"] == "order_maybe_live_1"

    def test_exception_before_placement_row_is_failed(self, executor, db):
        """unknown_state=True without an order_id: nothing was placed — failed."""
        trade_id = self._make_trade(db)
        leg = {"platform": "polymarket", "_trade_id": trade_id}
        executor._record_failed_leg(trade_id, leg, unknown_state=True)
        assert all(t["id"] != trade_id for t in db.get_pending_trades())
        rows = db.conn.execute(
            "SELECT status FROM trades WHERE id = ?", (trade_id,)).fetchall()
        assert rows[0][0] == "failed"


# ---------------------------------------------------------------------------
# _derive_position_platform: position.platform must reflect the legs' actual
# exchanges so check_settlements dispatches to the right API. Previously,
# anything that wasn't "Kalshi*" or "Cross*" was stamped "polymarket",
# silently misclassifying Betfair/Gemini/IBKR/Matchbook/Smarkets/SXBet
# positions and preventing them from ever settling.
# ---------------------------------------------------------------------------

class TestDerivePositionPlatform:
    def test_single_polymarket_leg(self):
        from executor import _derive_position_platform
        legs = [{"platform": "polymarket"}]
        assert _derive_position_platform(legs) == "polymarket"

    def test_multi_leg_same_platform_kalshi(self):
        from executor import _derive_position_platform
        legs = [{"platform": "kalshi"}, {"platform": "kalshi"}]
        assert _derive_position_platform(legs) == "kalshi"

    def test_multi_leg_same_platform_betfair_back_lay(self):
        """A BetfairBackLay opp with two Betfair legs must map to 'betfair',
        not 'polymarket' (regression for the Kalshi/Cross-only string match)."""
        from executor import _derive_position_platform
        legs = [{"platform": "betfair"}, {"platform": "betfair"}]
        assert _derive_position_platform(legs) == "betfair"

    def test_directional_gemini(self):
        from executor import _derive_position_platform
        assert _derive_position_platform([{"platform": "gemini"}]) == "gemini"

    def test_directional_ibkr(self):
        from executor import _derive_position_platform
        assert _derive_position_platform([{"platform": "ibkr"}]) == "ibkr"

    def test_directional_matchbook_smarkets_sxbet(self):
        from executor import _derive_position_platform
        for plat in ("matchbook", "smarkets", "sxbet"):
            assert _derive_position_platform([{"platform": plat}]) == plat

    def test_cross_platform_polymarket_kalshi(self):
        from executor import _derive_position_platform
        legs = [{"platform": "polymarket"}, {"platform": "kalshi"}]
        assert _derive_position_platform(legs) == "cross"

    def test_cross_platform_three_exchanges(self):
        from executor import _derive_position_platform
        legs = [{"platform": "polymarket"}, {"platform": "kalshi"}, {"platform": "betfair"}]
        assert _derive_position_platform(legs) == "cross"

    def test_empty_legs_returns_unknown(self):
        from executor import _derive_position_platform
        assert _derive_position_platform([]) == "unknown"


# ---------------------------------------------------------------------------
# Finding #1: the legacy KalshiRewards execution path (opp_type ==
# "KalshiRewards" -> _build_legs -> _execute_single_leg's kalshi arm,
# calling kalshi_client.place_order directly) must be disabled while the MM
# pilot owns Kalshi quoting (MM_KALSHI_PILOT_ENABLED) — two independent
# systems placing orders on the same venue at once defeats the pilot's
# single-choke-point safety design. When the flag is false, this path is
# unchanged from master.
# Fail-before: opp_type == "KalshiRewards" ran straight through execute()'s
# full pipeline into kalshi_client.place_order with no awareness of the
# pilot at all, regardless of MM_KALSHI_PILOT_ENABLED.
# ---------------------------------------------------------------------------

class TestMmPilotBlocksLegacyKalshiRewards:
    def _opportunity(self):
        return {
            "type": "KalshiRewards",
            "market": "KXTEST-26DEC31",
            "market_ticker": "KXTEST-26DEC31",
            "optimal_bid": 0.45,
            "optimal_ask": 0.55,
            "size": 5.0,
        }

    def test_blocked_when_pilot_enabled(self, executor, db, monkeypatch):
        import config
        monkeypatch.setattr(config, "MM_KALSHI_PILOT_ENABLED", True)
        result = executor.execute(self._opportunity())
        assert result is False
        executor.kalshi_client.place_order.assert_not_called()
        opps = db.get_recent_opportunities()
        assert opps[-1]["action"] == "skipped:mm_pilot_owns_kalshi"

    def test_unaffected_when_pilot_disabled(self, executor, db, monkeypatch):
        import config
        monkeypatch.setattr(config, "MM_KALSHI_PILOT_ENABLED", False)
        # Needed only because this test now runs deep enough into the
        # pipeline (risk_manager.check's Kalshi balance comparison) to
        # touch it — unrelated to the guard under test.
        executor.kalshi_client.get_balance.return_value = 100.0
        executor.execute(self._opportunity())
        opps = db.get_recent_opportunities()
        # Whatever happens to this opportunity downstream (revalidation,
        # risk gate, leg building) is out of scope here — the only thing
        # under test is that the NEW guard did not fire when disabled.
        assert opps[-1]["action"] != "skipped:mm_pilot_owns_kalshi"

    def test_other_opp_types_unaffected_by_the_flag(self, executor, db,
                                                     monkeypatch):
        """The guard is scoped to opp_type == "KalshiRewards" only — it must
        not touch any other strategy's execution path."""
        import config
        monkeypatch.setattr(config, "MM_KALSHI_PILOT_ENABLED", True)
        executor.pm_trader.get_balance.return_value = 100.0
        opp = {
            "type": "Binary",
            "market": "Test Market",
            "prices": "Y=0.400 N=0.450",
            "total_cost": "$0.8500",
            "net_profit": 0.138,
            "net_roi": "16.2%",
            "_clob_depth": 100.0,
            "_token_ids": ["tok_yes", "tok_no"],
        }
        result = executor.execute(opp)
        assert result is True
        opps = db.get_recent_opportunities()
        assert opps[-1]["action"] != "skipped:mm_pilot_owns_kalshi"


# ---------------------------------------------------------------------------
# TestExecutorFrechet
# ---------------------------------------------------------------------------

class TestExecutorFrechet:
    def test_build_legs_frechet_polymarket(self, executor):
        opp = {
            "type": "FrechetArb",
            "_platform": "polymarket",
            "_buy_yes_token": "tok_sup_yes",
            "_buy_no_token": "tok_sub_no",
            "_p_b": 0.45,
            "_p_a": 0.70,
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "polymarket"
        assert legs[0]["side"] == "BUY"
        assert legs[0]["token"] == "yes"
        assert legs[0]["price"] == pytest.approx(0.45)
        assert legs[0]["_token_id"] == "tok_sup_yes"
        assert legs[0]["_contracts"] == 6
        assert legs[0]["size"] == pytest.approx(2.70)

        assert legs[1]["platform"] == "polymarket"
        assert legs[1]["side"] == "BUY"
        assert legs[1]["token"] == "no"
        assert legs[1]["price"] == pytest.approx(0.30)
        assert legs[1]["_token_id"] == "tok_sub_no"
        assert legs[1]["_contracts"] == 6
        assert legs[1]["size"] == pytest.approx(1.80)

    def test_build_legs_frechet_kalshi(self, executor):
        opp = {
            "type": "FrechetArb",
            "_platform": "kalshi",
            "_buy_yes_ticker": "KXBTC-T90k",
            "_buy_no_ticker": "KXBTC-T100k",
            "_p_b": 0.45,
            "_p_a": 0.70,
        }
        legs = executor._build_legs(opp, 5.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "kalshi"
        assert legs[0]["side"] == "yes"
        assert legs[0]["action"] == "buy"
        assert legs[0]["price"] == pytest.approx(0.45)
        assert legs[0]["_ticker"] == "KXBTC-T90k"
        assert legs[0]["_contracts"] == 6
        assert legs[0]["size"] == pytest.approx(2.70)

        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "no"
        assert legs[1]["action"] == "buy"
        assert legs[1]["price"] == pytest.approx(0.30)
        assert legs[1]["_ticker"] == "KXBTC-T100k"
        assert legs[1]["_contracts"] == 6
        assert legs[1]["size"] == pytest.approx(1.80)

    def test_revalidate_frechet_kalshi_passed(self, executor):
        opp = {
            "type": "FrechetArb",
            "_platform": "kalshi",
            "_buy_yes_ticker": "KXBTC-T90k",
            "_buy_no_ticker": "KXBTC-T100k",
            "net_profit": 0.20,
        }
        mock_book_b = {"orderbook": {"yes": [[40, 100]], "no": [[55, 100]]}}
        mock_book_a = {"orderbook": {"yes": [[70, 100]], "no": [[25, 100]]}}

        def mock_fetch(ticker):
            return mock_book_b if ticker == "KXBTC-T90k" else mock_book_a

        executor.kalshi_client.fetch_order_book.side_effect = mock_fetch
        passed = executor._revalidate(opp)
        assert passed is True
        assert opp["_p_b"] == pytest.approx(0.45)
        assert opp["_p_a"] == pytest.approx(0.70)
        assert opp["net_profit"] > 0

    def test_revalidate_frechet_kalshi_degraded(self, executor):
        opp = {
            "type": "FrechetArb",
            "_platform": "kalshi",
            "_buy_yes_ticker": "KXBTC-T90k",
            "_buy_no_ticker": "KXBTC-T100k",
            "net_profit": 0.20,
        }
        # Books moved so total cost >= 1.0 (no edge)
        mock_book_b = {"orderbook": {"yes": [[40, 100]], "no": [[45, 100]]}}  # yes_ask = 0.55
        mock_book_a = {"orderbook": {"yes": [[45, 100]], "no": [[50, 100]]}}  # no_ask = 0.55

        def mock_fetch(ticker):
            return mock_book_b if ticker == "KXBTC-T90k" else mock_book_a

        executor.kalshi_client.fetch_order_book.side_effect = mock_fetch
        passed = executor._revalidate(opp)
        assert passed is False

    def test_revalidate_frechet_kalshi_missing_book(self, executor):
        opp = {
            "type": "FrechetArb",
            "_platform": "kalshi",
            "_buy_yes_ticker": "KXBTC-T90k",
            "_buy_no_ticker": "KXBTC-T100k",
            "net_profit": 0.20,
        }
        executor.kalshi_client.fetch_order_book.return_value = None
        passed = executor._revalidate(opp)
        assert passed is False

    def test_revalidate_frechet_polymarket_passed(self, executor):
        from unittest.mock import patch
        opp = {
            "type": "FrechetArb",
            "_platform": "polymarket",
            "_buy_yes_token": "tok_b",
            "_buy_no_token": "tok_a",
            "net_profit": 0.20,
        }
        # Mock order books:
        # tok_b (YES on B): ask 0.45
        # tok_a (NO on A): ask 0.30 (implied P(A) = 0.70)
        def mock_fetch(tok):
            return {"book": tok}

        def mock_bba(book):
            if book["book"] == "tok_b":
                return {"ask": 0.45, "bid": 0.43}
            else:
                return {"ask": 0.30, "bid": 0.28}

        with patch("executor.fetch_order_book", side_effect=mock_fetch), \
             patch("executor.get_best_bid_ask", side_effect=mock_bba):
            passed = executor._revalidate(opp)

        assert passed is True
        assert opp["_p_b"] == 0.45
        assert opp["_p_a"] == pytest.approx(0.70)

    def test_revalidate_frechet_polymarket_degraded(self, executor):
        from unittest.mock import patch
        opp = {
            "type": "FrechetArb",
            "_platform": "polymarket",
            "_buy_yes_token": "tok_b",
            "_buy_no_token": "tok_a",
            "net_profit": 0.20,
        }
        # Prices moved so cost is 0.50 + 0.50 = 1.00 -> profit is negative
        def mock_fetch(tok):
            return {"book": tok}

        def mock_bba(book):
            return {"ask": 0.50, "bid": 0.48}

        with patch("executor.fetch_order_book", side_effect=mock_fetch), \
             patch("executor.get_best_bid_ask", side_effect=mock_bba):
            passed = executor._revalidate(opp)

        assert passed is False

    def test_execute_frechet_max_trade_size_clamped(self, executor):
        from unittest.mock import patch
        opp = {
            "type": "FrechetArb",
            "market": "BTC 100k subset of 90k",
            "_platform": "polymarket",
            "_buy_yes_token": "tok_b",
            "_buy_no_token": "tok_a",
            "_p_b": 0.45,
            "_p_a": 0.70,
            "total_cost": "$0.75",
            "net_profit": 0.20,
            "net_roi": "26.7%",
            "_clob_depth": 500.0,
        }
        executor.max_trade_size = 100.0
        # FRECHET_ARB_MAX_TRADE_SIZE is 20.0
        with patch("config.FRECHET_ARB_MAX_TRADE_SIZE", 20.0), \
             patch.object(executor, "_revalidate", return_value=True):
            legs = executor._build_legs(opp, 20.0)
            assert len(legs) == 2
            # 20.0 / 0.75 = 26 contracts
            assert legs[0]["_contracts"] == 26
            assert legs[1]["_contracts"] == 26


class TestExecutorTemporal:
    """Test executor leg building, contract quantity matching, and revalidation for TemporalArb."""

    def test_build_legs_temporal_equal_contract_quantities(self, executor) -> None:
        opp = {
            "type": "TemporalArb",
            "_platform": "kalshi",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "_p_late": 0.45,
            "_p_early": 0.70,
            "_kalshi_late_yes": 0.45,
            "_kalshi_early_no": 0.30,
        }
        # unit cost = 0.45 + 0.30 = 0.75
        # size = 15.0 -> contracts = 20
        legs = executor._build_legs(opp, 15.0)
        assert len(legs) == 2
        assert legs[0]["platform"] == "kalshi"
        assert legs[0]["side"] == "yes"
        assert legs[0]["action"] == "buy"
        assert legs[0]["price"] == 0.45
        assert legs[0]["_ticker"] == "KXBTC-26JUN30-T100000"
        assert legs[0]["_contracts"] == 20
        assert legs[0]["size"] == 9.0  # 20 * 0.45

        assert legs[1]["platform"] == "kalshi"
        assert legs[1]["side"] == "no"
        assert legs[1]["action"] == "buy"
        assert legs[1]["price"] == 0.30
        assert legs[1]["_ticker"] == "KXBTC-26MAR31-T100000"
        assert legs[1]["_contracts"] == 20
        assert legs[1]["size"] == 6.0  # 20 * 0.30

    def test_build_legs_temporal_contracts_zero(self, executor) -> None:
        opp = {
            "type": "TemporalArb",
            "_platform": "kalshi",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "_p_late": 0.45,
            "_p_early": 0.70,
            "_kalshi_late_yes": 0.50,
            "_kalshi_early_no": 0.50,
        }
        # unit cost = 1.00, size = 0.50 -> contracts = 0
        legs = executor._build_legs(opp, 0.50)
        assert legs == []

    def test_revalidate_temporal_success(self, executor) -> None:
        from unittest.mock import MagicMock
        opp = {
            "type": "TemporalArb",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "net_profit": 0.15,
        }
        mock_kalshi = MagicMock()
        mock_kalshi.fetch_order_book.side_effect = [
            # late ticker book: NO bid 0.52 -> YES ask = 0.48
            {"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.52", "100"]]}},
            # early ticker book: YES bid 0.68 -> NO ask = 0.32
            {"orderbook_fp": {"yes_dollars": [["0.68", "100"]], "no_dollars": [["0.28", "100"]]}},
        ]
        executor.kalshi_client = mock_kalshi

        passed = executor._revalidate(opp)
        assert passed is True
        assert opp["_kalshi_late_yes"] == 0.48
        assert opp["_kalshi_early_no"] == 0.32
        assert opp["_p_late"] == 0.48
        assert opp["_p_early"] == 0.68

    def test_revalidate_temporal_spread_degradation_fails(self, executor) -> None:
        from unittest.mock import MagicMock
        opp = {
            "type": "TemporalArb",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "net_profit": 0.20,
        }
        mock_kalshi = MagicMock()
        # Spread moved so cost = 0.50 + 0.50 = 1.00 -> profit is negative
        mock_kalshi.fetch_order_book.side_effect = [
            {"orderbook_fp": {"yes_dollars": [["0.45", "100"]], "no_dollars": [["0.50", "100"]]}},
            {"orderbook_fp": {"yes_dollars": [["0.50", "100"]], "no_dollars": [["0.45", "100"]]}},
        ]
        executor.kalshi_client = mock_kalshi

        passed = executor._revalidate(opp)
        assert passed is False

    def test_revalidate_temporal_missing_client_raises(self, executor) -> None:
        from executor import _RevalidationAPIError
        import pytest
        opp = {
            "type": "TemporalArb",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "net_profit": 0.10,
        }
        executor.kalshi_client = None
        with pytest.raises(_RevalidationAPIError):
            executor._revalidate_temporal(opp, 0.10)

    def test_execute_temporal_max_trade_size_clamped(self, executor) -> None:
        from unittest.mock import patch
        opp = {
            "type": "TemporalArb",
            "market": "KXBTC-26MAR31 subset of KXBTC-26JUN30",
            "_platform": "kalshi",
            "_late_ticker": "KXBTC-26JUN30-T100000",
            "_early_ticker": "KXBTC-26MAR31-T100000",
            "_p_late": 0.45,
            "_p_early": 0.70,
            "_kalshi_late_yes": 0.45,
            "_kalshi_early_no": 0.30,
            "total_cost": "$0.75",
            "net_profit": 0.20,
            "net_roi": "26.7%",
            "_clob_depth": 500.0,
        }
        executor.max_trade_size = 100.0
        with patch("config.TEMPORAL_ARB_MAX_TRADE_SIZE", 15.0), \
             patch.object(executor, "_revalidate", return_value=(True, 0.20, "passed")):
            legs = executor._build_legs(opp, 15.0)
            assert len(legs) == 2
            # 15.0 / 0.75 = 20 contracts
            assert legs[0]["_contracts"] == 20
            assert legs[1]["_contracts"] == 20


# ---------------------------------------------------------------------------
# TestExecutorWebSocketOrderbookStreaming
# ---------------------------------------------------------------------------

class TestExecutorWebSocketOrderbookStreaming:
    """Tests for Phase 4 WebSocket orderbook streaming in ArbitrageExecutor."""

    def test_polymarket_orderbook_ws_hit(self, executor) -> None:
        """Fresh Polymarket book from feed_manager is returned without REST call."""
        mock_fm = MagicMock()
        book = {"bids": [{"price": "0.45", "size": "100"}], "asks": [{"price": "0.55", "size": "100"}]}
        mock_fm.get_orderbook.return_value = (book, 1.2)
        executor.feed_manager = mock_fm

        with patch("executor.fetch_order_book") as mock_rest:
            res_book, source = executor._get_orderbook("polymarket", "token_123")
            assert res_book == book
            assert source == "ws"
            mock_rest.assert_not_called()
            mock_fm.get_orderbook.assert_called_once_with("polymarket", "token_123")
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 1
            assert telemetry["rest_fetches"] == 0

    def test_polymarket_orderbook_stale_fallback_rest(self, executor) -> None:
        """Stale Polymarket book from feed_manager triggers REST fallback."""
        mock_fm = MagicMock()
        stale_book = {"bids": [], "asks": []}
        mock_fm.get_orderbook.return_value = (stale_book, 25.0)
        executor.feed_manager = mock_fm

        rest_book = {"bids": [{"price": "0.48", "size": "50"}], "asks": [{"price": "0.52", "size": "50"}]}
        with patch("executor.fetch_order_book", return_value=rest_book) as mock_rest:
            res_book, source = executor._get_orderbook("polymarket", "token_123")
            assert res_book == rest_book
            assert source == "rest"
            mock_rest.assert_called_once_with("token_123")
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 0
            assert telemetry["rest_fetches"] == 1

    def test_polymarket_orderbook_missing_fallback_rest(self, executor) -> None:
        """Missing Polymarket book from feed_manager triggers REST fallback."""
        mock_fm = MagicMock()
        mock_fm.get_orderbook.return_value = (None, None)
        executor.feed_manager = mock_fm

        rest_book = {"bids": [], "asks": [{"price": "0.50", "size": "10"}]}
        with patch("executor.fetch_order_book", return_value=rest_book) as mock_rest:
            res_book, source = executor._get_orderbook("polymarket", "token_123")
            assert res_book == rest_book
            assert source == "rest"
            mock_rest.assert_called_once_with("token_123")
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 0
            assert telemetry["rest_fetches"] == 1

    def test_polymarket_orderbook_disabled_streaming(self, executor) -> None:
        """When streaming disabled, bypasses feed_manager completely."""
        mock_fm = MagicMock()
        executor.feed_manager = mock_fm
        rest_book = {"bids": [], "asks": []}

        with patch("config.WS_ORDERBOOK_STREAMING_ENABLED", False), \
             patch("executor.fetch_order_book", return_value=rest_book) as mock_rest:
            res_book, source = executor._get_orderbook("polymarket", "token_123")
            assert res_book == rest_book
            assert source == "rest"
            mock_fm.get_orderbook.assert_not_called()
            mock_rest.assert_called_once_with("token_123")
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 0
            assert telemetry["rest_fetches"] == 1

    def test_kalshi_orderbook_ws_hit(self, executor) -> None:
        """Fresh Kalshi book from feed_manager is returned without REST call."""
        mock_fm = MagicMock()
        kalshi_book = {"orderbook": {"yes": [[45, 100]], "no": [[55, 100]]}}
        mock_fm.get_orderbook.return_value = (kalshi_book, 0.5)
        executor.feed_manager = mock_fm
        executor.kalshi_client = MagicMock()

        res_book, source = executor._get_orderbook("kalshi", "KXTEST")
        assert res_book == kalshi_book
        assert source == "ws"
        executor.kalshi_client.fetch_order_book.assert_not_called()
        telemetry = executor.get_orderbook_telemetry()
        assert telemetry["ws_hits"] == 1
        assert telemetry["rest_fetches"] == 0

    def test_kalshi_orderbook_stale_fallback_rest(self, executor) -> None:
        """Stale Kalshi book triggers REST fallback to kalshi_client.fetch_order_book."""
        mock_fm = MagicMock()
        stale_book = {"orderbook": {"yes": [[40, 10]], "no": [[60, 10]]}}
        mock_fm.get_orderbook.return_value = (stale_book, 30.0)
        executor.feed_manager = mock_fm
        executor.kalshi_client = MagicMock()
        rest_book = {"orderbook": {"yes": [[45, 50]], "no": [[55, 50]]}}
        executor.kalshi_client.fetch_order_book.return_value = rest_book

        res_book, source = executor._get_orderbook("kalshi", "KXTEST")
        assert res_book == rest_book
        assert source == "rest"
        executor.kalshi_client.fetch_order_book.assert_called_once_with("KXTEST")
        telemetry = executor.get_orderbook_telemetry()
        assert telemetry["ws_hits"] == 0
        assert telemetry["rest_fetches"] == 1

    def test_kalshi_orderbook_no_client_returns_none(self, executor) -> None:
        """When Kalshi orderbook is missing from WS and client is None, returns (None, 'none')."""
        executor.feed_manager = None
        executor.kalshi_client = None

        res_book, source = executor._get_orderbook("kalshi", "KXTEST")
        assert res_book is None
        assert source == "none"

    def test_check_exit_liquidity_uses_ws_orderbooks(self, executor) -> None:
        """_check_exit_liquidity uses fresh WS orderbooks for both Polymarket and Kalshi."""
        mock_fm = MagicMock()
        poly_book = {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.55", "size": "100"}],
        }
        kalshi_book = {
            "orderbook": {"yes": [[48, 100]], "no": [[52, 100]]},
        }

        def fake_get_orderbook(platform, key):
            if platform == "polymarket":
                return (poly_book, 1.0)
            elif platform == "kalshi":
                return (kalshi_book, 1.0)
            return (None, None)

        mock_fm.get_orderbook.side_effect = fake_get_orderbook
        executor.feed_manager = mock_fm
        executor.kalshi_client = MagicMock()
        executor.dry_run = False

        opp = {"type": "CrossPlatformArb"}
        legs = [
            {"platform": "kalshi", "action": "buy", "side": "yes", "_ticker": "KXTEST", "price": 0.48, "size": 10},
            {"platform": "polymarket", "action": "buy", "side": "BUY", "_token_id": "tok_1", "price": 0.50, "size": 10},
        ]

        with patch("config.EXIT_LIQUIDITY_GATE_ENABLED", True), \
             patch("polymarket_api.fetch_order_book") as mock_poly_rest, \
             patch("kalshi_api.parse_orderbook", return_value={"yes_bids": [(0.48, 100.0)], "no_bids": [(0.52, 100.0)]}), \
             patch("kalshi_api.best_yes_bid", return_value=(0.48, 100.0)), \
             patch("polymarket_api.get_best_bid_ask", return_value={"bid": 0.45, "ask": 0.55, "bid_size": 100, "ask_size": 100}):
            passed, reason = executor._check_exit_liquidity(opp, legs)
            assert passed is True
            mock_poly_rest.assert_not_called()
            executor.kalshi_client.fetch_order_book.assert_not_called()
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 2
            assert telemetry["rest_fetches"] == 0

    def test_revalidate_kalshi_binary_uses_ws_orderbook(self, executor) -> None:
        """_revalidate_kalshi_binary revalidates with fresh WS book without calling client."""
        mock_fm = MagicMock()
        kalshi_book = {"orderbook": {"yes": [[45, 100]], "no": [[50, 100]]}}
        mock_fm.get_orderbook.return_value = (kalshi_book, 0.5)
        executor.feed_manager = mock_fm
        executor.kalshi_client = MagicMock()

        opp = {
            "type": "KalshiBinaryArb",
            "_kalshi_ticker": "KXTEST",
            "net_profit": 0.05,
            "net_roi": "5.0%",
            "total_cost": "$0.95",
        }

        with patch("kalshi_api.parse_orderbook", return_value={"yes_bids": [(0.45, 100)], "no_bids": [(0.50, 100)]}), \
             patch("kalshi_api.best_yes_ask", return_value=(0.50, 100.0)), \
             patch("kalshi_api.best_no_ask", return_value=(0.45, 100.0)), \
             patch("executor.net_profit_kalshi_binary", return_value={"net_profit": 0.05, "net_roi": 0.05}):
            passed, net_profit, reason = executor._revalidate_kalshi_binary(opp, 0.05)
            assert passed is True
            assert net_profit == 0.05
            executor.kalshi_client.fetch_order_book.assert_not_called()
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 1
            assert telemetry["rest_fetches"] == 0

    def test_revalidate_cross_uses_ws_orderbooks(self, executor) -> None:
        """_revalidate_cross revalidates with fresh WS books on both legs."""
        mock_fm = MagicMock()
        poly_book = {
            "bids": [{"price": "0.45", "size": "100"}],
            "asks": [{"price": "0.48", "size": "100"}],
        }
        kalshi_book = {
            "orderbook": {"yes": [[48, 100]], "no": [[50, 100]]},
        }

        def fake_get_orderbook(platform, key):
            if platform == "polymarket":
                return (poly_book, 0.8)
            elif platform == "kalshi":
                return (kalshi_book, 0.8)
            return (None, None)

        mock_fm.get_orderbook.side_effect = fake_get_orderbook
        executor.feed_manager = mock_fm
        executor.kalshi_client = MagicMock()

        opp = {
            "type": "CrossPlatformArb",
            "_token_ids": ["tok_yes", "tok_no"],
            "_kalshi_ticker": "KXTEST",
            "net_profit": 0.04,
            "net_roi": "4.0%",
            "total_cost": "$0.96",
        }

        with patch("polymarket_api.fetch_order_book") as mock_poly_rest, \
             patch("kalshi_api.parse_orderbook", return_value={"yes_bids": [(0.48, 100)], "no_bids": [(0.50, 100)]}), \
             patch("kalshi_api.best_yes_ask", return_value=(0.50, 100.0)), \
             patch("kalshi_api.best_no_ask", return_value=(0.48, 100.0)), \
             patch("executor.get_best_bid_ask", return_value={"bid": 0.45, "ask": 0.48, "bid_size": 100, "ask_size": 100}), \
             patch("executor.net_profit_cross_platform", return_value={"net_profit": 0.04, "net_roi": 0.04}), \
             patch("config.JEV_CROSS_EQUIVALENCE_ENABLED", False):
            passed, net_profit, reason = executor._revalidate_cross(opp, 0.04, price_cache={})
            assert passed is True
            assert net_profit == 0.04
            mock_poly_rest.assert_not_called()
            executor.kalshi_client.fetch_order_book.assert_not_called()
            telemetry = executor.get_orderbook_telemetry()
            assert telemetry["ws_hits"] == 3  # 2 poly tokens + 1 kalshi ticker
            assert telemetry["rest_fetches"] == 0
