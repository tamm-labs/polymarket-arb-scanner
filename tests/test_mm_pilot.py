"""Plan 10 tests — fill detection, auto-hedge, kill switch, canary, dry-run.

Spec test-plan cases 1-5 and 13-15 (docs/plans/10-mm-pilot-prep.md section 10).
Fail-before: none of these behaviors exist on origin/master (mm_pilot.py is
new); every test here fails on master by construction.
"""

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib
import json
import logging
from unittest.mock import MagicMock

import pytest

from market_maker import ToxicFlowDetector, VolatilityTracker
from mm_pilot import (
    ControlsPoller,
    FillEvent,
    KalshiMMPilot,
    SupabaseControlsClient,
    build_controls_client_from_env,
)


def live_config():
    """Resolve the LIVE config module.

    Other test files in this suite pop/replace sys.modules["config"], so a
    module-level `import config` binding can go stale — mm_pilot reads config
    off sys.modules at call time and would see a different object (same
    pattern as tests/test_negrisk_no_side.py).
    """
    return importlib.import_module("config")


TICKER = "KXTEST-26DEC31"


# ---------------------------------------------------------------------------
# Fakes / fixtures
# ---------------------------------------------------------------------------

def make_book(yes_bid=0.49, no_bid=0.49, yes_qty=500.0, no_qty=500.0):
    """Raw Kalshi book (orderbook_fp schema). yes_ask derives as 1 - no_bid."""
    return {"orderbook_fp": {
        "yes_dollars": [[f"{yes_bid:.4f}", f"{yes_qty:.2f}"]],
        "no_dollars": [[f"{no_bid:.4f}", f"{no_qty:.2f}"]],
    }}


class FakeKalshiClient:
    def __init__(self, books=None):
        self.books = books if books is not None else {TICKER: make_book()}
        self.fills_script: list[dict] = []
        self.placed: list[dict] = []
        self.cancelled: list[str] = []
        self.place_order_calls = 0
        self.cancel_order_calls = 0
        self.fail_place = False
        # Cancel-retry / backoff testing (finding #2): the next N calls to
        # cancel_order fail (return False without raising); 0 means succeed.
        self.cancel_fail_count = 0
        # Fill-poll-failure testing (finding #3): raise instead of returning
        # fills_script when True and the caller opted into raise_on_error.
        self.fail_get_fills = False
        # Reconciliation testing (finding #4).
        self.positions_script: list[dict] = []
        self.open_orders_script: list[dict] = []
        self.fail_get_positions = False
        self.fail_get_open_orders = False

    def fetch_order_book(self, ticker):
        return self.books.get(ticker)

    def place_order(self, ticker, side, action, count, price_dollars,
                    time_in_force="fill_or_kill", reducing=False):
        self.place_order_calls += 1
        if self.fail_place:
            return None
        oid = f"k_{self.place_order_calls}"
        self.placed.append({
            "order_id": oid, "ticker": ticker, "side": side, "action": action,
            "count": count, "price": price_dollars, "tif": time_in_force,
            "reducing": reducing,
        })
        return {"order": {"order_id": oid}}

    def cancel_order(self, order_id):
        self.cancel_order_calls += 1
        if self.cancel_fail_count > 0:
            self.cancel_fail_count -= 1
            return False
        self.cancelled.append(order_id)
        self.open_orders_script = [
            order for order in self.open_orders_script
            if str(order.get("order_id") or order.get("id") or "") != order_id
        ]
        return True

    def get_fills(self, min_ts=None, raise_on_error=False, **kwargs):
        if self.fail_get_fills:
            if raise_on_error:
                raise RuntimeError("fake get_fills failure")
            return []
        return list(self.fills_script)

    def get_positions(self, raise_on_error=False):
        if self.fail_get_positions:
            if raise_on_error:
                raise RuntimeError("fake get_positions failure")
            return []
        return list(self.positions_script)

    def get_open_orders(self, ticker=None, **kwargs):
        if self.fail_get_open_orders:
            raise RuntimeError("fake get_open_orders failure")
        return list(self.open_orders_script)

    def get_balance(self):
        return self.balance


class RecordingHedger:
    """Stub hedge executor: records calls, scripted success."""

    def __init__(self, result=True):
        self.result = result
        self.calls: list[dict] = []

    def hedge_inventory(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def kfill(order_id, ticker=TICKER, side="yes", action="buy", count=4,
          yes_price=49, trade_id=None, is_taker=False, created=None):
    fill = {
        "trade_id": trade_id or f"tr_{order_id}_{count}_{yes_price}",
        "order_id": order_id, "ticker": ticker, "side": side,
        "action": action, "count": count, "yes_price": yes_price,
        "is_taker": is_taker,
    }
    if created is not None:
        fill["created_time"] = created
    return fill


@pytest.fixture
def clock():
    return [1_000_000.0]


@pytest.fixture
def pilot_env(monkeypatch):
    """Force the pilot's flag preconditions on (against the LIVE config).

    Yields the live config module so tests can monkeypatch further keys on
    the object mm_pilot actually reads.
    """
    from inventory_balancer import reset_inventory_balancer
    reset_inventory_balancer()
    cfg = live_config()
    monkeypatch.setattr(cfg, "MM_KALSHI_PILOT_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_TOXIC_FLOW_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_VOLATILITY_ADJUSTED_ENABLED", True)
    monkeypatch.setattr(cfg, "MM_AUTO_HEDGE_ENABLED", True)
    yield cfg
    reset_inventory_balancer()


def build_pilot(clock, client=None, dry_run=False, hedger=None,
                controls_on=True, detector=None, vol=None,
                selection=(TICKER,), reconciled=True, state_path=None,
                inventory_balancer=None):
    from inventory_balancer import InventoryBalancer
    def time_fn():
        return clock[0]
    controls = ControlsPoller(time_fn=time_fn)
    controls.set_cached(controls_on)
    decisions: list[dict] = []
    hedger = hedger if hedger is not None else RecordingHedger()
    pilot = KalshiMMPilot(
        kalshi_client=client,
        controls=controls,
        toxic_detector=detector or ToxicFlowDetector(),
        volatility_tracker=vol or VolatilityTracker(min_samples=1),
        hedger_factory=lambda proxy: hedger,
        decision_writer=decisions.append,
        dry_run=dry_run,
        time_fn=time_fn,
        mono_fn=time_fn,
        state_path=state_path,
        inventory_balancer=inventory_balancer if inventory_balancer is not None else InventoryBalancer(),
    )
    if selection is not None:
        pilot.update_selection(list(selection))
    if client is not None:
        for ticker, book in client.books.items():
            pilot.update_book(ticker, book)
    # Most tests exercise gate/fill/hedge logic, not the startup
    # reconciliation feature itself — default to "already reconciled" so
    # authorize_order's finding-#4 gate doesn't block every other test.
    # TestReconciliation below constructs KalshiMMPilot directly to exercise
    # the real unreconciled-by-default state.
    pilot._reconciled = reconciled or dry_run
    pilot._decisions = decisions
    pilot._test_controls = controls
    pilot._test_hedger = hedger
    return pilot


class TestAlertLogging:
    @pytest.mark.parametrize(("severity", "expected_level"), [
        ("INFO", logging.INFO),
        ("WARNING", logging.WARNING),
        ("CRITICAL", logging.CRITICAL),
    ])
    def test_alert_logs_at_declared_severity(self, clock, caplog, severity,
                                             expected_level):
        pilot = build_pilot(clock, dry_run=True)
        with caplog.at_level(logging.INFO, logger="mm_pilot"):
            pilot._alert("test_alert", severity, "test message")

        record = caplog.records[-1]
        assert record.levelno == expected_level
        assert record.getMessage() == "MM pilot test_alert: test message"


# ---------------------------------------------------------------------------
# 1. Fill detection: exactly one FillEvent, deduped across polls
# ---------------------------------------------------------------------------

class TestFillDetection:
    def test_registered_fill_yields_exactly_one_event(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is not None
        client.fills_script = [kfill(oid, count=4, created=clock[0])]

        events = pilot.poll_fills()
        assert len(events) == 1
        assert isinstance(events[0], FillEvent)
        assert events[0].order_id == oid
        assert events[0].count == 4
        assert events[0].price == pytest.approx(0.49)

    def test_duplicate_fill_across_overlap_window_deduped(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        fill = kfill(oid, count=4, created=clock[0])
        client.fills_script = [fill]
        first = pilot.poll_fills()
        # Same fill re-served by the 60s overlap window on the next poll
        clock[0] += 2
        second = pilot.poll_fills()
        assert len(first) == 1
        assert len(second) == 0

    def test_fill_without_trade_id_halts_before_accounting(self, pilot_env,
                                                            clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        fill = kfill(oid, count=4, created=clock[0])
        del fill["trade_id"]
        client.fills_script = [fill]

        assert pilot.poll_fills() == []
        assert pilot.halted is True
        assert "fill without trade_id" in pilot.halt_reason
        assert pilot.inventory.net_contracts(TICKER) == 0


# ---------------------------------------------------------------------------
# 2. Unknown-order fill in a pilot market -> halt, all cancels issued
# ---------------------------------------------------------------------------

class TestUnknownOrderFill:
    def test_foreign_fill_halts_pilot_and_cancels_everything(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill("someone_elses_order", created=clock[0])]

        pilot.poll_fills()
        assert pilot.halted is True
        assert "unknown order_id" in pilot.halt_reason
        assert oid in client.cancelled          # resting quote cancelled
        assert pilot.resting_orders() == []

    def test_fill_in_non_pilot_market_is_ignored(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        client.fills_script = [kfill("arb_executor_order", ticker="OTHER-MKT",
                                     created=clock[0])]
        events = pilot.poll_fills()
        assert events == []
        assert pilot.halted is False


# ---------------------------------------------------------------------------
# 3. Auto-hedge fires past the deadband, correct reduce direction
# ---------------------------------------------------------------------------

class TestAutoHedge:
    def test_long_fill_past_deadband_triggers_sell_yes_hedge(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True  # isolate hedge logic from canary sizing
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # |net| = $10 > deadband $5 -> hedge with excess = $10
        assert len(hedger.calls) == 1
        call = hedger.calls[0]
        assert call["side"] == "yes"
        assert call["platform"] == "kalshi"
        assert call["size"] == pytest.approx(10.0)
        assert call["reduce_action"] == "sell"

    def test_short_fill_past_deadband_hedges_no_side(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True
        oid = pilot.place_pilot_order(TICKER, "no", "buy", 20, 0.50,
                                      purpose="quote_ask")
        client.fills_script = [kfill(oid, side="no", count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert len(hedger.calls) == 1
        assert hedger.calls[0]["side"] == "no"

    def test_fill_inside_deadband_does_not_hedge(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0])]
        pilot.poll_fills()
        # $1.96 < $5 deadband: rebalance arm, no hedge order
        assert hedger.calls == []
        assert any(d.get("reason") == "hedge_deadband"
                   for d in pilot._decisions)


# ---------------------------------------------------------------------------
# 4. Hedge failure -> quotes pulled, market halted, no re-quote
# ---------------------------------------------------------------------------

class TestHedgeFailClosed:
    def test_failed_hedge_pulls_quotes_and_halts_market(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=False)  # e.g. book with no bids
        pilot = build_pilot(clock, client=client, hedger=hedger)
        pilot.canary_graduated = True  # post-canary: market halt, not pilot halt
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # Retry once, then fail closed
        assert len(hedger.calls) == 2
        assert TICKER in pilot.get_status()["markets_halted"]
        assert pilot.resting_orders(TICKER) == []
        # Next refresh places zero quotes in the halted market
        assert pilot.refresh_market(TICKER) == []

    def test_hedge_failure_during_canary_halts_whole_pilot(self, pilot_env,
                                                           clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=False)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert pilot.halted is True

    def test_second_market_halt_in_window_halts_pilot(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.canary_graduated = True
        pilot.halt_market(TICKER, "hedge failure")
        assert pilot.halted is False
        clock[0] += 600  # inside MM_HALT_WINDOW_SECONDS (3600)
        pilot._market_halted.pop(TICKER)  # operator reconciled the market
        pilot.halt_market(TICKER, "hedge failure again")
        assert pilot.halted is True


# ---------------------------------------------------------------------------
# 5. Hedge latency ceiling (frozen clock)
# ---------------------------------------------------------------------------

class TestHedgeLatency:
    def test_latency_over_ceiling_takes_halt_path(self, pilot_env, clock,
                                                  monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)  # hedge eventually lands...
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # Fill created 20s ago -> detection + hedge exceed the 10s ceiling
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0] - 20)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "latency" in pilot.halt_reason

    def test_unfilled_hedge_order_past_ceiling_halts(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 10, 0.49)
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 10, 0.49,
                                       purpose="hedge", reducing=True)
        assert hoid is not None
        clock[0] += 11  # past MM_HEDGE_MAX_LATENCY_SECONDS with no fill
        pilot.poll_fills()
        assert pilot.halted is True
        assert hoid in client.cancelled  # remainder cancelled


# ---------------------------------------------------------------------------
# 13. Kill switch: flip false / stale cache -> cancel everything
# ---------------------------------------------------------------------------

class TestKillSwitch:
    def test_read_only_rest_client_fetches_control(self):
        response = MagicMock()
        response.json.return_value = [{"value": True}]
        session = MagicMock()
        session.get.return_value = response
        client = SupabaseControlsClient(
            "https://example.supabase.co", "test-key", session=session)

        assert client.fetch_control("mm_pilot_enabled") is True
        response.raise_for_status.assert_called_once()
        _, kwargs = session.get.call_args
        assert kwargs["params"]["key"] == "eq.mm_pilot_enabled"
        assert kwargs["params"]["select"] == "value"

    def test_read_only_rest_client_rejects_string_false(self):
        response = MagicMock()
        response.json.return_value = [{"value": "false"}]
        session = MagicMock()
        session.get.return_value = response
        client = SupabaseControlsClient(
            "https://example.supabase.co", "test-key", session=session)

        assert client.fetch_control("mm_pilot_enabled") is False

    def test_rest_client_builder_requires_control_credentials(
            self, monkeypatch):
        monkeypatch.delenv("SUPABASE_URL", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
        monkeypatch.delenv("SUPABASE_KEY", raising=False)
        with pytest.raises(RuntimeError, match="SUPABASE_URL"):
            build_controls_client_from_env()

    def test_rest_client_builder_accepts_service_role_key(self, monkeypatch):
        monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
        monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
        monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-test")
        monkeypatch.delenv("SUPABASE_KEY", raising=False)

        client = build_controls_client_from_env()

        assert client._key == "service-role-test"

    @pytest.mark.parametrize("url", ["http://example.supabase.co", "https://169.254.169.254"])
    def test_rest_client_builder_rejects_unsafe_url(self, monkeypatch, url):
        monkeypatch.setenv("SUPABASE_URL", url)
        monkeypatch.setenv("SUPABASE_SERVICE_KEY", "service-role-test")

        with pytest.raises(ValueError, match="SUPABASE_URL"):
            build_controls_client_from_env()

    def test_controls_poller_accepts_read_only_rest_client(self, clock):
        client = MagicMock()
        client.fetch_control.return_value = True
        poller = ControlsPoller(supabase_client=client,
                                time_fn=lambda: clock[0])

        poller.poll()

        assert poller.is_enabled() is True
        client.fetch_control.assert_called_once_with("mm_pilot_enabled")

    def test_controls_flip_false_cancels_all_next_cycle(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot.resting_orders() != []
        pilot._test_controls.set_cached(False)
        pilot.refresh_market(TICKER)
        assert pilot.halted is True
        assert oid in client.cancelled
        assert pilot.resting_orders() == []

    def test_stale_controls_cache_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                purpose="quote_bid")
        # Cache is true but older than MM_CONTROLS_MAX_STALE_SECONDS (300)
        pilot._test_controls.set_cached(True, fetched_at=clock[0] - 301)
        pilot.refresh_market(TICKER)
        assert pilot.halted is True
        assert pilot.resting_orders() == []

    def test_env_flag_off_rejects_orders(self, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_KALSHI_PILOT_ENABLED", False)
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        result = pilot.authorize_order(TICKER, "yes", "buy", 4, 0.49)
        assert result.allowed is False
        assert result.reason == "kill_switch_env"

    def test_controls_poller_fail_closed_without_client(self, clock):
        poller = ControlsPoller(supabase_client=None,
                                time_fn=lambda: clock[0])
        poller.poll()  # no client — cache never populates
        assert poller.is_enabled() is False


# ---------------------------------------------------------------------------
# 14. Canary: graduation, max loss, oversized fill
# ---------------------------------------------------------------------------

class TestCanary:
    def test_graduation_after_clean_fills_and_min_hours(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 40, 0.49,
                                      purpose="quote_bid")
        for i in range(9):
            client.fills_script = [kfill(oid, count=4, created=clock[0],
                                         trade_id=f"c{i}")]
            pilot.poll_fills()
            clock[0] += 10
        assert pilot.canary_graduated is False
        clock[0] += 25 * 3600  # past MM_CANARY_MIN_HOURS
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     trade_id="c9")]
        pilot.poll_fills()
        assert pilot.canary_clean_fills == 10
        assert pilot.canary_graduated is True
        assert any(d.get("reason") == "CANARY PASSED"
                   for d in pilot._decisions)

    def test_canary_loss_over_ceiling_halts(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        # no_bid=0.30 -> yes_ask=0.70, so a resting bid at 0.60 does not
        # cross (finding #2's pre-submit TOCTOU guard would otherwise abort
        # this placement outright — the default book's 0.51 ask is below
        # 0.60 and this test isn't about crossing behavior).
        client = FakeKalshiClient(books={TICKER: make_book(no_bid=0.30)})
        pilot = build_pilot(clock, client=client, hedger=RecordingHedger())
        # Buy 20 @ 0.60, hedge-exit 20 @ 0.05 -> realized -$11.00 < -$10
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.60,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=20, yes_price=60,
                                     created=clock[0], trade_id="in")]
        pilot.poll_fills()
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 20, 0.05,
                                       purpose="hedge", reducing=True)
        client.fills_script = [kfill(hoid, action="sell", count=20,
                                     yes_price=5, created=clock[0],
                                     trade_id="out", is_taker=True)]
        pilot.poll_fills()
        assert pilot.inventory.realized_pnl_total() == pytest.approx(-11.0)
        assert pilot.halted is True
        assert "canary realized P&L" in pilot.halt_reason

    def test_canary_fill_oversized_is_a_deviation_halt(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 10, 0.50,
                                      purpose="quote_bid")
        # $5.00 notional > MM_CANARY_QUOTE_SIZE_USD ($2)
        client.fills_script = [kfill(oid, count=10, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "exceeds canary size" in pilot.halt_reason

    def test_taker_fill_on_resting_quote_halts(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert "taker fill" in pilot.halt_reason
        # Codex round-3 finding: the halt must not come at the cost of
        # forgetting the fill. It already happened at the exchange —
        # halting stops FUTURE activity, it must never discard what
        # already occurred. Fail-before: halt_all() + return fired BEFORE
        # any of registry/inventory/log accounting ran, so the order
        # stayed at its pre-fill count and inventory read zero even
        # though 4 real contracts had actually traded.
        assert oid not in pilot._orders  # registry: fully filled, removed
        assert pilot.inventory.net_contracts(TICKER) == 4  # inventory recorded
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(4 * 0.49)

    def test_taker_fill_on_resting_quote_records_partial_shrink(
            self, pilot_env, clock):
        """Same deviation, but a PARTIAL fill — the registry entry must
        shrink (not vanish) to reflect the remaining resting size, exactly
        like the non-deviation fill path already does. The fake client is
        set to fail cancels so halt_all()'s own pull_all() can't ALSO
        remove the order (via a successful cancel) before we can observe
        the shrink — isolating what THIS fix is responsible for from the
        pre-existing, separately-tested cancel-then-pop behavior."""
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 10, 0.49,
                                      purpose="quote_bid")
        client.cancel_fail_count = 999
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        assert oid in pilot._orders  # 10 - 4 = 6 remaining, not fully filled
        assert pilot._orders[oid]["count"] == 6
        assert pilot.inventory.net_contracts(TICKER) == 4

    def test_taker_fill_on_resting_quote_persists_state_before_halting(
            self, pilot_env, clock, tmp_path):
        """The corrected inventory must be durably persisted before the
        halt, not just held in memory — a crash immediately after halting
        must still be able to reconcile from the right numbers."""
        from mm_pilot import ControlsPoller, KalshiMMPilot, PilotStateStore
        path = tmp_path / "state.json"
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        pilot._reconciled = True
        pilot.update_selection([TICKER])
        pilot.update_book(TICKER, make_book())
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fills_script = [kfill(oid, count=4, created=clock[0],
                                     is_taker=True)]
        pilot.poll_fills()
        assert pilot.halted is True
        saved = PilotStateStore(str(path)).load()
        assert saved["inventory"]["net"].get(TICKER) == 4


# ---------------------------------------------------------------------------
# 15. Dry-run isolation: zero real client calls across a full session
# ---------------------------------------------------------------------------

class TestDryRunIsolation:
    def test_full_dry_session_never_touches_the_client(self, pilot_env, clock):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.49,
                                                           no_bid=0.49)})
        pilot = build_pilot(clock, client=client, dry_run=True)
        # Full cycle: quotes placed (dry ids), book crosses generate synthetic
        # fills, hedge fires through the proxy, everything stays local.
        placed = pilot.refresh_market(TICKER)
        assert placed, "dry-run must still produce (synthetic) order ids"
        assert all(oid.startswith("dry_") for oid in placed)
        # Crash the mid through the bid so the dry bid fills
        pilot.update_book(TICKER, make_book(yes_bid=0.10, no_bid=0.88))
        for _ in range(5):
            pilot.poll_fills()
            clock[0] += 2
        pilot.refresh_market(TICKER)
        pilot.stop()
        assert client.place_order_calls == 0
        assert client.cancel_order_calls == 0

    def test_simulated_fill_runs_the_full_pipeline(self, pilot_env, clock):
        client = FakeKalshiClient()
        hedger = RecordingHedger()
        pilot = build_pilot(clock, client=client, dry_run=True, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid.startswith("dry_")
        pilot.update_book(TICKER, make_book(yes_bid=0.10, no_bid=0.88))
        events = pilot.poll_fills()
        assert len(events) == 1
        # End to end in dry-run: inventory updated + audit rows written
        assert pilot.inventory.net_contracts(TICKER) == 4
        assert any(d.get("gate") == "hedge" for d in pilot._decisions)


# ---------------------------------------------------------------------------
# Choke-point runtime counter (test 10 companion; grep half lives in
# test_mm_pilot_gates.py)
# ---------------------------------------------------------------------------

class TestPlaceOrderCounter:
    def test_all_live_placements_flow_through_the_choke_point(self, pilot_env,
                                                              clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.refresh_market(TICKER)
        pilot.place_pilot_order(TICKER, "yes", "sell", 2, 0.49,
                                purpose="hedge", reducing=True)
        assert client.place_order_calls == pilot.place_order_calls
        assert client.place_order_calls > 0


# ---------------------------------------------------------------------------
# Finding #2: cancel confirms on the exchange before the registry pop;
# failed cancels stay in the registry, retry with backoff, and escalate to
# halt_all after MAX_CANCEL_ATTEMPTS.
# Fail-before: on the pre-fix code, _cancel_order popped the order_id from
# the registry unconditionally before even trying to cancel, so a failed
# cancel silently vanished from the registry while the order stayed live on
# the exchange (found invisible to future retries).
# ---------------------------------------------------------------------------

class TestCancelConfirmBeforePop:
    def test_failed_cancel_stays_in_registry_not_popped(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 1  # first cancel attempt fails
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        ok = pilot._cancel_order(oid)
        assert ok is False
        assert oid in pilot._orders  # NOT popped — still tracked
        assert pilot._orders[oid]["pending_cancel"] is True
        assert oid not in client.cancelled

    def test_confirmed_cancel_pops_from_registry(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        ok = pilot._cancel_order(oid)
        assert ok is True
        assert oid not in pilot._orders
        assert oid in client.cancelled

    def test_repeated_failures_escalate_to_halt_after_max_attempts(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 999  # never succeeds
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        for _ in range(3):
            pilot._cancel_order(oid)
        assert pilot.halted is True
        assert "uncancellable" in pilot.halt_reason
        # Still tracked — never silently dropped despite the halt.
        assert oid in pilot._orders

    def test_retry_pending_cancels_respects_backoff_then_succeeds(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        client.cancel_fail_count = 1  # fails once, then succeeds
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert pilot._cancel_order(oid) is False
        # Immediately retrying inside the backoff window does nothing yet.
        pilot._retry_pending_cancels()
        assert client.cancel_order_calls == 1
        assert oid in pilot._orders
        # Advance the fake clock past the 1s backoff (attempt 1 -> 2**0=1s).
        clock[0] += 2
        pilot._retry_pending_cancels()
        assert client.cancel_order_calls == 2
        assert oid not in pilot._orders  # confirmed cancel, finally popped
        assert oid in client.cancelled


# ---------------------------------------------------------------------------
# Finding #3: a fill-poll failure must fail closed — skip the refresh step,
# and after FILL_POLL_FAILURE_LIMIT consecutive failures pull all resting
# quotes and halt quoting until polling recovers.
# Fail-before: get_fills exceptions were swallowed, poll_fills returned []
# indistinguishable from "confirmed no new fills", and refresh_all kept
# quoting through an indefinite blind spell.
# ---------------------------------------------------------------------------

class TestFillPollFailClosed:
    def test_single_failure_does_not_pull_or_halt(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_fills = True
        pilot = build_pilot(clock, client=client)
        pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                purpose="quote_bid")
        events = pilot.poll_fills()
        assert events == []
        assert pilot.halted is False
        assert pilot._fill_poll_failures == 1
        assert pilot.resting_orders() != []  # not pulled yet

    def test_limit_consecutive_failures_pulls_all_and_blinds(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        client.fail_get_fills = True
        for _ in range(pilot.FILL_POLL_FAILURE_LIMIT):
            pilot.poll_fills()
        assert pilot._fills_blind is True
        assert pilot.halted is False  # blind, not a full halt — self-heals
        assert oid in client.cancelled  # pull_all fired
        placed_before = client.place_order_calls
        assert pilot.refresh_market(TICKER) == []
        assert pilot.refresh_all() == []
        assert client.place_order_calls == placed_before

    def test_recovery_clears_blind_state(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        client.fail_get_fills = True
        for _ in range(pilot.FILL_POLL_FAILURE_LIMIT):
            pilot.poll_fills()
        assert pilot._fills_blind is True
        client.fail_get_fills = False
        pilot.poll_fills()
        assert pilot._fills_blind is False
        assert pilot._fill_poll_failures == 0
        assert pilot.refresh_market(TICKER)


# ---------------------------------------------------------------------------
# Finding #4: startup reconciliation against live venue state. Quoting must
# be refused (authorize_order fails closed) until reconcile() succeeds; a
# fresh KalshiMMPilot in live mode starts unreconciled.
# Fail-before: _reconciled didn't exist / wasn't enforced anywhere, and
# _persist_state()/reconcile() were called or needed but never defined —
# a restart reseeded _last_fill_ts to "now" and inventory to zero with no
# attempt to recover real venue state.
# ---------------------------------------------------------------------------

class TestReconciliation:
    def _direct_pilot(self, clock, client, dry_run=False):
        """Construct KalshiMMPilot directly (bypassing build_pilot's
        reconciled=True convenience default) to exercise the true
        unreconciled-by-default live-mode state."""
        from mm_pilot import ControlsPoller, KalshiMMPilot
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        return KalshiMMPilot(
            kalshi_client=client,
            controls=controls,
            # See build_pilot's comment: avoid the production-default
            # min_samples=5 module singleton blocking G8 on tests that
            # aren't exercising volatility warm-up behavior.
            volatility_tracker=VolatilityTracker(min_samples=1),
            dry_run=dry_run,
            time_fn=time_fn,
            mono_fn=time_fn,
            state_path=None,  # no disk I/O in unit tests
        )

    def test_dry_run_is_reconciled_trivially(self, clock):
        pilot = self._direct_pilot(clock, client=None, dry_run=True)
        assert pilot._reconciled is True
        assert pilot.reconcile() is True

    def test_live_pilot_starts_unreconciled(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        assert pilot._reconciled is False

    def test_unreconciled_pilot_rejects_every_order(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        result = pilot.authorize_order(TICKER, "yes", "buy", 4, 0.49)
        assert result.allowed is False
        assert result.reason == "not_reconciled"

    def test_unreconciled_pilot_rejects_reducing_orders_too(self, pilot_env,
                                                            clock):
        # Reducing orders bypass inventory caps but must NOT bypass the
        # reconciliation gate — a "reduce" computed off unknown state is
        # not trustworthy either.
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        result = pilot.authorize_order(TICKER, "yes", "sell", 4, 0.49,
                                       reducing=True)
        assert result.allowed is False
        assert result.reason == "not_reconciled"

    def test_evaluate_gates_pulls_with_not_reconciled_reason(self, pilot_env,
                                                             clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        pilot.update_selection([TICKER])
        plan = pilot._evaluate_gates(TICKER)
        assert plan["action"] == "pull"
        assert plan["reason"] == "not_reconciled"

    def test_no_client_fails_closed(self, pilot_env, clock):
        pilot = self._direct_pilot(clock, client=None)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_positions_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_positions = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_fills_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_fills = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_open_orders_query_failure_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.fail_get_open_orders = True
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_uncancellable_stale_order_fails_closed(self, pilot_env, clock):
        client = FakeKalshiClient()
        client.open_orders_script = [{"order_id": "stale_1", "ticker": TICKER}]
        client.cancel_fail_count = 999
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False

    def test_success_seeds_inventory_and_cancels_stale_orders(self, pilot_env,
                                                               clock):
        # Field names match Kalshi's documented MarketPosition schema
        # (docs.kalshi.com/api-reference/portfolio/get-positions):
        # position_fp is the signed net-contracts STRING (not "position" /
        # "net_contracts"), and there is no average-price field at all —
        # avg cost is derived from market_exposure_dollars / |position_fp|.
        # A CodeRabbit round-3 finding caught the original test (and the
        # reconcile() code it was validating) guessing at the wrong keys,
        # which would have silently reconciled every real position to zero.
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "12",
             "market_exposure_dollars": 5.28},  # 12 contracts @ $0.44 avg
        ]
        client.open_orders_script = [
            {"order_id": "stale_1", "ticker": TICKER},
            {"order_id": "stale_2", "ticker": TICKER},
        ]
        pilot = self._direct_pilot(clock, client)
        ok = pilot.reconcile()
        assert ok is True
        assert pilot._reconciled is True
        assert pilot.inventory.net_contracts(TICKER) == 12
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(0.44)
        assert set(client.cancelled) == {"stale_1", "stale_2"}
        assert pilot._orders == {}  # nothing adopted — clean slate

    def test_negative_position_fp_means_no_contracts(self, pilot_env, clock):
        """Per the documented schema: negative position_fp = NO contracts
        (mm_pilot's inventory convention: long NO is a negative signed
        count too — signs line up directly, no inversion needed)."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "-7",
             "market_exposure_dollars": 3.5},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.net_contracts(TICKER) == -7
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(0.5)

    def test_unparsable_position_fp_fails_closed(self, pilot_env, clock):
        """Malformed venue positions cannot be treated as zero exposure."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "not-a-number"},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is False
        assert pilot._reconciled is False
        assert pilot.inventory.net_contracts(TICKER) == 0

    def test_cancels_and_confirms_before_position_and_fill_snapshot(
            self, pilot_env, clock):
        calls: list[str] = []

        class OrderedClient(FakeKalshiClient):
            def get_open_orders(self, ticker=None, **kwargs):
                calls.append("open_orders")
                return super().get_open_orders(ticker=ticker, **kwargs)

            def cancel_order(self, order_id):
                calls.append("cancel")
                return super().cancel_order(order_id)

            def get_positions(self, raise_on_error=False):
                calls.append("positions")
                return super().get_positions(raise_on_error=raise_on_error)

            def get_fills(self, min_ts=None, raise_on_error=False, **kwargs):
                calls.append("fills")
                return super().get_fills(
                    min_ts=min_ts, raise_on_error=raise_on_error, **kwargs)

        client = OrderedClient()
        client.open_orders_script = [{"order_id": "stale_1", "ticker": TICKER}]
        pilot = self._direct_pilot(clock, client)

        assert pilot.reconcile() is True
        assert calls == ["open_orders", "cancel", "open_orders",
                         "positions", "fills"]

    def test_missing_exposure_field_assumes_worst_case_not_zero(self,
                                                                 pilot_env,
                                                                 clock):
        """CodeRabbit round-3: a 0.0 avg-cost fallback would make
        net_usd() = net * avg read as $0 regardless of contract count,
        silently bypassing every USD-denominated cap for a ticker
        reconciliation just discovered real inventory on. A missing/
        unparsable exposure field must assume the worst case ($1.00 —
        the max possible price per contract) so caps trip early instead
        of being bypassed."""
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "10"},  # no exposure field
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.net_contracts(TICKER) == 10
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(1.0)
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(10.0)

    def test_unparsable_exposure_field_also_assumes_worst_case(self,
                                                                pilot_env,
                                                                clock):
        client = FakeKalshiClient()
        client.positions_script = [
            {"ticker": TICKER, "position_fp": "10",
             "market_exposure_dollars": "not-a-number"},
        ]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert pilot.inventory.avg_cost(TICKER) == pytest.approx(1.0)

    def test_success_marks_fills_seen_and_advances_cursor(self, pilot_env,
                                                           clock):
        client = FakeKalshiClient()
        client.fills_script = [kfill("k_old", count=3, yes_price=40,
                                     trade_id="tr_old", created=clock[0] - 5000)]
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        assert "tr_old" in pilot._seen_fill_ids
        assert pilot._last_fill_ts == pytest.approx(clock[0] - 5000)

    def test_reconciled_pilot_can_then_place_orders(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        assert pilot.reconcile() is True
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert oid is not None

    def test_run_loop_reconciles_before_quoting(self, pilot_env, clock,
                                                monkeypatch):
        """Integration: run_loop must call reconcile() before its first
        refresh/poll cycle, and must not place orders while unreconciled."""
        import threading
        client = FakeKalshiClient()
        pilot = self._direct_pilot(clock, client)
        pilot.update_selection([TICKER])
        stop = threading.Event()
        assert pilot._reconciled is False

        calls = []
        reconcile = pilot.reconcile
        refresh_all = pilot.refresh_all

        def recording_reconcile():
            calls.append("reconcile")
            return reconcile()

        def recording_refresh():
            calls.append("refresh")
            placed = refresh_all()
            stop.set()
            return placed

        def no_op_stop():
            calls.append("stop")

        monkeypatch.setattr(pilot, "reconcile", recording_reconcile)
        monkeypatch.setattr(pilot, "refresh_all", recording_refresh)
        monkeypatch.setattr(pilot, "stop", no_op_stop)

        pilot.run_loop(stop)

        assert pilot._reconciled is True
        assert calls[:2] == ["reconcile", "refresh"]
        assert pilot.resting_orders()  # placed only after reconciliation


# ---------------------------------------------------------------------------
# Finding #5: hedge reduce orders must never be sized past the actual
# current position — hedger.hedge_inventory/_hedge_kalshi accept
# max_contracts and clamp; the pilot passes its own tracked position size.
# Fail-before: count = max(1, int(size / touch)) had no ceiling, so a large
# dollar excess against a stale/moved touch price could compute a count
# exceeding actual holdings, flipping a "reduce" into the opposite side
# while reducing=True bypassed inventory caps entirely.
# ---------------------------------------------------------------------------

class TestHedgeSizeClamp:
    def test_hedge_on_fill_passes_actual_position_as_max_contracts(
            self, pilot_env, clock, monkeypatch):
        # Canary quote-size cap (default $2) would otherwise halt on a $10
        # fill before the hedge decision even runs — bump it out of the way
        # like TestHedgeLatency does, since canary sizing isn't what this
        # test is about.
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        hedger = RecordingHedger(result=True)
        pilot = build_pilot(clock, client=client, hedger=hedger)
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # 20 contracts @ 0.50 = $10 net; well past the deadband -> hedges.
        client.fills_script = [kfill(oid, count=20, yes_price=50)]
        pilot.poll_fills()
        assert hedger.calls  # hedge fired
        call = hedger.calls[-1]
        assert call["max_contracts"] == 20  # abs(net_contracts), not a guess


class TestHedgeKalshiContractClamp:
    """Direct hedger.py unit coverage for the clamp itself (complements the
    mm_pilot integration test above, which only proves wiring)."""

    @staticmethod
    def _book(yes_bid=0.30, no_bid=0.68):
        return {"orderbook_fp": {
            "yes_dollars": [[f"{yes_bid:.4f}", "500.00"]],
            "no_dollars": [[f"{no_bid:.4f}", "500.00"]],
        }}

    def test_count_clamped_to_max_contracts_when_price_moved(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        mock_kalshi.place_order.return_value = {"order_id": "k_1"}
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        # size=$20 excess / touch=$0.30 -> naive count=66; actual position
        # is only 5 contracts (price moved a long way since entry).
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell",
                                      max_contracts=5)
        assert result is True
        call = mock_kalshi.place_order.call_args
        assert call[1]["count"] == 5  # clamped, never the naive 66

    def test_zero_max_contracts_places_nothing(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell",
                                      max_contracts=0)
        assert result is False
        mock_kalshi.place_order.assert_not_called()

    def test_none_max_contracts_preserves_unclamped_legacy_behavior(self):
        from hedger import PartialFillHedger
        mock_kalshi = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        mock_kalshi.fetch_order_book.return_value = self._book()
        mock_kalshi.place_order.return_value = {"order_id": "k_1"}
        hedger = PartialFillHedger(kalshi_client=mock_kalshi)
        result = hedger._hedge_kalshi("TICK", fill_price=0.50, size=20.0,
                                      max_loss=1.0, side="yes", action="sell")
        assert result is True
        call = mock_kalshi.place_order.call_args
        assert call[1]["count"] == 66  # int(20 / 0.30), unclamped


# ---------------------------------------------------------------------------
# Finding #10: hedge-latency and hedge-order aging use the monotonic clock,
# not wall time — a frozen/mocked _time_fn (or a real NTP step) must not
# defeat or false-trigger MM_HEDGE_MAX_LATENCY_SECONDS.
# Fail-before: latency = self._time_fn() - event.created_ts, and
# _check_pending_hedges aged orders off wall-clock `placed_at`.
# ---------------------------------------------------------------------------

class TestMonotonicHedgeLatency:
    def test_frozen_wall_clock_does_not_hide_processing_latency(
            self, pilot_env, clock, monkeypatch):
        """If _time_fn() were still used for the reaction-time component, a
        wall clock that never advances during hedge attempts would always
        read latency=0 no matter how much monotonic time passed.

        ``mono`` is an independent counter the fake hedger advances itself
        (rather than counting _mono_fn() invocations, which is fragile —
        place_pilot_order also reads the monotonic clock for its own
        ``placed_mono`` bookkeeping). This ties the simulated 15s directly
        to "the hedge attempt took 15 real seconds", which is exactly the
        scenario the monotonic-latency fix must catch.
        """
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 100.0)
        client = FakeKalshiClient()
        mono = [clock[0]]  # independent monotonic counter

        class SlowHedger:
            def hedge_inventory(self, **kwargs):
                # The hedge attempt itself burns 15s of monotonic time
                # while the wall clock (clock[0]) never moves at all.
                mono[0] += 15
                return True

        pilot = build_pilot(clock, client=client, hedger=SlowHedger())
        monkeypatch.setattr(pilot, "_mono_fn", lambda: mono[0])
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 20, 0.50,
                                      purpose="quote_bid")
        # Fill reported as happening right now — zero wall-clock detection
        # lag, isolating the assertion to the monotonic reaction-time term.
        client.fills_script = [kfill(oid, count=20, yes_price=50,
                                     created=clock[0])]
        pilot.poll_fills()
        # 15s of monotonic reaction latency exceeds the 10s ceiling even
        # though the wall clock never moved and the fill was "fresh".
        assert pilot.halted is True
        assert "latency" in pilot.halt_reason

    def test_pending_hedge_ages_on_monotonic_placed_time(self, pilot_env,
                                                          clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 10, 0.49)
        hoid = pilot.place_pilot_order(TICKER, "yes", "sell", 10, 0.49,
                                       purpose="hedge", reducing=True)
        assert pilot._orders[hoid]["placed_mono"] == pytest.approx(clock[0])
        clock[0] += 11  # advances both time_fn and mono_fn (shared fixture)
        pilot.poll_fills()
        assert pilot.halted is True
        assert hoid in client.cancelled


# ---------------------------------------------------------------------------
# Finding #4 support: PilotStateStore / _persist_state on a REAL file.
# The reconciliation tests above use state_path=None throughout (no disk
# I/O) and cover reconcile()'s logic against a mocked venue; these cover
# the persistence mechanism itself, which reconcile() falls back on for
# last_fill_ts / realized P&L when a live query can't supply it.
# Fail-before: _persist_state() was called from four call sites in the
# uncommitted diff but was never defined (AttributeError at runtime on
# every cancel/fill/order-placement in live mode).
# ---------------------------------------------------------------------------

class TestPilotStatePersistence:
    def test_save_then_load_roundtrip(self, tmp_path):
        from mm_pilot import PilotStateStore
        path = str(tmp_path / "state.json")
        store = PilotStateStore(path)
        store.save({"last_fill_ts": 123.0, "orders": {"o1": {"ticker": TICKER}}})
        loaded = store.load()
        assert loaded == {"last_fill_ts": 123.0, "orders": {"o1": {"ticker": TICKER}}}

    def test_load_missing_file_returns_none(self, tmp_path):
        from mm_pilot import PilotStateStore
        store = PilotStateStore(str(tmp_path / "does_not_exist.json"))
        assert store.load() is None

    def test_load_corrupted_file_raises(self, tmp_path):
        from mm_pilot import PilotStateStore
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        store = PilotStateStore(str(path))
        with pytest.raises(json.JSONDecodeError):
            store.load()

    def test_reconcile_tolerates_corrupted_persisted_file(self, pilot_env,
                                                           clock, tmp_path):
        """A corrupted local cache must not block reconciliation as long as
        the live venue queries still succeed — the file is a fallback, not
        the authority."""
        from mm_pilot import ControlsPoller, KalshiMMPilot
        path = tmp_path / "state.json"
        path.write_text("{not valid json")
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        assert pilot.reconcile() is True

    def test_place_cancel_and_fill_persist_real_state_to_disk(
            self, pilot_env, clock, tmp_path):
        """End-to-end: a live (non-None state_path) pilot actually writes a
        loadable state file across the placement/cancel/fill lifecycle,
        proving _persist_state's four call sites are wired to a real,
        working implementation (not just silently no-op'd)."""
        from mm_pilot import ControlsPoller, KalshiMMPilot, PilotStateStore
        path = tmp_path / "state.json"
        def time_fn():
            return clock[0]
        controls = ControlsPoller(time_fn=time_fn)
        controls.set_cached(True)
        client = FakeKalshiClient()
        pilot = KalshiMMPilot(
            kalshi_client=client, controls=controls,
            volatility_tracker=VolatilityTracker(min_samples=1),
            time_fn=time_fn, mono_fn=time_fn, state_path=str(path),
            dry_run=False,
        )
        pilot._reconciled = True  # bypass live reconciliation for this test
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49,
                                      purpose="quote_bid")
        assert path.exists()
        state = PilotStateStore(str(path)).load()
        assert oid in state["orders"]

        pilot._cancel_order(oid)
        state = PilotStateStore(str(path)).load()
        assert oid not in state["orders"]

        fill_oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        assert fill_oid is not None
        client.fills_script = [kfill(
            fill_oid, count=4, created=clock[0], trade_id="persisted_fill")]
        events = pilot.poll_fills()
        assert len(events) == 1
        state = PilotStateStore(str(path)).load()
        assert state["inventory"]["net"][TICKER] == 4
        assert state["last_fill_ts"] == pytest.approx(clock[0])
        assert fill_oid not in state["orders"]


# ---------------------------------------------------------------------------
# CodeRabbit round-3 finding: place_pilot_order must not hold self._lock for
# the duration of the live venue round-trip — a different thread (the WS
# feed handler calling on_ws_price) touches disjoint state (self._books)
# and must not be blocked for as long as a slow/stuck placement call takes.
# threading.RLock is reentrant, so a same-thread nested call can't detect
# this regression — the test needs a genuine second thread.
# Fail-before: the entire method (auth through registry write) ran inside
# one `with self._lock:` block, including the network call itself.
# ---------------------------------------------------------------------------

class TestLockNotHeldDuringNetworkCall:
    def test_on_ws_price_proceeds_while_placement_network_call_in_flight(
            self, pilot_env, clock):
        import threading

        entered_network_call = threading.Event()
        release_network_call = threading.Event()

        class SlowClient(FakeKalshiClient):
            def place_order(self, *a, **kw):
                entered_network_call.set()
                # Blocks here until the test explicitly releases it —
                # simulates a slow/stuck venue round-trip.
                release_network_call.wait(timeout=5)
                return super().place_order(*a, **kw)

        slow_client = SlowClient()
        pilot = build_pilot(clock, client=slow_client)

        result: dict = {}

        def placer():
            result["oid"] = pilot.place_pilot_order(
                TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        placing_thread = threading.Thread(target=placer)
        placing_thread.start()
        try:
            assert entered_network_call.wait(timeout=2), (
                "placement never reached the (fake) network call")

            # While that "network call" is in flight on the other thread,
            # on_ws_price must be able to proceed without waiting on
            # self._lock — if the fix regressed, this blocks until the
            # network call above is released (5s), and the timeout below
            # fires first.
            ws_updated = threading.Event()

            def ws_updater():
                pilot.on_ws_price(TICKER, 0.55)
                ws_updated.set()

            ws_thread = threading.Thread(target=ws_updater)
            ws_thread.start()
            try:
                assert ws_updated.wait(timeout=1), (
                    "on_ws_price blocked while place_pilot_order's network "
                    "call was in flight — the lock is still held for the "
                    "duration of the venue round-trip")
            finally:
                ws_thread.join(timeout=5)
        finally:
            release_network_call.set()
            placing_thread.join(timeout=5)

        assert result["oid"] is not None
        assert pilot._book(TICKER)["mid"] == pytest.approx(0.55)


# ---------------------------------------------------------------------------
# CodeRabbit round-4 safety regressions
# ---------------------------------------------------------------------------

class TestInventoryDerivedReduction:
    def test_reducing_flag_without_inventory_is_rejected(self, pilot_env,
                                                          clock):
        pilot = build_pilot(clock, client=FakeKalshiClient())

        result = pilot.authorize_order(
            TICKER, "yes", "sell", 5, 0.49, reducing=True)

        assert result.allowed is False
        assert result.reason == "not_reducing"

    def test_reducing_order_is_capped_at_held_contracts(self, pilot_env,
                                                        clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 5, 0.49)

        oid = pilot.place_pilot_order(
            TICKER, "yes", "sell", 50, 0.49,
            purpose="hedge", reducing=True,
        )

        assert oid is not None
        assert client.placed[-1]["count"] == 5
        assert pilot._orders[oid]["count"] == 5

    def test_derived_reduction_reaches_client_policy_boundary(self, pilot_env,
                                                               clock):
        blocked_ticker = "KXNFLGAME-26AUG18"
        client = FakeKalshiClient(books={blocked_ticker: make_book()})
        pilot = build_pilot(clock, client=client)
        pilot.inventory.apply_fill(blocked_ticker, "yes", "buy", 5, 0.49)

        oid = pilot.place_pilot_order(
            blocked_ticker, "yes", "sell", 5, 0.49,
            purpose="hedge",
        )

        assert oid is not None
        assert client.placed[-1]["reducing"] is True


class TestIndeterminatePlacement:
    @pytest.mark.parametrize("response", [None, {}, {"order": {}}])
    def test_missing_order_identity_halts_and_requires_reconciliation(
            self, pilot_env, clock, response, monkeypatch):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        monkeypatch.setattr(client, "place_order", lambda **kwargs: response)

        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        assert oid is None
        assert pilot.halted is True
        assert pilot._reconciled is False
        assert "indeterminate order placement" in pilot.halt_reason

    def test_placement_exception_takes_same_fail_closed_path(
            self, pilot_env, clock, monkeypatch):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)

        def raise_timeout(**kwargs):
            raise TimeoutError("venue response lost")

        monkeypatch.setattr(client, "place_order", raise_timeout)

        assert pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49,
            purpose="quote_bid") is None
        assert pilot.halted is True
        assert pilot._reconciled is False


class TestCancelReplaceSafety:
    def test_failed_cancel_prevents_replacement_quotes(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        old_oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        placements_before = client.place_order_calls
        client.cancel_fail_count = 1

        assert pilot.refresh_market(TICKER) == []
        assert client.place_order_calls == placements_before
        assert old_oid in pilot._orders
        assert pilot._orders[old_oid]["pending_cancel"] is True


class TestHaltedFillAccounting:
    def test_known_fill_is_accounted_while_halted(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.cancel_fail_count = 99
        pilot.halt_all("test halt with live order")
        client.fills_script = [kfill(
            oid, count=4, yes_price=49, trade_id="halted_fill",
            created=clock[0],
        )]

        events = pilot.poll_fills()

        assert [event.fill_id for event in events] == ["halted_fill"]
        assert pilot.inventory.net_contracts(TICKER) == 4

    def test_unparseable_known_fill_requires_reconciliation(
            self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        bad_fill = kfill(oid, trade_id="bad_fill", created=clock[0])
        bad_fill.pop("yes_price")
        client.fills_script = [bad_fill]

        assert pilot.poll_fills() == []
        assert pilot.halted is True
        assert pilot._reconciled is False
        assert "bad_fill" not in pilot._seen_fill_ids
        assert pilot.inventory.net_contracts(TICKER) == 0


class TestToxicityFailClosed:
    def test_record_failure_halts_affected_market(self, pilot_env, clock):
        class RaisingToxic(ToxicFlowDetector):
            def record_fill(self, *args, **kwargs):
                raise RuntimeError("toxicity store unavailable")

        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, detector=RaisingToxic())
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.fills_script = [kfill(oid, created=clock[0])]

        pilot.poll_fills()

        assert TICKER in pilot._market_halted
        assert "toxicity accounting failed" in pilot._market_halted[TICKER]

    def test_refresh_market_fails_closed_when_toxicity_lookup_raises(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_TOXIC_FLOW_ENABLED", True)

        class RaisingMultiplierToxic(ToxicFlowDetector):
            def get_spread_multiplier(self, *args, **kwargs):
                raise RuntimeError("spread multiplier lookup crashed")

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, detector=RaisingMultiplierToxic(), selection=[TICKER])
        oid = pilot.place_pilot_order(TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")

        placed = pilot.refresh_market(TICKER)
        assert placed == []
        assert oid in client.cancelled
        assert pilot.resting_orders(TICKER) == []

    def test_sized_count_preserves_minimum_quote_floor(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_TOXIC_FLOW_ENABLED", True)
        detector = ToxicFlowDetector(min_size_fraction=0.2, size_taper_factor=0.8)
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=10.0, no_qty=10.0)})
        pilot = build_pilot(clock, client=client, detector=detector, selection=[TICKER])
        # 2 adverse fills and 3 favorable fills -> toxicity = 0.40 (< 0.60 pause threshold)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.60)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.60)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.60)
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        orders = pilot.resting_orders(TICKER)
        assert all(o["count"] >= 1 for o in orders)


class TestShutdownCancellation:
    def test_stop_without_client_confirms_when_no_orders_remain(self, clock):
        pilot = build_pilot(clock, client=None, dry_run=False)
        pilot._alert = MagicMock()

        pilot.stop()

        pilot._alert.assert_not_called()

    def test_stop_retries_until_venue_confirms_no_orders(self, pilot_env,
                                                         clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid")
        client.open_orders_script = [{"order_id": oid, "ticker": TICKER}]
        client.cancel_fail_count = 2

        pilot.stop()

        assert client.cancel_order_calls >= 3
        assert client.get_open_orders() == []
        assert pilot.resting_orders() == []


class TestMMPilotStatusTelemetry:
    """Test get_status() and _persist_state() extended telemetry snapshots."""

    def test_get_status_contains_all_extended_fields(self, pilot_env, clock):
        pilot = build_pilot(clock, selection=None)
        status = pilot.get_status()

        # Core status
        assert "active" in status
        assert status["active"] is True
        assert status["status"] == "active"
        assert status["stopped"] is False
        assert status["halted"] is False
        assert status["halt_reason"] == ""
        assert status["markets_halted"] == {}
        # Canary
        assert status["canary_graduated"] is False
        assert status["canary_clean_fills"] == 0
        assert status["canary_target_fills"] == live_config().MM_CANARY_FILLS
        # Orders and inventory
        assert status["resting_orders"] == 0
        assert status["orders"] == []
        assert status["selected_markets"] == []
        assert status["total_inventory_usd"] == 0.0
        assert status["realized_pnl"] == 0.0
        assert status["inventory"] == {"net": {}, "avg": {}, "realized": {}}
        # Safety & control
        assert "reconciled" in status
        assert "fills_blind" in status
        assert "kill_switch_enabled" in status
        assert "loop_error_streak" in status
        assert "last_fill_ts" in status

    def test_get_status_with_resting_orders_and_inventory(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client)
        pilot.update_selection([TICKER])

        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid"
        )
        assert oid

        # Apply a fill to generate inventory
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 4, 0.49)

        status = pilot.get_status()
        assert status["resting_orders"] == 1
        assert len(status["orders"]) == 1
        assert status["orders"][0]["order_id"] == oid
        assert status["selected_markets"] == [TICKER]
        assert status["total_inventory_usd"] > 0
        assert TICKER in status["inventory"]["net"]
        assert status["inventory"]["net"][TICKER] == 4
        assert "toxicity" in status
        assert TICKER in status["toxicity"]
        tox_item = status["toxicity"][TICKER]
        assert "score" in tox_item
        assert "spread_multiplier" in tox_item
        assert "size_multiplier" in tox_item
        assert "paused" in tox_item
        assert "pause_remaining" in tox_item
        assert "fill_velocity" in tox_item
        assert "is_burst" in tox_item

    def test_persist_state_includes_extended_telemetry(self, pilot_env, clock, tmp_path):
        client = FakeKalshiClient()
        state_file = tmp_path / "mm_state.json"
        pilot = build_pilot(clock, client=client, state_path=str(state_file))
        pilot.update_selection([TICKER])
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid"
        )
        assert oid

        pilot._persist_state()

        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert saved["active"] is True
        assert saved["status"] == "active"
        assert saved["stopped"] is False
        assert saved["halted"] is False
        assert saved["canary_target_fills"] == live_config().MM_CANARY_FILLS
        assert saved["selected_markets"] == [TICKER]
        assert oid in saved["orders"]
        assert "inventory" in saved
        assert "kill_switch_enabled" in saved
        assert "toxicity" in saved
        assert TICKER in saved["toxicity"]
        assert "spread_multiplier" in saved["toxicity"][TICKER]
        assert "saved_at" in saved

    def test_stop_persists_stopped_state(self, pilot_env, clock, tmp_path):
        """Calling stop() persists stopped=True, active=False, and status='stopped'."""
        client = FakeKalshiClient()
        state_file = tmp_path / "mm_state.json"
        pilot = build_pilot(clock, client=client, state_path=str(state_file))
        pilot.update_selection([TICKER])
        oid = pilot.place_pilot_order(
            TICKER, "yes", "buy", 4, 0.49, purpose="quote_bid"
        )
        assert oid

        # Before stop
        assert pilot.get_status()["active"] is True
        assert pilot.get_status()["stopped"] is False

        # Stop pilot
        pilot.stop()

        status = pilot.get_status()
        assert status["active"] is False
        assert status["stopped"] is True
        assert status["status"] == "stopped"

        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert saved["active"] is False
        assert saved["stopped"] is True
        assert saved["status"] == "stopped"

    def test_canary_target_fills_reflects_config(self, pilot_env, clock, monkeypatch, tmp_path):
        """Telemetry reflects the configured MM_CANARY_FILLS threshold."""
        monkeypatch.setattr(live_config(), "MM_CANARY_FILLS", 75)
        state_file = tmp_path / "mm_state.json"
        pilot = build_pilot(clock, state_path=str(state_file))

        status = pilot.get_status()
        assert status["canary_target_fills"] == 75

        pilot._persist_state()
        saved = json.loads(state_file.read_text())
        assert saved["canary_target_fills"] == 75


class TestMMPilotLIPYieldTracker:
    def test_update_selection_registers_lip_program_metadata(self, pilot_env, clock):
        pilot = build_pilot(clock)
        items = [
            {
                "ticker": TICKER,
                "pool_dollars": 5000.0,
                "category": "Economics",
                "discount_factor_bps": 9500,
                "program_end": "2026-10-01T00:00:00Z",
            }
        ]
        pilot.update_selection(items)
        assert pilot._selected == {TICKER}
        prog = pilot._lip_tracker._programs.get(TICKER)
        assert prog is not None
        assert prog["pool_dollars"] == 5000.0
        assert prog["discount_factor"] == 0.95
        assert prog["category"] == "Economics"

    def test_refresh_market_records_lip_snapshot(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot._lip_tracker.set_market_program(TICKER, pool_dollars=1000.0)

        placed = pilot.refresh_market(TICKER)
        assert len(placed) >= 1
        stat = pilot._lip_tracker._stats.get(TICKER)
        assert stat is not None
        assert stat["snapshots_count"] == 1
        assert stat["last_our_score"] > 0
        assert stat["last_qualifying_share"] > 0

    def test_get_status_contains_lip_rewards_and_blended_apr(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot._lip_tracker.set_market_program(TICKER, pool_dollars=1000.0)

        pilot.refresh_market(TICKER)
        status = pilot.get_status()
        assert "lip_rewards" in status
        lip = status["lip_rewards"]
        assert "total_estimated_reward_usd" in lip
        assert "estimated_daily_rate_usd" in lip
        assert "blended_apr_pct" in lip
        assert "by_ticker" in lip
        assert TICKER in lip["by_ticker"]
        ticker_stat = lip["by_ticker"][TICKER]
        assert ticker_stat["pool_dollars"] == 1000.0
        assert ticker_stat["qualifying_share_pct"] > 0
        assert ticker_stat["daily_rate_usd"] > 0

    def test_lip_tracker_state_persistence_and_restore(self, pilot_env, clock, tmp_path):
        state_file = tmp_path / "mm_state_lip.json"
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, state_path=str(state_file), selection=[TICKER])
        pilot._lip_tracker.set_market_program(TICKER, pool_dollars=2500.0, discount_factor=0.92)
        pilot.refresh_market(TICKER)
        pilot._persist_state()

        assert state_file.exists()
        saved = json.loads(state_file.read_text())
        assert "lip_tracker" in saved
        assert TICKER in saved["lip_tracker"]["programs"]
        assert saved["lip_tracker"]["programs"][TICKER]["pool_dollars"] == 2500.0
        assert "lip_rewards" in saved
        assert saved["lip_rewards"]["by_ticker"][TICKER]["pool_dollars"] == 2500.0

        # Restart simulation
        pilot2 = build_pilot(clock, client=client, state_path=str(state_file))
        assert TICKER in pilot2._lip_tracker._programs
        prog2 = pilot2._lip_tracker._programs[TICKER]
        assert prog2["pool_dollars"] == 2500.0
        assert prog2["discount_factor"] == 0.92
        assert pilot2._lip_tracker._stats[TICKER]["snapshots_count"] == 1

    def test_reconcile_does_not_overwrite_lip_tracker(self, pilot_env, clock, tmp_path):
        state_file = tmp_path / "mm_state_lip_reconcile.json"
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, state_path=str(state_file), selection=[TICKER])
        pilot._lip_tracker.set_market_program(TICKER, pool_dollars=1000.0)
        pilot.refresh_market(TICKER)
        pilot._persist_state()

        # Simulate newer snapshot accrued in memory
        pilot._lip_tracker.record_snapshot(
            TICKER,
            [{"purpose": "quote_bid", "side": "yes", "action": "buy", "price": 0.50, "count": 50}],
            None,
            now=200.0,
        )
        assert pilot._lip_tracker._stats[TICKER]["snapshots_count"] == 2

        # Reconcile again — must not reload/overwrite newer LIP tracker state
        pilot.reconcile()
        assert pilot._lip_tracker._stats[TICKER]["snapshots_count"] == 2


class TestMMPilotAdverseSelection:
    """Test dynamic adverse selection spread widening and sizing taper in pilot."""

    def test_adverse_selection_tuning_widens_spread_and_tapers_size(self, pilot_env, clock, monkeypatch):
        monkeypatch.setattr(live_config(), "MM_TOXIC_FLOW_ENABLED", True)
        monkeypatch.setattr(live_config(), "MM_CANARY_QUOTE_SIZE_USD", 50.0)

        detector = ToxicFlowDetector(
            decay_half_life_seconds=60.0,
            toxicity_spread_factor=1.5,
            size_taper_factor=0.6,
            min_size_fraction=0.2,
            fill_velocity_window_seconds=30.0,
            fill_velocity_burst_threshold=3,
        )
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=100.0, no_qty=100.0)})
        pilot = build_pilot(clock, client=client, detector=detector, selection=[TICKER])

        # Step 1: Baseline refresh with clean book
        placed_baseline = pilot.refresh_market(TICKER)
        assert len(placed_baseline) > 0
        baseline_orders = {o["order_id"]: o for o in pilot.resting_orders(TICKER)}
        bid_order = next(o for o in baseline_orders.values() if o["purpose"] == "quote_bid")
        ask_order = next(o for o in baseline_orders.values() if o["purpose"] == "quote_ask")
        baseline_spread = (1.0 - ask_order["price"]) - bid_order["price"]
        baseline_count = bid_order["count"]

        # Step 2: Feed 3 adverse fills spaced 40s apart (so no burst yet, but toxicity = 1.0)
        t0 = clock[0]
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40, timestamp=t0 - 80.0)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40, timestamp=t0 - 40.0)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40, timestamp=t0)

        assert detector.get_toxicity(TICKER, now=t0) > 0.0
        assert detector.is_velocity_burst(TICKER, now=t0) is False

        # Refresh quotes under elevated toxicity
        placed_toxic = pilot.refresh_market(TICKER)
        assert len(placed_toxic) > 0
        toxic_orders = {o["order_id"]: o for o in pilot.resting_orders(TICKER)}
        tox_bid = next(o for o in toxic_orders.values() if o["purpose"] == "quote_bid")
        tox_ask = next(o for o in toxic_orders.values() if o["purpose"] == "quote_ask")
        toxic_spread = (1.0 - tox_ask["price"]) - tox_bid["price"]

        # Spread must be strictly wider and/or count must be smaller
        assert toxic_spread >= baseline_spread
        assert tox_bid["count"] <= baseline_count

        # Step 3: Trigger burst velocity by adding 2 more fills within 2 seconds
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40, timestamp=t0 + 1.0)
        detector.record_fill(TICKER, "bid", 0.50, 10.0, 0.40, timestamp=t0 + 2.0)
        clock[0] = t0 + 3.0
        assert detector.is_velocity_burst(TICKER, now=clock[0]) is True

        placed_burst = pilot.refresh_market(TICKER)
        assert len(placed_burst) > 0
        burst_orders = {o["order_id"]: o for o in pilot.resting_orders(TICKER)}
        burst_bid = next(o for o in burst_orders.values() if o["purpose"] == "quote_bid")
        burst_ask = next(o for o in burst_orders.values() if o["purpose"] == "quote_ask")
        burst_spread = (1.0 - burst_ask["price"]) - burst_bid["price"]

        assert burst_spread >= toxic_spread
        assert burst_bid["count"] <= tox_bid["count"]


class TestMMPilotLIPBalancer:
    """Test dynamic LIP target size balancer integration in mm_pilot."""

    def test_refresh_market_records_balancer_decision(self, pilot_env, clock):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=100.0, no_qty=100.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 500.0,
            "target_size": 1000.0,
            "discount_factor_bps": 9500,
        }])
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        decisions = [d for d in pilot._decisions if d.get("gate") == "G11b_lip_target_balancer"]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["decision"] == "pass"
        assert "bid_size=" in d["reason"]
        assert "target=1000" in d["reason"]
        assert "headroom=" in d["reason"]

    def test_canary_mode_does_not_scale_up_quote_size(self, pilot_env, clock, monkeypatch):
        # Canary quote size is $2.00, at price 0.48 -> base_count = 4 contracts
        # Even with target_size = 2000, canary mode must not scale up past canary base size
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=1000.0, no_qty=1000.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 1000.0,
            "target_size": 2000.0,
            "discount_factor_bps": 9800,
        }])
        assert pilot.canary_graduated is False
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # Canary size: count should be <= 4 (not scaled up to 25% of 2000 = 500)
        assert orders["quote_bid"]["count"] <= 4

    def test_graduated_mode_scales_up_quote_size_with_high_target(self, pilot_env, clock, monkeypatch):
        # Graduate the canary
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=1000.0, no_qty=1000.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 2000.0,
            "target_size": 2000.0,
            "discount_factor_bps": 9800,
        }])
        pilot.canary_graduated = True

        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # With target_size = 2000, max_share = 0.25 -> qualifying_cap = 500
        # depth_cap = 0.25 * 1000 = 250
        # inventory_headroom = (100 / 0.48) = 208 contracts
        # Sizing should scale up to ~208 (inventory headroom), far above standard $10 notional (20 contracts)
        assert orders["quote_bid"]["count"] > 50

    def test_low_target_market_scales_down_quote_size(self, pilot_env, clock, monkeypatch):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=100.0, no_qty=100.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        # target_size = 100, max_share = 0.05 -> qualifying_cap = 5 contracts
        monkeypatch.setattr(live_config(), "MM_LIP_BALANCER_MAX_SHARE", 0.05)
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 50.0,
            "target_size": 100.0,
            "discount_factor_bps": 9500,
        }])
        pilot.canary_graduated = True

        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # Standard $10 notional / 0.48 = 20 contracts.
        # But target_size * 0.05 = 5 contracts -> scales down to 5 contracts!
        assert orders["quote_bid"]["count"] == 5

    def test_status_and_persisted_state_contain_lip_balancer_telemetry(self, pilot_env, clock, tmp_path):
        from mm_pilot import PilotStateStore
        state_file = str(tmp_path / "pilot_state.json")
        store = PilotStateStore(state_file)
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=100.0, no_qty=100.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], state_path=state_file)
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 100.0,
            "target_size": 500.0,
        }])

        # get_status
        status = pilot.get_status()
        assert "lip_balancer" in status
        balancer_status = status["lip_balancer"]
        assert balancer_status["enabled"] is True
        assert balancer_status["max_share"] == 0.25
        assert balancer_status["scale_up"] is True
        assert balancer_status["min_efficiency"] == 0.50

        # persist_state
        pilot._persist_state()
        persisted = store.load()
        assert persisted is not None
        assert "lip_balancer" in persisted
        assert persisted["lip_balancer"]["enabled"] is True

    def test_graduated_mode_without_known_target_size_does_not_scale_up(self, pilot_env, clock):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=1000.0, no_qty=1000.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        # Update selection with NO target_size (omitted from program metadata)
        pilot.update_selection([{
            "ticker": TICKER,
            "pool_dollars": 2000.0,
        }])
        pilot.canary_graduated = True

        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # Base count is $10 / 0.48 = 20 contracts.
        # Even though graduated and book depth is huge (1000), scale-up is prohibited
        # because target_size is not explicitly known.
        assert orders["quote_bid"]["count"] == 20


# ---------------------------------------------------------------------------
# Sub-minute WebSocket Orderbook Streaming (Phase 3)
# ---------------------------------------------------------------------------


class TestMMPilotWSOrderbookStreaming:
    def test_update_book_from_ws_updates_levels_and_counters(self, pilot_env, clock):
        pilot = build_pilot(clock, selection=[TICKER])
        assert pilot._ws_book_updates == 0
        assert pilot._rest_book_fetches == 0

        ws_book = {
            "orderbook": {
                "yes": [[48, 50]],
                "no": [[48, 50]],
            }
        }
        pilot.update_book_from_ws(TICKER, ws_book)
        assert pilot._ws_book_updates == 1
        assert pilot._rest_book_fetches == 0

        book = pilot._book(TICKER)
        assert book is not None
        assert book["source"] == "ws"
        assert book["mid"] == pytest.approx(0.50)
        assert book["yes_bid"] == (0.48, 50.0)
        assert book["no_bid"] == (0.48, 50.0)
        assert book["yes_ask"] == (pytest.approx(0.52), 50.0)
        assert book["levels_updated_at"] == clock[0]
        assert book["updated_at"] == clock[0]

    def test_refresh_market_uses_fresh_ws_levels_skipping_rest(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        ws_book = {
            "orderbook": {
                "yes": [[48, 50]],
                "no": [[48, 50]],
            }
        }
        clock[0] = 1000.0
        pilot.update_book_from_ws(TICKER, ws_book)
        assert pilot._ws_book_updates == 1

        # Advance 5 seconds (< 15.0s max age)
        clock[0] = 1005.0
        initial_rest_fetches = pilot._rest_book_fetches
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        # REST book refresh should NOT have been invoked; only pre-submit order checks occur
        assert pilot._rest_book_fetches == initial_rest_fetches + len(placed)

        # Check G10b_book_source decision
        g10b = [d for d in pilot._decisions if d.get("gate") == "G10b_book_source"]
        assert len(g10b) > 0
        latest = g10b[-1]
        assert latest["decision"] == "pass"
        assert latest["reason"] == "ws_orderbook_fresh"
        assert latest["source"] == "ws"
        assert latest["levels_age"] == pytest.approx(5.0)

    def test_refresh_market_falls_back_to_rest_when_levels_stale(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        # Prime WS book at t=1000
        clock[0] = 1000.0
        ws_book = {
            "orderbook": {
                "yes": [[48, 50]],
                "no": [[48, 50]],
            }
        }
        pilot.update_book_from_ws(TICKER, ws_book)

        # Advance 20s (> 15.0s max age)
        clock[0] = 1020.0
        initial_rest_fetches = pilot._rest_book_fetches
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        # 1 REST book refresh + pre-submit order checks
        assert pilot._rest_book_fetches == initial_rest_fetches + 1 + len(placed)

        # Check G10b_book_source decision
        g10b = [d for d in pilot._decisions if d.get("gate") == "G10b_book_source"]
        assert len(g10b) > 0
        latest = g10b[-1]
        assert latest["decision"] == "fail"
        assert latest["reason"] == "levels_stale"
        assert latest["source"] == "rest"
        assert latest["levels_age"] == pytest.approx(20.0)

    def test_refresh_market_calls_rest_when_streaming_disabled(self, pilot_env, clock, monkeypatch):
        import config
        monkeypatch.setattr(config, "MM_WS_ORDERBOOK_STREAMING_ENABLED", False)

        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])

        # Fresh WS book 2 seconds ago
        clock[0] = 1000.0
        ws_book = {
            "orderbook": {
                "yes": [[48, 50]],
                "no": [[48, 50]],
            }
        }
        pilot.update_book_from_ws(TICKER, ws_book)

        clock[0] = 1002.0
        initial_rest_fetches = pilot._rest_book_fetches
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        # 1 REST book refresh + pre-submit order checks
        assert pilot._rest_book_fetches == initial_rest_fetches + 1 + len(placed)

        g10b = [d for d in pilot._decisions if d.get("gate") == "G10b_book_source"]
        assert len(g10b) > 0
        latest = g10b[-1]
        assert latest["decision"] == "fail"
        assert latest["reason"] == "streaming_disabled"
        assert latest["source"] == "rest"

    def test_refresh_market_calls_rest_when_book_missing(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        pilot._books.clear()

        initial_rest_fetches = pilot._rest_book_fetches
        placed = pilot.refresh_market(TICKER)
        assert len(placed) > 0
        # 1 REST book refresh + pre-submit order checks
        assert pilot._rest_book_fetches == initial_rest_fetches + 1 + len(placed)

        g10b = [d for d in pilot._decisions if d.get("gate") == "G10b_book_source"]
        assert len(g10b) > 0
        latest = g10b[-1]
        assert latest["decision"] == "fail"
        assert latest["reason"] == "book_missing"
        assert latest["source"] == "rest"

    def test_status_and_persisted_state_contain_ws_orderbook_telemetry(self, pilot_env, clock, tmp_path):
        import config
        state_file = str(tmp_path / "pilot_state.json")
        pilot = build_pilot(clock, selection=[TICKER], state_path=state_file)

        pilot._ws_book_updates = 42
        pilot._rest_book_fetches = 7

        # get_status
        status = pilot.get_status()
        assert "ws_orderbook_streaming" in status
        ws_status = status["ws_orderbook_streaming"]
        assert ws_status["enabled"] is True
        assert ws_status["max_age_seconds"] == getattr(config, "MM_WS_BOOK_MAX_AGE_SECONDS", 15.0)
        assert ws_status["ws_updates_count"] == 42
        assert ws_status["rest_fetches_count"] == 7

        # persist_state
        pilot._persist_state()
        persisted = pilot._state_store.load()
        assert persisted is not None
        assert "ws_orderbook_streaming" in persisted
        assert persisted["ws_orderbook_streaming"]["ws_updates_count"] == 42
        assert persisted["ws_orderbook_streaming"]["rest_fetches_count"] == 7

        # state restore in new pilot instance
        pilot2 = build_pilot(clock, selection=[TICKER], state_path=state_file)
        assert pilot2._ws_book_updates == 42
        assert pilot2._rest_book_fetches == 7

    def test_would_cross_increments_rest_book_fetches(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        initial_fetches = pilot._rest_book_fetches
        pilot._would_cross(TICKER, "yes", "buy", 0.40)
        assert pilot._rest_book_fetches == initial_fetches + 1

    def test_refresh_market_fails_closed_when_rest_fallback_fails(self, pilot_env, clock):
        client = FakeKalshiClient()
        pilot = build_pilot(clock, client=client, selection=[TICKER])
        # Place a resting order first with valid book
        pilot.update_book(TICKER, make_book(), source="rest")
        pilot.place_pilot_order(TICKER, "yes", "buy", 10, 0.45, purpose="quote_bid")
        assert len(pilot.resting_orders(TICKER)) == 1

        # Simulate REST fetch failure when refresh_market falls back to REST
        client.books = {}

        # Advance clock to make levels stale, triggering need_rest
        clock[0] += 60.0
        placed = pilot.refresh_market(TICKER)
        assert placed == []
        assert len(pilot.resting_orders(TICKER)) == 0

    def test_refresh_market_fails_closed_when_client_is_none_and_rest_needed(self, pilot_env, clock):
        pilot = build_pilot(clock, client=None, selection=[TICKER])
        placed = pilot.refresh_market(TICKER)
        assert placed == []


# ---------------------------------------------------------------------------
# Cross-Venue Inventory Skew Quoting
# ---------------------------------------------------------------------------


class TestMMPilotInventorySkewQuoting:
    def test_quote_widening_with_local_inventory_skew(self, pilot_env, clock):
        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, selection=[TICKER])

        # Flat inventory baseline
        placed_flat = pilot.refresh_market(TICKER)
        assert len(placed_flat) == 2
        orders_flat = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        spread_flat = orders_flat["quote_ask"]["price"] - orders_flat["quote_bid"]["price"]

        # Now simulate holding inventory under cap (100 contracts @ 0.50 = $50 with max $100)
        pilot.inventory.apply_fill(TICKER, "yes", "buy", 100, 0.50)
        assert pilot.inventory.net_usd(TICKER) == pytest.approx(50.0)

        # Refresh with skewed local inventory
        placed_skewed = pilot.refresh_market(TICKER)
        assert len(placed_skewed) == 2
        orders_skewed = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        spread_skewed = orders_skewed["quote_ask"]["price"] - orders_skewed["quote_bid"]["price"]

        # Skew spread must be strictly wider than flat baseline
        assert spread_skewed > spread_flat

    def test_quote_widening_with_cross_venue_skew(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=100.0)
        # Polymarket long YES 30 contracts (under INVENTORY_MAX_DELTA_CONTRACTS=50) -> delta_net = +30.0
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 30.0)

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        # Flat pilot without balancer baseline
        pilot_flat = build_pilot(clock, client=client, selection=[TICKER])
        pilot_flat.refresh_market(TICKER)
        orders_flat = {o["purpose"]: o for o in pilot_flat.resting_orders(TICKER)}
        spread_flat = orders_flat["quote_ask"]["price"] - orders_flat["quote_bid"]["price"]

        # Pilot with cross-venue balancer
        pilot_cv = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)
        pilot_cv.refresh_market(TICKER)
        orders_cv = {o["purpose"]: o for o in pilot_cv.resting_orders(TICKER)}
        spread_cv = orders_cv["quote_ask"]["price"] - orders_cv["quote_bid"]["price"]

        assert spread_cv > spread_flat

    def test_one_sided_quoting_when_cross_venue_severely_imbalanced(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=50.0)
        # Severe imbalance: long YES 85 contracts (>= max_delta_contracts=50 and ratio=1.0 >= 0.70)
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 85.0)
        skew = balancer.get_market_delta(TICKER)
        assert skew["is_imbalanced"] is True
        assert skew["delta_net"] > 0

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)
        placed = pilot.refresh_market(TICKER)

        # Net long YES -> one_side = "ask_only" (sell YES to reduce cross-venue risk)
        assert len(placed) == 1
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        assert "quote_ask" in orders
        assert "quote_bid" not in orders

        # Now test negative imbalance (net long NO -> delta_net < 0)
        balancer_neg = InventoryBalancer(max_delta_contracts=50.0)
        balancer_neg.update_position(TICKER, "polymarket", "no", "buy", 85.0)
        skew_neg = balancer_neg.get_market_delta(TICKER)
        assert skew_neg["is_imbalanced"] is True
        assert skew_neg["delta_net"] < 0

        pilot_neg = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer_neg)
        placed_neg = pilot_neg.refresh_market(TICKER)

        # Net long NO -> one_side = "bid_only" (buy YES to reduce cross-venue risk)
        assert len(placed_neg) == 1
        orders_neg = {o["purpose"]: o for o in pilot_neg.resting_orders(TICKER)}
        assert "quote_bid" in orders_neg
        assert "quote_ask" not in orders_neg

    def test_accumulating_side_headroom_and_taper(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=100.0)
        # Long YES 25 contracts (under cap of 50 contracts -> 2-sided quoting with headroom)
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 25.0)

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48, yes_qty=1000.0, no_qty=1000.0)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)

        placed = pilot.refresh_market(TICKER)
        assert len(placed) == 2
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}

        # Bid (YES) is accumulating side; cv_delta = 25.
        # Bid count should be strictly smaller than Ask count (reducing side is not tapered)
        assert orders["quote_bid"]["count"] < orders["quote_ask"]["count"]

    def test_decision_logging_g7b_inventory_skew_widening(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=100.0)
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 60.0)

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)
        pilot.refresh_market(TICKER)

        g7b_decisions = [d for d in pilot._decisions if d.get("gate") == "G7b_inventory_skew_widening"]
        assert len(g7b_decisions) > 0
        latest = g7b_decisions[-1]
        assert latest["decision"] == "pass"
        assert "local_usd=" in latest["reason"]
        assert "cv_delta=" in latest["reason"]
        assert "skew_ratio=" in latest["reason"]
        assert "skew_spread_mult=" in latest["reason"]

    def test_inventory_balancer_position_update_on_fill(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer()
        pilot = build_pilot(clock, selection=[TICKER], inventory_balancer=balancer)

        event = FillEvent(
            fill_id="fill-1",
            order_id="ord-fill-1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=15,
            price=0.52,
            is_taker=False,
            created_ts=clock[0],
            mid_at_detect=0.50,
        )
        order_info = {"purpose": "quote_bid", "ticker": TICKER, "side": "yes", "action": "buy"}
        pilot._process_fill(event, order_info)
        assert pilot.inventory.net_contracts(TICKER) == 15
        assert balancer.get_delta(TICKER) == 15.0

    def test_status_and_persisted_state_inventory_skew(self, pilot_env, clock, tmp_path):
        import config
        state_file = str(tmp_path / "pilot_state_skew.json")
        pilot = build_pilot(clock, selection=[TICKER], state_path=state_file)

        # get_status
        status = pilot.get_status()
        assert "inventory_skew" in status
        skew_status = status["inventory_skew"]
        assert skew_status["enabled"] is True
        assert skew_status["cross_venue_enabled"] is True
        assert skew_status["factor"] == getattr(config, "MM_SKEW_SPREAD_FACTOR", 1.0)
        assert skew_status["max_multiplier"] == getattr(config, "MM_SKEW_SPREAD_MAX_MULTIPLIER", 3.0)

        # persist_state
        pilot._persist_state()
        persisted = pilot._state_store.load()
        assert persisted is not None
        assert "inventory_skew" in persisted
        assert persisted["inventory_skew"]["enabled"] is True

    def test_cross_venue_skew_disabled_by_config(self, pilot_env, clock, monkeypatch):
        import config
        monkeypatch.setattr(config, "MM_CROSS_VENUE_SKEW_ENABLED", False)

        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer(max_delta_contracts=100.0)
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 85.0)

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)
        skew_info = pilot._get_cross_venue_skew(TICKER)
        assert skew_info == {"delta_net": 0.0, "imbalance_ratio": 0.0, "is_imbalanced": False}

        placed = pilot.refresh_market(TICKER)
        # When disabled, severe cross-venue delta does NOT force one_side
        assert len(placed) == 2

    def test_accumulating_side_zero_headroom_does_not_place_one_contract(self, pilot_env, clock, monkeypatch):
        import config
        from inventory_balancer import InventoryBalancer
        # INVENTORY_MAX_DELTA_CONTRACTS = 50
        monkeypatch.setattr(config, "INVENTORY_MAX_DELTA_CONTRACTS", 50.0)
        balancer = InventoryBalancer(max_delta_contracts=100.0)
        # Polymarket has 50 YES contracts (reaches INVENTORY_MAX_DELTA_CONTRACTS, so cv_headroom = 0)
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 50.0)

        client = FakeKalshiClient(books={TICKER: make_book(yes_bid=0.48, no_bid=0.48)})
        pilot = build_pilot(clock, client=client, selection=[TICKER], inventory_balancer=balancer)

        pilot.refresh_market(TICKER)
        orders = {o["purpose"]: o for o in pilot.resting_orders(TICKER)}
        # Bid order (accumulating side) had cv_headroom = 0 and balanced_base = 0; must not place 1 contract
        assert "quote_bid" not in orders

    def test_kalshi_fills_do_not_double_count_in_cross_venue_skew(self, pilot_env, clock):
        from inventory_balancer import InventoryBalancer
        balancer = InventoryBalancer()
        # Add 30 YES contracts on Polymarket
        balancer.update_position(TICKER, "polymarket", "yes", "buy", 30.0)

        pilot = build_pilot(clock, selection=[TICKER], inventory_balancer=balancer)
        skew_before = pilot._get_cross_venue_skew(TICKER)
        assert skew_before["delta_net"] == 30.0

        # Now simulate a fill on Kalshi
        event = FillEvent(
            fill_id="fill-kalshi-1",
            order_id="ord-k-1",
            ticker=TICKER,
            side="yes",
            action="buy",
            count=15,
            price=0.50,
            is_taker=False,
            created_ts=clock[0],
            mid_at_detect=0.50,
        )
        order_info = {"purpose": "quote_bid", "ticker": TICKER, "side": "yes", "action": "buy"}
        pilot._process_fill(event, order_info)

        # Local inventory recorded the fill
        assert pilot.inventory.net_contracts(TICKER) == 15

        # Balancer overall has 30 (poly) + 15 (kalshi) = 45 delta
        assert balancer.get_delta(TICKER) == 45.0

        # BUT pilot._get_cross_venue_skew excludes Kalshi, so cv_delta is STILL 30 (no double counting!)
        skew_after = pilot._get_cross_venue_skew(TICKER)
        assert skew_after["delta_net"] == 30.0


class TestMMPilotPortfolioMarginGuard:
    """Tests for portfolio-level capital and margin utilization guard."""

    def test_total_resting_notional_across_markets(self, pilot_env, clock):
        """total_resting_notional correctly sums order notional across multiple markets."""
        client = FakeKalshiClient(books={"TICKER_A": make_book(), "TICKER_B": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A", "TICKER_B"])
        # Place orders in two distinct tickers
        pilot.place_pilot_order("TICKER_A", side="yes", action="buy", count=20, price=0.40, purpose="quote_bid")
        pilot.place_pilot_order("TICKER_B", side="no", action="buy", count=30, price=0.50, purpose="quote_ask")

        # TICKER_A notional: 20 * 0.40 = $8.00
        # TICKER_B notional: 30 * 0.50 = $15.00
        # Total: $23.00
        assert pilot._resting_notional("TICKER_A") == 8.0
        assert pilot._resting_notional("TICKER_B") == 15.0
        assert pilot.total_resting_notional() == 23.0

    def test_get_portfolio_margin_metrics(self, pilot_env, clock):
        """get_portfolio_margin_metrics returns complete aggregate exposure and utilization."""
        client = FakeKalshiClient(books={"TICKER_A": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A"])
        # Give TICKER_A inventory
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=50, yes_price=0.50)
        # Place a resting order
        pilot.place_pilot_order("TICKER_A", side="no", action="buy", count=20, price=0.40, purpose="quote_ask")

        metrics = pilot.get_portfolio_margin_metrics()
        # Inventory: 50 * 0.50 = $25.00
        # Resting: 20 * 0.40 = $8.00
        # Total: $33.00
        assert metrics["total_inventory_notional"] == 25.0
        assert metrics["total_resting_notional"] == 8.0
        assert metrics["total_notional"] == 33.0
        assert metrics["is_over_cap"] is False
        assert metrics["cap_breached_reason"] == "ok"
        assert metrics["active_market_count"] == 1

    def test_g10c_gate_halts_flat_market_when_cap_breached(self, pilot_env, clock, monkeypatch):
        """G10c pulls quotes on flat markets when portfolio notional cap is reached."""
        import config
        monkeypatch.setattr(config, "MM_MAX_PORTFOLIO_NOTIONAL_USD", 50.0)

        client = FakeKalshiClient(books={"TICKER_A": make_book(), "TICKER_B": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A", "TICKER_B"])
        # Fill TICKER_A with $60 of inventory (exceeding $50 cap)
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=120, yes_price=0.50)

        # TICKER_B is flat
        res = pilot._evaluate_gates("TICKER_B")
        assert res["action"] == "pull"
        assert "portfolio_notional_cap" in res["reason"]

    def test_g10c_pulls_resting_quotes_on_flat_market_at_cap(self, pilot_env, clock, monkeypatch):
        """refresh_market pulls existing quotes on flat market when portfolio cap is breached."""
        import config
        monkeypatch.setattr(config, "MM_MAX_PORTFOLIO_NOTIONAL_USD", 50.0)

        client = FakeKalshiClient(books={"TICKER_A": make_book(), "TICKER_B": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A", "TICKER_B"])
        # Place resting quote on flat market TICKER_B before cap is breached
        pilot.place_pilot_order("TICKER_B", side="yes", action="buy", count=10, price=0.40, purpose="quote_bid")
        assert len(pilot.resting_orders("TICKER_B")) == 1

        # Breach portfolio cap via TICKER_A
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=120, yes_price=0.50)

        # Refresh TICKER_B: must pull resting orders and return empty placed list
        placed = pilot.refresh_market("TICKER_B")
        assert placed == []
        assert len(pilot.resting_orders("TICKER_B")) == 0

    def test_g10c_gate_allows_reducing_quotes_when_cap_breached(self, pilot_env, clock, monkeypatch):
        """G10c restricts already-skewed markets to reducing-only quotes when cap is reached."""
        import config
        monkeypatch.setattr(config, "MM_MAX_PORTFOLIO_NOTIONAL_USD", 50.0)

        client = FakeKalshiClient(books={"TICKER_A": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A"])
        # Fill TICKER_A with $60 of long YES inventory
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=120, yes_price=0.50)

        res = pilot._evaluate_gates("TICKER_A")
        assert res["action"] == "quote"
        # Long YES can only place ask (sell YES / buy NO) to reduce exposure
        assert res["one_side"] == "ask_only"

    def test_refresh_market_clamps_accumulating_quote_size_to_headroom(self, pilot_env, clock, monkeypatch):
        """refresh_market scales accumulating order size down so total exposure remains within cap."""
        import config
        monkeypatch.setattr(config, "MM_MAX_PORTFOLIO_NOTIONAL_USD", 50.0)
        monkeypatch.setattr(config, "MM_QUOTE_SIZE_USD", 30.0)  # wants $30 quotes

        client = FakeKalshiClient(books={
            TICKER: make_book(yes_bid=0.48, no_bid=0.48),
            "OTHER": make_book(),
        })
        pilot = build_pilot(clock, client=client, selection=["OTHER", TICKER])
        pilot.canary_graduated = True

        # Populate $40 of inventory in other market
        pilot.inventory.apply_fill("OTHER", side="yes", action="buy", count=80, yes_price=0.50)

        # Remaining portfolio headroom = $50 - $40 = $10
        # QuoteEngine wants count for $30 quote @ 0.50 = 60 contracts
        # Headroom allows only $10 total across competing quotes when flat
        pilot.refresh_market(TICKER)
        # Total portfolio notional (inventory + resting orders) must be <= $50 cap
        total_portfolio_exposure = pilot.inventory.total_net_usd() + pilot.total_resting_notional()
        assert total_portfolio_exposure <= 50.0
        # Resting orders on TICKER must not exceed the $10 available headroom
        ticker_resting = pilot._resting_notional(TICKER)
        assert ticker_resting <= 10.0

    def test_get_portfolio_margin_metrics_uses_client_balance(self, pilot_env, clock):
        """get_portfolio_margin_metrics uses venue balance when available below bankroll."""
        client = FakeKalshiClient(books={"TICKER_A": make_book()})
        client.balance = 100.0  # Venue balance is $100 (< default bankroll $2000)
        pilot = build_pilot(clock, client=client, selection=["TICKER_A"])
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=100, yes_price=0.50)  # $50 notional
        metrics = pilot.get_portfolio_margin_metrics()
        assert metrics["base_capital"] == 100.0
        # 50 / 100 = 0.50 margin utilization
        assert metrics["margin_utilization"] == pytest.approx(0.50)

    def test_authorize_order_enforces_portfolio_limits_and_consecutive_placements(self, pilot_env, clock, monkeypatch):
        """authorize_order rejects orders exceeding portfolio caps and blocks consecutive placements."""
        import config
        monkeypatch.setattr(config, "MM_MAX_PORTFOLIO_NOTIONAL_USD", 50.0)
        client = FakeKalshiClient(books={"TICKER_A": make_book(), "TICKER_B": make_book()})
        pilot = build_pilot(clock, client=client, selection=["TICKER_A", "TICKER_B"])

        # Current exposure: 0. Remaining headroom: $50
        # First order: $30 notional (60 contracts @ 0.50) -> should be allowed
        res1 = pilot.authorize_order("TICKER_A", side="yes", action="buy", count=60, price=0.50)
        assert res1.allowed is True
        oid1 = pilot.place_pilot_order("TICKER_A", side="yes", action="buy", count=60, price=0.50, purpose="quote_bid")
        assert oid1 is not None

        # Now resting notional is $30. Remaining headroom: $20
        # Second order on TICKER_B: $25 notional (50 contracts @ 0.50) -> exceeds $20 headroom!
        res2 = pilot.authorize_order("TICKER_B", side="yes", action="buy", count=50, price=0.50)
        assert res2.allowed is False
        assert res2.reason == "portfolio_notional_cap_exceeded"

        # Third order on TICKER_B: $15 notional (30 contracts @ 0.50) -> within $20 headroom!
        res3 = pilot.authorize_order("TICKER_B", side="yes", action="buy", count=30, price=0.50)
        assert res3.allowed is True

        # Now fill TICKER_A with long YES inventory, making it skewed
        pilot.inventory.apply_fill("TICKER_A", side="yes", action="buy", count=60, yes_price=0.50)
        # Even if over cap, reducing order (selling YES) is allowed
        res_red = pilot.authorize_order("TICKER_A", side="yes", action="sell", count=20, price=0.50, reducing=True)
        assert res_red.allowed is True

    def test_get_status_reports_portfolio_margin_metrics(self, pilot_env, clock):
        """get_status includes portfolio_margin metrics dictionary."""
        pilot = build_pilot(clock, selection=[TICKER])
        status = pilot.get_status()
        assert "portfolio_margin" in status
        pm = status["portfolio_margin"]
        assert "total_notional" in pm
        assert "margin_utilization" in pm
        assert "is_over_cap" in pm


class TestMMPilotDynamicMarketSelection:
    def test_update_selection_records_metadata_and_selection_status(self, pilot_env, clock):
        pilot = build_pilot(clock)
        items = [
            {
                "ticker": "DYNAMIC_A",
                "score": 95.5,
                "base_score": 80.0,
                "volume_24h": 5000.0,
                "spread_cents": 8.0,
                "pool_dollars": 3000.0,
                "category": "Economics",
                "target_size": 20,
                "discount_factor_bps": 5000,
            }
        ]
        pilot.update_selection(items)
        assert pilot._selected == {"DYNAMIC_A"}
        status = pilot.get_selection_status()
        assert status["selected_tickers"] == ["DYNAMIC_A"]
        assert status["market_count"] == 1
        assert status["dynamic_selection_enabled"] is True
        assert status["refresh_interval_sec"] == 1800.0
        assert status["min_24h_volume"] == 0.0
        assert status["max_spread_cents"] == 0.0
        assert status["volume_weight"] == 0.20
        meta = status["markets"]["DYNAMIC_A"]
        assert meta["score"] == 95.5
        assert meta["base_score"] == 80.0
        assert meta["volume_24h"] == 5000.0
        assert meta["spread_cents"] == 8.0
        assert meta["pool_dollars"] == 3000.0

        full_status = pilot.get_status()
        assert "selection" in full_status
        assert full_status["selection"]["market_count"] == 1
        assert full_status["selection"]["selected_tickers"] == ["DYNAMIC_A"]

    def test_update_selection_strings_captured_in_status(self, pilot_env, clock):
        pilot = build_pilot(clock)
        pilot.update_selection(["TICKER_X", "TICKER_Y"])
        assert pilot._selected == {"TICKER_X", "TICKER_Y"}
        status = pilot.get_selection_status()
        assert status["selected_tickers"] == ["TICKER_X", "TICKER_Y"]
        assert status["market_count"] == 2
        assert "TICKER_X" in status["markets"]
        assert "TICKER_Y" in status["markets"]

    def test_run_loop_auto_wires_dynamic_selection(self, pilot_env, clock, monkeypatch):
        import threading
        client = FakeKalshiClient()
        # Add fetch_incentive_programs so pilot detects client support
        client.fetch_incentive_programs = lambda **kw: []
        pilot = build_pilot(clock, client=client)

        selected_mock = [{"ticker": "DYNAMIC_SEL", "score": 100.0}]
        monkeypatch.setattr("scans.lip_select.select_lip_markets", lambda c: selected_mock)

        stop = threading.Event()
        original_poll = pilot._controls.poll

        def stop_after_poll():
            original_poll()
            stop.set()

        pilot._controls.poll = stop_after_poll
        pilot.run_loop(stop, selection_provider=None)

        assert pilot._selected == {"DYNAMIC_SEL"}
        status = pilot.get_selection_status()
        assert "DYNAMIC_SEL" in status["markets"]

    def test_run_loop_selection_provider_failure_does_not_halt(self, pilot_env, clock):
        import threading
        pilot = build_pilot(clock, selection=[TICKER])
        stop = threading.Event()

        def failing_provider():
            raise RuntimeError("API timeout during selection")

        original_poll = pilot._controls.poll

        def stop_after_poll():
            original_poll()
            stop.set()

        pilot._controls.poll = stop_after_poll
        pilot.run_loop(stop, selection_provider=failing_provider)

        assert pilot.halted is False
        assert pilot._selected == {TICKER}
