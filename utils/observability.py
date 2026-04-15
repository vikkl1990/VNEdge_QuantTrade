"""Observability layer: structured logs, metrics, error tracking."""
import json
import logging
import os
import time
from typing import Any, Dict


# ─── Structured JSON logging ───
class JsonFormatter(logging.Formatter):
    """Output logs as JSON for ingestion by observability platforms."""
    def format(self, record):
        out = {
            "ts": int(time.time() * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }
        if record.exc_info:
            out["exc_type"] = record.exc_info[0].__name__ if record.exc_info[0] else None
            out["exc_msg"] = str(record.exc_info[1]) if record.exc_info[1] else None
        return json.dumps(out)


def setup_json_logging():
    """Switch root logger to JSON format if ENABLE_JSON_LOGS=1."""
    if os.getenv("ENABLE_JSON_LOGS", "0") != "1":
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]


# ─── Prometheus metrics export ───
_metrics = {
    "trades_total": 0,
    "trades_won": 0,
    "trades_lost": 0,
    "signals_total": 0,
    "api_requests_total": 0,
    "api_errors_total": 0,
    "ws_messages_total": 0,
    "brain_observations": 0,
}


def inc_metric(name: str, value: int = 1):
    _metrics[name] = _metrics.get(name, 0) + value


def get_metrics_text() -> str:
    """Prometheus text format export."""
    lines = ["# VN Edge metrics"]
    for k, v in sorted(_metrics.items()):
        lines.append(f"# TYPE vnedge_{k} counter")
        lines.append(f"vnedge_{k} {v}")
    return "\n".join(lines) + "\n"


# ─── Sentry-style error tracking ───
_sentry_dsn = os.getenv("SENTRY_DSN", "")


def capture_exception(exc: Exception, context: Dict[str, Any] = None):
    """Send exception to Sentry if configured, else log."""
    if _sentry_dsn:
        try:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
            return
        except ImportError:
            pass
    logging.getLogger("observability").error("EXCEPTION: %s | context=%s", exc, context or {})


def init_sentry():
    """Initialize Sentry if DSN configured."""
    if not _sentry_dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.aiohttp import AioHttpIntegration
        sentry_sdk.init(
            dsn=_sentry_dsn,
            integrations=[AioHttpIntegration()],
            traces_sample_rate=0.1,  # 10% APM sampling
            environment=os.getenv("ENV", "production"),
        )
        logging.getLogger("observability").info("Sentry initialized")
    except ImportError:
        logging.getLogger("observability").warning("Sentry DSN set but sentry-sdk not installed")
