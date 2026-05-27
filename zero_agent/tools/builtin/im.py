"""send_im — 即时消息发送工具.

通过 webhook URL 向 IM 平台发送消息（text/markdown 格式）.
支持企业微信、飞书、钉钉等常见的 webhook 消息格式.

依赖: requests
"""

from __future__ import annotations

from typing import Any, Dict

from zero_agent.tools.registry import ToolDefinition


def send_im(
    webhook_url: str,
    message: str,
    msg_type: str = "text",
    title: str = "",
) -> Dict[str, Any]:
    """通过 webhook 发送即时消息.

    Args:
        webhook_url: 消息平台的 webhook URL.
        message: 消息内容.
        msg_type: 消息类型 "text" 或 "markdown".
        title: Markdown 消息的标题（仅 markdown 类型）.

    Returns:
        发送结果 {"success": bool, "status_code": int, "response": str}.
    """
    import requests as _requests

    if msg_type == "markdown":
        payload: Dict[str, Any] = {
            "msgtype": "markdown",
            "markdown": {
                "title": title or "ZeroAgent Message",
                "text": message,
            },
        }
    else:
        payload = {
            "msgtype": "text",
            "text": {"content": message},
        }

    try:
        resp = _requests.post(webhook_url, json=payload, timeout=15)
        return {
            "success": resp.status_code < 400,
            "status_code": resp.status_code,
            "response": resp.text[:500],
        }
    except Exception as e:
        return {
            "success": False,
            "status_code": 0,
            "response": str(e),
        }


def _make_send_im_handler():
    """创建 send_im 工具的 handler 工厂函数."""

    def handler(args: Dict[str, Any], response: Any, handler_self: Any):
        yield "Sending instant message ...\n"
        result = send_im(
            webhook_url=args.get("webhook_url", ""),
            message=args.get("message", ""),
            msg_type=args.get("msg_type", "text"),
            title=args.get("title", ""),
        )
        return result

    return handler


def register_im_tool(registry: Any) -> None:
    """向 ToolRegistry 注册 send_im 工具.

    Args:
        registry: ToolRegistry 实例.
    """
    tool_def = ToolDefinition(
        name="send_im",
        description="发送即时消息到指定的 webhook URL。/ Send instant message to webhook URL.",
        parameters={
            "type": "object",
            "properties": {
                "webhook_url": {
                    "type": "string",
                    "description": "消息平台的 webhook URL / Webhook URL of the IM platform",
                },
                "message": {
                    "type": "string",
                    "description": "消息内容 / Message content",
                },
                "msg_type": {
                    "type": "string",
                    "enum": ["text", "markdown"],
                    "description": "消息类型 text 或 markdown / Message type",
                },
                "title": {
                    "type": "string",
                    "description": "Markdown 消息标题（仅 markdown 类型）/ Title for markdown messages",
                },
            },
            "required": ["webhook_url", "message"],
        },
        handler=_make_send_im_handler(),
        category="communication",
    )
    registry.register(tool_def)
