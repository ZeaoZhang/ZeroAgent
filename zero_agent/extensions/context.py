"""面向插件的稳定 API（ExtensionContext）.

插件只通过这个对象与框架交互。它刻意保持小：注册工具、订阅三类扩展点、
贡献提示词块，其余能力由这几类组合而来。

契约要点:

- 通知型回调的返回值被忽略，异常被记录后继续。
- 变换型回调返回新值，链式传递；回调返回 None 表示不干预。
- 门控型回调返回 ``None`` 表示放行，返回 ``ctx.block(reason)`` 表示否决；
  门控回调自身抛异常时，运行时按 fail-closed 处理，同样否决。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Generator, List, Optional

from zero_agent.extensions.events import ApprovalRequired, ExtensionVeto, PromptBlock
from zero_agent.tools.registry import ToolDefinition

_logger = logging.getLogger("zero_agent.extensions")


def _wrap_tool(fn: Callable[[Dict[str, Any]], Any], name: str) -> Callable[..., Generator]:
    """把插件里 ``fn(args) -> Any`` 适配成 registry 需要的 generator handler.

    扩展工具异常必须隔离：插件代码抛错时返回结构化的 error 结果，让模型
    看到失败并换策略，而不是让异常冒到 AgentLoop 把整个任务判为 FAILED。
    不用 except BaseException，保留 KeyboardInterrupt/SystemExit 的语义。

    Args:
        fn: 插件提供的工具函数，接受参数字典并返回结果.
        name: 工具名称，用于状态提示.

    Returns:
        符合 ``ToolHandler`` 签名的 generator 函数.
    """

    def handler(
        args: Dict[str, Any],
        _response: Any,
        _handler: Any,
    ) -> Generator[str, None, Any]:
        yield f"[{name}] running ...\n"
        try:
            return fn(args)
        except Exception as exc:  # noqa: BLE001 - 插件失败不得中止任务
            _logger.exception("扩展工具 %s 执行失败", name)
            return {
                "status": "error",
                "error": f"extension tool {name} failed: {exc}",
                "exception": exc.__class__.__name__,
            }

    return handler


class ExtensionContext:
    """一个插件的注册入口.

    Attributes:
        name: 插件名（文件名或目录名）.
        runtime: 持有注册表的运行时.
    """

    def __init__(self, name: str, runtime: Any) -> None:
        """初始化上下文.

        Args:
            name: 插件名.
            runtime: ExtensionRuntime 实例.
        """
        self.name = name
        self.runtime = runtime

    # ---- 注册工具 -------------------------------------------------------

    def tool(
        self,
        *,
        name: str,
        description: str,
        parameters: Optional[Dict[str, Any]] = None,
        evidence_kind: Optional[str] = None,
        category: str = "extension",
        promotes_task_state: bool = True,
        effects: tuple[str, ...] = ("unknown",),
        write_path_keys: tuple[str, ...] = (),
        execute_code_keys: tuple[str, ...] = (),
    ) -> Callable[[Callable[[Dict[str, Any]], Any]], Callable[[Dict[str, Any]], Any]]:
        """注册一个模型可调用的工具.

        Args:
            name: 工具名称.
            description: 工具描述，进入 tool schema.
            parameters: JSON Schema 参数定义.
            evidence_kind: 证据类型；声明后成功结果可被 complete_task 引用.
            category: 工具分类.
            promotes_task_state: 成功分发前是否推进 OPEN → EXECUTING.
            effects: 副作用声明；未声明时策略层按 unknown 要求审批.
            write_path_keys: 参数字典中指向写入目标的键.
            execute_code_keys: 参数字典中承载待执行代码的键.

        Returns:
            装饰器，返回原函数以便直接调用.
        """

        def decorator(fn: Callable[[Dict[str, Any]], Any]) -> Callable[[Dict[str, Any]], Any]:
            definition = ToolDefinition(
                name=name,
                description=description,
                parameters=parameters or {"type": "object", "properties": {}},
                handler=_wrap_tool(fn, name),
                category=category,
                promotes_task_state=promotes_task_state,
                evidence_kind=evidence_kind,  # type: ignore[arg-type]
                effects=tuple(effects),
                write_path_keys=tuple(write_path_keys),
                execute_code_keys=tuple(execute_code_keys),
            )
            self.runtime.register_tool(self.name, definition)
            return fn

        return decorator

    # ---- 通知型扩展点 ---------------------------------------------------

    def on_agent_start(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅 agent 开始事件（通知型）."""
        self.runtime.subscribe_notify("agent_start", self.name, callback)
        return callback

    def on_agent_end(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅 agent 结束事件（通知型）."""
        self.runtime.subscribe_notify("agent_end", self.name, callback)
        return callback

    def on_turn_start(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅每轮开始事件（通知型）."""
        self.runtime.subscribe_notify("turn_start", self.name, callback)
        return callback

    def on_turn_end(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅每轮结束事件（通知型）."""
        self.runtime.subscribe_notify("turn_end", self.name, callback)
        return callback

    def on_llm_before(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅模型请求发出前事件（通知型，只观察）."""
        self.runtime.subscribe_notify("llm_before", self.name, callback)
        return callback

    def on_llm_after(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅模型响应完成事件（通知型，只观察，不能改写响应）."""
        self.runtime.subscribe_notify("llm_after", self.name, callback)
        return callback

    def on_tool_before(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅工具执行前事件（通知型，只观察）.

        该事件在门控与真实执行之前触发；回调不能改变执行结果。
        """
        self.runtime.subscribe_notify("tool_before", self.name, callback)
        return callback

    def on_tool_after(self, callback: Callable[..., None]) -> Callable[..., None]:
        """订阅工具结束事件（通知型，只观察）."""
        self.runtime.subscribe_notify("tool_after", self.name, callback)
        return callback

    # ---- 变换型扩展点 ---------------------------------------------------

    def on_llm_request(self, callback: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """订阅模型请求变换：可改 messages / tools / system_prompt / headers."""
        self.runtime.subscribe_llm_request(self.name, callback)
        return callback

    def on_tool_result(self, callback: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """订阅工具结果变换：返回新结果，返回 None 表示不干预."""
        self.runtime.subscribe_tool_result(self.name, callback)
        return callback

    def prompt_block(
        self,
        name: str,
        factory: Callable[[], str],
    ) -> Callable[[], str]:
        """贡献一段系统提示词，``factory`` 在每次组装提示词时调用.

        Args:
            name: 块名称.
            factory: 返回文本的回调；返回空串表示本轮不贡献.

        Returns:
            原 factory，便于直接复用.
        """
        self.runtime.subscribe_prompt_block(self.name, name, factory)
        return factory

    # ---- 门控型扩展点 ---------------------------------------------------

    def on_tool_call(self, callback: Callable[[Any], Any]) -> Callable[[Any], Any]:
        """订阅工具调用门控：放行返回 None，否决返回 ``ctx.block(...)``."""
        self.runtime.subscribe_tool_call(self.name, callback)
        return callback

    # ---- 辅助 -----------------------------------------------------------

    @staticmethod
    def block(reason: str) -> ExtensionVeto:
        """构造否决返回值.

        Args:
            reason: 否决原因，会展示给模型.

        Returns:
            ExtensionVeto 实例；门控回调直接 return 即可.
        """
        return ExtensionVeto(reason)

    def confirm(
        self,
        question: str,
        candidates: Optional[List[str]] = None,
        scope: str = "",
    ) -> ApprovalRequired:
        """请求人类批准这次工具调用.

        在门控回调里直接 ``return ctx.confirm("原因")``。运行时会暂停任务、
        向用户提问；批准后只放行与本次参数完全一致的一次调用。

        传 ``scope`` 时额外登记一个「批量授权键」：同一任务内、同一 scope 的
        后续调用在通行证有效期内不再重复询问。用来避免模型把同一个文件反复
        改写（例如内容只差一个换行）时不断弹审批。

        Args:
            question: 展示给用户的说明，讲清要做什么、影响是什么.
            candidates: 可选快捷选项，默认由运行时提供「批准 / 拒绝」.
            scope: 批量授权键，例如「工具名 + 目标路径」；空串表示仅单次.

        Returns:
            ApprovalRequired，交给运行时处理.
        """
        return ApprovalRequired(reason=question, candidates=candidates, scope=scope)


# 供类型标注使用，避免循环导入
PromptBlockType = PromptBlock
