
"""
ZeroAgent Stock Analysis Skill — Plugin Entry Point
====================================================
Auto-registers stock analysis tools into ZeroAgent's ToolRegistry.

Usage:
    # In config.yaml or environment, set:
    # ZA_ENABLED_SKILLS: stock_analysis
    #
    # Or import directly:
    # from zero_agent.skills.stock_analysis import register_stock_tools
    # registry = ToolRegistry()
    # register_stock_tools(registry)

Tools provided:
    - analyze_stock: Full technical analysis of a single stock
    - get_stock_quote: Real-time stock quote
    - get_market_overview: Brief market overview
    - search_stock_news: Search for stock-related news (requires SerpAPI/Tavily)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Generator, Optional

from zero_agent.tools.registry import ToolDefinition, ToolRegistry

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
# Tool Handlers
# ──────────────────────────────────────────────


def _handler_analyze_stock(
    args: Dict[str, Any],
    response: Any,
    handler: Any,
) -> Generator[str, None, Optional[dict]]:
    """Analyze a single stock with technical indicators.

    Args:
        code: Stock code (e.g. "600519" for A-share, "AAPL" for US).
        days: Number of trading days to analyze (default: 60).
        name: Optional stock name for display.

    Returns:
        Analysis result dict with trend, volume, signal, and summary.
    """
    code = args.get("code", "")
    days = int(args.get("days", 60))
    name = args.get("name", "")

    if not code:
        yield "错误: 请提供股票代码"
        return {"error": "股票代码不能为空"}

    yield f"正在分析股票 {code}..."

    # Lazy import to keep startup fast
    from zero_agent.skills.stock_analysis.data_provider import get_fetcher
    from zero_agent.skills.stock_analysis.indicators import analyze_stock_data, format_analysis_for_llm

    fetcher = get_fetcher()
    df = fetcher.get_daily_history(code, days=days)

    if df is None or df.empty:
        yield f"无法获取 {code} 的数据，请检查股票代码是否正确"
        return {"error": f"无法获取 {code} 的数据"}

    result = analyze_stock_data(code, df, name=name)
    formatted = format_analysis_for_llm(result)

    yield formatted

    return {
        "code": result.code,
        "name": result.name,
        "price": result.price,
        "change_pct": result.change_pct,
        "signal": result.signal.signal if result.signal else "unknown",
        "confidence": result.signal.confidence if result.signal else 0.0,
        "sentiment_score": result.sentiment_score,
        "operation_advice": result.operation_advice,
        "summary": result.summary,
        "trend_status": result.trend.status if result.trend else "",
        "volume_status": result.volume.status if result.volume else "",
    }


def _handler_get_stock_quote(
    args: Dict[str, Any],
    response: Any,
    handler: Any,
) -> Generator[str, None, Optional[dict]]:
    """Get real-time stock quote.

    Args:
        code: Stock code.

    Returns:
        Quote dict with price, change, volume.
    """
    code = args.get("code", "")
    if not code:
        yield "错误: 请提供股票代码"
        return {"error": "股票代码不能为空"}

    yield f"正在获取 {code} 实时行情..."

    from zero_agent.skills.stock_analysis.data_provider import get_fetcher
    fetcher = get_fetcher()
    quote = fetcher.get_realtime_quote(code)

    if quote is None:
        yield f"无法获取 {code} 的实时行情"
        return {"error": f"无法获取 {code} 的实时行情"}

    result = (
        f"## {code} 实时行情\n"
        f"价格: {quote['price']:.2f}\n"
        f"涨跌: {quote['change']:+.2f} ({quote['change_pct']:+.2f}%)\n"
        f"最高: {quote['high']:.2f} | 最低: {quote['low']:.2f}\n"
        f"成交量: {quote['volume']:.0f}"
    )
    yield result
    return quote


def _handler_analyze_trend(
    args: Dict[str, Any],
    response: Any,
    handler: Any,
) -> Generator[str, None, Optional[dict]]:
    """Simple trend check: MA alignment, MACD, RSI.

    Args:
        code: Stock code.
        days: History window (default: 60).

    Returns:
        Trend analysis text.
    """
    code = args.get("code", "")
    days = int(args.get("days", 60))

    from zero_agent.skills.stock_analysis.data_provider import get_fetcher
    from zero_agent.skills.stock_analysis.indicators import analyze_stock_data

    fetcher = get_fetcher()
    df = fetcher.get_daily_history(code, days=days)
    if df is None:
        yield f"无法获取 {code} 数据"
        return {"error": "数据获取失败"}

    result = analyze_stock_data(code, df)
    yield (
        f"## {code} 趋势分析\n"
        f"趋势: {result.trend.description if result.trend else 'N/A'}\n"
        f"MA5={result.trend.ma5:.2f} MA10={result.trend.ma10:.2f} MA20={result.trend.ma20:.2f}\n"
        f"RSI(14)={result.trend.rsi14:.1f}\n"
        f"MACD柱={result.trend.macd_histogram:.3f}"
    )
    return {
        "trend_status": result.trend.status if result.trend else "",
        "ma5": result.trend.ma5 if result.trend else 0,
        "rsi14": result.trend.rsi14 if result.trend else 0,
    }


# ──────────────────────────────────────────────
# Tool Definitions
# ──────────────────────────────────────────────

STOCK_TOOLS = [
    ToolDefinition(
        name="analyze_stock",
        description="对单只股票进行全面技术分析，包括趋势判断、量能分析、买卖信号评估。"
                    "适用于A股、港股、美股。返回分析摘要和操作建议。",
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "股票代码，如 A股'600519'、港股'00700'、美股'AAPL'",
                },
                "days": {
                    "type": "integer",
                    "description": "分析的历史天数，默认60天",
                    "default": 60,
                },
                "name": {
                    "type": "string",
                    "description": "股票名称（可选，用于显示）",
                    "default": "",
                },
            },
            "required": ["code"],
        },
        handler=_handler_analyze_stock,
        category="stock_analysis",
    ),
    ToolDefinition(
        name="get_stock_quote",
        description="获取股票的实时行情数据，包括当前价格、涨跌幅、最高最低价、成交量。",
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "股票代码",
                },
            },
            "required": ["code"],
        },
        handler=_handler_get_stock_quote,
        category="stock_analysis",
    ),
    ToolDefinition(
        name="analyze_trend",
        description="快速检查股票的趋势状态：均线排列、MACD、RSI。比analyze_stock更轻量。",
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "股票代码",
                },
                "days": {
                    "type": "integer",
                    "description": "分析天数，默认60",
                    "default": 60,
                },
            },
            "required": ["code"],
        },
        handler=_handler_analyze_trend,
        category="stock_analysis",
    ),
]


def register_stock_tools(registry: ToolRegistry) -> None:
    """Register all stock analysis tools into a ToolRegistry.

    Call this during ZeroAgent initialization to add stock analysis capabilities.

    Args:
        registry: ToolRegistry instance (e.g. from ZeroAgent or standalone).
    """
    for tool in STOCK_TOOLS:
        registry.register(tool)
    logger.info(f"Registered {len(STOCK_TOOLS)} stock analysis tools")


def auto_register() -> None:
    """Auto-register hook: called at import time when skill is enabled.

    This function is discovered by ZeroAgent's skill auto-loader.
    """
    # This would be called by the skill loader system
    pass


# Make handlers discoverable
__all__ = [
    "STOCK_TOOLS",
    "register_stock_tools",
    "auto_register",
]
