"""MCP 接入测试：配置解析、stdio 协议与工具注册."""

from __future__ import annotations

import json
from pathlib import Path

from zero_agent.extensions.builtin import mcp_client
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolRegistry

FAKE_SERVER = '''
import json
import sys


def handle(message):
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "serverInfo": {"name": "fake", "version": "0"},
        }}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [
            {
                "name": "echo",
                "description": "回显文本",
                "inputSchema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]}}
    if method == "tools/call":
        arguments = (message.get("params") or {}).get("arguments") or {}
        return {"jsonrpc": "2.0", "id": request_id, "result": {
            "content": [{"type": "text", "text": "echo:" + str(arguments.get("text"))}],
        }}
    return None


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    response = handle(message)
    if response is not None:
        print(json.dumps(response), flush=True)
'''


def _runtime(home: Path) -> ExtensionRuntime:
    return ExtensionRuntime(registry=ToolRegistry(), home=home)


def _write_server(tmp_path: Path) -> Path:
    server = tmp_path / "fake_server.py"
    server.write_text(FAKE_SERVER, encoding="utf-8")
    return server


def _write_config(tmp_path: Path, server: Path) -> Path:
    config = {
        "mcpServers": {
            "fake": {
                "command": "python3",
                "args": [str(server)],
                "timeout": 10,
            }
        }
    }
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert mcp_client.load_config(tmp_path) == {}

    def test_broken_json_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / "mcp.json").write_text("{not json", encoding="utf-8")
        assert mcp_client.load_config(tmp_path) == {}

    def test_disabled_server_is_filtered(self, tmp_path: Path) -> None:
        config = {
            "mcpServers": {
                "on": {"command": "x"},
                "off": {"command": "y", "enabled": False},
            }
        }
        (tmp_path / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

        servers = mcp_client.load_config(tmp_path)
        assert set(servers) == {"on"}

    def test_env_placeholder_is_expanded(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        monkeypatch.setenv("MCP_TOKEN", "secret-token")
        config = {
            "mcpServers": {
                "auth": {"command": "x", "env": {"TOKEN": "${MCP_TOKEN}"}},
            }
        }
        (tmp_path / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

        servers = mcp_client.load_config(tmp_path)
        assert servers["auth"]["env"]["TOKEN"] == "secret-token"


class TestStdioClient:
    def test_list_and_call(self, tmp_path: Path) -> None:
        server = _write_server(tmp_path)
        client = mcp_client.StdioMcpClient("python3", [str(server)])
        try:
            client.start()
            tools = client.list_tools()
            assert [tool["name"] for tool in tools] == ["echo"]
            assert client.call_tool("echo", {"text": "hi"}) == "echo:hi"
        finally:
            client.close()

    def test_server_that_exits_is_reported(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.py"
        broken.write_text("import sys\nsys.exit(1)\n", encoding="utf-8")
        client = mcp_client.StdioMcpClient("python3", [str(broken)], timeout=3)
        try:
            try:
                client.start()
            except Exception as exc:  # noqa: BLE001 - 失败本身就是断言目标
                assert "MCP" in str(exc) or "超时" in str(exc)
            else:
                raise AssertionError("应当因进程退出而失败")
        finally:
            client.close()


class TestSetup:
    def test_registers_prefixed_tools(self, tmp_path: Path) -> None:
        server = _write_server(tmp_path)
        _write_config(tmp_path, server)
        runtime = _runtime(tmp_path)

        mcp_client.setup(ExtensionContext("mcp_client", runtime))

        tool = runtime.registry.get("mcp__fake__echo")
        assert tool is not None
        assert runtime.registry.owner_of("mcp__fake__echo") == "mcp_client"

        result = mcp_client._render_content(
            {"content": [{"type": "text", "text": "ok"}]}
        )
        assert result == "ok"

    def test_missing_command_is_skipped(self, tmp_path: Path) -> None:
        config = {"mcpServers": {"http_only": {"url": "https://example.com/mcp"}}}
        (tmp_path / "mcp.json").write_text(json.dumps(config), encoding="utf-8")
        runtime = _runtime(tmp_path)

        mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert runtime.registry.list_all() == []

    def test_no_config_registers_nothing(self, tmp_path: Path) -> None:
        runtime = _runtime(tmp_path)

        mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert runtime.registry.list_all() == []
