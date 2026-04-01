"""
TradeMonitorAgent — Real-time P&L monitoring and loss analysis agent.

Runs alongside the bot, analyzing every closed trade to:
1. Categorize loss causes (wrong direction, noise stopout, weak setup, etc.)
2. Detect loss streaks and drawdown patterns
3. Track per-setup, per-symbol, per-side performance over time
4. Generate actionable recommendations
5. Maintain a running loss report for dashboard display

All state persists to disk and survives restarts.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).resolve().parent.parent / "storage"
_MONITOR_FILE = _STORAGE_DIR / "trade_monitor.json"

# Loss categories
LOSS_WRONG_DIRECTION = "wrong_direction"       # Shorting uptrend / longing downtrend
LOSS_NOISE_STOPOUT = "noise_stopout"           # SL too tight, stopped by noise
LOSS_WEAK_SETUP = "weak_setup"                 # Low confidence / Grade C
LOSS_POST_TP1_REVERSAL = "post_tp1_reversal"   # Hit TP1 then reversed to SL
LOSS_WIDE_SL = "wide_sl_loss"                  # Proper SL but wrong trade
LOSS_EXPIRED = "expired"                       # Signal timed out
LOSS_FAST_STOP = "fast_stop"                   # Stopped within 2 minutes
LOSS_HIGH_CONF_FAIL = "high_conf_failure"      # High confidence (75+) but lost

# Severity levels
SEV_LOW = "low"
SEV_MEDIUM = "medium"
SEV_HIGH = "high"
SEV_CRITICAL = "critical"


class TradeMonitorAgent:
    """Monitors every trade closure and maintains loss analysis reports."""

    def __init__(self) -> None:
        _STORAGE_DIR.mkdir(parents=True, exist_ok=True)

        # ── Trade analysis history ──
        self._analyzed_trades: List[Dict[str, Any]] = []  # All analyzed trades
        self._loss_log: List[Dict[str, Any]] = []          # Loss-only log with causes

        # ── Running metrics ──
        self._metrics: Dict[str, Any] = {
            "total_analyzed": 0,
            "total_wins": 0,
            "total_losses": 0,
            "current_streak": 0,         # positive = win streak, negative = loss streak
            "max_win_streak": 0,
            "max_loss_streak": 0,
            "rolling_20_wr": 0.0,        # Win rate of last 20 trades
            "rolling_20_pnl": 0.0,       # PnL of last 20 trades
            "peak_balance": 1000.0,
            "current_drawdown": 0.0,
            "max_drawdown": 0.0,
            "paper_balance": 1000.0,
        }

        # ── Loss cause counters ──
        self._loss_causes: Dict[str, int] = defaultdict(int)

        # ── Per-setup loss tracking ──
        self._setup_losses: Dict[str, Dict[str, Any]] = {}

        # ── Per-side tracking ──
        self._side_stats: Dict[str, Dict[str, Any]] = {
            "long": {"wins": 0, "losses": 0, "pnl": 0.0},
            "short": {"wins": 0, "losses": 0, "pnl": 0.0},
        }

        # ── Active recommendations ──
        self._recommendations: List[Dict[str, Any]] = []

        # ── Hourly performance ──
        self._hourly_stats: Dict[int, Dict[str, Any]] = {
            h: {"wins": 0, "losses": 0, "pnl": 0.0} for h in range(24)
        }

        # ── Seen trade IDs (avoid double-processing, bounded to prevent memory leak) ──
        self._seen_ids: set = set()
        self._MAX_SEEN_IDS = 2000  # auto-evict oldest when exceeded

        # ── Recent trades buffer for rolling stats ──
        self._recent_trades: deque = deque(maxlen=50)

        self._load()

    # ------------------------------------------------------------------
    # Public API: Called by orchestrator on every trade closure
    # ------------------------------------------------------------------

    def analyze_trade(self, closed_signal: Dict[str, Any]) -> Dict[str, Any]:
        """Analyze a single closed trade and return the analysis result.

        Called by the orchestrator whenever a signal hits SL, TP3, or expires.
        Returns a dict with loss_causes, severity, recommendations.
        """
        trade_id = closed_signal.get("trade_id", "")
        if trade_id in self._seen_ids:
            return {"status": "already_analyzed"}
        self._seen_ids.add(trade_id)
        # Prevent unbounded growth
        if len(self._seen_ids) > self._MAX_SEEN_IDS:
            # Remove oldest entries (set is unordered, but this prevents OOM)
            excess = len(self._seen_ids) - self._MAX_SEEN_IDS
            for _ in range(excess):
                self._seen_ids.pop()

        pnl = float(closed_signal.get("pnl_pct", 0))
        pnl_usd = float(closed_signal.get("pnl_usd", 0))
        is_win = pnl > 0
        symbol = closed_signal.get("symbol", "")
        side = closed_signal.get("side", "long")
        setup = closed_signal.get("setup_type", "") or "(investment)"
        confidence = int(closed_signal.get("confidence", 0))
        grade = closed_signal.get("grade", "")
        status = closed_signal.get("status", "")
        entry_price = float(closed_signal.get("entry_price", 0))
        stop_loss = float(closed_signal.get("stop_loss", 0))
        exit_price = float(closed_signal.get("exit_price", 0))
        tp1 = float(closed_signal.get("tp1", 0))
        tp1_hit = closed_signal.get("tp1_hit", False)
        tp2_hit = closed_signal.get("tp2_hit", False)
        tp3_hit = closed_signal.get("tp3_hit", False)
        entry_time = closed_signal.get("entry_time", "")
        exit_time = closed_signal.get("exit_time", "")
        leverage = int(closed_signal.get("leverage", 1))
        position_size = float(closed_signal.get("position_size_usd", 0))

        # Calculate derived metrics
        sl_dist_pct = abs(entry_price - stop_loss) / entry_price * 100 if entry_price > 0 else 0
        tp1_dist_pct = abs(tp1 - entry_price) / entry_price * 100 if entry_price > 0 and tp1 > 0 else 0
        duration_sec = self._calc_duration(entry_time, exit_time)
        hour_utc = self._extract_hour(entry_time)

        # ── Build analysis ──
        analysis = {
            "trade_id": trade_id,
            "symbol": symbol,
            "side": side,
            "setup": setup,
            "confidence": confidence,
            "grade": grade,
            "leverage": leverage,
            "position_size_usd": position_size,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_loss": stop_loss,
            "tp1": tp1,
            "sl_dist_pct": round(sl_dist_pct, 4),
            "tp1_dist_pct": round(tp1_dist_pct, 4),
            "pnl_pct": round(pnl, 4),
            "pnl_usd": round(pnl_usd, 2),
            "status": status,
            "duration_sec": duration_sec,
            "hour_utc": hour_utc,
            "is_win": is_win,
            "tp1_hit": tp1_hit,
            "tp2_hit": tp2_hit,
            "tp3_hit": tp3_hit,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # ── Categorize loss causes ──
        loss_causes = []
        severity = SEV_LOW

        if not is_win:
            loss_causes, severity = self._categorize_loss(
                side=side, setup=setup, confidence=confidence, grade=grade,
                sl_dist_pct=sl_dist_pct, duration_sec=duration_sec,
                tp1_hit=tp1_hit, status=status, pnl=pnl, pnl_usd=pnl_usd,
            )
            analysis["loss_causes"] = loss_causes
            analysis["severity"] = severity

            # Update loss counters
            for cause in loss_causes:
                self._loss_causes[cause] += 1

            # Add to loss log
            self._loss_log.append(analysis)
            self._loss_log = self._loss_log[-200:]  # keep last 200

        # ── Update running metrics ──
        self._update_metrics(is_win, pnl, pnl_usd, side, setup, hour_utc)

        # ── Generate recommendations ──
        self._generate_recommendations()

        # ── Store ──
        self._analyzed_trades.append(analysis)
        self._analyzed_trades = self._analyzed_trades[-500:]  # keep last 500
        self._recent_trades.append(analysis)

        # Persist
        self._save()

        # Log
        if is_win:
            logger.info(
                "📊 MONITOR: WIN %s %s %s | PnL=%+.4f%% ($%+.2f) | streak=%+d",
                symbol, side, setup, pnl, pnl_usd, self._metrics["current_streak"],
            )
        else:
            logger.warning(
                "📊 MONITOR: LOSS %s %s %s | PnL=%+.4f%% ($%+.2f) | causes=%s | severity=%s | streak=%+d",
                symbol, side, setup, pnl, pnl_usd,
                ",".join(loss_causes), severity, self._metrics["current_streak"],
            )

        return analysis

    def bulk_analyze(self, closed_signals: List[Dict[str, Any]]) -> None:
        """Analyze a batch of already-closed signals (for catching up after restart)."""
        for sig in closed_signals:
            self.analyze_trade(sig)

    def should_pause_trading(self) -> Tuple[bool, str]:
        """No pausing — all signals fire. Confidence scoring handles quality."""
        return False, ""

    # ------------------------------------------------------------------
    # Loss categorization
    # ------------------------------------------------------------------

    def _categorize_loss(
        self, *, side: str, setup: str, confidence: int, grade: str,
        sl_dist_pct: float, duration_sec: int, tp1_hit: bool,
        status: str, pnl: float, pnl_usd: float,
    ) -> Tuple[List[str], str]:
        """Determine why a trade lost. Returns (causes, severity)."""
        causes = []
        severity = SEV_LOW

        # 1. Fast stopout (< 2 minutes)
        if duration_sec > 0 and duration_sec < 120 and status == "stopped":
            causes.append(LOSS_FAST_STOP)
            severity = SEV_MEDIUM

        # 2. Noise stopout (SL < 0.10%, old-style)
        if sl_dist_pct < 0.10 and status == "stopped":
            causes.append(LOSS_NOISE_STOPOUT)
            severity = SEV_MEDIUM

        # 3. Post-TP1 reversal
        if tp1_hit and status in ("partial_win", "stopped"):
            # If PnL is negative despite hitting TP1, it's a reversal
            if pnl <= 0:
                causes.append(LOSS_POST_TP1_REVERSAL)
                severity = SEV_MEDIUM

        # 4. Weak setup (Grade C or low confidence)
        if grade in ("C", "D", "F") or confidence < 55:
            causes.append(LOSS_WEAK_SETUP)
            if not causes or causes == [LOSS_WEAK_SETUP]:
                severity = SEV_LOW

        # 5. High confidence failure (75+)
        if confidence >= 75:
            causes.append(LOSS_HIGH_CONF_FAIL)
            severity = SEV_HIGH

        # 6. Wide SL loss (proper SL >= 0.10% but still lost)
        if sl_dist_pct >= 0.10 and status == "stopped" and not tp1_hit:
            causes.append(LOSS_WIDE_SL)
            if confidence >= 75:
                severity = SEV_HIGH

        # 7. Wrong direction heuristic
        # If loss is significantly worse than SL distance, price moved hard against
        if abs(pnl) > sl_dist_pct * 1.5 and status == "stopped":
            causes.append(LOSS_WRONG_DIRECTION)
            severity = SEV_HIGH

        # 8. Expired
        if status == "expired":
            causes.append(LOSS_EXPIRED)
            severity = SEV_LOW

        # If no specific cause found
        if not causes:
            causes.append(LOSS_WIDE_SL)

        # Escalate to critical if dollar loss is large
        if pnl_usd < -2.0:
            severity = SEV_CRITICAL

        return causes, severity

    # ------------------------------------------------------------------
    # Metrics update
    # ------------------------------------------------------------------

    def _update_metrics(
        self, is_win: bool, pnl: float, pnl_usd: float,
        side: str, setup: str, hour: int,
    ) -> None:
        """Update all running metrics."""
        m = self._metrics
        m["total_analyzed"] += 1

        if is_win:
            m["total_wins"] += 1
            if m["current_streak"] >= 0:
                m["current_streak"] += 1
            else:
                m["current_streak"] = 1
        else:
            m["total_losses"] += 1
            if m["current_streak"] <= 0:
                m["current_streak"] -= 1
            else:
                m["current_streak"] = -1

        m["max_win_streak"] = max(m["max_win_streak"], m["current_streak"])
        m["max_loss_streak"] = min(m["max_loss_streak"], m["current_streak"])

        # Paper balance tracking
        m["paper_balance"] += pnl_usd
        if m["paper_balance"] > m["peak_balance"]:
            m["peak_balance"] = m["paper_balance"]
        m["current_drawdown"] = round(
            (m["peak_balance"] - m["paper_balance"]) / m["peak_balance"] * 100
            if m["peak_balance"] > 0 else 0, 2
        )
        m["max_drawdown"] = max(m["max_drawdown"], m["current_drawdown"])

        # Rolling 20-trade stats
        recent = list(self._recent_trades)[-20:]
        if recent:
            wins_20 = sum(1 for t in recent if t["is_win"])
            m["rolling_20_wr"] = round(wins_20 / len(recent) * 100, 1)
            m["rolling_20_pnl"] = round(sum(t["pnl_pct"] for t in recent), 4)

        # Sharpe and Sortino ratios (annualized, using all recent_trades pnl_pct)
        all_recent = list(self._recent_trades)
        if len(all_recent) >= 5:
            pnl_returns = [t["pnl_pct"] for t in all_recent]
            n = len(pnl_returns)
            mean_ret = sum(pnl_returns) / n
            # Standard deviation (sample, ddof=1 for small samples)
            ddof = 1 if n > 1 else 0
            variance = sum((r - mean_ret) ** 2 for r in pnl_returns) / max(1, n - ddof)
            std_ret = math.sqrt(variance) if variance > 0 else 0.0
            # Sharpe ratio: mean / std * sqrt(trades_per_year)
            # Using trades/day * 365 instead of hardcoded 252
            trades_per_year = max(1, n / max(1, (all_recent[-1].get("duration_sec", 86400) or 86400))) * 365
            annualization = math.sqrt(min(trades_per_year, 10000))  # cap to avoid explosion
            if std_ret > 0:
                m["sharpe_ratio"] = round(mean_ret / std_ret * annualization, 3)
            else:
                m["sharpe_ratio"] = 0.0
            # Sortino ratio: mean / downside_std * sqrt(252)
            neg_returns = [r for r in pnl_returns if r < 0]
            if neg_returns:
                down_var = sum(r ** 2 for r in neg_returns) / len(pnl_returns)
                down_std = math.sqrt(down_var) if down_var > 0 else 0.0
                if down_std > 0:
                    m["sortino_ratio"] = round(mean_ret / down_std * math.sqrt(252), 3)
                else:
                    m["sortino_ratio"] = 0.0
            else:
                m["sortino_ratio"] = 0.0  # no negative returns
        else:
            m["sharpe_ratio"] = 0.0
            m["sortino_ratio"] = 0.0

        # Side stats
        if side in self._side_stats:
            ss = self._side_stats[side]
            if is_win:
                ss["wins"] += 1
            else:
                ss["losses"] += 1
            ss["pnl"] = round(ss["pnl"] + pnl, 4)

        # Setup losses
        if setup not in self._setup_losses:
            self._setup_losses[setup] = {
                "wins": 0, "losses": 0, "total_pnl": 0.0,
                "total_pnl_usd": 0.0, "avg_loss": 0.0,
                "worst_loss": 0.0, "last_5": [],
            }
        sl = self._setup_losses[setup]
        if is_win:
            sl["wins"] += 1
        else:
            sl["losses"] += 1
            sl["avg_loss"] = round(
                (sl["avg_loss"] * (sl["losses"] - 1) + pnl) / sl["losses"], 4
            ) if sl["losses"] > 0 else pnl
            sl["worst_loss"] = min(sl["worst_loss"], pnl)
        sl["total_pnl"] = round(sl["total_pnl"] + pnl, 4)
        sl["total_pnl_usd"] = round(sl["total_pnl_usd"] + pnl_usd, 2)
        sl["last_5"].append({"pnl": round(pnl, 4), "win": is_win})
        sl["last_5"] = sl["last_5"][-5:]

        # Hourly stats
        if 0 <= hour < 24:
            hs = self._hourly_stats[hour]
            if is_win:
                hs["wins"] += 1
            else:
                hs["losses"] += 1
            hs["pnl"] = round(hs["pnl"] + pnl, 4)

    # ------------------------------------------------------------------
    # Recommendation engine
    # ------------------------------------------------------------------

    def _generate_recommendations(self) -> None:
        """Generate actionable recommendations based on accumulated data."""
        recs = []
        m = self._metrics

        # 1. Loss streak alert
        if m["current_streak"] <= -3:
            recs.append({
                "type": "streak_alert",
                "severity": SEV_HIGH if m["current_streak"] <= -5 else SEV_MEDIUM,
                "message": f"🔴 {abs(m['current_streak'])}-trade LOSS STREAK active. Consider pausing.",
                "action": "pause_trading" if m["current_streak"] <= -5 else "reduce_size",
            })

        # 2. Drawdown alert
        if m["current_drawdown"] > 5:
            recs.append({
                "type": "drawdown_alert",
                "severity": SEV_CRITICAL if m["current_drawdown"] > 10 else SEV_HIGH,
                "message": f"📉 Drawdown at {m['current_drawdown']:.1f}% (max: {m['max_drawdown']:.1f}%)",
                "action": "reduce_leverage",
            })

        # 3. Setup-specific kills
        for setup, data in self._setup_losses.items():
            total = data["wins"] + data["losses"]
            if total >= 8:
                wr = data["wins"] / total * 100
                if wr < 25 and data["total_pnl"] < -0.3:
                    recs.append({
                        "type": "kill_setup",
                        "severity": SEV_HIGH,
                        "message": f"❌ {setup}: {wr:.0f}% WR over {total} trades (PnL: {data['total_pnl']:+.2f}%). Consider disabling.",
                        "action": f"disable_{setup}",
                        "setup": setup,
                    })

        # 4. Side imbalance
        long_s = self._side_stats.get("long", {})
        short_s = self._side_stats.get("short", {})
        long_total = long_s.get("wins", 0) + long_s.get("losses", 0)
        short_total = short_s.get("wins", 0) + short_s.get("losses", 0)

        if short_total >= 10:
            short_wr = short_s["wins"] / short_total * 100
            if short_wr < 35 and short_s["pnl"] < -0.5:
                recs.append({
                    "type": "side_warning",
                    "severity": SEV_MEDIUM,
                    "message": f"⚠️ SHORT trades underperforming: {short_wr:.0f}% WR, PnL: {short_s['pnl']:+.2f}%",
                    "action": "reduce_shorts",
                })

        if long_total >= 10:
            long_wr = long_s["wins"] / long_total * 100
            if long_wr < 35 and long_s["pnl"] < -0.5:
                recs.append({
                    "type": "side_warning",
                    "severity": SEV_MEDIUM,
                    "message": f"⚠️ LONG trades underperforming: {long_wr:.0f}% WR, PnL: {long_s['pnl']:+.2f}%",
                    "action": "reduce_longs",
                })

        # 5. Dominant loss cause
        if self._loss_causes:
            top_cause = max(self._loss_causes, key=self._loss_causes.get)
            top_count = self._loss_causes[top_cause]
            total_losses = m["total_losses"]
            if total_losses > 0 and top_count / total_losses > 0.4:
                cause_labels = {
                    LOSS_WRONG_DIRECTION: "Wrong direction (against trend)",
                    LOSS_NOISE_STOPOUT: "Noise stopouts (SL too tight)",
                    LOSS_WEAK_SETUP: "Weak setups (low confidence/grade)",
                    LOSS_POST_TP1_REVERSAL: "Post-TP1 reversals",
                    LOSS_WIDE_SL: "Proper SL but wrong trade",
                    LOSS_EXPIRED: "Expired signals",
                    LOSS_FAST_STOP: "Fast stopouts (< 2 min)",
                    LOSS_HIGH_CONF_FAIL: "High confidence failures",
                }
                recs.append({
                    "type": "dominant_cause",
                    "severity": SEV_MEDIUM,
                    "message": f"🔍 Primary loss driver: {cause_labels.get(top_cause, top_cause)} ({top_count}/{total_losses} = {top_count/total_losses*100:.0f}% of losses)",
                    "action": f"investigate_{top_cause}",
                    "cause": top_cause,
                })

        # 6. Rolling WR declining
        if m["total_analyzed"] >= 20 and m["rolling_20_wr"] < 45:
            recs.append({
                "type": "wr_decline",
                "severity": SEV_MEDIUM,
                "message": f"📉 Rolling 20-trade WR dropped to {m['rolling_20_wr']:.1f}%",
                "action": "review_strategy",
            })

        # 7. Worst performing hour
        if m["total_analyzed"] >= 30:
            worst_hour = None
            worst_pnl = 0
            for h, hs in self._hourly_stats.items():
                total = hs["wins"] + hs["losses"]
                if total >= 5 and hs["pnl"] < worst_pnl:
                    worst_hour = h
                    worst_pnl = hs["pnl"]
            if worst_hour is not None and worst_pnl < -0.3:
                recs.append({
                    "type": "bad_hour",
                    "severity": SEV_LOW,
                    "message": f"⏰ Worst trading hour: {worst_hour:02d}:00 UTC (PnL: {worst_pnl:+.2f}%)",
                    "action": f"avoid_hour_{worst_hour}",
                })

        self._recommendations = recs


    def sync_with_tracker(self, tracker_stats, closed_signals):
        """Rebuild monitor metrics from signal tracker as source of truth.

        Called on startup to ensure monitor and tracker agree on trade counts
        and paper balance. The signal tracker is the authoritative source.
        """
        if not tracker_stats:
            return

        tracker_closed = tracker_stats.get("closed", 0)
        tracker_balance = tracker_stats.get("paper_balance", 1000)
        tracker_start = tracker_stats.get("paper_start_balance", 1000)
        old_total = self._metrics["total_analyzed"]

        if abs(old_total - tracker_closed) > 2 or abs(self._metrics["paper_balance"] - tracker_balance) > 5:
            logger.info(
                "TradeMonitor SYNC: monitor had %d trades (bal=$%.2f), tracker has %d (bal=$%.2f) -- rebuilding",
                old_total, self._metrics["paper_balance"], tracker_closed, tracker_balance,
            )

            self._metrics = {
                "total_analyzed": 0,
                "total_wins": 0,
                "total_losses": 0,
                "current_streak": 0,
                "max_win_streak": 0,
                "max_loss_streak": 0,
                "rolling_20_wr": 0.0,
                "rolling_20_pnl": 0.0,
                "peak_balance": tracker_start,
                "current_drawdown": 0.0,
                "max_drawdown": 0.0,
                "paper_balance": tracker_start,
            }
            self._seen_ids.clear()
            self._loss_causes.clear()
            self._setup_losses.clear()
            self._side_stats = {
                "long": {"wins": 0, "losses": 0, "pnl": 0.0},
                "short": {"wins": 0, "losses": 0, "pnl": 0.0},
            }
            self._hourly_stats = {h: {"wins": 0, "losses": 0, "pnl": 0.0} for h in range(24)}
            self._analyzed_trades.clear()
            self._loss_log.clear()
            self._recent_trades.clear()
            self._recommendations.clear()

            for sig in closed_signals:
                self.analyze_trade(sig)

            logger.info(
                "TradeMonitor SYNC complete: %d trades, bal=$%.2f",
                self._metrics["total_analyzed"], self._metrics["paper_balance"],
            )

    # ------------------------------------------------------------------
    # Dashboard API
    # ------------------------------------------------------------------

    def get_monitor_report(self) -> Dict[str, Any]:
        """Return the full monitor report for dashboard display."""
        m = self._metrics

        # Calculate overall WR
        total = m["total_wins"] + m["total_losses"]
        overall_wr = round(m["total_wins"] / total * 100, 1) if total > 0 else 0

        # Top loss causes
        sorted_causes = sorted(
            self._loss_causes.items(), key=lambda x: x[1], reverse=True
        )[:6]

        cause_labels = {
            LOSS_WRONG_DIRECTION: "Wrong Direction",
            LOSS_NOISE_STOPOUT: "Noise Stopout",
            LOSS_WEAK_SETUP: "Weak Setup",
            LOSS_POST_TP1_REVERSAL: "Post-TP1 Reversal",
            LOSS_WIDE_SL: "Wrong Trade (Wide SL)",
            LOSS_EXPIRED: "Expired",
            LOSS_FAST_STOP: "Fast Stop (<2m)",
            LOSS_HIGH_CONF_FAIL: "High Conf Failure",
        }

        # Setup performance table
        setup_perf = []
        for setup, data in self._setup_losses.items():
            total_s = data["wins"] + data["losses"]
            if total_s > 0:
                setup_perf.append({
                    "setup": setup,
                    "trades": total_s,
                    "wins": data["wins"],
                    "losses": data["losses"],
                    "win_rate": round(data["wins"] / total_s * 100, 1),
                    "total_pnl": round(data["total_pnl"], 4),
                    "total_pnl_usd": round(data["total_pnl_usd"], 2),
                    "avg_loss": round(data["avg_loss"], 4),
                    "worst_loss": round(data["worst_loss"], 4),
                    "last_5": data["last_5"],
                    "health": self._setup_health(data),
                })
        setup_perf.sort(key=lambda x: x["total_pnl"], reverse=True)

        # Side performance
        side_perf = {}
        for side_name, ss in self._side_stats.items():
            total_s = ss["wins"] + ss["losses"]
            side_perf[side_name] = {
                "trades": total_s,
                "wins": ss["wins"],
                "losses": ss["losses"],
                "win_rate": round(ss["wins"] / total_s * 100, 1) if total_s > 0 else 0,
                "pnl": round(ss["pnl"], 4),
            }

        # Recent losses (last 10)
        recent_losses = []
        for loss in reversed(self._loss_log[-10:]):
            recent_losses.append({
                "trade_id": loss.get("trade_id", "")[:8],
                "symbol": loss.get("symbol", ""),
                "side": loss.get("side", ""),
                "setup": loss.get("setup", ""),
                "confidence": loss.get("confidence", 0),
                "grade": loss.get("grade", ""),
                "pnl_pct": loss.get("pnl_pct", 0),
                "pnl_usd": loss.get("pnl_usd", 0),
                "sl_dist_pct": loss.get("sl_dist_pct", 0),
                "duration_sec": loss.get("duration_sec", 0),
                "loss_causes": loss.get("loss_causes", []),
                "severity": loss.get("severity", "low"),
            })

        # Hourly heatmap data
        hourly = []
        for h in range(24):
            hs = self._hourly_stats.get(h, {"wins": 0, "losses": 0, "pnl": 0})
            total_h = hs["wins"] + hs["losses"]
            hourly.append({
                "hour": h,
                "trades": total_h,
                "wins": hs["wins"],
                "losses": hs["losses"],
                "pnl": round(hs["pnl"], 4),
                "wr": round(hs["wins"] / total_h * 100, 1) if total_h > 0 else 0,
            })

        return {
            "active": True,
            "last_update": datetime.now(timezone.utc).isoformat(),

            # Summary metrics
            "total_analyzed": m["total_analyzed"],
            "total_wins": m["total_wins"],
            "total_losses": m["total_losses"],
            "overall_wr": overall_wr,
            "current_streak": m["current_streak"],
            "max_win_streak": m["max_win_streak"],
            "max_loss_streak": m["max_loss_streak"],
            "rolling_20_wr": m["rolling_20_wr"],
            "rolling_20_pnl": m["rolling_20_pnl"],
            "paper_balance": round(m["paper_balance"], 2),
            "peak_balance": round(m["peak_balance"], 2),
            "current_drawdown": m["current_drawdown"],
            "max_drawdown": m["max_drawdown"],
            "sharpe_ratio": m.get("sharpe_ratio", 0.0),
            "sortino_ratio": m.get("sortino_ratio", 0.0),

            # Loss analysis
            "loss_causes": [
                {"cause": cause_labels.get(c, c), "key": c, "count": n}
                for c, n in sorted_causes
            ],
            "recent_losses": recent_losses,

            # Breakdowns
            "setup_performance": setup_perf,
            "side_performance": side_perf,
            "hourly_heatmap": hourly,

            # Recommendations
            "recommendations": self._recommendations,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_duration(entry_time: str, exit_time: str) -> int:
        """Calculate trade duration in seconds."""
        try:
            et = datetime.fromisoformat(entry_time)
            xt = datetime.fromisoformat(exit_time)
            return int((xt - et).total_seconds())
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _extract_hour(timestamp: str) -> int:
        """Extract UTC hour from timestamp."""
        try:
            return datetime.fromisoformat(timestamp).hour
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def _setup_health(data: Dict) -> str:
        """Determine health status for a setup."""
        total = data["wins"] + data["losses"]
        if total < 5:
            return "learning"
        wr = data["wins"] / total * 100
        if wr >= 65 and data["total_pnl"] > 0:
            return "excellent"
        elif wr >= 50 and data["total_pnl"] >= 0:
            return "good"
        elif wr >= 40:
            return "warning"
        else:
            return "critical"

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        """Load monitor state from disk."""
        try:
            if _MONITOR_FILE.exists():
                data = json.loads(_MONITOR_FILE.read_text(encoding="utf-8"))
                self._metrics = data.get("metrics", self._metrics)
                self._loss_causes = defaultdict(int, data.get("loss_causes", {}))
                self._setup_losses = data.get("setup_losses", {})
                self._side_stats = data.get("side_stats", self._side_stats)
                self._recommendations = data.get("recommendations", [])
                self._loss_log = data.get("loss_log", [])
                self._analyzed_trades = data.get("analyzed_trades", [])
                self._seen_ids = set(data.get("seen_ids", []))
                self._hourly_stats = {
                    int(k): v for k, v in data.get("hourly_stats", {}).items()
                }
                # Ensure all hours exist
                for h in range(24):
                    if h not in self._hourly_stats:
                        self._hourly_stats[h] = {"wins": 0, "losses": 0, "pnl": 0.0}

                # Rebuild recent trades deque
                for t in self._analyzed_trades[-50:]:
                    self._recent_trades.append(t)

                logger.info(
                    "TradeMonitor loaded: %d analyzed, %d losses tracked",
                    self._metrics.get("total_analyzed", 0),
                    len(self._loss_log),
                )
        except Exception as exc:
            logger.warning("Failed to load trade monitor state: %s", exc)

    def _save(self) -> None:
        """Persist monitor state to disk."""
        try:
            data = {
                "metrics": self._metrics,
                "loss_causes": dict(self._loss_causes),
                "setup_losses": self._setup_losses,
                "side_stats": self._side_stats,
                "recommendations": self._recommendations,
                "loss_log": self._loss_log[-200:],
                "analyzed_trades": self._analyzed_trades[-500:],
                "seen_ids": list(self._seen_ids)[-1000:],
                "hourly_stats": {str(k): v for k, v in self._hourly_stats.items()},
                "saved_at": datetime.now(timezone.utc).isoformat(),
            }
            _MONITOR_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to save trade monitor state: %s", exc)
