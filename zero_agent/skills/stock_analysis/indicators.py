"""
ZeroAgent Stock Analysis Skill — Technical Indicators
=====================================================
Pure NumPy/Pandas technical indicators, no external data dependencies.
Rewritten from daily_stock_analysis/src/stock_analyzer.py concepts,
with cleaner API, fewer edge cases, and comprehensive docstrings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


# ──────────────────────────────────────────────
# Data structures
# ──────────────────────────────────────────────


@dataclass
class TrendAnalysis:
    """Trend analysis result."""
    status: str                     # e.g. "strong_bull", "bull", "bear", "consolidation"
    ma5: float
    ma10: float
    ma20: float
    ma60: float
    macd: float
    macd_signal: float
    macd_histogram: float
    rsi14: float
    description: str = ""


@dataclass
class VolumeAnalysis:
    """Volume analysis result."""
    status: str                     # e.g. "heavy_up", "heavy_down", "shrink", "normal"
    avg_volume_5: float
    avg_volume_10: float
    volume_ratio: float             # current_vol / avg_vol_5
    description: str = ""


@dataclass
class ChipAnalysis:
    """Chip/distribution analysis result."""
    concentration: str              # "concentrated", "分散", "normal"
    avg_cost: float
    profit_ratio: float             # % of holders in profit
    description: str = ""


@dataclass
class BuySignal:
    """Buy/sell signal assessment."""
    signal: str                     # "strong_buy", "buy", "hold", "wait", "sell", "strong_sell"
    confidence: float               # 0.0 ~ 1.0
    reasons: List[str] = field(default_factory=list)


@dataclass
class StockAnalysisResult:
    """Complete stock analysis result."""
    code: str
    name: str = ""
    price: float = 0.0
    change_pct: float = 0.0
    trend: Optional[TrendAnalysis] = None
    volume: Optional[VolumeAnalysis] = None
    chip: Optional[ChipAnalysis] = None
    signal: Optional[BuySignal] = None
    sentiment_score: int = 0        # -100 ~ 100
    operation_advice: str = "观望"
    summary: str = ""


# ──────────────────────────────────────────────
# Indicator computation (pure functions)
# ──────────────────────────────────────────────


def compute_moving_average(series: pd.Series, window: int) -> pd.Series:
    """Compute simple moving average."""
    return series.rolling(window=window).mean()


def compute_macd(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Compute MACD line, signal line, and histogram."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def compute_rsi(series: pd.Series, window: int = 14) -> pd.Series:
    """Compute Relative Strength Index."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.rolling(window=window, min_periods=1).mean()
    avg_loss = loss.rolling(window=window, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def compute_bollinger_bands(
    close: pd.Series,
    window: int = 20,
    num_std: float = 2.0,
) -> Tuple[pd.Series, pd.Series, pd.Series]:
    """Compute Bollinger Bands (middle, upper, lower)."""
    ma = compute_moving_average(close, window)
    std = close.rolling(window=window).std()
    upper = ma + num_std * std
    lower = ma - num_std * std
    return ma, upper, lower


def compute_volume_ratio(volume: pd.Series, window: int = 5) -> pd.Series:
    """Compute volume ratio relative to moving average."""
    avg_vol = volume.rolling(window=window).mean()
    return volume / avg_vol.replace(0, np.nan)


def determine_trend_status(
    ma5: float,
    ma10: float,
    ma20: float,
    ma60: float,
    rsi: float,
) -> Tuple[str, str]:
    """Determine trend status and description.

    Returns:
        (status_key, description)
    """
    if pd.isna(ma5) or pd.isna(ma10):
        return "insufficient_data", "数据不足"

    if ma5 > ma10 > ma20:
        if ma20 > ma60 and (ma5 - ma20) / ma20 > 0.05:
            return "strong_bull", "强势多头排列，均线发散向上"
        return "bull", "多头排列，趋势向上"
    elif ma5 < ma10 < ma20:
        if ma20 < ma60 and (ma20 - ma5) / ma5 > 0.05:
            return "strong_bear", "强势空头排列，均线发散向下"
        return "bear", "空头排列，趋势向下"
    elif ma5 > ma10 and ma10 < ma20:
        return "weak_bull", "弱势多头，短期向上但中期承压"
    elif ma5 < ma10 and ma10 > ma20:
        return "weak_bear", "弱势空头，短期回调但中期尚有支撑"
    else:
        return "consolidation", "盘整格局，均线缠绕"


def determine_volume_status(
    vol_ratio: float,
    change_pct: float,
) -> Tuple[str, str]:
    """Determine volume status from ratio and price change."""
    if pd.isna(vol_ratio):
        return "normal", "量能正常"

    if vol_ratio > 1.5:
        if change_pct > 2:
            return "heavy_up", "放量上涨，资金积极介入"
        elif change_pct < -2:
            return "heavy_down", "放量下跌，资金出逃明显"
        return "heavy_neutral", "放量但价格平稳，多空分歧"
    elif vol_ratio < 0.6:
        if change_pct > 0:
            return "shrink_up", "缩量上涨，上涨动能不足"
        elif change_pct < 0:
            return "shrink_down", "缩量回调，抛压减轻"
        return "shrink", "缩量整理，观望情绪浓厚"
    else:
        return "normal", "量能正常"


def assess_buy_signal(
    trend_status: str,
    volume_status: str,
    rsi: float,
    macd_histogram: float,
    ma5: float,
    ma10: float,
    price: float,
) -> BuySignal:
    """Assess buy/sell signal from combined indicators."""
    score = 0.0
    reasons = []

    # Trend scoring
    if trend_status == "strong_bull":
        score += 30
        reasons.append("强势多头排列")
    elif trend_status == "bull":
        score += 20
        reasons.append("多头排列")
    elif trend_status == "weak_bull":
        score += 10
        reasons.append("弱势多头")
    elif trend_status == "consolidation":
        score += 0
    elif trend_status == "weak_bear":
        score -= 10
        reasons.append("弱势空头")
    elif trend_status == "bear":
        score -= 20
        reasons.append("空头排列")
    elif trend_status == "strong_bear":
        score -= 30
        reasons.append("强势空头排列")

    # Volume scoring
    if volume_status == "heavy_up":
        score += 15
        reasons.append("放量上涨")
    elif volume_status == "shrink_down":
        score += 10
        reasons.append("缩量回调，抛压减轻")
    elif volume_status == "heavy_down":
        score -= 20
        reasons.append("放量下跌")

    # RSI scoring
    if not pd.isna(rsi):
        if rsi < 30:
            score += 15
            reasons.append("RSI超卖区")
        elif rsi > 70:
            score -= 15
            reasons.append("RSI超买区")

    # MACD scoring
    if not pd.isna(macd_histogram):
        if macd_histogram > 0:
            score += 10
            if macd_histogram > 0 and macd_histogram > 0:  # increasing
                score += 5
                reasons.append("MACD红柱扩大")
        else:
            score -= 10
            if macd_histogram < -0.5:
                score -= 5
                reasons.append("MACD绿柱扩大")

    # Price position (deviation from MA5)
    if not pd.isna(ma5) and price > 0:
        deviation = (price - ma5) / ma5 * 100
        if -2 < deviation < 5:
            score += 10
            reasons.append("价格在MA5附近，乖离率适中")
        elif deviation > 10:
            score -= 10
            reasons.append(f"乖离率 {deviation:.1f}%，偏离过大")

    # Determine signal
    if score >= 40:
        signal = "strong_buy"
        confidence = min(1.0, score / 60)
    elif score >= 15:
        signal = "buy"
        confidence = min(0.7, score / 40)
    elif score >= -10:
        signal = "hold" if score >= 0 else "wait"
        confidence = 0.5
    elif score >= -30:
        signal = "sell"
        confidence = min(0.7, abs(score) / 40)
    else:
        signal = "strong_sell"
        confidence = min(1.0, abs(score) / 60)

    return BuySignal(signal=signal, confidence=confidence, reasons=reasons)


# ──────────────────────────────────────────────
# High-level analysis
# ──────────────────────────────────────────────


def analyze_stock_data(
    code: str,
    df: pd.DataFrame,
    name: str = "",
) -> StockAnalysisResult:
    """Perform full technical analysis on a stock DataFrame.

    Args:
        code: Stock code (e.g. "600519").
        df: DataFrame with columns: ['close', 'volume', 'high', 'low', 'open'].
             Must have at least 60 rows for reliable indicators.
        name: Optional stock name.

    Returns:
        StockAnalysisResult with trend, volume, chip, and signal analysis.
    """
    result = StockAnalysisResult(code=code, name=name)

    if df.empty or len(df) < 5:
        result.summary = "数据不足，无法分析"
        return result

    close = df["close"]
    volume = df["volume"]
    result.price = float(close.iloc[-1])

    # Price change
    if len(close) >= 2:
        prev_close = close.iloc[-2]
        result.change_pct = ((result.price - prev_close) / prev_close) * 100

    # ── Trend Analysis ──
    ma5 = compute_moving_average(close, 5)
    ma10 = compute_moving_average(close, 10)
    ma20 = compute_moving_average(close, 20)
    ma60 = compute_moving_average(close, 60)
    macd_line, macd_signal, macd_hist = compute_macd(close)
    rsi = compute_rsi(close)

    ma5_v = float(ma5.iloc[-1]) if not pd.isna(ma5.iloc[-1]) else 0.0
    ma10_v = float(ma10.iloc[-1]) if not pd.isna(ma10.iloc[-1]) else 0.0
    ma20_v = float(ma20.iloc[-1]) if not pd.isna(ma20.iloc[-1]) else 0.0
    ma60_v = float(ma60.iloc[-1]) if not pd.isna(ma60.iloc[-1]) else 0.0
    rsi_v = float(rsi.iloc[-1]) if not pd.isna(rsi.iloc[-1]) else 50.0

    trend_status, trend_desc = determine_trend_status(ma5_v, ma10_v, ma20_v, ma60_v, rsi_v)

    result.trend = TrendAnalysis(
        status=trend_status,
        ma5=ma5_v,
        ma10=ma10_v,
        ma20=ma20_v,
        ma60=ma60_v,
        macd=float(macd_line.iloc[-1]) if not pd.isna(macd_line.iloc[-1]) else 0.0,
        macd_signal=float(macd_signal.iloc[-1]) if not pd.isna(macd_signal.iloc[-1]) else 0.0,
        macd_histogram=float(macd_hist.iloc[-1]) if not pd.isna(macd_hist.iloc[-1]) else 0.0,
        rsi14=rsi_v,
        description=trend_desc,
    )

    # ── Volume Analysis ──
    vol_ratio_s = compute_volume_ratio(volume, 5)
    vol_ratio_v = float(vol_ratio_s.iloc[-1]) if not pd.isna(vol_ratio_s.iloc[-1]) else 1.0
    avg_vol_5 = float(volume.tail(5).mean())
    avg_vol_10 = float(volume.tail(10).mean())

    vol_status, vol_desc = determine_volume_status(vol_ratio_v, result.change_pct)
    result.volume = VolumeAnalysis(
        status=vol_status,
        avg_volume_5=avg_vol_5,
        avg_volume_10=avg_vol_10,
        volume_ratio=vol_ratio_v,
        description=vol_desc,
    )

    # ── Buy/Sell Signal ──
    macd_hist_v = float(macd_hist.iloc[-1]) if not pd.isna(macd_hist.iloc[-1]) else 0.0
    result.signal = assess_buy_signal(
        trend_status, vol_status, rsi_v, macd_hist_v, ma5_v, ma10_v, result.price,
    )

    # ── Sentiment Score & Advice ──
    signal_map = {
        "strong_buy": 80,
        "buy": 40,
        "hold": 10,
        "wait": -10,
        "sell": -40,
        "strong_sell": -80,
    }
    result.sentiment_score = signal_map.get(result.signal.signal, 0)

    advice_map = {
        "strong_buy": "强烈建议买入",
        "buy": "建议买入",
        "hold": "建议持有",
        "wait": "建议观望",
        "sell": "建议卖出",
        "strong_sell": "强烈建议卖出",
    }
    result.operation_advice = advice_map.get(result.signal.signal, "观望")

    # ── Summary ──
    result.summary = (
        f"{name or code} 当前价 {result.price:.2f}，"
        f"{trend_desc}。"
        f"{vol_desc}。"
        f"信号: {result.operation_advice}（置信度 {result.signal.confidence:.0%}）。"
    )

    return result


def format_analysis_for_llm(result: StockAnalysisResult) -> str:
    """Format analysis result as a concise text for LLM context."""
    lines = [
        f"## 股票分析: {result.name or result.code} ({result.code})",
        f"价格: {result.price:.2f} | 涨跌幅: {result.change_pct:+.2f}%",
        f"信号: {result.operation_advice} (得分: {result.sentiment_score})",
        f"置信度: {result.signal.confidence:.0%}",
    ]
    if result.signal and result.signal.reasons:
        lines.append("理由: " + ", ".join(result.signal.reasons))
    if result.trend:
        lines.append(f"趋势: {result.trend.description}")
        lines.append(f"MA5={result.trend.ma5:.2f} MA10={result.trend.ma10:.2f} MA20={result.trend.ma20:.2f}")
        lines.append(f"RSI={result.trend.rsi14:.1f} MACD={result.trend.macd_histogram:.3f}")
    if result.volume:
        lines.append(f"量能: {result.volume.description} (量比={result.volume.volume_ratio:.2f})")
    lines.append(f"总结: {result.summary}")
    return "\n".join(lines)
