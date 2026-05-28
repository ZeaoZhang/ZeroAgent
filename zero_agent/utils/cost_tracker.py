"""Per-session LLM token cost tracking via HookSystem.

使用 ZeroAgent 的 HookSystem (llm_after 事件) 进行 per-thread
token 用量追踪, 避免 GenericAgent 的 monkey-patching 方式.

Usage:
    from zero_agent.utils.cost_tracker import CostTracker
    tracker = CostTracker()
    agent.hooks.register("llm_after", tracker.on_llm_after)
    # ... run agent ...
    stats = tracker.get("za-agent-runner")
    print(f"Input: {stats.input}, Output: {stats.output}")
"""

from __future__ import annotations

import glob
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class TokenStats:
    """单线程 token 用量统计."""

    requests: int = 0
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0
    last_input: int = 0
    last_output: int = 0
    started_at: float = field(default_factory=time.time)

    def total_input_side(self) -> int:
        return self.input + self.cache_create + self.cache_read

    def total_tokens(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read

    def cache_hit_rate(self) -> float:
        side = self.total_input_side()
        return (self.cache_read / side * 100.0) if side else 0.0

    def elapsed_seconds(self) -> float:
        return max(0.0, time.time() - self.started_at)


class CostTracker:
    """Per-thread token 用量追踪器.

    注册在 HookSystem 的 llm_after 事件上, 从 context["usage"]
    提取 token 统计并按线程名累积.

    Usage:
        tracker = CostTracker()
        agent.hooks.register("llm_after", tracker.on_llm_after)
        # 任务完成后:
        stats = tracker.get(threading.current_thread().name)
        print(f"Tokens: {stats.total_tokens()}")
    """

    def __init__(self) -> None:
        self._trackers: Dict[str, TokenStats] = {}
        self._lock = threading.Lock()

    def get(self, thread_name: str) -> TokenStats:
        with self._lock:
            if thread_name not in self._trackers:
                self._trackers[thread_name] = TokenStats()
            return self._trackers[thread_name]

    def reset(self, thread_name: str) -> None:
        with self._lock:
            self._trackers.pop(thread_name, None)

    def all_trackers(self) -> Dict[str, TokenStats]:
        with self._lock:
            return dict(self._trackers)

    def on_llm_after(self, event: str, context: dict) -> None:
        """HookSystem llm_after 回调.

        Args:
            event: 事件名 ("llm_after").
            context: hook context, 包含 "usage" 字典.
        """
        usage = context.get("usage", {})
        if not usage:
            return

        tname = threading.current_thread().name
        stats = self.get(tname)

        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_create_tokens = (
            usage.get("cache_creation_input_tokens", 0)
            or usage.get("cache_creation", 0)
        )
        cache_read_tokens = (
            usage.get("cache_read_input_tokens", 0)
            or usage.get("cache_read", 0)
        )

        stats.requests += 1
        stats.input += input_tokens
        stats.output += output_tokens
        stats.cache_create += cache_create_tokens
        stats.cache_read += cache_read_tokens
        stats.last_input = input_tokens
        stats.last_output = output_tokens


# 匹配 litellm 日志中的 token 信息
_TOKEN_RE = re.compile(r"tokens\s*[=:]\s*(\d+)")
# 匹配 "keyword: number" 对 (write/read cache)
_KEYVAL_RE = re.compile(r"(write|read)\s*[:=]\s*(\d+)", re.IGNORECASE)


def scan_subagent_logs(log_dir: str) -> Dict[str, TokenStats]:
    """扫描子进程 stdout.log 解析 token 用量.

    解析 `temp/*/stdout.log` 中 litellm 输出的 token 信息,
    用于 Conductor 等多进程场景.

    Args:
        log_dir: 包含子进程日志的目录.

    Returns:
        {subagent_id: TokenStats} 映射.
    """
    result: Dict[str, TokenStats] = {}

    for log_path in glob.glob(os.path.join(log_dir, "*", "stdout.log")):
        sid = os.path.basename(os.path.dirname(log_path))
        stats = TokenStats()

        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if "[Output]" in line:
                        m = _TOKEN_RE.search(line)
                        if m:
                            stats.output += int(m.group(1))
                    elif "[Cache]" in line:
                        for m in _KEYVAL_RE.finditer(line):
                            key = m.group(1).lower()
                            val = int(m.group(2))
                            if key == "write":
                                stats.cache_create += val
                            elif key == "read":
                                stats.cache_read += val
        except (OSError, UnicodeDecodeError):
            continue

        result[sid] = stats

    return result
