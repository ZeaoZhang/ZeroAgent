"""MCP 接入扩展：读取单个 ``mcp.json``，把外部工具注册进 ToolRegistry.

设计约束（见 ``docs/extension-design.md`` 第七节）:

- 单文件 ``~/.zeroagent/mcp.json``，使用社区标准的 ``mcpServers`` 键.
- 靠字段形状判断传输方式：有 ``command`` → stdio；有 ``url`` → HTTP（首版未实现）.
- 支持 ``${ENV_NAME}`` 环境变量替换，凭据只放环境变量.
- 工具名统一加前缀 ``mcp__<server>__<tool>``，避免与内置工具重名.
- **不依赖 MCP SDK**：stdio 传输直接按 newline-delimited JSON-RPC 手写，
  因此未配置 MCP 时进程内不需要任何额外依赖.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import re
import select
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

_logger = logging.getLogger("zero_agent.extensions.mcp")

_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT = 10.0
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# 进程退出时统一关闭，避免残留子进程
_live_clients: List["StdioMcpClient"] = []


def _close_all() -> None:
    """关闭全部 MCP 子进程."""
    for client in list(_live_clients):
        try:
            client.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            pass
    _live_clients.clear()


atexit.register(_close_all)


def _expand_env(value: Any) -> Any:
    """递归替换字符串中的 ``${ENV}``.

    Args:
        value: 任意 JSON 值.

    Returns:
        替换后的值；未设置的环境变量替换为空串.
    """
    if isinstance(value, str):

        def repl(match: re.Match[str]) -> str:
            return os.environ.get(match.group(1), "")

        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def load_config(home: Optional[Path]) -> Dict[str, Any]:
    """读取 ``mcp.json``.

    Args:
        home: 扩展根目录.

    Returns:
        ``mcpServers`` 映射；文件不存在或损坏时返回空字典.
    """
    if home is None:
        return {}
    path = Path(home) / "mcp.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _logger.warning("mcp.json 解析失败，已忽略: %s", exc)
        return {}
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return {}
    return {
        name: _expand_env(cfg)
        for name, cfg in servers.items()
        if isinstance(cfg, dict) and cfg.get("enabled", True) is not False
    }


class StdioMcpClient:
    """最小的同步 MCP stdio 客户端.

    只覆盖 ``initialize`` / ``tools/list`` / ``tools/call`` 三个方法，
    足以把外部工具注册进来。
    """

    def __init__(
        self,
        command: str,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        """记录启动参数（不启动进程）."""
        self.command = command
        self.args = list(args or [])
        self.env = dict(os.environ)
        self.env.update({str(k): str(v) for k, v in (env or {}).items()})
        self.cwd = cwd
        self.timeout = timeout
        self._proc: Optional[subprocess.Popen[str]] = None
        self._next_id = 0

    # ---- 进程与协议 -----------------------------------------------------

    def start(self) -> None:
        """启动子进程并完成 MCP 握手.

        Raises:
            RuntimeError: 进程启动或握手失败.
        """
        try:
            self._proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                cwd=self.cwd,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"MCP 进程启动失败: {exc}") from exc
        _live_clients.append(self)

        self._request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "zeroagent", "version": "0.2.0"},
            },
        )
        self._notify("notifications/initialized", {})

    def close(self) -> None:
        """终止子进程."""
        if self._proc is None:
            return
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
        except Exception:  # noqa: BLE001 - 关闭管道失败可忽略
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except Exception:  # noqa: BLE001 - 进程可能已退出
            try:
                self._proc.kill()
            except Exception:  # noqa: BLE001
                pass
        self._proc = None
        if self in _live_clients:
            _live_clients.remove(self)

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出远端工具.

        Returns:
            工具定义列表（含 name / description / inputSchema）.
        """
        response = self._request("tools/list", {})
        tools = response.get("tools")
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> str:
        """调用远端工具并返回文本结果.

        Args:
            name: 远端工具名.
            arguments: 参数字典.

        Returns:
            拼接后的文本内容；失败时返回带 ``Error:`` 前缀的说明.
        """
        try:
            response = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
        except Exception as exc:  # noqa: BLE001 - 工具失败转成可读错误
            return f"Error: MCP 调用失败: {exc}"
        return _render_content(response)

    # ---- 内部 -----------------------------------------------------------

    def _write(self, payload: Dict[str, Any]) -> None:
        """写入一行 JSON-RPC 消息."""
        if self._proc is None or self._proc.stdin is None:
            raise RuntimeError("MCP 进程未启动")
        self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self._proc.stdin.flush()

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        """发送无需响应的通知."""
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self,
        method: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """发送请求并等待匹配 id 的响应.

        Args:
            method: JSON-RPC 方法名.
            params: 参数.

        Returns:
            响应中的 ``result`` 字段.

        Raises:
            TimeoutError: 超时未收到响应.
            RuntimeError: 进程退出或返回错误.
        """
        self._next_id += 1
        request_id = self._next_id
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"MCP 请求 {method} 超时")
            message = self._read_message(remaining)
            if message.get("id") != request_id:
                # 通知或其他请求的响应，忽略
                continue
            if "error" in message:
                raise RuntimeError(f"MCP {method} 返回错误: {message['error']}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def _read_message(self, timeout: float) -> Dict[str, Any]:
        """读取一行 JSON-RPC 消息.

        Args:
            timeout: 最长等待秒数.

        Returns:
            解析后的消息字典.

        Raises:
            TimeoutError: 超时.
            RuntimeError: 进程退出.
        """
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("MCP 进程未启动")
        ready, _, _ = select.select([self._proc.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("MCP 读取超时")
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP 进程已退出")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MCP 返回了非法 JSON: {line[:200]!r}") from exc
        return message if isinstance(message, dict) else {}


def _render_content(response: Dict[str, Any]) -> str:
    """把 ``tools/call`` 结果渲染为文本.

    Args:
        response: ``result`` 字段.

    Returns:
        文本结果.
    """
    if response.get("isError"):
        prefix = "Error: "
    else:
        prefix = ""
    parts: List[str] = []
    content = response.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(f"[{item.get('type')}]")
    if not parts:
        return prefix + json.dumps(response, ensure_ascii=False)
    return prefix + "\n".join(parts)


def _register_server(
    ctx: Any,
    server_name: str,
    config: Dict[str, Any],
) -> int:
    """连接一个 MCP server 并注册其工具.

    Args:
        ctx: 插件上下文.
        server_name: server 名（来自 ``mcpServers`` 的键）.
        config: 该 server 的配置.

    Returns:
        成功注册的工具数量.
    """
    command = config.get("command")
    if not command:
        _logger.warning(
            "MCP server %s 未提供 command（HTTP 传输暂未实现），已跳过",
            server_name,
        )
        return 0

    client = StdioMcpClient(
        command=str(command),
        args=[str(item) for item in (config.get("args") or [])],
        env=config.get("env") if isinstance(config.get("env"), dict) else None,
        cwd=config.get("cwd"),
        timeout=float(config.get("timeout", _DEFAULT_TIMEOUT)),
    )
    client.start()
    tools = client.list_tools()

    registered = 0
    for tool in tools:
        remote_name = str(tool.get("name") or "").strip()
        if not remote_name:
            continue
        exposed = f"mcp__{server_name}__{remote_name}"
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}

        def make_handler(name: str):
            def call(arguments: Dict[str, Any]) -> str:
                return client.call_tool(name, arguments)

            return call

        ctx.tool(
            name=exposed,
            description=str(tool.get("description") or f"MCP {server_name} 工具"),
            parameters=schema,
            evidence_kind=None,
            category="mcp",
        )(make_handler(remote_name))
        registered += 1
    return registered


def setup(ctx: Any) -> None:
    """安装 MCP 扩展.

    Args:
        ctx: 插件上下文（ExtensionContext）.
    """
    servers = load_config(getattr(ctx.runtime, "home", None))
    if not servers:
        return
    for server_name, config in servers.items():
        try:
            count = _register_server(ctx, server_name, config)
            if count:
                _logger.info("MCP server %s 注册了 %d 个工具", server_name, count)
        except Exception as exc:  # noqa: BLE001 - 单个 server 失败不影响其他
            _logger.warning("MCP server %s 连接失败，已跳过: %s", server_name, exc)
