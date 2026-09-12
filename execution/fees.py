"""
Fee model — single source of truth for trading costs.

Delta Exchange India perpetual futures (verified against GET /v2/products on
2026-09-10 for every mapped product):

    maker_commission_rate = 0.0002   (0.02 % of notional)
    taker_commission_rate = 0.0005   (0.05 % of notional)
    settlement fee        = none     (perpetuals; settlement_time is null)
    GST                   = 18 % levied on the commission itself (India)

Effective per-side rates incl. GST:  maker 0.0236 %, taker 0.059 %.

Fees are charged on NOTIONAL (price × contract value × contracts), never on
margin. A 10x-leveraged $100 margin position is a $1,000 notional position
and pays fees on $1,000.

Every leg of a trade is charged separately: the entry, and each partial or
final exit. Exits driven by stop / take-profit / trail orders are market
orders → taker. Entries are maker when the resting limit order fills at the
signal price, taker when the order had to cross the spread (slippage > 0)
or the strategy is configured taker-only.

Scalper Offer (Delta Exchange India, confirmed live 2026-09-12): once an
account has opted in (irreversible, done once on the Futures page), every
closing leg of a Futures position — full or partial, maker or taker — is
free of charge PROVIDED that leg executes within a window measured from
when the position was OPENED: 30 minutes for BTCUSD/ETHUSD, 15 minutes for
every other Future. Liquidations never qualify. The entry leg always pays.
Each leg is judged independently on its own elapsed time (FeeLeg.elapsed_sec)
— a TP1 partial inside the window and a later stop-out outside it are priced
differently on the same trade. This is OFF by default (fees.scalper_offer.
enabled: false) because it is a per-account opt-in the exchange API cannot
report; it must be confirmed on the account and turned on deliberately.

Funding (8-hourly, paid/received on notional) is exposed for completeness
but not applied by default: scalp holds are minutes and funding is applied
at fixed timestamps, so charging it pro-rata would be wrong more often than
right. Set fees.funding.apply: true to charge an estimate.

Usage::

    from execution.fees import get_fee_model
    fm = get_fee_model()                       # from settings.yaml `fees:`
    fm.side_pct("maker")                       # 0.0236 (% of notional)
    fm.round_trip_pct("maker", "taker")        # 0.0826 (conservative: no offer credit)
    legs = [FeeLeg(fraction=0.35, price=tp1, liquidity="taker", elapsed_sec=612), ...]
    res = fm.trade_fees(entry_price, entry_liquidity="maker", exit_legs=legs, symbol="BTC/USDT")
    res.total_pct                              # % of ENTRY notional
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

# Exchange defaults (Delta Exchange India, perpetual futures)
DEFAULT_MAKER_RATE = 0.0002
DEFAULT_TAKER_RATE = 0.0005
DEFAULT_GST_RATE = 0.18

# Scalper Offer free-close windows in seconds, keyed by base asset; "_default"
# covers everything not listed. Confirmed live 2026-09-12 (Delta Exchange
# India support article + account opt-in).
DEFAULT_SCALPER_WINDOWS: Dict[str, float] = {
    "BTC": 30 * 60,
    "ETH": 30 * 60,
    "_default": 15 * 60,
}

Liquidity = str  # "maker" | "taker"


@dataclass(frozen=True)
class FeeLeg:
    """One executed leg of a trade.

    fraction    : share of the ORIGINAL position closed by this leg (0-1).
                  For the entry leg this is 1.0.
    price       : execution price of the leg.
    liquidity   : "maker" or "taker".
    elapsed_sec : seconds from position OPEN to this leg's execution. Used
                  only for exit legs, only when the Scalper Offer is enabled,
                  to decide whether this specific leg falls inside the free
                  window. Irrelevant for the entry leg.
    liquidation : True if this leg was a forced liquidation, which never
                  qualifies for the Scalper Offer regardless of timing.
    """
    fraction: float
    price: float
    liquidity: Liquidity = "taker"
    elapsed_sec: float = 0.0
    liquidation: bool = False


@dataclass
class FeeBreakdown:
    entry_pct: float          # % of entry notional
    exit_pct: float           # % of entry notional (sum of legs)
    funding_pct: float        # % of entry notional (0 unless enabled)
    total_pct: float          # entry + exit + funding
    legs: List[Dict[str, Any]] = field(default_factory=list)

    def usd(self, entry_notional_usd: float) -> float:
        return entry_notional_usd * self.total_pct / 100.0


class FeeModel:
    """Per-side commission model with GST, per-leg accounting."""

    def __init__(
        self,
        maker_rate: float = DEFAULT_MAKER_RATE,
        taker_rate: float = DEFAULT_TAKER_RATE,
        gst_rate: float = DEFAULT_GST_RATE,
        free_exit: bool = False,
        funding_rate_8h: float = 0.0001,
        apply_funding: bool = False,
        per_product: Optional[Dict[str, Dict[str, float]]] = None,
        scalper_offer: bool = False,
        scalper_windows: Optional[Dict[str, float]] = None,
    ) -> None:
        self.maker_rate = float(maker_rate)
        self.taker_rate = float(taker_rate)
        self.gst_rate = float(gst_rate)
        # Unconditional "every exit is free" override — kept for tests and
        # for estimation contexts with no timing info. NOT the Scalper Offer
        # (which is time-windowed); use scalper_offer for that.
        self.free_exit = bool(free_exit)
        self.funding_rate_8h = float(funding_rate_8h)
        self.apply_funding = bool(apply_funding)
        # optional per-product override: {"BTC/USDT": {"maker": .., "taker": ..}}
        self.per_product: Dict[str, Dict[str, float]] = dict(per_product or {})
        # Scalper Offer (see module docstring). OFF by default — this is a
        # per-account opt-in the exchange API cannot report; confirm on the
        # account before enabling in settings.yaml.
        self.scalper_offer = bool(scalper_offer)
        self.scalper_windows: Dict[str, float] = dict(scalper_windows or DEFAULT_SCALPER_WINDOWS)

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "FeeModel":
        cfg = (config or {}).get("fees", {}) or {}
        funding = cfg.get("funding", {}) or {}
        scalper = cfg.get("scalper_offer", {}) or {}
        return cls(
            maker_rate=cfg.get("maker_rate", DEFAULT_MAKER_RATE),
            taker_rate=cfg.get("taker_rate", DEFAULT_TAKER_RATE),
            gst_rate=cfg.get("gst_rate", DEFAULT_GST_RATE),
            free_exit=cfg.get("free_exit", False),
            funding_rate_8h=funding.get("rate_8h", 0.0001),
            apply_funding=funding.get("apply", False),
            per_product=cfg.get("per_product"),
            scalper_offer=scalper.get("enabled", False),
            scalper_windows=scalper.get("windows"),
        )

    def update_from_products(self, products: Iterable[Dict[str, Any]],
                             symbol_map: Dict[str, int]) -> List[str]:
        """Refresh per-product rates from GET /v2/products payloads.

        symbol_map: {"BTC/USDT": 27, ...}. Returns a list of human-readable
        differences versus the configured defaults (for logging).
        """
        by_id = {p.get("id"): p for p in products}
        diffs: List[str] = []
        for sym, pid in symbol_map.items():
            p = by_id.get(pid)
            if not p:
                continue
            try:
                mk = float(p.get("maker_commission_rate"))
                tk = float(p.get("taker_commission_rate"))
            except (TypeError, ValueError):
                continue
            self.per_product[sym] = {"maker": mk, "taker": tk}
            if abs(mk - self.maker_rate) > 1e-9 or abs(tk - self.taker_rate) > 1e-9:
                diffs.append(f"{sym}: exchange maker={mk:.4%} taker={tk:.4%} (config {self.maker_rate:.4%}/{self.taker_rate:.4%})")
        return diffs

    # ------------------------------------------------------------------
    # Rates
    # ------------------------------------------------------------------
    def base_rate(self, liquidity: Liquidity, symbol: Optional[str] = None) -> float:
        """Commission rate as a fraction of notional, before GST."""
        rates = self.per_product.get(symbol or "", None)
        if liquidity == "maker":
            return rates["maker"] if rates else self.maker_rate
        return rates["taker"] if rates else self.taker_rate

    def side_rate(self, liquidity: Liquidity, symbol: Optional[str] = None) -> float:
        """Effective fraction of notional per side, GST included."""
        return self.base_rate(liquidity, symbol) * (1.0 + self.gst_rate)

    def side_pct(self, liquidity: Liquidity, symbol: Optional[str] = None) -> float:
        """Effective per-side fee in PERCENT of notional (0.059 for taker)."""
        return self.side_rate(liquidity, symbol) * 100.0

    def round_trip_pct(self, entry: Liquidity = "maker", exit: Liquidity = "taker",
                       symbol: Optional[str] = None) -> float:
        """Entry + one full exit, in percent of notional.

        Conservative by design: this has no hold-time to check against the
        Scalper Offer window, so it never credits the offer even when
        enabled. Used for pre-trade estimates (EV gates, breakeven buffers,
        fee-schedule display) where the eventual hold time is unknown.
        """
        exit_pct = 0.0 if self.free_exit else self.side_pct(exit, symbol)
        return self.side_pct(entry, symbol) + exit_pct

    def scalper_window_sec(self, symbol: Optional[str] = None) -> float:
        """Free-close window for `symbol`'s base asset (seconds)."""
        base = (symbol or "").split("/")[0].upper()
        return self.scalper_windows.get(base, self.scalper_windows.get("_default", 15 * 60))

    def exit_leg_is_free(self, leg: "FeeLeg", symbol: Optional[str] = None) -> bool:
        """Whether this specific exit leg pays no closing fee."""
        if self.free_exit:
            return True
        if not self.scalper_offer or leg.liquidation:
            return False
        return leg.elapsed_sec <= self.scalper_window_sec(symbol)

    # ------------------------------------------------------------------
    # Per-trade accounting
    # ------------------------------------------------------------------
    def entry_liquidity(self, order_type: str = "auto", slippage_bps: float = 0.0) -> Liquidity:
        """Classify the entry: a resting order that filled at the signal price
        is maker; anything that crossed the spread is taker."""
        ot = (order_type or "auto").lower()
        if ot in ("taker", "taker_only", "market"):
            return "taker"
        if ot in ("maker", "maker_only", "post_only"):
            return "maker"
        return "maker" if (slippage_bps or 0.0) <= 0.0 else "taker"

    def trade_fees(
        self,
        entry_price: float,
        entry_liquidity: Liquidity = "maker",
        exit_legs: Optional[Iterable[FeeLeg]] = None,
        symbol: Optional[str] = None,
        hold_seconds: float = 0.0,
    ) -> FeeBreakdown:
        """Fees for a whole trade, expressed as % of the ENTRY notional.

        Each exit leg is charged on its own notional (fraction × leg price),
        so a leg closed higher than entry pays slightly more than one closed
        lower — exactly as the exchange does it.
        """
        if entry_price <= 0:
            return FeeBreakdown(0.0, 0.0, 0.0, 0.0)
        legs_out: List[Dict[str, Any]] = []
        entry_pct = self.side_pct(entry_liquidity, symbol)
        legs_out.append({"leg": "entry", "fraction": 1.0, "price": entry_price,
                         "liquidity": entry_liquidity, "pct": round(entry_pct, 5)})
        exit_pct = 0.0
        for leg in (exit_legs or []):
            if leg.fraction <= 0 or leg.price <= 0:
                continue
            free = self.exit_leg_is_free(leg, symbol)
            pct = 0.0 if free else (
                self.side_pct(leg.liquidity, symbol) * leg.fraction * (leg.price / entry_price)
            )
            exit_pct += pct
            legs_out.append({"leg": "exit", "fraction": leg.fraction, "price": leg.price,
                             "liquidity": leg.liquidity, "elapsed_sec": leg.elapsed_sec,
                             "scalper_free": free, "pct": round(pct, 5)})
        funding_pct = 0.0
        if self.apply_funding and hold_seconds > 0:
            funding_pct = self.funding_rate_8h * 100.0 * (hold_seconds / (8 * 3600.0))
        total = entry_pct + exit_pct + funding_pct
        return FeeBreakdown(round(entry_pct, 5), round(exit_pct, 5), round(funding_pct, 5),
                            round(total, 5), legs_out)

    def leg_fee_usd(self, notional_usd: float, liquidity: Liquidity, symbol: Optional[str] = None,
                    elapsed_sec: float = 0.0, is_entry: bool = False,
                    liquidation: bool = False) -> float:
        """Fee in USD for one leg executed on `notional_usd` (full notional, not margin).

        `elapsed_sec` (time since position open) and `is_entry` let this
        credit the Scalper Offer the same way trade_fees() does; omit them
        (defaults) for a pre-trade estimate, which is always charged.
        """
        if not is_entry:
            leg = FeeLeg(1.0, 0.0, liquidity, elapsed_sec=elapsed_sec, liquidation=liquidation)
            if self.exit_leg_is_free(leg, symbol):
                return 0.0
        return notional_usd * self.side_rate(liquidity, symbol)

    def describe(self) -> str:
        promo = ""
        if self.free_exit:
            promo = " (FREE exit promo ON — unconditional)"
        elif self.scalper_offer:
            promo = (f" (Scalper Offer ON — free close within "
                     f"{self.scalper_windows.get('BTC', 1800)/60:.0f}m BTC/ETH, "
                     f"{self.scalper_windows.get('_default', 900)/60:.0f}m others)")
        return (f"maker {self.side_pct('maker'):.4f}% / taker {self.side_pct('taker'):.4f}% per side incl. "
                f"{self.gst_rate:.0%} GST; round-trip maker→taker {self.round_trip_pct():.4f}%" + promo)


# ----------------------------------------------------------------------
# Process-wide singleton (config-driven, refreshable from the exchange)
# ----------------------------------------------------------------------
_MODEL: Optional[FeeModel] = None


def get_fee_model(config: Optional[Dict[str, Any]] = None) -> FeeModel:
    """Return the shared FeeModel. First call may pass config to build it;
    later calls reuse it. Without config, settings.yaml is loaded lazily."""
    global _MODEL
    if _MODEL is None:
        cfg = config
        if cfg is None:
            try:
                from config import get_config
                cfg = get_config()
            except Exception:
                cfg = {}
        _MODEL = FeeModel.from_config(cfg)
        logger.info("FeeModel: %s", _MODEL.describe())
    return _MODEL


def set_fee_model(model: FeeModel) -> None:
    global _MODEL
    _MODEL = model
