"""Toxic Flow Pause observability scan (Strategy ToxicFlowPause).

Surfaces markets that the ``ToxicFlowDetector`` has currently paused due to
adverse-selection (informed-flow) detection. The runtime pause is enforced
inside ``MarketMaker.refresh_quotes`` — this scan exposes the same state so
operators can monitor paused markets without diffing logs.

Returns ``[]`` whenever ``MM_TOXIC_FLOW_ENABLED`` is false so an always-on
call in the orchestrator is a cheap no-op.
"""

import logging

logger = logging.getLogger(__name__)


def scan_toxic_flow_pause(
    market_keys: list[str] | None = None,
    detector=None,
) -> list[dict]:
    """Emit ToxicFlowPause defensive opportunities for currently-paused markets.

    Args:
        market_keys: Markets to inspect. If empty/None, returns ``[]`` (we
            cannot enumerate markets ourselves without coupling to the live
            MarketMaker registry).
        detector: Optional ``ToxicFlowDetector`` instance. Defaults to the
            module singleton ``market_maker.get_toxic_flow_detector()``.

    Returns:
        List of ToxicFlowPause opportunity dicts (possibly empty).
    """
    from config import MM_TOXIC_FLOW_ENABLED

    if not MM_TOXIC_FLOW_ENABLED:
        return []

    if not market_keys:
        return []

    if detector is None:
        from market_maker import get_toxic_flow_detector
        detector = get_toxic_flow_detector()

    opps: list[dict] = []
    for market_key in market_keys:
        if not market_key:
            continue
        if not detector.should_pause(market_key):
            continue

        toxicity = detector.get_toxicity(market_key)
        pause_remaining = detector.get_pause_remaining(market_key)
        reason = getattr(detector, "get_pause_reason", lambda k: "")(market_key)

        price_summary = f"toxicity={toxicity:.2f} remaining={pause_remaining:.0f}s"
        if reason:
            price_summary = f"{price_summary} ({reason[:40]})"

        opps.append({
            "type": "ToxicFlowPause",
            "_layer": 3,
            "market": f"{market_key[:60]} (toxic flow pause)",
            "prices": price_summary,
            "total_cost": "$0.00",
            "net_profit": 0.0,
            "net_roi": 0.0,
            "confidence": 0.90,
            "reason": reason,
            "_market_key": market_key,
            "_toxicity": toxicity,
            "_pause_remaining_seconds": pause_remaining,
            "_reason": reason,
        })

    return opps
