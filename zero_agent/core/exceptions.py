"""ZeroAgent 异常体系.

异常层级:
    ZeroAgentError (基类)
    ├── ConfigError       — 配置错误，不可恢复
    ├── LLMError          — LLM API 调用失败，可恢复（可重试/回退）
    └── ToolError         — 工具执行失败，转为结构化数据回传 LLM 自纠正
"""


class ZeroAgentError(Exception):
    """ZeroAgent 所有异常的基类.

    Attributes:
        recoverable: 是否可恢复。True 表示调用方可重试或回退.
    """

    def __init__(self, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable


class ConfigError(ZeroAgentError):
    """配置无效或不完整，致命错误，必须修正配置后才能继续."""


class LLMError(ZeroAgentError):
    """LLM API 调用失败，默认可恢复.

    由 Session 层在 HTTP 错误、流中断、超时等场景抛出.
    litellm Router 可配置 fallback chains 自动重试或切换后端.
    """

    def __init__(self, message: str, *, recoverable: bool = True) -> None:
        super().__init__(message, recoverable=recoverable)


class ToolError(ZeroAgentError):
    """工具执行失败.

    由工具 handler 抛出，BaseHandler.dispatch() 捕获后转为结构化
    错误数据作为 tool_result 回传给 LLM，让模型自行纠正参数.
    """
