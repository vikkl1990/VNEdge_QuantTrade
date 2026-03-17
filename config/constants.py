"""
Enums and constants for the crypto trading bot.
"""

from enum import Enum, unique


@unique
class BotMode(str, Enum):
    """Operating mode for the trading bot."""
    SIGNAL_ONLY = "signal_only"
    PAPER = "paper"
    LIVE = "live"
    BACKTEST = "backtest"
    FORWARD_TEST = "forward_test"

    @classmethod
    def from_str(cls, value: str) -> "BotMode":
        """Parse a mode string, case-insensitive."""
        normalised = value.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        raise ValueError(
            f"Invalid BotMode '{value}'. "
            f"Choose from: {', '.join(m.value for m in cls)}"
        )


@unique
class SignalType(str, Enum):
    """Types of trading signals the bot can generate."""
    PRE_BUY = "pre_buy"
    BUY = "buy"
    PRE_SELL = "pre_sell"
    SELL = "sell"
    TP1 = "tp1"
    TP2 = "tp2"
    TP3 = "tp3"
    EXIT = "exit"
    FORCE_EXIT = "force_exit"
    REVERSE = "reverse"

    @property
    def is_entry(self) -> bool:
        return self in (SignalType.BUY, SignalType.SELL)

    @property
    def is_pre_signal(self) -> bool:
        return self in (SignalType.PRE_BUY, SignalType.PRE_SELL)

    @property
    def is_exit(self) -> bool:
        return self in (
            SignalType.TP1, SignalType.TP2, SignalType.TP3,
            SignalType.EXIT, SignalType.FORCE_EXIT,
        )


@unique
class MarketRegime(str, Enum):
    """Detected market regime / environment."""
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    SIDEWAYS = "sideways"
    BREAKOUT = "breakout"
    MEAN_REVERSION = "mean_reversion"
    HIGH_VOLATILITY = "high_volatility"
    LOW_LIQUIDITY = "low_liquidity"

    @property
    def is_trending(self) -> bool:
        return self in (MarketRegime.TRENDING_UP, MarketRegime.TRENDING_DOWN)

    @property
    def is_risky(self) -> bool:
        return self in (MarketRegime.HIGH_VOLATILITY, MarketRegime.LOW_LIQUIDITY)


@unique
class TradeGrade(str, Enum):
    """Quality grade assigned to a trade setup."""
    A_PLUS = "A+"
    A = "A"
    B = "B"
    C = "C"
    REJECT = "REJECT"

    # Ordered from best to worst for comparison
    _RANK = None  # placeholder; see _rank property

    @property
    def rank(self) -> int:
        """Lower rank = higher quality."""
        _ranks = {
            TradeGrade.A_PLUS: 0,
            TradeGrade.A: 1,
            TradeGrade.B: 2,
            TradeGrade.C: 3,
            TradeGrade.REJECT: 4,
        }
        return _ranks[self]

    def meets_minimum(self, minimum: "TradeGrade") -> bool:
        """Return True if this grade is at least as good as *minimum*."""
        return self.rank <= minimum.rank

    @classmethod
    def from_str(cls, value: str) -> "TradeGrade":
        normalised = value.strip().upper()
        for member in cls:
            if member.value == normalised:
                return member
        raise ValueError(
            f"Invalid TradeGrade '{value}'. "
            f"Choose from: {', '.join(m.value for m in cls)}"
        )


@unique
class OrderSide(str, Enum):
    """Direction of a trade."""
    LONG = "long"
    SHORT = "short"

    @property
    def opposite(self) -> "OrderSide":
        return OrderSide.SHORT if self is OrderSide.LONG else OrderSide.LONG


@unique
class OrderType(str, Enum):
    """Type of order to place on the exchange."""
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"
    TAKE_PROFIT = "take_profit"
    TAKE_PROFIT_LIMIT = "take_profit_limit"
    TRAILING_STOP = "trailing_stop"


@unique
class TimeInForce(str, Enum):
    """Time-in-force policy for orders."""
    GTC = "GTC"   # Good Till Cancelled
    IOC = "IOC"   # Immediate Or Cancel
    FOK = "FOK"   # Fill Or Kill


@unique
class MarketType(str, Enum):
    """Market type: spot or futures (perpetual)."""
    SPOT = "spot"
    FUTURES = "futures"


@unique
class PositionSide(str, Enum):
    """Position side for futures hedging modes."""
    LONG = "long"
    SHORT = "short"
    BOTH = "both"


@unique
class ExchangeName(str, Enum):
    """Supported exchange identifiers."""
    BINANCE = "binance"
    BYBIT = "bybit"
    OKX = "okx"
    DELTA = "delta"

    @classmethod
    def from_str(cls, value: str) -> "ExchangeName":
        normalised = value.strip().lower()
        for member in cls:
            if member.value == normalised:
                return member
        raise ValueError(
            f"Invalid ExchangeName '{value}'. "
            f"Choose from: {', '.join(m.value for m in cls)}"
        )


@unique
class ExchangeStatus(str, Enum):
    """Exchange connection lifecycle status."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    ERROR = "error"


@unique
class TradeStatus(str, Enum):
    """Current status of a trade."""
    PENDING = "pending"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


@unique
class AlertLevel(str, Enum):
    """System alert severity level."""
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


@unique
class AlertType(str, Enum):
    """Type of trade alert."""
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    TRAILING_STOP = "TRAILING_STOP"
    BREAK_EVEN = "BREAK_EVEN"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    SIGNAL_PRE = "SIGNAL_PRE"
    SIGNAL_CONFIRMED = "SIGNAL_CONFIRMED"


# ---------------------------------------------------------------------------
# Numeric / string constants
# ---------------------------------------------------------------------------

# Default precision for USD-quoted pairs
DEFAULT_PRICE_PRECISION = 2
DEFAULT_QTY_PRECISION = 6

# Timestamp formats
ISO_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
LOG_TS_FORMAT = "%Y-%m-%d %H:%M:%S"
FILE_TS_FORMAT = "%Y%m%d_%H%M%S"

# Network / retry defaults
DEFAULT_RETRY_ATTEMPTS = 3
DEFAULT_RETRY_DELAY = 1.0        # seconds
DEFAULT_RETRY_BACKOFF = 2.0      # exponential multiplier
DEFAULT_REQUEST_TIMEOUT = 30.0   # seconds

# State file names
STATE_DIR = "data/state"
POSITIONS_FILE = "open_positions.json"
SIGNALS_FILE = "active_signals.json"
BOT_STATE_FILE = "bot_state.json"

# Grade thresholds for confidence scores
GRADE_THRESHOLDS = {
    TradeGrade.A_PLUS: 90,
    TradeGrade.A: 80,
    TradeGrade.B: 65,
    TradeGrade.C: 50,
    TradeGrade.REJECT: 0,
}


def confidence_to_grade(confidence: int) -> TradeGrade:
    """Convert a confidence score (0-100) to a TradeGrade."""
    for grade, threshold in GRADE_THRESHOLDS.items():
        if confidence >= threshold:
            return grade
    return TradeGrade.REJECT
