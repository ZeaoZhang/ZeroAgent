"""事件驱动的插件钩子系统.

提供 8 个标准事件钩子点，支持插件注册、触发和自动发现。
参照 GenericAgent 的 plugins/hooks.py，设计为干净的内置模块。
"""

from __future__ import annotations

import importlib
import os
from typing import Any, Callable, Dict, List


# 标准钩子事件
EVENT_AGENT_BEFORE = "agent_before"
EVENT_TURN_BEFORE = "turn_before"
EVENT_LLM_BEFORE = "llm_before"
EVENT_LLM_AFTER = "llm_after"
EVENT_TURN_AFTER = "turn_after"
EVENT_TOOL_BEFORE = "tool_before"
EVENT_TOOL_AFTER = "tool_after"
EVENT_AGENT_AFTER = "agent_after"

ALL_EVENTS = [
    EVENT_AGENT_BEFORE,
    EVENT_TURN_BEFORE,
    EVENT_LLM_BEFORE,
    EVENT_LLM_AFTER,
    EVENT_TURN_AFTER,
    EVENT_TOOL_BEFORE,
    EVENT_TOOL_AFTER,
    EVENT_AGENT_AFTER,
]


class HookSystem:
    """事件驱动的钩子注册和触发系统.

    插件通过 register(event, callback) 注册回调函数，
    AgentLoop 在关键生命周期点调用 trigger() 触发所有已注册的回调.

    用法:
        hooks = HookSystem()
        hooks.register("tool_before", lambda ctx: print(ctx["tool_name"]))
        hooks.trigger("tool_before", {"tool_name": "file_read", "args": {...}})

    Attributes:
        _handlers: 事件名 → 回调函数列表的映射.
    """

    def __init__(self) -> None:
        """初始化空的钩子系统."""
        self._handlers: Dict[str, List[Callable[[dict], None]]] = {
            event: [] for event in ALL_EVENTS
        }

    def register(self, event: str, callback: Callable[[dict], None]) -> None:
        """注册一个事件回调.

        Args:
            event: 事件名，必须是 ALL_EVENTS 中的值.
            callback: 回调函数，接受一个上下文字典作为参数.

        Raises:
            ValueError: 事件名不合法.
        """
        if event not in self._handlers:
            raise ValueError(
                f"未知的钩子事件: {event}，可用事件: {ALL_EVENTS}"
            )
        self._handlers[event].append(callback)

    def trigger(self, event: str, context: dict) -> None:
        """触发一个事件，调用所有已注册的回调.

        每个回调在 try/except 中执行，单个回调失败不影响其他回调.

        Args:
            event: 事件名.
            context: 传递给回调的上下文字典.
        """
        for callback in self._handlers.get(event, []):
            try:
                callback(context)
            except Exception:
                pass

    def discover_and_load(self, plugins_dir: str) -> int:
        """自动发现并加载插件目录中的 Python 模块.

        扫描 plugins_dir 下的所有 .py 文件（排除 __init__.py 和以 _ 开头的），
        导入每个模块。模块可以在顶层调用 hooks.register() 来注册自己.

        Args:
            plugins_dir: 插件目录路径.

        Returns:
            成功加载的插件数量.
        """
        if not os.path.isdir(plugins_dir):
            return 0

        loaded = 0
        for filename in sorted(os.listdir(plugins_dir)):
            if filename.startswith("_") or not filename.endswith(".py"):
                continue
            module_name = filename[:-3]  # 去掉 .py
            module_path = os.path.join(plugins_dir, filename)

            # 将 hooks 实例注入插件的全局命名空间
            spec = importlib.util.spec_from_file_location(
                f"zero_plugin_{module_name}", module_path,
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            module.hooks = self
            try:
                spec.loader.exec_module(module)
                loaded += 1
            except Exception:
                pass

        return loaded

    def unregister(self, event: str, callback: Callable[[dict], None]) -> bool:
        """移除指定事件的回调函数.

        Args:
            event: 事件名.
            callback: 要移除的回调函数.

        Returns:
            True 如果找到并移除了回调，False 如果回调未注册.
        """
        if event not in self._handlers:
            return False
        try:
            self._handlers[event].remove(callback)
            return True
        except ValueError:
            return False

    def clear(self, event: Optional[str] = None) -> None:
        """清空指定事件（或全部事件）的回调.

        Args:
            event: 事件名. None 时清空所有事件的回调.
        """
        if event:
            if event in self._handlers:
                self._handlers[event].clear()
        else:
            for handlers in self._handlers.values():
                handlers.clear()

    def has(
        self,
        event: str,
        callback: Optional[Callable[[dict], None]] = None,
    ) -> bool:
        """检查钩子是否存在.

        Args:
            event: 事件名.
            callback: 若提供，检查该具体回调是否已注册. 若不提供，检查事件是否有至少一个回调.

        Returns:
            True 如果事件/回调存在.
        """
        if event not in self._handlers:
            return False
        if callback is None:
            return len(self._handlers[event]) > 0
        return callback in self._handlers[event]

    @property
    def registered_events(self) -> Dict[str, int]:
        """返回每个已注册事件的回调数量.

        Returns:
            事件名 → 回调计数的字典.
        """
        return {
            event: len(cbs)
            for event, cbs in self._handlers.items()
            if cbs
        }
