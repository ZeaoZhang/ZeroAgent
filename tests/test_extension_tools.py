"""Tests for optional builtin extension tool adapters."""

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolRegistry


@pytest.mark.skip(
    reason="Extension tools (im/memory_plot/search) are intentionally not exposed as standalone builtin modules. "
    "Vision is a conditional configured builtin tool. IM send is handled by frontend Bot processes."
)
def test_optional_extension_handlers_return_data(monkeypatch) -> None:
    """可选扩展工具手动注册后应按 registry handler 协议返回数据."""
    from zero_agent.tools.builtin import im, memory_plot, search

    registry = ToolRegistry()
    search.register_search_tool(registry)
    memory_plot.register_memory_plot_tool(registry)
    im.register_im_tool(registry)

    monkeypatch.setattr(
        search,
        "search_web",
        lambda query, num_results=5, engine="auto": {
            "results": [{"title": query}],
            "engine": engine,
        },
    )
    monkeypatch.setattr(
        memory_plot,
        "memory_plot",
        lambda output_path=None, stats_path=None: {
            "path": output_path or "plot.png",
        },
    )
    monkeypatch.setattr(
        im,
        "send_im",
        lambda webhook_url, message, msg_type="text", title="": {
            "success": True,
            "response": message,
        },
    )

    assert _exhaust(handler.dispatch(
        "search_web", {"query": "zero"}, MockResponse(),
    )).data["results"][0]["title"] == "zero"
    assert _exhaust(handler.dispatch(
        "memory_plot", {"output_path": "out.png"}, MockResponse(),
    )).data["path"] == "out.png"
    assert _exhaust(handler.dispatch(
        "send_im", {"webhook_url": "https://example.com", "message": "hi"}, MockResponse(),
    )).data["success"] is True


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
