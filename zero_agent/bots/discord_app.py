"""Discord Bot 前端 for ZeroAgent.

使用 discord.py 库。适配 ZeroAgent 的 AgentRunner 接口。
支持 per-chat agent 隔离, 每个频道/私信有独立的对话历史。

Usage:
    python -m zero_agent.bots.discord_app
"""

from __future__ import annotations

import asyncio
import json
import os
import queue as Q
import re
import sys
import threading
import time
from collections import OrderedDict

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMP_DIR = os.path.join(_PROJECT_ROOT, "temp")
MEDIA_DIR = os.path.join(_TEMP_DIR, "discord_media")
ACTIVE_FILE = os.path.join(_TEMP_DIR, "discord_active_channels.json")
ACTIVE_TTL_SECONDS = 30 * 24 * 3600
EXIT_CHANNEL_TEXTS = {"退出该频道", "退出此频道", "退出频道"}
EXIT_THREAD_TEXTS = {"退出该子区", "退出此子区", "退出子区"}
os.makedirs(MEDIA_DIR, exist_ok=True)

from zero_agent.core.agent import ZeroAgent
from zero_agent.adapters.agent_runner import AgentRunner
from zero_agent.bots.common import (
    AgentBotMixin,
    FILE_HINT,
    HELP_TEXT,
    clean_reply,
    ensure_single_instance,
    extract_files,
    format_restore,
    load_keys,
    public_access,
    redirect_log,
    require_runtime,
    split_text,
    strip_files,
)
from zero_agent.bots.shared.continue_cmd import handle_frontend_command, reset_conversation
from zero_agent.bots.shared.btw_cmd import handle_frontend_command as handle_btw_frontend_command

_KEYS = load_keys()
BOT_TOKEN = str(_KEYS.get("discord_bot_token", "") or "").strip()
ALLOWED = {str(x).strip() for x in _KEYS.get("discord_allowed_users", []) if str(x).strip()}

try:
    import discord
except ImportError:
    print("Please install discord.py: pip install discord.py")
    sys.exit(1)


def _extract_discord_progress(text):
    """Return the newest concise <summary> from a streaming transcript."""
    matches = re.findall(r"<summary>\s*(.*?)\s*</summary>", text or "", flags=re.DOTALL)
    if not matches:
        return ""
    summary = re.sub(r"\s+", " ", matches[-1]).strip()
    return summary[:120]


def _strip_discord_transcript(text):
    """Hide LLM/tool transcript noise while preserving the final natural reply."""
    text = text or ""
    text = re.sub(
        r"^\s*\*?\*?LLM Running \(Turn \d+\) \.\.\.\*?\*?\s*$", "", text, flags=re.M
    )
    text = re.sub(
        r"^\s*🛠️\s+.*?(?=^\s*(?:\*?\*?LLM Running|<summary>|$))",
        "", text, flags=re.M | re.DOTALL,
    )
    text = re.sub(r"^\s*(?:✅|❌|ERR|STDOUT|PAT\b|RC\b).*?$", "", text, flags=re.M)
    text = re.sub(r"<tool_use>.*?</tool_use>", "", text, flags=re.DOTALL)
    text = clean_reply(text)
    return strip_files(text).strip()


def _display_done_text(text):
    body = _strip_discord_transcript(text)
    if body and body != "...":
        return body
    summaries = re.findall(r"<summary>\s*(.*?)\s*</summary>", text or "", flags=re.DOTALL)
    if summaries:
        return re.sub(r"\s+", " ", summaries[-1]).strip() or "..."
    return "..."


class DiscordApp(AgentBotMixin):
    """Discord Bot 前端, 使用 AgentBotMixin + per-chat ZeroAgent 隔离.

    每个频道/私信维护独立的 ZeroAgent 实例和对话历史 (LRU 上限 200).
    频道激活机制: @mention 激活, 30 天 TTL, 支持 "退出频道" 手动取消.

    Attributes:
        client: discord.Client 实例.
        background_tasks: 后台 asyncio Task 集合, 防止被 GC 回收.
        _channel_cache: 已解析的 Discord 频道缓存 (LRU, 上限 500).
        _active_channels: 活跃频道字典 {chat_id: {last_seen}}.
        _runners: per-chat AgentRunner 缓存 (LRU, 上限 200).
    """

    label = "Discord"
    source = "discord"
    split_limit = 1900

    def __init__(self):
        """初始化 DiscordApp.

        创建默认 runner 用于兼容性检查, 配置 discord.py Intents
        (message_content, guilds, dm_messages), 绑定事件回调.
        """
        # 创建初始 runner 仅用于兼容性检查; 实际每个 chat 有独立 runner
        za = ZeroAgent()
        default_runner = AgentRunner(za)
        default_runner.verbose = False
        super().__init__(default_runner, {})

        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.dm_messages = True
        proxy = str(_KEYS.get("proxy", "") or "").strip() or None
        self.client = discord.Client(intents=intents, proxy=proxy)
        self.background_tasks = set()
        # LRU 缓存: Discord channel 对象 (上限 500)
        self._channel_cache = OrderedDict()
        self._active_channels = self._load_active_channels()
        self._active_lock = threading.Lock()
        # LRU 缓存: per-chat AgentRunner 实例 (上限 200)
        self._runners = OrderedDict()
        self._agent_lock = threading.Lock()

        @self.client.event
        async def on_ready():
            """Bot 就绪回调."""
            print(f"[Discord] bot ready: {self.client.user} ({self.client.user.id})")

        @self.client.event
        async def on_message(message):
            """消息回调, 转发到 _handle_message."""
            await self._handle_message(message)

    def _chat_id(self, message):
        """从消息对象提取会话标识.

        Args:
            message: discord.Message 实例.

        Returns:
            "dm:{user_id}" 格式 (私信) 或 "ch:{channel_id}" 格式 (频道).
        """
        if isinstance(message.channel, discord.DMChannel):
            return f"dm:{message.author.id}"
        return f"ch:{message.channel.id}"

    def _load_active_channels(self):
        """从 JSON 文件加载活跃频道列表, 自动清理过期条目.

        Returns:
            {chat_id: {last_seen}} 字典.
        """
        try:
            with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
            now = time.time()
            active = {}
            for chat_id, item in data.items():
                if not str(chat_id).startswith("ch:") or not isinstance(item, dict):
                    continue
                last_seen = float(item.get("last_seen") or 0)
                # 只保留 TTL 内的频道
                if now - last_seen <= ACTIVE_TTL_SECONDS:
                    active[str(chat_id)] = {"last_seen": last_seen}
            return active
        except FileNotFoundError:
            return {}
        except Exception as e:
            print(f"[Discord] failed to load active channels: {e}")
            return {}

    def _save_active_channels(self):
        """原子写入活跃频道到 JSON 文件 (先写临时文件再 replace)."""
        try:
            os.makedirs(os.path.dirname(ACTIVE_FILE), exist_ok=True)
            tmp = ACTIVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._active_channels, f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp, ACTIVE_FILE)
        except Exception as e:
            print(f"[Discord] failed to save active channels: {e}")

    def _is_active_channel(self, chat_id, now=None):
        """检查频道是否在激活期内, 过期自动清理.

        Args:
            chat_id: 频道标识 ("ch:xxx").
            now: 当前时间戳, 默认 time.time().

        Returns:
            True 如果频道活跃且未过期.
        """
        now = now or time.time()
        with self._active_lock:
            item = self._active_channels.get(chat_id)
            if not item:
                return False
            if now - float(item.get("last_seen") or 0) > ACTIVE_TTL_SECONDS:
                self._active_channels.pop(chat_id, None)
                self._save_active_channels()
                print(f"[Discord] channel expired: {chat_id}")
                return False
            return True

    def _touch_active_channel(self, chat_id, now=None):
        """更新频道的最后活跃时间.

        仅对 guild 频道生效, DM 不需要激活跟踪.

        Args:
            chat_id: 频道标识.
            now: 时间戳.
        """
        if not chat_id.startswith("ch:"):
            return
        with self._active_lock:
            self._active_channels[chat_id] = {"last_seen": float(now or time.time())}
            self._save_active_channels()

    def _deactivate_channel(self, chat_id):
        """手动取消激活频道, 中止当前任务.

        Args:
            chat_id: 频道标识.

        Returns:
            True 如果频道之前是活跃的.
        """
        with self._active_lock:
            changed = self._active_channels.pop(chat_id, None) is not None
            self._save_active_channels()
        # 停止该频道的运行中任务
        state = self.user_tasks.get(chat_id)
        if state:
            state["running"] = False
        try:
            self._get_runner(chat_id).abort()
        except Exception as e:
            print(f"[Discord] deactivate abort failed for {chat_id}: {e}")
        return changed

    def _get_runner(self, chat_id):
        """获取或创建 per-chat AgentRunner (LRU 缓存, 隔离对话历史).

        每个 chat 有独立的 ZeroAgent + AgentRunner 实例.
        超过 200 个缓存时淘汰最早未使用的.

        Args:
            chat_id: 会话标识.

        Returns:
            该 chat 对应的 AgentRunner 实例.
        """
        with self._agent_lock:
            r = self._runners.get(chat_id)
            if r is None:
                za = ZeroAgent()
                r = AgentRunner(za)
                r.verbose = False
                self._runners[chat_id] = r
                if len(self._runners) > 200:
                    old_chat_id, _old_runner = self._runners.popitem(last=False)
                    print(f"[Discord] dropped runner cache entry: {old_chat_id}")
            else:
                self._runners.move_to_end(chat_id)  # LRU: 标记为最近使用
            return r

    async def _download_attachments(self, message):
        """下载消息中的附件到本地 MEDIA_DIR.

        Args:
            message: discord.Message 实例.

        Returns:
            本地文件路径列表.
        """
        paths = []
        for att in message.attachments:
            safe_name = re.sub(r'[<>:"/\\|?*]', "_", att.filename or f"file_{att.id}")
            local_path = os.path.join(MEDIA_DIR, f"{att.id}_{safe_name}")
            try:
                await att.save(local_path)
                paths.append(local_path)
                print(f"[Discord] saved attachment: {local_path}")
            except Exception as e:
                print(f"[Discord] failed to save attachment {att.filename}: {e}")
        return paths

    async def send_text(self, chat_id, content, **ctx):
        """发送文本消息到指定会话.

        自动按 split_limit 分段, 首次发送时解析并缓存 Discord 频道对象.

        Args:
            chat_id: 会话标识 ("dm:xxx" 或 "ch:xxx").
            content: 消息文本.
        """
        # 解析或从缓存获取 Discord channel 对象
        channel = self._channel_cache.get(chat_id)
        if channel is None:
            try:
                if chat_id.startswith("dm:"):
                    user = await self.client.fetch_user(int(chat_id[3:]))
                    channel = await user.create_dm()
                else:
                    channel = await self.client.fetch_channel(int(chat_id[3:]))
                self._channel_cache[chat_id] = channel
                if len(self._channel_cache) > 500:
                    self._channel_cache.popitem(last=False)  # LRU 淘汰
            except Exception as e:
                print(f"[Discord] cannot resolve channel for {chat_id}: {e}")
                return
        for part in split_text(content, self.split_limit):
            try:
                await channel.send(part)
            except Exception as e:
                print(f"[Discord] send error: {e}")

    async def send_done(self, chat_id, raw_text, **ctx):
        """发送 Agent 完成消息, 含文件附件.

        从 raw_text 中提取 [FILE:path] 引用, 通过 Discord 文件上传发送.

        Args:
            chat_id: 会话标识.
            raw_text: Agent 的原始输出文本.
        """
        files = [p for p in extract_files(raw_text) if os.path.exists(p)]
        body = _display_done_text(raw_text)
        if body and body != "...":
            await self.send_text(chat_id, body, **ctx)
        if files:
            channel = self._channel_cache.get(chat_id)
            if channel:
                for fpath in files:
                    try:
                        await channel.send(file=discord.File(fpath))
                    except Exception as e:
                        print(f"[Discord] failed to send file {fpath}: {e}")
                        await self.send_text(chat_id, f"⚠️ 文件发送失败: {os.path.basename(fpath)}", **ctx)
        # 文件发送失败或无内容时发送回退提示
        if not body and not files:
            await self.send_text(chat_id, "...", **ctx)

    async def handle_command(self, chat_id, cmd, **ctx):
        """处理斜杠命令, 使用 per-chat runner 执行.

        覆盖 AgentBotMixin 默认实现, 将对全局 runner 的操作重定向到
        当前 chat 的隔离 runner.

        Args:
            chat_id: 会话标识.
            cmd: 完整命令字符串 (如 "/help", "/stop").
        """
        r = self._get_runner(chat_id)
        parts = (cmd or "").split()
        op = (parts[0] if parts else "").lower()
        if op == "/help":
            return await self.send_text(chat_id, HELP_TEXT, **ctx)
        if op == "/stop":
            state = self.user_tasks.get(chat_id)
            if state:
                state["running"] = False
            r.abort()
            return await self.send_text(chat_id, "⏹️ 正在停止...", **ctx)
        if op == "/status":
            llm = r.get_llm_name() if r.llmclient else "未配置"
            return await self.send_text(
                chat_id,
                f"状态: {'🔴 运行中' if r.is_running else '🟢 空闲'}\nLLM: [{r.llm_no}] {llm}",
                **ctx,
            )
        if op == "/llm":
            if not r.llmclient:
                return await self.send_text(chat_id, "❌ 当前没有可用的 LLM 配置", **ctx)
            if len(parts) > 1:
                try:
                    r.next_llm(int(parts[1]))
                    return await self.send_text(
                        chat_id, f"✅ 已切换到 [{r.llm_no}] {r.get_llm_name()}", **ctx
                    )
                except Exception:
                    return await self.send_text(
                        chat_id, f"用法: /llm <0-{len(r.list_llms()) - 1}>", **ctx
                    )
            lines = [f"{'→' if cur else '  '} [{i}] {name}" for i, name, cur in r.list_llms()]
            return await self.send_text(chat_id, "LLMs:\n" + "\n".join(lines), **ctx)
        if op == "/restore":
            try:
                restored_info, err = format_restore()
                if err:
                    return await self.send_text(chat_id, err, **ctx)
                restored, fname, count = restored_info
                r.abort()
                r.history.extend(restored)
                return await self.send_text(
                    chat_id,
                    f"✅ 已恢复 {count} 轮对话\n来源: {fname}\n(仅恢复上下文, 请输入新问题继续)",
                    **ctx,
                )
            except Exception as e:
                return await self.send_text(chat_id, f"❌ 恢复失败: {e}", **ctx)
        if op == "/continue":
            return await self.send_text(chat_id, handle_frontend_command(r, cmd), **ctx)
        if op == "/new":
            return await self.send_text(chat_id, reset_conversation(r), **ctx)
        if op == "/btw":
            answer = await asyncio.to_thread(handle_btw_frontend_command, r, cmd)
            return await self.send_text(chat_id, answer, **ctx)
        return await self.send_text(chat_id, HELP_TEXT, **ctx)

    async def run_agent(self, chat_id, text, **ctx):
        """在隔离的 per-chat agent 上执行任务, 流式推送进度.

        从 AgentRunner display queue 消费输出, 提取 <summary> 标签
        作为步骤进度推送给用户.

        Args:
            chat_id: 会话标识.
            text: 用户消息文本.
        """
        r = self._get_runner(chat_id)
        state = {"running": True}
        self.user_tasks[chat_id] = state
        try:
            await self.send_text(chat_id, "思考中...", **ctx)
            dq = r.put_task(f"{FILE_HINT}\n\n{text}", source=self.source)
            last_ping = time.time()
            last_step = ""
            step_no = 0
            while state["running"]:
                try:
                    item = await asyncio.to_thread(dq.get, True, 3)
                except Q.Empty:
                    if r.is_running and time.time() - last_ping > self.ping_interval:
                        await self.send_text(chat_id, "⏳ 还在处理中, 请稍等...", **ctx)
                        last_ping = time.time()
                    continue
                if "next" in item:
                    step = _extract_discord_progress(item.get("next", ""))
                    if step and step != last_step:
                        step_no += 1
                        await self.send_text(chat_id, f"步骤{step_no}: {step}", **ctx)
                        last_step = step
                        last_ping = time.time()
                    continue
                if "done" in item:
                    await self.send_done(chat_id, item.get("done", ""), **ctx)
                    break
            if not state["running"]:
                await self.send_text(chat_id, "⏹️ 已停止", **ctx)
        except Exception as e:
            import traceback
            print(f"[{self.label}] run_agent error: {e}")
            traceback.print_exc()
            await self.send_text(chat_id, f"❌ 错误: {e}", **ctx)
        finally:
            self.user_tasks.pop(chat_id, None)

    async def _handle_message(self, message):
        """处理收到的 Discord 消息.

        执行流程:
        1. 过滤自身/bot消息
        2. 权限检查
        3. Guild 频道: @mention 激活 / 已激活频道保持 / 未激活忽略
        4. 移除 @mention 文本, 下载附件
        5. 路由: 斜杠命令 → handle_command, 普通文本 → run_agent

        Args:
            message: discord.Message 实例.
        """
        # 忽略自身和其他 bot 的消息
        if message.author == self.client.user or message.author.bot:
            return

        is_dm = isinstance(message.channel, discord.DMChannel)
        is_guild = message.guild is not None
        chat_id = self._chat_id(message)
        now = time.time()
        mentioned = bool(is_guild and self.client.user and self.client.user.mentioned_in(message))

        # 缓存 Discord channel 对象供 send_text 复用
        self._channel_cache[chat_id] = message.channel
        if len(self._channel_cache) > 500:
            self._channel_cache.popitem(last=False)

        user_id = str(message.author.id)
        user_name = str(message.author)

        # 访问控制
        if not public_access(ALLOWED) and user_id not in ALLOWED:
            print(f"[Discord] unauthorized user: {user_name} ({user_id})")
            return

        # Guild 频道激活管理: @mention 激活, 已激活保持, 否则忽略
        if is_guild:
            active = self._is_active_channel(chat_id, now)
            if not mentioned and not active:
                return
            if mentioned or active:
                self._touch_active_channel(chat_id, now)

        # 移除 @mention 文本
        content = message.content or ""
        if is_guild and self.client.user:
            content = re.sub(rf"<@!?{self.client.user.id}>", "", content).strip()
        else:
            content = content.strip()

        # "退出频道" / "退出子区" 手动取消激活
        normalized = re.sub(r"\s+", "", content)
        if is_guild and normalized in EXIT_CHANNEL_TEXTS | EXIT_THREAD_TEXTS:
            self._deactivate_channel(chat_id)
            label = "子区" if normalized in EXIT_THREAD_TEXTS else "频道"
            await self.send_text(chat_id, f"✅ 已退出该{label}, 之后除非重新 @ 我, 否则不会主动响应。")
            print(f"[Discord] manually deactivated {chat_id} by {user_name} ({user_id})")
            return

        # 下载附件并追加到消息文本
        attachment_paths = await self._download_attachments(message)
        if attachment_paths:
            paths_text = "\n".join(f"[附件: {p}]" for p in attachment_paths)
            content = f"{content}\n{paths_text}" if content else paths_text

        if not content:
            return

        print(f"[Discord] message from {user_name} ({user_id}, {'dm' if is_dm else 'guild'}): {content[:200]}")

        if content.startswith("/"):
            return await self.handle_command(chat_id, content)

        # 后台执行 agent 任务
        task = asyncio.create_task(self.run_agent(chat_id, content))
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)

    async def start(self):
        """启动 Discord Bot, 含自动重连.

        异常断开后递增退避重连 (5s → 300s max),
        稳定运行超过 60s 则重置退避计时器.
        """
        print("[Discord] bot starting...")
        delay, max_delay = 5, 300
        while True:
            started_at = time.monotonic()
            try:
                await self.client.start(BOT_TOKEN)
            except Exception as e:
                print(f"[Discord] error: {e}")
            if time.monotonic() - started_at >= 60:
                delay = 5
            print(f"[Discord] reconnect in {delay}s...")
            await asyncio.sleep(delay)
            delay = min(delay * 2, max_delay)


if __name__ == "__main__":
    _LOCK_SOCK = ensure_single_instance(19532, "Discord")
    require_runtime(AgentRunner(ZeroAgent()), "Discord", discord_bot_token=BOT_TOKEN)
    redirect_log(__file__, "dcapp.log", "Discord", ALLOWED)
    asyncio.run(DiscordApp().start())
