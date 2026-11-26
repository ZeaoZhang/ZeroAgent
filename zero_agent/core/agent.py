"""ZeroAgent — 顶层 agent 编排器.

管理组件生命周期（配置 → 工具 → LLM → handler → loop），
提供单任务执行的统一入口。由 CLI / Web Server 等 runner 驱动.
"""

from __future__ import annotations

import atexit
import copy
import logging
import os
import re
import time
import weakref
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Generator, Iterable, List, Optional

from zero_agent.core.config import (
    PERMISSION_MODES,
    AgentConfig,
    load_default_config,
    normalize_permission_mode,
)
from zero_agent.core.checkpoint import CONTINUATION_PROMPT
from zero_agent.core import checkpoint as checkpoint_store
from zero_agent.core import hot_restart
from zero_agent.core.exceptions import ConfigError, ExtensionBootstrapError
from zero_agent.core.handler import BaseHandler
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

_logger = logging.getLogger("zero_agent")

# 进程退出时兜底释放扩展运行时（MCP 子进程等）。
# 弱引用集合不会阻止 agent 被回收，显式 close() 过的 agent 会自行移出。
_live_agents: "weakref.WeakSet[Any]" = weakref.WeakSet()


def close_live_agents() -> None:
    """进程退出兜底：关闭仍存活 agent 的扩展运行时."""
    for agent in list(_live_agents):
        try:
            agent.close()
        except Exception:  # noqa: BLE001 - 退出阶段不再抛异常
            continue


atexit.register(close_live_agents)



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
        session_log_path: str | None = None,
    ) -> None:
        """初始化 ZeroAgent.

        Args:
            config: Agent 配置，None 时从项目 config.yaml 或环境变量构建.
            handler: 自定义工具分发器，None 时创建 BaseHandler.
            registry: 自定义工具注册中心，None 时自动加载内置工具.
            session_log_path: Optional explicit response-log path.
        """
        self.config = config or load_default_config()
        self._session_log_path = session_log_path
        self._response_log_retired = False
        from zero_agent.plugins.langfuse_tracing import LangfuseTracer

        try:
            self._langfuse_tracer = LangfuseTracer.from_config(self.config)
        except Exception:
            self._langfuse_tracer = LangfuseTracer()

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

        # 2. LLM 会话
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

        # 3.5 扩展运行时：必须在 handler registry 归一化之后加载，
        # 否则插件工具会注册进随后被丢弃的 registry
        self.extensions = self._init_extensions()
        self._extension_topology_state = self._extension_topology_value()
        self._mcp_signature = hot_restart.file_signature(self._mcp_path())
        self._core_signature = hot_restart.process_signature()
        self._core_restart_blocked_sig = ""
        self._hot_restart_hook: Optional[Any] = None
        self._resume_handler_state: Optional[dict[str, Any]] = None
        self._checkpoint_pending_messages: list[Any] = []
        self._checkpoint_created_at = 0.0
        self._restart_history: list[float] = []
        self._restart_checkpoint_armed = False
        self._last_checkpoint_path: Optional[str] = None
        self._restart_prompt: Optional[str] = None
        self.checkpoint_context: dict[str, Any] = {}

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
        _live_agents.add(self)

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

    def close(self) -> None:
        """释放扩展运行时（MCP 子进程、门控与插件工具注册）；幂等.

        显式关闭后 agent 仍可继续运行，但扩展能力（含 ``permission_gate``）
        不再生效。用于「确定不再使用该 agent」时的确定性清理；进程退出时还有
        ``atexit`` 兜底。
        """
        extensions = getattr(self, "extensions", None)
        if extensions is None:
            _live_agents.discard(self)
            return
        self.extensions = None
        try:
            extensions.release()
        except Exception:  # noqa: BLE001 - 清理失败不得抛给退出路径
            _logger.warning("扩展运行时清理失败", exc_info=True)
        _live_agents.discard(self)

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
            _logger.warning(
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
                # code_run 的沙箱开关由 registry 构建时捕获的 config 决定，
                # 因此 sandbox 变更必须重建 registry 才能生效
                sandbox_changed = (
                    getattr(old_config, "sandbox_mode", None)
                    != getattr(new_config, "sandbox_mode", None)
                )
                if old_tool_language != new_tool_language or sandbox_changed:
                    new_registry = ToolRegistry.with_builtins(
                        new_config,
                        language=new_tool_language,
                    )
                    registry_changed = True
        except Exception as exc:
            _logger.warning(
                "reload_config: 重建运行时失败，将保留旧会话: %s", exc
            )
            return False

        try:
            self._carry_runtime_config_attrs(old_config, new_config)
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
                self._replay_extension_registrations()
            else:
                # 配置对象换了但没重建 registry：策略层仍要读新配置
                self._refresh_extension_config()
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
            _logger.warning(
                "reload_config: 提交运行时失败，将保留旧状态: %s", exc
            )
            return False

        _commit_config_mtime(
            self._config_path,
            mtime=getattr(new_config, "_source_mtime_ns", None),
        )

        try:
            self._langfuse_tracer.reconfigure(new_config)
        except Exception:
            _logger.warning(
                "reload_config: Langfuse reconfiguration failed",
                exc_info=True,
            )

        _logger.info(
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
        record_activity: bool = True,
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
            record_activity: 是否刷新用户活跃时间戳；后台任务应传 False.

        Yields:
            str → 状态文本，供 UI 实时展示.
            dict → 结构化信息（如 {"turn": 1}）.

        Returns:
            TerminalEvent describing the task terminal state.

        Raises:
            RuntimeError: 当前有任务正在运行（不支持并发）.
            ValueError: 无 pending task 时 PLAN/EXECUTING 未提供 plan_path.
        """
        self._checkpoint_pending_messages = []
        # 入口处还不存在本次任务的 contract，因此只做非核心同步：
        # 生成器内没有重启错误通道，重启校验只在 turn 边界发生。
        self.reload_workflow(allow_core_restart=False)
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
        if record_activity:
            from zero_agent.utils.activity import record_user_activity
            record_user_activity(self.config.memory_dir)

        # 每个任务创建新 handler；等待用户的任务恢复同一契约和证据账本。
        self.handler = self._new_task_handler()
        self.handler.reset_code_stop_signal()
        self._apply_resume_handler_state()
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
            if self._restart_checkpoint_armed:
                pass  # 保留 running checkpoint，供热重启恢复
            elif terminal.status is TerminalStatus.WAITING:
                self.save_checkpoint(status="waiting")
            else:
                self.clear_checkpoint()
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

    _APPROVAL_WORDS = frozenset({
        "批准", "同意", "允许", "可以", "确认", "通过",
        "approve", "allow", "yes", "y", "ok", "confirm",
    })

    @classmethod
    def _is_approval_answer(cls, answer: str) -> bool:
        """判断用户答复是否表示批准（默认拒绝，安全优先）.

        Args:
            answer: 用户答复文本.

        Returns:
            True 表示批准.
        """
        normalized = " ".join(str(answer or "").strip().lower().split())
        # 允许句末标点（"批准。" / "OK！"），但仍要求整串命中词表：
        # 不做前缀或模糊匹配，避免把"批准吧""不同意"当成批准
        normalized = normalized.rstrip("。.!！,，~～ ")
        return normalized in cls._APPROVAL_WORDS

    def _approval_continuation(
        self,
        pending: PendingTaskState,
        answer: str,
    ) -> str:
        """处理「等待人类批准」的恢复，返回续跑提示.

        Args:
            pending: 暂停的任务状态.
            answer: 用户答复.

        Returns:
            交给模型的续跑说明.
        """
        approval = (pending.waiting_data or {}).get("approval")
        if not isinstance(approval, dict):
            approval = {}
        fingerprint = str(approval.get("fingerprint") or "")
        scope = str(approval.get("scope") or "")
        tool_name = str(approval.get("tool_name") or "该工具")

        extensions = getattr(self, "extensions", None)

        if not fingerprint:
            # 指纹缺失说明暂停状态已失效，绝不能谎报批准
            if extensions is not None:
                extensions.clear_approvals(task_id=pending.contract.task_id)
            return (
                "[System] 审批状态已失效（缺少操作指纹），本次操作未获批准。"
                "请重新发起该操作以获取新的审批请求。"
            )

        if self._is_approval_answer(answer):
            if extensions is not None:
                extensions.grant_approval(
                    fingerprint,
                    task_id=pending.contract.task_id,
                    scope=scope,
                )
            return (
                f"[System] 用户已批准对 {tool_name} 的这一次调用。"
                "只重新执行这一个调用，且参数必须与上次完全一致；"
                "本轮此前已经成功的调用不要重放，避免重复副作用。"
                "如果不再需要该操作，说明情况后继续。"
            )

        if extensions is not None:
            # 只清当前任务：其他任务/插件刚发放的通行证不应被这次拒绝连带作废
            extensions.clear_approvals(task_id=pending.contract.task_id)
        return (
            f"[System] 用户没有批准 {tool_name} 的这次调用。用户原话：{answer}\n"
            "不要重试该操作；如果用户在答复里给了新的指令，按新指令处理。"
        )

    def _pending_continuation_content(
        self,
        pending: PendingTaskState,
        answer: str,
    ) -> str:
        """Build the continuation message without changing the original objective."""

        if pending.waiting_kind == "approval":
            return self._approval_continuation(pending, answer)

        if pending.waiting_kind == "restart":
            base = (
                f"{CONTINUATION_PROMPT}\n\n"
                f"原任务：{pending.contract.user_request}"
            )
            # 自动续跑时 answer 就是 CONTINUATION_PROMPT 本身（丢弃）；
            # 进程重启失败后由用户输入继续时，用户这句话不能被吞掉。
            extra = str(answer or "").strip()
            if extra and extra != CONTINUATION_PROMPT:
                return f"{base}\n\n用户补充：{extra}"
            return base

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
        approval = waiting_data.get("approval") if isinstance(waiting_data, dict) else None
        if isinstance(approval, dict) and approval.get("fingerprint"):
            waiting_kind = "approval"
        elif (
            self.handler.task_contract.mode is TaskMode.PLAN
            and set(candidates or []) == partial_candidates
        ):
            waiting_kind = "plan_partial_acceptance"
        else:
            waiting_kind = "ask_user"
        self._pending_task_state = PendingTaskState(
            contract=copy.deepcopy(self.handler.task_contract),
            ledger=copy.deepcopy(self.handler.evidence_ledger),
            plan_verify_status=self.handler.plan_verify_status,
            waiting_kind=waiting_kind,
            waiting_data=waiting_data,
        )

    # ---- 热重启检查点 ---------------------------------------------------

    def _checkpoint_session_id(self) -> str:
        """Return the checkpoint identity used for this agent process."""

        explicit = str(getattr(self.config, "_checkpoint_session_id", "") or "")
        if explicit:
            return explicit
        from_env = os.environ.get("ZA_AGENT_SESSION_ID", "").strip()
        if from_env:
            return from_env
        return f"cli-{os.getpid()}"

    def _checkpoint_source(self) -> str:
        """Return ``desktop`` or ``cli`` for this agent."""

        value = getattr(self.config, "_checkpoint_source", "cli")
        return "desktop" if value == "desktop" else "cli"

    @staticmethod
    def _pending_payload(pending: PendingTaskState) -> dict[str, Any]:
        """Serialize one pending task state for checkpoint storage."""

        return {
            "contract": checkpoint_store.contract_to_payload(pending.contract),
            "ledger": checkpoint_store.ledger_to_payload(pending.ledger),
            "plan_verify_status": pending.plan_verify_status,
            "waiting_kind": pending.waiting_kind,
            "waiting_data": copy.deepcopy(pending.waiting_data or {}),
        }

    def save_checkpoint(
        self,
        *,
        status: str = "running",
        restart: bool = False,
        reset_budget: bool = False,
        core_signature: Optional[str] = None,
    ) -> Optional[Any]:
        """Persist the current task state so a hot restart can resume it.

        Args:
            status: ``running`` 或 ``waiting``.
            restart: True 表示本次写盘用于热重启（计一次重启预算）.
            reset_budget: True 时清空重启预算（手动重启按钮）.
            core_signature: 覆盖检查点里的核心签名.

        Returns:
            写入的路径；空闲任务或未配置 sessions_dir 时返回 None.
        """
        if status not in {"running", "waiting"}:
            raise ValueError(f"invalid checkpoint status: {status!r}")
        if (
            status == "running"
            and not self._is_running_task
            and self._pending_task_state is None
        ):
            return None
        sessions_dir = getattr(self.config, "sessions_dir", None)
        if not sessions_dir:
            return None

        handler = self.handler
        contract = getattr(handler, "task_contract", None)
        ledger = getattr(handler, "evidence_ledger", None)
        if not isinstance(ledger, EvidenceLedger):
            ledger = EvidenceLedger()
        handler_payload = {
            "working": copy.deepcopy(dict(getattr(handler, "working", {}) or {})),
            "history_info": list(getattr(handler, "history_info", []) or []),
            "last_user_request": str(
                getattr(handler, "last_user_request", "") or ""
            ),
            "plan_verify_status": str(
                getattr(handler, "plan_verify_status", "missing") or "missing"
            ),
            "task_contract": checkpoint_store.contract_to_payload(contract),
            "evidence_ledger": checkpoint_store.ledger_to_payload(ledger),
        }
        pending = self._pending_task_state
        if reset_budget:
            restart_history: list[float] = []
        else:
            restart_history = list(self._restart_history)
        if restart:
            restart_history.append(time.time())
        created_at = self._checkpoint_created_at or time.time()
        payload = {
            "version": checkpoint_store.CHECKPOINT_VERSION,
            "session_id": self._checkpoint_session_id(),
            "source": self._checkpoint_source(),
            "status": status,
            "created_at": created_at,
            "updated_at": time.time(),
            "restart_history": restart_history,
            "core_signature": core_signature or hot_restart.core_signature(),
            "config_path": str(getattr(self.config, "_source_path", "") or ""),
            "cwd": str(getattr(self.config, "workspace_dir", "") or ""),
            "user_request": str(getattr(contract, "user_request", "") or ""),
            "initial_mode": getattr(
                getattr(contract, "mode", None), "value", TaskMode.OPEN.value,
            ),
            "plan_path": getattr(contract, "plan_path", None),
            "history": checkpoint_store.normalize_history(
                getattr(self.client, "history", []),
            ),
            "pending_messages": checkpoint_store.normalize_history(
                self._checkpoint_pending_messages,
            ),
            "handler": handler_payload,
            "pending": self._pending_payload(pending) if pending is not None else {},
            "mode_context": copy.deepcopy(self.checkpoint_context or {}),
            "turn": int(getattr(handler, "current_turn", 0) or 0),
        }
        path = checkpoint_store.save(
            sessions_dir,
            self._checkpoint_session_id(),
            payload,
        )
        self._checkpoint_created_at = created_at
        self._restart_history = restart_history
        self._last_checkpoint_path = str(path)
        if restart:
            self._restart_checkpoint_armed = True
        return path

    def restore_checkpoint(self, checkpoint: dict | str | Any) -> bool:
        """Restore a checkpoint into this agent.

        Args:
            checkpoint: 已解析的 dict，或检查点文件路径.

        Returns:
            True 表示恢复成功；版本、会话标识或契约不匹配时返回 False.
        """
        data: Optional[dict[str, Any]]
        if isinstance(checkpoint, dict):
            data = dict(checkpoint)
        else:
            data = checkpoint_store.load(checkpoint)
        if not data:
            return False
        if data.get("version") != checkpoint_store.CHECKPOINT_VERSION:
            return False
        if str(data.get("session_id") or "") != self._checkpoint_session_id():
            return False

        history = checkpoint_store.normalize_history(data.get("history"))
        pending_messages = data.get("pending_messages")
        self.client.history = checkpoint_store.merge_pending_messages(
            history,
            pending_messages,
        )

        handler_state = data.get("handler")
        if not isinstance(handler_state, dict):
            handler_state = {}
        contract = checkpoint_store.contract_from_payload(
            handler_state.get("task_contract"),
        )
        ledger = checkpoint_store.ledger_from_payload(
            handler_state.get("evidence_ledger"),
        )
        pending_payload = data.get("pending")
        if not isinstance(pending_payload, dict):
            pending_payload = {}

        pending_contract = checkpoint_store.contract_from_payload(
            pending_payload.get("contract"),
        ) or contract
        if pending_contract is None:
            return False
        if pending_payload:
            pending_ledger = checkpoint_store.ledger_from_payload(
                pending_payload.get("ledger"),
            )
        else:
            pending_ledger = ledger
        plan_verify_status = str(
            pending_payload.get("plan_verify_status")
            or handler_state.get("plan_verify_status")
            or "missing"
        )
        waiting_kind = str(pending_payload.get("waiting_kind") or "restart")
        if waiting_kind not in {
            "ask_user", "plan_partial_acceptance", "approval", "restart",
        }:
            waiting_kind = "restart"
        waiting_data = pending_payload.get("waiting_data")
        if not isinstance(waiting_data, dict):
            waiting_data = {}
        if (
            waiting_kind == "approval"
            and normalize_permission_mode(
                getattr(self.config, "permission_mode", None),
            ) == "full"
        ):
            waiting_kind = "restart"
            waiting_data = {}

        self._pending_task_state = PendingTaskState(
            contract=pending_contract,
            ledger=pending_ledger,
            plan_verify_status=plan_verify_status,
            waiting_kind=waiting_kind,  # type: ignore[arg-type]
            waiting_data=copy.deepcopy(waiting_data),
        )
        self._resume_handler_state = handler_state
        self.checkpoint_context = (
            copy.deepcopy(data.get("mode_context"))
            if isinstance(data.get("mode_context"), dict)
            else {}
        )
        self._restart_history = [
            float(item)
            for item in data.get("restart_history") or []
            if isinstance(item, (int, float))
        ]
        self._restart_checkpoint_armed = False
        self._checkpoint_created_at = float(data.get("created_at") or 0.0)
        self._restart_prompt = (
            CONTINUATION_PROMPT if waiting_kind == "restart" else None
        )
        return True

    def clear_checkpoint(self) -> None:
        """Delete the on-disk checkpoint and reset restart bookkeeping."""

        sessions_dir = getattr(self.config, "sessions_dir", None)
        if sessions_dir:
            checkpoint_store.clear(sessions_dir, self._checkpoint_session_id())
        self._restart_checkpoint_armed = False
        self._restart_history = []
        self._restart_prompt = None
        self._last_checkpoint_path = None
        self._checkpoint_created_at = 0.0

    def take_restart_prompt(self) -> Optional[str]:
        """Return the one-shot continuation prompt after a restart restore."""

        prompt = self._restart_prompt
        self._restart_prompt = None
        return prompt

    @property
    def pending_task_state(self) -> Optional[PendingTaskState]:
        """当前暂停中、等待用户答复的任务状态；没有则为 None."""

        return self._pending_task_state

    @property
    def restart_checkpoint_armed(self) -> bool:
        """本次任务是否已写好等待热重启的检查点（前端据此决定要不要补写）."""

        return self._restart_checkpoint_armed

    @property
    def last_checkpoint_path(self) -> Optional[str]:
        """最近一次写入的检查点文件路径；从未写过则为 None."""

        return self._last_checkpoint_path

    def note_pending_messages(self, messages: Any) -> None:
        """Remember the messages currently being sent to the LLM."""

        self._checkpoint_pending_messages = list(messages or [])

    def clear_pending_messages(self) -> None:
        """Forget the last LLM request payload (its response is in history)."""

        self._checkpoint_pending_messages = []

    def _apply_resume_handler_state(self) -> None:
        """Restore handler working memory captured by a checkpoint."""

        state = self._resume_handler_state
        if not state:
            return
        self.handler.working = copy.deepcopy(state.get("working") or {})
        self.handler.history_info = list(state.get("history_info") or [])
        self.handler.last_user_request = str(state.get("last_user_request") or "")
        self._resume_handler_state = None

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
            self._replay_extension_registrations()
        else:
            self._refresh_extension_config()
        self.memory = new_memory
        self.handler.cwd = self.config.workspace_dir
        self.handler.client = self.client
        self.pending_runtime_config = None

    @staticmethod
    def _carry_runtime_config_attrs(old_config: Any, new_config: Any) -> None:
        """Preserve process-local config attributes across a YAML reload.

        Checkpoint identity and desktop session wiring are runtime-only
        attributes (never persisted in YAML). Dropping them on reload would
        orphan the current task's checkpoint and mislabel the source.
        """
        for attr in (
            "_checkpoint_session_id",
            "_checkpoint_source",
            "_desktop_session_id",
            "_desktop_agent_id",
            "_desktop_subagent_registry_path",
        ):
            if hasattr(old_config, attr):
                setattr(new_config, attr, getattr(old_config, attr))

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
            self._replay_extension_registrations()

        active_loop = self.loop
        if active_loop is not None:
            active_loop.client = self.client
            active_loop.tools_schema = self.registry.generate_openai_schema()
            prompt_factory = getattr(active_loop, "system_prompt_factory", None)
            if callable(prompt_factory):
                prompt = prompt_factory()
                if self.client.system != prompt:
                    self.client.system = prompt

    def _replay_extension_registrations(self) -> None:
        """registry 被重建后重放扩展注册，并刷新 loop 的 tool schema.

        共有三条重建路径（reload_config / _apply_pending_runtime_config /
        switch_backend），都必须经过这里，否则插件工具会随旧 registry 消失。

        同时把运行时持有的 config 换成新对象：``permission_gate`` 等内置扩展
        通过 ``runtime.config`` 解析相对路径的基准目录，若仍指向旧对象，
        路径判定会与实际写入位置（handler.cwd）分叉。插件与扩展目录本身
        （``extensions_home``）变更仍需重启，这里不重新加载。
        """
        self._refresh_extension_config()
        extensions = getattr(self, "extensions", None)
        if extensions is not None:
            extensions.attach_registry(self.registry)
        active_loop = getattr(self, "loop", None)
        if active_loop is not None:
            active_loop.tools_schema = self.registry.generate_openai_schema()

    def _refresh_extension_config(self) -> None:
        """把运行时持有的 config 换成当前对象.

        ``permission_gate`` 用 ``runtime.config`` 解析 workspace 与受保护前缀。
        只要配置换了对象（哪怕没触发 registry 重建，例如只改了
        ``extensions.protected_writes``），就必须同步刷新，否则策略仍在按旧
        配置判定。

        插件与扩展目录本身（``extensions_home``）变更仍需重启，这里不重新加载。
        """
        extensions = getattr(self, "extensions", None)
        if extensions is not None:
            extensions.config = self.config

    # 安全类内置扩展：extensions.enabled=false 时仍然保留，避免“关插件顺手关掉安全门”
    _SAFETY_EXTENSIONS = ("permission_gate",)
    # 能力类内置扩展：只在启用扩展时安装
    _CAPABILITY_EXTENSIONS = ("skills_index", "mcp_client")
    # 可选能力扩展：默认不进入 with_builtins()，按配置加载
    _OPTIONAL_CAPABILITY_EXTENSIONS = ("browser_tools",)
    # 内部扩展：与外部插件共用同一运行时，始终安装（自身按配置 no-op）
    _INTERNAL_EXTENSIONS = (
        ("langfuse_tracing", "zero_agent.plugins.langfuse_tracing"),
        ("project_mode", "zero_agent.plugins.project_mode"),
        ("worldline_tracking", "zero_agent.plugins.worldline_tracking"),
    )

    def _init_extensions(self) -> Any:
        """创建扩展运行时并加载插件与内置扩展.

        ``extensions_enabled=false`` 只关闭外部插件与能力类扩展，安全门仍然生效。
        ``permissions.mode`` 决定是否执行工具调用门控这一层：``full``（默认）下
        ``evaluate_tool_call`` 直接返回 ``allow``，**完全不执行**任何 ``on_tool_call``
        回调——危险 shell、effects 声明缺失、插件 veto、门控异常都不再生效；
        ``ask`` 才启用审批三态与 fail-closed。真正的强制隔离只有
        ``extensions.sandbox``（``code_run`` 的进程沙箱）。

        安全类扩展或运行时本身安装失败时抛 :class:`ExtensionBootstrapError`，
        绝不在 ``ask`` 缺了门控来源的情况下继续启动。外部插件与能力类扩展的失败仍只
        记录日志，不影响启动。

        Returns:
            ExtensionRuntime 实例.

        Raises:
            ExtensionBootstrapError: 运行时或安全类扩展无法安装.
        """
        enabled = bool(getattr(self.config, "extensions_enabled", True))
        from zero_agent.extensions.runtime import ExtensionRuntime

        try:
            runtime = ExtensionRuntime(
                registry=self.registry,
                home=getattr(self.config, "extensions_home", None),
                config=self.config,
                agent=self,
            )
            runtime.services["langfuse_tracer"] = self._langfuse_tracer
        except Exception as exc:  # noqa: BLE001 - 无运行时意味着无安全门
            raise ExtensionBootstrapError(
                f"扩展运行时初始化失败: {exc}",
            ) from exc

        try:
            if enabled:
                runtime.load_all()
            self._sync_builtin_extensions(runtime, enabled)
        except Exception:
            try:
                runtime.release()
            except Exception:  # noqa: BLE001 - 清理失败不覆盖原始错误
                _logger.warning("扩展运行时清理失败", exc_info=True)
            raise
        return runtime

    def _builtin_targets(self, enabled: bool) -> list[tuple[str, str, bool]]:
        """Return ``(name, module_path, is_safety)`` for desired builtins."""

        targets = [
            (name, f"zero_agent.extensions.builtin.{name}", name in self._SAFETY_EXTENSIONS)
            for name in self._SAFETY_EXTENSIONS
        ]
        if enabled:
            targets.extend(
                (name, f"zero_agent.extensions.builtin.{name}", False)
                for name in self._CAPABILITY_EXTENSIONS
            )
            if getattr(self.config, "browser_tools", "auto") != "off":
                targets.extend(
                    (name, f"zero_agent.extensions.builtin.{name}", False)
                    for name in self._OPTIONAL_CAPABILITY_EXTENSIONS
                )
        targets.extend(
            (name, module_path, False)
            for name, module_path in self._INTERNAL_EXTENSIONS
        )
        return targets

    def _install_builtin_extension(
        self,
        runtime: Any,
        name: str,
        module_path: str,
        is_safety: bool,
        reinstall: bool = False,
    ) -> bool:
        """Install one builtin extension; safety failures are fatal.

        Args:
            runtime: ExtensionRuntime 实例.
            name: 内置扩展归属名.
            module_path: 可导入的模块路径.
            is_safety: True 时安装失败抛 :class:`ExtensionBootstrapError`.
            reinstall: True 时重新执行模块代码（拓扑变化触发的重装），
                使内置扩展的源码改动在不重启的前提下生效；首次安装无需
                重执行，``sys.modules`` 里就是刚导入的代码.

        Returns:
            True 表示安装成功.
        """
        from zero_agent.extensions.context import ExtensionContext

        try:
            module = runtime.load_builtin_module(module_path, reexecute=reinstall)
            teardown = module.setup(ExtensionContext(name, runtime))
            if callable(teardown):
                runtime.register_teardown(name, teardown)
            # 内置扩展不经 load_all，必须显式登记归属，
            # 否则 release()（ZeroAgent.close() 与进程退出兜底）不会撤销
            # 它们注册的工具与回调
            runtime.track_owner(name)
            # 记下模块对象：reload_builtin 失败时回滚需要它
            runtime.register_builtin_module(name, module)
            return True
        except Exception as exc:  # noqa: BLE001 - 按安全/能力分级处理
            # 安装到一半失败时清掉半成品注册（例如门控只注册了一半）
            try:
                runtime.unregister_owner(name)
            except Exception:  # noqa: BLE001 - 清理失败不影响后续处理
                pass
            if is_safety:
                raise ExtensionBootstrapError(
                    f"安全扩展 {name} 安装失败: {exc}",
                ) from exc
            _logger.warning("内置扩展 %s 安装失败，已跳过: %s", name, exc)
            return False

    def _sync_builtin_extensions(
        self,
        runtime: Any,
        enabled: bool,
        force_names: set[str] = frozenset(),
    ) -> None:
        """Unload stale builtins and install missing/forced ones."""

        targets = self._builtin_targets(enabled)
        desired = {
            name: (module_path, is_safety)
            for name, module_path, is_safety in targets
        }
        installed = set(runtime.installed_builtins)
        for name in sorted(installed - set(desired)):
            runtime.unload_owner(name)
        for name, module_path, is_safety in targets:
            if name in installed and name not in force_names:
                continue
            reinstall = name in installed
            if name in set(runtime.installed_builtins):
                runtime.unload_owner(name)
            self._install_builtin_extension(
                runtime, name, module_path, is_safety, reinstall=reinstall,
            )

    # ---- 拓扑（plugins / home / browser）与工作流热更新 -----------------

    def _extension_topology_value(self) -> tuple[bool, str, str]:
        """Return the config tuple that requires extension reloading."""

        return (
            bool(self.config.extensions_enabled),
            str(self.config.extensions_home or ""),
            str(self.config.browser_tools or "auto"),
        )

    def _mcp_path(self) -> Optional[Path]:
        """Return ``<extensions.home>/mcp.json`` for the live runtime."""

        extensions = getattr(self, "extensions", None)
        if extensions is None:
            return None
        return Path(extensions.home) / "mcp.json"

    def _apply_extension_topology(self) -> bool:
        """Apply extension enable/home/browser changes at a turn boundary."""

        extensions = getattr(self, "extensions", None)
        if extensions is None:
            return False
        old_enabled, old_home, old_browser = self._extension_topology_state
        new_enabled, new_home, new_browser = self._extension_topology_value()
        home_changed = old_home != new_home
        enabled_changed = old_enabled != new_enabled
        browser_changed = old_browser != new_browser
        if home_changed:
            extensions.set_home(new_home or None)
        if not new_enabled or home_changed:
            extensions.unload_all_plugins()
        force_names = {"skills_index", "mcp_client"} if (
            home_changed or enabled_changed
        ) else set()
        if browser_changed:
            # browser_tools 的 setup 在安装时就按 extensions.browser 决定注册什么；
            # 不强制重装的话 auto↔on↔off 的切换会被当成「已应用」而静默不生效。
            force_names.add("browser_tools")
        self._sync_builtin_extensions(extensions, new_enabled, force_names=force_names)
        if new_enabled:
            extensions.sync_plugins(force=home_changed)
        self._extension_topology_state = (new_enabled, new_home, new_browser)
        self._mcp_signature = hot_restart.file_signature(self._mcp_path())
        return True

    def _reload_builtin_extension(self, name: str) -> bool:
        """Reload one installed builtin extension by name.

        失败时 ``reload_builtin`` 已回滚到旧注册，这里只负责记录。
        """

        extensions = getattr(self, "extensions", None)
        if extensions is None:
            return False
        targets = {
            target: module_path
            for target, module_path, _is_safety in self._builtin_targets(True)
        }
        module_path = targets.get(name)
        if not module_path:
            return False
        try:
            return extensions.reload_builtin(name, module_path)
        except Exception as exc:  # noqa: BLE001 - 回滚后继续用旧注册
            _logger.warning("内置扩展 %s 重载失败，已保留旧注册: %s", name, exc)
            return False

    def _refresh_loop_tools(self) -> None:
        """Refresh the active loop's tool schema after registry changes."""

        active_loop = getattr(self, "loop", None)
        if active_loop is not None:
            active_loop.tools_schema = self.registry.generate_openai_schema()

    def set_hot_restart_hook(self, hook: Optional[Any]) -> None:
        """Install the callback used to perform a core process restart."""

        self._hot_restart_hook = hook

    def set_permission_mode(self, mode: str) -> str:
        """Switch the live permission mode and refresh extension config.

        Args:
            mode: ``ask`` 或 ``full``.

        Returns:
            生效后的模式.

        Raises:
            ValueError: 未知模式.
        """
        normalized = str(mode or "").strip().lower()
        if normalized not in PERMISSION_MODES:
            raise ValueError(f"unknown permission mode: {mode!r}")
        self.config.permission_mode = normalized
        self._refresh_extension_config()
        return normalized

    def reload_workflow(
        self,
        *,
        force: bool = False,
        allow_core_restart: bool = True,
    ) -> dict[str, Any]:
        """Hot-sync config, extensions, plugins, MCP and core code at a boundary.

        Args:
            force: True 时无视签名强制重载插件（手动 ``/update workflow``）.
            allow_core_restart: False 时只做非核心同步（``run()`` 入口用，
                此时新任务的 contract 还不存在，不能写 running checkpoint）.

        Returns:
            结构化同步报告：``config``（是否重载了配置）、``topology``、
            ``plugins``（loaded/reloaded/removed/failed）、``mcp``、
            ``tools_changed``，以及 ``core_restart``——``""`` 表示无重启，
            ``"blocked"`` 表示校验失败（错误在 ``core_restart_error``），
            ``"requested"`` 表示 ``_hot_restart_hook`` 已被调用.

            重启细节：``checkpoint_path`` 只在**有运行中任务**时出现；
            ``core_restart_error == "checkpoint_failed"`` 只在“有任务但写不了
            检查点（如未配置 ``sessions_dir``）”时出现。空闲时照常请求重启，
            但不写检查点、重启后不恢复对话。
        """
        report: dict[str, Any] = {
            "config": False,
            "topology": False,
            "plugins": {},
            "mcp": False,
            "tools_changed": False,
            "core_restart": "",
            "core_restart_error": "",
        }
        report["config"] = self.reload_config()

        extensions = getattr(self, "extensions", None)
        if self._extension_topology_value() != self._extension_topology_state:
            report["topology"] = self._apply_extension_topology()
            report["tools_changed"] = True
        elif (
            extensions is not None
            and self.config.extensions_enabled
            and (self.config.extensions_hot_reload or force)
        ):
            plugins = extensions.sync_plugins(force=force)
            report["plugins"] = plugins
            if plugins.get("loaded") or plugins.get("reloaded") or plugins.get("removed"):
                report["tools_changed"] = True
            mcp_path = self._mcp_path()
            mcp_signature = hot_restart.file_signature(mcp_path)
            if mcp_path is not None and mcp_signature != self._mcp_signature:
                report["mcp"] = self._reload_builtin_extension("mcp_client")
                self._mcp_signature = mcp_signature
                report["tools_changed"] = True

        signature = hot_restart.core_signature()
        should_restart = (
            extensions is not None
            and allow_core_restart
            and self.config.extensions_core_restart
            and self._hot_restart_hook is not None
            and signature != self._core_signature
            and signature != self._core_restart_blocked_sig
        )
        if should_restart:
            valid, error = hot_restart.validate_core()
            if not valid:
                self._core_restart_blocked_sig = signature
                report["core_restart"] = "blocked"
                report["core_restart_error"] = error
            else:
                resumable = bool(self._is_running_task or self._pending_task_state)
                path = (
                    self.save_checkpoint(status="running", restart=True)
                    if resumable
                    else None
                )
                if resumable and path is None:
                    report["core_restart"] = "blocked"
                    report["core_restart_error"] = "checkpoint_failed"
                else:
                    self._core_signature = signature
                    report["core_restart"] = "requested"
                    if path is not None:
                        report["checkpoint_path"] = str(path)
                    self._hot_restart_hook("core_changed")
        elif allow_core_restart:
            # 未启用重启、没有 hook 或本次签名已被判定为 blocked：
            # 刷新基线，避免每个 turn 重复校验同一份代码。
            self._core_signature = signature

        if report["config"] or report["tools_changed"]:
            self._refresh_loop_tools()
        return report

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

        # 扩展贡献的提示词块（插件 / skill 索引 / 其他内置扩展）
        extensions = getattr(self, "extensions", None)
        if extensions is not None:
            try:
                for block in extensions.collect_prompt_blocks():
                    blocks.append(PromptBlock(
                        f"ext:{block.name}",
                        f"\n{block.text}\n",
                        dynamic=True,
                    ))
            except Exception:  # noqa: BLE001 - 提示词块失败不得阻断任务
                _logger.warning("扩展提示词块组装失败", exc_info=True)

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
