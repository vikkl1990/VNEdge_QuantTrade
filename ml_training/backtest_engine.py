"""VN Edge Backtest Engine — replays signals on historical candles.

Minimal viable backtester: loads closed trades from DB and replays PnL
with different parameter sets for what-if analysis.
"""
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

logger = logging.getLogger("backtest.engine")


@dataclass
class BacktestConfig:
    symbols: List[str] = field(default_factory=list)
    start_date: str = ""
    end_date: str = ""
    scanners: List[str] = field(default_factory=list)
    ml_threshold: float = 0.65
    confidence_floor: int = 55
    leverage: int = 10
    risk_pct: float = 1.0


@dataclass
class BacktestResult:
    job_id: str
    config: Dict[str, Any]
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_r: float = 0.0
    win_rate: float = 0.0
    avg_r: float = 0.0
    max_drawdown: float = 0.0
    sharpe: float = 0.0


class BacktestEngine:
    """Replays historical closed_signals.json trades against a new config."""

    def __init__(self, trades_file: str = "storage/closed_signals.json"):
        self.trades_file = trades_file

    def run(self, config: BacktestConfig) -> BacktestResult:
        """Execute a backtest given a config. Returns aggregate results."""
        import uuid, os
        job_id = str(uuid.uuid4())[:8]

        trades = []
        if os.path.exists(self.trades_file):
            try:
                with open(self.trades_file) as f:
                    trades = json.load(f)
            except Exception as e:
                logger.warning("Failed loading %s: %s", self.trades_file, e)

        # Filter trades
        filtered = []
        for t in trades:
            # Symbol filter
            if config.symbols and t.get("symbol") not in config.symbols:
                continue
            # Scanner filter (from metadata)
            if config.scanners:
                scanner = (t.get("metadata", {}) or {}).get("scanner", "")
                if scanner not in config.scanners:
                    continue
            # Confidence floor
            conf = int(t.get("confidence", 0) or 0)
            if conf < config.confidence_floor:
                continue
            # ML threshold
            ml = (t.get("metadata", {}) or {}).get("ml_probability")
            if ml and float(ml) < config.ml_threshold:
                continue
            filtered.append(t)

        # Aggregate
        wins = sum(1 for t in filtered if (t.get("pnl_usd", 0) or 0) > 0)
        losses = len(filtered) - wins
        total_pnl = sum(float(t.get("pnl_usd", 0) or 0) for t in filtered)
        total_r = sum(float(t.get("exit_r", 0) or t.get("r_multiple", 0) or 0) for t in filtered)

        # Drawdown
        equity = 0
        peak = 0
        max_dd = 0
        for t in sorted(filtered, key=lambda x: x.get("closed_at", "")):
            equity += float(t.get("pnl_usd", 0) or 0)
            if equity > peak: peak = equity
            dd = peak - equity
            if dd > max_dd: max_dd = dd

        # Sharpe (simple)
        if len(filtered) >= 5:
            returns = [float(t.get("pnl_usd", 0) or 0) for t in filtered]
            mean_r = sum(returns) / len(returns)
            variance = sum((r - mean_r) ** 2 for r in returns) / len(returns)
            std = variance ** 0.5
            sharpe = (mean_r / std * (252 ** 0.5)) if std > 0 else 0
        else:
            sharpe = 0

        return BacktestResult(
            job_id=job_id,
            config=config.__dict__,
            total_trades=len(filtered),
            wins=wins,
            losses=losses,
            total_pnl=round(total_pnl, 2),
            total_r=round(total_r, 3),
            win_rate=round(wins / len(filtered) * 100, 1) if filtered else 0,
            avg_r=round(total_r / len(filtered), 3) if filtered else 0,
            max_drawdown=round(max_dd, 2),
            sharpe=round(sharpe, 3),
        )


def run_backtest(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience entry point for API."""
    cfg = BacktestConfig(
        symbols=config_dict.get("symbols", []),
        scanners=config_dict.get("scanners", []),
        ml_threshold=float(config_dict.get("ml_threshold", 0.65)),
        confidence_floor=int(config_dict.get("confidence_floor", 55)),
        leverage=int(config_dict.get("leverage", 10)),
        risk_pct=float(config_dict.get("risk_pct", 1.0)),
    )
    engine = BacktestEngine()
    result = engine.run(cfg)
    return result.__dict__
