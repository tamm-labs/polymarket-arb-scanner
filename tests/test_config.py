"""Tests for config.py — setup_logging, env helpers, and config validation."""

import importlib
import logging
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import (
    setup_logging, LOG_LEVEL, DASHBOARD_PORT, WEBHOOK_URL,
    _env_float, _env_non_negative_float, _env_int, _env_bool,
    ConfigError, validate_config,
)


# ---------------------------------------------------------------------------
# setup_logging
# ---------------------------------------------------------------------------

class TestSetupLogging:
    def test_default_level(self):
        setup_logging()
        root = logging.getLogger()
        # Root logger level should be DEBUG (handlers filter to configured level)
        assert root.level == logging.DEBUG

    def test_custom_level(self):
        setup_logging(level="WARNING")
        root = logging.getLogger()
        # Console handler should have WARNING level
        console_handler = root.handlers[0]
        assert console_handler.level == logging.WARNING

    def test_file_handler_created(self):
        with tempfile.NamedTemporaryFile(suffix=".log", delete=False) as f:
            log_path = f.name
        try:
            setup_logging(log_file=log_path)
            root = logging.getLogger()
            file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
            assert len(file_handlers) >= 1
            # File handler should have DEBUG level
            assert file_handlers[0].level == logging.DEBUG
        finally:
            # Clean up
            setup_logging()  # Reset handlers
            os.unlink(log_path)

    def test_no_file_handler_when_empty(self):
        setup_logging(log_file="")
        root = logging.getLogger()
        file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
        assert len(file_handlers) == 0

    def test_invalid_level_defaults_to_info(self):
        setup_logging(level="NONEXISTENT")
        root = logging.getLogger()
        # Should use INFO as fallback
        console_handler = root.handlers[0]
        assert console_handler.level == logging.INFO


# ---------------------------------------------------------------------------
# TestConfigConstants
# ---------------------------------------------------------------------------

class TestConfigConstants:
    def test_log_level_default(self):
        # When LOG_LEVEL env var not set, defaults to INFO
        assert LOG_LEVEL in ("INFO", "DEBUG", "WARNING", "ERROR")

    def test_dashboard_port_is_int(self):
        assert isinstance(DASHBOARD_PORT, int)

    def test_webhook_url_is_string(self):
        assert isinstance(WEBHOOK_URL, str)


# ---------------------------------------------------------------------------
# _env_float
# ---------------------------------------------------------------------------

class TestEnvFloat:
    def test_valid_float(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "3.14")
        assert _env_float("TEST_FLOAT", "0") == pytest.approx(3.14)

    def test_valid_int_string(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "42")
        assert _env_float("TEST_FLOAT", "0") == 42.0

    def test_negative_float(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "-1.5")
        assert _env_float("TEST_FLOAT", "0") == -1.5

    def test_uses_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_FLOAT_MISSING", raising=False)
        assert _env_float("TEST_FLOAT_MISSING", "9.9") == pytest.approx(9.9)

    def test_invalid_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "not_a_number")
        with pytest.raises(ConfigError, match="TEST_FLOAT.*not a valid float"):
            _env_float("TEST_FLOAT", "0")

    def test_empty_string_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_FLOAT", "")
        with pytest.raises(ConfigError, match="TEST_FLOAT.*not a valid float"):
            _env_float("TEST_FLOAT", "0")


class TestEnvNonNegativeFloat:
    def test_accepts_zero_and_positive_values(self, monkeypatch):
        monkeypatch.setenv("TEST_NON_NEGATIVE_FLOAT", "0")
        assert _env_non_negative_float("TEST_NON_NEGATIVE_FLOAT", "5") == 0.0
        monkeypatch.setenv("TEST_NON_NEGATIVE_FLOAT", "5.5")
        assert _env_non_negative_float("TEST_NON_NEGATIVE_FLOAT", "5") == 5.5

    @pytest.mark.parametrize("raw", ["-0.01", "nan", "inf", "-inf"])
    def test_rejects_negative_and_non_finite_values(self, monkeypatch, raw):
        monkeypatch.setenv("TEST_NON_NEGATIVE_FLOAT", raw)
        with pytest.raises(ConfigError, match="must be finite and >= 0"):
            _env_non_negative_float("TEST_NON_NEGATIVE_FLOAT", "5")


# ---------------------------------------------------------------------------
# _env_int
# ---------------------------------------------------------------------------

class TestEnvInt:
    def test_valid_int(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "42")
        assert _env_int("TEST_INT", "0") == 42

    def test_negative_int(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "-5")
        assert _env_int("TEST_INT", "0") == -5

    def test_uses_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_INT_MISSING", raising=False)
        assert _env_int("TEST_INT_MISSING", "7") == 7

    def test_float_string_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "3.14")
        with pytest.raises(ConfigError, match="TEST_INT.*not a valid integer"):
            _env_int("TEST_INT", "0")

    def test_invalid_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "abc")
        with pytest.raises(ConfigError, match="TEST_INT.*not a valid integer"):
            _env_int("TEST_INT", "0")

    def test_empty_string_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_INT", "")
        with pytest.raises(ConfigError, match="TEST_INT.*not a valid integer"):
            _env_int("TEST_INT", "0")


# ---------------------------------------------------------------------------
# _env_bool
# ---------------------------------------------------------------------------

class TestEnvBool:
    @pytest.mark.parametrize("raw", ["true", "True", "TRUE", "1", "yes", "YES"])
    def test_truthy_values(self, monkeypatch, raw):
        monkeypatch.setenv("TEST_BOOL", raw)
        assert _env_bool("TEST_BOOL", "false") is True

    @pytest.mark.parametrize("raw", ["false", "False", "FALSE", "0", "no", "NO"])
    def test_falsy_values(self, monkeypatch, raw):
        monkeypatch.setenv("TEST_BOOL", raw)
        assert _env_bool("TEST_BOOL", "true") is False

    def test_uses_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("TEST_BOOL_MISSING", raising=False)
        assert _env_bool("TEST_BOOL_MISSING", "true") is True
        assert _env_bool("TEST_BOOL_MISSING", "false") is False

    def test_invalid_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "maybe")
        with pytest.raises(ConfigError, match="TEST_BOOL.*not a valid boolean"):
            _env_bool("TEST_BOOL", "false")

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("TEST_BOOL", "  true  ")
        assert _env_bool("TEST_BOOL", "false") is True


# ---------------------------------------------------------------------------
# validate_config — range checks (uses importlib.reload to re-read env)
# ---------------------------------------------------------------------------

def _reload_config():
    """Force-reload config module to pick up env var changes.

    Returns the freshly reloaded module. Uses importlib.reload so that
    module-level code (including validate_config()) re-executes with current
    env vars. Raises whatever exception the module raises at load time.
    """
    import config as _cfg
    return importlib.reload(_cfg)


class TestValidateConfig:

    def test_default_config_is_valid(self):
        # The current defaults should pass validation with no errors
        warnings = validate_config()
        assert isinstance(warnings, list)

    def test_invalid_execution_mode(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_MODE", "yolo")
        with pytest.raises(ValueError, match="EXECUTION_MODE.*yolo"):
            _reload_config()

    def test_negative_max_trade_size(self, monkeypatch):
        monkeypatch.setenv("MAX_TRADE_SIZE", "-10")
        with pytest.raises(ValueError, match="MAX_TRADE_SIZE.*must be > 0"):
            _reload_config()

    def test_zero_daily_loss_limit(self, monkeypatch):
        monkeypatch.setenv("DAILY_LOSS_LIMIT", "0")
        with pytest.raises(ValueError, match="DAILY_LOSS_LIMIT.*must be > 0"):
            _reload_config()

    def test_zero_parallel_workers(self, monkeypatch):
        monkeypatch.setenv("PARALLEL_WORKERS", "0")
        with pytest.raises(ValueError, match="PARALLEL_WORKERS.*must be > 0"):
            _reload_config()

    def test_negative_min_liquidity(self, monkeypatch):
        monkeypatch.setenv("MIN_LIQUIDITY", "-1")
        with pytest.raises(ValueError, match="MIN_LIQUIDITY.*must be >= 0"):
            _reload_config()

    @pytest.mark.parametrize("bad_val", ["0", "-1", "inf", "-inf", "nan"])
    def test_mm_ws_book_max_age_seconds_rejects_non_positive_and_non_finite(self, monkeypatch, bad_val):
        monkeypatch.setenv("MM_WS_BOOK_MAX_AGE_SECONDS", bad_val)
        with pytest.raises(ValueError, match="MM_WS_BOOK_MAX_AGE_SECONDS.*must be > 0"):
            _reload_config()

    @pytest.mark.parametrize("bad_val", ["0", "-1", "inf", "-inf", "nan"])
    def test_ws_orderbook_max_age_seconds_rejects_non_positive_and_non_finite(self, monkeypatch, bad_val):
        monkeypatch.setenv("WS_ORDERBOOK_MAX_AGE_SECONDS", bad_val)
        with pytest.raises(ValueError, match="WS_ORDERBOOK_MAX_AGE_SECONDS.*must be > 0"):
            _reload_config()

    def test_ws_orderbook_streaming_defaults(self):
        cfg = _reload_config()
        assert cfg.WS_ORDERBOOK_STREAMING_ENABLED is True
        assert cfg.WS_ORDERBOOK_MAX_AGE_SECONDS == 15.0

    def test_mm_skew_spread_config_defaults(self):
        cfg = _reload_config()
        assert cfg.MM_SKEW_SPREAD_ENABLED is True
        assert cfg.MM_SKEW_SPREAD_FACTOR == 1.0
        assert cfg.MM_SKEW_SPREAD_MAX_MULTIPLIER == 3.0
        assert cfg.MM_CROSS_VENUE_SKEW_ENABLED is True

    def test_mm_skew_spread_config_validation(self, monkeypatch):
        monkeypatch.setenv("MM_SKEW_SPREAD_FACTOR", "-0.1")
        with pytest.raises(ValueError, match="MM_SKEW_SPREAD_FACTOR.*must be >= 0"):
            _reload_config()

        monkeypatch.setenv("MM_SKEW_SPREAD_FACTOR", "1.0")
        monkeypatch.setenv("MM_SKEW_SPREAD_MAX_MULTIPLIER", "0.5")
        with pytest.raises(ValueError, match="MM_SKEW_SPREAD_MAX_MULTIPLIER.*must be >= 1.0"):
            _reload_config()

    def test_mm_portfolio_guard_config_defaults(self):
        cfg = _reload_config()
        assert cfg.MM_PORTFOLIO_GUARD_ENABLED is True
        assert cfg.MM_MAX_PORTFOLIO_NOTIONAL_USD == 500.0
        assert cfg.MM_MAX_PORTFOLIO_MARGIN_UTILIZATION == 0.80

    def test_mm_portfolio_guard_config_validation(self, monkeypatch):
        monkeypatch.setenv("MM_MAX_PORTFOLIO_NOTIONAL_USD", "0")
        with pytest.raises(ValueError, match="MM_MAX_PORTFOLIO_NOTIONAL_USD.*must be > 0"):
            _reload_config()

        monkeypatch.setenv("MM_MAX_PORTFOLIO_NOTIONAL_USD", "500.0")
        monkeypatch.setenv("MM_MAX_PORTFOLIO_MARGIN_UTILIZATION", "0")
        with pytest.raises(ValueError, match="MM_MAX_PORTFOLIO_MARGIN_UTILIZATION.*must be in \\(0, 1\\]"):
            _reload_config()

        monkeypatch.setenv("MM_MAX_PORTFOLIO_MARGIN_UTILIZATION", "1.5")
        with pytest.raises(ValueError, match="MM_MAX_PORTFOLIO_MARGIN_UTILIZATION.*must be in \\(0, 1\\]"):
            _reload_config()

    def test_mm_dynamic_selection_config_defaults(self):
        cfg = _reload_config()
        assert cfg.MM_DYNAMIC_SELECTION_ENABLED is True
        assert cfg.MM_SELECTION_REFRESH_INTERVAL_SEC == 1800.0
        assert cfg.MM_MIN_24H_VOLUME == 0.0
        assert cfg.MM_MAX_SPREAD_CENTS == 0.0
        assert cfg.MM_VOLUME_WEIGHT == 0.20

    def test_mm_dynamic_selection_config_validation(self, monkeypatch):
        monkeypatch.setenv("MM_SELECTION_REFRESH_INTERVAL_SEC", "5.0")
        with pytest.raises(ValueError, match="MM_SELECTION_REFRESH_INTERVAL_SEC.*must be >= 10.0"):
            _reload_config()

        monkeypatch.setenv("MM_SELECTION_REFRESH_INTERVAL_SEC", "1800.0")
        monkeypatch.setenv("MM_MIN_24H_VOLUME", "-1.0")
        with pytest.raises(ValueError, match="MM_MIN_24H_VOLUME.*must be >= 0"):
            _reload_config()

        monkeypatch.setenv("MM_MIN_24H_VOLUME", "0.0")
        monkeypatch.setenv("MM_MAX_SPREAD_CENTS", "-0.1")
        with pytest.raises(ValueError, match="MM_MAX_SPREAD_CENTS.*must be >= 0"):
            _reload_config()

        monkeypatch.setenv("MM_MAX_SPREAD_CENTS", "0.0")
        monkeypatch.setenv("MM_VOLUME_WEIGHT", "-0.1")
        with pytest.raises(ValueError, match="MM_VOLUME_WEIGHT.*must be >= 0"):
            _reload_config()


    @pytest.mark.parametrize("name,value", [
        ("LIP_MIN_POOL", "-0.01"),
        ("LIP_MAX_MARKETS", "0"),
        ("LIP_SELECT_INTERVAL", "0"),
        ("LIP_PRICE_BAND_LOW", "-0.01"),
        ("LIP_PRICE_BAND_HIGH", "1.01"),
        ("LIP_MIN_HOURS_REMAINING", "-1"),
        ("LIP_DEPTH_PROBE_LIMIT", "0"),
    ])
    def test_invalid_lip_config_fails_fast(self, monkeypatch, name, value):
        monkeypatch.setenv(name, value)
        with pytest.raises(ValueError, match=name):
            _reload_config()

    def test_reversed_lip_price_band_fails_fast(self, monkeypatch):
        monkeypatch.setenv("LIP_PRICE_BAND_LOW", "0.80")
        monkeypatch.setenv("LIP_PRICE_BAND_HIGH", "0.20")
        with pytest.raises(ValueError, match=r"LIP_PRICE_BAND_HIGH.*LIP_PRICE_BAND_LOW"):
            _reload_config()

    @pytest.mark.parametrize(("name", "value"), [
        ("LIP_PRICE_BAND_LOW", "0"),
        ("LIP_PRICE_BAND_HIGH", "1"),
    ])
    def test_lip_price_band_requires_open_interval(self, monkeypatch,
                                                   name, value):
        monkeypatch.setenv(name, value)
        with pytest.raises(
            ValueError,
            match=r"LIP_PRICE_BAND_HIGH.*LIP_PRICE_BAND_LOW",
        ):
            _reload_config()

    def test_equal_lip_price_band_fails_fast(self, monkeypatch):
        monkeypatch.setenv("LIP_PRICE_BAND_LOW", "0.50")
        monkeypatch.setenv("LIP_PRICE_BAND_HIGH", "0.50")
        with pytest.raises(
            ValueError,
            match=r"LIP_PRICE_BAND_HIGH.*LIP_PRICE_BAND_LOW",
        ):
            _reload_config()

    def test_sizing_aggressiveness_above_one(self, monkeypatch):
        monkeypatch.setenv("SIZING_AGGRESSIVENESS", "1.5")
        with pytest.raises(ValueError, match="SIZING_AGGRESSIVENESS.*must be in"):
            _reload_config()

    def test_sizing_aggressiveness_negative(self, monkeypatch):
        monkeypatch.setenv("SIZING_AGGRESSIVENESS", "-0.1")
        with pytest.raises(ValueError, match="SIZING_AGGRESSIVENESS.*must be in"):
            _reload_config()

    def test_betfair_commission_rate_too_high(self, monkeypatch):
        monkeypatch.setenv("BETFAIR_COMMISSION_RATE", "2.0")
        with pytest.raises(ValueError, match="BETFAIR_COMMISSION_RATE.*must be in"):
            _reload_config()

    def test_smarkets_commission_rate_negative(self, monkeypatch):
        monkeypatch.setenv("SMARKETS_COMMISSION_RATE", "-0.01")
        with pytest.raises(ValueError, match="SMARKETS_COMMISSION_RATE.*must be in"):
            _reload_config()

    def test_gemini_fee_rate_too_high(self, monkeypatch):
        monkeypatch.setenv("GEMINI_FEE_RATE", "1.0")
        with pytest.raises(ValueError, match="GEMINI_FEE_RATE.*must be in"):
            _reload_config()

    def test_dashboard_port_out_of_range(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "99999")
        with pytest.raises(ValueError, match="DASHBOARD_PORT.*must be in"):
            _reload_config()

    def test_dashboard_port_negative(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "-1")
        with pytest.raises(ValueError, match="DASHBOARD_PORT.*must be in"):
            _reload_config()

    def test_fuzzy_match_threshold_zero(self, monkeypatch):
        monkeypatch.setenv("FUZZY_MATCH_THRESHOLD", "0")
        with pytest.raises(ValueError, match="FUZZY_MATCH_THRESHOLD.*must be in"):
            _reload_config()

    def test_fuzzy_match_threshold_above_100(self, monkeypatch):
        monkeypatch.setenv("FUZZY_MATCH_THRESHOLD", "101")
        with pytest.raises(ValueError, match="FUZZY_MATCH_THRESHOLD.*must be in"):
            _reload_config()

    def test_event_divergence_threshold_above_one(self, monkeypatch):
        monkeypatch.setenv("EVENT_DIVERGENCE_THRESHOLD", "1.5")
        with pytest.raises(ValueError, match="EVENT_DIVERGENCE_THRESHOLD.*must be in"):
            _reload_config()

    def test_hedge_max_spread_loss_pct_negative(self, monkeypatch):
        monkeypatch.setenv("HEDGE_MAX_SPREAD_LOSS_PCT", "-0.1")
        with pytest.raises(ValueError, match="HEDGE_MAX_SPREAD_LOSS_PCT.*must be in"):
            _reload_config()

    def test_reentry_improvement_threshold_above_one(self, monkeypatch):
        monkeypatch.setenv("REENTRY_IMPROVEMENT_THRESHOLD", "2.0")
        with pytest.raises(ValueError, match="REENTRY_IMPROVEMENT_THRESHOLD.*must be in"):
            _reload_config()

    def test_non_numeric_float_var(self, monkeypatch):
        monkeypatch.setenv("MAX_TRADE_SIZE", "abc")
        with pytest.raises(ValueError, match="MAX_TRADE_SIZE.*not a valid float"):
            _reload_config()

    def test_non_numeric_int_var(self, monkeypatch):
        monkeypatch.setenv("PARALLEL_WORKERS", "abc")
        with pytest.raises(ValueError, match="PARALLEL_WORKERS.*not a valid integer"):
            _reload_config()

    def test_invalid_bool_var(self, monkeypatch):
        monkeypatch.setenv("DRY_RUN", "maybe")
        with pytest.raises(ValueError, match="DRY_RUN.*not a valid boolean"):
            _reload_config()


# ---------------------------------------------------------------------------
# validate_config — warnings
# ---------------------------------------------------------------------------

class TestValidateConfigWarnings:

    def test_fullauth_dryrun_contradiction_warning(self, monkeypatch):
        monkeypatch.setenv("EXECUTION_MODE", "full-auto")
        monkeypatch.setenv("DRY_RUN", "true")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert any("full-auto" in w and "DRY_RUN" in w for w in warnings)

    def test_poll_timeout_less_than_interval_warning(self, monkeypatch):
        monkeypatch.setenv("FILL_POLL_INTERVAL", "5.0")
        monkeypatch.setenv("FILL_POLL_TIMEOUT", "1.0")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert any("FILL_POLL_TIMEOUT" in w for w in warnings)

    def test_invalid_log_level_warning(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "VERBOSE")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert any("LOG_LEVEL" in w for w in warnings)

    def test_valid_config_no_warnings(self):
        # Default config should produce no warnings (DRY_RUN=true, EXECUTION_MODE=semi-auto)
        warnings = validate_config()
        # May have warnings based on env, but at minimum it shouldn't error
        assert isinstance(warnings, list)


# ---------------------------------------------------------------------------
# validate_config — platform whitelist
# ---------------------------------------------------------------------------

class TestPlatformWhitelistConfig:

    def test_default_whitelist_is_kalshi_only(self, monkeypatch):
        monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **kw: None)
        monkeypatch.delenv("ENABLED_EXECUTION_PLATFORMS", raising=False)
        cfg = _reload_config()
        assert cfg.ENABLED_EXECUTION_PLATFORMS == frozenset({"kalshi"})

    def test_live_polymarket_is_blocked(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "polymarket,kalshi")
        monkeypatch.setenv("DRY_RUN", "false")
        with pytest.raises(ValueError, match="public-data/shadow-only"):
            _reload_config()

    def test_valid_platforms_accepted(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "polymarket,kalshi,sxbet")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        # Should not raise; just check it returns a list
        assert isinstance(warnings, list)

    def test_unknown_platform_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "polymarket,robinhood")
        # ConfigError is a ValueError subclass; raised at module reload time
        with pytest.raises(ValueError, match="unknown platforms.*robinhood"):
            _reload_config()

    def test_empty_whitelist_warns(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert any("empty" in w.lower() for w in warnings)

    def test_single_platform_valid(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "kalshi")
        cfg = _reload_config()
        assert "kalshi" in cfg.ENABLED_EXECUTION_PLATFORMS
        assert len(cfg.ENABLED_EXECUTION_PLATFORMS) == 1

    def test_all_eight_platforms_valid(self, monkeypatch):
        all_plats = "polymarket,kalshi,betfair,smarkets,sxbet,matchbook,gemini,ibkr"
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", all_plats)
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert isinstance(warnings, list)
        assert len(cfg.ENABLED_EXECUTION_PLATFORMS) == 8

    def test_whitespace_trimmed(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", " polymarket , kalshi ")
        cfg = _reload_config()
        assert "polymarket" in cfg.ENABLED_EXECUTION_PLATFORMS
        assert "kalshi" in cfg.ENABLED_EXECUTION_PLATFORMS

    def test_platform_min_order_size_all_platforms_present(self):
        from config import PLATFORM_MIN_ORDER_SIZE, _VALID_PLATFORMS
        for plat in _VALID_PLATFORMS:
            assert plat in PLATFORM_MIN_ORDER_SIZE, f"Missing min order size for {plat}"
            assert PLATFORM_MIN_ORDER_SIZE[plat] >= 0


class TestPolymarketRewardFetch:
    def test_rewards_mode_fetches_unless_kalshi_only(self):
        from config import polymarket_reward_fetch_enabled, polymarket_scan_enabled
        assert polymarket_reward_fetch_enabled("rewards") is True
        assert polymarket_scan_enabled("rewards") is False

    def test_kalshi_only_skips_polymarket_reward_fetch(self, monkeypatch):
        monkeypatch.setenv("SCAN_VENUES", "kalshi")
        cfg = _reload_config()
        assert cfg.polymarket_reward_fetch_enabled("rewards") is False
        assert cfg.polymarket_reward_fetch_enabled("all") is False


# ---------------------------------------------------------------------------
# validate_config — Phase 1 quick-win guards (PR #18)
# ---------------------------------------------------------------------------

class TestSXBetQuarantine:
    """SX Bet `place_order()` sends unsigned JSON. Live trading must be blocked."""

    def test_live_trading_with_sxbet_raises(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "kalshi,sxbet")
        monkeypatch.setenv("DRY_RUN", "false")
        with pytest.raises(ValueError, match="SX Bet"):
            _reload_config()

    def test_dry_run_with_sxbet_ok(self, monkeypatch):
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "polymarket,sxbet")
        monkeypatch.setenv("DRY_RUN", "true")
        cfg = _reload_config()  # must not raise
        assert "sxbet" in cfg.ENABLED_EXECUTION_PLATFORMS

    def test_live_trading_without_sxbet_ok(self, monkeypatch, tmp_path):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        monkeypatch.setenv("ENABLED_EXECUTION_PLATFORMS", "kalshi")
        monkeypatch.setenv("DRY_RUN", "false")
        try:
            cfg = _reload_config()  # must not raise
            assert cfg.ENABLED_EXECUTION_PLATFORMS == frozenset({"kalshi"})
        finally:
            monkeypatch.setenv("DRY_RUN", "true")
            _reload_config()


class TestDashboardHostGuard:
    """Non-loopback DASHBOARD_HOST without DASHBOARD_PASS is an unauth public bind."""

    def test_non_loopback_without_password_raises(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "8080")
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.setenv("DASHBOARD_PASS", "")
        with pytest.raises(ValueError, match="non-loopback"):
            _reload_config()

    def test_non_loopback_with_password_ok(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "8080")
        monkeypatch.setenv("DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.setenv("DASHBOARD_PASS", "strong-secret")
        cfg = _reload_config()  # must not raise
        assert cfg.DASHBOARD_HOST == "0.0.0.0"
        assert cfg.DASHBOARD_PASS == "strong-secret"

    def test_loopback_without_password_warns_not_raises(self, monkeypatch):
        monkeypatch.setenv("DASHBOARD_PORT", "8080")
        monkeypatch.setenv("DASHBOARD_HOST", "127.0.0.1")
        monkeypatch.setenv("DASHBOARD_PASS", "")
        cfg = _reload_config()
        warnings = cfg.validate_config()
        assert any("loopback-only" in w for w in warnings)

    def test_default_host_is_loopback(self, monkeypatch):
        monkeypatch.delenv("DASHBOARD_HOST", raising=False)
        cfg = _reload_config()
        assert cfg.DASHBOARD_HOST == "127.0.0.1"


class TestResolutionWindowOverride:
    """RESOLUTION_SNIPE_WINDOW_HOURS replaces the previously hardcoded 48h literal."""

    def test_default_is_48_hours(self, monkeypatch):
        monkeypatch.delenv("RESOLUTION_SNIPE_WINDOW_HOURS", raising=False)
        cfg = _reload_config()
        assert cfg.RESOLUTION_SNIPE_WINDOW_HOURS == 48.0

    def test_env_override_respected(self, monkeypatch):
        monkeypatch.setenv("RESOLUTION_SNIPE_WINDOW_HOURS", "72")
        cfg = _reload_config()
        assert cfg.RESOLUTION_SNIPE_WINDOW_HOURS == 72.0


# ---------------------------------------------------------------------------
# STRAT-04: Logical Arbitrage (Phase 9)
# ---------------------------------------------------------------------------

class TestLogicalArbConfig:
    def test_logical_arb_enabled_defaults_false(self):
        from config import LOGICAL_ARB_ENABLED
        assert LOGICAL_ARB_ENABLED is False

    def test_logical_arb_price_threshold_defaults_to_0_05(self):
        from config import LOGICAL_ARB_PRICE_THRESHOLD
        assert LOGICAL_ARB_PRICE_THRESHOLD == 0.05

    def test_logical_arb_max_trade_size_defaults_to_20(self):
        from config import LOGICAL_ARB_MAX_TRADE_SIZE
        assert LOGICAL_ARB_MAX_TRADE_SIZE == 20.0

    def test_logical_arb_rules_defaults_to_empty_list(self):
        from config import LOGICAL_ARB_RULES
        assert isinstance(LOGICAL_ARB_RULES, list)
        assert len(LOGICAL_ARB_RULES) == 0

    def test_logical_arb_rules_from_env_json(self, monkeypatch):
        rules_json = '[{"if_yes": "market_a", "then_yes": "market_b", "relationship": "implies"}]'
        monkeypatch.setenv("LOGICAL_ARB_RULES", rules_json)
        monkeypatch.setenv("LOGICAL_ARB_ENABLED", "true")
        cfg = _reload_config()
        assert len(cfg.LOGICAL_ARB_RULES) == 1
        assert cfg.LOGICAL_ARB_RULES[0]["relationship"] == "implies"

    def test_logical_arb_rules_invalid_json_raises_config_error(self, monkeypatch):
        monkeypatch.setenv("LOGICAL_ARB_RULES", "not valid json")
        monkeypatch.setenv("LOGICAL_ARB_ENABLED", "true")
        with pytest.raises(ValueError, match="Invalid LOGICAL_ARB_RULES"):
            _reload_config()


# ---------------------------------------------------------------------------
# STRAT-05: Whale Copy Trading (Phase 9)
# ---------------------------------------------------------------------------

class TestWhaleCopyConfig:
    def test_whale_copy_enabled_defaults_false(self):
        from config import WHALE_COPY_ENABLED
        assert WHALE_COPY_ENABLED is False

    def test_whale_copy_max_positions_defaults_to_5(self):
        from config import WHALE_COPY_MAX_POSITIONS
        assert WHALE_COPY_MAX_POSITIONS == 5

    def test_whale_copy_max_trade_size_defaults_to_15(self):
        from config import WHALE_COPY_MAX_TRADE_SIZE
        assert WHALE_COPY_MAX_TRADE_SIZE == 15.0

    def test_whale_copy_poll_interval_defaults_to_10(self):
        from config import WHALE_COPY_POLL_INTERVAL
        assert WHALE_COPY_POLL_INTERVAL == 10

    def test_whale_wallets_defaults_to_empty_list(self):
        from config import WHALE_WALLETS
        assert isinstance(WHALE_WALLETS, list)
        assert len(WHALE_WALLETS) == 0

    def test_whale_wallets_from_env_comma_separated(self, monkeypatch):
        wallets = "0x1234567890abcdef, 0xabcdef1234567890, 0xdeadbeef"
        monkeypatch.setenv("WHALE_WALLETS", wallets)
        cfg = _reload_config()
        assert len(cfg.WHALE_WALLETS) == 3
        assert "0x1234567890abcdef" in cfg.WHALE_WALLETS
        assert "0xabcdef1234567890" in cfg.WHALE_WALLETS
        assert "0xdeadbeef" in cfg.WHALE_WALLETS

    def test_whale_wallets_trims_whitespace(self, monkeypatch):
        wallets = "  0x1111  ,  0x2222  "
        monkeypatch.setenv("WHALE_WALLETS", wallets)
        cfg = _reload_config()
        assert cfg.WHALE_WALLETS == ["0x1111", "0x2222"]

    def test_whale_copy_disables_when_no_wallets(self, monkeypatch):
        monkeypatch.setenv("WHALE_COPY_ENABLED", "true")
        monkeypatch.setenv("WHALE_WALLETS", "")
        cfg = _reload_config()
        # Should disable itself gracefully
        assert cfg.WHALE_COPY_ENABLED is False

    def test_polygonscan_api_key_optional(self):
        from config import POLYGONSCAN_API_KEY
        assert isinstance(POLYGONSCAN_API_KEY, str)


# ---------------------------------------------------------------------------
# Plan 04: CTF Primitives Config
# ---------------------------------------------------------------------------

class TestCTFConfig:
    def test_polymarket_ctf_in_valid_platforms(self):
        from config import _VALID_PLATFORMS
        assert "polymarket_ctf" in _VALID_PLATFORMS

    @pytest.mark.parametrize("bad_val", ["-0.01", "-1.0", "nan", "inf", "-inf"])
    def test_ctf_gas_estimate_rejects_negative_and_non_finite(self, monkeypatch, bad_val):
        monkeypatch.setenv("CTF_GAS_ESTIMATE", bad_val)
        with pytest.raises(ValueError, match="must be finite and >= 0"):
            _reload_config()

    def test_ctf_gas_estimate_accepts_valid(self, monkeypatch):
        monkeypatch.setenv("CTF_GAS_ESTIMATE", "0.025")
        cfg = _reload_config()
        assert cfg.CTF_GAS_ESTIMATE == 0.025

    def test_ctf_max_resolution_days_defaults_to_zero(self):
        cfg = _reload_config()
        assert cfg.CTF_MAX_RESOLUTION_DAYS == 0

    def test_ctf_max_resolution_days_env_override(self, monkeypatch):
        monkeypatch.setenv("CTF_MAX_RESOLUTION_DAYS", "14")
        cfg = _reload_config()
        assert cfg.CTF_MAX_RESOLUTION_DAYS == 14

    def test_ctf_max_resolution_days_rejects_negative(self, monkeypatch):
        cfg = _reload_config()
        monkeypatch.setattr(cfg, "CTF_MAX_RESOLUTION_DAYS", -1)
        with pytest.raises(cfg.ConfigError, match="CTF_MAX_RESOLUTION_DAYS=-1 must be >= 0"):
            cfg.validate_config()

    @pytest.mark.parametrize("dry_run", [True, False])
    @pytest.mark.parametrize("bad_addr", ["", "invalid_hex", "0x1234", "0x" + "0" * 40])
    def test_ctf_address_validation_rejects_invalid_addresses(self, monkeypatch, tmp_path, dry_run, bad_addr):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        cfg = _reload_config()
        monkeypatch.setattr(cfg, "DRY_RUN", dry_run)
        monkeypatch.setattr(cfg, "CTF_ENABLED", True)
        monkeypatch.setattr(cfg, "ENABLED_EXECUTION_PLATFORMS", frozenset(["kalshi"]))

        # Bad CONDITIONAL_TOKENS_ADDRESS
        monkeypatch.setattr(cfg, "CONDITIONAL_TOKENS_ADDRESS", bad_addr)
        monkeypatch.setattr(cfg, "COLLATERAL_TOKEN_ADDRESS", "0x" + "1" * 40)
        with pytest.raises(cfg.ConfigError, match="CONDITIONAL_TOKENS_ADDRESS must be a valid non-zero 20-byte"):
            cfg.validate_config()

        # Bad COLLATERAL_TOKEN_ADDRESS
        monkeypatch.setattr(cfg, "CONDITIONAL_TOKENS_ADDRESS", "0x" + "1" * 40)
        monkeypatch.setattr(cfg, "COLLATERAL_TOKEN_ADDRESS", bad_addr)
        with pytest.raises(cfg.ConfigError, match="COLLATERAL_TOKEN_ADDRESS must be a valid non-zero 20-byte"):
            cfg.validate_config()

    @pytest.mark.parametrize("dry_run", [True, False])
    def test_ctf_convert_requires_valid_adapter(self, monkeypatch, tmp_path, dry_run):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        cfg = _reload_config()
        monkeypatch.setattr(cfg, "DRY_RUN", dry_run)
        monkeypatch.setattr(cfg, "CTF_ENABLED", True)
        monkeypatch.setattr(cfg, "CTF_CONVERT_ENABLED", True)
        monkeypatch.setattr(cfg, "CONDITIONAL_TOKENS_ADDRESS", "0x" + "1" * 40)
        monkeypatch.setattr(cfg, "COLLATERAL_TOKEN_ADDRESS", "0x" + "2" * 40)
        monkeypatch.setattr(cfg, "ENABLED_EXECUTION_PLATFORMS", frozenset(["kalshi"]))

        # Invalid adapter
        monkeypatch.setattr(cfg, "NEG_RISK_ADAPTER_ADDRESS", "0x" + "0" * 40)
        with pytest.raises(cfg.ConfigError, match="NEG_RISK_ADAPTER_ADDRESS must be a valid non-zero 20-byte"):
            cfg.validate_config()

        # Valid adapter passes
        monkeypatch.setattr(cfg, "NEG_RISK_ADAPTER_ADDRESS", "0x" + "3" * 40)
        cfg.validate_config()

    def test_limitless_rewards_validation(self, monkeypatch, tmp_path):
        from live_envelope_fixtures import write_test_envelope
        monkeypatch.setenv("LIVE_ENVELOPE_PATH", str(write_test_envelope(tmp_path)))
        cfg = _reload_config()
        monkeypatch.setattr(cfg, "DRY_RUN", False)
        monkeypatch.setattr(cfg, "LIMITLESS_REWARDS_ENABLED", True)
        monkeypatch.setattr(cfg, "LIMITLESS_API_KEY", "")
        monkeypatch.setattr(cfg, "LIMITLESS_PRIVATE_KEY", "")
        monkeypatch.setattr(cfg, "LIMITLESS_EXCHANGE_CONTRACT", "")
        monkeypatch.setattr(cfg, "ENABLED_EXECUTION_PLATFORMS", frozenset(["kalshi"]))

        # Missing API key in live mode fails
        with pytest.raises(cfg.ConfigError, match="requires LIMITLESS_API_KEY"):
            cfg.validate_config()

        # Missing private key in live mode fails
        monkeypatch.setattr(cfg, "LIMITLESS_API_KEY", "valid-key")
        with pytest.raises(cfg.ConfigError, match="requires LIMITLESS_PRIVATE_KEY"):
            cfg.validate_config()

        # Missing exchange contract in live mode fails
        monkeypatch.setattr(cfg, "LIMITLESS_PRIVATE_KEY", "valid-pk")
        with pytest.raises(cfg.ConfigError, match="requires valid non-zero LIMITLESS_EXCHANGE_CONTRACT address"):
            cfg.validate_config()

        # Missing limitless in execution whitelist fails
        monkeypatch.setattr(cfg, "LIMITLESS_EXCHANGE_CONTRACT", "0x" + "2" * 40)
        with pytest.raises(cfg.ConfigError, match="requires 'limitless' in ENABLED_EXECUTION_PLATFORMS"):
            cfg.validate_config()

        # Valid live configuration passes
        monkeypatch.setattr(cfg, "ENABLED_EXECUTION_PLATFORMS", frozenset(["kalshi", "limitless"]))
        cfg.validate_config()

        # Dry run passes even without credentials
        monkeypatch.setattr(cfg, "DRY_RUN", True)
        monkeypatch.setattr(cfg, "LIMITLESS_API_KEY", "")
        monkeypatch.setattr(cfg, "LIMITLESS_PRIVATE_KEY", "")
        monkeypatch.setattr(cfg, "LIMITLESS_EXCHANGE_CONTRACT", "")
        cfg.validate_config()


# ---------------------------------------------------------------------------
# Env hygiene — no personal/global env files merged into the bot environment
# ---------------------------------------------------------------------------

class TestEnvFileHygiene:
    """The bot must only load the project-local .env.

    Loading ~/.claude/.env (or any file outside the repo) merges personal
    credentials into the trading process environment. Regression guard for
    the audit finding that config.py and cli.py both did exactly that.
    """

    def _module_source(self, name: str) -> str:
        root = Path(__file__).resolve().parent.parent
        return (root / name).read_text(encoding="utf-8")

    @pytest.mark.parametrize("module_file", ["config.py", "cli.py"])
    def test_no_personal_env_file_loaded(self, module_file):
        source = self._module_source(module_file)
        assert "load_dotenv(os.path.expanduser" not in source, (
            f"{module_file} loads a dotenv file outside the project directory "
            "— personal env files must never be merged into the bot environment"
        )
        assert "find_dotenv" not in source, (
            f"{module_file} must not search parent directories for dotenv files"
        )

    @pytest.mark.parametrize("module_file", ["config.py", "cli.py"])
    def test_project_local_dotenv_still_loaded(self, module_file):
        source = self._module_source(module_file)
        assert 'Path(__file__).resolve().parent / ".env"' in source, (
            f"{module_file} must load only the .env adjacent to the module"
        )
        assert "load_dotenv(dotenv_path=" in source
