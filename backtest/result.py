"""
Backtest result container with comprehensive performance metrics and reporting.

Stores completed trades and equity curve data, computes risk-adjusted return
metrics, and provides export/plotting utilities for post-run analysis.
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Container for backtest output with computed performance metrics."""

    trades: List[Any] = field(default_factory=list)
    equity_curve: List[Tuple[datetime, float]] = field(default_factory=list)
    initial_balance: float = 10_000.0
    total_fees_paid: float = 0.0
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    symbols: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Core return metrics
    # ------------------------------------------------------------------

    @property
    def final_equity(self) -> float:
        if not self.equity_curve:
            return self.initial_balance
        return self.equity_curve[-1][1]

    @property
    def total_return(self) -> float:
        return self.final_equity - self.initial_balance

    @property
    def total_return_pct(self) -> float:
        if self.initial_balance == 0:
            return 0.0
        return (self.total_return / self.initial_balance) * 100.0

    # ------------------------------------------------------------------
    # Trade statistics
    # ------------------------------------------------------------------

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def total_fees(self) -> float:
        return self.total_fees_paid

    @property
    def _pnl_list(self) -> List[float]:
        return [self._trade_pnl(t) for t in self.trades]

    @property
    def _winners(self) -> List[float]:
        return [p for p in self._pnl_list if p > 0]

    @property
    def _losers(self) -> List[float]:
        return [p for p in self._pnl_list if p <= 0]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return (len(self._winners) / len(self.trades)) * 100.0

    @property
    def avg_winner(self) -> float:
        w = self._winners
        return float(np.mean(w)) if w else 0.0

    @property
    def avg_loser(self) -> float:
        los = self._losers
        return float(np.mean(los)) if los else 0.0

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(self._winners)
        gross_loss = abs(sum(self._losers))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def expectancy(self) -> float:
        """Average PnL per trade."""
        if not self.trades:
            return 0.0
        return float(np.mean(self._pnl_list))

    # ------------------------------------------------------------------
    # Risk metrics
    # ------------------------------------------------------------------

    @property
    def sharpe_ratio(self) -> float:
        """Annualised Sharpe ratio using daily equity returns."""
        returns = self._daily_returns
        if len(returns) < 2:
            return 0.0
        mean_r = np.mean(returns)
        std_r = np.std(returns, ddof=1)
        if std_r == 0:
            return 0.0
        return float((mean_r / std_r) * np.sqrt(365))

    @property
    def sortino_ratio(self) -> float:
        """Annualised Sortino ratio (downside deviation only)."""
        returns = self._daily_returns
        if len(returns) < 2:
            return 0.0
        mean_r = np.mean(returns)
        downside = returns[returns < 0]
        if len(downside) == 0:
            return float("inf") if mean_r > 0 else 0.0
        dd_std = np.std(downside, ddof=1)
        if dd_std == 0:
            return 0.0
        return float((mean_r / dd_std) * np.sqrt(365))

    @property
    def max_drawdown(self) -> float:
        """Maximum drawdown in absolute dollar terms."""
        dd_abs, _ = self._compute_drawdowns()
        return dd_abs

    @property
    def max_drawdown_pct(self) -> float:
        """Maximum drawdown as a percentage of peak equity."""
        _, dd_pct = self._compute_drawdowns()
        return dd_pct

    # ------------------------------------------------------------------
    # Streak analysis
    # ------------------------------------------------------------------

    @property
    def consecutive_wins(self) -> int:
        return self._max_streak(win=True)

    @property
    def consecutive_losses(self) -> int:
        return self._max_streak(win=False)

    # ------------------------------------------------------------------
    # Duration
    # ------------------------------------------------------------------

    @property
    def avg_trade_duration(self) -> timedelta:
        durations = []
        for t in self.trades:
            entry_ts = self._get_trade_attr(t, "entry_time", "entry_timestamp", "opened_at")
            exit_ts = self._get_trade_attr(t, "exit_time", "exit_timestamp", "closed_at")
            if entry_ts and exit_ts:
                if isinstance(entry_ts, (int, float)):
                    entry_ts = datetime.utcfromtimestamp(entry_ts / 1000 if entry_ts > 1e12 else entry_ts)
                if isinstance(exit_ts, (int, float)):
                    exit_ts = datetime.utcfromtimestamp(exit_ts / 1000 if exit_ts > 1e12 else exit_ts)
                durations.append((exit_ts - entry_ts).total_seconds())
        if not durations:
            return timedelta(0)
        return timedelta(seconds=float(np.mean(durations)))

    # ------------------------------------------------------------------
    # Directional performance
    # ------------------------------------------------------------------

    @property
    def long_performance(self) -> Dict[str, float]:
        return self._direction_stats("long")

    @property
    def short_performance(self) -> Dict[str, float]:
        return self._direction_stats("short")

    # ------------------------------------------------------------------
    # Monthly returns
    # ------------------------------------------------------------------

    @property
    def monthly_returns(self) -> Dict[str, float]:
        """Return dict mapping 'YYYY-MM' -> return percentage for that month."""
        if len(self.equity_curve) < 2:
            return {}

        monthly: Dict[str, List[Tuple[datetime, float]]] = defaultdict(list)
        for ts, eq in self.equity_curve:
            key = ts.strftime("%Y-%m")
            monthly[key].append((ts, eq))

        result: Dict[str, float] = {}
        sorted_keys = sorted(monthly.keys())
        prev_end_equity = self.initial_balance
        for key in sorted_keys:
            points = monthly[key]
            end_equity = points[-1][1]
            if prev_end_equity != 0:
                result[key] = ((end_equity - prev_end_equity) / prev_end_equity) * 100.0
            else:
                result[key] = 0.0
            prev_end_equity = end_equity
        return result

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def summary(self) -> str:
        """Return a formatted multi-line performance report."""
        lines = [
            "=" * 64,
            "  BACKTEST PERFORMANCE REPORT",
            "=" * 64,
            f"  Period          : {self._fmt_date(self.start_date)} to {self._fmt_date(self.end_date)}",
            f"  Symbols         : {', '.join(self.symbols) if self.symbols else 'N/A'}",
            f"  Initial Balance : ${self.initial_balance:,.2f}",
            f"  Final Equity    : ${self.final_equity:,.2f}",
            "-" * 64,
            f"  Total Return    : ${self.total_return:,.2f} ({self.total_return_pct:+.2f}%)",
            f"  Total Trades    : {self.total_trades}",
            f"  Win Rate        : {self.win_rate:.1f}%",
            f"  Profit Factor   : {self.profit_factor:.2f}",
            f"  Expectancy      : ${self.expectancy:,.2f}",
            "-" * 64,
            f"  Avg Winner      : ${self.avg_winner:,.2f}",
            f"  Avg Loser       : ${self.avg_loser:,.2f}",
            f"  Max Consec Wins : {self.consecutive_wins}",
            f"  Max Consec Loss : {self.consecutive_losses}",
            f"  Avg Duration    : {self._fmt_duration(self.avg_trade_duration)}",
            "-" * 64,
            f"  Sharpe Ratio    : {self.sharpe_ratio:.3f}",
            f"  Sortino Ratio   : {self.sortino_ratio:.3f}",
            f"  Max Drawdown    : ${self.max_drawdown:,.2f} ({self.max_drawdown_pct:.2f}%)",
            f"  Total Fees      : ${self.total_fees:,.2f}",
            "-" * 64,
            "  LONG  PERFORMANCE",
            f"    Count    : {self.long_performance['count']:.0f}",
            f"    Win Rate : {self.long_performance['win_rate']:.1f}%",
            f"    Avg PnL  : ${self.long_performance['avg_pnl']:,.2f}",
            "  SHORT PERFORMANCE",
            f"    Count    : {self.short_performance['count']:.0f}",
            f"    Win Rate : {self.short_performance['win_rate']:.1f}%",
            f"    Avg PnL  : ${self.short_performance['avg_pnl']:,.2f}",
            "=" * 64,
        ]
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serializable dictionary of all metrics."""
        return {
            "initial_balance": self.initial_balance,
            "final_equity": self.final_equity,
            "total_return": round(self.total_return, 4),
            "total_return_pct": round(self.total_return_pct, 4),
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 2),
            "profit_factor": round(self.profit_factor, 4),
            "expectancy": round(self.expectancy, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "sortino_ratio": round(self.sortino_ratio, 4),
            "max_drawdown": round(self.max_drawdown, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "avg_winner": round(self.avg_winner, 4),
            "avg_loser": round(self.avg_loser, 4),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "avg_trade_duration_seconds": self.avg_trade_duration.total_seconds(),
            "total_fees": round(self.total_fees, 4),
            "long_performance": self.long_performance,
            "short_performance": self.short_performance,
            "monthly_returns": self.monthly_returns,
            "start_date": self._fmt_date(self.start_date),
            "end_date": self._fmt_date(self.end_date),
            "symbols": self.symbols,
        }

    def export_csv(self, filepath: str) -> None:
        """Export the trade list to a CSV file."""
        if not self.trades:
            logger.warning("No trades to export.")
            return

        fieldnames = [
            "trade_id", "symbol", "side", "entry_price", "exit_price",
            "quantity", "pnl", "pnl_pct", "fees", "entry_time", "exit_time",
            "duration_s", "exit_reason",
        ]
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for t in self.trades:
                entry_ts = self._get_trade_attr(t, "entry_time", "entry_timestamp", "opened_at")
                exit_ts = self._get_trade_attr(t, "exit_time", "exit_timestamp", "closed_at")
                dur = 0.0
                if entry_ts and exit_ts:
                    if isinstance(entry_ts, (int, float)):
                        entry_ts = datetime.utcfromtimestamp(entry_ts / 1000 if entry_ts > 1e12 else entry_ts)
                    if isinstance(exit_ts, (int, float)):
                        exit_ts = datetime.utcfromtimestamp(exit_ts / 1000 if exit_ts > 1e12 else exit_ts)
                    dur = (exit_ts - entry_ts).total_seconds()
                writer.writerow({
                    "trade_id": self._get_trade_attr(t, "trade_id", "id") or "",
                    "symbol": self._get_trade_attr(t, "symbol") or "",
                    "side": self._get_trade_attr(t, "side", "direction") or "",
                    "entry_price": self._get_trade_attr(t, "entry_price") or "",
                    "exit_price": self._get_trade_attr(t, "exit_price") or "",
                    "quantity": self._get_trade_attr(t, "quantity", "size", "amount") or "",
                    "pnl": round(self._trade_pnl(t), 4),
                    "pnl_pct": round(self._trade_pnl_pct(t), 4),
                    "fees": round(self._trade_fees(t), 4),
                    "entry_time": entry_ts if entry_ts else "",
                    "exit_time": exit_ts if exit_ts else "",
                    "duration_s": round(dur, 1),
                    "exit_reason": self._get_trade_attr(t, "exit_reason", "close_reason") or "",
                })
        logger.info("Exported %d trades to %s", len(self.trades), filepath)

    # ------------------------------------------------------------------
    # Plotting
    # ------------------------------------------------------------------

    def plot_equity_curve(self, filepath: str) -> None:
        """Save an equity curve chart to *filepath*."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        if len(self.equity_curve) < 2:
            logger.warning("Insufficient data to plot equity curve.")
            return

        timestamps = [ts for ts, _ in self.equity_curve]
        equities = [eq for _, eq in self.equity_curve]

        fig, ax = plt.subplots(figsize=(14, 6))
        ax.plot(timestamps, equities, linewidth=1.2, color="#2196F3")
        ax.axhline(y=self.initial_balance, color="gray", linestyle="--", linewidth=0.8, alpha=0.6)
        ax.fill_between(
            timestamps, self.initial_balance, equities,
            where=[e >= self.initial_balance for e in equities],
            alpha=0.15, color="#4CAF50", interpolate=True,
        )
        ax.fill_between(
            timestamps, self.initial_balance, equities,
            where=[e < self.initial_balance for e in equities],
            alpha=0.15, color="#F44336", interpolate=True,
        )
        ax.set_title("Equity Curve", fontsize=14, fontweight="bold")
        ax.set_xlabel("Date")
        ax.set_ylabel("Equity ($)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(filepath, dpi=150)
        plt.close(fig)
        logger.info("Equity curve saved to %s", filepath)

    def plot_drawdown(self, filepath: str) -> None:
        """Save a drawdown chart to *filepath*."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        if len(self.equity_curve) < 2:
            logger.warning("Insufficient data to plot drawdown.")
            return

        timestamps = [ts for ts, _ in self.equity_curve]
        equities = np.array([eq for _, eq in self.equity_curve])
        running_max = np.maximum.accumulate(equities)
        drawdown_pct = np.where(running_max > 0, ((equities - running_max) / running_max) * 100.0, 0.0)

        fig, ax = plt.subplots(figsize=(14, 4))
        ax.fill_between(timestamps, 0, drawdown_pct, color="#F44336", alpha=0.4)
        ax.plot(timestamps, drawdown_pct, color="#D32F2F", linewidth=0.8)
        ax.set_title("Drawdown", fontsize=14, fontweight="bold")
        ax.set_xlabel("Date")
        ax.set_ylabel("Drawdown (%)")
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator())
        fig.autofmt_xdate()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(filepath, dpi=150)
        plt.close(fig)
        logger.info("Drawdown chart saved to %s", filepath)

    def plot_monthly_returns(self, filepath: str) -> None:
        """Save a monthly returns heatmap to *filepath*."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        mr = self.monthly_returns
        if not mr:
            logger.warning("No monthly return data to plot.")
            return

        # Build a year x month matrix
        years: Dict[int, Dict[int, float]] = defaultdict(lambda: defaultdict(float))
        for key, val in mr.items():
            parts = key.split("-")
            years[int(parts[0])][int(parts[1])] = val

        sorted_years = sorted(years.keys())
        month_labels = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

        data = np.full((len(sorted_years), 12), np.nan)
        for yi, year in enumerate(sorted_years):
            for month in range(1, 13):
                if month in years[year]:
                    data[yi, month - 1] = years[year][month]

        fig, ax = plt.subplots(figsize=(14, max(3, len(sorted_years) * 0.8 + 1)))
        vmax = max(abs(np.nanmin(data)) if not np.all(np.isnan(data)) else 1,
                   abs(np.nanmax(data)) if not np.all(np.isnan(data)) else 1)
        cmap = plt.cm.RdYlGn  # type: ignore[attr-defined]
        im = ax.imshow(data, cmap=cmap, aspect="auto", vmin=-vmax, vmax=vmax)

        ax.set_xticks(range(12))
        ax.set_xticklabels(month_labels)
        ax.set_yticks(range(len(sorted_years)))
        ax.set_yticklabels([str(y) for y in sorted_years])

        # Annotate cells
        for yi in range(len(sorted_years)):
            for mi in range(12):
                val = data[yi, mi]
                if not np.isnan(val):
                    color = "white" if abs(val) > vmax * 0.6 else "black"
                    ax.text(mi, yi, f"{val:.1f}%", ha="center", va="center",
                            fontsize=9, color=color, fontweight="bold")

        ax.set_title("Monthly Returns (%)", fontsize=14, fontweight="bold")
        fig.colorbar(im, ax=ax, label="Return %", shrink=0.8)
        fig.tight_layout()
        fig.savefig(filepath, dpi=150)
        plt.close(fig)
        logger.info("Monthly returns heatmap saved to %s", filepath)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_trade_attr(trade: Any, *names: str) -> Any:
        """Try multiple attribute/key names on a trade object or dict."""
        for name in names:
            if isinstance(trade, dict):
                if name in trade:
                    return trade[name]
            else:
                val = getattr(trade, name, None)
                if val is not None:
                    return val
        return None

    @classmethod
    def _trade_pnl(cls, trade: Any) -> float:
        val = cls._get_trade_attr(trade, "pnl", "realized_pnl", "profit")
        return float(val) if val is not None else 0.0

    @classmethod
    def _trade_pnl_pct(cls, trade: Any) -> float:
        val = cls._get_trade_attr(trade, "pnl_pct", "return_pct", "profit_pct")
        return float(val) if val is not None else 0.0

    @classmethod
    def _trade_fees(cls, trade: Any) -> float:
        val = cls._get_trade_attr(trade, "fees", "total_fees", "commission")
        return float(val) if val is not None else 0.0

    @classmethod
    def _trade_side(cls, trade: Any) -> str:
        val = cls._get_trade_attr(trade, "side", "direction")
        return str(val).lower() if val else ""

    @property
    def _daily_returns(self) -> np.ndarray:
        """Compute daily percentage returns from equity curve."""
        if len(self.equity_curve) < 2:
            return np.array([])

        # Group by date
        daily_equity: Dict[str, float] = {}
        for ts, eq in self.equity_curve:
            key = ts.strftime("%Y-%m-%d")
            daily_equity[key] = eq  # last value of each day

        values = list(daily_equity.values())
        if len(values) < 2:
            return np.array([])

        arr = np.array(values)
        returns = np.diff(arr) / arr[:-1]
        return returns

    def _compute_drawdowns(self) -> Tuple[float, float]:
        """Return (max_drawdown_absolute, max_drawdown_pct)."""
        if len(self.equity_curve) < 2:
            return 0.0, 0.0

        equities = np.array([eq for _, eq in self.equity_curve])
        running_max = np.maximum.accumulate(equities)
        drawdowns = running_max - equities
        dd_pct = np.where(running_max > 0, (drawdowns / running_max) * 100.0, 0.0)
        return float(np.max(drawdowns)), float(np.max(dd_pct))

    def _max_streak(self, win: bool) -> int:
        if not self.trades:
            return 0
        max_streak = 0
        current = 0
        for t in self.trades:
            is_win = self._trade_pnl(t) > 0
            if is_win == win:
                current += 1
                max_streak = max(max_streak, current)
            else:
                current = 0
        return max_streak

    def _direction_stats(self, direction: str) -> Dict[str, float]:
        trades = [t for t in self.trades if self._trade_side(t) == direction]
        if not trades:
            return {"count": 0, "win_rate": 0.0, "avg_pnl": 0.0}
        pnls = [self._trade_pnl(t) for t in trades]
        winners = [p for p in pnls if p > 0]
        return {
            "count": float(len(trades)),
            "win_rate": (len(winners) / len(trades)) * 100.0,
            "avg_pnl": float(np.mean(pnls)),
        }

    @staticmethod
    def _fmt_date(dt: Optional[datetime]) -> str:
        if dt is None:
            return "N/A"
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def _fmt_duration(td: timedelta) -> str:
        total_seconds = int(td.total_seconds())
        if total_seconds == 0:
            return "N/A"
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h {minutes}m"
        return f"{hours}h {minutes}m {seconds}s"
