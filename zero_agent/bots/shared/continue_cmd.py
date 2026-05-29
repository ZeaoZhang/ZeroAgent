"""/continue 命令: list & restore past model_responses sessions.

适配 ZeroAgent/AgentRunner, 去除对 GenericAgent 内部结构的 monkey-patch 依赖。
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
import time

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_sessions_dir = os.path.join(_PROJECT_ROOT, "workspace", "sessions")
_sessions_glob = os.path.join(_sessions_dir, "model_responses_*.txt")
_BLOCK_RE = re.compile(
    r"^=== (Prompt|Response) ===.*?\n(.*?)(?=^=== (?:Prompt|Response) ===|\Z)",
    re.DOTALL | re.MULTILINE,
)
_SUMMARY_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_MD_ESCAPE_RE = re.compile(r"([\\`*_\[\]])")
_INJECT_MARKERS = (
    "### [WORKING MEMORY]", "[SYSTEM TIPS]", "[SYSTEM]", "[System]",
    "[DANGER]", "### [总结提炼经验]",
)


def set_sessions_dir(path: str) -> None:
    """Override the sessions log directory at runtime.

    Call this once during app initialization to point session history functions
    at the directory configured in AgentConfig.sessions_dir.
    """
    global _sessions_dir, _sessions_glob
    _sessions_dir = path
    _sessions_glob = os.path.join(path, "model_responses_*.txt")


def _rel_time(mtime: float) -> str:
    d = int(time.time() - mtime)
    if d < 60:
        return f"{d}秒前"
    if d < 3600:
        return f"{d // 60}分前"
    if d < 86400:
        return f"{d // 3600}小时前"
    return f"{d // 86400}天前"


def _pairs(content: str) -> list:
    blocks = _BLOCK_RE.findall(content or "")
    pairs, pending = [], None
    for label, body in blocks:
        if label == "Prompt":
            pending = body.strip()
        elif pending is not None:
            pairs.append((pending, body.strip()))
            pending = None
    return pairs


def _first_user(pairs: list) -> str:
    for p, _ in pairs:
        try:
            msg = json.loads(p)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(msg, dict):
            continue
        for blk in msg.get("content", []) or []:
            if isinstance(blk, dict) and blk.get("type") == "text":
                t = (blk.get("text") or "").strip()
                if t and "<history>" not in t and not t.startswith("### [WORKING MEMORY]"):
                    return t
    for p, _ in pairs[:1]:
        for line in p.splitlines():
            s = line.strip()
            if s and not s.startswith("###") and not s.startswith("{"):
                return s
    return ""


def _last_summary(pairs: list) -> str:
    for _, response_body in reversed(pairs):
        try:
            blocks = ast.literal_eval(response_body)
        except (SyntaxError, ValueError):
            continue
        if not isinstance(blocks, list):
            continue
        text_parts = []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if isinstance(text, str) and text:
                    text_parts.append(text)
        match = _SUMMARY_RE.search("\n".join(text_parts))
        if match:
            summary = match.group(1).strip()
            if summary:
                return summary
    return ""


def _preview_text(pairs: list) -> str:
    return _last_summary(pairs) or _first_user(pairs)


def _escape_md(s: str) -> str:
    return _MD_ESCAPE_RE.sub(r"\\\1", s)


def _user_text(prompt_body: str) -> str:
    """User-typed text from a prompt JSON; '' if this is an agent auto-continuation."""
    try:
        msg = json.loads(prompt_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""
    if not isinstance(msg, dict):
        return ""
    blocks = msg.get("content", []) or []
    if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
        return ""
    for blk in blocks:
        if isinstance(blk, dict) and blk.get("type") == "text":
            t = (blk.get("text") or "").strip()
            if t and not any(mk in t for mk in _INJECT_MARKERS):
                return t
    return ""


def _assistant_text(response_body: str) -> str:
    """Joined plain text from a response blocks repr; '' on parse failure."""
    try:
        blocks = ast.literal_eval(response_body)
    except (SyntaxError, ValueError):
        return ""
    if not isinstance(blocks, list):
        return ""
    return "\n".join(
        b["text"] for b in blocks
        if isinstance(b, dict) and b.get("type") == "text"
        and isinstance(b.get("text"), str) and b["text"].strip()
    )


def _parse_native_history(pairs: list) -> list | None:
    """Try to parse pairs into native message history. Returns None on failure."""
    history = []
    for p, r in pairs:
        try:
            user_msg = json.loads(p)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        try:
            blocks = ast.literal_eval(r)
        except (SyntaxError, ValueError):
            return None
        if not (isinstance(user_msg, dict) and user_msg.get("role") == "user"):
            return None
        if not isinstance(blocks, list):
            return None
        history.append(user_msg)
        history.append({"role": "assistant", "content": blocks})
    return history


def list_sessions(exclude_pid: int | None = None) -> list:
    """Newest-first list of (path, mtime, first_user_text, n_rounds)."""
    files = glob.glob(_sessions_glob)
    if exclude_pid is not None:
        tag = f"model_responses_{exclude_pid}.txt"
        files = [f for f in files if not f.endswith(tag)]
    out = []
    for f in files:
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        pairs = _pairs(content)
        if not pairs:
            continue
        out.append((f, os.path.getmtime(f), _preview_text(pairs), len(pairs)))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def format_list(sessions: list, limit: int = 20) -> str:
    if not sessions:
        return "❌ 没有可恢复的历史会话"
    lines = ["**可恢复会话**（输入 `/continue N` 恢复第 N 个）：", ""]
    for i, (_, mtime, first, n) in enumerate(sessions[:limit], 1):
        preview = _escape_md((first or "（无法预览）").replace("\n", " ")[:60])
        lines.append(f"{i}. `{_rel_time(mtime)}` · **{n} 轮** · {preview}")
    return "\n".join(lines)


def _snapshot_current_log(pid: int | None = None) -> str | None:
    """Persist current PID log as a standalone recoverable snapshot, then clear it."""
    path = os.path.join(_sessions_dir, f"model_responses_{pid or os.getpid()}.txt")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError:
        return None
    if not _pairs(content):
        return None
    os.makedirs(_sessions_dir, exist_ok=True)
    pid_val = pid or os.getpid()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    snapshot = os.path.join(
        _sessions_dir,
        f"model_responses_snapshot_{pid_val}_{stamp}_{time.time_ns() % 1_000_000_000:09d}.txt",
    )
    with open(snapshot, "w", encoding="utf-8", errors="replace") as fh:
        fh.write(content)
    with open(path, "w", encoding="utf-8", errors="replace"):
        pass
    return snapshot


def reset_conversation(runner, message: str = "🆕 已开启新对话，当前上下文已清空") -> str:
    """Abort current work and clear all known conversation state."""
    try:
        runner.abort()
    except Exception:  # abort 可能因多种原因失败, 不影响后续清理
        pass
    _snapshot_current_log()
    if hasattr(runner, "history"):
        try:
            runner.history.clear()
        except AttributeError:
            pass
    # 清理 backend history (best-effort, 不保证所有 backend 都有 history 属性)
    try:
        client = runner.llmclient
        if client is not None:
            backend = getattr(client, "backend", None)
            if backend is not None and hasattr(backend, "history"):
                backend.history = []
            elif hasattr(client, "history"):
                # ZeroAgent: LiteLLMSession is the backend itself
                client.history = []
            if hasattr(client, "last_tools"):
                client.last_tools = ""
    except AttributeError:
        pass
    return message


def restore(runner, path: str) -> tuple[str, bool]:
    """Restore session at path. Returns (msg, is_full)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as e:
        return f"❌ 读取失败: {e}", False
    pairs = _pairs(content)
    if not pairs:
        return f"❌ {os.path.basename(path)} 为空或格式不符", False
    history = _parse_native_history(pairs)
    name = os.path.basename(path)
    if history is not None:
        runner.abort()
        try:
            client = runner.llmclient
            if client is not None:
                backend = getattr(client, "backend", None)
                if backend is not None and hasattr(backend, "history"):
                    backend.history = list(history)
                elif hasattr(client, "history"):
                    # ZeroAgent: LiteLLMSession/AutoFailoverSession is the backend itself
                    client.history = list(history)
        except AttributeError:
            pass
        return f"✅ 已恢复 {len(pairs)} 轮完整对话（{name}）\n(已写入 backend.history，可直接继续)", True
    # 降级: text-based restore
    from zero_agent.bots.common import _restore_text_pairs, _restore_native_history
    summary = _restore_text_pairs(content) or _restore_native_history(content)
    if not summary:
        return f"❌ {name} 无法解析（非 native 且无摘要可提取）", False
    runner.abort()
    try:
        runner.history.extend(summary)
    except AttributeError:
        pass
    n = sum(1 for l in summary if l.startswith("[USER]: "))
    return f"⚠️ 非 native 格式，已降级恢复 {n} 轮摘要（{name}）\n(请输入新问题继续)", False


def _format_tool_use(block: dict) -> str:
    """格式化工具调用块，匹配 agent_loop 的 verbose tool-call header.

    Args:
        block: 工具调用块字典，含 name 和 input 字段.

    Returns:
        格式化后的 markdown 字符串.
    """
    name = block.get("name", "?")
    args = block.get("input", {})
    try:
        pretty = json.dumps(args, indent=2, ensure_ascii=False).replace("\\n", "\n")
    except Exception:
        pretty = str(args)
    return f"🛠️ Tool: `{name}`  📥 args:\n````text\n{pretty}\n````\n"


def _format_tool_result(content) -> str:
    """格式化工具结果，匹配 agent_loop 的五反引号 fence.

    Args:
        content: 工具返回内容（str、list 或 None）.

    Returns:
        格式化后的 fence 字符串.
    """
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(b.get("text", "") or "")
            elif isinstance(b, str):
                parts.append(b)
        body = "\n".join(parts)
    else:
        body = "" if content is None else str(content)
    return f"`````\n{body}\n`````\n"


def _tool_results_from_prompt(prompt_body: str) -> dict:
    """从 Prompt JSON 的 content blocks 中提取 {tool_use_id: formatted_fence}.

    Args:
        prompt_body: Prompt JSON 字符串.

    Returns:
        tool_use_id 到格式化结果 fence 的映射字典.
    """
    try:
        msg = json.loads(prompt_body)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    if not isinstance(msg, dict):
        return {}
    out: dict = {}
    for blk in msg.get("content", []) or []:
        if isinstance(blk, dict) and blk.get("type") == "tool_result":
            tid = blk.get("tool_use_id") or ""
            if tid:
                out[tid] = _format_tool_result(blk.get("content"))
    return out


def _format_response_segment(response_body: str, tool_results: dict) -> str:
    """重建单次 LLM 调用的转录片段: text blocks + tool_use headers + tool_result fences.

    使 fold_turns 在回放模式下看到与 live 模式相同的字符串形状.

    Args:
        response_body: Response blocks 的 repr 字符串.
        tool_results: _tool_results_from_prompt 返回的工具结果映射.

    Returns:
        格式化的转录片段字符串.
    """
    try:
        blocks = ast.literal_eval(response_body)
    except (SyntaxError, ValueError):
        return ""
    if not isinstance(blocks, list):
        return ""
    texts: list[str] = []
    tool_parts: list[str] = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            s = b.get("text", "")
            if isinstance(s, str) and s.strip():
                texts.append(s)
        elif t == "tool_use":
            tool_parts.append(_format_tool_use(b))
            tid = b.get("id") or ""
            if tid and tid in tool_results:
                tool_parts.append(tool_results[tid])
    return "\n\n".join(
        p for p in ["\n\n".join(texts), "\n".join(tool_parts)] if p
    )


def extract_ui_messages(path: str) -> list:
    """解析 model_responses 日志为 [{role, content}, ...] 用于 UI 回放.

    每个用户发起的轮次生成一个 user bubble 和一个 assistant bubble.
    自动续接的 LLM 调用合并到同一 assistant bubble 中，用 ``**LLM Running (Turn N) ...**``
    标记分隔，使 fold_turns 在回放时与 live 模式行为一致.

    Args:
        path: model_responses 日志文件路径.

    Returns:
        [{role, content}, ...] 消息列表.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read()
    except OSError:
        return []
    pairs = _pairs(content)
    if not pairs:
        return []
    # 工具结果存放在下一个 Prompt 的 content 中；使用 look-ahead 索引
    next_tr: list[dict] = [{} for _ in pairs]
    for i in range(len(pairs) - 1):
        next_tr[i] = _tool_results_from_prompt(pairs[i + 1][0])

    out: list[dict] = []
    assistant: dict | None = None
    round_turn = 0
    for i, (prompt, response) in enumerate(pairs):
        user = _user_text(prompt)
        seg = _format_response_segment(response, next_tr[i])
        if user:
            if assistant is not None:
                out.append(assistant)
            out.append({"role": "user", "content": user})
            assistant = {
                "role": "assistant",
                "content": f"\n\n**LLM Running (Turn 1) ...**\n\n{seg}",
            }
            round_turn = 1
        else:
            if assistant is None:
                assistant = {"role": "assistant", "content": ""}
                round_turn = 1
            round_turn += 1
            marker = f"\n\n**LLM Running (Turn {round_turn}) ...**\n\n"
            assistant["content"] = (assistant["content"] or "") + marker + seg
    if assistant is not None:
        out.append(assistant)
    return [m for m in out if (m.get("content") or "").strip()]


def _recent_context(my_pid: int, n: int = 5) -> str:
    """扫描最近 n 个 model_response 文件（排除自身），提取 lastQ / lastA.

    用于实现跨并行会话的上下文感知.

    Args:
        my_pid: 当前进程 PID，其日志会被排除.
        n: 最多扫描的会话数.

    Returns:
        格式化的 [RecentContext] 字符串，无并发会话时返回空字符串.
    """
    out: list[str] = []
    for f in sorted(glob.glob(_sessions_glob), key=os.path.getmtime, reverse=True):
        m = re.search(r"model_responses_(\d+)", os.path.basename(f))
        if not m or m.group(1) == str(my_pid):
            continue
        try:
            c = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        q = s = ""
        for hm in re.finditer(r"<history>(.*?)</history>", c, re.DOTALL):
            u = re.search(r"\[USER\]:\s*(.+?)(?:\\n|<)", hm.group(1))
            if u:
                q = u.group(1)
        sm = _SUMMARY_RE.search(c)
        if sm:
            s = sm.group(1).strip()
        q, s = q[:60].strip(), s[:60].replace("\n", " ").strip()
        out.append(f"· {m.group(1)} | lastQ: {q or '-'} | lastA: {s or '-'}")
        if len(out) >= n:
            break
    if out:
        return (
            "[RecentContext] 近期并行会话（非当前）:\n"
            + "\n".join(out)
            + "\n[/RecentContext]"
        )
    return ""


def handle_frontend_command(runner, query: str, exclude_pid: int | None = None) -> str:
    """Frontend-friendly /continue entry that returns text directly."""
    s = (query or "").strip()
    exclude_pid = os.getpid() if exclude_pid is None else exclude_pid
    if s == "/continue":
        return format_list(list_sessions(exclude_pid=exclude_pid))
    m = re.match(r"/continue\s+(\d+)\s*$", s)
    if not m:
        return "用法: /continue 或 /continue N"
    sessions = list_sessions(exclude_pid=exclude_pid)
    idx = int(m.group(1)) - 1
    if not (0 <= idx < len(sessions)):
        return f"❌ 索引越界（有效范围 1-{len(sessions)}）"
    reset_conversation(runner, message=None)
    msg, _ = restore(runner, sessions[idx][0])
    return msg
