"""AgentRunner — ZeroAgent 后台线程包装器.

将 ZeroAgent 的 generator-based run() 包装为兼容 GenericAgent 的
queue-based put_task() 接口，使现有 Bot/Conductor/Desktop Bridge 等
消费者无需改动即可对接 ZeroAgent.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Dict, List, Optional

from zero_agent.core.agent import ZeroAgent


class AgentRunner:
    """在后台线程中运行 ZeroAgent, 提供 queue 风格的任务接口.

    与 GenericAgent 的 put_task() / display_queue 模式兼容.
    put_task() 非阻塞, 立即返回消费者可读的 queue.Queue.

    Usage:
        za = ZeroAgent(config=config)
        runner = AgentRunner(za)
        dq = runner.put_task("hello world", source="telegram")
        while True:
            item = dq.get()
            if "next" in item:
                print(item["next"], end="", flush=True)
            if "done" in item:
                print(item["done"])
                break
    """

    def __init__(self, agent: ZeroAgent) -> None:
        self._agent = agent
        self._task_queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._running = False
        self._stop_sig = False
        self._worker_thread: Optional[threading.Thread] = None

    # —— 兼容 GenericAgent 的公开属性 ——

    @property
    def za(self):
        """返回底层 ZeroAgent 实例 (兼容前端直接访问)."""
        return self._agent

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def handler(self):
        return self._agent.handler

    @property
    def history(self):
        try:
            return self._agent.client.history
        except AttributeError:
            return []

    @property
    def llm_no(self) -> int:
        """当前活跃 LLM 的索引 (兼容 GenericAgent)."""
        backends = self._agent.list_backends()
        for i, (_, _, active) in enumerate(backends):
            if active:
                return i
        return 0

    @property
    def verbose(self) -> bool:
        return self._agent.config.verbose

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self._agent.config.verbose = value

    @property
    def llmclient(self):
        """兼容 GenericAgent 的 llmclient 检查 (非 None 表示已配置)."""
        return self._agent.client

    # —— LLM 管理 (兼容 GenericAgent bot /list_llms + /llm n) ——

    def list_llms(self) -> List[tuple]:
        """列出所有可用后端. 返回 [(index, display_name, is_active), ...]."""
        result: List[tuple] = []
        backends = self._agent.list_backends()
        for i, (name, model, active) in enumerate(backends):
            result.append((i, f"{name}/{model}", active))
        return result

    def next_llm(self, n: int = -1) -> None:
        """切换到下一个 (或指定) LLM 后端.

        Args:
            n: 目标后端索引, -1 表示顺序切换到下一个.
        """
        backends = self._agent.list_backends()
        if not backends:
            return
        active_idx = next((i for i, (_, _, a) in enumerate(backends) if a), 0)
        if n < 0:
            n = (active_idx + 1) % len(backends)
        else:
            n = n % len(backends)
        target_name = backends[n][0]
        self._agent.switch_backend(target_name)

    def get_llm_name(self) -> str:
        """返回当前活跃 LLM 的 display 名称."""
        backends = self._agent.list_backends()
        for name, model, active in backends:
            if active:
                return f"{name}/{model}"
        return "unknown"

    def list_backends(self):
        """列出所有后端 (兼容前端 stapp)."""
        return self._agent.list_backends()

    def switch_backend(self, name: str) -> None:
        """切换后端 (兼容前端 stapp)."""
        self._agent.switch_backend(name)

    def get_model_name(self) -> str:
        """返回当前模型名称 (兼容前端 stapp)."""
        try:
            return self._agent.client.name
        except AttributeError:
            return "unknown"

    def taskloop(self, prompt: str):
        """执行任务并逐块 yield 结果 (兼容前端 stapp 的 generator 迭代模式)."""
        return self._agent.run(prompt)

    # —— 任务接口 (兼容 GenericAgent.put_task) ——

    def put_task(
        self,
        query: str,
        source: str = "user",
        images: Optional[list] = None,
    ) -> queue.Queue:
        """提交任务到 ZeroAgent, 返回流式输出队列.

        非阻塞: 任务在后台线程中执行, 结果通过返回的 queue.Queue 推送.

        Queue 消息格式:
            {"next": str, "source": str, "turn": int}  — 增量输出
            {"done": str, "source": str, "turn": int}   — 任务完成, 完整输出

        Args:
            query: 用户输入 / 任务描述.
            source: 来源标识 (e.g. "telegram", "user", "subagent:xxx").
            images: 图片路径列表 (预留, 当前未实现).

        Returns:
            queue.Queue — 消费者从中读取流式输出.
        """
        display_queue: queue.Queue = queue.Queue()
        self._task_queue.put({
            "query": query,
            "source": source,
            "images": images or [],
            "output": display_queue,
        })
        self._ensure_worker()
        return display_queue

    def abort(self) -> None:
        """中止当前任务."""
        self._stop_sig = True
        self._agent.abort()

    # —— 生命周期 ——

    def start(self) -> None:
        """启动后台 worker 线程."""
        self._ensure_worker()

    def stop(self) -> None:
        """停止后台 worker 并等待线程退出."""
        self._stop_sig = True
        self.abort()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)

    # —— 内部 ——

    def _ensure_worker(self) -> None:
        """确保 worker 线程已启动."""
        with self._lock:
            if self._worker_thread and self._worker_thread.is_alive():
                return
            self._stop_sig = False
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="za-agent-runner",
                daemon=True,
            )
            self._worker_thread.start()

    def _worker_loop(self) -> None:
        """主循环: 从 task_queue 取任务, 依次串行执行."""
        while not self._stop_sig:
            try:
                task = self._task_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            query = task["query"]
            source = task["source"]
            display_queue = task["output"]

            with self._lock:
                self._running = True
            self._stop_sig = False

            full_resp = ""
            curr_turn = 0
            try:
                gen = self._agent.run(query)
                for chunk in gen:
                    if self._stop_sig:
                        break
                    if isinstance(chunk, dict) and "turn" in chunk:
                        curr_turn = chunk["turn"]
                        continue
                    chunk_str = str(chunk)
                    full_resp += chunk_str
                    display_queue.put({
                        "next": full_resp,
                        "source": source,
                        "turn": curr_turn,
                    })
                display_queue.put({
                    "done": full_resp,
                    "source": source,
                    "turn": curr_turn,
                })
            except Exception as exc:
                display_queue.put({
                    "done": f"{full_resp}\n```\n{exc}\n```",
                    "source": source,
                    "turn": curr_turn,
                })
            finally:
                with self._lock:
                    self._running = False
                self._stop_sig = False
                self._task_queue.task_done()
