"""QQ Bot 前端 for ZeroAgent.

使用 qq-botpy (QQ 官方 Python SDK)。适配 ZeroAgent 的 AgentRunner 接口。
支持 C2C 私聊和群聊 @ 消息, markdown 发送带回退到纯文本。

Usage:
    python -m zero_agent.bots.qq_app
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import threading
import time
from collections import deque
from pathlib import Path

import requests

from zero_agent.core.agent import ZeroAgent
from zero_agent.runners.agent_runner import AgentRunner
from zero_agent.bots.common import (
    AgentBotMixin,
    channel_is_linked,
    ensure_single_instance,
    load_keys,
    public_access,
    redirect_log,
    require_runtime,
    split_text,
    IMAGE_EXTS,
)

try:
    import botpy
    from botpy.http import Route
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
_QQ_MAX_FILE_SIZE = 200 * 1024 * 1024
_QQ_MD5_10M_SIZE = 10_002_432


def _next_msg_seq():
    """获取下一个消息序号, 线程安全."""
    global MSG_SEQ
    with SEQ_LOCK:
        MSG_SEQ += 1
        return MSG_SEQ


def _hash_qq_upload(path: Path):
    """Compute the checksums QQ requires before a local file upload."""
    md5 = hashlib.md5()
    sha1 = hashlib.sha1()
    md5_10m = hashlib.md5()
    size = 0
    prefix_size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            md5.update(chunk)
            sha1.update(chunk)
            if prefix_size < _QQ_MD5_10M_SIZE:
                prefix = chunk[:_QQ_MD5_10M_SIZE - prefix_size]
                md5_10m.update(prefix)
                prefix_size += len(prefix)
    return {
        "size": size,
        "md5": md5.hexdigest(),
        "sha1": sha1.hexdigest(),
        "md5_10m": md5_10m.hexdigest(),
    }


def _read_qq_upload_part(path: Path, offset: int, size: int) -> bytes:
    with path.open("rb") as source:
        source.seek(offset)
        return source.read(size)


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

    async def send_file(self, chat_id, file_path, *, msg_id=None, is_group=False, **_):
        """Upload an image or file and deliver it as a QQ rich-media message."""
        if not self.client:
            return

        try:
            path = Path(file_path)
            file_type = 1 if Path(file_path).suffix.lower() in IMAGE_EXTS else 4
            api = self.client.api
            target_key = "group_openid" if is_group else "openid"
            route_scope = "groups" if is_group else "users"
            send_media = api.post_group_message if is_group else api.post_c2c_message
            hashes = await asyncio.to_thread(_hash_qq_upload, path)
            if hashes["size"] <= 0:
                raise ValueError("Cannot upload an empty file")
            if hashes["size"] > _QQ_MAX_FILE_SIZE:
                raise ValueError("QQ Bot file uploads are limited to 200 MiB")

            prepare_route = Route(
                "POST", f"/v2/{route_scope}/{{{target_key}}}/upload_prepare",
                **{target_key: chat_id},
            )
            prepare_payload = {
                "file_type": file_type,
                "file_name": path.name,
                "file_size": str(hashes["size"]),
                "md5": hashes["md5"],
                "sha1": hashes["sha1"],
                "md5_10m": hashes["md5_10m"],
            }
            prepared = await api._http.request(prepare_route, json=prepare_payload)
            if not isinstance(prepared, dict) or not prepared.get("upload_id"):
                raise RuntimeError("QQ upload preparation returned no upload_id")
            try:
                block_size = int(prepared["block_size"])
                parts = prepared["parts"]
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("QQ upload preparation returned an invalid part plan") from exc
            if block_size <= 0 or not isinstance(parts, list) or not parts:
                raise RuntimeError("QQ upload preparation returned an empty part plan")

            for part_number, part in enumerate(parts, start=1):
                if not isinstance(part, dict):
                    raise RuntimeError("QQ upload preparation returned an invalid part")
                upload_url = part.get("presigned_url") or part.get("upload_url")
                if not upload_url:
                    raise RuntimeError("QQ upload part is missing its presigned URL")
                part_index = part.get("index", part.get("part_index", part_number))
                try:
                    part_index = int(part_index)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError("QQ upload part has an invalid index") from exc

                offset = (part_number - 1) * block_size
                part_data = await asyncio.to_thread(
                    _read_qq_upload_part, path, offset, block_size,
                )
                if not part_data:
                    raise RuntimeError("QQ upload part plan exceeds the local file size")
                response = await asyncio.to_thread(
                    requests.put, upload_url, data=part_data, timeout=300,
                )
                response.raise_for_status()

                finish_route = Route(
                    "POST", f"/v2/{route_scope}/{{{target_key}}}/upload_part_finish",
                    **{target_key: chat_id},
                )
                await api._http.request(finish_route, json={
                    "upload_id": prepared["upload_id"],
                    "part_index": part_index,
                    "block_size": str(len(part_data)),
                    "md5": hashlib.md5(part_data).hexdigest(),
                })

            complete_route = Route(
                "POST", f"/v2/{route_scope}/{{{target_key}}}/files",
                **{target_key: chat_id},
            )
            upload = await api._http.request(complete_route, json={
                "file_type": file_type,
                "file_name": path.name,
                "upload_id": prepared["upload_id"],
                "srv_send_msg": False,
            })
            if not isinstance(upload, dict) or not upload.get("file_info"):
                raise RuntimeError("QQ upload completion returned no file_info")

            await send_media(**{
                target_key: chat_id,
                "msg_type": 7,
                "media": {"file_info": upload["file_info"]},
                "msg_id": msg_id,
                "msg_seq": _next_msg_seq(),
            })
        except Exception as exc:
            print(f"[QQ] failed to send file {file_path}: {exc}")
            await self.send_text(
                chat_id, f"⚠️ 文件发送失败: {os.path.basename(file_path)}",
                msg_id=msg_id, is_group=is_group,
            )

    async def on_message(self, data, is_group=False):
        """处理收到的消息.

        执行去重、权限检查后, 将消息路由到 handle_command (斜杠命令)
        或 run_agent (普通对话).

        Args:
            data: botpy 消息对象 (C2CMessage / GroupMessage).
            is_group: 是否群聊消息.
        """
        if not channel_is_linked(self.source):
            return
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
