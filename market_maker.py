"""Market making engine — provide liquidity and earn bid-ask spreads.

Components:
- QuoteEngine: calculates optimal bid/ask quotes
- InventoryTracker: tracks net position per market per platform
- QuoteManager: places, cancels, and updates resting limit orders
- MarketMaker: orchestrates the full market making loop
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

DEFAULT_MIN_SPREAD = 0.03       # Minimum bid-ask spread ($0.03)
DEFAULT_MAX_INVENTORY = 50.0    # Max position per market ($50)
DEFAULT_MAX_TOTAL_EXPOSURE = 500.0  # Max total MM exposure ($500)
DEFAULT_QUOTE_SIZE = 5.0        # Default size per quote ($5)
DEFAULT_INVENTORY_SKEW = 0.5    # How much inventory skews quotes (0-1)
DEFAULT_REFRESH_INTERVAL = 10.0  # Seconds between quote updates


# ---------------------------------------------------------------------------
# InventoryTracker
# ---------------------------------------------------------------------------

class InventoryTracker:
    """Track net position per market per platform.

    Thread-safe.  Positions are tracked in dollar terms.
    """

    def __init__(self, max_per_market: float = DEFAULT_MAX_INVENTORY,
                 max_total: float = DEFAULT_MAX_TOTAL_EXPOSURE):
        self.max_per_market = max_per_market
        self.max_total = max_total
        self._positions: dict[str, dict[str, float]] = {}  # {market_key: {platform: position}}
        self._lock = threading.Lock()

    def update(self, market_key: str, platform: str, delta: float) -> None:
        """Record a position change.

        Args:
            market_key: Market identifier.
            platform: Platform name.
            delta: Change in position (+buy, -sell) in dollars.
        """
        with self._lock:
            if market_key not in self._positions:
                self._positions[market_key] = {}
            current = self._positions[market_key].get(platform, 0.0)
            self._positions[market_key][platform] = current + delta

    def get_position(self, market_key: str, platform: str = "") -> float:
        """Get net position for a market (optionally filtered by platform).

        Returns:
            Net position in dollars. Positive = long YES, negative = long NO.
        """
        with self._lock:
            if market_key not in self._positions:
                return 0.0
            if platform:
                return self._positions[market_key].get(platform, 0.0)
            return sum(self._positions[market_key].values())

    def get_total_exposure(self) -> float:
        """Get total absolute exposure across all markets."""
        with self._lock:
            total = 0.0
            for market_positions in self._positions.values():
                total += sum(abs(v) for v in market_positions.values())
            return total

    def can_trade(self, market_key: str, size: float) -> bool:
        """Check if a trade would exceed inventory limits.

        Args:
            market_key: Market identifier.
            size: Absolute trade size in dollars.
        """
        current = abs(self.get_position(market_key))
        total = self.get_total_exposure()
        return (current + size <= self.max_per_market and
                total + size <= self.max_total)

    def needs_hedge(self, market_key: str) -> bool:
        """Check if a market position needs hedging (>80% of max)."""
        position = abs(self.get_position(market_key))
        return position > self.max_per_market * 0.8

    def get_all_positions(self) -> dict[str, float]:
        """Get all market positions (net across platforms)."""
        with self._lock:
            return {
                key: sum(plats.values())
                for key, plats in self._positions.items()
                if any(v != 0 for v in plats.values())
            }


# ---------------------------------------------------------------------------
# QuoteEngine
# ---------------------------------------------------------------------------

class QuoteEngine:
    """Calculate optimal bid/ask quotes for market making.

    Spread formula: base_spread + inventory_skew + volatility_adjustment
    """

    def __init__(
        self,
        min_spread: float = DEFAULT_MIN_SPREAD,
        inventory_skew_factor: float = DEFAULT_INVENTORY_SKEW,
    ):
        self.min_spread = min_spread
        self.inventory_skew_factor = inventory_skew_factor

    def calculate_quotes(
        self,
        mid_price: float,
        inventory: float = 0.0,
        max_inventory: float = DEFAULT_MAX_INVENTORY,
        volatility: float = 0.0,
        market_key: str = "",
        toxicity: float | None = None,
        toxicity_spread_multiplier: float | None = None,
        skew_spread_multiplier: float | None = None,
    ) -> dict:
        """Calculate bid and ask prices.

        Args:
            mid_price: Current mid-market price (0-1).
            inventory: Current net position. Positive = long, quotes skew to sell.
            max_inventory: Maximum inventory for skew calculation.
            volatility: Recent price volatility (0-1). Widens spread.
            market_key: Optional market key. When set and
                ``MM_VOLATILITY_ADJUSTED_ENABLED`` is true, the VolatilityTracker
                singleton multiplies the half-spread by
                ``get_spread_multiplier(market_key)``.
            toxicity: Optional toxicity probability (0-1). Widens spread.
            toxicity_spread_multiplier: Optional explicit spread multiplier from toxicity.
            skew_spread_multiplier: Optional explicit spread multiplier from inventory skew.

        Returns:
            Dict with ``bid``, ``ask``, ``spread``, ``skew``, ``mid``,
            ``toxicity_spread_multiplier``, and ``skew_spread_multiplier``.
        """
        half_spread = self.min_spread / 2

        # VolatilityAdjustedMM hook — widen spread when the tracker reports
        # elevated volatility for this market. The multiplier is 1.0 when
        # MM_VOLATILITY_ADJUSTED_ENABLED is false, making this a cheap no-op.
        if market_key:
            try:
                multiplier = get_volatility_tracker().get_spread_multiplier(market_key)
                half_spread = half_spread * multiplier
            except Exception:
                pass

        # Adverse Selection / Toxicity hook — widen spread when informed flow is active
        tox_mult = 1.0
        if toxicity_spread_multiplier is not None and toxicity_spread_multiplier > 0:
            tox_mult = float(toxicity_spread_multiplier)
        elif market_key:
            try:
                tox_mult = get_toxic_flow_detector().get_spread_multiplier(market_key)
            except Exception:
                tox_mult = 1.0
        elif toxicity is not None and toxicity > 0:
            from config import MM_TOXIC_SPREAD_FACTOR
            tox_mult = 1.0 + (float(toxicity) * MM_TOXIC_SPREAD_FACTOR)

        if tox_mult > 1.0:
            half_spread = half_spread * tox_mult

        # Inventory Skew Spread Widening — widen spread when inventory is skewed
        skew_mult = 1.0
        if skew_spread_multiplier is not None and skew_spread_multiplier > 0:
            skew_mult = float(skew_spread_multiplier)

        if skew_mult > 1.0:
            half_spread = half_spread * skew_mult

        # Volatility adjustment: wider spread in volatile markets
        vol_adj = volatility * 0.5  # 10% vol -> 5 cent wider spread

        # Inventory skew: when long, lower bid (buy less) and lower ask (sell faster)
        if max_inventory > 0:
            inventory_ratio = inventory / max_inventory  # -1 to +1
        else:
            inventory_ratio = 0.0
        skew = inventory_ratio * self.inventory_skew_factor * half_spread

        total_half = half_spread + vol_adj

        bid = mid_price - total_half - skew
        ask = mid_price + total_half - skew

        # Clamp to valid price range
        bid = max(0.01, min(0.99, bid))
        ask = max(0.01, min(0.99, ask))

        # Ensure ask > bid
        if ask <= bid:
            mid = (bid + ask) / 2
            bid = mid - 0.01
            ask = mid + 0.01

        return {
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "spread": round(ask - bid, 4),
            "skew": round(skew, 4),
            "mid": round(mid_price, 4),
            "toxicity_spread_multiplier": round(tox_mult, 2),
            "skew_spread_multiplier": round(skew_mult, 2),
        }


# ---------------------------------------------------------------------------
# QuoteManager
# ---------------------------------------------------------------------------

class QuoteManager:
    """Place, cancel, and update resting limit orders.

    Wraps platform-specific order placement APIs behind a common interface.
    """

    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run
        self._active_orders: dict[str, dict] = {}  # order_id -> order_info
        self._lock = threading.Lock()

    def place_quote(
        self,
        platform: str,
        market_key: str,
        side: str,
        price: float,
        size: float,
        trader=None,
    ) -> str | None:
        """Place a resting limit order (GTC).

        Args:
            platform: Platform name.
            market_key: Market identifier.
            side: "bid" or "ask".
            price: Limit price.
            size: Order size in dollars.
            trader: Platform-specific trader/client instance.

        Returns:
            Order ID or None on failure.
        """
        if trader is None or self.dry_run or (getattr(trader, "dry_run", False) is True):
            logger.debug("No trader or dry run for %s — dry run quote %s %s @ %.4f",
                         platform, side, market_key, price)
            # Dry run: generate a fake order ID for tracking
            order_id = f"dry_{platform}_{market_key}_{side}_{time.time():.0f}"
            with self._lock:
                self._active_orders[order_id] = {
                    "platform": platform,
                    "market_key": market_key,
                    "side": side,
                    "price": price,
                    "size": size,
                    "status": "resting",
                    "placed_at": time.time(),
                }
            return order_id

        norm_platform = platform.lower()
        from config import ENABLED_EXECUTION_PLATFORMS
        if norm_platform not in ENABLED_EXECUTION_PLATFORMS:
            logger.error(
                "Platform %s not in ENABLED_EXECUTION_PLATFORMS (%s); rejecting quote",
                norm_platform, ENABLED_EXECUTION_PLATFORMS,
            )
            return None

        if norm_platform == "limitless":
            from config import LIMITLESS_MAX_INVENTORY
            with self._lock:
                current_active = sum(
                    o.get("size", 0) for o in self._active_orders.values()
                    if o.get("platform") == "limitless" and o.get("market_key") == market_key
                )
            if current_active + size > LIMITLESS_MAX_INVENTORY:
                logger.warning(
                    "Limitless quote exceeds LIMITLESS_MAX_INVENTORY (active $%.2f + size $%.2f > max $%.2f); rejecting",
                    current_active, size, LIMITLESS_MAX_INVENTORY,
                )
                return None

        if norm_platform == "kalshi":
            from kalshi_policy import live_kalshi_submit_allowed
            if not live_kalshi_submit_allowed(market_key, reducing=False):
                logger.warning("Kalshi quote rejected by policy guard for ticker %s", market_key)
                return None

        order_side = "buy" if side.lower() in ("bid", "buy") else "sell"
        quantity = max(1, int(round(size / price))) if price > 0 else 1

        resp = None
        try:
            if norm_platform == "limitless":
                resp = trader.place_order(
                    market_id=market_key,
                    side=order_side,
                    outcome="yes",
                    quantity=quantity,
                    price=price,
                    time_in_force="gtc",
                )
            elif norm_platform == "kalshi":
                resp = trader.place_order(
                    ticker=market_key,
                    side="yes",
                    action=order_side,
                    count=quantity,
                    price_dollars=price,
                    time_in_force="gtc",
                )
            elif norm_platform == "polymarket":
                resp = trader.place_order(
                    token_id=market_key,
                    side=order_side.upper(),
                    price=price,
                    size=float(quantity),
                    order_type="GTC",
                )
            elif hasattr(trader, "place_order"):
                try:
                    resp = trader.place_order(
                        market_id=market_key,
                        side=order_side,
                        price=price,
                        size=size,
                    )
                except TypeError:
                    resp = trader.place_order(market_key, order_side, price, size)
        except Exception as exc:
            logger.error("Failed to place live MM quote on %s for %s: %s", platform, market_key, exc)
            return None

        if resp is None:
            logger.warning("Live MM quote returned None for %s %s", platform, market_key)
            return None

        order_id = None
        if isinstance(resp, str):
            order_id = resp
        elif isinstance(resp, dict):
            order_id = (
                resp.get("order_id")
                or resp.get("orderID")
                or resp.get("id")
                or (resp.get("order", {}).get("order_id") if isinstance(resp.get("order"), dict) else None)
            )

        if not order_id:
            logger.warning("Live MM quote on %s returned response without order ID: %s", platform, resp)
            return None

        order_id = str(order_id)
        with self._lock:
            self._active_orders[order_id] = {
                "platform": platform,
                "market_key": market_key,
                "side": side,
                "price": price,
                "size": size,
                "status": "resting",
                "placed_at": time.time(),
            }
        logger.info("MM quote placed: %s %s %s @ %.4f ($%.2f) -> order_id=%s",
                    platform, side, market_key, price, size, order_id)
        return order_id

    @staticmethod
    def _cancel_on_exchange(order_id: str, trader) -> bool:
        """Best-effort live cancel (plan 10 side-finding 1).

        Before this, "cancelling" only cleared the local dict — a live
        resting order would survive process death. Dry-run order IDs
        (``dry_`` prefix) never reach the exchange.

        Args:
            order_id: Venue order identifier, or a ``dry_`` synthetic ID.
            trader: Venue client whose ``cancel_order`` must explicitly
                confirm a live cancellation.

        Returns:
            True for a synthetic dry-run cancellation or an explicitly
            confirmed live cancellation; False otherwise.
        """
        if order_id.startswith("dry_"):
            return True
        if trader is None:
            return False
        try:
            result = trader.cancel_order(order_id)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            logger.warning("Live cancel failed for %s: %s", order_id, exc)
            return False
        return result is True

    def cancel_quote(self, order_id: str, trader=None) -> bool:
        """Cancel a resting order.

        Args:
            order_id: Order ID to cancel.
            trader: Platform-specific trader/client instance. When provided,
                the cancel is also sent to the exchange (live orders only).

        Returns:
            True if cancelled or already gone.
        """
        if not self._cancel_on_exchange(order_id, trader):
            return False
        with self._lock:
            if order_id in self._active_orders:
                self._active_orders[order_id]["status"] = "cancelled"
                del self._active_orders[order_id]
                return True
        return False

    def cancel_all(self, market_key: str = "", trader=None) -> int:
        """Cancel all active orders, optionally filtered by market.

        When ``trader`` is provided, live orders are also cancelled on the
        exchange (plan 10 side-finding 1).

        Returns number of orders cancelled.
        """
        with self._lock:
            to_cancel = [
                oid for oid, info in self._active_orders.items()
                if not market_key or info["market_key"] == market_key
            ]
        cancelled = []
        for oid in to_cancel:
            if self._cancel_on_exchange(oid, trader):
                cancelled.append(oid)
        with self._lock:
            for oid in cancelled:
                self._active_orders.pop(oid, None)
        return len(cancelled)

    def get_active_orders(self, market_key: str = "") -> list[dict]:
        """Get all active orders, optionally filtered by market."""
        with self._lock:
            return [
                {"order_id": oid, **info}
                for oid, info in self._active_orders.items()
                if not market_key or info["market_key"] == market_key
            ]

    def record_fill(self, order_id: str, fill_size: float, fill_price: float) -> None:
        """Record a fill event for an order."""
        with self._lock:
            if order_id in self._active_orders:
                order = self._active_orders[order_id]
                order["status"] = "filled"
                order["fill_size"] = fill_size
                order["fill_price"] = fill_price
                order["filled_at"] = time.time()
                # Remove from active
                del self._active_orders[order_id]


# ---------------------------------------------------------------------------
# MarketMaker
# ---------------------------------------------------------------------------

class MarketMaker:
    """Orchestrates the full market making loop.

    Selects markets, calculates quotes, manages orders and inventory.
    Runs as a background task in continuous mode.
    """

    def __init__(
        self,
        inventory: InventoryTracker | None = None,
        quote_engine: QuoteEngine | None = None,
        quote_manager: QuoteManager | None = None,
        min_spread: float = DEFAULT_MIN_SPREAD,
        quote_size: float = DEFAULT_QUOTE_SIZE,
        max_inventory: float = DEFAULT_MAX_INVENTORY,
        max_total_exposure: float = DEFAULT_MAX_TOTAL_EXPOSURE,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        dry_run: bool = True,
        hedger=None,
        auto_hedge_enabled: bool = False,
    ):
        self.inventory = inventory or InventoryTracker(max_inventory, max_total_exposure)
        self.quote_engine = quote_engine or QuoteEngine(min_spread)
        self.quote_manager = quote_manager or QuoteManager(dry_run=dry_run)
        self.quote_size = quote_size
        self.max_inventory = max_inventory
        self.refresh_interval = refresh_interval
        self.dry_run = dry_run
        # Strategy #12: when auto_hedge_enabled, inventory breaches trigger
        # PartialFillHedger.hedge_inventory() to sell back at best bid.
        self.hedger = hedger
        self.auto_hedge_enabled = auto_hedge_enabled
        self._running = False
        self._active_markets: dict[str, dict] = {}  # market_key -> market info
        self._lock = threading.Lock()

    def add_market(self, market_key: str, platform: str, mid_price: float,
                   token_id: str = "", ticker: str = "") -> None:
        """Register a market for market making.

        Args:
            market_key: Unique market identifier.
            platform: Platform to make markets on.
            mid_price: Current mid-market price.
            token_id: Polymarket token ID (if applicable).
            ticker: Kalshi ticker (if applicable).
        """
        with self._lock:
            self._active_markets[market_key] = {
                "platform": platform,
                "mid_price": mid_price,
                "token_id": token_id,
                "ticker": ticker,
                "last_update": time.time(),
            }

    def remove_market(self, market_key: str) -> None:
        """Stop making markets on a specific market."""
        self.quote_manager.cancel_all(market_key)
        with self._lock:
            self._active_markets.pop(market_key, None)

    def update_price(self, market_key: str, mid_price: float) -> None:
        """Update the mid price for a market (e.g. from WebSocket feed)."""
        with self._lock:
            if market_key in self._active_markets:
                self._active_markets[market_key]["mid_price"] = mid_price
                self._active_markets[market_key]["last_update"] = time.time()

    def refresh_quotes(self, market_key: str = "", trader=None) -> list[dict]:
        """Recalculate and update quotes for active markets.

        Args:
            market_key: If set, refresh only this market. Otherwise refresh all.
            trader: Platform trader instance for order placement.

        Returns:
            List of new quote dicts placed.
        """
        with self._lock:
            markets = dict(self._active_markets)

        if market_key:
            markets = {k: v for k, v in markets.items() if k == market_key}

        new_quotes = []
        for mkey, info in markets.items():
            platform = info["platform"]
            mid = info["mid_price"]

            # Skip invalid prices
            if mid <= 0.01 or mid >= 0.99:
                continue

            # ToxicFlowPause hook — when MM_TOXIC_FLOW_ENABLED is true and the
            # detector flags this market as toxic, skip quoting. should_pause
            # is itself flag-gated so this is a cheap no-op when disabled.
            try:
                if get_toxic_flow_detector().should_pause(mkey):
                    continue
            except Exception:
                pass

            # Get current inventory
            inventory = self.inventory.get_position(mkey)

            # Skip if inventory limit hit and we can't reduce
            if abs(inventory) >= self.max_inventory:
                continue

            # Calculate quotes
            quotes = self.quote_engine.calculate_quotes(
                mid, inventory, self.max_inventory, market_key=mkey,
            )

            # Cancel existing quotes for this market
            live_trader = trader if not self.dry_run else None
            outstanding = len(self.quote_manager.get_active_orders(mkey))
            cancelled = self.quote_manager.cancel_all(mkey, trader=live_trader)
            if cancelled < outstanding:
                logger.error(
                    "MM: %d of %d resting orders on %s were not cancelled — "
                    "skipping requote to avoid stacking live quotes",
                    outstanding - cancelled, outstanding, mkey,
                )
                continue

            # Place new bid
            if self.inventory.can_trade(mkey, self.quote_size):
                bid_id = self.quote_manager.place_quote(
                    platform, mkey, "bid", quotes["bid"], self.quote_size,
                    trader=trader if not self.dry_run else None,
                )
                if bid_id:
                    new_quotes.append({
                        "order_id": bid_id,
                        "market": mkey,
                        "side": "bid",
                        "price": quotes["bid"],
                        "size": self.quote_size,
                    })

            # Place new ask
            if self.inventory.can_trade(mkey, self.quote_size):
                ask_id = self.quote_manager.place_quote(
                    platform, mkey, "ask", quotes["ask"], self.quote_size,
                    trader=trader if not self.dry_run else None,
                )
                if ask_id:
                    new_quotes.append({
                        "order_id": ask_id,
                        "market": mkey,
                        "side": "ask",
                        "price": quotes["ask"],
                        "size": self.quote_size,
                    })

        if new_quotes:
            logger.info("MM refreshed %d quotes across %d markets",
                        len(new_quotes), len(markets))
        return new_quotes

    def on_fill(self, order_id: str, market_key: str, platform: str,
                side: str, price: float, size: float,
                token_id: str = "", **identifiers) -> None:
        """Handle a fill event: update inventory, record fill, check hedging.

        Args:
            order_id: Filled order ID.
            market_key: Market identifier.
            platform: Platform name.
            side: "bid" or "ask".
            price: Fill price.
            size: Fill size in dollars.
            token_id: Platform-specific token / ticker for the hedge sell-side.
            **identifiers: Forwarded to PartialFillHedger.hedge_inventory
                (market_id, selection_id, contract_id, market_hash, runner_id,
                outcome_id, symbol).
        """
        # Update inventory
        delta = size if side == "bid" else -size
        self.inventory.update(market_key, platform, delta)

        # Record the fill
        self.quote_manager.record_fill(order_id, size, price)

        logger.info("MM fill: %s %s %s @ %.4f ($%.2f) | inventory=%.2f",
                     platform, side, market_key, price, size,
                     self.inventory.get_position(market_key))

        # Strategy #12: trigger inventory hedge when over threshold and
        # auto-hedging is enabled with a real hedger wired in. The next
        # refresh_quotes cycle will automatically apply more aggressive
        # inventory skew via QuoteEngine, so even without a successful
        # hedge order the maker reduces directional exposure on its own.
        if not self.inventory.needs_hedge(market_key):
            return
        logger.warning("MM: inventory on %s exceeds threshold — hedge needed",
                       market_key)
        if not self.auto_hedge_enabled or self.hedger is None:
            return
        try:
            self.hedger.hedge_inventory(
                market_key=market_key,
                platform=platform,
                side=side,
                fill_price=price,
                size=size,
                token_id=token_id,
                **identifiers,
            )
        except Exception as exc:
            logger.exception("MM auto-hedge raised on %s/%s: %s",
                             platform, market_key, exc)

    def generate_opportunities(self) -> list[dict]:
        """Generate market making pseudo-opportunities for the executor.

        Returns a list of opportunity dicts (one per active market) that
        the executor can process in dry-run mode for reporting.
        """
        with self._lock:
            markets = dict(self._active_markets)

        opportunities = []
        for mkey, info in markets.items():
            mid = info["mid_price"]
            if mid <= 0.01 or mid >= 0.99:
                continue

            inventory = self.inventory.get_position(mkey)
            quotes = self.quote_engine.calculate_quotes(
                mid, inventory, self.max_inventory
            )

            spread = quotes["spread"]
            # Estimated profit per round-trip (both sides fill)
            est_profit = spread - (spread * 0.03)  # ~3% fee estimate

            if est_profit <= 0:
                continue

            opportunities.append({
                "type": "MarketMake",
                "_layer": 3,  # Layer 3: market making
                "market": mkey,
                "prices": f"bid={quotes['bid']:.4f} ask={quotes['ask']:.4f} mid={mid:.4f}",
                "total_cost": f"${self.quote_size:.2f}",
                "net_profit": est_profit,
                "net_roi": est_profit / self.quote_size if self.quote_size > 0 else 0,
                "_platform": info["platform"],
                "_bid_price": quotes["bid"],
                "_ask_price": quotes["ask"],
                "_mid_price": mid,
                "_spread": spread,
                "_inventory": inventory,
                "_market_key": mkey,
            })

        return opportunities

    def stop(self, trader=None) -> None:
        """Stop market making — cancel all outstanding quotes.

        Args:
            trader: Optional platform client; when provided, live resting
                orders are cancelled on the exchange too, not just in the
                local dict (plan 10 side-finding 1).
        """
        self._running = False
        cancelled = self.quote_manager.cancel_all(trader=trader)
        logger.info("MM stopped: cancelled %d outstanding quotes", cancelled)

    def get_status(self) -> dict:
        """Get current market maker status for dashboard."""
        with self._lock:
            num_markets = len(self._active_markets)

        active_orders = self.quote_manager.get_active_orders()
        positions = self.inventory.get_all_positions()

        return {
            "active_markets": num_markets,
            "active_orders": len(active_orders),
            "total_exposure": self.inventory.get_total_exposure(),
            "positions": positions,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# CrossPlatformMaker (Strategy #11)
# ---------------------------------------------------------------------------

class CrossPlatformMaker:
    """Posts opposing limit orders on two platforms for the same event.

    Composes two InventoryTracker instances (one per platform), a single
    QuoteEngine for each side, and a shared QuoteManager. When a leg fills
    on one side, the other side is canceled and ``hedger.hedge_inventory``
    is invoked so the residual exposure is squared off automatically.

    The detection layer (``scans.cross_mm.scan_cross_mm``) emits
    ``CrossPlatformMM`` opportunity dicts; this class owns the inventory
    bookkeeping and one-sided-fill cleanup that comes after detection.
    """

    def __init__(
        self,
        inventory_a: InventoryTracker | None = None,
        inventory_b: InventoryTracker | None = None,
        quote_engine: QuoteEngine | None = None,
        quote_manager: QuoteManager | None = None,
        min_spread: float = DEFAULT_MIN_SPREAD,
        quote_size: float = DEFAULT_QUOTE_SIZE,
        max_inventory: float = DEFAULT_MAX_INVENTORY,
        hedger=None,
        auto_hedge_enabled: bool = False,
        dry_run: bool = True,
    ):
        self.inventory_a = inventory_a or InventoryTracker(max_inventory)
        self.inventory_b = inventory_b or InventoryTracker(max_inventory)
        self.quote_engine = quote_engine or QuoteEngine(min_spread)
        self.quote_manager = quote_manager or QuoteManager(dry_run=dry_run)
        self.quote_size = quote_size
        self.max_inventory = max_inventory
        self.hedger = hedger
        self.auto_hedge_enabled = auto_hedge_enabled
        self.dry_run = dry_run
        self._pairs: dict[str, dict] = {}
        self._traders: dict[str, object] = {}
        self._lock = threading.Lock()

    def _cancel_market_orders(self, market_key: str) -> bool:
        """Cancel tracked legs without forgetting unconfirmed live orders."""
        all_cancelled = True
        for order in self.quote_manager.get_active_orders(market_key):
            trader = (None if self.dry_run
                      else self._traders.get(order["platform"]))
            if not self.quote_manager.cancel_quote(order["order_id"], trader):
                all_cancelled = False
        return all_cancelled

    def add_pair(
        self,
        market_key: str,
        platform_a: str,
        platform_b: str,
        mid_a: float,
        mid_b: float,
        token_a: str = "",
        token_b: str = "",
    ) -> None:
        """Register a matched cross-platform market pair."""
        with self._lock:
            self._pairs[market_key] = {
                "platform_a": platform_a,
                "platform_b": platform_b,
                "mid_a": mid_a,
                "mid_b": mid_b,
                "token_a": token_a,
                "token_b": token_b,
                "last_update": time.time(),
            }

    def remove_pair(self, market_key: str) -> None:
        """Stop quoting a paired market; cancel any open legs."""
        if not self._cancel_market_orders(market_key):
            logger.warning("CrossPlatformMM retained pair %s because one or "
                           "more live cancels were unconfirmed", market_key)
            return
        with self._lock:
            self._pairs.pop(market_key, None)

    def update_price(self, market_key: str, platform: str, mid_price: float) -> None:
        """Update one side's mid price (e.g. from a WebSocket tick)."""
        with self._lock:
            pair = self._pairs.get(market_key)
            if not pair:
                return
            if platform == pair["platform_a"]:
                pair["mid_a"] = mid_price
            elif platform == pair["platform_b"]:
                pair["mid_b"] = mid_price
            pair["last_update"] = time.time()

    def refresh_quotes_paired(self, market_key: str = "",
                              traders: dict | None = None) -> list[dict]:
        """Cancel and repost both legs of every (or one) paired market.

        Args:
            market_key: Restrict to one pair; default refreshes all pairs.
            traders: ``{platform: trader_client}`` for live order placement.
                Pass ``None`` (default) for dry-run.
        """
        traders = traders or {}
        if not self.dry_run:
            self._traders.update(traders)
        with self._lock:
            pairs = dict(self._pairs)
        if market_key:
            pairs = {k: v for k, v in pairs.items() if k == market_key}

        new_quotes = []
        for mk, pair in pairs.items():
            # Inventory-aware quote on each side
            inv_a = self.inventory_a.get_position(mk)
            inv_b = self.inventory_b.get_position(mk)
            quotes_a = self.quote_engine.calculate_quotes(
                pair["mid_a"], inv_a, self.max_inventory)
            quotes_b = self.quote_engine.calculate_quotes(
                pair["mid_b"], inv_b, self.max_inventory)

            if not self._cancel_market_orders(mk):
                logger.warning("CrossPlatformMM skipped refresh for %s because "
                               "a prior live order remains unconfirmed", mk)
                continue

            trader_a = traders.get(pair["platform_a"]) if not self.dry_run else None
            trader_b = traders.get(pair["platform_b"]) if not self.dry_run else None

            if self.inventory_a.can_trade(mk, self.quote_size):
                bid_a = self.quote_manager.place_quote(
                    pair["platform_a"], mk, "bid", quotes_a["bid"],
                    self.quote_size, trader=trader_a,
                )
                if bid_a:
                    new_quotes.append({"order_id": bid_a, "platform": pair["platform_a"],
                                       "side": "bid", "market": mk,
                                       "price": quotes_a["bid"]})

            if self.inventory_b.can_trade(mk, self.quote_size):
                ask_b = self.quote_manager.place_quote(
                    pair["platform_b"], mk, "ask", quotes_b["ask"],
                    self.quote_size, trader=trader_b,
                )
                if ask_b:
                    new_quotes.append({"order_id": ask_b, "platform": pair["platform_b"],
                                       "side": "ask", "market": mk,
                                       "price": quotes_b["ask"]})
        if new_quotes:
            logger.info("CrossPlatformMM refreshed %d legs across %d pairs",
                        len(new_quotes), len(pairs))
        return new_quotes

    def on_fill(self, order_id: str, market_key: str, platform: str,
                side: str, price: float, size: float,
                token_id: str = "", **identifiers) -> None:
        """Handle a fill: update side-specific inventory; cancel & hedge sibling.

        When one leg fills the other side becomes a directional exposure.
        We cancel the unfilled sibling immediately and route the filled
        side through ``PartialFillHedger.hedge_inventory`` to sell back at
        market when ``auto_hedge_enabled`` is set.
        """
        with self._lock:
            pair = self._pairs.get(market_key)
        if pair is None:
            logger.warning("CrossPlatformMM fill on unknown pair %s", market_key)
            return

        if platform == pair["platform_a"]:
            self.inventory_a.update(market_key, platform,
                                    size if side == "bid" else -size)
        elif platform == pair["platform_b"]:
            self.inventory_b.update(market_key, platform,
                                    size if side == "bid" else -size)

        self.quote_manager.record_fill(order_id, size, price)

        # Cancel every residual leg (including a partially-filled source
        # order) so we do not accidentally double-fill. Missing traders or
        # failed venue cancels retain local truth for retry/escalation.
        if not self._cancel_market_orders(market_key):
            logger.error("CrossPlatformMM fill left one or more unconfirmed "
                         "live orders for %s", market_key)

        logger.info(
            "CrossPlatformMM fill: %s %s %s @ %.4f ($%.2f) | inv_a=%.2f inv_b=%.2f",
            platform, side, market_key, price, size,
            self.inventory_a.get_position(market_key),
            self.inventory_b.get_position(market_key),
        )

        if not self.auto_hedge_enabled or self.hedger is None:
            return
        try:
            self.hedger.hedge_inventory(
                market_key=market_key,
                platform=platform,
                side=side,
                fill_price=price,
                size=size,
                token_id=token_id,
                **identifiers,
            )
        except Exception as exc:
            logger.exception("CrossPlatformMM auto-hedge raised on %s/%s: %s",
                             platform, market_key, exc)

    def generate_opportunities(self) -> list[dict]:
        """Emit CrossPlatformMM opp dicts (matches scan_cross_mm format)."""
        with self._lock:
            pairs = dict(self._pairs)

        opps = []
        for mk, pair in pairs.items():
            mid_a = pair["mid_a"]
            mid_b = pair["mid_b"]
            if not (0.01 < mid_a < 0.99) or not (0.01 < mid_b < 0.99):
                continue
            spread = abs(mid_b - mid_a)
            if spread <= 0:
                continue
            buy_plat = pair["platform_a"] if mid_a < mid_b else pair["platform_b"]
            sell_plat = pair["platform_b"] if mid_a < mid_b else pair["platform_a"]
            buy_price = min(mid_a, mid_b)
            sell_price = max(mid_a, mid_b)
            est_profit = spread * self.quote_size

            opps.append({
                "type": "CrossPlatformMM",
                "_layer": 3,
                "market": mk,
                "prices": (
                    f"{buy_plat}_BID={buy_price:.4f} "
                    f"{sell_plat}_ASK={sell_price:.4f}"
                ),
                "total_cost": f"${self.quote_size * 2:.2f}",
                "net_profit": est_profit,
                "net_roi": spread / max(buy_price, 0.01),
                "_market_key": mk,
                "_platform_a": buy_plat,
                "_platform_b": sell_plat,
                "_spread": spread,
                "_leg_a": {
                    "platform": buy_plat, "side": "BUY", "token": "yes",
                    "price": buy_price, "size": self.quote_size,
                    "_market_key": mk,
                },
                "_leg_b": {
                    "platform": sell_plat, "side": "SELL", "token": "yes",
                    "price": sell_price, "size": self.quote_size,
                    "_market_key": mk,
                },
            })
        return opps

    def stop(self) -> None:
        """Cancel all paired quotes."""
        cancelled = self.quote_manager.cancel_all()
        logger.info("CrossPlatformMM stopped: cancelled %d outstanding legs",
                    cancelled)


# ---------------------------------------------------------------------------
# RewardTracker (Polymarket)
# ---------------------------------------------------------------------------

class RewardTracker:
    """Track Polymarket reward program metadata and calculate optimal spreads.

    Thread-safe cache of reward scores per market with TTL-based expiration.
    """

    def __init__(self):
        self._reward_cache: dict[str, dict] = {}  # {market_key: reward_data}
        self._cache_timestamps: dict[str, float] = {}  # {market_key: timestamp}
        self._lock = threading.Lock()

    def update_polymarket_reward(self, market_key: str, reward_data: dict,
                                ttl_seconds: float = 300.0) -> None:
        """Update reward metadata for a market.

        Args:
            market_key: Market identifier (conditionId).
            reward_data: Dict with min_incentive_size, max_incentive_spread, pool_size_usdc, etc.
            ttl_seconds: Cache TTL in seconds.
        """
        with self._lock:
            self._reward_cache[market_key] = reward_data
            self._cache_timestamps[market_key] = time.time() + ttl_seconds

    def get_polymarket_reward(self, market_key: str) -> dict | None:
        """Get cached reward metadata for a market.

        Returns None if not cached, expired, or never set.
        """
        with self._lock:
            if market_key not in self._reward_cache:
                return None
            expiry = self._cache_timestamps.get(market_key, 0)
            if time.time() > expiry:
                # Cache expired
                del self._reward_cache[market_key]
                if market_key in self._cache_timestamps:
                    del self._cache_timestamps[market_key]
                return None
            return self._reward_cache[market_key]

    def calculate_optimal_reward_spread(self, market_key: str, mid_price: float,
                                       inventory: float = 0.0) -> dict | None:
        """Calculate bid/ask spread optimized for reward qualification.

        Takes into account platform-specific constraints and inventory position.

        Args:
            market_key: Market identifier.
            mid_price: Current mid-price (0-1).
            inventory: Current inventory position (positive = long).

        Returns:
            Dict with bid, ask, spread, or None if no reward data.
        """
        reward_data = self.get_polymarket_reward(market_key)
        if not reward_data:
            return None

        max_spread = reward_data.get("max_incentive_spread", 0.05)

        # Target spread: 60% of max for good reward score while staying competitive
        target_spread = max_spread * 0.6
        half_spread = target_spread / 2

        # Apply inventory skew: when long, tighten ask (sell faster)
        skew = 0.0
        if inventory > 0:
            skew = -target_spread * 0.1

        bid = mid_price - half_spread + skew
        ask = mid_price + half_spread + skew

        # Clamp to valid range
        bid = max(0.01, min(0.99, bid))
        ask = max(0.01, min(0.99, ask))

        return {
            "bid": round(bid, 4),
            "ask": round(ask, 4),
            "spread": round(ask - bid, 4),
            "reward_optimized": True,
        }


# ---------------------------------------------------------------------------
# KalshiRewardTracker
# ---------------------------------------------------------------------------

class KalshiRewardTracker:
    """Track Kalshi liquidity incentive program participation via local order logging.

    Kalshi has no public reward API, so we track qualifying order metrics locally.
    Thread-safe.
    """

    def __init__(self, db=None):
        """Initialize tracker.

        Args:
            db: TradeDB instance for persisting reward metrics (optional).
        """
        self._db = db
        self._active_orders: dict[str, dict] = {}  # {order_id: order_data}
        self._lock = threading.Lock()

    def log_order_placed(self, order_id: str, market_key: str, size: float,
                        price: float, mid_price: float, side: str) -> None:
        """Log a Kalshi limit order placement.

        Args:
            order_id: Order ID from exchange.
            market_key: Market identifier.
            size: Order size in dollars.
            price: Limit price.
            mid_price: Current mid-price for spread calculation.
            side: "buy" or "sell".
        """
        spread = abs(price - mid_price) / mid_price if mid_price > 0 else 0

        with self._lock:
            self._active_orders[order_id] = {
                "market_key": market_key,
                "size": size,
                "price": price,
                "side": side,
                "spread": spread,
                "placed_at": time.time(),
            }

        # Persist to database if available
        if self._db:
            self._db.log_reward_metric(
                platform="kalshi",
                market_key=market_key,
                order_id=order_id,
                event="placed",
                size=size,
                spread=spread,
                resting_seconds=0,
            )

    def log_order_cancelled(self, order_id: str) -> None:
        """Log a Kalshi order cancellation.

        Args:
            order_id: Order ID that was cancelled.
        """
        with self._lock:
            if order_id not in self._active_orders:
                return

            order = self._active_orders.pop(order_id)
            resting_seconds = int(time.time() - order["placed_at"])

            # Persist cancellation to database if available
            if self._db:
                self._db.log_reward_metric(
                    platform="kalshi",
                    market_key=order["market_key"],
                    order_id=order_id,
                    event="cancelled",
                    size=order["size"],
                    spread=order["spread"],
                    resting_seconds=resting_seconds,
                )

    def get_active_orders(self) -> list[dict]:
        """Get all currently active orders."""
        with self._lock:
            return [
                {"order_id": oid, **info}
                for oid, info in self._active_orders.items()
            ]

    def estimate_daily_reward(self, market_key: str) -> float:
        """Estimate daily reward yield for a market based on resting orders.

        This is a rough estimate only; actual rewards are computed daily by Kalshi.

        Args:
            market_key: Market identifier.

        Returns:
            Estimated daily reward in USDC.
        """
        with self._lock:
            orders = [
                o for o in self._active_orders.values()
                if o["market_key"] == market_key
            ]

        if not orders:
            return 0.0

        # Kalshi reward formula is proprietary; estimate based on resting time + spread
        # Assumption: ~$0.50/day per 24h of resting at mid-spread (3%)
        total_resting = sum(time.time() - o["placed_at"] for o in orders)
        avg_spread = sum(o["spread"] for o in orders) / len(orders) if orders else 0

        # Estimate: reward ∝ resting_time * (1 - spread_tightness)
        estimated_daily = (total_resting / 86400) * (1 - avg_spread) * 0.50
        return max(0.0, estimated_daily)


# ---------------------------------------------------------------------------
# Strategy #36: Volatility Tracker for Spread Adjustment
# ---------------------------------------------------------------------------

class VolatilityTracker:
    """Track price volatility for dynamic spread adjustment.

    Calculates rolling volatility from price changes to inform spread
    widening during high-volatility periods.
    """

    def __init__(
        self,
        lookback_seconds: float = 300.0,
        min_samples: int = 5,
    ):
        """Initialize the volatility tracker.

        Args:
            lookback_seconds: Rolling window for volatility calculation.
            min_samples: Minimum samples required for valid volatility.
        """
        self.lookback_seconds = lookback_seconds
        self.min_samples = min_samples
        self._prices: dict[str, list[tuple[float, float]]] = {}
        self._lock = threading.Lock()

    def record_price(self, market_key: str, price: float) -> None:
        """Record a price observation.

        Args:
            market_key: Market identifier.
            price: Current price (0-1).
        """
        now = time.time()
        with self._lock:
            if market_key not in self._prices:
                self._prices[market_key] = []
            self._prices[market_key].append((now, price))
            cutoff = now - self.lookback_seconds
            self._prices[market_key] = [
                (t, p) for t, p in self._prices[market_key]
                if t > cutoff
            ]

    def get_volatility(self, market_key: str) -> float:
        """Calculate rolling volatility for a market.

        Returns:
            Volatility as standard deviation of price changes (0-1).
            Returns 0.0 if insufficient data.
        """
        with self._lock:
            samples = self._prices.get(market_key, [])
            if len(samples) < self.min_samples:
                return 0.0

            prices = [p for _, p in samples]
            if len(prices) < 2:
                return 0.0

            returns = [
                prices[i] - prices[i - 1]
                for i in range(1, len(prices))
            ]

            if not returns:
                return 0.0

            mean_return = sum(returns) / len(returns)
            variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
            return variance ** 0.5

    def has_min_samples(self, market_key: str) -> bool:
        """True once enough price samples exist for a trustworthy reading.

        ``get_volatility`` returns 0.0 (interpreted by callers as "calm")
        both when a market is genuinely calm AND when there simply isn't
        enough data yet (fewer than ``min_samples`` observations) — those
        are not the same thing. A safety gate that treats "unknown" as
        "calmest possible" during warm-up would let a fast-moving market
        quote at the base spread before there is any data to judge it by.
        Callers that must fail closed on "unknown" (e.g. the MM pilot's G8
        volatility ceiling) check this before trusting the multiplier.
        """
        with self._lock:
            return len(self._prices.get(market_key, [])) >= self.min_samples

    def get_spread_multiplier(
        self,
        market_key: str,
        base_multiplier: float = 1.0,
        max_multiplier: float = 3.0,
    ) -> float:
        """Calculate spread multiplier based on current volatility.

        Higher volatility -> wider spread to compensate for adverse selection risk.

        Args:
            market_key: Market identifier.
            base_multiplier: Base multiplier (usually 1.0).
            max_multiplier: Maximum spread multiplier.

        Returns:
            Spread multiplier (1.0 to max_multiplier).
        """
        from config import MM_VOLATILITY_ADJUSTED_ENABLED, MM_VOLATILITY_SPREAD_MULTIPLIER

        if not MM_VOLATILITY_ADJUSTED_ENABLED:
            return base_multiplier

        vol = self.get_volatility(market_key)
        vol_threshold = 0.01

        if vol <= vol_threshold:
            return base_multiplier

        multiplier = base_multiplier + (vol - vol_threshold) * MM_VOLATILITY_SPREAD_MULTIPLIER * 100
        return min(multiplier, max_multiplier)

    def is_high_volatility(self, market_key: str, threshold: float = 0.03) -> bool:
        """Check if a market is in high volatility mode."""
        return self.get_volatility(market_key) > threshold


# ---------------------------------------------------------------------------
# Strategy #37: Lead-Lag Market Making
# ---------------------------------------------------------------------------

class LeadLagMM:
    """Market make on lagging platforms using leader's price as fair value.

    Identifies which platform leads price discovery and uses that price
    to quote on slower platforms.
    """

    def __init__(self):
        self._last_updates: dict[str, dict[str, float]] = {}
        self._prices: dict[str, dict[str, float]] = {}
        self._lock = threading.Lock()

    def record_update(
        self,
        market_key: str,
        platform: str,
        price: float,
    ) -> None:
        """Record a price update from a platform.

        Args:
            market_key: Market identifier.
            platform: Platform name.
            price: Current price.
        """
        now = time.time()
        with self._lock:
            if market_key not in self._last_updates:
                self._last_updates[market_key] = {}
                self._prices[market_key] = {}
            self._last_updates[market_key][platform] = now
            self._prices[market_key][platform] = price

    def get_leader(self, market_key: str) -> str | None:
        """Identify the leading platform for a market.

        The leader is the platform that updated most recently.

        Returns:
            Platform name of the leader, or None.
        """
        from config import LEAD_LAG_MM_ENABLED

        if not LEAD_LAG_MM_ENABLED:
            return None

        with self._lock:
            updates = self._last_updates.get(market_key, {})
            if not updates:
                return None
            return max(updates.keys(), key=lambda p: updates[p])

    def get_lag_ms(self, market_key: str, platform: str) -> float:
        """Get lag in milliseconds for a platform relative to leader.

        Args:
            market_key: Market identifier.
            platform: Platform to check lag for.

        Returns:
            Lag in milliseconds, or 0 if platform is leader.
        """
        leader = self.get_leader(market_key)
        if not leader or leader == platform:
            return 0.0

        with self._lock:
            leader_time = self._last_updates.get(market_key, {}).get(leader, 0)
            platform_time = self._last_updates.get(market_key, {}).get(platform, 0)
            return max(0, (leader_time - platform_time) * 1000)

    def get_fair_value(self, market_key: str) -> float | None:
        """Get fair value from the leading platform.

        Returns:
            Leader's price as fair value, or None.
        """
        leader = self.get_leader(market_key)
        if not leader:
            return None

        with self._lock:
            return self._prices.get(market_key, {}).get(leader)

    def should_quote(
        self,
        market_key: str,
        platform: str,
        min_lag_ms: float = 500.0,
    ) -> bool:
        """Check if we should quote on a lagging platform.

        Only quote if the platform lags the leader by at least min_lag_ms.

        Args:
            market_key: Market identifier.
            platform: Platform to check.
            min_lag_ms: Minimum lag to trigger quoting.

        Returns:
            True if platform lags enough to quote.
        """
        from config import LEAD_LAG_MM_ENABLED, LEAD_LAG_MIN_DELAY_MS

        if not LEAD_LAG_MM_ENABLED:
            return False

        min_lag_ms = min_lag_ms or LEAD_LAG_MIN_DELAY_MS
        lag = self.get_lag_ms(market_key, platform)
        return lag >= min_lag_ms


# ---------------------------------------------------------------------------
# Strategy #38: Toxic Flow Detector
# ---------------------------------------------------------------------------

class ToxicFlowDetector:
    """Detect adverse selection and pause quoting.

    Tracks fill patterns to identify when informed traders are
    picking off our quotes, and pauses quoting when toxicity is high.
    """

    def __init__(
        self,
        lookback_trades: int = 20,
        toxicity_threshold: float = 0.60,
        decay_half_life_seconds: float = 60.0,
        toxicity_spread_factor: float = 1.5,
        size_taper_factor: float = 0.8,
        min_size_fraction: float = 0.2,
        fill_velocity_window_seconds: float = 30.0,
        fill_velocity_burst_threshold: int = 3,
        time_fn=time.time,
    ):
        """Initialize the toxic flow detector.

        Args:
            lookback_trades: Number of recent trades to analyze.
            toxicity_threshold: Threshold above which to pause (0-1).
            decay_half_life_seconds: Half-life in seconds for exponential decay of fills.
            toxicity_spread_factor: Scaling factor for spread widening (default 1.5).
            size_taper_factor: Scaling factor for quote size reduction (default 0.8).
            min_size_fraction: Minimum quote size floor fraction (default 0.2).
            fill_velocity_window_seconds: Sliding window for burst detection (default 30s).
            fill_velocity_burst_threshold: Fill count in window triggering burst penalty.
            time_fn: Clock function for deterministic testing.
        """
        self.lookback_trades = lookback_trades
        self.toxicity_threshold = toxicity_threshold
        self.decay_half_life_seconds = decay_half_life_seconds
        self.toxicity_spread_factor = toxicity_spread_factor
        self.size_taper_factor = size_taper_factor
        self.min_size_fraction = min_size_fraction
        self.fill_velocity_window_seconds = fill_velocity_window_seconds
        self.fill_velocity_burst_threshold = fill_velocity_burst_threshold
        self._time_fn = time_fn
        self._fills: dict[str, list[dict]] = {}
        self._pause_until: dict[str, float] = {}
        self._pause_reasons: dict[str, str] = {}
        self._lock = threading.Lock()

    def record_fill(
        self,
        market_key: str,
        side: str,
        price: float,
        size: float,
        mid_at_fill: float,
        timestamp: float | None = None,
    ) -> None:
        """Record a fill for toxicity analysis.

        Args:
            market_key: Market identifier.
            side: "bid" or "ask" (which side got filled).
            price: Fill price.
            size: Fill size in dollars.
            mid_at_fill: Mid price at time of fill.
            timestamp: Optional fill timestamp (defaults to time_fn()).
        """
        now = timestamp if timestamp is not None else self._time_fn()
        with self._lock:
            if market_key not in self._fills:
                self._fills[market_key] = []

            adverse = False
            if side == "bid" and mid_at_fill < price:
                adverse = True
            elif side == "ask" and mid_at_fill > price:
                adverse = True

            self._fills[market_key].append({
                "timestamp": now,
                "side": side,
                "price": price,
                "size": size,
                "mid": mid_at_fill,
                "adverse": adverse,
            })

            self._fills[market_key] = self._fills[market_key][-self.lookback_trades:]

    def get_toxicity(self, market_key: str, now: float | None = None) -> float:
        """Calculate time-decayed toxicity ratio for a market.

        Toxicity = (weighted adverse fills) / (total weighted fills)
        where each fill is weighted by exponential decay with half-life
        `decay_half_life_seconds`.

        Returns:
            Toxicity ratio (0-1). Higher = more informed flow.
        """
        now_val = now if now is not None else self._time_fn()
        with self._lock:
            fills = self._fills.get(market_key, [])
            if len(fills) < 3:
                return 0.0

            if self.decay_half_life_seconds and self.decay_half_life_seconds > 0:
                weighted_adverse = 0.0
                weighted_total = 0.0
                for f in fills:
                    dt = max(0.0, now_val - float(f.get("timestamp", now_val)))
                    weight = 2.0 ** (-dt / self.decay_half_life_seconds)
                    weighted_total += weight
                    if f.get("adverse"):
                        weighted_adverse += weight

                if weighted_total <= 0.0:
                    return 0.0
                return round(min(1.0, weighted_adverse / weighted_total), 4)

            adverse_count = sum(1 for f in fills if f.get("adverse"))
            return round(adverse_count / len(fills), 4)

    def get_fill_velocity(
        self,
        market_key: str,
        window_seconds: float | None = None,
        now: float | None = None,
    ) -> int:
        """Count fills that occurred within the trailing window."""
        window = (
            window_seconds
            if window_seconds is not None
            else self.fill_velocity_window_seconds
        )
        now_val = now if now is not None else self._time_fn()
        with self._lock:
            fills = self._fills.get(market_key, [])
            if not fills or window <= 0:
                return 0
            cutoff = now_val - window
            return sum(1 for f in fills if f.get("timestamp", 0) >= cutoff)

    def is_velocity_burst(
        self,
        market_key: str,
        window_seconds: float | None = None,
        burst_threshold: int | None = None,
        now: float | None = None,
    ) -> bool:
        """Check if recent fill rate constitutes an adverse velocity burst."""
        threshold = (
            burst_threshold
            if burst_threshold is not None
            else self.fill_velocity_burst_threshold
        )
        return self.get_fill_velocity(market_key, window_seconds=window_seconds, now=now) >= threshold

    def get_spread_multiplier(
        self,
        market_key: str,
        toxicity_spread_factor: float | None = None,
        now: float | None = None,
    ) -> float:
        """Calculate dynamic quote spread multiplier based on toxicity and fill velocity.

        Multiplier = 1.0 + (toxicity * factor) (+ 0.5 burst if velocity spikes).

        Returns:
            Spread multiplier (>= 1.0).
        """
        from config import MM_TOXIC_FLOW_ENABLED

        if not MM_TOXIC_FLOW_ENABLED:
            return 1.0

        factor = (
            toxicity_spread_factor
            if toxicity_spread_factor is not None
            else self.toxicity_spread_factor
        )
        toxicity = self.get_toxicity(market_key, now=now)
        burst = self.is_velocity_burst(market_key, now=now)

        mult = 1.0
        if toxicity > 0:
            mult += toxicity * factor
        if burst:
            mult += 0.5  # Widen extra 50% on aggressive sweeps

        return round(max(1.0, mult), 2)

    def get_size_multiplier(
        self,
        market_key: str,
        size_taper_factor: float | None = None,
        min_size_fraction: float | None = None,
        now: float | None = None,
    ) -> float:
        """Calculate dynamic quote size multiplier based on toxicity and fill velocity.

        Reduces quote sizes as informed flow probability rises or burst sweeps occur.

        Returns:
            Size multiplier in [min_size_fraction, 1.0].
        """
        from config import MM_TOXIC_FLOW_ENABLED

        if not MM_TOXIC_FLOW_ENABLED:
            return 1.0

        taper = (
            size_taper_factor
            if size_taper_factor is not None
            else self.size_taper_factor
        )
        min_frac = (
            min_size_fraction
            if min_size_fraction is not None
            else self.min_size_fraction
        )
        toxicity = self.get_toxicity(market_key, now=now)
        burst = self.is_velocity_burst(market_key, now=now)

        size_mult = max(min_frac, 1.0 - (toxicity * taper))
        if burst:
            size_mult = max(min_frac, size_mult * 0.5)

        return round(size_mult, 2)

    def should_pause(self, market_key: str, now: float | None = None) -> bool:
        """Check if quoting should be paused due to toxic flow.

        Args:
            market_key: Market identifier.
            now: Optional timestamp.

        Returns:
            True if quoting should be paused.
        """
        from config import MM_TOXIC_FLOW_ENABLED

        if not MM_TOXIC_FLOW_ENABLED:
            return False

        now_val = now if now is not None else self._time_fn()
        with self._lock:
            if self._pause_until.get(market_key, 0) > now_val:
                return True

        toxicity = self.get_toxicity(market_key, now=now_val)
        return toxicity >= self.toxicity_threshold

    def trigger_pause(
        self,
        market_key: str,
        pause_seconds: float | None = None,
        reason: str = "",
        now: float | None = None,
    ) -> None:
        """Trigger a quoting pause for a market.

        Args:
            market_key: Market identifier.
            pause_seconds: Duration of pause (default from config).
            reason: Optional human/system reason for the pause.
            now: Optional timestamp.
        """
        from config import MM_TOXIC_FLOW_PAUSE_SECONDS

        pause_seconds = pause_seconds or MM_TOXIC_FLOW_PAUSE_SECONDS
        now_val = now if now is not None else self._time_fn()
        toxicity = self.get_toxicity(market_key, now=now_val)
        with self._lock:
            self._pause_until[market_key] = now_val + pause_seconds
            if reason:
                self._pause_reasons[market_key] = reason
        logger.warning(
            "Toxic flow detected on %s (%.1f%%)%s, pausing for %.0fs",
            market_key, toxicity * 100, f" [{reason}]" if reason else "", pause_seconds,
        )

    def get_pause_remaining(self, market_key: str, now: float | None = None) -> float:
        """Get remaining pause time in seconds."""
        now_val = now if now is not None else self._time_fn()
        with self._lock:
            return max(0.0, self._pause_until.get(market_key, 0) - now_val)

    def get_pause_reason(self, market_key: str) -> str:
        """Get pause reason if set, or empty string."""
        with self._lock:
            return self._pause_reasons.get(market_key, "")


# Module-level instances for convenience
_volatility_tracker: VolatilityTracker | None = None
_lead_lag_mm: LeadLagMM | None = None
_toxic_flow_detector: ToxicFlowDetector | None = None


def get_volatility_tracker() -> VolatilityTracker:
    """Get or create the module-level VolatilityTracker."""
    global _volatility_tracker
    if _volatility_tracker is None:
        from config import MM_VOLATILITY_LOOKBACK_SECONDS
        _volatility_tracker = VolatilityTracker(
            lookback_seconds=MM_VOLATILITY_LOOKBACK_SECONDS,
        )
    return _volatility_tracker


def get_lead_lag_mm() -> LeadLagMM:
    """Get or create the module-level LeadLagMM."""
    global _lead_lag_mm
    if _lead_lag_mm is None:
        _lead_lag_mm = LeadLagMM()
    return _lead_lag_mm


def get_toxic_flow_detector() -> ToxicFlowDetector:
    """Get or create the module-level ToxicFlowDetector."""
    global _toxic_flow_detector
    if _toxic_flow_detector is None:
        from config import (
            MM_TOXIC_FLOW_THRESHOLD,
            MM_TOXIC_FLOW_HALF_LIFE_SECONDS,
            MM_TOXIC_SPREAD_FACTOR,
            MM_TOXIC_SIZE_TAPER_FACTOR,
            MM_TOXIC_MIN_SIZE_FRACTION,
            MM_FILL_VELOCITY_WINDOW_SECONDS,
            MM_FILL_VELOCITY_BURST_THRESHOLD,
        )
        _toxic_flow_detector = ToxicFlowDetector(
            toxicity_threshold=MM_TOXIC_FLOW_THRESHOLD,
            decay_half_life_seconds=MM_TOXIC_FLOW_HALF_LIFE_SECONDS,
            toxicity_spread_factor=MM_TOXIC_SPREAD_FACTOR,
            size_taper_factor=MM_TOXIC_SIZE_TAPER_FACTOR,
            min_size_fraction=MM_TOXIC_MIN_SIZE_FRACTION,
            fill_velocity_window_seconds=MM_FILL_VELOCITY_WINDOW_SECONDS,
            fill_velocity_burst_threshold=MM_FILL_VELOCITY_BURST_THRESHOLD,
        )
    return _toxic_flow_detector
