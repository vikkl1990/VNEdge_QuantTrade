"""
Configuration loader for the crypto trading bot.

Reads settings from ``config/settings.yaml`` and ``.env``, merges them into a
single typed :class:`Config` singleton accessible via :func:`get_config`.

Usage::

    from config import get_config
    cfg = get_config()
    print(cfg.risk.risk_per_trade_pct)
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Project root is two levels above this file (crypto-trading-bot/)
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _THIS_DIR.parent
DEFAULT_SETTINGS_PATH = _THIS_DIR / "settings.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


# ---------------------------------------------------------------------------
# Typed sub-config dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BotConfig:
    name: str = "CryptoAlgoBot"
    version: str = "1.0.0"
    mode: str = "paper"
    strict_mode: bool = True


@dataclass(frozen=True)
class ExchangeConfig:
    name: str = "delta"
    region: str = ""               # "india" for Delta India (api.india.delta.exchange)
    market_type: str = "futures"
    testnet: bool = True
    rate_limit: bool = True
    websocket: bool = True
    rest_fallback: bool = True
    reconnect_attempts: int = 10
    reconnect_delay: int = 5
    # Populated from .env
    api_key: str = ""
    api_secret: str = ""
    passphrase: str = ""


@dataclass(frozen=True)
class TimeframesConfig:
    primary: str = "5m"
    higher: str = "15m"
    trigger: str = "1m"


@dataclass(frozen=True)
class EMAConfig:
    fast: int = 9
    medium: int = 21
    slow: int = 50
    trend: int = 200


@dataclass(frozen=True)
class RSIConfig:
    period: int = 14
    overbought: int = 70
    oversold: int = 30


@dataclass(frozen=True)
class MACDConfig:
    fast: int = 12
    slow: int = 26
    signal: int = 9


@dataclass(frozen=True)
class ATRConfig:
    period: int = 14
    multiplier: float = 1.5


@dataclass(frozen=True)
class SupertrendConfig:
    period: int = 10
    multiplier: float = 3.0


@dataclass(frozen=True)
class BollingerConfig:
    period: int = 20
    std_dev: float = 2.0


@dataclass(frozen=True)
class VWAPConfig:
    enabled: bool = True


@dataclass(frozen=True)
class MFIConfig:
    period: int = 14
    overbought: int = 80
    oversold: int = 20


@dataclass(frozen=True)
class VolumeConfig:
    spike_multiplier: float = 2.0
    lookback: int = 20


@dataclass(frozen=True)
class IndicatorsConfig:
    ema: EMAConfig = field(default_factory=EMAConfig)
    rsi: RSIConfig = field(default_factory=RSIConfig)
    macd: MACDConfig = field(default_factory=MACDConfig)
    atr: ATRConfig = field(default_factory=ATRConfig)
    supertrend: SupertrendConfig = field(default_factory=SupertrendConfig)
    bollinger: BollingerConfig = field(default_factory=BollingerConfig)
    vwap: VWAPConfig = field(default_factory=VWAPConfig)
    mfi: MFIConfig = field(default_factory=MFIConfig)
    volume: VolumeConfig = field(default_factory=VolumeConfig)


@dataclass(frozen=True)
class FiltersConfig:
    min_confidence: int = 60
    min_grade: str = "B"
    chop_filter: bool = True
    spread_max_pct: float = 0.1
    cooldown_seconds: int = 300
    duplicate_prevention: bool = True
    max_signals_per_hour: int = 10


@dataclass(frozen=True)
class StrategyConfig:
    active: str = "multi_indicator_confluence"
    indicators: IndicatorsConfig = field(default_factory=IndicatorsConfig)
    filters: FiltersConfig = field(default_factory=FiltersConfig)


@dataclass(frozen=True)
class StopLossConfig:
    type: str = "atr"
    fixed_pct: float = 1.5
    atr_multiplier: float = 1.5


@dataclass(frozen=True)
class TakeProfitConfig:
    tp1_rr: float = 1.5
    tp2_rr: float = 2.5
    tp3_rr: float = 4.0
    tp1_close_pct: int = 40
    tp2_close_pct: int = 30
    tp3_close_pct: int = 30


@dataclass(frozen=True)
class TrailingConfig:
    enabled: bool = True
    activation_rr: float = 1.0
    trail_pct: float = 0.5
    break_even_after_tp1: bool = True


@dataclass(frozen=True)
class SafetyConfig:
    circuit_breaker_losses: int = 3
    circuit_breaker_cooldown: int = 3600
    volatility_kill_switch: bool = True
    volatility_kill_atr_multiplier: float = 3.0
    max_api_failures: int = 5
    api_failure_cooldown: int = 300
    cooloff_after_sl: int = 180


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade_pct: float = 1.0
    max_position_size_usd: float = 10000.0
    max_daily_loss_pct: float = 3.0
    max_open_positions: int = 3
    max_exposure_per_symbol_pct: float = 50.0
    max_correlated_exposure_pct: float = 100.0
    default_leverage: int = 5
    max_leverage: int = 20
    stop_loss: StopLossConfig = field(default_factory=StopLossConfig)
    take_profit: TakeProfitConfig = field(default_factory=TakeProfitConfig)
    trailing: TrailingConfig = field(default_factory=TrailingConfig)
    safety: SafetyConfig = field(default_factory=SafetyConfig)


@dataclass(frozen=True)
class PaperTradingConfig:
    initial_balance: float = 10000.0
    fee_rate: float = 0.0004
    slippage_pct: float = 0.05


@dataclass(frozen=True)
class BacktestConfig:
    start_date: str = "2025-01-01"
    end_date: str = "2025-12-31"
    fee_rate: float = 0.0004
    slippage_pct: float = 0.05
    initial_balance: float = 10000.0


@dataclass(frozen=True)
class TelegramAlertsConfig:
    enabled: bool = True
    include_chart: bool = False
    alert_on_pre_signal: bool = True
    alert_on_confirmed: bool = True
    alert_on_tp_hit: bool = True
    alert_on_sl_hit: bool = True
    alert_on_exit: bool = True
    alert_on_system: bool = True
    # Populated from .env
    bot_token: str = ""
    chat_id: str = ""


@dataclass(frozen=True)
class ConsoleAlertsConfig:
    enabled: bool = True
    colored: bool = True


@dataclass(frozen=True)
class AlertsConfig:
    telegram: TelegramAlertsConfig = field(default_factory=TelegramAlertsConfig)
    console: ConsoleAlertsConfig = field(default_factory=ConsoleAlertsConfig)


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    file_enabled: bool = True
    console_enabled: bool = True
    max_file_size_mb: int = 50
    backup_count: int = 10
    log_indicator_values: bool = False
    log_rejected_signals: bool = True
    log_dir: str = "logs"


@dataclass(frozen=True)
class JournalConfig:
    enabled: bool = True
    export_csv: bool = True
    export_json: bool = True


@dataclass(frozen=True)
class DashboardConfig:
    enabled: bool = True
    refresh_interval: int = 5
    max_alerts_display: int = 50
    host: str = "0.0.0.0"
    port: int = 8080
    secret_key: str = ""


@dataclass(frozen=True)
class IndianMarketConfig:
    """Indian Market Session Engine configuration.

    Controls NSE open/close awareness, F&O expiry detection,
    and regime overrides during Indian flow hours.
    """
    enabled: bool = False                  # Default OFF for backward compat
    fno_expiry_day: str = "thursday"
    fno_expiry_boost: int = 5              # +5 confidence on expiry flow hours
    holiday_file: str = ""                 # Path to NSE holiday JSON (optional)
    ranging_limited_scanners: List[str] = field(default_factory=lambda: [
        "liquidity_sweep", "vwap_mean_revert", "structure_bounce", "rsi_divergence",
    ])
    windows: List[Dict[str, Any]] = field(default_factory=list)    # Indian market windows
    sessions: List[Dict[str, Any]] = field(default_factory=list)   # Configurable legacy sessions


# ---------------------------------------------------------------------------
# Top-level Config
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Config:
    """Immutable, typed representation of the full bot configuration.

    Also supports dict-like access via ``.get()`` and ``[]`` so that
    modules written for a plain-dict config work transparently.
    """
    bot: BotConfig = field(default_factory=BotConfig)
    symbols: List[str] = field(default_factory=lambda: ["BTC/USDT"])
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    timeframes: TimeframesConfig = field(default_factory=TimeframesConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    paper_trading: PaperTradingConfig = field(default_factory=PaperTradingConfig)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    journal: JournalConfig = field(default_factory=JournalConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)
    indian_market: IndianMarketConfig = field(default_factory=IndianMarketConfig)
    database_url: str = "sqlite:///data/trading_bot.db"

    # --- dict-like API so modules using config.get("key") work ----------
    def _to_dict(self) -> Dict[str, Any]:
        import dataclasses as _dc
        def _convert(obj):
            if _dc.is_dataclass(obj) and not isinstance(obj, type):
                return {f.name: _convert(getattr(obj, f.name)) for f in _dc.fields(obj)}
            if isinstance(obj, list):
                return [_convert(i) for i in obj]
            return obj
        return _convert(self)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            val = getattr(self, key)
            import dataclasses as _dc
            if _dc.is_dataclass(val) and not isinstance(val, type):
                return val._to_dict() if hasattr(val, '_to_dict') else self._to_dict().get(key, default)
            return val
        except AttributeError:
            return default

    def __getitem__(self, key: str) -> Any:
        try:
            val = getattr(self, key)
            import dataclasses as _dc
            if _dc.is_dataclass(val) and not isinstance(val, type):
                return self._to_dict()[key]
            return val
        except AttributeError:
            raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict."""
    merged = dict(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build_dataclass(cls, data: Dict[str, Any]):
    """
    Recursively instantiate a dataclass from a nested dict, ignoring
    unknown keys so that YAML additions don't crash the loader.
    """
    import dataclasses as _dc

    if not _dc.is_dataclass(cls):
        return data

    kwargs: Dict[str, Any] = {}

    for f in _dc.fields(cls):
        if f.name not in data:
            continue
        raw = data[f.name]
        # Resolve the field's type for nested dataclasses
        ftype = _resolve_type(f.type)
        if _dc.is_dataclass(ftype) and isinstance(raw, dict):
            kwargs[f.name] = _build_dataclass(ftype, raw)
        else:
            kwargs[f.name] = raw

    return cls(**kwargs)


def _resolve_type(type_hint) -> type:
    """
    Best-effort resolution of a type hint string or generic alias to a
    concrete class.  Falls back to ``str`` if resolution fails.
    """
    if isinstance(type_hint, type):
        return type_hint
    # Handle string annotations (from __future__.annotations)
    if isinstance(type_hint, str):
        # Look up in this module's namespace
        return globals().get(type_hint, str)
    # typing generics (List[str], Optional[...], etc.) -- not dataclasses
    origin = getattr(type_hint, "__origin__", None)
    if origin is not None:
        return origin
    return str


def _env_str(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _inject_env(data: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay .env values onto the parsed YAML dict."""

    # Bot mode override
    env_mode = _env_str("BOT_MODE")
    if env_mode:
        data.setdefault("bot", {})["mode"] = env_mode

    # Exchange credentials
    active = _env_str("ACTIVE_EXCHANGE", data.get("exchange", {}).get("name", "delta"))
    prefix = active.upper()
    data.setdefault("exchange", {}).update({
        "name": active,
        "api_key": _env_str(f"{prefix}_API_KEY"),
        "api_secret": _env_str(f"{prefix}_API_SECRET"),
        "passphrase": _env_str(f"{prefix}_PASSPHRASE"),
    })

    # Telegram
    data.setdefault("alerts", {}).setdefault("telegram", {}).update({
        "bot_token": _env_str("TELEGRAM_BOT_TOKEN"),
        "chat_id": _env_str("TELEGRAM_CHAT_ID"),
        "enabled": _env_str("TELEGRAM_ENABLED", "true").lower() == "true",
    })

    # Dashboard
    data.setdefault("dashboard", {}).update({
        "host": _env_str("DASHBOARD_HOST", "0.0.0.0"),
        "port": int(_env_str("DASHBOARD_PORT", "8080")),
        "secret_key": _env_str("DASHBOARD_SECRET_KEY", ""),
        "enabled": _env_str("DASHBOARD_ENABLED", "true").lower() == "true",
    })

    # Logging
    env_level = _env_str("LOG_LEVEL")
    if env_level:
        data.setdefault("logging", {})["level"] = env_level
    env_log_dir = _env_str("LOG_DIR")
    if env_log_dir:
        data.setdefault("logging", {})["log_dir"] = env_log_dir

    # Database
    env_db = _env_str("DATABASE_URL")
    if env_db:
        data["database_url"] = env_db

    return data


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_instance: Optional[Config] = None


def load_config(
    settings_path: Optional[Path] = None,
    env_path: Optional[Path] = None,
    overrides: Optional[Dict[str, Any]] = None,
) -> Config:
    """
    Load configuration from YAML + .env, apply optional *overrides*, and
    return a :class:`Config` instance.

    This always creates a fresh instance (useful for tests).  For the
    process-wide singleton, use :func:`get_config`.
    """
    settings_path = Path(settings_path) if settings_path else DEFAULT_SETTINGS_PATH
    env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH

    # Load .env first so os.environ is populated for _inject_env
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)

    # Load YAML
    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as fh:
            data: Dict[str, Any] = yaml.safe_load(fh) or {}
    else:
        data = {}

    # Merge .env values
    data = _inject_env(data)

    # Apply caller-supplied overrides last
    if overrides:
        data = _deep_merge(data, overrides)

    return _build_dataclass(Config, data)


def get_config(
    settings_path: Optional[Path] = None,
    env_path: Optional[Path] = None,
    reload: bool = False,
) -> Config:
    """
    Return the process-wide :class:`Config` singleton.

    On the first call (or when *reload* is ``True``), the config is loaded
    from disk.  Subsequent calls return the cached instance.
    """
    global _instance
    if _instance is not None and not reload:
        return _instance

    with _lock:
        # Double-check after acquiring lock
        if _instance is not None and not reload:
            return _instance
        _instance = load_config(settings_path=settings_path, env_path=env_path)
        return _instance


def reset_config() -> None:
    """Clear the cached singleton (primarily for tests)."""
    global _instance
    with _lock:
        _instance = None
