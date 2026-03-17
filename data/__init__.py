"""
Data ingestion layer for the crypto trading bot.

Provides market data management, live/historical feeds, and technical indicators.
"""

from data.manager import DataManager
from data.feed import DataFeed
from data.indicators import calc_all_indicators

__all__ = ["DataManager", "DataFeed", "calc_all_indicators"]
