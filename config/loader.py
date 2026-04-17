"""Configuration loader - reads settings.yaml and .env into a unified config dict."""

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

_config: Optional[Dict[str, Any]] = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Load configuration from YAML file and environment variables.

    Environment variables override YAML settings where applicable.
    """
    global _config

    # Load .env file
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    # Load YAML config
    if config_path is None:
        config_path = str(PROJECT_ROOT / "config" / "settings.yaml")

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    # Apply environment variable overrides
    config.setdefault("telegram", {})
    config["telegram"]["bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    config["telegram"]["chat_id"] = os.getenv("TELEGRAM_CHAT_ID", "")
    config["telegram"]["enabled"] = os.getenv(
        "TELEGRAM_ENABLED", str(config.get("alerts", {}).get("telegram", {}).get("enabled", False))
    ).lower() == "true"

    # Exchange override from env
    if os.getenv("ACTIVE_EXCHANGE"):
        config.setdefault("exchange", {})
        config["exchange"]["name"] = os.getenv("ACTIVE_EXCHANGE")

    # Inject exchange API keys from env
    config.setdefault("exchange", {})
    exchange_name = config["exchange"].get("name", "").lower()
    prefix = exchange_name.upper()
    config["exchange"]["api_key"] = os.getenv(f"{prefix}_API_KEY", "")
    config["exchange"]["api_secret"] = os.getenv(f"{prefix}_API_SECRET", "")
    if exchange_name == "okx":
        config["exchange"]["passphrase"] = os.getenv("OKX_PASSPHRASE", "")

    if os.getenv("BOT_MODE"):
        config["bot"]["mode"] = os.getenv("BOT_MODE")
    if os.getenv("LOG_LEVEL"):
        config["logging"]["level"] = os.getenv("LOG_LEVEL")
    if os.getenv("LOG_DIR"):
        config["logging"]["log_dir"] = os.getenv("LOG_DIR")
    else:
        config["logging"].setdefault("log_dir", str(PROJECT_ROOT / "logs"))

    config["database"] = {
        "url": os.getenv("DATABASE_URL", f"sqlite:///{PROJECT_ROOT / 'data' / 'trading_bot.db'}"),
    }

    config["_project_root"] = str(PROJECT_ROOT)

    # Validate critical config on startup
    mode = config.get("bot", {}).get("mode", "paper")
    api_key = config["exchange"].get("api_key", "")
    api_secret = config["exchange"].get("api_secret", "")
    if mode in ("live",) and (not api_key or not api_secret):
        raise ValueError(f"FATAL: {exchange_name} API key/secret required for live mode")
    if not config.get("symbols"):
        raise ValueError("FATAL: No symbols configured")
    dash_pw = os.getenv("DASHBOARD_PASSWORD", "")
    if not dash_pw:
        logger.warning("SECURITY: DASHBOARD_PASSWORD not set — random password will be generated")

    _config = config
    return config


def get_config() -> Dict[str, Any]:
    """Return the loaded config, loading it first if necessary."""
    global _config
    if _config is None:
        _config = load_config()
    return _config
