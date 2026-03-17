"""
Trade journal subsystem.

Records every trade with full metadata, computes performance analytics,
and exports to CSV/JSON for analysis.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from execution.trade import Trade, TradeStatus

logger = logging.getLogger(__name__)


class TradeJournal:
    """Persistent trade journal with analytics.

    Stores trades in memory and on disk (JSON + optional CSV).
    Provides performance metrics matching the backtest engine output
    so live and backtest results are directly comparable.
    """

    def __init__(self, config: Dict[str, Any]) -> None:
        journal_cfg = config.get("journal", {})
        self.enabled: bool = journal_cfg.get("enabled", True)
        self.export_csv: bool = journal_cfg.get("export_csv", True)
        self.export_json: bool = journal_cfg.get("export_json", True)

        project_root = config.get("_project_root", ".")
        self.data_dir = Path(project_root) / "data" / "journal"
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self._trades: List[Trade] = []
        self._closed_trades: List[Trade] = []

        # Load existing journal
        self._load()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_trade(self, trade: Trade) -> None:
        """Add or update a trade in the journal."""
        if not self.enabled:
            return

        # Check if trade already exists (update it)
        for i, existing in enumerate(self._trades):
            if existing.trade_id == trade.trade_id:
                self._trades[i] = trade
                if trade.status in (TradeStatus.CLOSED, TradeStatus.CANCELLED, TradeStatus.FAILED):
                    self._closed_trades.append(trade)
                self._persist()
                return

        self._trades.append(trade)
        if trade.status in (TradeStatus.CLOSED, TradeStatus.CANCELLED, TradeStatus.FAILED):
            self._closed_trades.append(trade)
        self._persist()

        logger.info(
            "Journal: recorded trade %s %s %s | PnL: %.2f",
            trade.trade_id,
            trade.symbol,
            trade.side.value,
            trade.pnl,
        )

    def get_all_trades(self) -> List[Trade]:
        return list(self._trades)

    def get_open_trades(self) -> List[Trade]:
        return [t for t in self._trades if t.is_open]

    def get_closed_trades(self) -> List[Trade]:
        return list(self._closed_trades)

    def get_trades_for_symbol(self, symbol: str) -> List[Trade]:
        return [t for t in self._trades if t.symbol == symbol]

    # ------------------------------------------------------------------
    # Performance analytics
    # ------------------------------------------------------------------

    def get_performance(self) -> Dict[str, Any]:
        """Compute comprehensive performance metrics from closed trades."""
        closed = self._closed_trades
        if not closed:
            return self._empty_performance()

        winners = [t for t in closed if t.pnl > 0]
        losers = [t for t in closed if t.pnl <= 0]

        total_pnl = sum(t.pnl for t in closed)
        total_fees = sum(t.fees for t in closed)
        gross_profit = sum(t.pnl for t in winners) if winners else 0
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0

        win_rate = len(winners) / len(closed) * 100 if closed else 0
        avg_winner = gross_profit / len(winners) if winners else 0
        avg_loser = gross_loss / len(losers) if losers else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        expectancy = total_pnl / len(closed) if closed else 0

        # Max drawdown
        equity_curve = []
        running = 0
        for t in closed:
            running += t.pnl
            equity_curve.append(running)

        peak = 0
        max_dd = 0
        for eq in equity_curve:
            if eq > peak:
                peak = eq
            dd = peak - eq
            if dd > max_dd:
                max_dd = dd

        # Consecutive wins/losses
        max_consec_wins, max_consec_losses = self._calc_consecutive(closed)

        # Average duration
        durations = [t.duration_seconds for t in closed if t.duration_seconds is not None]
        avg_duration = sum(durations) / len(durations) if durations else 0

        # Long vs short
        longs = [t for t in closed if t.side.value == "long"]
        shorts = [t for t in closed if t.side.value == "short"]

        # Sharpe (simplified daily)
        pnl_series = [t.pnl for t in closed]
        if len(pnl_series) > 1:
            import numpy as np
            mean_pnl = np.mean(pnl_series)
            std_pnl = np.std(pnl_series, ddof=1)
            sharpe = (mean_pnl / std_pnl) * (252 ** 0.5) if std_pnl > 0 else 0
        else:
            sharpe = 0

        # Monthly breakdown
        monthly = self._monthly_breakdown(closed)

        return {
            "total_trades": len(closed),
            "winners": len(winners),
            "losers": len(losers),
            "win_rate": round(win_rate, 2),
            "total_pnl": round(total_pnl, 2),
            "total_fees": round(total_fees, 2),
            "net_pnl": round(total_pnl, 2),
            "gross_profit": round(gross_profit, 2),
            "gross_loss": round(gross_loss, 2),
            "profit_factor": round(profit_factor, 2),
            "expectancy": round(expectancy, 2),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "largest_winner": round(max((t.pnl for t in winners), default=0), 2),
            "largest_loser": round(min((t.pnl for t in losers), default=0), 2),
            "max_drawdown": round(max_dd, 2),
            "sharpe_ratio": round(sharpe, 2),
            "max_consecutive_wins": max_consec_wins,
            "max_consecutive_losses": max_consec_losses,
            "avg_trade_duration_seconds": round(avg_duration, 0),
            "long_trades": len(longs),
            "long_win_rate": round(
                len([t for t in longs if t.pnl > 0]) / len(longs) * 100, 2
            ) if longs else 0,
            "long_pnl": round(sum(t.pnl for t in longs), 2),
            "short_trades": len(shorts),
            "short_win_rate": round(
                len([t for t in shorts if t.pnl > 0]) / len(shorts) * 100, 2
            ) if shorts else 0,
            "short_pnl": round(sum(t.pnl for t in shorts), 2),
            "monthly_performance": monthly,
        }

    def get_daily_pnl(self) -> Dict[str, float]:
        """PnL grouped by date."""
        daily: Dict[str, float] = defaultdict(float)
        for t in self._closed_trades:
            if t.exit_time:
                key = t.exit_time.strftime("%Y-%m-%d")
                daily[key] += t.pnl
        return dict(daily)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_to_csv(self, path: Optional[str] = None) -> str:
        """Export closed trades to CSV."""
        if path is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = str(self.data_dir / f"trades_{ts}.csv")

        fields = [
            "trade_id", "symbol", "side", "status",
            "entry_price", "entry_time", "exit_price", "exit_time",
            "stop_loss", "position_size", "position_size_usd", "leverage",
            "pnl", "pnl_pct", "fees", "slippage",
            "max_favorable_excursion", "max_adverse_excursion",
            "entry_reason", "exit_reason", "grade", "confidence",
            "timeframe",
        ]

        with open(path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for trade in self._closed_trades:
                row = trade.to_dict()
                writer.writerow(row)

        logger.info("Exported %d trades to %s", len(self._closed_trades), path)
        return path

    def export_to_json(self, path: Optional[str] = None) -> str:
        """Export all trades to JSON."""
        if path is None:
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            path = str(self.data_dir / f"trades_{ts}.json")

        data = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "total_trades": len(self._trades),
            "closed_trades": len(self._closed_trades),
            "performance": self.get_performance(),
            "trades": [t.to_dict() for t in self._trades],
        }

        with open(path, "w") as f:
            json.dump(data, f, indent=2, default=str)

        logger.info("Exported journal to %s", path)
        return path

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _persist(self) -> None:
        """Save current state to disk."""
        if not self.enabled:
            return

        state_path = self.data_dir / "journal_state.json"
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "trades": [t.to_dict() for t in self._trades],
        }
        try:
            with open(state_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            logger.error("Failed to persist journal: %s", e)

    def _load(self) -> None:
        """Load state from disk if available."""
        state_path = self.data_dir / "journal_state.json"
        if not state_path.exists():
            return

        try:
            with open(state_path, "r") as f:
                data = json.load(f)

            for td in data.get("trades", []):
                trade = Trade.from_dict(td)
                self._trades.append(trade)
                if trade.status in (TradeStatus.CLOSED, TradeStatus.CANCELLED, TradeStatus.FAILED):
                    self._closed_trades.append(trade)

            logger.info(
                "Loaded %d trades from journal (%d closed)",
                len(self._trades),
                len(self._closed_trades),
            )
        except Exception as e:
            logger.error("Failed to load journal: %s", e)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_consecutive(trades: List[Trade]) -> tuple[int, int]:
        """Calculate max consecutive wins and losses."""
        max_wins = max_losses = 0
        cur_wins = cur_losses = 0

        for t in trades:
            if t.pnl > 0:
                cur_wins += 1
                cur_losses = 0
                max_wins = max(max_wins, cur_wins)
            else:
                cur_losses += 1
                cur_wins = 0
                max_losses = max(max_losses, cur_losses)

        return max_wins, max_losses

    @staticmethod
    def _monthly_breakdown(trades: List[Trade]) -> Dict[str, Dict[str, Any]]:
        """Group trades by month and compute stats."""
        months: Dict[str, List[Trade]] = defaultdict(list)
        for t in trades:
            if t.exit_time:
                key = t.exit_time.strftime("%Y-%m")
                months[key].append(t)

        result = {}
        for month, month_trades in sorted(months.items()):
            wins = [t for t in month_trades if t.pnl > 0]
            pnl = sum(t.pnl for t in month_trades)
            result[month] = {
                "trades": len(month_trades),
                "wins": len(wins),
                "win_rate": round(len(wins) / len(month_trades) * 100, 1),
                "pnl": round(pnl, 2),
            }

        return result

    @staticmethod
    def _empty_performance() -> Dict[str, Any]:
        return {
            "total_trades": 0,
            "winners": 0,
            "losers": 0,
            "win_rate": 0,
            "total_pnl": 0,
            "total_fees": 0,
            "net_pnl": 0,
            "gross_profit": 0,
            "gross_loss": 0,
            "profit_factor": 0,
            "expectancy": 0,
            "avg_winner": 0,
            "avg_loser": 0,
            "largest_winner": 0,
            "largest_loser": 0,
            "max_drawdown": 0,
            "sharpe_ratio": 0,
            "max_consecutive_wins": 0,
            "max_consecutive_losses": 0,
            "avg_trade_duration_seconds": 0,
            "long_trades": 0,
            "long_win_rate": 0,
            "long_pnl": 0,
            "short_trades": 0,
            "short_win_rate": 0,
            "short_pnl": 0,
            "monthly_performance": {},
        }
