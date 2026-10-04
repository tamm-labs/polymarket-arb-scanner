"""Tests for scans/lip_select.py — LIP pool ranking and filters."""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scans.lip_select import select_lip_markets


FUTURE = (datetime.now(timezone.utc) + timedelta(days=365)).isoformat()
SOON = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


def _client(programs, prices=None, depths=None):
    c = MagicMock()
    c.fetch_incentive_programs.return_value = programs
    prices = prices or {}
    c.get_market_price.side_effect = lambda m: prices.get(m["ticker"], (0.50, 0.50))
    depths = depths or {}
    c.get_order_book_depth.side_effect = lambda t: depths.get(t, {"yes_ask_size": 0, "no_ask_size": 0})
    return c


def _data(markets_by_event, categories=None):
    categories = categories or {}
    events = [{"event_ticker": ev, "category": categories.get(ev, "Politics")}
              for ev in markets_by_event]
    return (events, markets_by_event, {ev: ev for ev in markets_by_event})


def _prog(ticker, dollars, end=FUTURE):
    return {"market_ticker": ticker, "period_reward_dollars": dollars,
            "discount_factor_bps": 5000, "end_date": end}


class TestSelectLipMarkets:
    def test_ranks_by_pool_over_competition(self):
        programs = [_prog("BIG", 100.0), _prog("CROWDED", 100.0), _prog("SMALL", 20.0)]
        data = _data({"EV": [
            {"ticker": "BIG", "close_time": FUTURE},
            {"ticker": "CROWDED", "close_time": FUTURE},
            {"ticker": "SMALL", "close_time": FUTURE},
        ]})
        client = _client(programs, depths={
            "BIG": {"yes_ask_size": 10, "no_ask_size": 10},
            "CROWDED": {"yes_ask_size": 500, "no_ask_size": 500},
            "SMALL": {"yes_ask_size": 0, "no_ask_size": 0},
        })
        out = select_lip_markets(client, kalshi_data=data, max_markets=3)
        assert [o["ticker"] for o in out] == ["SMALL", "BIG", "CROWDED"]
        assert out[1]["pool_dollars"] == pytest.approx(100.0)
        assert out[1]["competition_depth"] == 20

    def test_aggregates_multiple_programs_per_ticker_deterministically(self):
        earlier = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
        later = (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()
        first = _prog("X", 40.0, end=later)
        first["discount_factor_bps"] = 4000
        second = _prog("X", 60.0, end=earlier)
        second["discount_factor_bps"] = 6000
        data = _data({"EV": [{"ticker": "X", "close_time": FUTURE}]})
        forward = select_lip_markets(_client([first, second]), kalshi_data=data)
        reverse = select_lip_markets(_client([second, first]), kalshi_data=data)
        assert forward == reverse
        assert forward[0]["pool_dollars"] == pytest.approx(100.0)
        assert forward[0]["program_end"] == earlier
        assert forward[0]["discount_factor_bps"] == 6000

    def test_excludes_sports_category(self):
        programs = [_prog("SPORTY", 500.0), _prog("POLI", 50.0)]
        data = _data(
            {"EVS": [{"ticker": "SPORTY", "close_time": FUTURE}],
             "EVP": [{"ticker": "POLI", "close_time": FUTURE}]},
            categories={"EVS": "Sports", "EVP": "Politics"},
        )
        out = select_lip_markets(_client(programs), kalshi_data=data)
        assert [o["ticker"] for o in out] == ["POLI"]

    def test_excludes_mention_ticker_even_if_category_clean(self):
        programs = [_prog("KXEARNINGSMENTIONBA", 500.0), _prog("POLI", 50.0)]
        data = _data(
            {"KXEARNINGSMENTIONBA": [{"ticker": "KXEARNINGSMENTIONBA", "close_time": FUTURE}],
             "EVP": [{"ticker": "POLI", "close_time": FUTURE}]},
            categories={"KXEARNINGSMENTIONBA": "Economics", "EVP": "Politics"},
        )
        out = select_lip_markets(_client(programs), kalshi_data=data)
        assert [o["ticker"] for o in out] == ["POLI"]

    def test_excludes_small_pools(self):
        programs = [_prog("DUST", 1.0)]  # below LIP_MIN_POOL=10
        data = _data({"EV": [{"ticker": "DUST", "close_time": FUTURE}]})
        assert select_lip_markets(_client(programs), kalshi_data=data) == []

    def test_excludes_imminent_close(self):
        programs = [_prog("ENDING", 100.0)]
        data = _data({"EV": [{"ticker": "ENDING", "close_time": SOON}]})
        assert select_lip_markets(_client(programs), kalshi_data=data) == []

    def test_excludes_imminent_program_end(self):
        programs = [_prog("PROGEND", 100.0, end=SOON)]
        data = _data({"EV": [{"ticker": "PROGEND", "close_time": FUTURE}]})
        assert select_lip_markets(_client(programs), kalshi_data=data) == []

    def test_excludes_price_band_tails(self):
        programs = [_prog("TAIL", 100.0), _prog("MID", 100.0)]
        data = _data({"EV": [
            {"ticker": "TAIL", "close_time": FUTURE},
            {"ticker": "MID", "close_time": FUTURE},
        ]})
        client = _client(programs, prices={"TAIL": (0.04, 0.98), "MID": (0.55, 0.55)})
        out = select_lip_markets(client, kalshi_data=data)
        assert [o["ticker"] for o in out] == ["MID"]
        assert out[0]["mid"] == pytest.approx(0.50)

    def test_price_band_uses_mid_not_yes_ask(self):
        programs = [_prog("WIDE", 100.0)]
        data = _data({"EV": [{"ticker": "WIDE", "close_time": FUTURE}]})
        out = select_lip_markets(
            _client(programs, prices={"WIDE": (0.92, 0.52)}),
            kalshi_data=data,
        )
        assert len(out) == 1
        assert out[0]["mid"] == pytest.approx(0.70)

    @pytest.mark.parametrize("program_end,market_close", [
        (None, FUTURE),
        ("not-a-date", FUTURE),
        (FUTURE, None),
        (FUTURE, "not-a-date"),
    ])
    def test_missing_or_invalid_duration_is_excluded(self, program_end, market_close):
        programs = [_prog("UNKNOWN-DURATION", 100.0, end=program_end)]
        data = _data({"EV": [{"ticker": "UNKNOWN-DURATION", "close_time": market_close}]})
        assert select_lip_markets(_client(programs), kalshi_data=data) == []

    def test_unknown_ticker_skipped(self):
        programs = [_prog("GHOST", 100.0)]
        data = _data({"EV": [{"ticker": "OTHER", "close_time": FUTURE}]})
        assert select_lip_markets(_client(programs), kalshi_data=data) == []

    def test_no_programs_returns_empty(self):
        data = _data({"EV": [{"ticker": "X", "close_time": FUTURE}]})
        assert select_lip_markets(_client([]), kalshi_data=data) == []

    def test_no_client_returns_empty(self):
        assert select_lip_markets(None) == []

    def test_respects_max_markets(self):
        programs = [_prog(f"T{i}", 100.0 - i) for i in range(8)]
        data = _data({"EV": [{"ticker": f"T{i}", "close_time": FUTURE} for i in range(8)]})
        out = select_lip_markets(_client(programs), kalshi_data=data, max_markets=3)
        assert len(out) == 3

    def test_carries_market_title_and_dynamic_price_ranges(self):
        programs = [_prog("GRID", 100.0)]
        ranges = [{"start": "0.00", "end": "1.00", "step": "0.02"}]
        data = _data({"EV": [{
            "ticker": "GRID",
            "title": None,
            "close_time": FUTURE,
            "price_ranges": ranges,
        }]})
        out = select_lip_markets(_client(programs), kalshi_data=data)
        assert out[0]["title"] == "GRID"
        assert out[0]["price_ranges"] == ranges

    def test_filters_out_wide_spread(self):
        programs = [_prog("WIDE_SPD", 100.0), _prog("TIGHT_SPD", 100.0)]
        data = _data({"EV": [
            {"ticker": "WIDE_SPD", "close_time": FUTURE},
            {"ticker": "TIGHT_SPD", "close_time": FUTURE},
        ]})
        # WIDE: yes_ask=0.75, no_ask=0.65 -> yes_bid=0.35, spread=0.40 (40 cents)
        # TIGHT: yes_ask=0.55, no_ask=0.55 -> yes_bid=0.45, spread=0.10 (10 cents)
        client = _client(programs, prices={
            "WIDE_SPD": (0.75, 0.65),
            "TIGHT_SPD": (0.55, 0.55),
        })
        out = select_lip_markets(client, kalshi_data=data, max_spread_cents=25.0)
        assert [o["ticker"] for o in out] == ["TIGHT_SPD"]
        assert out[0]["spread_cents"] == 10.0

    def test_filters_out_low_volume(self):
        programs = [_prog("DEAD", 100.0), _prog("ACTIVE", 100.0)]
        data = _data({"EV": [
            {"ticker": "DEAD", "close_time": FUTURE, "volume_24h": 5.0},
            {"ticker": "ACTIVE", "close_time": FUTURE, "volume_24h": 500.0},
        ]})
        client = _client(programs)
        out = select_lip_markets(client, kalshi_data=data, min_volume=50.0)
        assert [o["ticker"] for o in out] == ["ACTIVE"]
        assert out[0]["volume_24h"] == 500.0

    def test_volume_weight_boosts_liquid_market_score(self):
        programs = [_prog("LOW_VOL", 100.0), _prog("HIGH_VOL", 100.0)]
        data = _data({"EV": [
            {"ticker": "LOW_VOL", "close_time": FUTURE, "volume_24h": 0.0},
            {"ticker": "HIGH_VOL", "close_time": FUTURE, "volume_24h": 1000.0},
        ]})
        client = _client(programs)
        out = select_lip_markets(client, kalshi_data=data, volume_weight=0.25)
        assert len(out) == 2
        assert out[0]["ticker"] == "HIGH_VOL"
        assert out[1]["ticker"] == "LOW_VOL"
        assert out[0]["base_score"] == pytest.approx(100.0)
        assert out[0]["score"] > out[1]["score"]
        assert out[0]["volume_factor"] > 1.0
        assert out[1]["volume_factor"] == 1.0



if __name__ == "__main__":
    import unittest
    unittest.main()
