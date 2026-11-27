"""General-purpose workspace, document, HTTP and search tools.

These are intentionally small, read-mostly primitives that complement the core
file tools and the optional browser bundle:

- find:   glob + content search across a workspace tree
- read_doc: extract text from PDF/Office/CSV/HTML/text documents
- fetch:  HTTP request with size/timeout/private-network guards
- web_search: DuckDuckGo fallback or Brave Search API when configured
"""

from __future__ import annotations

import base64
import csv
import fnmatch
import ipaddress
import json
import os
import re
import socket
import sys
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urlparse

import requests

from zero_agent.core.config import AgentConfig
from zero_agent.tools.registry import ToolRegistry
from zero_agent.tools.builtin.file import resolve_file_path
from zero_agent.utils.doc_extract import extract_pdf_quartz, parse_page_spec
from zero_agent.utils.text import format_error

_SKIP_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build", "target",
}


def _t(zh: str, en: str, lang: str) -> str:
    return zh if lang == "zh" else en


def _clamp(value: Any, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(int(value), high))
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_text(value: Any, limit: int = 400) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "..."


def _find(args: Dict[str, Any], config: AgentConfig) -> dict:
    root = resolve_file_path(str(args.get("path") or "."), config)
    if not os.path.isdir(root):
        return {"status": "error", "msg": f"not a directory: {root}"}
    pattern = str(args.get("pattern") or "*").strip()
    content = args.get("content")
    include_hidden = bool(args.get("include_hidden", False))
    max_results = _clamp(args.get("max_results"), 100, 1, 1000)
    regex: Optional[re.Pattern[str]] = None
    if content not in (None, ""):
        try:
            regex = re.compile(str(content), re.IGNORECASE)
        except re.error as exc:
            return {"status": "error", "msg": f"invalid content regex: {exc}"}

    def matches(name: str, rel: str) -> bool:
        if not pattern or pattern in {"*", "**", "**/*"}:
            return True
        if "/" in pattern or "\\" in pattern:
            return fnmatch.fnmatch(rel, pattern)
        return fnmatch.fnmatch(name, pattern)

    found: List[dict] = []
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name for name in dirnames
            if name not in _SKIP_DIRS and (include_hidden or not name.startswith("."))
        ]
        for name in filenames:
            if not include_hidden and name.startswith("."):
                continue
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if not matches(name, rel):
                continue
            scanned += 1
            if regex is None:
                found.append({"path": full, "relative": rel})
            else:
                try:
                    if os.path.getsize(full) > 2 * 1024 * 1024:
                        continue
                    with open(full, "r", encoding="utf-8", errors="replace") as handle:
                        for lineno, line in enumerate(handle, 1):
                            if regex.search(line):
                                found.append({
                                    "path": full,
                                    "relative": rel,
                                    "line": lineno,
                                    "text": line.rstrip("\r\n")[:400],
                                })
                                if len(found) >= max_results:
                                    break
                except (OSError, UnicodeError):
                    continue
            if len(found) >= max_results:
                break
        if len(found) >= max_results:
            break
    return {
        "status": "success", "root": root, "pattern": pattern,
        "content": str(content) if content not in (None, "") else None,
        "scanned": scanned, "count": len(found), "matches": found,
        "truncated": len(found) >= max_results,
    }


class _HTMLText(HTMLParser):
    """Small, dependency-free HTML-to-text extractor."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip += 1
        elif tag in {"p", "div", "li", "tr", "br", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip:
            self._skip -= 1
        elif tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        return "\n".join(
            line.strip() for line in "".join(self.parts).splitlines() if line.strip()
        )


def _html_text(raw: str) -> str:
    parser = _HTMLText()
    parser.feed(raw)
    return parser.text()


def _extract_pdf(path: str, pages: Any) -> tuple[str, dict]:
    # 优先 pypdf（跨平台），不可用时回退 macOS 原生 Quartz（零依赖）
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        PdfReader = None  # type: ignore

    if PdfReader is not None:
        reader = PdfReader(path)
        selected = parse_page_spec(pages, len(reader.pages))
        parts = []
        for index in selected:
            text = reader.pages[index].extract_text() or ""
            parts.append(f"--- page {index + 1} ---\n{text}")
        return "\n\n".join(parts), {
            "pages": [i + 1 for i in selected],
            "total_pages": len(reader.pages),
            "backend": "pypdf",
        }

    if sys.platform == "darwin":
        return extract_pdf_quartz(path, pages)

    raise RuntimeError(
        "PDF support requires pypdf; install zero-agent[docs]",
    )


def _extract_docx(path: str) -> tuple[str, dict]:
    try:
        from docx import Document  # type: ignore
    except ImportError:
        from zero_agent.utils.doc_extract import extract_docx as _fallback_docx

        return _fallback_docx(path)
    document = Document(path)
    parts = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            parts.append("\t".join(cell.text for cell in row.cells))
    return "\n".join(parts), {"paragraphs": len(document.paragraphs)}


def _extract_xlsx(path: str, max_rows: int = 2000) -> tuple[str, dict]:
    try:
        import openpyxl  # type: ignore
    except ImportError:
        from zero_agent.utils.doc_extract import extract_xlsx as _fallback_xlsx

        return _fallback_xlsx(path, max_rows)
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    rows_seen = 0
    for sheet in workbook.worksheets:
        parts.append(f"--- sheet: {sheet.title} ---")
        for row in sheet.iter_rows(values_only=True):
            rows_seen += 1
            if rows_seen > max_rows:
                break
            parts.append("\t".join("" if cell is None else str(cell) for cell in row))
        if rows_seen > max_rows:
            break
    workbook.close()
    return "\n".join(parts), {"rows": min(rows_seen, max_rows)}


def _extract_pptx(path: str) -> tuple[str, dict]:
    try:
        from pptx import Presentation  # type: ignore
    except ImportError:
        from zero_agent.utils.doc_extract import extract_pptx as _fallback_pptx

        return _fallback_pptx(path)
    presentation = Presentation(path)
    parts = []
    for index, slide in enumerate(presentation.slides, 1):
        parts.append(f"--- slide {index} ---")
        for shape in slide.shapes:
            text = getattr(shape, "text", "")
            if text:
                parts.append(text)
    return "\n".join(parts), {"slides": len(presentation.slides)}


def _read_doc(args: Dict[str, Any], config: AgentConfig) -> dict:
    path = resolve_file_path(str(args.get("path") or ""), config)
    if not os.path.isfile(path):
        return {"status": "error", "msg": f"file not found: {path}"}
    max_chars = _clamp(args.get("max_chars"), 20000, 1000, 200000)
    suffix = os.path.splitext(path)[1].lower()
    meta: dict = {}
    if suffix == ".pdf":
        content, meta = _extract_pdf(path, args.get("pages"))
    elif suffix == ".docx":
        content, meta = _extract_docx(path)
    elif suffix in {".xlsx", ".xlsm"}:
        content, meta = _extract_xlsx(path)
    elif suffix == ".pptx":
        content, meta = _extract_pptx(path)
    elif suffix in {".html", ".htm"}:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = _html_text(handle.read())
    elif suffix == ".csv":
        rows = []
        with open(path, "r", encoding="utf-8", errors="replace", newline="") as handle:
            for row in csv.reader(handle):
                rows.append("\t".join(row))
                if len(rows) >= 2000:
                    break
        content = "\n".join(rows)
        meta = {"rows": len(rows)}
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError as exc:
            return {"status": "error", "msg": str(exc)}
        if "\x00" in content[:4096]:
            return {
                "status": "error",
                "msg": "binary file; use a format-specific reader or code_run",
            }
        meta = {"format": "text"}
    truncated = len(content) > max_chars
    if truncated:
        content = content[:max_chars] + "\n...[truncated]"
    return {
        "status": "success", "path": path, "format": suffix.lstrip(".") or "text",
        "chars": len(content), "truncated": truncated, "content": content,
        **meta,
    }


def _guard_url(url: str, allow_private: bool) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute http(s) URL")
    if allow_private:
        return
    host = parsed.hostname
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        infos = socket.getaddrinfo(host, None)
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for address in addresses:
        if (
            address.is_private or address.is_loopback or address.is_link_local
            or address.is_reserved or address.is_multicast
        ):
            raise ValueError(
                "private/loopback address blocked; pass allow_private=true only "
                "for trusted internal APIs",
            )


def _decode_text(content: bytes, response: requests.Response) -> str:
    encoding = getattr(response, "encoding", None) or "utf-8"
    return content.decode(encoding, errors="replace")


def _fetch(args: Dict[str, Any], config: AgentConfig) -> dict:
    url = str(args.get("url") or "").strip()
    _guard_url(url, bool(args.get("allow_private", False)))
    method = str(args.get("method") or "GET").upper()
    headers = args.get("headers") or {}
    if not isinstance(headers, dict):
        return {"status": "error", "msg": "headers must be an object"}
    body = args.get("body")
    json_payload = args.get("json")
    if body is not None and json_payload is not None:
        return {"status": "error", "msg": "body and json are mutually exclusive"}
    timeout = _clamp(args.get("timeout_seconds"), 20, 1, 120)
    max_bytes = _clamp(args.get("max_bytes"), 1_000_000, 1024, 10_000_000)
    save_to_file = args.get("save_to_file")
    if save_to_file:
        save_to_file = resolve_file_path(str(save_to_file), config)
    response = requests.request(
        method, url, headers=headers,
        data=body if body is not None else None,
        json=json_payload if json_payload is not None else None,
        timeout=timeout, allow_redirects=True,
    )
    raw = response.content or b""
    truncated = len(raw) > max_bytes
    payload = raw[:max_bytes]
    if truncated and not save_to_file:
        return {
            "status": "error",
            "msg": f"response is {len(raw)} bytes, exceeds max_bytes={max_bytes}; pass save_to_file",
            "status_code": response.status_code,
        }
    content_type = response.headers.get("content-type", "")
    saved = None
    if save_to_file:
        os.makedirs(os.path.dirname(str(save_to_file)) or ".", exist_ok=True)
        with open(str(save_to_file), "wb") as handle:
            handle.write(payload)
        saved = {"path": os.path.abspath(str(save_to_file)), "bytes": len(payload)}
    result: dict = {
        "status": "success", "url": url, "status_code": response.status_code,
        "content_type": content_type, "bytes": len(payload),
        "truncated": truncated, "saved": saved,
    }
    is_text = content_type.startswith("text/") or any(
        marker in content_type for marker in ("json", "xml", "javascript", "x-www-form-urlencoded")
    )
    if is_text:
        text = _decode_text(payload, response)
        result["text"] = text
        if "json" in content_type:
            try:
                result["json"] = json.loads(text)
            except ValueError:
                pass
    elif not save_to_file:
        result["content_base64"] = base64.b64encode(payload).decode("ascii")
    return result


class _DDGParser(HTMLParser):
    """Parse result titles/snippets from DuckDuckGo's HTML endpoint."""

    def __init__(self) -> None:
        super().__init__()
        self.results: List[dict] = []
        self._current: Optional[dict] = None
        self._capture: Optional[str] = None
        self._text: List[str] = []

    def _finish(self) -> None:
        if self._current is not None:
            self.results.append(self._current)
            self._current = None

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag != "a":
            return
        attributes = dict(attrs)
        classes = str(attributes.get("class") or "")
        if "result__a" in classes:
            self._finish()
            self._current = {
                "title": "", "url": attributes.get("href") or "", "snippet": "",
            }
            self._capture = "title"
            self._text = []
        elif "result__snippet" in classes and self._current is not None:
            self._capture = "snippet"
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._capture and self._current is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if not self._capture or self._current is None:
            return
        if tag not in {"a", "div", "span", "p"}:
            return
        text = " ".join("".join(self._text).split())
        if self._capture == "title":
            self._current["title"] = text
        else:
            self._current["snippet"] = (
                self._current.get("snippet", "") + " " + text
            ).strip()
        self._capture = None
        self._text = []

    def close(self) -> None:
        super().close()
        self._finish()


def _unwrap_ddg(href: str) -> str:
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    target = parse_qs(parsed.query).get("uddg")
    if target:
        return target[0]
    return href


def _web_search(args: Dict[str, Any], config: AgentConfig) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"status": "error", "msg": "query is required"}
    limit = _clamp(args.get("limit"), 5, 1, 20)
    timeout = _clamp(args.get("timeout_seconds"), 15, 1, 60)
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        response = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": limit},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": brave_key,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        results = []
        for item in (payload.get("web", {}) or {}).get("results", [])[:limit]:
            results.append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("description", ""),
            })
        return {"status": "success", "query": query, "provider": "brave", "results": results}

    response = requests.get(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
            ),
        },
        timeout=timeout,
    )
    response.raise_for_status()
    parser = _DDGParser()
    parser.feed(response.text)
    parser.close()
    results = []
    for item in parser.results:
        url = _unwrap_ddg(item.get("url", ""))
        if not url.startswith(("http://", "https://")):
            continue
        results.append({
            "title": _safe_text(item.get("title"), 200),
            "url": url,
            "snippet": _safe_text(item.get("snippet"), 400),
        })
        if len(results) >= limit:
            break
    if not results:
        return {"status": "error", "msg": "no search results parsed", "provider": "duckduckgo"}
    return {"status": "success", "query": query, "provider": "duckduckgo", "results": results}


def _handler_for(name: str, fn: Any):
    def handler(args: Dict[str, Any], _response: Any, _handler: Any):
        yield f"[{name}] running ...\n"
        try:
            return fn(args)
        except Exception as exc:  # noqa: BLE001 - tool errors are model-visible
            return {"status": "error", "msg": format_error(exc)}
    return handler


def register_utility_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """Register find/read_doc/fetch/web_search."""
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="find",
        description=_t(
            "在 workspace 中按 glob 查找文件，或按正则搜索文件内容；返回路径、行号和命中行。"
            "适合不知道文件位置时先定位，再用 file_read 阅读。",
            "Find files by glob or search file contents by regex in the workspace; returns "
            "paths, line numbers and matching lines. Use before file_read when the path is unknown.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _t("搜索根目录，默认当前目录", "Search root; defaults to current directory", lang)},
                "pattern": {"type": "string", "description": _t("glob，如 **/*.py；默认 *", "Glob, e.g. **/*.py; defaults to *", lang)},
                "content": {"type": "string", "description": _t("内容正则；提供后返回命中行", "Content regex; returns matching lines", lang)},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100, "description": _t("最多返回条数", "Maximum results", lang)},
                "include_hidden": {"type": "boolean", "description": _t("包含隐藏目录/文件", "Include hidden paths", lang)},
            },
        },
        handler=_handler_for("find", lambda args: _find(args, config)),
        evidence_kind="read",
        category="file",
        effects=("read",),
    ))

    registry.register(ToolDefinition(
        name="read_doc",
        description=_t(
            "读取文本、HTML、CSV、PDF、DOCX、XLSX 或 PPTX 并提取纯文本；PDF 可用 pages 选择页范围。"
            "PDF/Office 需要安装 zero-agent[docs]。",
            "Read text, HTML, CSV, PDF, DOCX, XLSX or PPTX and extract plain text; PDFs accept "
            "a page range. PDF/Office formats require zero-agent[docs].",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": _t("文档路径（相对路径基于 workspace）", "Document path (relative paths use workspace)", lang)},
                "pages": {"type": "string", "description": _t("PDF 页范围，如 1-5,8；默认前 20 页", "PDF pages, e.g. 1-5,8; defaults to first 20", lang)},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 20000, "description": _t("最大返回字符数", "Maximum returned characters", lang)},
            },
            "required": ["path"],
        },
        handler=_handler_for("read_doc", lambda args: _read_doc(args, config)),
        evidence_kind="read",
        category="file",
        effects=("read",),
    ))

    registry.register(ToolDefinition(
        name="fetch",
        description=_t(
            "发送 HTTP 请求并返回状态、响应头和文本/JSON。默认拒绝私网/回环地址；"
            "二进制或大响应请传 save_to_file。适合 API、文档和公开资源，不适合需要登录态的页面。",
            "Send an HTTP request and return status, headers and text/JSON. Private/loopback "
            "addresses are blocked by default; pass save_to_file for binary or large responses. "
            "For logged-in pages use the browser tools.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": _t("http(s) URL", "http(s) URL", lang)},
                "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], "default": "GET"},
                "headers": {"type": "object", "additionalProperties": {"type": "string"}, "description": _t("请求头", "Request headers", lang)},
                "body": {"type": "string", "description": _t("原始请求体", "Raw request body", lang)},
                "json": {"type": "object", "description": _t("JSON 请求体", "JSON request body", lang)},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 120, "default": 20},
                "max_bytes": {"type": "integer", "minimum": 1024, "maximum": 10000000, "default": 1000000},
                "save_to_file": {"type": "string", "description": _t("保存响应体到文件", "Save response body to a file", lang)},
                "allow_private": {"type": "boolean", "description": _t("允许访问私网/回环地址", "Allow private/loopback addresses", lang)},
            },
            "required": ["url"],
        },
        handler=_handler_for("fetch", lambda args: _fetch(args, config)),
        evidence_kind="web",
        category="web",
        effects=("network", "write"),
        write_path_keys=("save_to_file",),
        optional_write_paths=True,
    ))

    registry.register(ToolDefinition(
        name="web_search",
        description=_t(
            "搜索网页。默认使用 DuckDuckGo HTML；配置 BRAVE_SEARCH_API_KEY 后使用 Brave Search API。"
            "返回标题、URL 和摘要，不抓取页面正文。",
            "Search the web. Uses DuckDuckGo HTML by default, or the Brave Search API when "
            "BRAVE_SEARCH_API_KEY is set. Returns titles, URLs and snippets; it does not fetch pages.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": _t("搜索关键词", "Search query", lang)},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 5, "description": _t("返回结果数", "Number of results", lang)},
                "timeout_seconds": {"type": "number", "minimum": 1, "maximum": 60, "default": 15},
            },
            "required": ["query"],
        },
        handler=_handler_for("web_search", lambda args: _web_search(args, config)),
        evidence_kind="web",
        category="web",
        effects=("network",),
    ))
