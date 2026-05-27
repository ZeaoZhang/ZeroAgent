"""ZeroAgent — 顶层 agent 编排器.

管理组件生命周期（配置 → 工具 → LLM → handler → loop），
提供单任务执行的统一入口。由 CLI / Web Server 等 runner 驱动.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.core.handler import BaseHandler
from zero_agent.core.hooks import HookSystem
from zero_agent.core.loop import AgentLoop
from zero_agent.memory.manager import MemoryManager
from zero_agent.tools.registry import ToolRegistry
from zero_agent.llm.factory import LLMFactory


# 系统提示词模板
_SYSTEM_PROMPT_ZH = """你是一个具备工具调用能力的 AI 助手，可以使用以下工具：
- 代码执行 (code_run)
- 文件操作 (file_read, file_write, file_patch)
- 浏览器交互 (web_scan, web_execute_js)
- 记忆管理 (update_working_checkpoint)

在指定的工作目录中操作。逐步使用工具完成任务。
任务完成时直接给出最终回复，无需再调用工具。
需要用户输入时使用 ask_user 工具。
每次回复中请包含 <summary>标签</summary> 简要概括本轮操作。

当前日期: {date}
工作目录: {workspace_dir}
"""

_SYSTEM_PROMPT_EN = """You are a helpful AI assistant with access to tools for:
- Code execution (code_run)
- File operations (file_read, file_write, file_patch)
- Browser interaction (web_scan, web_execute_js)
- Memory management (update_working_checkpoint)

Work in the designated workspace directory. Use tools to accomplish tasks step by step.
When a task is complete, provide a final response without calling further tools.
If you need user input, use the ask_user tool.
Include a <summary>brief summary of this turn</summary> in every reply.

Current date: {date}
Working directory: {workspace_dir}
"""


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
    ) -> None:
        """初始化 ZeroAgent.

        Args:
            config: Agent 配置，None 时从环境变量构建.
            handler: 自定义工具分发器，None 时创建 BaseHandler.
            registry: 自定义工具注册中心，None 时自动加载内置工具.
            hooks: 自定义 HookSystem，None 时创建默认 HookSystem.
        """
        self.config = config or AgentConfig.from_env()
        self.hooks = hooks or HookSystem()
        self._register_builtin_plugins()

        # 1. 工具注册中心
        self.registry = registry or ToolRegistry.with_builtins(self.config)

        # 2. LLM 会话：创建所有独立 session + 当前活跃 client
        self._sessions = LLMFactory.create_all_sessions(self.config)
        default_name = self.config.default_backend
        self.client = self._sessions.get(default_name)
        if self.client is None:
            self.client = next(iter(self._sessions.values()))

        # 3. 工具分发器
        self.handler = handler or BaseHandler(
            registry=self.registry,
            cwd=self.config.workspace_dir,
        )
        self.handler.parent = self

        # 4. 记忆管理器
        self.memory = MemoryManager(
            memory_dir=self.config.memory_dir,
            workspace_dir=self.config.workspace_dir,
        )

        # 5. 运行时状态
        self.task_dir: Optional[str] = None
        self._turn_end_hooks: Dict[str, Any] = {}
        self.loop: Optional[AgentLoop] = None

    def run(
        self,
        user_input: str,
        system_prompt: Optional[str] = None,
        initial_user_content: Optional[str] = None,
    ) -> Generator[Any, None, dict]:
        """执行单次 agent 任务.

        创建 AgentLoop 并驱动执行，每次 yield 返回状态信息供 UI 消费.
        这是 ZeroAgent 的主入口，runner 层通过此方法驱动 agent.

        Args:
            user_input: 用户输入（任务描述）.
            system_prompt: 系统提示词，None 时使用默认构建.
            initial_user_content: 可选的首条 user message 内容.

        Yields:
            str → 状态文本，供 UI 实时展示.
            dict → 结构化信息（如 {"turn": 1}）.

        Returns:
            exit_reason 字典.

        Raises:
            RuntimeError: 当前有任务正在运行（不支持并发）.
        """
        # 创建工作目录和记忆目录
        os.makedirs(self.config.workspace_dir, exist_ok=True)
        self.memory.init_memory()

        # 构建系统提示词
        prompt = system_prompt or self._build_system_prompt()

        # 创建 AgentLoop
        tools_schema = self.registry.generate_openai_schema()
        loop = AgentLoop(
            client=self.client,
            handler=self.handler,
            tools_schema=tools_schema,
            max_turns=self.config.max_turns,
            verbose=self.config.verbose,
            hooks=self.hooks,
        )
        self.loop = loop

        return (yield from loop.run(
            system_prompt=prompt,
            user_input=user_input,
            initial_user_content=initial_user_content,
        ))

    def abort(self) -> None:
        """中止当前任务.

        设置 code_stop_signal 通知 code_run 等工具停止执行.
        """
        if self.handler is not None:
            self.handler.code_stop_signal.append(1)

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

        # 迁移历史：从旧 client 复制到新 session
        try:
            old_history = old_client.history
        except AttributeError:
            old_history = []

        target.history = old_history
        target.system = getattr(old_client, "system", "")
        target.last_tools = ""

        self.client = target

    def _register_builtin_plugins(self) -> None:
        """注册内置插件；缺依赖或缺配置时静默跳过."""
        try:
            from zero_agent.plugins.langfuse_tracing import register
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

    def _get_active_backend_name(self) -> str:
        """获取当前活跃后端的名称.

        Returns:
            后端名称字符串.
        """
        # 按 model 匹配 _sessions 中的引用
        for name, session in self._sessions.items():
            if session is self.client:
                return name
        return "unknown"

    def _build_system_prompt(self) -> str:
        """从配置和已注册工具构建默认系统提示词.

        根据 config.resolved_language 选择中英文模板.
        包含日期、工作目录、可用工具列表、记忆上下文.

        Returns:
            系统提示词字符串.
        """
        import datetime

        lang = self.config.resolved_language
        tool_lang = self.config.resolved_tool_language
        template = _SYSTEM_PROMPT_ZH if lang == "zh" else _SYSTEM_PROMPT_EN

        tools_desc = self._generate_tools_description(tool_lang)
        prompt = template.format(
            date=datetime.date.today().strftime("%Y-%m-%d %a"),
            workspace_dir=os.path.abspath(self.config.workspace_dir),
        )
        if tools_desc:
            prompt += f"\n## Available Tools\n{tools_desc}"

        # 注入记忆上下文
        memory_ctx = self.memory.get_global_memory_context()
        if memory_ctx:
            prompt += f"\n\n[Memory]\n{memory_ctx}"

        # 注入会话级额外系统提示词
        extra_sys = getattr(self.client, "extra_sys_prompt", "")
        if extra_sys:
            prompt += f"\n{extra_sys}"

        return prompt

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
