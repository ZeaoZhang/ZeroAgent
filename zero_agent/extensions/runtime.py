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

import hashlib
import importlib.util
import json
import logging
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.events import (
    ApprovalRequired,
    ExtensionVeto,
    LlmRequest,
    PromptBlock,
    ToolCall,
    ToolCallVerdict,
    ToolResult,
)
from zero_agent.tools.registry import ToolDefinition, ToolRegistry

_logger = logging.getLogger("zero_agent.extensions")

DEFAULT_EXTENSION_HOME = "~/.zeroagent"

# 旧 HookSystem 的 hook 事件 → 扩展通知事件（设计第六节的通知类）
# 由 AgentLoop 在触发 hook 时统一转发，插件通过 ctx.on_agent_start 等订阅
_HOOK_TO_NOTIFY: Dict[str, str] = {
    "agent_before": "agent_start",
    "turn_before": "turn_start",
    "turn_after": "turn_end",
    "agent_after": "agent_end",
    "llm_after": "llm_after",
}

# dispatch 注入的内部参数键，不参与审批指纹
_INTERNAL_ARG_KEYS = frozenset({"_index", "_tool_num"})

NotifyCallback = Callable[[Dict[str, Any]], None]
GateCallback = Callable[[ToolCall], Any]
TransformCallback = Callable[[Any], Any]
PromptFactory = Callable[[], str]

# 单个提示词块的字符上限；超限截断并告警（设计第十四节的风险缓解）
_MAX_PROMPT_BLOCK_CHARS = 16_000


def _default_home() -> Path:
    """返回默认扩展根目录."""
    return Path(DEFAULT_EXTENSION_HOME).expanduser()


def approval_fingerprint(tool_name: str, args: Dict[str, Any]) -> str:
    """计算一次调用的审批指纹.

    指纹绑定工具名与全部业务参数（忽略 ``_index`` 等内部键），因此模型只要
    改了目标或内容，就需要重新请求批准。

    Args:
        tool_name: 工具名.
        args: 工具参数.

    Returns:
        短哈希指纹.
    """
    payload = {
        key: value
        for key, value in (args or {}).items()
        if key not in _INTERNAL_ARG_KEYS
    }
    blob = json.dumps(
        {"tool": tool_name, "args": payload},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


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
        self._teardowns: Dict[str, Callable[[], None]] = {}
        self._notify: Dict[str, List[Tuple[str, NotifyCallback]]] = {}
        self._tool_call: List[Tuple[str, GateCallback]] = []
        self._tool_result: List[Tuple[str, TransformCallback]] = []
        self._llm_request: List[Tuple[str, TransformCallback]] = []
        self._prompt_blocks: List[Tuple[str, str, PromptFactory]] = []
        self._tool_defs: List[Tuple[str, ToolDefinition]] = []
        # 内置扩展（permission_gate 等）的归属；它们不经 load_all 加载，
        # 单独记录才能在 release() 时一并撤销
        self._builtin_owners: List[str] = []
        self._approvals: Dict[str, Tuple[float, str]] = {}

    # ---- 加载与卸载 -----------------------------------------------------

    def load_all(self) -> int:
        """加载全部可发现的插件.

        单个插件失败只记录日志并跳过，不影响其他插件与启动流程.

        Returns:
            成功加载的插件数量.
        """
        loaded = 0
        try:
            discovered = discover(self.home)
        except Exception as exc:  # noqa: BLE001 - 目录不可读不得阻断启动
            # 这里必须隔离：若异常冒到 ZeroAgent._init_extensions，
            # 整个运行时（含 permission_gate 安全门）都会被跳过。
            _logger.warning("扩展目录扫描失败，已跳过: %s", exc)
            return 0
        for name, path, is_package in discovered:
            try:
                self._load_one(name, path, is_package)
                loaded += 1
            except (Exception, SystemExit) as exc:  # noqa: BLE001 - 插件失败不得阻断启动
                _logger.warning("扩展 %s 加载失败，已跳过: %s", name, exc)
        return loaded

    def _load_one(self, name: str, path: Path, is_package: bool) -> None:
        """加载单个插件并调用其 setup.

        setup 中途失败时必须撤销它已经产生的全部注册，否则会留下无法卸载的
        工具或门控（门控还可能 fail-closed 拒绝一切调用）。

        Args:
            name: 插件名.
            path: 入口文件.
            is_package: 是否目录插件.

        Raises:
            Exception: 模块导入或 setup 抛出的任何异常，由调用方记录跳过.
        """
        context = ExtensionContext(name, self)
        try:
            module = _import_plugin(name, path, is_package)
            setup = getattr(module, "setup", None)
            if not callable(setup):
                raise TypeError(f"扩展 {name} 缺少 setup(ctx) 入口")
            teardown = setup(context)
            if callable(teardown):
                self._teardowns[name] = teardown
        except (Exception, SystemExit):
            self._purge_owner(name)
            sys.modules.pop("zero_extension_" + re.sub(r"\W", "_", name), None)
            raise
        self._loaded[name] = module

    def _purge_owner(self, owner: str) -> None:
        """彻底清除某个归属留下的全部注册（用于加载失败与卸载）.

        Args:
            owner: 归属名.
        """
        self.registry.release_owner(owner)
        self._tool_defs = [
            item for item in self._tool_defs if item[0] != owner
        ]
        self._tool_call = [
            item for item in self._tool_call if item[0] != owner
        ]
        self._tool_result = [
            item for item in self._tool_result if item[0] != owner
        ]
        self._llm_request = [
            item for item in self._llm_request if item[0] != owner
        ]
        self._prompt_blocks = [
            item for item in self._prompt_blocks if item[0] != owner
        ]
        for event in list(self._notify):
            self._notify[event] = [
                item for item in self._notify[event] if item[0] != owner
            ]
            if not self._notify[event]:
                del self._notify[event]

    def register_teardown(self, owner: str, callback: Callable[[], None]) -> None:
        """登记一个卸载回调（内置扩展用；插件可通过 setup 返回值登记）.

        Args:
            owner: 归属名.
            callback: 卸载时调用的无参函数.
        """
        self._teardowns[owner] = callback

    def track_owner(self, owner: str) -> None:
        """登记一个内置扩展的注册归属，使其注册能被 :meth:`release` 撤销.

        内置扩展不像外部插件那样经过 ``_load_one``，不会进入 ``_loaded``，
        因此必须单独登记，否则 ``release()`` 后它们的工具与回调会残留。

        Args:
            owner: 归属名（例如 ``permission_gate``）.
        """
        if owner not in self._builtin_owners:
            self._builtin_owners.append(owner)

    def unregister_owner(self, owner: str) -> None:
        """撤销某个归属的全部注册（内置扩展安装失败时清理半成品）.

        Args:
            owner: 归属名.
        """
        self._purge_owner(owner)
        if owner in self._builtin_owners:
            self._builtin_owners.remove(owner)

    def release(self) -> None:
        """卸载全部插件与内置扩展，撤销注册并执行清理回调."""
        for owner, teardown in list(self._teardowns.items()):
            try:
                teardown()
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响卸载
                _logger.warning("扩展 %s 的清理回调失败: %s", owner, exc)
        self._teardowns.clear()

        for owner in [*self._loaded, *self._builtin_owners]:
            self._purge_owner(owner)
        self._loaded.clear()
        self._builtin_owners.clear()
        self._notify.clear()
        self._tool_call.clear()
        self._tool_result.clear()
        self._llm_request.clear()
        self._prompt_blocks.clear()
        self._tool_defs.clear()
        self._approvals.clear()

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

        每个回调收到的是负载的浅拷贝，避免一个回调的修改污染后续回调。

        Args:
            event: 事件名.
            payload: 事件负载.
        """
        data = payload if isinstance(payload, dict) else {}
        for owner, callback in self._notify.get(event, []):
            try:
                callback(dict(data))
            except Exception as exc:  # noqa: BLE001 - 通知失败只记录
                _logger.warning("扩展 %s 的 %s 回调失败: %s", owner, event, exc)

    def dispatch_hook_notify(self, hook_event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        """把旧 HookSystem 的生命周期 hook 转发成扩展通知事件.

        ``AgentLoop`` 在每个 hook 点调用本方法，从而让
        ``ctx.on_agent_start / on_turn_start / on_turn_end / on_agent_end /
        on_llm_after`` 真正被触发。

        Args:
            hook_event: 旧 hook 事件名（agent_before 等）.
            payload: 该 hook 的上下文.
        """
        event = _HOOK_TO_NOTIFY.get(hook_event)
        if event is None:
            return
        self.notify(event, payload)

    def evaluate_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        task_id: str = "",
    ) -> ToolCallVerdict:
        """执行工具调用门控，返回三态判定.

        门控回调可以直接修改 ``args``（原地），返回 ``ctx.block(...)`` 拒绝，
        返回 ``ctx.confirm(...)`` 要求人类批准。**fail-closed**：门控自身异常
        时返回拒绝，而不是放行。

        审批指纹绑定「工具名 + 参数」，并用后即焚：批准只对完全相同的一次
        调用有效。

        Args:
            tool_name: 工具名.
            args: 工具参数，可被回调原地修改.
            task_id: 当前任务标识，用于限定审批通行证的作用域.

        Returns:
            ToolCallVerdict.
        """
        if not self._tool_call:
            return ToolCallVerdict("allow")

        call = ToolCall(tool_name=tool_name, args=args)
        approval_reason = ""
        approval_fingerprint_value = ""
        for owner, callback in self._tool_call:
            try:
                result = callback(call)
            except ExtensionVeto as veto:
                return ToolCallVerdict("block", veto.reason)
            except Exception as exc:  # noqa: BLE001 - 门控必须 fail-closed
                _logger.warning("门控 %s 执行失败，按 fail-closed 拒绝: %s", owner, exc)
                return ToolCallVerdict(
                    "block", f"门控 {owner} 执行失败，本次调用已被拒绝",
                )
            if isinstance(result, ApprovalRequired):
                # 先记下审批需求，跑完全部门控再决定，避免通行证被后续 block 白烧
                if not approval_fingerprint_value:
                    approval_fingerprint_value = approval_fingerprint(tool_name, args)
                    approval_reason = result.reason
                continue
            if isinstance(result, ExtensionVeto):
                return ToolCallVerdict("block", result.reason)
            if result is False:
                return ToolCallVerdict("block", f"被扩展 {owner} 拒绝")
            if result is not None:
                # fail-closed：门控只认 None（放行）、block / confirm / False。
                # 其它返回值说明插件作者意图不明确，按“判断不了就阻止”处理，
                # 避免拼错返回值导致门控静默失效。
                _logger.warning(
                    "门控 %s 返回了无法识别的判定 %r，按 fail-closed 拒绝",
                    owner,
                    result,
                )
                return ToolCallVerdict(
                    "block",
                    f"门控 {owner} 返回了无法识别的判定，本次调用已被拒绝",
                )

        if approval_fingerprint_value:
            if self.consume_approval(
                approval_fingerprint_value, task_id=task_id,
            ):
                return ToolCallVerdict("allow")
            return ToolCallVerdict(
                "approve",
                approval_reason,
                approval_fingerprint_value,
            )
        return ToolCallVerdict("allow")

    def run_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Optional[str]:
        """门控的简化接口：可以直接执行时返回 None，否则返回原因.

        .. note::
           这是二态接口：``approve``（需人类批准）会被折叠成「返回原因」。
           生产路径请使用 :meth:`evaluate_tool_call`，否则会自动跳过审批。

        Args:
            tool_name: 工具名.
            args: 工具参数.

        Returns:
            拒绝或待批准的原因；``None`` 表示放行.
        """
        verdict = self.evaluate_tool_call(tool_name, args)
        return None if verdict.is_allow else (verdict.reason or "blocked")

    # ---- 审批通行证 -----------------------------------------------------

    def grant_approval(
        self,
        fingerprint: str,
        ttl: float = 600.0,
        task_id: str = "",
    ) -> None:
        """为一次具体调用发放一次性通行证.

        通行证绑定任务：只有同一 task_id 的调用才能消费它，避免批准在任务结束
        后被后续任务复用。

        Args:
            fingerprint: 由 :func:`approval_fingerprint` 计算.
            ttl: 通行证有效期（秒）.
            task_id: 任务标识；空串表示不限定任务.
        """
        if not fingerprint:
            return
        self._approvals[fingerprint] = (time.monotonic() + ttl, task_id)

    def consume_approval(self, fingerprint: str, task_id: str = "") -> bool:
        """消费一次性通行证；无论成功与否都会清除该指纹.

        Args:
            fingerprint: 审批指纹.
            task_id: 当前任务标识；与发放时不一致则视为无效.

        Returns:
            True 表示这次调用已被批准.
        """
        if not fingerprint:
            return False
        entry = self._approvals.pop(fingerprint, None)
        if entry is None:
            return False
        expiry, granted_task = entry
        if expiry <= time.monotonic():
            return False
        # 绑定了任务的通行证只能被同一任务消费；空 task_id 不再视为“任意任务”
        if granted_task and granted_task != task_id:
            return False
        return True

    def clear_approvals(self) -> None:
        """清除全部未使用的通行证（拒绝或任务结束时调用）."""
        self._approvals.clear()

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
            if len(rendered) > _MAX_PROMPT_BLOCK_CHARS:
                _logger.warning(
                    "扩展 %s 的提示词块 %s 过长（%d 字符），已截断到 %d 字符",
                    owner,
                    name,
                    len(rendered),
                    _MAX_PROMPT_BLOCK_CHARS,
                )
                rendered = rendered[:_MAX_PROMPT_BLOCK_CHARS] + "\n…（已截断）"
            if rendered:
                blocks.append(PromptBlock(name=f"{owner}:{name}", text=rendered))
        return blocks

    # ---- 查询 -----------------------------------------------------------

    @property
    def loaded_names(self) -> List[str]:
        """返回已加载插件名列表."""
        return sorted(self._loaded)
