"""/btw 命令: side question — 不打断主 Agent 的临时 subagent 问答。

适配 ZeroAgent/AgentRunner。与 GenericAgent 版本不同, ZeroAgent 的 litellm session
不暴露 raw_ask/make_messages 内部接口, 因此 /btw 创建独立的 side runner,
避免把临时插问排入主 runner 的串行任务队列。
"""

from __future__ import annotations

import os
import queue
import time

from zero_agent.adapters.agent_runner import AgentRunner
from zero_agent.core.agent import ZeroAgent


_WRAPPER_ZH = """<system-reminder>
这是用户的临时插问 (side question)。主 agent 仍在后台运行, **不会被打断**。

身份与边界:
- 你是一个独立的轻量 sub-agent
- 上下文里能看到主 agent 与用户的完整对话、最近的工具调用与结果
- 用户在问当前进展或顺便确认某事——基于已有信息**一次性**作答
- 没有任何工具可用: 不要"让我查一下" / "我去试试" / 任何承诺动作
- 信息不足就坦白说"基于目前对话我不知道"

侧问内容如下:
</system-reminder>

{question}"""

_WRAPPER_EN = """<system-reminder>
This is a side question from the user. The main agent is NOT interrupted — it continues in the background.

Identity & boundaries:
- You are an independent lightweight sub-agent
- You can see the full conversation between the main agent and the user, plus recent tool calls/results
- The user is asking about current progress or a quick aside — answer in **one shot** from existing info
- You have NO tools — never say "let me check" / "I'll try" / any action promise
- If info is missing, just say "based on the conversation I don't know"

Question:
</system-reminder>

{question}"""

_TIMEOUT_SEC = 120


def _wrapper() -> str:
    return _WRAPPER_EN if os.environ.get("GA_LANG") == "en" else _WRAPPER_ZH


def _strip_cmd(query: str) -> str:
    s = (query or "").strip()
    return s[len("/btw"):].strip() if s.startswith("/btw") else s


def _help_text() -> str:
    return (
        "**/btw 用法**: side question — 临时问主 agent 当前进展, 不打断主线\n\n"
        "`/btw <你的问题>`\n\n"
        "行为: 抓取当前对话上下文 → 单轮纯文本作答(无工具) → 主 agent 历史不变。"
    )


def _format(question: str, body: str, took: float) -> str:
    head = f"> 🟡 /btw {question}\n\n"
    return head + (body.strip() or "*(空回复)*") + f"\n\n*({took:.1f}s)*"


def _make_side_runner(runner) -> AgentRunner:
    """Create an independent side runner from main config and history snapshot."""
    config = runner.config_snapshot() if hasattr(runner, "config_snapshot") else runner.config
    history = runner.history_snapshot() if hasattr(runner, "history_snapshot") else list(getattr(runner, "history", []) or [])
    side_runner = AgentRunner(ZeroAgent(config=config))
    if hasattr(side_runner, "replace_history"):
        side_runner.replace_history(history)
    return side_runner


def _run(runner, question: str, deadline: float, side_runner_factory=None) -> str:
    """Run /btw on an independent side runner, never on the main queue."""
    try:
        prompt = _wrapper().format(question=question)
        side_runner_factory = side_runner_factory or _make_side_runner
        side_runner = side_runner_factory(runner)
        dq = side_runner.put_task(prompt, source="btw-sidequest")
        text_parts: list[str] = []
        while time.time() < deadline:
            try:
                item = dq.get(timeout=3)
            except queue.Empty:
                continue
            if "next" in item:
                text_parts.append(str(item["next"]))
            if "done" in item:
                return str(item.get("done", ""))
        return "\n".join(text_parts) + "\n\n⚠️ /btw 超时, 仅返回部分回复。"
    except Exception as e:
        return f"❌ /btw 失败: {type(e).__name__}: {e}"


def handle_frontend_command(runner, query: str) -> str:
    """同步入口: 返回文本字符串 (供 tg/wx/discord 等前端使用).

    注意: 这是一个阻塞调用, 会等待 btw 子任务完成或超时。
    在异步 bot (如 Telegram) 中应通过 asyncio.to_thread 调用。
    """
    question = _strip_cmd(query)
    if not question or question in ("help", "?", "-h", "--help"):
        return _help_text()
    started = time.time()
    body = _run(runner, question, started + _TIMEOUT_SEC)
    return _format(question, body, time.time() - started)
