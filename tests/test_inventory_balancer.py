"""Tests for the Cross-Venue Delta-Neutral Inventory Balancer."""

import sys
import os
from unittest.mock import MagicMock, patch
import pytest

# Add project root to sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config as cfg
from inventory_balancer import InventoryBalancer
from risk_manager import RiskManager


class TestInventoryBalancerConfig:
    """Test configuration validation for inventory balancer."""

    def test_default_config_values(self):
        """Default balancer parameters match documented standards."""
        balancer = InventoryBalancer()
        assert balancer.enabled is True
        assert balancer.max_delta_contracts == 50.0
        assert balancer.max_imbalance_ratio == 0.5
        assert balancer.max_rebalance_cost == 25.0

    def test_validate_config_rejects_non_positive_max_delta(self):
        """validate_config rejects INVENTORY_MAX_DELTA_CONTRACTS <= 0."""
        with patch.object(cfg, "INVENTORY_MAX_DELTA_CONTRACTS", 0.0):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_MAX_DELTA_CONTRACTS=0.0 must be > 0"):
                cfg.validate_config()

        with patch.object(cfg, "INVENTORY_MAX_DELTA_CONTRACTS", -10.0):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_MAX_DELTA_CONTRACTS=-10.0 must be > 0"):
                cfg.validate_config()

    def test_validate_config_rejects_invalid_imbalance_ratio(self):
        """validate_config rejects INVENTORY_MAX_IMBALANCE_RATIO <= 0 or > 1."""
        with patch.object(cfg, "INVENTORY_MAX_IMBALANCE_RATIO", 0.0):
            with pytest.raises(cfg.ConfigError, match="must be in \\(0, 1\\]"):
                cfg.validate_config()

        with patch.object(cfg, "INVENTORY_MAX_IMBALANCE_RATIO", 1.5):
            with pytest.raises(cfg.ConfigError, match="must be in \\(0, 1\\]"):
                cfg.validate_config()

        with patch.object(cfg, "INVENTORY_MAX_IMBALANCE_RATIO", float("nan")):
            with pytest.raises(cfg.ConfigError, match="must be in \\(0, 1\\]"):
                cfg.validate_config()

    def test_validate_config_rejects_non_positive_max_cost(self):
        """validate_config rejects INVENTORY_REBALANCE_MAX_COST <= 0."""
        with patch.object(cfg, "INVENTORY_REBALANCE_MAX_COST", 0.0):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_REBALANCE_MAX_COST=0.0 must be > 0"):
                cfg.validate_config()

    def test_validate_config_rejects_negative_cooldown(self):
        """validate_config rejects INVENTORY_REBALANCE_COOLDOWN_SEC < 0."""
        with patch.object(cfg, "INVENTORY_REBALANCE_COOLDOWN_SEC", -5.0):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_REBALANCE_COOLDOWN_SEC=-5.0 must be >= 0"):
                cfg.validate_config()

    def test_validate_config_rejects_invalid_min_imbalance_ratio(self):
        """validate_config rejects INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO <= 0 or > 1."""
        with patch.object(cfg, "INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO", 0.0):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO=0.0 must be in \\(0, 1\\]"):
                cfg.validate_config()

        with patch.object(cfg, "INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO", 1.2):
            with pytest.raises(cfg.ConfigError, match="INVENTORY_REBALANCE_MIN_IMBALANCE_RATIO=1.2 must be in \\(0, 1\\]"):
                cfg.validate_config()



class TestInventoryBalancerDeltaTracking:
    """Test delta calculation and multi-platform inventory aggregation."""

    def test_update_position_buy_and_sell(self):
        """update_position correctly updates YES and NO holdings."""
        balancer = InventoryBalancer()
        balancer.update_position("m1", "polymarket", "yes", "buy", 100.0)
        balancer.update_position("m1", "polymarket", "no", "buy", 40.0)
        balancer.update_position("m1", "kalshi", "no", "buy", 60.0)

        deltas = balancer.compute_inventory_deltas()
        assert "m1" in deltas
        m1 = deltas["m1"]
        # Total YES = 100, Total NO = 40 + 60 = 100 -> Delta = 0 (perfect neutrality)
        assert m1["qty_yes"] == 100.0
        assert m1["qty_no"] == 100.0
        assert m1["total_qty"] == 200.0
        assert m1["delta_net"] == 0.0
        assert m1["imbalance_ratio"] == 0.0
        assert m1["is_imbalanced"] is False

        # Now sell 50 YES on Polymarket
        balancer.update_position("m1", "polymarket", "yes", "sell", 50.0)
        deltas = balancer.compute_inventory_deltas()
        m1 = deltas["m1"]
        # Total YES = 50, Total NO = 100 -> Delta = -50
        assert m1["qty_yes"] == 50.0
        assert m1["qty_no"] == 100.0
        assert m1["delta_net"] == -50.0
        assert m1["imbalance_ratio"] == 50.0 / 150.0

    def test_compute_inventory_deltas_from_explicit_list(self):
        """compute_inventory_deltas accepts a list of raw positions."""
        balancer = InventoryBalancer(max_delta_contracts=30.0, max_imbalance_ratio=0.4)
        raw_positions = [
            {"market_key": "btc_100k", "platform": "polymarket", "outcome": "yes", "size": 80.0},
            {"market_key": "btc_100k", "platform": "kalshi", "outcome": "no", "size": 20.0},
        ]
        deltas = balancer.compute_inventory_deltas(raw_positions)
        btc = deltas["btc_100k"]
        assert btc["qty_yes"] == 80.0
        assert btc["qty_no"] == 20.0
        assert btc["delta_net"] == 60.0
        assert btc["total_qty"] == 100.0
        assert btc["imbalance_ratio"] == 0.6
        assert btc["is_imbalanced"] is True

    def test_sync_from_db_parses_open_positions_and_trades(self):
        """sync_from_db reconstructs inventory positions from TradeDB."""
        balancer = InventoryBalancer()
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [
            {"opportunity_id": 101, "market_identifier": "fed_cut", "platform": "cross"},
        ]
        mock_db.get_trades_for_opportunity.return_value = [
            {"platform": "polymarket", "side": "buy", "outcome": "yes", "size": 75.0, "status": "filled"},
            {"platform": "kalshi", "side": "buy", "outcome": "no", "size": 75.0, "status": "filled"},
            {"platform": "kalshi", "side": "buy", "outcome": "no", "size": 25.0, "status": "pending"},  # ignored
        ]

        synced = balancer.sync_from_db(mock_db)
        assert synced == 1

        deltas = balancer.compute_inventory_deltas()
        fed = deltas["fed_cut"]
        assert fed["qty_yes"] == 75.0
        assert fed["qty_no"] == 75.0
        assert fed["delta_net"] == 0.0


class TestInventoryBalancerImbalanceDetection:
    """Test threshold filtering for imbalanced markets."""

    def test_get_imbalances_filters_by_contract_and_ratio_thresholds(self):
        """get_imbalances returns only markets meeting both contract and ratio thresholds."""
        balancer = InventoryBalancer(max_delta_contracts=50.0, max_imbalance_ratio=0.5)
        # Market 1: Delta = +60, Total = 100, Ratio = 0.6 -> Imbalanced (>=50 and >=0.5)
        balancer.update_position("m1", "polymarket", "yes", "buy", 80.0)
        balancer.update_position("m1", "polymarket", "no", "buy", 20.0)

        # Market 2: Delta = +40, Total = 50, Ratio = 0.8 -> NOT imbalanced (Delta 40 < 50)
        balancer.update_position("m2", "kalshi", "yes", "buy", 45.0)
        balancer.update_position("m2", "kalshi", "no", "buy", 5.0)

        # Market 3: Delta = +60, Total = 200, Ratio = 0.3 -> NOT imbalanced (Ratio 0.3 < 0.5)
        balancer.update_position("m3", "polymarket", "yes", "buy", 130.0)
        balancer.update_position("m3", "kalshi", "no", "buy", 70.0)

        imbalances = balancer.get_imbalances()
        assert len(imbalances) == 1
        assert imbalances[0]["market_key"] == "m1"
        assert imbalances[0]["delta_net"] == 60.0


class TestInventoryBalancerRebalancingProposals:
    """Test generation and pricing of rebalancing proposals."""

    def test_proposes_buying_deficient_no_when_delta_positive(self):
        """When net delta is positive (long YES heavy), proposal buys deficient NO."""
        balancer = InventoryBalancer(max_delta_contracts=40.0, max_rebalance_cost=25.0)
        # Delta = +50 (YES=60, NO=10)
        balancer.update_position("elec", "polymarket", "yes", "buy", 60.0)
        balancer.update_position("elec", "kalshi", "no", "buy", 10.0)

        mock_feed = MagicMock()
        # Mock quotes: Kalshi has NO ask @ 0.45 (depth 100), Polymarket has NO ask @ 0.50 (depth 50)
        # Kalshi is cheaper, so balancer should propose buying NO on Kalshi
        mock_feed.get_orderbook.side_effect = lambda plat, mkey: {
            ("kalshi", "elec"): ({"orderbook": {"yes": [[55, 100]], "no": [[45, 100]]}}, 1.0),
            ("polymarket", "elec"): ({"asks": [{"price": 0.50, "size": 50}], "bids": [{"price": 0.48, "size": 50}]}, 1.0),
        }.get((plat, mkey), (None, None))

        proposals = balancer.generate_rebalancing_proposals(feed_manager=mock_feed)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["market_key"] == "elec"
        assert p["action"] == "rebalance_buy"
        assert p["target_venue"] == "kalshi"
        assert p["outcome"] == "no"
        assert p["side"] == "buy"
        assert p["price"] == 0.45
        # Capped by max_rebalance_cost: $25 / 0.45 = 55.55 -> capped by needed contracts (50)
        assert p["size"] == 50.0
        assert p["projected_delta"] == 0.0

    def test_proposes_buying_deficient_yes_when_delta_negative(self):
        """When net delta is negative (long NO heavy), proposal buys deficient YES."""
        balancer = InventoryBalancer(max_delta_contracts=30.0, max_rebalance_cost=20.0)
        # Delta = -60 (YES=10, NO=70)
        balancer.update_position("gdp", "polymarket", "no", "buy", 70.0)
        balancer.update_position("gdp", "kalshi", "yes", "buy", 10.0)

        mock_feed = MagicMock()
        # Polymarket has YES ask @ 0.40 (size 100), Kalshi has YES ask @ 0.45 (size 100)
        # Polymarket is cheaper
        mock_feed.get_orderbook.side_effect = lambda plat, mkey: {
            ("polymarket", "gdp"): ({"asks": [{"price": 0.40, "size": 100}], "bids": [{"price": 0.38, "size": 50}]}, 1.0),
            ("kalshi", "gdp"): ({"orderbook": {"yes": [[45, 100]], "no": [[55, 100]]}}, 1.0),
        }.get((plat, mkey), (None, None))

        proposals = balancer.generate_rebalancing_proposals(feed_manager=mock_feed)
        assert len(proposals) == 1
        p = proposals[0]
        assert p["market_key"] == "gdp"
        assert p["target_venue"] == "polymarket"
        assert p["outcome"] == "yes"
        assert p["price"] == 0.40
        # Cost limit: $20 / 0.40 = 50 contracts (needed 60)
        assert p["size"] == 50.0
        assert p["projected_delta"] == -10.0  # -60 + 50 = -10


class TestInventoryBalancerTradeSkewGating:
    """Test check_trade_skew gating functionality."""

    def test_allows_trade_on_balanced_market(self):
        """When market has no significant delta skew, any trade is permitted."""
        balancer = InventoryBalancer(max_delta_contracts=50.0)
        balancer.update_position("m1", "polymarket", "yes", "buy", 20.0)

        allowed, reason = balancer.check_trade_skew("m1", "yes", "buy", 10.0)
        assert allowed is True
        assert "OK" in reason

    def test_rejects_trade_that_worsens_severe_skew(self):
        """Rejects a trade that increases |Delta| when market is already severely skewed."""
        balancer = InventoryBalancer(max_delta_contracts=50.0)
        # Market has +70 delta (already >= 50)
        balancer.update_position("m1", "polymarket", "yes", "buy", 70.0)

        # Propose buying 20 more YES -> projected delta = +90
        allowed, reason = balancer.check_trade_skew("m1", "yes", "buy", 20.0)
        assert allowed is False
        assert "Trade rejected by InventoryBalancer" in reason
        assert "worsen delta skew" in reason

    def test_allows_trade_that_reduces_severe_skew(self):
        """Allows a trade that reduces |Delta| on a severely skewed market."""
        balancer = InventoryBalancer(max_delta_contracts=50.0)
        # Market has +70 delta
        balancer.update_position("m1", "polymarket", "yes", "buy", 70.0)

        # Propose buying 20 NO -> projected delta = +50 (reduces skew)
        allowed, reason = balancer.check_trade_skew("m1", "no", "buy", 20.0)
        assert allowed is True
        assert "reduces or maintains" in reason

        # Propose selling 30 YES -> projected delta = +40 (reduces skew)
        allowed2, reason2 = balancer.check_trade_skew("m1", "yes", "sell", 30.0)
        assert allowed2 is True
        assert "reduces or maintains" in reason2

    def test_bypasses_gate_when_disabled(self):
        """When enabled=False, all trades pass through unconditionally."""
        balancer = InventoryBalancer(max_delta_contracts=50.0, enabled=False)
        balancer.update_position("m1", "polymarket", "yes", "buy", 100.0)

        allowed, reason = balancer.check_trade_skew("m1", "yes", "buy", 50.0)
        assert allowed is True
        assert "disabled" in reason


class TestRiskManagerInventorySkewIntegration:
    """Test RiskManager integration with InventoryBalancer."""

    def test_risk_manager_blocks_trade_that_worsens_inventory_skew(self):
        """RiskManager.check() fails closed when trade exacerbates inventory skew."""
        balancer = InventoryBalancer(max_delta_contracts=50.0)
        # Market has -60 delta (long NO heavy)
        balancer.update_position("market_abc", "polymarket", "no", "buy", 60.0)

        risk_config = {
            "base_trade_size": 10.0,
            "max_trade_size": 25.0,
            "daily_loss_limit": 50.0,
            "max_open_positions": 10,
        }
        rm = RiskManager(risk_config, inventory_balancer=balancer)

        mock_db = MagicMock()
        mock_db.get_daily_pnl.return_value = 0.0
        mock_db.get_open_positions_count.return_value = 1
        mock_db.is_market_active.return_value = False

        # Directional opportunity to buy more NO on market_abc
        opp_worsening = {
            "type": "WhaleCopy",
            "_market_key": "market_abc",
            "market": "Will ABC occur?",
            "_outcome": "no",
            "_side": "buy",
            "_size": 15.0,
            "total_cost": "$0.50",
            "net_profit": 0.10,
        }

        allowed, reason = rm.check(opp_worsening, mock_db)
        assert allowed is False
        assert "worsen delta skew" in reason

        # Directional opportunity to buy YES on market_abc (reduces skew)
        opp_healing = {
            "type": "WhaleCopy",
            "_market_key": "market_abc",
            "market": "Will ABC occur?",
            "_outcome": "yes",
            "_side": "buy",
            "_size": 15.0,
            "total_cost": "$0.50",
            "net_profit": 0.10,
        }

        allowed, reason = rm.check(opp_healing, mock_db)
        assert allowed is True
        assert reason == "OK"


class TestInventoryBalancerSkewMetricsAndSingleton:
    """Test get_delta, get_market_delta, get_skew_metrics, and singleton helper."""

    def test_get_delta_and_market_delta(self):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=50.0, max_imbalance_ratio=0.5)

        # Untracked market
        assert balancer.get_delta("untracked") == 0.0
        assert balancer.get_market_delta("untracked") is None

        # Add positions: Polymarket YES: 60, Kalshi NO: 10 -> net delta = 50
        balancer.update_position("mkt_1", "polymarket", "yes", "buy", 60.0)
        balancer.update_position("mkt_1", "kalshi", "no", "buy", 10.0)

        assert balancer.get_delta("mkt_1") == 50.0
        info = balancer.get_market_delta("mkt_1")
        assert info is not None
        assert info["market_key"] == "mkt_1"
        assert info["delta_net"] == 50.0
        assert info["total_qty"] == 70.0
        assert info["is_imbalanced"] is True
        assert info["imbalance_ratio"] == pytest.approx(50.0 / 70.0)

    def test_register_ticker_alias(self):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer()
        balancer.register_ticker_alias("KXNY-T75", "weather_ny_t75")
        balancer.update_position("weather_ny_t75", "polymarket", "yes", "buy", 40.0)

        # Query using the alias ticker
        assert balancer.get_delta("KXNY-T75") == 40.0
        delta_info = balancer.get_market_delta("KXNY-T75")
        assert delta_info is not None
        assert delta_info["delta_net"] == 40.0

    def test_get_skew_metrics_widens_spread_and_restricts_one_sided(self):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=50.0, max_imbalance_ratio=0.5)

        # Imbalanced long YES across venues
        balancer.update_position("mkt_2", "polymarket", "yes", "buy", 80.0)
        balancer.update_position("mkt_2", "kalshi", "no", "buy", 10.0)

        metrics = balancer.get_skew_metrics("mkt_2", local_inventory_usd=50.0, max_inventory_usd=100.0)
        assert metrics["cross_venue_delta"] == 70.0
        assert metrics["is_cross_imbalanced"] is True
        assert metrics["one_side_restriction"] == "ask_only"
        assert metrics["spread_multiplier"] > 1.0

    def test_singleton_get_and_reset(self):
        from inventory_balancer import get_inventory_balancer, reset_inventory_balancer
        reset_inventory_balancer()
        b1 = get_inventory_balancer()
        b2 = get_inventory_balancer()
        assert b1 is b2

        reset_inventory_balancer()
        b3 = get_inventory_balancer()
        assert b3 is not b1
        reset_inventory_balancer()

    def test_sync_from_db_preserves_non_db_pilot_positions(self):
        from unittest.mock import MagicMock
        balancer = InventoryBalancer()
        # Direct fill recorded from pilot
        balancer.update_position("pilot_ticker", "kalshi", "yes", "buy", 20.0)

        # Mock DB with positions for another market
        mock_db = MagicMock()
        mock_db.get_open_positions.return_value = [
            {"opportunity_id": 101, "market_identifier": "db_market", "platform": "cross"},
        ]
        mock_db.get_trades_for_opportunity.return_value = [
            {"platform": "polymarket", "side": "buy", "outcome": "yes", "size": 50.0, "status": "filled"},
        ]

        synced = balancer.sync_from_db(mock_db)
        assert synced == 1
        # DB position is tracked
        assert balancer.get_delta("db_market") == 50.0
        # Pilot position is preserved and not discarded!
        assert balancer.get_delta("pilot_ticker") == 20.0

    def test_resolve_market_key_avoids_substring_false_positives(self):
        balancer = InventoryBalancer()
        balancer.update_position("KXHIGHNY-26SEP28-T75", "kalshi", "yes", "buy", 10.0)

        # Exact match
        assert balancer._resolve_market_key("KXHIGHNY-26SEP28-T75") == "KXHIGHNY-26SEP28-T75"
        # Exact case-insensitive match
        assert balancer._resolve_market_key("kxhighny-26sep28-t75") == "KXHIGHNY-26SEP28-T75"
        # Substring "KX" should NOT falsely match "KXHIGHNY-26SEP28-T75"
        assert balancer._resolve_market_key("KX") == "KX"


class TestAutomatedInventoryRebalanceExecution:
    """Test automated rebalancing execution, safety guards, and dry-run/live paths."""

    def test_cooldown_tracking_and_expiry(self):
        """Rebalance cooldown is enforced per market and expires after configured interval."""
        balancer = InventoryBalancer(rebalance_cooldown_sec=60.0)
        assert balancer.is_cooldown_active("market_1", now=1000.0) is False

        balancer.record_rebalance_time("market_1", timestamp=1000.0)
        # Cooldown active inside 60s
        assert balancer.is_cooldown_active("market_1", now=1030.0) is True
        # Cooldown expired after 60s
        assert balancer.is_cooldown_active("market_1", now=1061.0) is False

    def test_execute_rebalancing_dry_run_records_trades_and_cooldown(self):
        """Dry-run execution logs opportunity and trade to TradeDB and activates cooldown."""
        balancer = InventoryBalancer(rebalance_cooldown_sec=60.0)
        mock_db = MagicMock()
        mock_db.log_opportunity.return_value = 42

        proposals = [{
            "market_key": "KXTEST-26SEP28",
            "action": "rebalance_buy",
            "target_venue": "kalshi",
            "side": "buy",
            "outcome": "no",
            "size": 20.0,
            "price": 0.45,
            "estimated_cost": 9.0,
            "current_delta": 40.0,
            "projected_delta": 20.0,
            "reason": "Deficient NO: buying 20.0 NO on kalshi @ 0.45",
        }]

        results = balancer.execute_rebalancing_proposals(
            proposals,
            dry_run=True,
            trade_db=mock_db,
        )

        assert len(results) == 1
        res = results[0]
        assert res["executed"] is True
        assert res["status"] == "dry_run"
        assert res["market_key"] == "KXTEST-26SEP28"
        assert res["size"] == 20.0
        assert res["cost"] == 9.0

        # DB logged
        mock_db.log_opportunity.assert_called_once()
        mock_db.log_trade.assert_called_once()
        assert mock_db.log_trade.call_args[1]["platform"] == "kalshi"
        assert mock_db.log_trade.call_args[1]["side"] == "BUY"
        assert mock_db.log_trade.call_args[1]["status"] == "dry_run"

        # Cooldown is now active for KXTEST-26SEP28
        assert balancer.is_cooldown_active("KXTEST-26SEP28") is True

    def test_execute_rebalancing_skips_when_cooldown_active(self):
        """Active cooldown causes proposal to be skipped."""
        balancer = InventoryBalancer(rebalance_cooldown_sec=60.0)
        balancer.record_rebalance_time("KXTEST-26SEP28")

        proposals = [{
            "market_key": "KXTEST-26SEP28",
            "action": "rebalance_buy",
            "target_venue": "kalshi",
            "side": "buy",
            "outcome": "no",
            "size": 15.0,
            "price": 0.50,
            "estimated_cost": 7.5,
        }]

        results = balancer.execute_rebalancing_proposals(proposals, dry_run=True)
        assert len(results) == 1
        assert results[0]["executed"] is False
        assert results[0]["status"] == "cooldown_active"

    def test_execute_rebalancing_cost_cap_exceeded(self):
        """Proposal exceeding max_rebalance_cost is skipped."""
        balancer = InventoryBalancer(max_rebalance_cost=25.0)

        proposals = [{
            "market_key": "KXTEST-26SEP28",
            "action": "rebalance_buy",
            "target_venue": "kalshi",
            "side": "buy",
            "outcome": "yes",
            "size": 100.0,
            "price": 0.50,
            "estimated_cost": 50.0,  # exceeds $25
        }]

        results = balancer.execute_rebalancing_proposals(proposals, dry_run=True)
        assert len(results) == 1
        assert results[0]["executed"] is False
        assert results[0]["status"] == "cost_cap_exceeded"

    def test_execute_rebalancing_kalshi_live_blocked_by_policy(self):
        """Live Kalshi order is blocked fail-closed when live_kalshi_submit_allowed is False."""
        balancer = InventoryBalancer()
        mock_kalshi = MagicMock()

        proposals = [{
            "market_key": "KXTEST-26SEP28",
            "action": "rebalance_buy",
            "target_venue": "kalshi",
            "side": "buy",
            "outcome": "no",
            "size": 10.0,
            "price": 0.40,
            "estimated_cost": 4.0,
        }]

        with patch("kalshi_policy.live_kalshi_submit_allowed", return_value=False):
            results = balancer.execute_rebalancing_proposals(
                proposals,
                dry_run=False,
                kalshi_client=mock_kalshi,
            )

        assert len(results) == 1
        assert results[0]["executed"] is False
        assert results[0]["status"] == "blocked_by_policy"
        mock_kalshi.place_order.assert_not_called()

    def test_execute_rebalancing_kalshi_live_success(self):
        """Live Kalshi order places successfully when policy allows it."""
        balancer = InventoryBalancer()
        mock_kalshi = MagicMock()
        mock_kalshi.place_order.return_value = {
            "order": {"order_id": "k_order_999", "status": "executed"}
        }
        mock_db = MagicMock()
        mock_db.log_opportunity.return_value = 101

        proposals = [{
            "market_key": "KXTEST-26SEP28",
            "action": "rebalance_buy",
            "target_venue": "kalshi",
            "side": "buy",
            "outcome": "no",
            "size": 10.0,
            "price": 0.40,
            "estimated_cost": 4.0,
        }]

        with patch("kalshi_policy.live_kalshi_submit_allowed", return_value=True):
            results = balancer.execute_rebalancing_proposals(
                proposals,
                dry_run=False,
                kalshi_client=mock_kalshi,
                trade_db=mock_db,
            )

        assert len(results) == 1
        res = results[0]
        assert res["executed"] is True
        assert res["status"] == "filled"
        assert res["order_id"] == "k_order_999"

        # Position updated in balancer
        assert balancer.get_delta("KXTEST-26SEP28") == -10.0

        # DB logged filled trade
        mock_db.log_trade.assert_called_once()
        assert mock_db.log_trade.call_args[1]["status"] == "filled"

    def test_execute_rebalancing_polymarket_live_success(self):
        """Live Polymarket order places successfully with valid client."""
        balancer = InventoryBalancer()
        mock_pm = MagicMock()
        mock_pm.place_order.return_value = {"success": True, "orderID": "pm_order_888"}
        mock_db = MagicMock()
        mock_db.log_opportunity.return_value = 102

        proposals = [{
            "market_key": "0xpm_condition_abc",
            "token_id": "tok_123",
            "action": "rebalance_buy",
            "target_venue": "polymarket",
            "side": "buy",
            "outcome": "yes",
            "size": 25.0,
            "price": 0.60,
            "estimated_cost": 15.0,
        }]

        results = balancer.execute_rebalancing_proposals(
            proposals,
            dry_run=False,
            polymarket_client=mock_pm,
            trade_db=mock_db,
        )

        assert len(results) == 1
        res = results[0]
        assert res["executed"] is True
        assert res["status"] == "filled"
        assert res["order_id"] == "pm_order_888"

        # Position updated in balancer
        assert balancer.get_delta("0xpm_condition_abc") == 25.0
        mock_pm.place_order.assert_called_once_with(
            token_id="tok_123",
            side="BUY",
            price=0.60,
            size=25.0,
            order_type="FOK",
        )
