"""Bot 前端共享基础设施.

提供 AgentBotMixin (适配 AgentRunner 的 chat mixin) 以及
文本清理、文件提取、历史恢复等工具函数。
"""

from __future__ import annotations

import ast
import asyncio
import glob
import json
import os
import queue as Q
import re
import socket
import sys
import time


# —— 命令列表 ——

HELP_COMMANDS = (
    ("/help", "显示帮助"),
    ("/status", "查看状态"),
    ("/stop", "停止当前任务"),
    ("/new", "开启新对话并清空当前上下文"),
    ("/restore", "恢复上次对话历史"),
    ("/continue", "列出可恢复会话"),
    ("/continue [n]", "恢复第 n 个会话"),
    ("/btw <q>", "side question — 临时插问主 agent 进展, 不打断主线"),
    ("/review [scope]", "in-session code review; 默认审当前 git diff"),
    ("/llm", "查看当前模型列表"),
    ("/llm [n]", "切换到第 n 个模型"),
)

TELEGRAM_MENU_COMMANDS = (
    ("help", "显示帮助"),
    ("status", "查看状态"),
    ("stop", "停止当前任务"),
    ("new", "开启新对话并清空当前上下文"),
    ("restore", "恢复上次对话历史"),
    ("continue", "列出可恢复会话; /continue n 恢复第 n 个"),
    ("btw", "临时插问主 agent 进展, 不打断主线"),
    ("review", "in-session code review; /review scope 指定范围"),
    ("llm", "查看模型列表; /llm n 切换到指定模型"),
)


def build_help_text(commands=HELP_COMMANDS) -> str:
    return "📖 命令列表:\n" + "\n".join(f"{cmd} - {desc}" for cmd, desc in commands)


HELP_TEXT = build_help_text()
FILE_HINT = "If you need to show files to user, use [FILE:filepath] in your response."
TAG_PATS = [r"<" + t + r">.*?</" + t + r">" for t in ("thinking", "summary", "tool_use", "file_content")]
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _restore_globs() -> tuple:
    """Return glob patterns for session log files from the configured sessions dir."""
    from zero_agent.bots.shared.continue_cmd import _sessions_dir
    d = _sessions_dir
    return (os.path.join(d, "model_responses_*.txt"),)
RESTORE_BLOCK_RE = re.compile(
    r"^=== (Prompt|Response) ===.*?\n(.*?)(?=^=== (?:Prompt|Response) ===|\Z)",
    re.DOTALL | re.MULTILINE,
)
HISTORY_RE = re.compile(r"<history>\s*(.*?)\s*</history>", re.DOTALL)
SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


# —— 文本处理 ——

def clean_reply(text: str) -> str:
    """移除 LLM 响应中的标签噪音."""
    for pat in TAG_PATS:
        text = re.sub(pat, "", text or "", flags=re.DOTALL)
    return re.sub(r"\n{3,}", "\n\n", text).strip() or "..."


def extract_files(text: str) -> list:
    """从文本中提取 [FILE:path] 引用."""
    return re.findall(r"\[FILE:([^\]]+)\]", text or "")


def strip_files(text: str) -> str:
    """移除文本中的 [FILE:path] 引用."""
    return re.sub(r"\[FILE:[^\]]+\]", "", text or "").strip()


def split_text(text: str, limit: int) -> list:
    """按长度限制分割文本, 尽量在换行处断开."""
    text, parts = (text or "").strip() or "...", []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit * 0.6:
            cut = limit
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else []) or ["..."]


def build_done_text(raw_text: str) -> str:
    """从 raw LLM 输出构建最终展示文本."""
    files = [p for p in extract_files(raw_text) if os.path.exists(p)]
    body = strip_files(clean_reply(raw_text))
    if files:
        body = (body + "\n\n" if body else "") + "\n".join(f"生成文件: {p}" for p in files)
    return body or "..."


# —— 历史恢复 ——

def _restore_log_files() -> list:
    files = []
    for pattern in _restore_globs():
        files.extend(glob.glob(pattern))
    return sorted(set(files))


def _restore_text_pairs(content: str) -> list | None:
    """尝试从旧格式 (=== USER === / === Response ===) 恢复文本对话."""
    users = re.findall(r"=== USER ===\n(.+?)(?==== |$)", content, re.DOTALL)
    resps = re.findall(r"=== Response ===.*?\n(.+?)(?==== Prompt|$)", content, re.DOTALL)
    if not users or not resps:
        return None
    restored = []
    for u, r in zip(users, resps):
        u, r = u.strip(), r.strip()[:500]
        if u and r:
            restored.extend([f"[USER]: {u}", f"[Agent] {r}"])
    return restored or None


def _native_prompt_obj(prompt_body: str) -> dict | None:
    try:
        prompt = json.loads(prompt_body)
    except Exception:
        return None
    if not isinstance(prompt, dict) or prompt.get("role") != "user":
        return None
    if not isinstance(prompt.get("content"), list):
        return None
    return prompt


def _native_prompt_text(prompt: dict) -> str:
    texts = []
    for block in prompt.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return "\n".join(texts).strip()


def _native_history_lines(prompt_text: str) -> list:
    match = HISTORY_RE.search(prompt_text or "")
    if not match:
        return []
    restored = []
    for line in match.group(1).splitlines():
        line = line.strip()
        if line.startswith("[USER]: ") or line.startswith("[Agent] "):
            restored.append(line)
    return restored


def _native_first_user_line(prompt_text: str) -> str:
    text = (prompt_text or "").strip()
    if not text or "<history>" in text or text.startswith("### [WORKING MEMORY]"):
        return ""
    if text.startswith(FILE_HINT):
        text = text[len(FILE_HINT):].lstrip()
    if "### 用户当前消息" in text:
        text = text.split("### 用户当前消息", 1)[-1].strip()
    return text


def _native_response_summary(response_body: str) -> str:
    try:
        blocks = ast.literal_eval((response_body or "").strip())
    except Exception:
        return ""
    if not isinstance(blocks, list):
        return ""
    text_parts = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if isinstance(text, str) and text:
                text_parts.append(text)
    match = SUMMARY_RE.search("\n".join(text_parts))
    return (match.group(1).strip() if match else "")[:500]


def _restore_native_history(content: str) -> list | None:
    """尝试从 native 格式 (JSON prompt + ast.literal_eval response) 恢复对话."""
    blocks = RESTORE_BLOCK_RE.findall(content or "")
    if not blocks:
        return None
    pairs = []
    pending_prompt = None
    for label, body in blocks:
        if label == "Prompt":
            pending_prompt = body
        elif pending_prompt is not None:
            pairs.append((pending_prompt, body))
            pending_prompt = None
    for prompt_body, response_body in reversed(pairs):
        prompt = _native_prompt_obj(prompt_body)
        if prompt is None:
            continue
        prompt_text = _native_prompt_text(prompt)
        restored = list(_native_history_lines(prompt_text))
        if restored:
            summary = _native_response_summary(response_body)
            summary_line = f"[Agent] {summary}" if summary else ""
            if summary_line and (not restored or restored[-1] != summary_line):
                restored.append(summary_line)
            return restored
        user_text = _native_first_user_line(prompt_text)
        summary = _native_response_summary(response_body)
        if user_text and summary:
            return [f"[USER]: {user_text}", f"[Agent] {summary}"]
    return None


def format_restore() -> tuple:
    """恢复最近一次对话历史. 返回 ((restored_lines, filename, count), None) 或 (None, error_msg)."""
    files = _restore_log_files()
    if not files:
        return None, "❌ 没有找到历史记录"
    latest = max(files, key=os.path.getmtime)
    with open(latest, "r", encoding="utf-8") as f:
        content = f.read()
    restored = _restore_text_pairs(content) or _restore_native_history(content)
    if not restored:
        return None, "❌ 历史记录里没有可恢复内容"
    count = sum(1 for line in restored if line.startswith("[USER]: "))
    return (restored, os.path.basename(latest), count), None


# —— 访问控制 ——

def public_access(allowed: set) -> bool:
    return not allowed or "*" in allowed


def to_allowed_set(value) -> set:
    if value is None:
        return set()
    if isinstance(value, str):
        value = [value]
    return {str(x).strip() for x in value if str(x).strip()}


def allowed_label(allowed: set) -> str:
    return "public" if public_access(allowed) else str(sorted(allowed))


# —— 单实例锁 ——

def ensure_single_instance(port: int, label: str):
    """通过绑定端口确保只有一个 bot 实例运行."""
    try:
        lock_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lock_sock.bind(("127.0.0.1", port))
        return lock_sock
    except OSError:
        print(f"[{label}] Another instance is already running, skipping...")
        sys.exit(1)


# —— 运行时检查 ——

def require_runtime(runner, label: str, **required) -> None:
    """验证必需的运行时配置已就绪."""
    missing = [k for k, v in required.items() if not v]
    if missing:
        print(f"[{label}] ERROR: please set {', '.join(missing)} in mykey.py or mykey.json")
        sys.exit(1)
    if runner.llmclient is None:
        print(f"[{label}] ERROR: no usable LLM backend found")
        sys.exit(1)


# —— 日志重定向 ——

def redirect_log(script_file: str, log_name: str, label: str, allowed: set) -> None:
    """将 stdout/stderr 重定向到日志文件."""
    log_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(script_file))), "temp")
    os.makedirs(log_dir, exist_ok=True)
    logf = open(os.path.join(log_dir, log_name), "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = logf
    print(f"[NEW] {label} process starting, the above are history infos ...")
    print(f"[{label}] allow list: {allowed_label(allowed)}")


# —— 配置加载 ——

def load_keys() -> dict:
    """从项目根目录 mykey.json 或环境变量加载 bot 配置."""
    keys: dict = {}
    # 1. 尝试 mykey.json
    for search in (
        os.path.join(PROJECT_ROOT, "mykey.json"),
        os.path.join(os.getcwd(), "mykey.json"),
    ):
        try:
            with open(search, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                keys.update(data)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            continue
    # 2. 环境变量覆盖
    env_map = {
        "tg_bot_token": "TG_BOT_TOKEN",
        "tg_allowed_users": "TG_ALLOWED_USERS",
        "discord_bot_token": "DISCORD_BOT_TOKEN",
        "discord_allowed_users": "DISCORD_ALLOWED_USERS",
        "proxy": "BOT_PROXY",
    }
    for key, env in env_map.items():
        val = os.environ.get(env, "")
        if val:
            keys[key] = val
    return keys


# —— AgentBotMixin ——

class AgentBotMixin:
    """Bot 前端 mixin, 提供命令处理和 agent 交互.

    适配 ZeroAgent 的 AgentRunner, 替代 GenericAgent 的 AgentChatMixin.
    子类需实现 send_text() 和 (可选) send_done().

    Attributes:
        label: bot 标签 (用于日志).
        source: 任务来源标识 (e.g. "telegram", "discord").
        split_limit: 单条消息长度上限.
        ping_interval: 处理中超时提醒间隔 (秒).
    """

    label = "Chat"
    source = "chat"
    split_limit = 1500
    ping_interval = 20

    def __init__(self, runner, user_tasks: dict):
        """初始化 mixin.

        Args:
            runner: AgentRunner 实例.
            user_tasks: 用户任务状态字典 (chat_id → {"running": bool}).
        """
        self.runner = runner
        self.user_tasks = user_tasks

    async def send_text(self, chat_id, content, **ctx):
        """发送文本消息 (子类必须实现)."""
        raise NotImplementedError

    async def send_done(self, chat_id, raw_text, **ctx):
        """发送任务完成消息 (默认调用 send_text)."""
        await self.send_text(chat_id, build_done_text(raw_text), **ctx)

    async def handle_command(self, chat_id, cmd, **ctx):
        """处理斜杠命令."""
        from zero_agent.bots.shared.continue_cmd import handle_frontend_command, reset_conversation
        from zero_agent.bots.shared.btw_cmd import handle_frontend_command as handle_btw_frontend_command
        from zero_agent.bots.shared.review_cmd import handle as handle_review_command

        parts = (cmd or "").split()
        op = (parts[0] if parts else "").lower()
        runner = self.runner

        if op == "/help":
            return await self.send_text(chat_id, HELP_TEXT, **ctx)
        if op == "/stop":
            state = self.user_tasks.get(chat_id)
            if state:
                state["running"] = False
            runner.abort()
            return await self.send_text(chat_id, "⏹️ 正在停止...", **ctx)
        if op == "/status":
            llm = runner.get_llm_name() if runner.llmclient else "未配置"
            return await self.send_text(
                chat_id,
                f"状态: {'🔴 运行中' if runner.is_running else '🟢 空闲'}\n"
                f"LLM: [{runner.llm_no}] {llm}",
                **ctx,
            )
        if op == "/llm":
            if not runner.llmclient:
                return await self.send_text(chat_id, "❌ 当前没有可用的 LLM 配置", **ctx)
            if len(parts) > 1:
                try:
                    runner.next_llm(int(parts[1]))
                    return await self.send_text(
                        chat_id,
                        f"✅ 已切换到 [{runner.llm_no}] {runner.get_llm_name()}",
                        **ctx,
                    )
                except Exception:
                    return await self.send_text(
                        chat_id, f"用法: /llm <0-{len(runner.list_llms()) - 1}>", **ctx
                    )
            lines = [
                f"{'→' if cur else '  '} [{i}] {name}"
                for i, name, cur in runner.list_llms()
            ]
            return await self.send_text(chat_id, "LLMs:\n" + "\n".join(lines), **ctx)
        if op == "/restore":
            try:
                restored_info, err = format_restore()
                if err:
                    return await self.send_text(chat_id, err, **ctx)
                restored, fname, count = restored_info
                runner.abort()
                runner.history.extend(restored)
                return await self.send_text(
                    chat_id,
                    f"✅ 已恢复 {count} 轮对话\n来源: {fname}\n(仅恢复上下文, 请输入新问题继续)",
                    **ctx,
                )
            except Exception as e:
                return await self.send_text(chat_id, f"❌ 恢复失败: {e}", **ctx)
        if op == "/continue":
            return await self.send_text(
                chat_id, handle_frontend_command(runner, cmd), **ctx
            )
        if op == "/new":
            return await self.send_text(chat_id, reset_conversation(runner), **ctx)
        if op == "/btw":
            answer = await asyncio.to_thread(handle_btw_frontend_command, runner, cmd)
            return await self.send_text(chat_id, answer, **ctx)
        if op == "/review":
            return await self.run_agent(chat_id, cmd, **ctx)
        return await self.send_text(chat_id, HELP_TEXT, **ctx)

    async def run_agent(self, chat_id, text, **ctx):
        """提交任务到 AgentRunner 并流式推送结果."""
        state = {"running": True}
        self.user_tasks[chat_id] = state
        try:
            await self.send_text(chat_id, "思考中...", **ctx)
            dq = self.runner.put_task(
                f"{FILE_HINT}\n\n{text}", source=self.source
            )
            last_ping = time.time()
            while state["running"]:
                try:
                    item = await asyncio.to_thread(dq.get, True, 3)
                except Q.Empty:
                    if self.runner.is_running and time.time() - last_ping > self.ping_interval:
                        await self.send_text(chat_id, "⏳ 还在处理中, 请稍等...", **ctx)
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
