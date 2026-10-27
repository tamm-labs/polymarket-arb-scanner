"""Regression tests for direct-provider boundaries and truthful research accounting."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import sys
from unittest.mock import MagicMock, patch
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jev_client import JevClient, JevError, load_api_key
from jev_calibration import calculate_edge_realization, calculate_brier_score, select_independent_records
from scripts.jev_semantic_benchmark import evaluate


class TestDirectProvider:
    def test_gateway_key_never_used(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "gateway-only"}, clear=True):
            assert not JevClient().is_available()

    def test_external_endpoint_rejected_before_sending_secret(self):
        with pytest.raises(JevError, match="official"):
            JevClient(api_key="private-value", base_url="https://example.org/decisions")

    def test_owner_only_key_file(self, tmp_path):
        path = tmp_path / "api-token"
        path.write_text("private-value")
        path.chmod(0o600)
        with patch.dict(os.environ, {"TYPESAFE_API_KEY_FILE": str(path)}, clear=True):
            assert load_api_key() == "private-value"
            path.chmod(0o644)
            with pytest.raises(JevError, match="600"):
                load_api_key()

    def test_http_body_is_not_exposed(self, caplog):
        error = urllib.error.HTTPError("https://api.typesafe.ai", 401, "error", {}, io.BytesIO(b"private-value"))
        with patch("jev_client._urlopen", side_effect=error):
            with pytest.raises(JevError) as caught:
                JevClient(api_key="private-value").query_decisions("state", {})
        assert "private-value" not in str(caught.value) + caplog.text

    @pytest.mark.parametrize("answers", [{}, {"q": {"type": "noul", "noul": 1.1}},
                                         {"q": {"type": "noul", "noul": float("nan")}}])
    def test_missing_and_invalid_probabilities_fail_closed(self, answers):
        response = MagicMock()
        response.read.return_value = json.dumps({"answers": answers}).encode()
        with patch("jev_client._urlopen") as request:
            request.return_value.__enter__.return_value = response
            with pytest.raises(JevError):
                JevClient(api_key="test").evaluate_noul("state", "True?")


class TestEvidenceAccounting:
    def test_no_ask_is_never_invented(self):
        record = {"action": "buy_no", "market_prob": 0.7, "resolved_outcome": 0,
                  "details": {"yes_ask": 0.7, "fees_usd": 0, "slippage_usd": 0}}
        result = calculate_edge_realization([record])
        assert result["evaluated_trades"] == 0
        assert result["total_pnl"] is None
        assert result["excluded_records"] == 1

    def test_costs_and_dollar_budget(self):
        record = {"action": "buy_no", "market_prob": 0.7, "resolved_outcome": 0,
                  "details": {"yes_ask": 0.7, "no_ask": 0.4, "fees_usd": 2, "slippage_usd": 1}}
        result = calculate_edge_realization([record], standard_stake=40)
        assert result["total_cost"] == 43
        assert result["total_pnl"] == 57  # 100 contracts, not 40; NO ask is 0.4, not 0.3.

    def test_missing_costs_excluded(self):
        record = {"action": "buy_yes", "resolved_outcome": 1,
                  "details": {"yes_ask": 0.4, "no_ask": 0.65, "fees_usd": 1}}
        assert calculate_edge_realization([record])["total_pnl"] is None

    def test_zero_samples_not_perfect_accuracy(self):
        assert calculate_brier_score([], []) is None
        with pytest.raises(ValueError):
            calculate_brier_score([float("nan")], [1])

    def test_duplicate_and_post_expiry_predictions_excluded(self):
        details = {"market_id": "m1", "observed_at": "2026-01-01T00:00:00Z",
                   "expires_at": "2026-02-01T00:00:00Z", "contract_hash": "a",
                   "model": "jev-1.13.0", "prompt_version": "v2",
                   "quote_method": "orderbook-extrema-v1", "resolution_source": "gamma_final_uma", "resolution_status": "resolved"}
        row = {"id": 1, "timestamp": "2026-01-01", "details": details,
               "jev_prob": 0.6, "market_prob": 0.5, "resolved_outcome": 1,
               "resolved_at": "2026-02-02T00:00:00Z"}
        later = dict(row, id=2, timestamp="2026-01-02")
        invalid = dict(row, id=3, details=dict(details, observed_at="2026-03-01T00:00:00Z"))
        selected, excluded = select_independent_records([later, row, invalid])
        assert [r["id"] for r in selected] == [1]
        assert excluded == {"invalid_or_missing_provenance": 1, "repeated_market": 1}


class TestSemanticBenchmark:
    def test_labels_are_never_sent_to_model(self):
        client = MagicMock()
        client.query_decisions.return_value = {"model": "jev-1.13.0", "answers": {
            "outcome_resolution": {"choice": "neutral_unclear", "confidence": 1.0}}}
        report = evaluate([{"id": "fixture", "task": "news", "expected": "neutral_unclear",
                            "state": {"headline": "rumor"}}], client)
        assert "expected" not in client.query_decisions.call_args.kwargs
        assert report["summary"]["accuracy"] == 1
        assert report["summary"]["accepted_coverage"] == 0


class TestSemanticEvidenceBoundaries:
    def test_discovery_cache_tracks_rules_ids_model_and_prompt(self, tmp_path):
        import asyncio
        from dataclasses import replace
        from market_discovery import DiscoveryCache, DiscoveryPipeline, JevJudge, MarketRef, pair_key
        from market_discovery import polymarket_refs, kalshi_refs
        client = MagicMock(model="jev-1.13.0")
        client.query_decisions.return_value = {"answers": {
            "resolution_equivalence": {"choice": "identical", "confidence": 0.99},
            "equivalence_probability": {"noul": 0.99}}}
        cache = DiscoveryCache(tmp_path / "cache.json")
        cache.put(pair_key("polymarket", "Bitcoin above 100", "kalshi", "Bitcoin above 100"),
                  {"equivalent": True, "confidence": 1})
        engine = DiscoveryPipeline(cache, JevJudge(client))
        markets = {"polymarket": [MarketRef("polymarket", "a", "Bitcoin above 100", "index X terminal")],
                   "kalshi": [MarketRef("kalshi", "b", "Bitcoin above 100", "index X terminal")]}
        first = asyncio.run(engine.run(markets))
        assert first.cached_hits == 0 and len(first.accepted) == 1
        assert asyncio.run(engine.run(markets)).cached_hits == 1
        markets["kalshi"][0] = replace(markets["kalshi"][0], rules="index Y terminal")
        assert asyncio.run(engine.run(markets)).cached_hits == 0
        client.model = "new-model"
        assert asyncio.run(engine.run(markets)).cached_hits == 0
        markets["kalshi"][0] = replace(markets["kalshi"][0], rules="")
        assert not asyncio.run(engine.run(markets)).accepted
        assert polymarket_refs([{"id": "a", "question": "test", "description": "full rules"}])[0].rules == "full rules"
        assert kalshi_refs([{"ticker": "a", "title": "test", "rules_primary": "full rules"}])[0].rules == "full rules"

    def test_live_jev_research_execution_is_rejected_before_any_pipeline(self):
        from executor import ArbitrageExecutor
        executor = object.__new__(ArbitrageExecutor)
        executor.dry_run = False
        executor._log_skipped = MagicMock()
        assert executor.execute({"type": "anything", "_research_only": True}) is False
        assert executor.execute({"type": "NewsSnipe", "_sentiment_source": "jev"}) is False
        assert executor._log_skipped.call_count == 2


class TestResolutionSync:
    def test_sync_uses_stable_id_and_preserves_finality_evidence(self):
        from db import TradeDB
        from scripts.sync_jev_resolutions import sync_resolutions
        db = TradeDB(":memory:")
        try:
            db.record_jev_decision(asset="BTC", strike=100, spot=90, action="pass_fair",
                                   details={"market_id": "condition-a", "question": "duplicate title"})
            market = {"conditionId": "condition-a", "closed": True, "umaResolutionStatus": "resolved",
                      "outcomes": '["No", "Yes"]', "outcomePrices": '["0", "1"]'}
            with patch("scripts.sync_jev_resolutions.fetch_market_by_id", return_value=market) as fetch:
                assert sync_resolutions(db, dry_run=True)["resolved"] == 1
                assert db.get_jev_decisions()[0]["resolved_outcome"] is None
                assert sync_resolutions(db)["resolved"] == 1
                fetch.assert_called_with("condition-a")
            record = db.get_jev_decisions()[0]
            details = json.loads(record["details"])
            assert record["resolved_outcome"] == 1
            assert details["resolution_source"] == "gamma_final_uma"
            assert details["resolution_status"] == "resolved"
            assert details["resolution_checked_at"] == record["resolved_at"]
        finally:
            db.close()
