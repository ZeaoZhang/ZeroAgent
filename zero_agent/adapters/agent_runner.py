"""AgentRunner — ZeroAgent UI adapter.

Wraps ZeroAgent's generator-based run() behind a queue-based task API and a
small frontend-facing contract for model profiles, cancellation, logs, config,
and history helpers. Legacy GenericAgent-style aliases remain only for callers
that have not yet migrated.
"""

from __future__ import annotations

import copy
import queue
import os
import threading
from typing import Any, Dict, List, Optional

from zero_agent.core.agent import ZeroAgent


_FORWARDED_RUNTIME_ATTRS = {
    "task_dir",
}


class _BackendCompat:
    """GenericAgent-style backend facade for frontend code."""

    def __init__(self, session: Any) -> None:
        self._session = session

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    @property
    def name(self) -> str:
        return getattr(self._session, "name", None) or getattr(
            getattr(self._session, "config", None), "name", "unknown"
        )

    @property
    def model(self) -> str:
        return getattr(getattr(self._session, "config", None), "model", "")

    @property
    def history(self) -> list:
        return getattr(self._session, "history", [])

    @history.setter
    def history(self, value: list) -> None:
        setattr(self._session, "history", value)

    @property
    def raw_msgs(self) -> list:
        return getattr(self._session, "raw_msgs", [])

    @raw_msgs.setter
    def raw_msgs(self, value: list) -> None:
        setattr(self._session, "raw_msgs", value)

    def ask(self, prompt: str):
        """GenericAgent health-check compatible ask() wrapper."""
        if hasattr(self._session, "ask"):
            return self._session.ask(prompt)

        old_history = copy.deepcopy(getattr(self._session, "history", []))
        try:
            gen = self._session.chat([{"role": "user", "content": prompt}], tools=None)
            return "".join(str(chunk) for chunk in gen if isinstance(chunk, str))
        finally:
            if hasattr(self._session, "history"):
                self._session.history = old_history


class _LLMClientCompat:
    """GenericAgent-style ToolClient facade for frontend code."""

    def __init__(self, session: Any) -> None:
        self._session = session
        self.backend = _BackendCompat(session)
        self.log_path = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    @property
    def name(self) -> str:
        return self.backend.name

    @property
    def history(self) -> list:
        return self.backend.history

    @history.setter
    def history(self, value: list) -> None:
        self.backend.history = value

    @property
    def last_tools(self) -> str:
        return getattr(self._session, "last_tools", "")

    @last_tools.setter
    def last_tools(self, value: str) -> None:
        setattr(self._session, "last_tools", value)


class AgentRunner:
    """在后台线程中运行 ZeroAgent, 提供 queue 风格的任务接口.

    Frontends should use this adapter instead of touching ZeroAgent internals.
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
        self.inc_out = False
        self.no_print = False
        self._configure_shared_session_commands()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._agent, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if (
            name in _FORWARDED_RUNTIME_ATTRS
            or (
                name.startswith("_")
                and name not in {
                    "_agent",
                    "_task_queue",
                    "_lock",
                    "_running",
                    "_stop_sig",
                    "_worker_thread",
                    "_handle_slash_cmd",
                }
            )
        ):
            agent = self.__dict__.get("_agent")
            if agent is not None:
                setattr(agent, name, value)
                return
        object.__setattr__(self, name, value)

    # —— 兼容 GenericAgent 的公开属性 ——

    @property
    def za(self):
        """返回底层 ZeroAgent 实例 (兼容前端直接访问)."""
        return self._agent

    @property
    def config(self):
        """返回底层 ZeroAgent 配置 (兼容共享命令)."""
        return self._agent.config

    @property
    def log_path(self) -> str | None:
        """当前进程的 model_responses 日志路径 (兼容 /export)."""
        sessions_dir = getattr(self._agent.config, "sessions_dir", None)
        if not sessions_dir:
            return None
        return os.path.join(
            os.path.abspath(sessions_dir),
            f"model_responses_{os.getpid()}.txt",
        )

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def handler(self):
        return self._agent.handler

    @property
    def task_queue(self):
        """GenericAgent-compatible queue handle used by older frontends."""
        return self._task_queue

    @property
    def history(self):
        try:
            return self._agent.client.history
        except AttributeError:
            return []

    @property
    def llmclients(self) -> List[_LLMClientCompat]:
        return [
            _LLMClientCompat(session)
            for session in getattr(self._agent, "_sessions", {}).values()
        ]

    def history_snapshot(self) -> list:
        """Return a deep-copy snapshot of the active LLM history."""
        return copy.deepcopy(list(self.history or []))

    def replace_history(self, history: list | None) -> None:
        """Replace the active LLM history without exposing client internals."""
        target = list(history or [])
        client = getattr(self._agent, "client", None)
        if client is not None and hasattr(client, "history"):
            client.history = copy.deepcopy(target)

    def clear_history(self) -> None:
        """Clear active conversation history."""
        self.replace_history([])

    def append_history_entries(self, entries: list) -> None:
        """Append history entries to the active LLM history."""
        client = getattr(self._agent, "client", None)
        if client is not None and hasattr(client, "history"):
            client.history.extend(copy.deepcopy(list(entries or [])))

    def clear_last_tools(self) -> None:
        """Clear the active client's last_tools marker when the backend has one."""
        client = getattr(self._agent, "client", None)
        if client is not None and hasattr(client, "last_tools"):
            client.last_tools = ""

    def config_snapshot(self):
        """Return a deep-copy snapshot of the ZeroAgent config."""
        return copy.deepcopy(self._agent.config)

    def set_runtime_attr(self, name: str, value: Any) -> None:
        """Set a runtime-only attribute on the wrapped ZeroAgent."""
        setattr(self._agent, name, value)

    def set_turn_end_hook(self, name: str, hook) -> None:
        """Install or replace a turn-end hook on the wrapped ZeroAgent."""
        if not hasattr(self._agent, "_turn_end_hooks"):
            self._agent._turn_end_hooks = {}
        self._agent._turn_end_hooks[name] = hook

    @property
    def llm_no(self) -> int:
        """当前活跃 LLM 的索引 (兼容 GenericAgent)."""
        backends = self._agent.list_backends()
        for i, (_, _, active) in enumerate(backends):
            if active:
                return i
        return 0

    @llm_no.setter
    def llm_no(self, value: int) -> None:
        """Switch active LLM by index for legacy frontends that assign llm_no."""
        self.next_llm(int(value))

    @property
    def verbose(self) -> bool:
        return self._agent.config.verbose

    @verbose.setter
    def verbose(self, value: bool) -> None:
        self._agent.config.verbose = value

    @property
    def llmclient(self):
        """兼容 GenericAgent 的 llmclient 检查 (非 None 表示已配置)."""
        client = getattr(self._agent, "client", None)
        return _LLMClientCompat(client) if client is not None else None

    @llmclient.setter
    def llmclient(self, value) -> None:
        session = getattr(value, "_session", value)
        for name, candidate in getattr(self._agent, "_sessions", {}).items():
            if candidate is session:
                self._agent.switch_backend(name)
                return
        self._agent.client = session

    def _configure_shared_session_commands(self) -> None:
        """让 /continue 等共享命令使用当前配置里的 sessions_dir."""
        sessions_dir = getattr(self._agent.config, "sessions_dir", None)
        if not sessions_dir:
            return
        try:
            from zero_agent.bots.shared.continue_cmd import set_sessions_dir
            set_sessions_dir(os.path.abspath(sessions_dir))
        except Exception:
            pass

    # —— LLM 管理 (兼容 GenericAgent bot /list_llms + /llm n) ——

    def list_llm_profiles(self) -> List[Dict[str, Any]]:
        """列出所有可用后端, 返回前端可消费的 profile DTO."""
        profiles: List[Dict[str, Any]] = []
        for index, (name, model, active) in enumerate(self._agent.list_backends()):
            display_name = f"{name}/{model}" if model else str(name)
            profiles.append({
                "index": index,
                "llmNo": index,
                "id": name,
                "name": name,
                "model": model,
                "displayName": display_name,
                "active": active,
            })
        return profiles

    def list_llms(self) -> List[tuple]:
        """Legacy tuple alias: [(index, display_name, is_active), ...]."""
        return [
            (profile["index"], profile["displayName"], profile["active"])
            for profile in self.list_llm_profiles()
        ]

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

    def switch_llm(self, index_or_id: int | str) -> None:
        """Switch backend by index, numeric string, backend id, or backend name."""
        backends = self._agent.list_backends()
        if not backends:
            raise ValueError("没有可用的 LLM 后端")

        if isinstance(index_or_id, int):
            self.next_llm(index_or_id)
            return

        value = str(index_or_id).strip()
        if value.lstrip("-").isdigit():
            self.next_llm(int(value))
            return

        for name, _, _ in backends:
            if name == value:
                self._agent.switch_backend(name)
                return

        available = ", ".join(name for name, _, _ in backends)
        raise ValueError(f"后端 '{value}' 不存在。可用后端: {available}")

    def check_llm_health(self, index_or_id: int | str, prompt: str = "你好") -> bool:
        """Check whether a backend can answer a small prompt without switching UI state."""
        backends = self._agent.list_backends()
        if not backends:
            return False

        target_name: str | None = None
        if isinstance(index_or_id, int):
            if not 0 <= index_or_id < len(backends):
                return False
            target_name = backends[index_or_id][0]
        else:
            value = str(index_or_id).strip()
            if value.lstrip("-").isdigit():
                idx = int(value)
                if not 0 <= idx < len(backends):
                    return False
                target_name = backends[idx][0]
            else:
                for name, _, _ in backends:
                    if name == value:
                        target_name = name
                        break
        if not target_name:
            return False

        session = getattr(self._agent, "_sessions", {}).get(target_name)
        if session is None:
            return False

        backend = _BackendCompat(session)
        try:
            reply = backend.ask(prompt)
            if hasattr(reply, "__iter__") and not isinstance(reply, str):
                reply = "".join(str(block) for block in reply if isinstance(block, str))
            text = str(reply).strip() if reply else ""
            ok = bool(text) and not text.startswith("Error") and not text.startswith("[")
        except Exception:
            ok = False

        if hasattr(backend, "raw_msgs") and backend.raw_msgs:
            backend.raw_msgs = [
                msg for msg in backend.raw_msgs
                if msg.get("prompt") != prompt
            ]
        return ok

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

    def _handle_slash_cmd(self, raw_query: str, display_queue: queue.Queue):
        """GenericAgent-style slash-command hook for legacy frontend patches.

        Legacy command modules monkey-patch this method and either return a
        replacement prompt, return ``None`` after pushing a system response, or
        fall through to the original implementation. The base implementation
        simply says "not handled" by returning the original query.
        """
        return raw_query

    def run(self, user_input: Optional[str] = None, *args, **kwargs):
        """GenericAgent-compatible run entrypoint.

        GA frontends start ``agent.run`` in a background thread, then submit
        work through ``put_task``. ZeroAgent's native ``run`` executes one
        prompt, so no-arg calls become the queue worker loop while prompted
        calls delegate to the wrapped ZeroAgent.
        """
        if user_input is not None:
            return self._agent.run(user_input, *args, **kwargs)

        current = threading.current_thread()
        with self._lock:
            existing = self._worker_thread
            if existing and existing.is_alive() and existing is not current:
                return None
            self._worker_thread = current
            self._stop_sig = False
        self._worker_loop()
        return None

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

            if task == "EXIT":
                self._task_queue.task_done()
                break

            query = task["query"]
            source = task["source"]
            display_queue = task["output"]

            if isinstance(query, str) and query.strip().startswith("/"):
                handled = self._handle_slash_cmd(query, display_queue)
                if handled is None:
                    self._task_queue.task_done()
                    continue
                query = handled

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
                        "next": chunk_str if self.inc_out else full_resp,
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
