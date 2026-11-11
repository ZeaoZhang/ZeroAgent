"""插件运行时：发现、加载、事件分发与注册归属.

这是扩展体系的唯一入口。调用方（``ZeroAgent`` / ``BaseHandler`` / ``AgentLoop``）
只通过 :class:`ExtensionRuntime` 暴露的几个方法接入扩展能力，
不直接接触插件模块。

设计要点（见 ``docs/extension-design.md`` 第六节）:

- 目录即插件，没有清单文件；只扫描一层。
- 三类扩展点语义：通知、变换、门控。门控 fail-closed。
- 注册有归属，可整体撤销；registry 重建后能重放。
"""

from __future__ import annotations

import importlib.util
import logging
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import (
    ExtensionVeto,
    LlmRequest,
    PromptBlock,
    ToolCall,
    ToolResult,
)
from zero_agent.tools.registry import ToolDefinition, ToolRegistry

_logger = logging.getLogger("zero_agent.extensions")

DEFAULT_EXTENSION_HOME = "~/.zeroagent"

NotifyCallback = Callable[[Dict[str, Any]], None]
GateCallback = Callable[[ToolCall], Any]
TransformCallback = Callable[[Any], Any]
PromptFactory = Callable[[], str]


def _default_home() -> Path:
    """返回默认扩展根目录."""
    return Path(DEFAULT_EXTENSION_HOME).expanduser()


def discover(home: Optional[Path | str] = None) -> List[Tuple[str, Path, bool]]:
    """发现扩展目录下的插件.

    只扫描一层，不递归。规则:

    - ``*.py``（不以 ``_`` 或 ``.`` 开头）为单文件插件.
    - 含 ``__init__.py`` 的子目录为目录插件.
    - ``_`` 开头、``.`` 开头与 ``__pycache__`` 一律跳过.

    Args:
        home: 扩展根目录，默认 ``~/.zeroagent``.

    Returns:
        ``(插件名, 入口路径, 是否目录插件)`` 列表，按名称排序.
    """
    root = Path(home).expanduser() if home is not None else _default_home()
    extensions_dir = root / "extensions"
    if not extensions_dir.is_dir():
        return []

    found: List[Tuple[str, Path, bool]] = []
    for entry in sorted(extensions_dir.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(("_", ".")):
            continue
        if entry.is_file() and entry.suffix == ".py":
            found.append((entry.stem, entry, False))
        elif entry.is_dir():
            init_file = entry / "__init__.py"
            if init_file.is_file():
                found.append((entry.name, init_file, True))
    return found


def _import_plugin(name: str, path: Path, is_package: bool) -> Any:
    """按文件路径导入一个插件模块.

    Args:
        name: 插件名.
        path: 入口文件路径.
        is_package: 是否为目录插件（需要包语义以支持相对导入）.

    Returns:
        已执行的模块对象.

    Raises:
        ImportError: 模块无法构造或执行失败.
    """
    module_name = "zero_extension_" + re.sub(r"\W", "_", name)
    if is_package:
        spec = importlib.util.spec_from_file_location(
            module_name,
            path,
            submodule_search_locations=[str(path.parent)],
        )
    else:
        spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法为扩展 {name} 构造模块规格")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class ExtensionRuntime:
    """插件运行时.

    Attributes:
        registry: 当前使用的工具注册表.
        home: 扩展根目录.
        hooks: 兼容旧版 HookSystem 的通知出口，可为 None.
    """

    def __init__(
        self,
        *,
        registry: ToolRegistry,
        home: Optional[Path | str] = None,
        hooks: Any = None,
        config: Any = None,
    ) -> None:
        """初始化运行时（不加载任何插件）.

        Args:
            registry: 已包含内置工具的工具注册表.
            home: 扩展根目录，默认 ``~/.zeroagent``.
            hooks: 可选的旧版 HookSystem，用于转发通知.
            config: 可选的 AgentConfig，传给插件上下文.
        """
        self.registry = registry
        self.home = Path(home).expanduser() if home is not None else _default_home()
        self.hooks = hooks
        self.config = config

        self._loaded: Dict[str, Any] = {}
        self._notify: Dict[str, List[Tuple[str, NotifyCallback]]] = {}
        self._tool_call: List[Tuple[str, GateCallback]] = []
        self._tool_result: List[Tuple[str, TransformCallback]] = []
        self._llm_request: List[Tuple[str, TransformCallback]] = []
        self._prompt_blocks: List[Tuple[str, str, PromptFactory]] = []
        self._tool_defs: List[Tuple[str, ToolDefinition]] = []

    # ---- 加载与卸载 -----------------------------------------------------

    def load_all(self) -> int:
        """加载全部可发现的插件.

        单个插件失败只记录日志并跳过，不影响其他插件与启动流程.

        Returns:
            成功加载的插件数量.
        """
        loaded = 0
        for name, path, is_package in discover(self.home):
            try:
                self._load_one(name, path, is_package)
                loaded += 1
            except Exception as exc:  # noqa: BLE001 - 插件失败不得阻断启动
                _logger.warning("扩展 %s 加载失败，已跳过: %s", name, exc)
        return loaded

    def _load_one(self, name: str, path: Path, is_package: bool) -> None:
        """加载单个插件并调用其 setup.

        Args:
            name: 插件名.
            path: 入口文件.
            is_package: 是否目录插件.

        Raises:
            Exception: 模块导入或 setup 抛出的任何异常，由调用方记录跳过.
        """
        module = _import_plugin(name, path, is_package)
        setup = getattr(module, "setup", None)
        if not callable(setup):
            raise TypeError(f"扩展 {name} 缺少 setup(ctx) 入口")
        context = ExtensionContext(name, self)
        setup(context)
        self._loaded[name] = module

    def release(self) -> None:
        """卸载全部插件，并撤销它们注册的工具."""
        for owner in list(self._loaded):
            self.registry.release_owner(owner)
        self._loaded.clear()
        self._notify.clear()
        self._tool_call.clear()
        self._tool_result.clear()
        self._llm_request.clear()
        self._prompt_blocks.clear()
        self._tool_defs.clear()

    def attach_registry(self, registry: ToolRegistry) -> None:
        """在 registry 被重建后重放全部插件工具注册.

        ``AgentConfig`` 热重载可能在语言变化时重建 registry，若不重放，
        插件工具会随旧 registry 一起消失。

        Args:
            registry: 新的工具注册表.
        """
        self.registry = registry
        for owner, definition in self._tool_defs:
            registry.register(definition, owner=owner)

    # ---- 注册回调 -------------------------------------------------------

    def register_tool(self, owner: str, definition: ToolDefinition) -> None:
        """注册插件工具，并记录冲突与归属.

        Args:
            owner: 插件名.
            definition: 工具定义.
        """
        existing_owner = self.registry.owner_of(definition.name)
        if definition.name in self.registry._tools and existing_owner != owner:
            _logger.warning(
                "扩展 %s 覆盖工具 %s（原归属: %s）",
                owner,
                definition.name,
                existing_owner or "builtin",
            )
        # 避免同一插件重复加载时重复记录
        self._tool_defs = [
            (item_owner, item_def)
            for item_owner, item_def in self._tool_defs
            if not (item_owner == owner and item_def.name == definition.name)
        ]
        self._tool_defs.append((owner, definition))
        self.registry.register(definition, owner=owner)

    def subscribe_notify(
        self,
        event: str,
        owner: str,
        callback: NotifyCallback,
    ) -> None:
        """订阅通知型事件."""
        self._notify.setdefault(event, []).append((owner, callback))

    def subscribe_tool_call(self, owner: str, callback: GateCallback) -> None:
        """订阅门控型事件."""
        self._tool_call.append((owner, callback))

    def subscribe_tool_result(self, owner: str, callback: TransformCallback) -> None:
        """订阅工具结果变换."""
        self._tool_result.append((owner, callback))

    def subscribe_llm_request(self, owner: str, callback: TransformCallback) -> None:
        """订阅模型请求变换."""
        self._llm_request.append((owner, callback))

    def subscribe_prompt_block(
        self,
        owner: str,
        name: str,
        factory: PromptFactory,
    ) -> None:
        """订阅提示词块."""
        self._prompt_blocks.append((owner, name, factory))

    # ---- 事件分发 -------------------------------------------------------

    def notify(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """分发通知型事件；单个回调失败不影响其他回调.

        Args:
            event: 事件名.
            payload: 事件负载.
        """
        data = payload or {}
        for owner, callback in self._notify.get(event, []):
            try:
                callback(data)
            except Exception as exc:  # noqa: BLE001 - 通知失败只记录
                _logger.warning("扩展 %s 的 %s 回调失败: %s", owner, event, exc)

    def run_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[str]:
        """执行工具调用门控.

        门控回调可以直接修改 ``args``（原地），也可以否决本次调用。
        **fail-closed**：门控自身抛异常时返回拒绝原因，而不是放行。

        Args:
            tool_name: 工具名.
            args: 工具参数，可被回调原地修改.

        Returns:
            拒绝原因；``None`` 表示全部放行.
        """
        if not self._tool_call:
            return None
        call = ToolCall(tool_name=tool_name, args=args)
        for owner, callback in self._tool_call:
            try:
                verdict = callback(call)
            except ExtensionVeto as veto:
                return veto.reason
            except Exception as exc:  # noqa: BLE001 - 门控必须 fail-closed
                _logger.warning("门控 %s 执行失败，按 fail-closed 拒绝: %s", owner, exc)
                return f"门控 {owner} 执行失败，本次调用已被拒绝"
            if isinstance(verdict, ExtensionVeto):
                return verdict.reason
            if verdict is False:
                return f"被扩展 {owner} 拒绝"
        return None

    def run_tool_result(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Any,
    ) -> Any:
        """执行工具结果变换链.

        Args:
            tool_name: 工具名.
            args: 工具参数.
            result: 原始结果.

        Returns:
            变换后的结果；回调返回 ``None`` 视为不干预.
        """
        current = result
        for owner, callback in self._tool_result:
            try:
                updated = callback(ToolResult(tool_name, args, current))
            except Exception as exc:  # noqa: BLE001 - 变换失败退回原值
                _logger.warning("扩展 %s 的结果变换失败，已忽略: %s", owner, exc)
                continue
            if updated is None:
                continue
            if isinstance(updated, ToolResult):
                current = updated.result
            else:
                current = updated
        return current

    def run_llm_request(self, request: LlmRequest) -> LlmRequest:
        """执行模型请求变换链.

        Args:
            request: 原始请求.

        Returns:
            变换后的请求.
        """
        current = request
        for owner, callback in self._llm_request:
            try:
                updated = callback(current)
            except Exception as exc:  # noqa: BLE001 - 变换失败退回原值
                _logger.warning("扩展 %s 的请求变换失败，已忽略: %s", owner, exc)
                continue
            if updated is None:
                continue
            if isinstance(updated, LlmRequest):
                current = updated
            else:
                _logger.warning("扩展 %s 的请求变换返回了非法类型，已忽略", owner)
        return current

    def collect_prompt_blocks(self) -> List[PromptBlock]:
        """收集当前生效的插件提示词块.

        Returns:
            提示词块列表；文本为空的块被跳过.
        """
        blocks: List[PromptBlock] = []
        for owner, name, factory in self._prompt_blocks:
            try:
                text = factory()
            except Exception as exc:  # noqa: BLE001 - 单块失败不影响其他块
                _logger.warning("扩展 %s 的提示词块 %s 生成失败: %s", owner, name, exc)
                continue
            if text is None:
                continue
            rendered = str(text).strip()
            if rendered:
                blocks.append(PromptBlock(name=f"{owner}:{name}", text=rendered))
        return blocks

    # ---- 查询 -----------------------------------------------------------

    @property
    def loaded_names(self) -> List[str]:
        """返回已加载插件名列表."""
        return sorted(self._loaded)
