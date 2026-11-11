"""权限门禁扩展：对高危工具调用做 fail-closed 拦截.

首版只做"拦截"，不做"询问后放行"——审批链路属于阶段 3（见设计文档第八节）。
被拦截时返回明确原因，模型不应原地重试。

规则来源优先级:

1. ``config.extensions_approval`` 中的自定义规则；
2. 内置默认规则。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

from zero_agent.extensions.events import ExtensionVeto

# 明显危险的 shell 片段：命中即拒绝，不做模糊判断
_DANGEROUS_SHELL: List[Tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*f", "递归强制删除"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*f[a-zA-Z]*[rR]", "递归强制删除"),
    (r"\bmkfs(\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\s+[^\n]*of=/dev/", "向块设备写入"),
    (r":\s*\(\)\s*\{.*\};?\s*:", "fork 炸弹"),
    (r"\bshutdown\b|\breboot\b|\bpoweroff\b", "关机或重启"),
    (r"\bchmod\s+(-[a-zA-Z]+\s+)*777\s+/\s*$", "放开根目录权限"),
]

# 受保护的写入目标：这些内容修改前必须经人类同意
_PROTECTED_PREFIXES: Tuple[str, ...] = (
    "~/.zeroagent/skills",
    "~/.zeroagent/extensions",
    "~/.zeroagent/mcp.json",
)

_WRITE_TOOLS = {"file_write", "file_patch"}


def _expand(path: str) -> str:
    """把 ``~`` 展开为绝对路径，便于前缀比较."""
    import os

    return os.path.expanduser(str(path))


def _under(path: str, prefixes: Tuple[str, ...]) -> str | None:
    """判断路径是否落在受保护前缀下，返回命中的前缀."""
    expanded = _expand(path)
    for prefix in prefixes:
        root = _expand(prefix)
        if expanded == root or expanded.startswith(root.rstrip("/") + "/"):
            return prefix
    return None


def _shell_verdict(code: str) -> str | None:
    """检查 shell 片段，命中危险模式时返回原因."""
    for pattern, reason in _DANGEROUS_SHELL:
        if re.search(pattern, code):
            return reason
    return None


def _protected_prefixes(runtime: Any) -> Tuple[str, ...]:
    """结合配置返回受保护的写入前缀."""
    prefixes = list(_PROTECTED_PREFIXES)
    config = getattr(runtime, "config", None)
    extra = getattr(config, "extensions_protected_writes", None) if config else None
    if isinstance(extra, (list, tuple)):
        prefixes.extend(str(item) for item in extra)
    home = getattr(runtime, "home", None)
    if home is not None:
        prefixes.append(str(home))
    return tuple(dict.fromkeys(prefixes))


def setup(ctx: Any) -> None:
    """安装权限门禁.

    Args:
        ctx: 插件上下文（ExtensionContext）.
    """

    @ctx.on_tool_call
    def guard(call: Any) -> Any:
        """门控回调：返回 None 放行，返回 ExtensionVeto 拒绝.

        Args:
            call: ToolCall 实例.

        Returns:
            None 或 ExtensionVeto.
        """
        args: Dict[str, Any] = call.args or {}

        if call.tool_name == "code_run":
            code = str(args.get("code") or "")
            reason = _shell_verdict(code)
            if reason is not None:
                return ctx.block(
                    f"已拦截：{reason}。这是不可逆的高危操作，如需执行请由用户手动完成。"
                )

        if call.tool_name in _WRITE_TOOLS:
            path = str(args.get("path") or "")
            prefix = _under(path, _protected_prefixes(ctx.runtime))
            if prefix is not None:
                return ctx.block(
                    f"已拦截：修改 {prefix} 下的内容需要人类批准，"
                    "当前版本尚不支持自动审批，请让用户手动修改。"
                )

        return None
