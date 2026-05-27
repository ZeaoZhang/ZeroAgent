"""search_web — 网页搜索工具.

支持 Exa MCP 工具和 DuckDuckGo 回退.
"""

from __future__ import annotations

from typing import Any, Dict, List

from zero_agent.tools.registry import ToolDefinition


def search_web(
    query: str,
    num_results: int = 5,
    engine: str = "auto",
) -> Dict[str, Any]:
    """执行网页搜索.

    Args:
        query: 搜索查询字符串.
        num_results: 返回结果数量.
        engine: 搜索引擎 "auto"|"duckduckgo"|"exa".

    Returns:
        {"results": [{"title": ..., "url": ..., "snippet": ...}], "engine": str}
    """
    if engine == "auto":
        if _try_exa():
            engine = "exa"
        else:
            engine = "duckduckgo"

    if engine == "exa":
        return _search_exa(query, num_results)
    return _search_duckduckgo(query, num_results)


def _try_exa() -> bool:
    """检测 Exa MCP 是否可用."""
    import os
    return bool(os.environ.get("EXA_API_KEY"))


def _search_exa(query: str, num_results: int) -> Dict[str, Any]:
    """通过 Exa API 搜索.

    Args:
        query: 搜索查询.
        num_results: 结果数量.

    Returns:
        搜索结果.
    """
    import os
    import requests as _requests

    api_key = os.environ.get("EXA_API_KEY", "")
    resp = _requests.post(
        "https://api.exa.ai/search",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "numResults": num_results,
            "contents": {"text": True, "highlights": True},
        },
        timeout=30,
    )

    if resp.status_code == 200:
        data = resp.json()
        results: List[Dict[str, Any]] = []
        for item in data.get("results", []):
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": (item.get("highlights") or [""])[0][:300],
            })
        return {"results": results, "engine": "exa"}
    return {"results": [], "engine": "exa", "error": f"HTTP {resp.status_code}"}


def _search_duckduckgo(query: str, num_results: int) -> Dict[str, Any]:
    """通过 DuckDuckGo HTML 搜索回退.

    Args:
        query: 搜索查询.
        num_results: 结果数量.

    Returns:
        搜索结果.
    """
    import re
    import requests as _requests

    try:
        resp = _requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={"User-Agent": "ZeroAgent/1.0"},
            timeout=15,
        )
        if resp.status_code != 200:
            return {"results": [], "engine": "duckduckgo",
                    "error": f"HTTP {resp.status_code}"}

        results: List[Dict[str, Any]] = []
        # 简单 HTML 解析提取链接
        link_pattern = re.compile(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>'
            r'(.*?)</a>',
            re.DOTALL,
        )
        snippet_pattern = re.compile(
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            re.DOTALL,
        )

        links = link_pattern.findall(resp.text)
        snippets = snippet_pattern.findall(resp.text)

        for i, (url, title) in enumerate(links[:num_results]):
            snippet = snippets[i] if i < len(snippets) else ""
            snippet = re.sub(r"<[^>]+>", "", snippet).strip()
            results.append({
                "title": re.sub(r"<[^>]+>", "", title).strip(),
                "url": url,
                "snippet": snippet[:300],
            })

        return {"results": results, "engine": "duckduckgo"}
    except Exception as e:
        return {"results": [], "engine": "duckduckgo", "error": str(e)}


def register_search_tool(registry: Any) -> None:
    """向 ToolRegistry 注册 search_web 工具.

    Args:
        registry: ToolRegistry 实例.
    """
    def _handler(args: Dict[str, Any], _response: Any, _handler_self: Any):
        yield "Searching web ...\n"
        return search_web(
            query=args.get("query", ""),
            num_results=args.get("num_results", 5),
        )

    tool_def = ToolDefinition(
        name="search_web",
        description="在互联网上搜索信息。/ Search the web for information.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索查询字符串 / Search query",
                },
                "num_results": {
                    "type": "integer",
                    "description": "返回结果数量（默认 5）/ Number of results (default 5)",
                },
            },
            "required": ["query"],
        },
        handler=_handler,
        category="web",
    )
    registry.register(tool_def)
