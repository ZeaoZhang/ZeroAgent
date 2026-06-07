"""
ZeroAgent Stock Analysis Skill — Data Provider
================================================
Thin wrapper around external stock data APIs (AkShare, etc.).
This is the "调用" (call) layer — we don't rewrite data sources,
we provide a clean unified interface.

Design principles:
- Lazy imports: only import what's needed, fail gracefully if not installed
- Unified return format: always returns pandas DataFrame with standard columns
- Caching: avoid redundant fetches within same session
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# Data source registry
# ──────────────────────────────────────────────

_DATA_SOURCES = {}


def register_source(name: str):
    """Decorator to register a data source implementation."""
    def wrapper(cls):
        _DATA_SOURCES[name] = cls
        return cls
    return wrapper


def get_available_sources() -> list[str]:
    """Return list of available (installed) data sources."""
    return list(_DATA_SOURCES.keys())


# ──────────────────────────────────────────────
# AkShare data source
# ──────────────────────────────────────────────

@register_source("akshare")
class AkShareFetcher:
    """Fetch stock data via AkShare (free, Chinese markets focused)."""

    @staticmethod
    def available() -> bool:
        try:
            import akshare  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def get_daily_history(
        code: str,
        days: int = 60,
        adjust: str = "qfq",
    ) -> Optional[pd.DataFrame]:
        """Fetch daily OHLCV history.

        Args:
            code: Stock code (e.g. "600519" for A-share, "AAPL" for US).
            days: Number of trading days to fetch.
            adjust:复权类型 ("qfq"=前复权, "hfq"=后复权, ""=不复权).

        Returns:
            DataFrame with columns: date, open, high, low, close, volume, amount
            or None if fetch fails.
        """
        try:
            import akshare as ak
        except ImportError:
            logger.warning("akshare not installed, cannot fetch data")
            return None

        try:
            # Determine market prefix
            if code.startswith(("6", "9")):
                symbol = f"sh{code}"
            elif code.startswith(("0", "3")):
                symbol = f"sz{code}"
            elif code.startswith(("4", "8")):
                symbol = f"bj{code}"
            else:
                # Try with "sh" prefix as default
                symbol = code

            df = ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=(datetime.now() - timedelta(days=days)).strftime("%Y%m%d"),
                adjust=adjust,
            )

            if df is None or df.empty:
                return None

            # Standardize columns
            df = df.rename(columns={
                "日期": "date",
                "开盘": "open",
                "最高": "high",
                "最低": "low",
                "收盘": "close",
                "成交量": "volume",
                "成交额": "amount",
                "振幅": "amplitude",
                "涨跌幅": "change_pct",
                "涨跌额": "change",
                "换手率": "turnover",
            })
            df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)

            # Ensure numeric types
            for col in ["open", "high", "low", "close", "volume", "amount"]:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")

            return df

        except Exception as e:
            logger.warning(f"AkShare fetch failed for {code}: {e}")
            return None


# ──────────────────────────────────────────────
# YFinance data source (US/HK stocks)
# ──────────────────────────────────────────────

@register_source("yfinance")
class YFinanceFetcher:
    """Fetch stock data via yfinance (free, US/HK markets focused)."""

    @staticmethod
    def available() -> bool:
        try:
            import yfinance  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def get_daily_history(
        code: str,
        days: int = 60,
    ) -> Optional[pd.DataFrame]:
        try:
            import yfinance as yf
        except ImportError:
            logger.warning("yfinance not installed")
            return None

        try:
            ticker = yf.Ticker(code)
            df = ticker.history(period=f"{days}d")

            if df is None or df.empty:
                return None

            df = df.reset_index()
            df = df.rename(columns={
                "Date": "date",
                "Open": "open",
                "High": "high",
                "Low": "low",
                "Close": "close",
                "Volume": "volume",
            })
            df["date"] = pd.to_datetime(df["date"])
            return df

        except Exception as e:
            logger.warning(f"YFinance fetch failed for {code}: {e}")
            return None


# ──────────────────────────────────────────────
# Unified fetcher
# ──────────────────────────────────────────────


class StockDataFetcher:
    """Unified stock data fetcher with auto-fallback."""

    def __init__(self, preferred_source: str = "akshare"):
        self.preferred_source = preferred_source

    def get_daily_history(
        self,
        code: str,
        days: int = 60,
    ) -> Optional[pd.DataFrame]:
        """Fetch daily history with automatic fallback.

        Tries preferred source first, then falls back to alternatives.
        """
        # Determine best source by stock code pattern
        if code.isdigit():
            # Chinese market code (digits)
            sources = ["akshare", "yfinance"]
        else:
            # US/HK stock (letters)
            sources = ["yfinance", "akshare"]

        # Move preferred to front
        if self.preferred_source in sources:
            sources.remove(self.preferred_source)
            sources.insert(0, self.preferred_source)

        for source_name in sources:
            fetcher_cls = _DATA_SOURCES.get(source_name)
            if fetcher_cls is None:
                continue
            if not fetcher_cls.available():
                continue

            df = fetcher_cls.get_daily_history(code, days)
            if df is not None and not df.empty:
                logger.info(f"Fetched {code} from {source_name} ({len(df)} rows)")
                return df

        logger.warning(f"All data sources failed for {code}")
        return None

    def get_realtime_quote(self, code: str) -> Optional[dict]:
        """Fetch real-time quote for a stock."""
        # For now, use the last row of daily history as approximate quote
        df = self.get_daily_history(code, days=5)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) > 1 else latest
            change = latest["close"] - prev["close"]
            change_pct = (change / prev["close"]) * 100 if prev["close"] > 0 else 0
            return {
                "code": code,
                "price": float(latest["close"]),
                "change": float(change),
                "change_pct": float(change_pct),
                "high": float(latest["high"]),
                "low": float(latest["low"]),
                "volume": float(latest["volume"]),
                "date": str(latest["date"]),
            }
        return None


# Singleton
_fetcher_instance: Optional[StockDataFetcher] = None


def get_fetcher(preferred_source: str = "akshare") -> StockDataFetcher:
    """Get or create the global fetcher singleton."""
    global _fetcher_instance
    if _fetcher_instance is None:
        _fetcher_instance = StockDataFetcher(preferred_source=preferred_source)
    return _fetcher_instance
