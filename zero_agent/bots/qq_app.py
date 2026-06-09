"""QQ Bot 前端 for ZeroAgent.

使用 qq-botpy (QQ 官方 Python SDK)。适配 ZeroAgent 的 AgentRunner 接口。
支持 C2C 私聊和群聊 @ 消息, markdown 发送带回退到纯文本。

Usage:
    python -m zero_agent.bots.qq_app
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from collections import deque

from zero_agent.core.agent import ZeroAgent
from zero_agent.runners.agent_runner import AgentRunner
from zero_agent.bots.common import (
    AgentBotMixin,
    ensure_single_instance,
    load_keys,
    public_access,
    redirect_log,
    require_runtime,
    split_text,
)

try:
    import botpy
    from botpy.message import C2CMessage, GroupMessage
except ImportError:
    print("Please install qq-botpy: pip install qq-botpy")
    sys.exit(1)

_KEYS = load_keys()

za = ZeroAgent()
runner = AgentRunner(za)
runner.verbose = False

APP_ID = str(_KEYS.get("qq_app_id", "") or "").strip()
APP_SECRET = str(_KEYS.get("qq_app_secret", "") or "").strip()
ALLOWED = {str(x).strip() for x in _KEYS.get("qq_allowed_users", []) if str(x).strip()}
# 消息去重队列, 防止同一条消息被多次处理
PROCESSED_IDS = deque(maxlen=1000)
USER_TASKS: dict = {}
SEQ_LOCK = threading.Lock()
MSG_SEQ = 1


def _next_msg_seq():
    """获取下一个消息序号, 线程安全."""
    global MSG_SEQ
    with SEQ_LOCK:
        MSG_SEQ += 1
        return MSG_SEQ


def _build_intents():
    """构建 QQ Bot Intents, 兼容不同版本的 botpy SDK.

    优先使用构造函数参数, 回退到逐个设置属性的方式.

    Returns:
        botpy.Intents 实例.
    """
    try:
        return botpy.Intents(public_messages=True, direct_message=True)
    except Exception:
        intents = botpy.Intents.none() if hasattr(botpy.Intents, "none") else botpy.Intents()
        for attr in (
            "public_messages", "public_guild_messages", "direct_message",
            "direct_messages", "c2c_message", "c2c_messages",
            "group_at_message", "group_at_messages",
        ):
            if hasattr(intents, attr):
                try:
                    setattr(intents, attr, True)
                except Exception:
                    pass
        return intents


def _make_bot_class(app):
    """创建 QQ Bot 客户端类, 绑定消息回调到 QQApp.

    Args:
        app: QQApp 实例, 用于处理消息.

    Returns:
        botpy.Client 的子类.
    """
    class QQBot(botpy.Client):
        def __init__(self):
            super().__init__(intents=_build_intents(), ext_handlers=False)

        async def on_ready(self):
            """Bot 就绪回调."""
            print(f"[QQ] bot ready: {getattr(getattr(self, 'robot', None), 'name', 'QQBot')}")

        async def on_c2c_message_create(self, message: C2CMessage):
            """C2C 私聊消息回调."""
            await app.on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: GroupMessage):
            """群聊 @ 消息回调."""
            await app.on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            """频道私信回调."""
            await app.on_message(message, is_group=False)

    return QQBot


class QQApp(AgentBotMixin):
    """QQ Bot 前端.

    通过 botpy WebSocket 接收消息, 转发给 ZeroAgent + AgentRunner 处理,
    支持 markdown 格式回复, 发送失败时自动回退到纯文本.

    Attributes:
        client: botpy.Client 实例, 在 start() 中初始化.
    """

    label = "QQ"
    source = "qq"
    split_limit = 1500

    def __init__(self):
        """初始化 QQApp, 复用全局 runner 和 USER_TASKS."""
        super().__init__(runner, USER_TASKS)
        self.client = None

    async def send_text(self, chat_id, content, *, msg_id=None, is_group=False):
        """发送文本消息.

        优先尝试 markdown 格式, 失败时回退到纯文本.
        长消息自动按 split_limit 分割发送.

        Args:
            chat_id: 目标会话 ID (C2C 为 user_openid, 群聊为 group_openid).
            content: 消息文本内容.
            msg_id: 被回复消息的 ID (可选).
            is_group: 是否为群聊消息.
        """
        if not self.client:
            return
        # 根据消息类型选择对应的 API 端点和 ID 字段
        api = self.client.api.post_group_message if is_group else self.client.api.post_c2c_message
        key = "group_openid" if is_group else "openid"
        for part in split_text(content, self.split_limit):
            seq = _next_msg_seq()
            try:
                # 优先发送 markdown 格式
                await api(**{
                    key: chat_id, "msg_type": 2, "markdown": {"content": part},
                    "msg_id": msg_id, "msg_seq": seq,
                })
            except Exception:
                # markdown 发送失败时回退到纯文本
                await api(**{
                    key: chat_id, "msg_type": 0, "content": part,
                    "msg_id": msg_id, "msg_seq": seq,
                })

    async def on_message(self, data, is_group=False):
        """处理收到的消息.

        执行去重、权限检查后, 将消息路由到 handle_command (斜杠命令)
        或 run_agent (普通对话).

        Args:
            data: botpy 消息对象 (C2CMessage / GroupMessage).
            is_group: 是否群聊消息.
        """
        try:
            # 消息去重
            msg_id = getattr(data, "id", None)
            if msg_id in PROCESSED_IDS:
                return
            PROCESSED_IDS.append(msg_id)
            content = (getattr(data, "content", "") or "").strip()
            if not content:
                return
            # 提取用户和会话标识
            author = getattr(data, "author", None)
            user_id = str(
                getattr(author, "member_openid" if is_group else "user_openid", "")
                or getattr(author, "id", "")
                or "unknown"
            )
            chat_id = str(getattr(data, "group_openid", "") or user_id) if is_group else user_id
            # 访问控制
            if not public_access(ALLOWED) and user_id not in ALLOWED:
                print(f"[QQ] unauthorized user: {user_id}")
                return
            print(f"[QQ] message from {user_id} ({'group' if is_group else 'c2c'}): {content}")
            if content.startswith("/"):
                return await self.handle_command(chat_id, content, msg_id=msg_id, is_group=is_group)
            asyncio.create_task(self.run_agent(chat_id, content, msg_id=msg_id, is_group=is_group))
        except Exception:
            import traceback
            print("[QQ] handle_message error")
            traceback.print_exc()

    async def start(self):
        """启动 QQ Bot, 含自动重连.

        异常断开后递增退避重连 (5s → 300s max),
        稳定运行超过 60s 则重置退避计时器.
        """
        self.client = _make_bot_class(self)()
        delay, max_delay = 5, 300
        while True:
            started_at = time.monotonic()
            try:
                print(f"[QQ] bot starting... {time.strftime('%m-%d %H:%M')}")
                await self.client.start(appid=APP_ID, secret=APP_SECRET)
            except Exception as e:
                print(f"[QQ] bot error: {e}")
            # 稳定运行超 60s 则重置退避, 说明只是临时断线
            if time.monotonic() - started_at >= 60:
                delay = 5
            print(f"[QQ] reconnect in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    _LOCK_SOCK = ensure_single_instance(19528, "QQ")
    require_runtime(runner, "QQ", qq_app_id=APP_ID, qq_app_secret=APP_SECRET)
    redirect_log(__file__, "qqapp.log", "QQ", ALLOWED)
    asyncio.run(QQApp().start())
