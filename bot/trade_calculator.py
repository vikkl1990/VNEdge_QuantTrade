"""
Trade Calculator — Delta Exchange perpetual futures math.

Computes liquidation price, PnL, position sizing, ROE using
the standard USDT-margined perpetual contract formulas.

Delta Exchange uses ISOLATED margin mode with these rates:
- Maintenance Margin: 0.5% for position < 5 BTC, then +0.075% per BTC above
- Initial Margin: 1/leverage (e.g., 10x = 10%, 50x = 2%)
- Fees: Taker 0.06%, Maker 0.04%, Settlement 0.06%

References:
- https://guides.delta.exchange/delta-exchange-user-guide/trading-guide/margin-explainer/margin-explainer/liquidation
- https://www.delta.exchange/support/solutions?categoryId=80000464979&folderId=80000726080&articleId=80001177923
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Constants (Delta Exchange India)
# ─────────────────────────────────────────────────────────────

TAKER_FEE = 0.0006       # 0.06%
MAKER_FEE = 0.0004       # 0.04%
SETTLEMENT_FEE = 0.0006  # 0.06%
ROUND_TRIP_TAKER = TAKER_FEE * 2 + SETTLEMENT_FEE  # 0.18%
ROUND_TRIP_MAKER = MAKER_FEE * 2 + SETTLEMENT_FEE  # 0.14%

# Maintenance margin rate (for positions < 5 BTC equivalent)
BASE_MM_RATE = 0.005  # 0.5%


# ─────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────

@dataclass
class TradeCalcResult:
    """Complete trade calculation output."""
    # Input echo
    side: str              # "long" or "short"
    entry_price: float
    leverage: int
    margin_usd: float      # margin/stake in USDT
    quantity: float         # position quantity in base asset

    # Position
    position_size_usd: float  # notional value
    initial_margin: float     # = position_size / leverage
    maintenance_margin: float # = position_size × MM_rate

    # Liquidation
    liquidation_price: float
    liquidation_buffer_pct: float  # distance from entry to liq as %
    bankruptcy_price: float

    # Risk status
    risk_status: str       # "SAFE", "WARNING", "REJECT"
    risk_reason: str

    # SL/TP with safety checks
    sl_price: float
    sl_pct: float          # SL distance as % of entry
    sl_pnl_usd: float     # loss at SL in USD
    sl_vs_liq_pct: float   # SL uses what % of liquidation buffer

    tp1_price: float
    tp1_pct: float
    tp1_pnl_usd: float
    tp2_price: float
    tp2_pct: float
    tp2_pnl_usd: float
    tp3_price: float
    tp3_pct: float
    tp3_pnl_usd: float

    # ROE at each TP
    roe_tp1: float         # % return on margin
    roe_tp2: float
    roe_tp3: float

    # Fees
    entry_fee: float
    exit_fee: float
    total_fees: float

    def to_dict(self) -> dict:
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in self.__dict__.items()}


# ─────────────────────────────────────────────────────────────
# Core calculations
# ─────────────────────────────────────────────────────────────

def calc_maintenance_margin_rate(position_size_btc: float) -> float:
    """Delta's tiered maintenance margin rate.

    MM% = 0.5% for ≤ 5 BTC, then +0.075% per BTC above 5.
    """
    if position_size_btc <= 5:
        return BASE_MM_RATE
    return BASE_MM_RATE + 0.00075 * (position_size_btc - 5)


def calc_liquidation_price(
    entry_price: float,
    leverage: int,
    side: str,
    mm_rate: float = BASE_MM_RATE,
) -> float:
    """Liquidation price for isolated margin position.

    LONG:  Liq = Entry × (1 - 1/Leverage + MM_rate)
    SHORT: Liq = Entry × (1 + 1/Leverage - MM_rate)

    At liquidation: Position Margin - Unrealized PnL = Maintenance Margin
    """
    im_rate = 1.0 / leverage  # Initial margin rate

    if side == "long":
        liq = entry_price * (1.0 - im_rate + mm_rate)
    else:
        liq = entry_price * (1.0 + im_rate - mm_rate)

    return round(liq, 2)


def calc_bankruptcy_price(
    entry_price: float,
    leverage: int,
    side: str,
) -> float:
    """Bankruptcy price — where unrealized loss = entire margin.

    LONG:  Bankrupt = Entry × (1 - 1/Leverage)
    SHORT: Bankrupt = Entry × (1 + 1/Leverage)
    """
    im_rate = 1.0 / leverage

    if side == "long":
        return round(entry_price * (1.0 - im_rate), 2)
    else:
        return round(entry_price * (1.0 + im_rate), 2)


def calc_pnl(
    entry_price: float,
    exit_price: float,
    quantity: float,
    side: str,
    fee_rate: float = ROUND_TRIP_TAKER,
) -> tuple:
    """Calculate PnL for a trade.

    Returns: (gross_pnl_usd, fee_usd, net_pnl_usd, pnl_pct, roe_pct)

    LONG PnL:  (Exit - Entry) × Quantity
    SHORT PnL: (Entry - Exit) × Quantity

    ROE = Net PnL / Initial Margin × 100
    """
    if side == "long":
        gross = (exit_price - entry_price) * quantity
    else:
        gross = (entry_price - exit_price) * quantity

    position_value = entry_price * quantity
    fees = position_value * fee_rate
    net = gross - fees
    pnl_pct = (gross / position_value * 100) if position_value > 0 else 0

    return gross, fees, net, pnl_pct


def calc_roe(
    net_pnl: float,
    margin: float,
) -> float:
    """Return on Equity (margin).

    ROE% = (Net PnL / Margin) × 100
    """
    if margin <= 0:
        return 0.0
    return (net_pnl / margin) * 100


def calc_target_price(
    entry_price: float,
    leverage: int,
    side: str,
    target_roe_pct: float,
    fee_rate: float = ROUND_TRIP_TAKER,
) -> float:
    """Calculate target price for desired ROE%.

    For LONG:
        Target = Entry × (1 + ROE%/100/Leverage + fee_rate)
    For SHORT:
        Target = Entry × (1 - ROE%/100/Leverage - fee_rate)
    """
    move_pct = target_roe_pct / 100.0 / leverage

    if side == "long":
        return round(entry_price * (1.0 + move_pct + fee_rate), 2)
    else:
        return round(entry_price * (1.0 - move_pct - fee_rate), 2)


# ─────────────────────────────────────────────────────────────
# Full trade calculation
# ─────────────────────────────────────────────────────────────

def calculate_trade(
    entry_price: float,
    side: str,
    margin_usd: float = 25.0,
    leverage: int = 10,
    sl_price: Optional[float] = None,
    sl_pct: Optional[float] = None,
    tp1_rr: float = 1.0,
    tp2_rr: float = 1.5,
    tp3_rr: float = 2.0,
    fee_type: str = "taker",
    **kwargs,
) -> TradeCalcResult:
    """Complete trade calculation with liquidation safety.

    Args:
        entry_price: Entry price
        side: "long" or "short"
        margin_usd: Margin/stake in USDT
        leverage: Leverage multiplier
        sl_price: Stop loss price (optional, if not provided uses sl_pct)
        sl_pct: Stop loss as % of entry (optional, default 0.5%)
        tp1_rr: TP1 risk:reward ratio
        tp2_rr: TP2 risk:reward ratio
        tp3_rr: TP3 risk:reward ratio
        fee_type: "taker" or "maker"

    Returns:
        TradeCalcResult with all calculations
    """
    fee_rate = ROUND_TRIP_TAKER if fee_type == "taker" else ROUND_TRIP_MAKER

    # Contract-based position sizing (Delta Exchange specs)
    # BTC: 1 contract = 0.001 BTC, ETH: 1 contract = 0.01 ETH
    contract_size = kwargs.get("contract_size", 0.001)  # default BTC

    raw_position_usd = margin_usd * leverage
    raw_contracts = raw_position_usd / (entry_price * contract_size)
    num_contracts = max(1, int(raw_contracts))  # round DOWN to whole contracts
    quantity = num_contracts * contract_size
    position_size_usd = round(quantity * entry_price, 2)  # actual after rounding

    # Margin calculations (based on actual position, not raw)
    initial_margin = position_size_usd / leverage
    mm_rate = calc_maintenance_margin_rate(quantity)
    maintenance_margin = position_size_usd * mm_rate

    # Liquidation price
    liq_price = calc_liquidation_price(entry_price, leverage, side, mm_rate)
    bankruptcy_price = calc_bankruptcy_price(entry_price, leverage, side)
    liq_buffer_pct = abs(entry_price - liq_price) / entry_price * 100

    # Stop loss
    if sl_price is None:
        if sl_pct is None:
            sl_pct = 0.5  # default 0.5%
        if side == "long":
            sl_price = entry_price * (1.0 - sl_pct / 100)
        else:
            sl_price = entry_price * (1.0 + sl_pct / 100)
    sl_price = round(sl_price, 2)
    actual_sl_pct = abs(entry_price - sl_price) / entry_price * 100
    sl_dist = abs(entry_price - sl_price)

    # SL P&L
    sl_gross, sl_fees, sl_net, sl_pnl_pct = calc_pnl(entry_price, sl_price, quantity, side, fee_rate)

    # SL vs liquidation safety
    sl_vs_liq = (actual_sl_pct / liq_buffer_pct * 100) if liq_buffer_pct > 0 else 100

    # Risk status
    risk_status = "SAFE"
    risk_reason = ""
    # Risk assessment — super scalp mode allows tight buffers with proper SL
    if liq_buffer_pct < 0.3:
        risk_status = "REJECT"
        risk_reason = f"Liquidation buffer {liq_buffer_pct:.1f}% — too close even for scalp"
    elif sl_vs_liq >= 80:
        risk_status = "REJECT"
        risk_reason = f"SL uses {sl_vs_liq:.0f}% of liq buffer — SL too wide for this leverage"
    elif sl_vs_liq >= 40 or liq_buffer_pct < 1.0:
        risk_status = "WARNING"
        risk_reason = f"Tight buffer: {liq_buffer_pct:.1f}% | SL uses {sl_vs_liq:.0f}% — need precise SL"
    elif liq_buffer_pct < 3.0:
        risk_status = "WARNING"
        risk_reason = f"Buffer {liq_buffer_pct:.1f}% — watch closely"

    # Take profit levels based on R:R
    risk_dist = sl_dist  # 1R = distance to SL

    if side == "long":
        tp1 = round(entry_price + risk_dist * tp1_rr, 2)
        tp2 = round(entry_price + risk_dist * tp2_rr, 2)
        tp3 = round(entry_price + risk_dist * tp3_rr, 2)
    else:
        tp1 = round(entry_price - risk_dist * tp1_rr, 2)
        tp2 = round(entry_price - risk_dist * tp2_rr, 2)
        tp3 = round(entry_price - risk_dist * tp3_rr, 2)

    # TP P&L calculations
    _, _, tp1_net, tp1_pct = calc_pnl(entry_price, tp1, quantity, side, fee_rate)
    _, _, tp2_net, tp2_pct = calc_pnl(entry_price, tp2, quantity, side, fee_rate)
    _, _, tp3_net, tp3_pct = calc_pnl(entry_price, tp3, quantity, side, fee_rate)

    # ROE at each level
    roe1 = calc_roe(tp1_net, margin_usd)
    roe2 = calc_roe(tp2_net, margin_usd)
    roe3 = calc_roe(tp3_net, margin_usd)

    # Fees
    entry_fee = position_size_usd * (TAKER_FEE if fee_type == "taker" else MAKER_FEE)
    exit_fee = entry_fee + position_size_usd * SETTLEMENT_FEE

    return TradeCalcResult(
        side=side,
        entry_price=entry_price,
        leverage=leverage,
        margin_usd=margin_usd,
        quantity=round(quantity, 6),
        position_size_usd=round(position_size_usd, 2),
        initial_margin=round(initial_margin, 2),
        maintenance_margin=round(maintenance_margin, 2),
        liquidation_price=liq_price,
        liquidation_buffer_pct=round(liq_buffer_pct, 2),
        bankruptcy_price=bankruptcy_price,
        risk_status=risk_status,
        risk_reason=risk_reason,
        sl_price=sl_price,
        sl_pct=round(actual_sl_pct, 3),
        sl_pnl_usd=round(sl_net, 2),
        sl_vs_liq_pct=round(sl_vs_liq, 1),
        tp1_price=tp1,
        tp1_pct=round(abs(tp1 - entry_price) / entry_price * 100, 3),
        tp1_pnl_usd=round(tp1_net, 2),
        tp2_price=tp2,
        tp2_pct=round(abs(tp2 - entry_price) / entry_price * 100, 3),
        tp2_pnl_usd=round(tp2_net, 2),
        tp3_price=tp3,
        tp3_pct=round(abs(tp3 - entry_price) / entry_price * 100, 3),
        tp3_pnl_usd=round(tp3_net, 2),
        roe_tp1=round(roe1, 2),
        roe_tp2=round(roe2, 2),
        roe_tp3=round(roe3, 2),
        entry_fee=round(entry_fee, 4),
        exit_fee=round(exit_fee, 4),
        total_fees=round(entry_fee + exit_fee, 4),
    )


# ─────────────────────────────────────────────────────────────
# Quick test
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    def print_trade(r):
        print(f"  Position: ${r.position_size_usd} ({r.quantity} units)")
        print(f"  Margin: ${r.initial_margin:.2f} | MM: ${r.maintenance_margin:.2f}")
        print(f"  Liquidation: ${r.liquidation_price} (buffer: {r.liquidation_buffer_pct}%)")
        print(f"  Bankruptcy:  ${r.bankruptcy_price}")
        print(f"  Risk: {r.risk_status} {r.risk_reason}")
        print(f"  SL: ${r.sl_price} ({r.sl_pct}%) → PnL: ${r.sl_pnl_usd} | uses {r.sl_vs_liq_pct}% of liq buffer")
        print(f"  TP1: ${r.tp1_price} ({r.tp1_pct}%) → PnL: ${r.tp1_pnl_usd} | ROE: {r.roe_tp1}%")
        print(f"  TP2: ${r.tp2_price} ({r.tp2_pct}%) → PnL: ${r.tp2_pnl_usd} | ROE: {r.roe_tp2}%")
        print(f"  TP3: ${r.tp3_price} ({r.tp3_pct}%) → PnL: ${r.tp3_pnl_usd} | ROE: {r.roe_tp3}%")
        print(f"  Fees: ${r.total_fees}")

    # BTC SHORT at $71,300, 10x leverage, $25 margin
    print("=== BTC SHORT @ $71,300 | 10x | $25 margin ===")
    r = calculate_trade(entry_price=71300, side="short", margin_usd=25, leverage=10, sl_pct=0.5, contract_size=0.001)
    print_trade(r)

    print("\n=== ETH LONG @ $2,180 | 20x | $25 margin ===")
    r = calculate_trade(entry_price=2180, side="long", margin_usd=25, leverage=20, sl_pct=0.4, contract_size=0.01)
    print_trade(r)

    print("\n=== BTC LONG @ $71,300 | 50x | $25 margin (HIGH LEV) ===")
    r = calculate_trade(entry_price=71300, side="long", margin_usd=25, leverage=50, sl_pct=0.3, contract_size=0.001)
    print_trade(r)

    # Safe leverage table
    print("\n=== SAFE LEVERAGE TABLE (BTC $71,300, $25 margin, 0.5% SL) ===")
    print(f"  {'Lev':>4s} | {'Contracts':>9s} | {'Position':>10s} | {'Margin':>8s} | {'Liq Buffer':>10s} | {'SL%Liq':>6s} | {'TP1 PnL':>8s} | {'TP1 ROE':>8s} | {'Status':>8s}")
    for lev in [3, 5, 10, 15, 20, 25, 50]:
        r = calculate_trade(entry_price=71300, side="long", margin_usd=25, leverage=lev, sl_pct=0.5, contract_size=0.001)
        print(f"  {lev:4d}x | {int(r.position_size_usd/71.3):9d}ct | ${r.position_size_usd:9.2f} | ${r.initial_margin:7.2f} | {r.liquidation_buffer_pct:9.1f}% | {r.sl_vs_liq_pct:5.1f}% | ${r.tp1_pnl_usd:7.2f} | {r.roe_tp1:7.1f}% | {r.risk_status}")
