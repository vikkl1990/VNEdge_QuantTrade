"""
Central Mode Manager — Single source of truth for operating mode.

Modes:
  paper_learning  — ALL signals fire, NO blocking, MAX data collection
  paper_enforced  — Filters active, paper trading with risk controls
  live            — Filters active, real money execution

Every module MUST check ModeManager before blocking any signal.
"""

from __future__ import annotations
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Singleton instance
_instance: Optional["ModeManager"] = None


class ModeManager:
    """Central mode controller — all modules reference this."""

    PAPER_LEARNING = "paper_learning"
    PAPER_ENFORCED = "paper_enforced"
    LIVE = "live"

    VALID_MODES = {PAPER_LEARNING, PAPER_ENFORCED, LIVE}

    def __init__(self, mode: str = PAPER_LEARNING):
        if mode not in self.VALID_MODES:
            logger.warning("Invalid mode '%s', defaulting to paper_learning", mode)
            mode = self.PAPER_LEARNING
        self._mode = mode
        logger.info("ModeManager initialized: %s", self._mode)

    @property
    def mode(self) -> str:
        return self._mode

    def is_learning(self) -> bool:
        """In learning mode: ALL signals fire, NO blocking."""
        return self._mode == self.PAPER_LEARNING

    def is_enforced(self) -> bool:
        """In enforced mode: filters active, paper trading."""
        return self._mode == self.PAPER_ENFORCED

    def is_live(self) -> bool:
        """In live mode: filters active, real execution."""
        return self._mode == self.LIVE

    def should_block(self) -> bool:
        """Should blocking logic be applied? Only in enforced/live."""
        return not self.is_learning()

    def should_enforce_risk(self) -> bool:
        """Should risk limits be enforced? Only in enforced/live."""
        return not self.is_learning()

    def set_mode(self, mode: str) -> None:
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}")
        old = self._mode
        self._mode = mode
        logger.info("Mode changed: %s → %s", old, self._mode)

    def __repr__(self) -> str:
        return f"ModeManager(mode={self._mode})"


def get_mode_manager(config=None) -> ModeManager:
    """Get or create the singleton ModeManager."""
    global _instance
    if _instance is None:
        # Default to paper_enforced (safe) — NOT paper_learning (everything bypassed)
        mode = "paper_enforced"
        if config:
            try:
                # Handle dict-style config
                if isinstance(config, dict):
                    mode = config.get("bot", {}).get("operating_mode", "paper_enforced")
                # Handle object-style config (dataclass, namespace, etc.)
                elif hasattr(config, "get"):
                    mode = config.get("bot", {}).get("operating_mode", "paper_enforced")
                elif hasattr(config, "bot"):
                    bot_cfg = getattr(config, "bot", {})
                    if isinstance(bot_cfg, dict):
                        mode = bot_cfg.get("operating_mode", "paper_enforced")
                    elif hasattr(bot_cfg, "get"):
                        mode = bot_cfg.get("operating_mode", "paper_enforced")
                    elif hasattr(bot_cfg, "operating_mode"):
                        mode = getattr(bot_cfg, "operating_mode", "paper_enforced")
            except Exception as e:
                logger.warning("Failed to read operating_mode from config (%s: %s), using paper_enforced", type(e).__name__, e)
                mode = "paper_enforced"
            logger.info("ModeManager: config_type=%s, resolved_mode=%s", type(config).__name__, mode)
        _instance = ModeManager(mode)
    return _instance


def reset_mode_manager() -> None:
    """Reset singleton (for testing)."""
    global _instance
    _instance = None
