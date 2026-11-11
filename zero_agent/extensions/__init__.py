"""扩展体系：插件运行时与内置扩展.

对外的稳定入口只有 :class:`ExtensionRuntime` 与 :class:`ExtensionContext`。
插件作者只需要在 ``~/.zeroagent/extensions/`` 下提供 ``setup(ctx)``。
"""

from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import (
    ExtensionVeto,
    LlmRequest,
    PromptBlock,
    ToolCall,
    ToolResult,
)
from zero_agent.extensions.runtime import ExtensionRuntime, discover

__all__ = [
    "ExtensionContext",
    "ExtensionRuntime",
    "ExtensionVeto",
    "LlmRequest",
    "PromptBlock",
    "ToolCall",
    "ToolResult",
    "discover",
]
