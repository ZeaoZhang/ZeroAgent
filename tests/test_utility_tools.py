"""Tests for find/read_doc/fetch/web_search global tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from zero_agent.core.config import AgentConfig
from zero_agent.tools.builtin import utility


def _config(tmp_path: Path) -> AgentConfig:
    return AgentConfig(
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
    )


def test_find_globs_and_searches_content(tmp_path: Path) -> None:
    config = _config(tmp_path)
    workspace = Path(config.workspace_dir)
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_text("first\nneedle here\n", encoding="utf-8")
    (workspace / "src" / "b.txt").write_text("needle too\n", encoding="utf-8")

    globbed = utility._find({"path": ".", "pattern": "**/*.py"}, config)
    assert globbed["status"] == "success"
    assert [item["relative"] for item in globbed["matches"]] == ["src/a.py"]

    searched = utility._find(
        {"path": ".", "pattern": "*.py", "content": "needle"}, config,
    )
    assert searched["count"] == 1
    assert searched["matches"][0]["line"] == 2
    assert "needle" in searched["matches"][0]["text"]


def test_read_doc_text_csv_and_html(tmp_path: Path) -> None:
    config = _config(tmp_path)
    workspace = Path(config.workspace_dir)
    workspace.mkdir(parents=True)
    (workspace / "a.txt").write_text("hello", encoding="utf-8")
    (workspace / "a.csv").write_text("name,age\nAlice,30\n", encoding="utf-8")
    (workspace / "a.html").write_text(
        "<h1>Title</h1><script>ignore()</script><p>Body</p>", encoding="utf-8",
    )

    text = utility._read_doc({"path": "a.txt"}, config)
    assert text["content"] == "hello"

    csv_result = utility._read_doc({"path": "a.csv"}, config)
    assert "name\tage" in csv_result["content"]
    assert "Alice\t30" in csv_result["content"]

    html_result = utility._read_doc({"path": "a.html"}, config)
    assert "Title" in html_result["content"]
    assert "Body" in html_result["content"]
    assert "ignore" not in html_result["content"]


def test_fetch_returns_json_and_blocks_private_by_default(
    tmp_path: Path, monkeypatch,
) -> None:
    config = _config(tmp_path)

    class FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json; charset=utf-8"}
        content = b'{"ok": true, "value": 3}'
        encoding = "utf-8"

    captured = {}

    def fake_request(method, url, **kwargs):
        captured.update({"method": method, "url": url, **kwargs})
        return FakeResponse()

    monkeypatch.setattr(utility.requests, "request", fake_request)

    result = utility._fetch(
        {
            "url": "http://127.0.0.1:8080/api",
            "method": "POST",
            "json": {"x": 1},
            "allow_private": True,
        },
        config,
    )
    assert result["status"] == "success"
    assert result["json"] == {"ok": True, "value": 3}
    assert captured["method"] == "POST"
    assert captured["json"] == {"x": 1}

    with pytest.raises(ValueError, match="private/loopback"):
        utility._fetch({"url": "http://127.0.0.1:8080/api"}, config)


def test_web_search_parses_duckduckgo_html(tmp_path: Path, monkeypatch) -> None:
    config = _config(tmp_path)
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)

    class FakeResponse:
        text = (
            '<a class="result__a" href="https://example.com/a">Title A</a>'
            '<a class="result__snippet">Snippet A</a>'
            '<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fb">'
            'Title B</a><a class="result__snippet">Snippet B</a>'
        )

        def raise_for_status(self):
            return None

    monkeypatch.setattr(utility.requests, "get", lambda *args, **kwargs: FakeResponse())

    result = utility._web_search({"query": "test", "limit": 2}, config)

    assert result["status"] == "success"
    assert result["provider"] == "duckduckgo"
    assert result["results"] == [
        {"title": "Title A", "url": "https://example.com/a", "snippet": "Snippet A"},
        {"title": "Title B", "url": "https://example.com/b", "snippet": "Snippet B"},
    ]
