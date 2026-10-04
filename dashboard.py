"""Lightweight HTTP dashboard for scanner status.

Serves a single-page trading dashboard at GET / and JSON API endpoints
for positions, trades, opportunities, P&L history, and system health.
Optional HTTP Basic Auth when DASHBOARD_PASS is set.
"""

import base64
import hmac
import json
import logging
import os
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

logger = logging.getLogger(__name__)

# Module start time for uptime calculation
_start_time = time.monotonic()


# ---------------------------------------------------------------------------
# Rewards metrics helpers
# ---------------------------------------------------------------------------

def _estimate_reward_yield(reward_tracker) -> float:
    """Estimate daily reward yield from RewardTracker active markets.

    Args:
        reward_tracker: RewardTracker instance or None.

    Returns:
        Estimated daily yield in USDC.
    """
    if not reward_tracker:
        return 0.0

    total_yield = 0.0
    try:
        # Access reward cache safely if it exists
        if hasattr(reward_tracker, '_reward_cache'):
            for market_key, reward_data in reward_tracker._reward_cache.items():
                if isinstance(reward_data, dict):
                    pool = reward_data.get("pool_size_usdc", 0)
                    # Rough estimate: daily yield = pool_size / 30 days
                    if pool > 0:
                        daily_estimate = pool / 30
                        total_yield += daily_estimate
    except Exception as e:
        logger.debug("Error estimating reward yield: %s", e)

    return total_yield


def _calculate_total_exposure(reward_tracker) -> float:
    """Sum total capital deployed in reward resting orders.

    Args:
        reward_tracker: RewardTracker instance or None.

    Returns:
        Total exposure in USDC (0 if not tracked).
    """
    if not reward_tracker:
        return 0.0

    # Would need to track active order sizes in reward_tracker
    # For now, return 0; can be enhanced with order tracking
    return 0.0


def _build_rewards_metrics(reward_tracker) -> dict:
    """Build rewards metrics dict for /status endpoint.

    Args:
        reward_tracker: RewardTracker instance or None.

    Returns:
        Dict with rewards metrics keys, all zero if no tracker.
    """
    if not reward_tracker:
        return {
            "strategy_name": "Liquidity Rewards",
            "resting_order_count": 0,
            "estimated_daily_yield_usdc": 0.0,
            "trading_pnl": 0.0,
            "total_reward_exposure": 0.0,
        }

    return {
        "strategy_name": "Liquidity Rewards",
        "resting_order_count": getattr(reward_tracker, 'resting_order_count', 0),
        "estimated_daily_yield_usdc": round(_estimate_reward_yield(reward_tracker), 2),
        "trading_pnl": 0.0,  # Filled positions' P&L tracked separately in strategy metrics
        "total_reward_exposure": round(_calculate_total_exposure(reward_tracker), 2),
    }


def _get_mm_pilot_telemetry() -> dict:
    """Retrieve MM Pilot telemetry from active instance or persisted state file.

    Returns:
        Dict with pilot status, orders, inventory, and safety attributes.
    """
    # 1. Check in-process active pilot instance
    if state.mm_pilot is not None:
        try:
            status = state.mm_pilot.get_status()
            status["source"] = "instance"
            return status
        except Exception as e:
            logger.debug("Error getting mm_pilot status from instance: %s", e)

    # 2. Check persisted state file
    candidate_paths: list[str] = []
    if state.mm_pilot_state_path:
        candidate_paths.append(state.mm_pilot_state_path)
    env_path = os.getenv("MM_STATE_PATH")
    if env_path:
        candidate_paths.append(env_path)
    candidate_paths.append("mm_pilot_state.json")
    candidate_paths.append("/app/mm_pilot_state.json")

    for path in candidate_paths:
        try:
            norm_path = os.path.normpath(path)
            if os.path.isfile(norm_path):
                with open(norm_path, "r", encoding="utf-8") as f:
                    file_state = json.load(f)
                if isinstance(file_state, dict):
                    # Normalize orders dict to list
                    raw_orders = file_state.get("orders")
                    if isinstance(raw_orders, dict):
                        file_state["orders"] = [
                            {"order_id": oid, **info}
                            for oid, info in raw_orders.items()
                            if isinstance(info, dict)
                        ]
                        file_state["resting_orders"] = len(file_state["orders"])
                    elif isinstance(raw_orders, list):
                        file_state["resting_orders"] = len(raw_orders)
                    else:
                        file_state["orders"] = []
                        file_state["resting_orders"] = 0

                    # Derive inventory totals from snapshot if not directly provided
                    inv = file_state.get("inventory")
                    if isinstance(inv, dict):
                        net_map = inv.get("net", {}) if isinstance(inv.get("net"), dict) else {}
                        avg_map = inv.get("avg", {}) if isinstance(inv.get("avg"), dict) else {}
                        realized_map = inv.get("realized", {}) if isinstance(inv.get("realized"), dict) else {}
                        if "total_inventory_usd" not in file_state:
                            file_state["total_inventory_usd"] = sum(
                                abs(cnt) * avg_map.get(tk, 0.0)
                                for tk, cnt in net_map.items()
                                if isinstance(cnt, (int, float))
                            )
                        if "realized_pnl" not in file_state:
                            file_state["realized_pnl"] = sum(
                                val for val in realized_map.values()
                                if isinstance(val, (int, float))
                            )
                    else:
                        file_state.setdefault("total_inventory_usd", 0.0)
                        file_state.setdefault("realized_pnl", 0.0)

                    # Freshness and explicit lifecycle status check
                    saved_at = file_state.get("saved_at")
                    is_stopped = file_state.get("stopped", False)
                    is_halted = file_state.get("halted", False)
                    now = time.time()

                    if is_stopped:
                        file_state["active"] = False
                        file_state["status"] = "stopped"
                    elif is_halted:
                        file_state["active"] = False
                        file_state["status"] = "halted"
                    elif not isinstance(saved_at, (int, float)) or (now - saved_at > 120.0):
                        file_state["active"] = False
                        file_state["status"] = "stale"
                    elif "active" not in file_state or "status" not in file_state:
                        file_state["active"] = False
                        file_state["status"] = "stale"
                    elif file_state.get("active", False):
                        file_state["status"] = "active"
                    else:
                        file_state["active"] = False
                        file_state["status"] = "inactive"

                    file_state.setdefault("lip_rewards", {})
                    file_state["source"] = "file"
                    file_state["path"] = norm_path
                    return file_state
        except Exception as e:
            logger.debug("Error reading mm_pilot state from %s: %s", path, e)

    return {
        "active": False,
        "status": "inactive",
        "message": "Kalshi MM Pilot not active (no running instance or state file)",
        "lip_rewards": {},
    }


# ---------------------------------------------------------------------------
# Shared scanner state (updated by cli.py / continuous.py)
# ---------------------------------------------------------------------------

class _DashboardState:
    """Shared mutable state updated by the scanner loop."""

    def __init__(self):
        self.scan_count = 0
        self.last_scan_time = None
        self.open_positions = 0
        self.daily_pnl = 0.0
        self.ws_connections = 0
        self.opportunities_found = 0
        self.last_opportunities: list[dict] = []
        # Capital tracking (OPTIMIZE-04, OPTIMIZE-05)
        self.platform_balances: dict[str, float] = {}
        self.last_bankroll_refresh: str | None = None
        self.platform_opp_flow: dict[str, int] = {}
        # Layer 2-5 state
        self.mm_active_markets = 0
        self.mm_active_orders = 0
        self.mm_total_exposure = 0.0
        self.stale_detections = 0
        self.resolution_snipes = 0
        self.convergence_signals = 0
        self.signal_sources_active = 0
        # MM Pilot telemetry
        self.mm_pilot = None
        self.mm_pilot_state_path: str | None = None
        # Analytics (MON-01): per-strategy P&L metrics
        self.strategy_metrics: list[dict] = []
        self.platform_health: dict = {}
        # Leaderboard (MON-02): strategy leaderboard state
        self.strategy_leaderboard: list[dict] = []
        self.leaderboard_updated_at: float = 0
        # Rewards tracking (Layer 3)
        self.reward_tracker = None
        # Detection funnel telemetry (Phase 1)
        self.funnel_stats: dict[str, int] = {}

    def update_strategy_metrics(self, strategy_metrics: list[dict]) -> None:
        """Update dashboard with strategy leaderboard metrics.

        Args:
            strategy_metrics: List of strategy dicts with per-strategy metrics.
                Each dict should contain: strategy, trade_count, wins, win_rate,
                total_pnl, avg_pnl, annual_sharpe, max_drawdown.
        """
        self.strategy_leaderboard = strategy_metrics
        self.leaderboard_updated_at = time.time()
        logger.debug("Updated strategy leaderboard: %d strategies", len(strategy_metrics))

    def to_dict(self) -> dict:
        # Build rewards metrics if tracker is available
        rewards_metrics = _build_rewards_metrics(self.reward_tracker)
        mm_pilot_telemetry = _get_mm_pilot_telemetry()

        return {
            "scan_count": self.scan_count,
            "last_scan_time": self.last_scan_time,
            "open_positions": self.open_positions,
            "daily_pnl": round(self.daily_pnl, 4),
            "ws_connections": self.ws_connections,
            "opportunities_found": self.opportunities_found,
            "last_opportunities": self.last_opportunities[:20],
            "funnel_stats": self.funnel_stats,
            "mm_active_markets": self.mm_active_markets,
            "mm_active_orders": self.mm_active_orders,
            "mm_total_exposure": round(self.mm_total_exposure, 2),
            "stale_detections": self.stale_detections,
            "resolution_snipes": self.resolution_snipes,
            "convergence_signals": self.convergence_signals,
            "signal_sources_active": self.signal_sources_active,
            "strategy_metrics": self.strategy_metrics,
            "platform_health": self.platform_health,
            "rewards": rewards_metrics,
            "mm_pilot": {
                "active": mm_pilot_telemetry.get("active", False),
                "halted": mm_pilot_telemetry.get("halted", False),
                "dry_run": mm_pilot_telemetry.get("dry_run", True),
                "canary_graduated": mm_pilot_telemetry.get("canary_graduated", False),
                "canary_clean_fills": mm_pilot_telemetry.get("canary_clean_fills", 0),
                "resting_orders": mm_pilot_telemetry.get("resting_orders", 0),
                "total_inventory_usd": mm_pilot_telemetry.get("total_inventory_usd", 0.0),
                "realized_pnl": mm_pilot_telemetry.get("realized_pnl", 0.0),
                "lip_rewards": mm_pilot_telemetry.get("lip_rewards", {}),
            },
        }


# Module-level singleton so scanner can update it and the server can read it
state = _DashboardState()


# ---------------------------------------------------------------------------
# Kill switch — runtime pause/resume (thread-safe)
# ---------------------------------------------------------------------------

_pause_lock = threading.Lock()
_paused = False
_pause_reason = ""
_pause_timestamp: float | None = None


def is_paused() -> bool:
    """Check if the kill switch is engaged (trading paused)."""
    return _paused


def pause(reason: str = "manual") -> dict:
    """Engage the kill switch — stop all trade execution.

    Args:
        reason: Human-readable reason for pausing (logged and returned).

    Returns:
        Dict with pause state info.
    """
    global _paused, _pause_reason, _pause_timestamp
    with _pause_lock:
        _paused = True
        _pause_reason = reason
        _pause_timestamp = time.time()
        logger.warning("KILL SWITCH ENGAGED: trading paused (%s)", reason)
    return get_pause_state()


def resume() -> dict:
    """Disengage the kill switch — allow trade execution to continue.

    Returns:
        Dict with pause state info.
    """
    global _paused, _pause_reason, _pause_timestamp
    with _pause_lock:
        was_paused = _paused
        _paused = False
        _pause_reason = ""
        _pause_timestamp = None
        if was_paused:
            logger.warning("KILL SWITCH DISENGAGED: trading resumed")
    return get_pause_state()


def get_pause_state() -> dict:
    """Return current kill switch state as a dict."""
    return {
        "paused": _paused,
        "reason": _pause_reason,
        "paused_since": _pause_timestamp,
    }


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------

def _check_auth(handler) -> bool:
    """Verify HTTP Basic Auth credentials if DASHBOARD_PASS is set.

    Returns True if auth passes (or auth is disabled). Sends 401 and
    returns False if auth fails.
    """
    from config import DASHBOARD_USER, DASHBOARD_PASS

    if not DASHBOARD_PASS:
        # Reads are allowed without a password (local/dev convenience); the
        # dangerous state-changing POST endpoints are separately fail-closed in
        # do_POST (audit S06). Warn loudly in full-auto, where this is unsafe.
        from config import EXECUTION_MODE
        if EXECUTION_MODE == "full-auto":
            logger.warning("Dashboard auth disabled in full-auto mode — set DASHBOARD_PASS")
        return True

    auth_header = handler.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        _send_401(handler)
        return False

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        user, pwd = decoded.split(":", 1)
    except Exception as e:
        logger.debug("Dashboard auth decode failed: %s", e)
        _send_401(handler)
        return False

    # Constant-time comparison (audit S14): plain == leaks length/prefix via
    # timing, enabling offline credential enumeration on a network-reachable
    # dashboard. Compare both fields as bytes, unconditionally.
    user_ok = hmac.compare_digest(user.encode("utf-8"), (DASHBOARD_USER or "").encode("utf-8"))
    pwd_ok = hmac.compare_digest(pwd.encode("utf-8"), DASHBOARD_PASS.encode("utf-8"))
    if user_ok and pwd_ok:
        return True

    _send_401(handler)
    return False


def _send_401(handler):
    """Send a 401 Unauthorized response with WWW-Authenticate header."""
    handler.send_response(401)
    handler.send_header("WWW-Authenticate", 'Basic realm="Arb Scanner Dashboard"')
    handler.send_header("Content-Type", "text/plain")
    body = b"Unauthorized"
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _send_json(handler, data, status: int = 200):
    """Send a JSON response."""
    body = json.dumps(data, indent=2, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def _send_html(handler, html: str, status: int = 200):
    """Send an HTML response."""
    body = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


# ---------------------------------------------------------------------------
# Database helper (lazy import to avoid circular deps)
# ---------------------------------------------------------------------------

def _get_db():
    """Get a TradeDB instance. Returns None on error."""
    try:
        from db import TradeDB
        return TradeDB()
    except Exception as e:
        logger.debug("Error creating TradeDB for dashboard: %s", e)
        return None


# ---------------------------------------------------------------------------
# Request handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    """HTTP handler for dashboard UI and JSON API endpoints."""

    def do_GET(self):
        path = self.path.split("?")[0]  # Strip query string

        # Health check endpoint — no auth required (for ECS/ALB probes)
        if path == "/healthz":
            _send_json(self, {"status": "ok"})
            return

        if not _check_auth(self):
            return

        # Route dispatch
        routes = {
            "/": self._handle_dashboard,
            "/dashboard": self._handle_dashboard,
            "/status": self._handle_status,
            "/metrics": self._handle_metrics,
            "/alerts": self._handle_alerts,
            "/api/health": self._handle_health,
            "/api/positions": self._handle_positions,
            "/api/platforms": self._handle_platforms,
            "/api/trades": self._handle_trades,
            "/api/opportunities": self._handle_opportunities,
            "/api/strategies": self._handle_strategies,
            "/api/history": self._handle_history,
            "/api/slippage": self._handle_slippage,
            "/api/failures": self._handle_failures,
            "/api/pause": self._handle_pause_get,
            "/api/db-stats": self._handle_db_stats,
            "/api/strategy-pnl": self._handle_strategy_pnl,
            "/api/strategy-leaderboard": self._handle_strategy_leaderboard,
            "/api/balances": self._handle_balances,
            "/api/rebalance": self._handle_rebalance,
            "/api/validation": self._handle_validation,
            "/api/jev/calibration": self._handle_jev_calibration,
            "/api/funnel": self._handle_funnel,
            "/api/mm-pilot": self._handle_mm_pilot,
        }

        handler_fn = routes.get(path)
        if handler_fn:
            try:
                handler_fn()
            except Exception as e:
                logger.warning("Dashboard handler error on %s: %s", path, e)
                _send_json(self, {"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]

        # Fail closed BEFORE reading the body (audit S06): state-changing
        # endpoints (kill-switch, resume, purge, fund-transfer) require a
        # configured password, and an unauthenticated client must not be able to
        # make the server read an arbitrarily large POST body first.
        from config import DASHBOARD_PASS
        if not DASHBOARD_PASS:
            logger.error("Dashboard POST %s denied — DASHBOARD_PASS is not set", path)
            _send_401(self)
            return
        if not _check_auth(self):
            return

        # Read the full request body (after auth) to avoid connection resets.
        post_body = b""
        try:
            content_len = int(self.headers.get("Content-Length", 0))
            if content_len > 0:
                post_body = self.rfile.read(content_len)
        except Exception as e:
            logger.debug("Dashboard POST body read error: %s", e)

        post_routes = {
            "/api/pause": self._handle_pause_post,
            "/api/resume": self._handle_resume_post,
            "/api/purge": self._handle_purge_post,
            "/api/rebalance/execute": self._handle_rebalance_execute,
        }

        handler_fn = post_routes.get(path)
        if handler_fn:
            try:
                handler_fn(post_body)
            except Exception as e:
                logger.warning("Dashboard POST handler error on %s: %s", path, e)
                _send_json(self, {"error": str(e)}, 500)
        else:
            self.send_response(404)
            self.end_headers()

    # -------------------------------------------------------------------
    # Existing endpoints (preserved)
    # -------------------------------------------------------------------

    def _handle_dashboard(self):
        """Serve the single-page HTML dashboard."""
        from config import DASHBOARD_REFRESH_SECONDS
        from dashboard_ui import get_dashboard_html
        html = get_dashboard_html(refresh_seconds=DASHBOARD_REFRESH_SECONDS)
        _send_html(self, html)

    def _handle_status(self):
        """Scanner state JSON (existing endpoint, preserved for compatibility)."""
        _send_json(self, state.to_dict())

    def _handle_funnel(self):
        """Detection funnel telemetry endpoint (Phase 1)."""
        from funnel import get_funnel_tracker
        tracker = get_funnel_tracker()
        _send_json(self, {
            "current_cycle": tracker.current_cycle.to_dict(),
            "cumulative": tracker.cumulative.to_dict(),
            "history": tracker.cycle_history[-20:],
        })

    def _handle_mm_pilot(self):
        """Serve Kalshi MM Pilot telemetry JSON."""
        data = _get_mm_pilot_telemetry()
        _send_json(self, data)

    def _handle_metrics(self):
        """Prometheus-compatible metrics endpoint (text exposition format).

        Endpoint: GET /metrics
        Content-Type: text/plain; version=0.0.4

        Prometheus scrape config example::

            scrape_configs:
              - job_name: 'arb-scanner'
                scrape_interval: 15s
                static_configs:
                  - targets: ['<host>:<dashboard-port>']
                metrics_path: /metrics

        Available metric families:
            scans_total          — counter: total scan cycles completed
            opportunities_found  — counter: total opportunities detected
            trades_executed      — counter: total trades executed
            trades_failed        — counter: total trades that failed
            scan_duration_seconds — histogram: time per scan cycle
            net_profit_total     — gauge: cumulative realised P&L
            open_positions       — gauge: current open position count
        """
        try:
            from metrics import metrics
            body = metrics.get_prometheus_text().encode("utf-8")
        except Exception as e:
            logger.debug("Error loading metrics: %s", e)
            body = b"# metrics unavailable\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_alerts(self):
        """Recent alerts as JSON."""
        try:
            from alerting import alert_manager
            alerts = alert_manager.get_recent_alerts(50)
        except Exception as e:
            logger.debug("Error loading alerts: %s", e)
            alerts = []
        _send_json(self, alerts)

    # -------------------------------------------------------------------
    # New API endpoints
    # -------------------------------------------------------------------

    def _handle_health(self):
        """System health: mode, uptime, metrics, config summary."""
        from config import DRY_RUN, EXECUTION_MODE, MAX_TRADE_SIZE, BASE_TRADE_SIZE

        uptime = time.monotonic() - _start_time

        metrics_data = {}
        try:
            from metrics import metrics
            metrics_data = metrics.get_all()
        except Exception as e:
            logger.debug("Dashboard metrics fetch failed: %s", e)

        cumulative = 0.0
        db = _get_db()
        if db:
            try:
                cumulative = db.get_cumulative_pnl()
            except Exception as e:
                logger.debug("Dashboard cumulative PnL fetch failed: %s", e)

        _send_json(self, {
            "dry_run": DRY_RUN,
            "execution_mode": EXECUTION_MODE,
            "max_trade_size": MAX_TRADE_SIZE,
            "base_trade_size": BASE_TRADE_SIZE,
            "uptime_seconds": round(uptime, 1),
            "cumulative_pnl": cumulative,
            "metrics": metrics_data,
            "paused": _paused,
        })

    def _handle_positions(self):
        """Open positions with trade details."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            positions = db.get_open_positions()
        except Exception as e:
            logger.debug("Dashboard positions fetch failed: %s", e)
            positions = []
        _send_json(self, positions)

    def _handle_platforms(self):
        """Open positions grouped by platform."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            platforms = db.get_positions_by_platform()
        except Exception as e:
            logger.debug("Dashboard platforms fetch failed: %s", e)
            platforms = []
        _send_json(self, platforms)

    def _handle_trades(self):
        """Recent trades with opportunity context."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            trades = db.get_recent_trades(limit=100)
        except Exception as e:
            logger.debug("Dashboard trades fetch failed: %s", e)
            trades = []
        _send_json(self, trades)

    def _handle_opportunities(self):
        """Recent opportunities."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            opps = db.get_recent_opportunities(limit=100)
        except Exception as e:
            logger.debug("Dashboard opportunities fetch failed: %s", e)
            opps = []
        _send_json(self, opps)

    def _handle_strategies(self):
        """Opportunity statistics grouped by strategy type."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            stats = db.get_opportunity_stats_by_type()
        except Exception as e:
            logger.debug("Dashboard strategies fetch failed: %s", e)
            stats = []
        _send_json(self, stats)

    def _handle_history(self):
        """Daily P&L history for charting (last 30 days)."""
        db = _get_db()
        if not db:
            _send_json(self, [])
            return
        try:
            history = db.get_daily_pnl_history(days=30)
        except Exception as e:
            logger.debug("Dashboard history fetch failed: %s", e)
            history = []
        _send_json(self, history)

    def _handle_slippage(self):
        """Average slippage across all trades."""
        db = _get_db()
        if not db:
            _send_json(self, {"avg_slippage": 0.0})
            return
        try:
            avg = db.get_avg_slippage()
        except Exception as e:
            logger.debug("Dashboard slippage fetch failed: %s", e)
            avg = 0.0
        _send_json(self, {"avg_slippage": avg})

    def _handle_failures(self):
        """Failed trades with error context and failure statistics."""
        db = _get_db()
        if not db:
            _send_json(self, {"trades": [], "stats": {}})
            return
        try:
            failed_trades = db.get_failed_trades(limit=50)
        except Exception as e:
            logger.debug("Dashboard failed trades fetch failed: %s", e)
            failed_trades = []
        try:
            stats = db.get_failure_stats()
        except Exception as e:
            logger.debug("Dashboard failure stats fetch failed: %s", e)
            stats = {}
        _send_json(self, {"trades": failed_trades, "stats": stats})

    # -------------------------------------------------------------------
    # Kill switch endpoints
    # -------------------------------------------------------------------

    def _handle_db_stats(self):
        """GET /api/db-stats — row counts for all tables."""
        db = _get_db()
        if not db:
            _send_json(self, {"error": "database unavailable"}, 500)
            return
        try:
            stats = db.get_db_stats()
        except Exception as e:
            _send_json(self, {"error": str(e)}, 500)
            return
        _send_json(self, stats)

    def _handle_strategy_pnl(self):
        """GET /api/strategy-pnl — per-strategy P&L breakdown from DB.

        Returns:
            JSON with ``strategies`` key: list of dicts with strategy,
            trade_count, win_count, total_pnl, avg_profit fields.
        """
        db = _get_db()
        if not db:
            _send_json(self, {"strategies": []})
            return
        try:
            strategies = db.get_strategy_pnl()
        except Exception as e:
            logger.debug("Dashboard strategy-pnl fetch failed: %s", e)
            strategies = []
        _send_json(self, {"strategies": strategies})

    def _handle_strategy_leaderboard(self):
        """GET /api/strategy-leaderboard — strategy leaderboard with 7-day rolling metrics.

        Returns:
            JSON with ``strategies`` list (sorted by total_pnl descending),
            ``timestamp`` (last update), and ``lookback_days`` (7).
            Each strategy dict has: strategy, trade_count, wins, win_rate,
            total_pnl, avg_pnl, annual_sharpe, max_drawdown.
        """
        response = {
            "strategies": state.strategy_leaderboard,
            "timestamp": state.leaderboard_updated_at,
            "lookback_days": 7,
        }
        _send_json(self, response)

    def _handle_balances(self):
        """GET /api/balances — cached platform balances with total and timestamp.

        Returns:
            JSON with ``balances`` dict (platform -> float), ``total`` float,
            and ``last_updated`` ISO timestamp string (or null).
        """
        balances = getattr(state, "platform_balances", {})
        _send_json(self, {
            "balances": balances,
            "total": sum(v for v in balances.values() if isinstance(v, (int, float))),
            "last_updated": getattr(state, "last_bankroll_refresh", None),
        })

    def _handle_rebalance(self):
        """GET /api/rebalance — recommended capital transfers by opp-flow alignment.

        Compares each platform's current capital percentage to its share of
        historical opportunity flow. Recommends transfers when drift exceeds 5%.

        Returns:
            JSON with ``recommendations`` list and ``total_balance`` float.
            Each recommendation has: platform, current_pct, recommended_pct,
            current_balance, transfer_amount (positive = receive, negative = send).
        """
        balances = getattr(state, "platform_balances", {})
        opp_flow = getattr(state, "platform_opp_flow", {})
        total = sum(v for v in balances.values() if isinstance(v, (int, float)))
        recommendations = []
        if total > 0:
            total_opps = sum(opp_flow.values()) or 1
            for platform in set(list(balances.keys()) + list(opp_flow.keys())):
                bal = balances.get(platform, 0)
                opps = opp_flow.get(platform, 0)
                current_pct = bal / total if total > 0 else 0
                recommended_pct = opps / total_opps
                diff = recommended_pct - current_pct
                if abs(diff) > 0.05:  # only recommend if >5% drift
                    recommendations.append({
                        "platform": platform,
                        "current_pct": round(current_pct, 4),
                        "recommended_pct": round(recommended_pct, 4),
                        "current_balance": round(bal, 2),
                        "transfer_amount": round(diff * total, 2),
                    })
        _send_json(self, {"recommendations": recommendations, "total_balance": round(total, 2)})

    def _handle_validation(self):
        """GET /api/validation — run 7-day validation against success criteria.

        Queries trades.db directly for the 3 milestone success criteria:
        1. Net positive P&L
        2. <5% false positive rate
        3. At least 1 profitable round-trip trade
        """
        from datetime import datetime, timedelta, timezone
        days = 7
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        db = _get_db()
        if not db:
            _send_json(self, {"error": "No database available"}, 500)
            return

        conn = db.conn
        # Criterion 1: P&L
        row = conn.execute(
            "SELECT COALESCE(SUM(net_profit), 0) as total_pnl, COUNT(*) as count "
            "FROM opportunities WHERE timestamp >= ? AND action IN ('executed','filled','dry_run')",
            (since,)
        ).fetchone()
        total_pnl = row["total_pnl"]
        opp_count = row["count"]

        strategies = conn.execute(
            "SELECT type, SUM(net_profit) as pnl, COUNT(*) as count "
            "FROM opportunities WHERE timestamp >= ? AND action IN ('executed','filled','dry_run') "
            "GROUP BY type ORDER BY pnl DESC",
            (since,)
        ).fetchall()

        # Criterion 2: FP rate
        det_row = conn.execute(
            "SELECT COUNT(*) as detected, "
            "SUM(CASE WHEN action IN ('executed','filled') THEN 1 ELSE 0 END) as executed, "
            "SUM(CASE WHEN action LIKE 'skipped:%' OR action LIKE 'rejected:%' THEN 1 ELSE 0 END) as rejected "
            "FROM opportunities WHERE timestamp >= ?",
            (since,)
        ).fetchone()
        detected = det_row["detected"]
        rejected = det_row["rejected"]
        fp_rate = (rejected / detected * 100) if detected > 0 else 0

        # Criterion 3: Profitable round-trip
        rt_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM opportunities o "
            "JOIN trades t ON t.opportunity_id = o.id "
            "WHERE o.timestamp >= ? AND o.net_profit > 0 AND t.status = 'filled'",
            (since,)
        ).fetchone()
        profitable_roundtrips = rt_row["cnt"]

        # Trade stats
        trade_row = conn.execute(
            "SELECT COUNT(*) as total, "
            "SUM(CASE WHEN status='filled' THEN 1 ELSE 0 END) as filled, "
            "SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed "
            "FROM trades WHERE timestamp >= ?",
            (since,)
        ).fetchone()

        c1_pass = total_pnl > 0
        c2_pass = fp_rate < 5
        c3_pass = profitable_roundtrips > 0

        _send_json(self, {
            "period_days": days,
            "criteria": {
                "1_net_positive_pnl": {
                    "passed": c1_pass,
                    "total_pnl": round(total_pnl, 4),
                    "opportunity_count": opp_count,
                    "strategies": [{"type": s["type"], "pnl": round(s["pnl"], 4), "count": s["count"]} for s in strategies],
                },
                "2_fp_rate_under_5pct": {
                    "passed": c2_pass,
                    "detected": detected,
                    "executed": det_row["executed"],
                    "rejected": rejected,
                    "fp_rate_pct": round(fp_rate, 2),
                },
                "3_profitable_roundtrip": {
                    "passed": c3_pass,
                    "profitable_count": profitable_roundtrips,
                },
            },
            "trades": {
                "total": trade_row["total"],
                "filled": trade_row["filled"],
                "failed": trade_row["failed"],
                "success_rate_pct": round(trade_row["filled"] / trade_row["total"] * 100, 1) if trade_row["total"] > 0 else 0,
            },
            "overall_pass": c1_pass and c2_pass and c3_pass,
            "milestone_status": "ACHIEVED" if (c1_pass and c2_pass and c3_pass) else "NOT ACHIEVED",
        })

    def _handle_jev_calibration(self):
        """GET /api/jev/calibration — empirical calibration report for Jev decisions."""
        db = _get_db()
        if not db:
            _send_json(self, {"error": "No database available"}, 500)
            return

        try:
            from jev_calibration import generate_calibration_report
            asset = "all"
            if "?" in self.path:
                query_str = self.path.split("?", 1)[1]
                for part in query_str.split("&"):
                    if part.startswith("asset="):
                        asset = part.split("=", 1)[1]
            report = generate_calibration_report(db, asset=asset)
            _send_json(self, report)
        except Exception as e:
            logger.warning("Error generating Jev calibration report: %s", e)
            _send_json(self, {"error": str(e)}, 500)
        finally:
            db.close()

    def _handle_pause_get(self):
        """GET /api/pause — return current kill switch state."""
        _send_json(self, get_pause_state())

    def _handle_pause_post(self, body: bytes = b""):
        """POST /api/pause — engage the kill switch."""
        reason = "dashboard"
        try:
            if body:
                parsed = json.loads(body)
                reason = parsed.get("reason", "dashboard")
        except Exception as e:
            logger.debug("Dashboard pause body parse failed: %s", e)
        _send_json(self, pause(reason))

    def _handle_resume_post(self, body: bytes = b""):
        """POST /api/resume — disengage the kill switch."""
        _send_json(self, resume())

    def _handle_rebalance_execute(self, body: bytes = b""):
        """POST /api/rebalance/execute — programmatic fund transfer (Strategy #18).

        Request body: {"from": "gemini", "to": "polymarket", "amount": 100.0,
                       "idempotency_key": "<optional>"}

        Only Gemini↔Polymarket corridors are supported. All other platforms
        return 400 with an explanation. Risk gates (daily limit, kill switch,
        min amount, feature flag) are enforced inside ``treasury.execute_transfer``.
        """
        try:
            parsed = json.loads(body) if body else {}
        except json.JSONDecodeError:
            _send_json(self, {"error": "Invalid JSON"}, 400)
            return

        from_platform = (parsed.get("from") or "").lower()
        to_platform = (parsed.get("to") or "").lower()
        amount_raw = parsed.get("amount")
        if not from_platform or not to_platform or amount_raw is None:
            _send_json(self, {"error": "from, to, amount required"}, 400)
            return
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            _send_json(self, {"error": "amount must be numeric"}, 400)
            return

        idempotency_key = parsed.get("idempotency_key")

        # Lazy-build a TreasuryManager so the dashboard module imports stay
        # cheap and the gemini client is only constructed when the endpoint
        # is actually hit.
        try:
            from treasury import TreasuryManager
            from gemini_api import GeminiClient
            from db import TradeDB
            import config as cfg
            db = _get_db() or TradeDB()
            gemini = None
            try:
                gemini = GeminiClient()
            except Exception as exc:
                logger.warning("Gemini client init failed: %s", exc)
            tm = TreasuryManager(
                db=db,
                gemini_client=gemini,
                kill_switch=lambda: state.scan_count >= 0 and is_paused(),
                dry_run=cfg.DRY_RUN,
            )
            result = tm.execute_transfer(
                from_platform=from_platform,
                to_platform=to_platform,
                amount_usd=amount,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            logger.exception("Rebalance execute failed: %s", exc)
            _send_json(self, {"ok": False, "error": str(exc)}, 500)
            return

        status_code = 200 if result.ok else 400
        _send_json(self, result.to_dict(), status_code)

    def _handle_purge_post(self, body: bytes = b""):
        """POST /api/purge — delete all opportunities/trades for a given type.

        Request body: {"type": "SpreadKalshi"}
        """
        try:
            if not body:
                _send_json(self, {"error": "Request body required with 'type' field"}, 400)
                return
            parsed = json.loads(body)
            opp_type = parsed.get("type", "")
            if not opp_type:
                _send_json(self, {"error": "'type' field is required"}, 400)
                return
        except json.JSONDecodeError:
            _send_json(self, {"error": "Invalid JSON"}, 400)
            return

        db = _get_db()
        if not db:
            _send_json(self, {"error": "database unavailable"}, 500)
            return
        try:
            result = db.purge_opportunities_by_type(opp_type)
            _send_json(self, {"purged": result, "type": opp_type})
        except Exception as e:
            _send_json(self, {"error": str(e)}, 500)

    # -------------------------------------------------------------------

    def log_message(self, format, *args):
        # Suppress default stderr logging from BaseHTTPRequestHandler
        logger.debug("Dashboard request: %s", args[0] if args else "")


# ---------------------------------------------------------------------------
# Server startup
# ---------------------------------------------------------------------------

def start_dashboard(port: int) -> HTTPServer | None:
    """Start the dashboard HTTP server on a background thread.

    Bind interface is governed by config.DASHBOARD_HOST (default 127.0.0.1).
    Production deploys must set DASHBOARD_HOST=0.0.0.0 + DASHBOARD_PASS.

    Args:
        port: TCP port to listen on. If 0 or negative, returns None.

    Returns:
        The HTTPServer instance (call .shutdown() to stop) or None.
    """
    if port <= 0:
        return None

    from config import DASHBOARD_HOST
    try:
        server = HTTPServer((DASHBOARD_HOST, port), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        logger.info("Dashboard running on http://%s:%d", DASHBOARD_HOST, port)
        return server
    except OSError as e:
        logger.warning("Failed to start dashboard on %s:%d: %s",
                       DASHBOARD_HOST, port, e)
        return None
