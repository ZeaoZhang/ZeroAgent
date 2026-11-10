"""ZeroAgent — 顶层 agent 编排器.

管理组件生命周期（配置 → 工具 → LLM → handler → loop），
提供单任务执行的统一入口。由 CLI / Web Server 等 runner 驱动.
"""

from __future__ import annotations

import copy
import os
import re
import time
from dataclasses import dataclass
from importlib import resources
from typing import Any, Dict, Generator, Iterable, List, Optional

from zero_agent.core.config import AgentConfig, load_default_config
from zero_agent.core.exceptions import ConfigError
from zero_agent.core.handler import BaseHandler
from zero_agent.core.hooks import HookSystem
from zero_agent.core.localization import (
    PROMPT_PEER_HINT,
    PROMPT_REPLY_LANGUAGE,
    PROMPT_TODAY_LABEL,
    PROMPT_CAPABILITY_FILE_DELIVERY,
    PROMPT_FILE_DELIVERY,
    WEEKDAY_MESSAGE_IDS,
    PromptLocalizer,
)
from zero_agent.core.loop import AgentLoop

from zero_agent.core.types import (
    EvidenceLedger,
    PendingTaskState,
    TaskContract,
    TaskMode,
    TerminalEvent,
    TerminalStatus,
)
from zero_agent.memory.manager import MemoryManager
from zero_agent.tools.registry import ToolRegistry



class _LLMFactoryProxy:
    """Lazy proxy so CLI config is loaded before importing LiteLLM."""

    def create_all_sessions(
        self,
        config: AgentConfig,
        session_log_path: str | None = None,
        *,
        tracer: Any | None = None,
    ):
        from zero_agent.llm.factory import LLMFactory as RealLLMFactory

        kwargs: dict[str, Any] = {}
        if tracer is not None:
            kwargs["tracer"] = tracer
        return RealLLMFactory.create_all_sessions(
            config,
            session_log_path=session_log_path,
            **kwargs,
        )



@dataclass(frozen=True)
class PromptBlock:
    """One ordered system-prompt block."""

    name: str
    content: str
    dynamic: bool = False


@dataclass(frozen=True)
class PromptAssembly:
    """Ordered prompt blocks with explicit dynamic-state metadata."""

    blocks: tuple[PromptBlock, ...]

    def render(self) -> str:
        """Render all blocks without changing their authored boundaries."""
        return "".join(block.content for block in self.blocks)

    @property
    def names(self) -> tuple[str, ...]:
        """Return block names in render order."""
        return tuple(block.name for block in self.blocks)

    @property
    def dynamic_names(self) -> tuple[str, ...]:
        """Return dynamic block names in render order."""
        return tuple(block.name for block in self.blocks if block.dynamic)

LLMFactory = _LLMFactoryProxy()

_USAGE_COUNTER_ATTRS = (
    "_total_input_tokens",
    "_total_output_tokens",
    "_total_cache_read_tokens",
    "_total_cache_creation_tokens",
    "_total_cache_miss_tokens",
    "_total_cached_tokens",
    "_total_requests",
    "_cache_metrics_available",
)

_RUNTIME_CONFIG_FIELDS = (
    "max_turns",
    "workspace_dir",
    "memory_dir",
    "sessions_dir",
    "verbose",
    "language",
    "incremental_output",
    "peer_hint",
    "enable_worldline",
)

_PROMPT_CAPABILITY_MESSAGE_IDS = {
    PROMPT_CAPABILITY_FILE_DELIVERY: PROMPT_FILE_DELIVERY,
}


def _client_signature(client: Any) -> tuple[Any, Any, Any, Any]:
    """Return the fields that decide whether session-local caches are reusable."""

    config = getattr(client, "config", None)
    return (
        getattr(config, "provider", None),
        getattr(config, "model", None),
        getattr(config, "tool_protocol", "native"),
        getattr(config, "api_mode", "chat_completions") or "chat_completions",
    )


def _iter_session_layers(client: Any):
    """Yield wrappers and concrete sessions for cache reset without allocations."""

    seen: set[int] = set()
    stack = [client]
    while stack:
        current = stack.pop()
        ident = id(current)
        if ident in seen:
            continue
        seen.add(ident)
        yield current
        backend = getattr(current, "backend", None)
        if backend is not None:
            stack.append(backend)
        primary = getattr(current, "primary", None)
        if primary is not None:
            stack.append(primary)
        backups = getattr(current, "backups", None) or []
        stack.extend(backups)


def _active_usage_owner(client: Any) -> Any:
    """Return the concrete active session that owns usage counters."""

    current = client
    seen: set[int] = set()
    while id(current) not in seen:
        seen.add(id(current))
        active = getattr(current, "_active", None)
        if active is not None:
            current = active
            continue
        backend = getattr(current, "backend", None)
        if backend is not None:
            current = backend
            continue
        break
    return current


def _copy_tool_protocol_cache(old_client: Any, new_client: Any) -> None:
    """Copy protocol cache markers from old to new compatible sessions."""

    for attr in ("last_tools", "_last_tools_json", "total_cd_tokens"):
        if not hasattr(old_client, attr):
            continue
        try:
            setattr(new_client, attr, copy.deepcopy(getattr(old_client, attr)))
        except Exception:
            pass

    old_owner = _active_usage_owner(old_client)
    new_owner = _active_usage_owner(new_client)
    if old_owner is old_client and new_owner is new_client:
        return
    for attr in ("last_tools", "_last_tools_json", "total_cd_tokens"):
        if not hasattr(old_owner, attr):
            continue
        try:
            setattr(new_owner, attr, copy.deepcopy(getattr(old_owner, attr)))
        except Exception:
            pass


def _reset_tool_protocol_cache(client: Any) -> None:
    """Clear protocol cache markers for a session or wrapper stack."""

    for layer in _iter_session_layers(client):
        reset = getattr(layer, "reset_tool_protocol_cache", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                pass
        for attr in ("last_tools", "_last_tools_json", "total_cd_tokens"):
            if hasattr(layer, attr):
                try:
                    setattr(layer, attr, "" if attr != "total_cd_tokens" else 0)
                except Exception:
                    pass


def _copy_usage_counters(old_client: Any, new_client: Any) -> None:
    """Copy token usage counters between concrete active sessions."""
    old_owner = _active_usage_owner(old_client)
    new_owner = _active_usage_owner(new_client)
    for attr in _USAGE_COUNTER_ATTRS:
        if hasattr(new_owner, attr):
            setattr(new_owner, attr, getattr(old_owner, attr, False if attr == "_cache_metrics_available" else 0))


def _zero_usage_counters(client: Any) -> None:
    """Reset token usage counters on every concrete session in a wrapper stack."""
    for layer in _iter_session_layers(client):
        for attr in _USAGE_COUNTER_ATTRS:
            if hasattr(layer, attr):
                setattr(layer, attr, False if attr == "_cache_metrics_available" else 0)


def _migrate_client_state(old_client: Any, new_client: Any, *, preserve_usage: bool) -> None:
    """Move reusable conversation state from one LLM client to another.

    History and system prompt always move. Protocol cache and usage counters are
    reusable only when the provider/model/tool protocol/API mode are identical.
    """

    try:
        history = copy.deepcopy(getattr(old_client, "history"))
    except Exception:
        history = []
    try:
        new_client.history = history
    except Exception:
        pass
    try:
        new_client._completed_task_pairs_cache = copy.deepcopy(
            getattr(old_client, "_completed_task_pairs_cache", []),
        )
    except Exception:
        pass
    try:
        new_client.system = getattr(old_client, "system", "")
    except Exception:
        pass

    compatible = _client_signature(old_client) == _client_signature(new_client)
    if compatible:
        _copy_tool_protocol_cache(old_client, new_client)
    else:
        _reset_tool_protocol_cache(new_client)

    if preserve_usage and compatible:
        _copy_usage_counters(old_client, new_client)
    else:
        _zero_usage_counters(new_client)


class ZeroAgent:
    """顶层 agent 编排器.

    负责创建和连接所有组件，提供 run() 入口执行单次任务。
    所有状态集中在实例内部，零模块级全局变量.

    Attributes:
        config: Agent 配置.
        registry: 工具注册中心.
        client: 当前活跃的 LLM 会话.
        _sessions: 所有已创建的 LLM 会话字典（name → session），用于运行时切换.
        handler: 工具分发器.
        memory: 记忆系统管理器.
        task_dir: 当前任务目录（用于文件注入干预）.
        _turn_end_hooks: turn 结束回调钩子字典.
    """

    def __init__(
        self,
        config: Optional[AgentConfig] = None,
        handler: Optional[BaseHandler] = None,
        registry: Optional[ToolRegistry] = None,
        hooks: Optional[HookSystem] = None,
        session_log_path: str | None = None,
    ) -> None:
        """初始化 ZeroAgent.

        Args:
            config: Agent 配置，None 时从项目 config.yaml 或环境变量构建.
            handler: 自定义工具分发器，None 时创建 BaseHandler.
            registry: 自定义工具注册中心，None 时自动加载内置工具.
            hooks: 自定义 HookSystem，None 时创建默认 HookSystem.
            session_log_path: Optional explicit response-log path.
        """
        self.config = config or load_default_config()
        self._session_log_path = session_log_path
        self._response_log_retired = False
        self.hooks = hooks or HookSystem()
        from zero_agent.plugins.langfuse_tracing import LangfuseTracer

        try:
            self._langfuse_tracer = LangfuseTracer.from_config(self.config)
        except Exception:
            self._langfuse_tracer = LangfuseTracer()
        self._register_builtin_plugins(self._langfuse_tracer)

        # 1. 工具注册中心
        self._owns_registry = registry is None
        if self._owns_registry:
            schema_backend = self.config.default_backend or next(
                iter(self.config.llm_backends),
                "",
            )
            self.registry = ToolRegistry.with_builtins(
                self.config,
                language=self.config.resolved_tool_language_for_backend(
                    schema_backend,
                ),
            )
        else:
            self.registry = registry

        session_kwargs: dict[str, Any] = {}
        if self._session_log_path is not None:
            session_kwargs["session_log_path"] = self._session_log_path
        if getattr(self._langfuse_tracer, "enabled", False) or getattr(
            self.config,
            "langfuse",
            None,
        ):
            session_kwargs["tracer"] = self._langfuse_tracer
        self._sessions = LLMFactory.create_all_sessions(
            self.config,
            **session_kwargs,
        )
        default_name = self.config.default_backend
        self.client = self._sessions.get(default_name)
        if self.client is None:
            self.client = next(iter(self._sessions.values()))

        # 3. 工具分发器
        self.handler = handler or BaseHandler(
            registry=self.registry,
            cwd=self.config.workspace_dir,
        )
        self._wire_handler(self.handler)
        handler_registry = getattr(self.handler, "registry", None)
        if (
            handler is not None
            and handler_registry is not None
            and handler_registry is not self.registry
        ):
            self._owns_registry = False
            self.registry = handler_registry

        # 4. 记忆管理器
        self.memory = MemoryManager(
            memory_dir=self.config.memory_dir,
            workspace_dir=self.config.workspace_dir,
            language=self.config.resolved_language,
        )

        # 5. 运行时状态
        self.task_dir: Optional[str] = None
        self._turn_end_hooks: Dict[str, Any] = {}
        self.loop: Optional[AgentLoop] = None
        self._is_running_task = False
        self.pending_runtime_config: Optional[AgentConfig] = None
        self._config_path: Optional[str] = getattr(self.config, "_source_path", None)
        self._pending_task_state: Optional[PendingTaskState] = None

    def set_config_path(self, path: Optional[str]) -> None:
        """设置配置文件的路径，用于热重载检测."""
        self._config_path = str(path) if path is not None else None

    def close_response_log(self) -> None:
        """Permanently retire response logging across future config reloads."""
        self._response_log_retired = True
        self._session_log_path = None
        for client in self._sessions.values():
            close = getattr(client, "close_response_log", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def reload_config(self) -> bool:
        """若配置文件已变更则原子热重载配置并重建 LLM session.

        检测 YAML 文件 mtime，变更时先在局部变量中解析配置并构建新
        sessions；任一步失败都会保留旧 config/sessions/client/handler/mtime。

        Returns:
            True 表示配置已重载，False 表示无变更或重载失败.
        """
        if not self._config_path:
            return False

        from zero_agent.core.config import _commit_config_mtime, reload_config_if_changed

        try:
            new_config = reload_config_if_changed(self._config_path)
        except Exception as exc:
            import logging
            logging.getLogger("zero_agent").warning(
                "reload_config: 解析配置失败，将保留旧配置: %s", exc
            )
            return False
        if new_config is None:
            return False

        old_config = self.config
        old_sessions = self._sessions
        old_client = self.client
        old_handler = self.handler
        old_registry = self.registry
        old_memory = self.memory
        old_pending_runtime_config = self.pending_runtime_config
        old_config_path = self._config_path
        old_handler_client = getattr(old_handler, "client", None)
        old_handler_registry = getattr(old_handler, "registry", None)
        old_handler_cwd = getattr(old_handler, "cwd", None)
        old_active_name = self._get_active_backend_name()

        try:
            session_kwargs: dict[str, Any] = {}
            if self._session_log_path is not None:
                session_kwargs["session_log_path"] = self._session_log_path
            if getattr(self._langfuse_tracer, "enabled", False) or getattr(
                new_config,
                "langfuse",
                None,
            ):
                session_kwargs["tracer"] = self._langfuse_tracer
            new_sessions = LLMFactory.create_all_sessions(
                new_config,
                **session_kwargs,
            )
            if self._response_log_retired:
                for session in new_sessions.values():
                    close = getattr(session, "close_response_log", None)
                    if callable(close):
                        close()
            target_name = self._select_reload_backend(old_active_name, new_config, new_sessions)
            new_client = new_sessions[target_name]
            _migrate_client_state(old_client, new_client, preserve_usage=True)
            runtime_changed = self._runtime_config_changed(old_config, new_config)
            new_registry = old_registry
            registry_changed = False
            if self._owns_registry and not runtime_changed:
                old_tool_language = (
                    old_config.resolved_tool_language
                    if old_active_name == "unknown"
                    else old_config.resolved_tool_language_for_backend(old_active_name)
                )
                new_tool_language = new_config.resolved_tool_language_for_backend(
                    target_name,
                )
                if old_tool_language != new_tool_language:
                    new_registry = ToolRegistry.with_builtins(
                        new_config,
                        language=new_tool_language,
                    )
                    registry_changed = True
        except Exception as exc:
            import logging
            logging.getLogger("zero_agent").warning(
                "reload_config: 重建运行时失败，将保留旧会话: %s", exc
            )
            return False

        try:
            self.config = new_config
            self._sessions = new_sessions
            self.client = new_client
            self._config_path = getattr(new_config, "_source_path", self._config_path)
            self.registry = new_registry

            if runtime_changed:
                self.pending_runtime_config = new_config

            self.handler = old_handler
            self.handler.client = self.client
            if registry_changed:
                self.handler.registry = self.registry
                if self.loop is not None:
                    self.loop.tools_schema = self.registry.generate_openai_schema()
        except Exception as exc:
            self.config = old_config
            self._sessions = old_sessions
            self.client = old_client
            self.handler = old_handler
            self.registry = old_registry
            self.memory = old_memory
            self.pending_runtime_config = old_pending_runtime_config
            self._config_path = old_config_path
            try:
                self.handler.client = old_handler_client
                self.handler.registry = old_handler_registry
                self.handler.cwd = old_handler_cwd
            except Exception:
                pass
            import logging
            logging.getLogger("zero_agent").warning(
                "reload_config: 提交运行时失败，将保留旧状态: %s", exc
            )
            return False

        _commit_config_mtime(
            self._config_path,
            mtime=getattr(new_config, "_source_mtime_ns", None),
        )

        try:
            self._langfuse_tracer.reconfigure(new_config)
            self._register_builtin_plugins(self._langfuse_tracer)
        except Exception:
            import logging

            logging.getLogger("zero_agent").warning(
                "reload_config: Langfuse reconfiguration failed",
                exc_info=True,
            )

        import logging
        logging.getLogger("zero_agent").info(
            "Config reloaded from %s, active backend: %s",
            self._config_path,
            target_name,
        )
        return True

    def run(
        self,
        user_input: str,
        system_prompt: Optional[str] = None,
        initial_user_content: Optional[str] = None,
        *,
        initial_mode: TaskMode = TaskMode.OPEN,
        plan_path: Optional[str] = None,
        prompt_capabilities: Iterable[str] = (),
    ) -> Generator[Any, None, TerminalEvent]:
        """执行单次 agent 任务.

        创建 AgentLoop 并驱动执行，每次 yield 返回状态信息供 UI 消费.
        这是 ZeroAgent 的主入口，runner 层通过此方法驱动 agent.

        Args:
            user_input: 用户输入（任务描述）.
            system_prompt: 系统提示词，None 时使用默认构建.
            initial_user_content: 可选的首条 user message 内容.
            initial_mode: 新任务的初始 TaskMode，默认 OPEN.
            plan_path: PLAN/EXECUTING 任务必须提供的 plan 文件路径.
            prompt_capabilities: 本次任务运行环境支持的能力提示块.

        Yields:
            str → 状态文本，供 UI 实时展示.
            dict → 结构化信息（如 {"turn": 1}）.

        Returns:
            TerminalEvent describing the task terminal state.

        Raises:
            RuntimeError: 当前有任务正在运行（不支持并发）.
            ValueError: 无 pending task 时 PLAN/EXECUTING 未提供 plan_path.
        """
        self._apply_pending_runtime_config()
        pending_state = self._pending_task_state
        if pending_state is not None:
            self.clear_pending_task()
        else:
            prepare_history = getattr(self.client, "prepare_for_new_task", None)
            if callable(prepare_history):
                prepare_history()

        if (
            pending_state is None
            and initial_mode in (TaskMode.PLAN, TaskMode.EXECUTING)
            and not plan_path
        ):
            raise ValueError(
                f"TaskMode.{initial_mode.value} requires a non-empty plan_path"
            )

        # 创建工作目录和记忆目录
        os.makedirs(self.config.workspace_dir, exist_ok=True)
        self.memory.init_memory()

        # 每个任务创建新 handler；等待用户的任务恢复同一契约和证据账本。
        self.handler = self._new_task_handler()
        self.handler.reset_code_stop_signal()
        effective_initial_content = initial_user_content
        if pending_state is None:
            self.handler.task_contract = TaskContract(
                task_id=f"task-{time.time_ns()}",
                user_request=user_input,
                mode=initial_mode,
                plan_path=plan_path,
            )
            self.handler.evidence_ledger = EvidenceLedger()
            self.handler.plan_verify_status = "missing"
        else:
            self.handler.task_contract = copy.deepcopy(pending_state.contract)
            self.handler.evidence_ledger = copy.deepcopy(pending_state.ledger)
            self.handler.plan_verify_status = pending_state.plan_verify_status
            if self.handler.task_contract.mode is TaskMode.PLAN:
                self.handler.max_turns = 120
            effective_initial_content = self._pending_continuation_content(
                pending_state,
                user_input,
            )

        task_prompt_capabilities = tuple(dict.fromkeys(prompt_capabilities))

        # None selects the state-aware default; an empty string is a valid
        # explicit replacement and must not fall back to the default prompt.
        if system_prompt is None:
            prompt = self._build_system_prompt(
                prompt_capabilities=task_prompt_capabilities,
            )

            def build_task_prompt() -> str:
                return self._build_system_prompt(
                    prompt_capabilities=task_prompt_capabilities,
                )

            prompt_factory = build_task_prompt
        else:
            prompt = system_prompt
            if task_prompt_capabilities:
                prompt += self._build_prompt_capabilities(
                    task_prompt_capabilities,
                ).render()
            prompt_factory = None

        # 创建 AgentLoop
        tools_schema = self.registry.generate_openai_schema()
        loop = AgentLoop(
            client=self.client,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=(120 if self.handler.task_contract.mode is TaskMode.PLAN else self.config.max_turns),
            verbose=self.config.verbose,
            hooks=self.hooks,
            agent=self,
        )
        self.loop = loop

        self._is_running_task = True
        try:
            terminal = yield from loop.run(
                system_prompt=prompt,
                user_input=user_input,
                initial_user_content=effective_initial_content,
                system_prompt_factory=prompt_factory,
            )
            self._update_pending_task_state(terminal)
            if terminal.status is TerminalStatus.COMPLETED:
                finish_history = getattr(self.client, "finish_task", None)
                if callable(finish_history):
                    finish_history(user_input, terminal.text)
            return terminal
        finally:
            self._is_running_task = False


    def clear_pending_task(self) -> None:
        """Discard a task paused for user input."""

        self._pending_task_state = None

    @staticmethod
    def _is_partial_acceptance(answer: str) -> bool:
        normalized = " ".join(str(answer or "").strip().split())
        return normalized in {
            "accept_partial",
            "接受 partial",
            "接受 PARTIAL 并完成",
        }

    def _pending_continuation_content(
        self,
        pending: PendingTaskState,
        answer: str,
    ) -> str:
        """Build the continuation message without changing the original objective."""

        if pending.waiting_kind == "plan_partial_acceptance":
            if self._is_partial_acceptance(answer):
                self.handler.plan_verify_status = "partial_accepted"
                return (
                    "[System] The user explicitly accepted the PARTIAL verification. "
                    "Re-evaluate completion now using verify_status=partial_accepted."
                )
            self.handler.plan_verify_status = "missing"
            return (
                "[System] The user did not accept PARTIAL completion. Continue fixing "
                f"the original plan. User reply: {answer}"
            )
        return answer

    def _update_pending_task_state(self, terminal: TerminalEvent) -> None:
        """Persist only waiting tasks; every other terminal closes the task."""

        if terminal.status is not TerminalStatus.WAITING:
            self.clear_pending_task()
            return
        waiting_data = copy.deepcopy(terminal.data) if isinstance(terminal.data, dict) else {}
        payload = waiting_data.get("data") if isinstance(waiting_data.get("data"), dict) else waiting_data
        candidates = payload.get("candidates", []) if isinstance(payload, dict) else []
        partial_candidates = {"接受 PARTIAL 并完成", "继续修复"}
        waiting_kind = (
            "plan_partial_acceptance"
            if self.handler.task_contract.mode is TaskMode.PLAN
            and set(candidates or []) == partial_candidates
            else "ask_user"
        )
        self._pending_task_state = PendingTaskState(
            contract=copy.deepcopy(self.handler.task_contract),
            ledger=copy.deepcopy(self.handler.evidence_ledger),
            plan_verify_status=self.handler.plan_verify_status,
            waiting_kind=waiting_kind,
            waiting_data=waiting_data,
        )

    def _wire_handler(self, handler: BaseHandler) -> BaseHandler:
        """Attach runtime references shared by freshly-created handlers."""
        handler.parent = self
        try:
            handler.client = self.client
        except Exception:
            pass
        return handler

    def _new_task_handler(self) -> BaseHandler:
        """Create a per-task handler and carry working memory."""
        old_handler = self.handler
        cls = type(old_handler) if old_handler is not None else BaseHandler
        try:
            handler = cls(registry=self.registry, cwd=self.config.workspace_dir)
        except TypeError:
            handler = BaseHandler(
                registry=self.registry,
                cwd=self.config.workspace_dir,
            )
        handler = self._wire_handler(handler)

        if old_handler is None:
            return handler

        old_working = getattr(old_handler, "working", {})
        if "key_info" in old_working:
            key_info = re.sub(
                r"\n\[SYSTEM\] 此为.*?工作记忆[。\n]*",
                "",
                old_working.get("key_info", ""),
            )
            handler.working["key_info"] = key_info
            passed_sessions = old_working.get("passed_sessions", 0) + 1
            handler.working["passed_sessions"] = passed_sessions
            if passed_sessions > 0:
                handler.working["key_info"] += (
                    f"\n[SYSTEM] 此为 {passed_sessions} 个对话前设置的key_info，"
                    "若已在新任务，先更新或清除工作记忆。\n"
                )
        if "related_sop" in old_working:
            handler.working["related_sop"] = old_working["related_sop"]
        return handler

    def _runtime_config_changed(
        self,
        old_config: AgentConfig,
        new_config: AgentConfig,
    ) -> bool:
        """Return True when non-LLM runtime components need rebuilding."""

        return any(
            getattr(old_config, field) != getattr(new_config, field)
            for field in _RUNTIME_CONFIG_FIELDS
        )

    def _apply_pending_runtime_config(self) -> None:
        """Apply deferred workspace/memory/registry changes at a task boundary."""

        if self.pending_runtime_config is None:
            return
        if self._owns_registry:
            new_registry = ToolRegistry.with_builtins(
                self.config,
                language=self._tool_language_for_backend(),
            )
        else:
            new_registry = self.registry
        new_memory = MemoryManager(
            memory_dir=self.config.memory_dir,
            workspace_dir=self.config.workspace_dir,
            language=self.config.resolved_language,
        )
        if self._owns_registry:
            self.registry = new_registry
            self.handler.registry = self.registry
        self.memory = new_memory
        self.handler.cwd = self.config.workspace_dir
        self.handler.client = self.client
        self.pending_runtime_config = None

    def _select_reload_backend(
        self,
        old_active_name: str,
        new_config: AgentConfig,
        new_sessions: dict[str, Any],
    ) -> str:
        """Select active backend after reload: same name, new default, then first."""

        if old_active_name in new_sessions:
            return old_active_name
        if new_config.default_backend in new_sessions:
            return new_config.default_backend
        return next(iter(new_sessions))

    def abort(self) -> None:
        """中止当前任务.

        设置 code_stop_signal 通知 code_run 等工具停止执行.
        """
        if self.handler is not None:
            self.handler.request_code_stop()

    def switch_backend(self, name: str) -> None:
        """切换到指定的 LLM 后端.

        将当前 client 的对话历史迁移到目标 session，
        后续 agent 调用将使用新后端。

        Args:
            name: 后端名称（对应 LLMBackendConfig.name）.

        Raises:
            ValueError: 指定名称的后端不存在.
        """
        if name not in self._sessions:
            available = ", ".join(self._sessions.keys())
            raise ValueError(
                f"后端 '{name}' 不存在。可用后端: {available}"
            )

        target = self._sessions[name]
        old_client = self.client
        if target is old_client:
            return
        _migrate_client_state(old_client, target, preserve_usage=False)
        self.client = target
        if self.handler is not None:
            self.handler.client = self.client
        if self._owns_registry:
            self.registry = ToolRegistry.with_builtins(
                self.config,
                language=self._tool_language_for_backend(name),
            )
            if self.handler is not None:
                self.handler.registry = self.registry

        active_loop = self.loop
        if active_loop is not None:
            active_loop.client = self.client
            active_loop.tools_schema = self.registry.generate_openai_schema()
            prompt_factory = getattr(active_loop, "system_prompt_factory", None)
            if callable(prompt_factory):
                prompt = prompt_factory()
                if self.client.system != prompt:
                    self.client.system = prompt

    def _register_builtin_plugins(self, tracer: Any = None) -> None:
        """注册内置插件；缺依赖或缺配置时静默跳过."""
        try:
            from zero_agent.plugins.langfuse_tracing import register

            register(self.hooks, config=self.config, tracer=tracer)
        except Exception:
            pass
        try:
            from zero_agent.plugins.project_mode import register
            register(self.hooks)
        except Exception:
            pass
        try:
            from zero_agent.plugins.worldline_tracking import register
            register(self.hooks)
        except Exception:
            pass

    def list_backends(self) -> list[tuple[str, str, bool]]:
        """列出所有可用的 LLM 后端.

        Returns:
            [(name, model, is_active), ...] 列表.
            name: 后端名称.
            model: 模型 ID.
            is_active: 是否为当前活跃的后端.
        """
        result: list[tuple[str, str, bool]] = []
        active_name = self._get_active_backend_name()
        for name, session in self._sessions.items():
            model = session.config.model
            result.append((name, model, name == active_name))
        return result

    def list_llms(self) -> list[tuple[int, str, bool]]:
        """列出所有可用 LLM 后端.

        Returns:
            [(index, display_name, is_active), ...] 列表.
        """
        result: list[tuple[int, str, bool]] = []
        backends = self.list_backends()
        for i, (name, model, active) in enumerate(backends):
            result.append((i, f"{name}/{model}", active))
        return result

    def next_llm(self, n: int = -1) -> None:
        """切换到下一个或指定 LLM 后端.

        Args:
            n: 目标索引, -1 表示顺序切换到下一个.
        """
        backends = self.list_backends()
        if not backends:
            return
        active_idx = next((i for i, (_, _, a) in enumerate(backends) if a), 0)
        if n < 0:
            n = (active_idx + 1) % len(backends)
        else:
            n = n % len(backends)
        target_name = backends[n][0]
        self.switch_backend(target_name)

    def get_llm_name(self) -> str:
        """返回当前活跃 LLM 的 display 名称."""
        backends = self.list_backends()
        for name, model, active in backends:
            if active:
                return f"{name}/{model}"
        return "unknown"

    def _get_active_backend_name(self) -> str:
        """获取当前实际活跃后端的名称.

        Returns:
            后端名称字符串.
        """
        active_name = getattr(self.client, "name", None)
        if active_name in self._sessions:
            return active_name
        for name, session in self._sessions.items():
            if session is self.client:
                return name
        return "unknown"

    def _tool_language_for_backend(self, backend_name: Optional[str] = None) -> str:
        """Resolve the tool-schema language for an active backend."""
        name = backend_name or self._get_active_backend_name()
        if name == "unknown":
            return self.config.resolved_tool_language
        return self.config.resolved_tool_language_for_backend(name)

    def _build_system_prompt(
        self,
        *,
        prompt_capabilities: Iterable[str] = (),
    ) -> str:
        """构建本地化、状态感知的默认系统提示词.

        Returns:
            系统提示词字符串.
        """
        return self._build_prompt_assembly(
            prompt_capabilities=prompt_capabilities,
        ).render()

    def _build_prompt_assembly(
        self,
        *,
        prompt_capabilities: Iterable[str] = (),
    ) -> PromptAssembly:
        """Build the ordered default prompt blocks for the current task state."""
        lang = self.config.resolved_language
        localizer = PromptLocalizer(lang)
        blocks = [
            PromptBlock("base", self._load_system_prompt_template(lang)),
            PromptBlock(
                "reply_language",
                f"\n{localizer.text(PROMPT_REPLY_LANGUAGE)}\n",
            ),
        ]
        now = time.localtime()
        date = time.strftime("%Y-%m-%d", now)
        weekday = localizer.text(WEEKDAY_MESSAGE_IDS[now.tm_wday])
        blocks.append(PromptBlock(
            "date",
            f"\n{localizer.text(PROMPT_TODAY_LABEL)} {date} {weekday}\n",
            dynamic=True,
        ))
        blocks.append(PromptBlock(
            "memory",
            self.memory.get_global_memory_context(),
            dynamic=True,
        ))
        blocks.append(PromptBlock(
            "task_control",
            f"\n{localizer.task_control(self.handler.task_contract.mode)}\n",
            dynamic=True,
        ))

        extra_sys = getattr(self.client, "extra_sys_prompt", "")
        if extra_sys:
            blocks.append(PromptBlock("backend_extra", f"\n{extra_sys}", dynamic=True))

        if getattr(self.config, "peer_hint", False):
            blocks.append(PromptBlock(
                "peer_hint",
                f"\n{localizer.text(PROMPT_PEER_HINT)}\n",
            ))

        blocks.extend(
            self._build_prompt_capabilities(
                prompt_capabilities,
                localizer=localizer,
            ).blocks
        )

        return PromptAssembly(tuple(blocks))

    def _build_prompt_capabilities(
        self,
        capabilities: Iterable[str],
        *,
        localizer: Optional[PromptLocalizer] = None,
    ) -> PromptAssembly:
        """Build localized system blocks for capabilities enabled by the caller."""
        capability_names = tuple(dict.fromkeys(capabilities))
        if not capability_names:
            return PromptAssembly(())
        if localizer is None:
            localizer = PromptLocalizer(self.config.resolved_language)
        blocks = []
        for capability in capability_names:
            try:
                message_id = _PROMPT_CAPABILITY_MESSAGE_IDS[capability]
            except KeyError as exc:
                raise ValueError(f"Unsupported prompt capability: {capability}") from exc
            blocks.append(PromptBlock(
                capability,
                f"\n{localizer.text(message_id)}\n",
            ))
        return PromptAssembly(tuple(blocks))

    @staticmethod
    def _load_system_prompt_template(lang: str) -> str:
        """Load the system prompt template from bundled assets.

        Args:
            lang: 语言代码 ("zh" 或 "en").

        Returns:
            系统提示词模板文本.

        Raises:
            ConfigError: 提示词资产不存在或不可读取.
        """
        suffix = "" if lang == "zh" else "_en"
        filename = f"sys_prompt{suffix}.txt"
        try:
            return resources.files("zero_agent.assets").joinpath(filename).read_text(encoding="utf-8")
        except (FileNotFoundError, ModuleNotFoundError, UnicodeDecodeError) as exc:
            raise ConfigError(f"System prompt asset is required but unavailable: {filename}") from exc

    def _generate_tools_description(self, lang: str = "zh") -> str:
        """从注册中心生成工具描述文本.

        Args:
            lang: 语言代码 "zh" 或 "en".

        Returns:
            格式化的工具列表字符串.
        """
        lines: List[str] = []
        for tool in self.registry.list_all():
            desc = tool.description.split("。")[0] if lang == "zh" else tool.description.split(".")[0]
            lines.append(f"- **{tool.name}**: {desc}")
        return "\n".join(lines)
