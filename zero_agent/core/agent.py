"""ZeroAgent — 顶层 agent 编排器.

管理组件生命周期（配置 → 工具 → LLM → handler → loop），
提供单任务执行的统一入口。由 CLI / Web Server 等 runner 驱动.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig, load_default_config
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

### 行动规范（持续有效）
每次回复（含工具调用轮）都先在回复文字中包含一个<summary></summary> 中输出极简单行（<30字）物理快照：上次结果新信息+本次意图。此内容进入长期工作记忆。

**若用户需求未完成，必须进行工具调用！**

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

### Action Protocol (always in effect)
The reply body should first include a minimal one-line (<30 words) physical snapshot in <summary></summary>: new info from last result + current intent. This goes into long-term working memory.

**If the user's request is not yet complete, tool calls are required!**

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
            config: Agent 配置，None 时从项目 config.yaml 或环境变量构建.
            handler: 自定义工具分发器，None 时创建 BaseHandler.
            registry: 自定义工具注册中心，None 时自动加载内置工具.
            hooks: 自定义 HookSystem，None 时创建默认 HookSystem.
        """
        self.config = config or load_default_config()
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
        self._config_path: Optional[str] = getattr(self.config, "_source_path", None)

    def set_config_path(self, path: Optional[str]) -> None:
        """设置配置文件的路径，用于热重载检测."""
        self._config_path = path

    def reload_config(self) -> bool:
        """若配置文件已变更则热重载配置并重建 LLM session.

        与 GenericAgent 的 reload_mykeys() 对齐：
        检测 YAML 文件 mtime，变更时重新加载并重建 client.

        Returns:
            True 表示配置已重载，False 表示无变更.
        """
        if not self._config_path:
            return False

        from zero_agent.core.config import reload_config_if_changed, _config_mtime

        new_config = reload_config_if_changed(self._config_path)
        if new_config is None:
            return False

        old_backend = self.config.default_backend
        self.config = new_config

        # 重建所有 LLM session
        from zero_agent.llm.factory import LLMFactory

        try:
            new_sessions = LLMFactory.create_all_sessions(new_config)
        except Exception as e:
            import logging
            logging.getLogger("zero_agent").warning(
                "reload_config: 重建 LLM 会话失败，将保留旧会话: %s", e
            )
            return False

        self._sessions = new_sessions
        new_default = new_config.default_backend

        # 保持当前活跃的后端名称不变（如果该后端还存在）
        target_name = old_backend
        if target_name not in self._sessions:
            target_name = new_default

        self.client = self._sessions.get(target_name)
        if self.client is None:
            self.client = next(iter(self._sessions.values()))

        # 更新 handler 引用
        self.handler.client = self.client

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
        self.handler.reset_code_stop_signal()

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
            agent=self,
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

    def list_llms(self) -> list[tuple[int, str, bool]]:
        """列出所有可用 LLM 后端 (兼容 GenericAgent 接口).

        Returns:
            [(index, display_name, is_active), ...] 列表.
        """
        result: list[tuple[int, str, bool]] = []
        backends = self.list_backends()
        for i, (name, model, active) in enumerate(backends):
            result.append((i, f"{name}/{model}", active))
        return result

    def next_llm(self, n: int = -1) -> None:
        """切换到下一个或指定 LLM 后端 (兼容 GenericAgent 接口).

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
        """返回当前活跃 LLM 的 display 名称 (兼容 GenericAgent 接口)."""
        backends = self.list_backends()
        for name, model, active in backends:
            if active:
                return f"{name}/{model}"
        return "unknown"

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

        优先级:
            1. config.prompt_file 指定的文件路径
            2. zero_agent/assets/sys_prompt.txt（自动检测）
            3. 代码中硬编码的默认常量（fallback）

        根据 config.resolved_language 选择中英文模板.
        包含日期、工作目录、可用工具列表、记忆上下文.

        Returns:
            系统提示词字符串.
        """
        import datetime

        lang = self.config.resolved_language
        tool_lang = self.config.resolved_tool_language

        # 尝试从外部文件加载模板
        template = self._load_prompt_template(lang)
        if template is None:
            # 回退到代码中的默认常量
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

    @staticmethod
    def _load_prompt_template(lang: str) -> Optional[str]:
        """从外部文件加载系统提示词模板.

        查找顺序:
            1. zero_agent/assets/sys_prompt.txt (zh) 或 sys_prompt_en.txt (en)
            2. 返回 None 表示使用代码中的默认常量.

        Args:
            lang: 语言代码 ("zh" 或 "en").

        Returns:
            模板字符串，未找到时返回 None.
        """
        import os

        suffix = "" if lang == "zh" else "_en"
        filename = f"sys_prompt{suffix}.txt"

        try:
            assets_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets",
            )
            prompt_path = os.path.join(assets_dir, filename)
            if os.path.isfile(prompt_path):
                with open(prompt_path, encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass

        return None

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
