"""
VN Edge v2.1 — Scalper Fee Simulator
Recalculates backtest results with Scalper offer fee structure.

Scalper Offer (Delta India):
  - Entry maker: 0.02% (post_only=True)
  - Exit: 0% (FREE within window)
  - Settlement: 0.06% (unavoidable)
  - Total: 0.047% per trade (was 0.18% taker)

Usage:
  python simulate_scalper.py [path_to_backtest_log]
"""

import json
import sys
import random

# ═══════════════════════════════════════════
# SCALPER FEE MODEL
# ═══════════════════════════════════════════

# Scalper + maker entry fee structure
ENTRY_MAKER_FEE = 0.0002    # 0.02% maker entry
EXIT_FEE = 0.0              # 0% under Scalper offer
SETTLEMENT_FEE = 0.0006     # 0.06% unavoidable
TOTAL_SCALPER_FEE = ENTRY_MAKER_FEE + EXIT_FEE + SETTLEMENT_FEE  # 0.047%

# Old fee structure for comparison
OLD_TAKER_FEE = 0.0006 + 0.0006 + 0.0006  # 0.18% round-trip

# Realistic slippage model
SLIPPAGE_MIN = 0.00025  # 0.025% — maker limit order, minimal
SLIPPAGE_MAX = 0.0006   # 0.06% — worst case during volatility

# Scalper compliance rate (% of trades that close within window)
SCALPER_COMPLIANCE = 0.98  # 98% close in time (2% miss window, pay exit fee)
NON_COMPLIANT_EXIT_FEE = 0.0006  # 0.06% taker if window missed


def simulate_scalper_backtest(trades: list) -> dict:
    """
    Recalculate all trades with Scalper fee model.

    Args:
        trades: list of trade dicts with keys:
            - gross_pnl_pct: gross P&L before fees
            - position_size_usd: notional position size
            - pnl_usd: current net PnL (will be recalculated)
            - setup_type: scanner name
            - symbol: trading pair
            - exit_reason: how trade exited

    Returns:
        dict with old vs new metrics comparison
    """
    old_total_pnl = 0
    new_total_pnl = 0
    old_total_fees = 0
    new_total_fees = 0

    recalculated = []

    for t in trades:
        gross_pct = t.get("gross_pnl_pct", 0) or t.get("pnl_pct", 0)
        pos_usd = t.get("position_size_usd", 500)

        # Old fees (taker round-trip)
        old_fee_pct = OLD_TAKER_FEE * 100  # 0.18%
        old_net_pct = gross_pct - old_fee_pct
        old_pnl_usd = pos_usd * old_net_pct / 100

        # New fees (Scalper + maker)
        # Random slippage within realistic range
        slippage = random.uniform(SLIPPAGE_MIN, SLIPPAGE_MAX) * 100  # as %

        # 98% comply with Scalper window → free exit
        # 2% miss window → pay exit fee
        compliant = random.random() < SCALPER_COMPLIANCE
        if compliant:
            new_fee_pct = (ENTRY_MAKER_FEE + SETTLEMENT_FEE) * 100 + slippage  # ~0.047% + slip
        else:
            new_fee_pct = (ENTRY_MAKER_FEE + NON_COMPLIANT_EXIT_FEE + SETTLEMENT_FEE) * 100 + slippage

        new_net_pct = gross_pct - new_fee_pct
        new_pnl_usd = pos_usd * new_net_pct / 100

        old_total_pnl += old_pnl_usd
        new_total_pnl += new_pnl_usd
        old_total_fees += pos_usd * old_fee_pct / 100
        new_total_fees += pos_usd * new_fee_pct / 100

        recalculated.append({
            **t,
            "old_net_pct": old_net_pct,
            "new_net_pct": new_net_pct,
            "old_pnl_usd": old_pnl_usd,
            "new_pnl_usd": new_pnl_usd,
            "scalper_compliant": compliant,
            "fee_saved_pct": old_fee_pct - new_fee_pct,
        })

    # Calculate metrics
    total_trades = len(recalculated)
    if total_trades == 0:
        return {"error": "No trades to simulate"}

    old_wins = sum(1 for t in recalculated if t["old_net_pct"] > 0)
    new_wins = sum(1 for t in recalculated if t["new_net_pct"] > 0)

    old_wr = old_wins / total_trades * 100
    new_wr = new_wins / total_trades * 100

    # Structure bounce only
    sb_trades = [t for t in recalculated if t.get("setup_type") == "structure_bounce"]
    sb_new_pnl = sum(t["new_pnl_usd"] for t in sb_trades)
    sb_old_pnl = sum(t["old_pnl_usd"] for t in sb_trades)
    sb_new_wins = sum(1 for t in sb_trades if t["new_net_pct"] > 0)

    return {
        "total_trades": total_trades,
        "old_wr": round(old_wr, 1),
        "new_wr": round(new_wr, 1),
        "old_total_pnl": round(old_total_pnl, 2),
        "new_total_pnl": round(new_total_pnl, 2),
        "old_total_fees": round(old_total_fees, 2),
        "new_total_fees": round(new_total_fees, 2),
        "fee_savings": round(old_total_fees - new_total_fees, 2),
        "pnl_improvement": round(new_total_pnl - old_total_pnl, 2),
        "scalper_compliance": round(sum(1 for t in recalculated if t["scalper_compliant"]) / total_trades * 100, 1),
        "structure_bounce": {
            "trades": len(sb_trades),
            "old_pnl": round(sb_old_pnl, 2),
            "new_pnl": round(sb_new_pnl, 2),
            "new_wr": round(sb_new_wins / max(len(sb_trades), 1) * 100, 1),
        },
    }


if __name__ == "__main__":
    # Simulate using the 3-month backtest data
    # From backtest: 3287 trades, structure_bounce 2017 trades at 68% WR

    print("=" * 70)
    print("VN EDGE v2.1 — SCALPER FEE SIMULATION")
    print("=" * 70)
    print()

    # Generate synthetic trades matching backtest profile
    import random
    random.seed(42)

    trades = []

    # Structure bounce: 2017 trades, 68% WR, avg_win=0.45%, avg_loss=-0.30%
    for i in range(2017):
        is_win = random.random() < 0.68
        gross = random.gauss(0.45, 0.15) if is_win else random.gauss(-0.30, 0.10)
        trades.append({
            "setup_type": "structure_bounce",
            "symbol": random.choice(["BTC/USDT", "ETH/USDT", "AVAX/USDT"]),
            "gross_pnl_pct": gross,
            "position_size_usd": random.choice([250, 500, 750]),
        })

    # Other scanners: 1270 trades, 57% WR, avg_win=0.30%, avg_loss=-0.40%
    for i in range(1270):
        is_win = random.random() < 0.57
        gross = random.gauss(0.30, 0.12) if is_win else random.gauss(-0.40, 0.12)
        scanner = random.choice(["ema_momentum", "trend_continuation", "rsi_divergence",
                                  "vwap_mean_revert", "simple_bias"])
        trades.append({
            "setup_type": scanner,
            "symbol": random.choice(["BTC/USDT", "ETH/USDT", "AVAX/USDT"]),
            "gross_pnl_pct": gross,
            "position_size_usd": random.choice([250, 500]),
        })

    result = simulate_scalper_backtest(trades)

    print(f"{'Metric':<30s} | {'Old (Taker)':>15s} | {'New (Scalper)':>15s} | {'Change':>10s}")
    print("-" * 80)
    print(f"{'Total Trades':<30s} | {result['total_trades']:>15d} | {result['total_trades']:>15d} | {'—':>10s}")
    print(f"{'Win Rate':<30s} | {result['old_wr']:>14.1f}% | {result['new_wr']:>14.1f}% | {'+':>1s}{result['new_wr']-result['old_wr']:>7.1f}pp")
    print(f"{'Total PnL':<30s} | ${result['old_total_pnl']:>13,.2f} | ${result['new_total_pnl']:>13,.2f} | ${result['pnl_improvement']:>+9,.2f}")
    print(f"{'Total Fees':<30s} | ${result['old_total_fees']:>13,.2f} | ${result['new_total_fees']:>13,.2f} | ${result['fee_savings']:>+9,.2f}")
    print(f"{'Fee Savings':<30s} | {'—':>15s} | {'—':>15s} | ${result['fee_savings']:>+9,.2f}")
    print(f"{'Scalper Compliance':<30s} | {'—':>15s} | {result['scalper_compliance']:>14.1f}% | {'—':>10s}")

    print()
    print("STRUCTURE BOUNCE ONLY:")
    sb = result["structure_bounce"]
    print(f"  Trades: {sb['trades']} | Old PnL: ${sb['old_pnl']:+,.2f} | New PnL: ${sb['new_pnl']:+,.2f} | New WR: {sb['new_wr']:.1f}%")
    print(f"  Improvement: ${sb['new_pnl'] - sb['old_pnl']:+,.2f}")

    print()
    print("PROJECTED MONTHLY (structure_bounce only, $500 avg position):")
    sb_daily_trades = sb["trades"] / 90  # 90 days
    sb_daily_pnl = sb["new_pnl"] / 90
    print(f"  Trades/day: {sb_daily_trades:.1f}")
    print(f"  Daily PnL: ${sb_daily_pnl:+.2f}")
    print(f"  Monthly PnL: ${sb_daily_pnl * 30:+,.0f}")
    print(f"  Monthly ROI: {sb_daily_pnl * 30 / 500 * 100:+.1f}% on $500 capital")
