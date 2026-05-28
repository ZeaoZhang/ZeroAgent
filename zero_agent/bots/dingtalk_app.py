"""DingTalk (钉钉) Bot 前端 for ZeroAgent.

使用 dingtalk_stream SDK (WebSocket 长连接)。适配 ZeroAgent 的 AgentRunner 接口。
支持文本消息、语音识别、OAuth2 token 自动刷新、markdown 批量发送。

Usage:
    python -m zero_agent.bots.dingtalk_app
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import requests

from zero_agent.core.agent import ZeroAgent
from zero_agent.adapters.agent_runner import AgentRunner
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
    from dingtalk_stream import AckMessage, CallbackHandler, Credential, DingTalkStreamClient
    from dingtalk_stream.chatbot import ChatbotMessage
except ImportError:
    print("Please install dingtalk-stream: pip install dingtalk-stream")
    sys.exit(1)

_KEYS = load_keys()

za = ZeroAgent()
runner = AgentRunner(za)
runner.verbose = False

CLIENT_ID = str(_KEYS.get("dingtalk_client_id", "") or "").strip()
CLIENT_SECRET = str(_KEYS.get("dingtalk_client_secret", "") or "").strip()
ALLOWED = {str(x).strip() for x in _KEYS.get("dingtalk_allowed_users", []) if str(x).strip()}
USER_TASKS: dict = {}


class _DingTalkHandler(CallbackHandler):
    """钉钉 Stream 回调处理器.

    将收到的 ChatbotMessage 转换为统一消息格式, 转交给 DingTalkApp 处理.

    Attributes:
        app: DingTalkApp 实例.
    """

    def __init__(self, app):
        """初始化回调处理器.

        Args:
            app: DingTalkApp 实例, 用于处理消息和发送回复.
        """
        super().__init__()
        self.app = app

    async def process(self, message):
        """处理钉钉推送的消息.

        提取文本内容和语音识别结果, 解析发送者信息, 转交 DingTalkApp.on_message.

        Args:
            message: dingtalk_stream 消息对象.

        Returns:
            (AckMessage.STATUS_OK, "OK") 元组.
        """
        try:
            chatbot_msg = ChatbotMessage.from_dict(message.data)
            text = getattr(getattr(chatbot_msg, "text", None), "content", "") or ""
            # 语音识别结果作为文本回退
            extensions = getattr(chatbot_msg, "extensions", None) or {}
            recognition = (
                ((extensions.get("content") or {}).get("recognition") or "").strip()
                if isinstance(extensions, dict) else ""
            )
            if not (text := text.strip()):
                text = recognition or str(
                    (message.data.get("text", {}) or {}).get("content", "") or ""
                ).strip()
            sender_id = str(
                getattr(chatbot_msg, "sender_staff_id", None)
                or getattr(chatbot_msg, "sender_id", None)
                or "unknown"
            )
            sender_name = getattr(chatbot_msg, "sender_nick", None) or "Unknown"
            await self.app.on_message(
                text, sender_id, sender_name,
                message.data.get("conversationType"),
                message.data.get("conversationId") or message.data.get("openConversationId"),
            )
        except Exception as e:
            print(f"[DingTalk] callback error: {e}")
        return AckMessage.STATUS_OK, "OK"


class DingTalkApp(AgentBotMixin):
    """钉钉 Bot 前端.

    通过 dingtalk_stream WebSocket 接收消息, 使用 HTTP API 批量发送回复.
    OAuth2 token 自动管理, 过期前 60s 自动刷新.

    Attributes:
        client: DingTalkStreamClient 实例.
        access_token: 当前有效的 OAuth2 access token.
        token_expiry: token 过期时间戳.
        background_tasks: 后台 asyncio Task 集合, 防止被 GC 回收.
    """

    label = "DingTalk"
    source = "dingtalk"
    split_limit = 1800

    def __init__(self):
        """初始化 DingTalkApp, 复用全局 runner 和 USER_TASKS."""
        super().__init__(runner, USER_TASKS)
        self.client = None
        self.access_token = None
        self.token_expiry = 0
        # 保持对后台任务的强引用, 防止被 GC 回收
        self.background_tasks: set = set()

    async def _get_access_token(self):
        """获取或刷新 OAuth2 access token.

        token 有效期内直接返回缓存值, 过期前 60s 自动刷新.
        失败时自动重试一次 (间隔 1s).

        Returns:
            access_token 字符串, 失败返回 None.
        """
        if self.access_token and time.time() < self.token_expiry:
            return self.access_token

        def _fetch():
            resp = requests.post(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                json={"appKey": CLIENT_ID, "appSecret": CLIENT_SECRET},
                timeout=20,
            )
            resp.raise_for_status()
            return resp.json()

        last_err = None
        for attempt in range(2):
            try:
                data = await asyncio.to_thread(_fetch)
                self.access_token = data.get("accessToken")
                # 提前 60s 过期, 避免边界情况
                self.token_expiry = time.time() + int(data.get("expireIn", 7200)) - 60
                return self.access_token
            except Exception as e:
                last_err = e
                if attempt == 0:
                    await asyncio.sleep(1)
        print(f"[DingTalk] token error after retry: {last_err}")
        return None

    async def _send_batch_message(self, chat_id, msg_key, msg_param):
        """通过钉钉机器人 HTTP API 发送消息.

        根据 chat_id 前缀自动选择群聊 (group:) 或单聊 API 端点.

        Args:
            chat_id: 目标会话 ID, 群聊以 "group:" 为前缀.
            msg_key: 消息类型键 (如 "sampleMarkdown").
            msg_param: 消息参数字典.

        Returns:
            发送成功返回 True, 失败返回 False.
        """
        token = await self._get_access_token()
        if not token:
            return False
        headers = {"x-acs-dingtalk-access-token": token}
        # 群聊和单聊使用不同的 API 端点和参数格式
        if chat_id.startswith("group:"):
            url = "https://api.dingtalk.com/v1.0/robot/groupMessages/send"
            payload = {
                "robotCode": CLIENT_ID,
                "openConversationId": chat_id[6:],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }
        else:
            url = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"
            payload = {
                "robotCode": CLIENT_ID,
                "userIds": [chat_id],
                "msgKey": msg_key,
                "msgParam": json.dumps(msg_param, ensure_ascii=False),
            }

        def _post():
            resp = requests.post(url, json=payload, headers=headers, timeout=20)
            body = resp.text
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}: {body[:300]}")
            result = resp.json() if "json" in resp.headers.get("content-type", "") else {}
            errcode = result.get("errcode")
            if errcode not in (None, 0):
                raise RuntimeError(f"API errcode={errcode}: {body[:300]}")
            return True

        try:
            return await asyncio.to_thread(_post)
        except Exception as e:
            print(f"[DingTalk] send error: {e}")
            return False

    async def send_text(self, chat_id, content, **_):
        """发送文本消息 (markdown 格式).

        长消息自动按 split_limit 分割发送.

        Args:
            chat_id: 目标会话 ID.
            content: 消息文本内容.
        """
        for part in split_text(content, self.split_limit):
            await self._send_batch_message(
                chat_id, "sampleMarkdown", {"text": part, "title": "Agent Reply"}
            )

    async def on_message(self, content, sender_id, sender_name,
                         conversation_type=None, conversation_id=None):
        """处理收到的消息.

        执行权限检查后, 将消息路由到 handle_command (斜杠命令)
        或 run_agent (普通对话). Agent 任务作为后台 task 运行, 避免阻塞消息循环.

        Args:
            content: 消息文本内容.
            sender_id: 发送者 staff ID.
            sender_name: 发送者昵称.
            conversation_type: 会话类型 ("2" 为群聊).
            conversation_id: 会话 ID.
        """
        try:
            if not content:
                return
            # 访问控制
            if not public_access(ALLOWED) and sender_id not in ALLOWED:
                print(f"[DingTalk] unauthorized user: {sender_id}")
                return
            # 群聊使用 group: 前缀区分会话类型
            is_group = conversation_type == "2" and conversation_id
            chat_id = f"group:{conversation_id}" if is_group else sender_id
            print(f"[DingTalk] message from {sender_name} ({sender_id}): {content}")
            if content.startswith("/"):
                return await self.handle_command(chat_id, content)
            # 后台执行 agent 任务, 不阻塞消息回调
            task = asyncio.create_task(self.run_agent(chat_id, content))
            self.background_tasks.add(task)
            task.add_done_callback(self.background_tasks.discard)
        except Exception:
            import traceback
            print("[DingTalk] handle_message error")
            traceback.print_exc()

    async def start(self):
        """启动钉钉 Bot, 含自动重连.

        异常断开后递增退避重连 (5s → 300s max),
        稳定运行超过 60s 则重置退避计时器.
        """
        self.client = DingTalkStreamClient(Credential(CLIENT_ID, CLIENT_SECRET))
        self.client.register_callback_handler(ChatbotMessage.TOPIC, _DingTalkHandler(self))
        print("[DingTalk] bot starting...")
        delay, max_delay = 5, 300
        while True:
            started_at = time.monotonic()
            try:
                await self.client.start()
            except Exception as e:
                print(f"[DingTalk] stream error: {e}")
            # 稳定运行超 60s 则重置退避
            if time.monotonic() - started_at >= 60:
                delay = 5
            print(f"[DingTalk] reconnect in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    _LOCK_SOCK = ensure_single_instance(19530, "DingTalk")
    require_runtime(runner, "DingTalk",
                    dingtalk_client_id=CLIENT_ID, dingtalk_client_secret=CLIENT_SECRET)
    redirect_log(__file__, "dingtalkapp.log", "DingTalk", ALLOWED)
    asyncio.run(DingTalkApp().start())
