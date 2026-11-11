"""扩展点契约：通知、变换、门控三类语义的类型定义.

本模块只定义数据形状与异常，不持有任何运行时状态。
插件作者通常不需要直接 import 这里，运行时会把实例传给回调。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ExtensionVeto(Exception):
    """门控否决一次工具调用.

    门控属于 fail-closed 语义：如果门控回调无法做出判断（例如自身抛异常），
    运行时同样会否决这次调用，而不是放行。
    """

    def __init__(self, reason: str) -> None:
        """构造否决.

        Args:
            reason: 展示给模型和用户的否决原因.
        """
        super().__init__(reason)
        self.reason = reason


@dataclass
class ToolCall:
    """一次工具调用（门控入参，可原地修改 args）.

    Attributes:
        tool_name: 工具名称.
        args: 工具参数；门控回调可以原地修改.
        owner: 发起这次分发的插件归属；核心分发为空字符串.
    """

    tool_name: str
    args: Dict[str, Any]
    owner: str = ""


@dataclass
class ToolResult:
    """一次工具结果（变换入参）.

    Attributes:
        tool_name: 工具名称.
        args: 工具参数.
        result: handler 返回的原始结果，变换回调可以返回新值.
    """

    tool_name: str
    args: Dict[str, Any]
    result: Any


@dataclass
class LlmRequest:
    """一次模型请求（变换入参）.

    Attributes:
        model: 当前模型名.
        messages: 消息列表；变换回调可以返回新的列表.
        tools: 工具 schema 列表.
        system_prompt: 系统提示词.
        headers: 额外请求头.
    """

    model: str
    messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    system_prompt: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)

    def replace(
        self,
        *,
        messages: Optional[List[Dict[str, Any]]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        system_prompt: Optional[str] = None,
    ) -> "LlmRequest":
        """返回替换字段后的新请求，保留未提及的字段.

        Args:
            messages: 新消息列表.
            tools: 新工具 schema 列表.
            system_prompt: 新系统提示词.

        Returns:
            新的 LlmRequest 实例.
        """
        return LlmRequest(
            model=self.model,
            messages=self.messages if messages is None else messages,
            tools=self.tools if tools is None else tools,
            system_prompt=(
                self.system_prompt if system_prompt is None else system_prompt
            ),
            headers=dict(self.headers),
        )


@dataclass
class PromptBlock:
    """插件贡献的一段系统提示词.

    Attributes:
        name: 块名称，用于排序、去重与日志.
        text: 块正文.
    """

    name: str
    text: str


@dataclass
class ApprovalRequired:
    """门控要求人类批准这次调用.

    门控回调直接 ``return ctx.confirm(...)``。运行时会暂停任务、向用户提问，
    并用「工具名 + 参数」的指纹把批准绑定到这一次具体操作上。
    """

    reason: str
    candidates: Optional[List[str]] = None


@dataclass
class ToolCallVerdict:
    """门控的最终判定.

    Attributes:
        action: ``allow`` 放行；``block`` 拒绝；``approve`` 需要人类批准.
        reason: 拒绝或审批的原因，会展示给模型与用户.
        fingerprint: 审批指纹；只有 ``approve`` 时有值.
    """

    action: Literal["allow", "block", "approve"]
    reason: str = ""
    fingerprint: str = ""

    @property
    def is_allow(self) -> bool:
        """是否放行."""
        return self.action == "allow"

    @property
    def is_block(self) -> bool:
        """是否拒绝."""
        return self.action == "block"

    @property
    def needs_approval(self) -> bool:
        """是否需要人类批准."""
        return self.action == "approve"
