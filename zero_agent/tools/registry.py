"""工具定义与注册系统.

ToolDefinition: 工具的声明式描述（名称、描述、JSON Schema 参数、handler 函数、分类）.
ToolRegistry: 工具注册、查找、Schema 生成（支持 OpenAI 和 Claude 两种格式）.

工具注册支持三种方式:
    1. ToolRegistry.with_builtins() — 工厂方法，预注册内置工具
    2. registry.register(ToolDefinition(...)) — 手动注册
    3. BaseHandler.do_<name>() 方法约定 — 由 handler.dispatch() 自动发现
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Generator, List, Optional

from zero_agent.core.config import AgentConfig


# handler 函数的签名: (args, response, handler) → Generator[str, None, Any]
ToolHandler = Callable[
    [Dict[str, Any], Any, "BaseHandler"],
    Generator[str, None, Any],
]


@dataclass
class ToolDefinition:
    """工具的声明式定义.

    Attributes:
        name: 工具名称（唯一标识），LLM 通过此名称调用工具.
        description: 工具功能描述，会出现在 LLM 的 tool schema 中.
        parameters: JSON Schema 格式的参数定义（properties + required）.
        handler: 工具执行函数，是一个 generator，yield 状态信息，return 结果.
        category: 工具分类标签，用于分组管理.
    """

    name: str
    description: str
    parameters: dict
    handler: ToolHandler
    category: str = "general"


@dataclass
class ToolRegistry:
    """工具注册中心.

    管理所有可用工具，生成不同 LLM API 格式的 tool schema.
    本身不持有任何外部依赖，可以在测试中独立构建.

    Attributes:
        _tools: 按名称索引的工具字典.
    """

    _tools: Dict[str, ToolDefinition] = field(default_factory=dict)

    def register(self, tool: ToolDefinition) -> None:
        """注册一个工具.

        Args:
            tool: 工具定义. 若名称已存在则覆盖.
        """
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[ToolDefinition]:
        """按名称查找工具.

        Args:
            name: 工具名称.

        Returns:
            ToolDefinition 或 None.
        """
        return self._tools.get(name)

    def list_all(self) -> List[ToolDefinition]:
        """列出所有已注册工具.

        Returns:
            工具定义列表.
        """
        return list(self._tools.values())

    def list_by_category(self, category: str) -> List[ToolDefinition]:
        """按分类筛选工具.

        Args:
            category: 分类标签.

        Returns:
            匹配的工具列表.
        """
        return [t for t in self._tools.values() if t.category == category]

    def generate_openai_schema(self) -> List[dict]:
        """生成 OpenAI function-calling 格式的 tool schema.

        Returns:
            [{"type": "function", "function": {"name": ..., "description": ...,
             "parameters": ...}}, ...]
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
        ]

    def generate_claude_schema(self) -> List[dict]:
        """生成 Claude tool_use 格式的 tool schema.

        Returns:
            [{"name": ..., "description": ..., "input_schema": ...}, ...]
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
            for tool in self._tools.values()
        ]

    def export_schemas_json(self, output_dir: Optional[str] = None) -> dict:
        """将工具 schema 导出为 JSON 文件.

        生成 OpenAI 和 Claude 两种格式的 JSON schema 文件，
        与 GenericAgent assets/tools_schema.json 对齐.

        Args:
            output_dir: 输出目录，None 时使用 zero_agent/assets/.

        Returns:
            {"openai": path, "claude": path} 导出文件路径字典.
        """
        import json
        import os

        if output_dir is None:
            output_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "assets",
            )

        os.makedirs(output_dir, exist_ok=True)
        result = {}

        # OpenAI 格式
        openai_path = os.path.join(output_dir, "tools_schema.json")
        openai_schemas = self.generate_openai_schema()
        with open(openai_path, "w", encoding="utf-8") as f:
            json.dump(openai_schemas, f, ensure_ascii=False, indent=2)
        result["openai"] = openai_path

        # Claude 格式
        claude_path = os.path.join(output_dir, "tools_schema_claude.json")
        claude_schemas = self.generate_claude_schema()
        with open(claude_path, "w", encoding="utf-8") as f:
            json.dump(claude_schemas, f, ensure_ascii=False, indent=2)
        result["claude"] = claude_path

        return result

    @classmethod
    def with_builtins(cls, config: AgentConfig) -> "ToolRegistry":
        """工厂方法：创建预注册了内置工具的 ToolRegistry.

        自动发现并注册 zero_agent/tools/builtin/ 下的所有工具模块.

        Args:
            config: Agent 配置，用于工具初始化时的路径参考.

        Returns:
            预注册了内置工具的 ToolRegistry 实例.
        """
        registry = cls()

        # 内置工具模块列表
        builtin_modules = [
            ("zero_agent.tools.builtin.code", "register_code_tools"),
            ("zero_agent.tools.builtin.file", "register_file_tools"),
            ("zero_agent.tools.builtin.memory", "register_memory_tools"),
            ("zero_agent.tools.builtin.user", "register_user_tools"),
            ("zero_agent.tools.builtin.web", "register_web_tools"),
        ]

        for module_name, register_func in builtin_modules:
            try:
                module = importlib.import_module(module_name)
                if hasattr(module, register_func):
                    getattr(module, register_func)(registry, config)
            except ImportError:
                # 内置模块不存在时跳过，不阻断注册流程
                pass

        return registry
