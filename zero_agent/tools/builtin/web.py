"""浏览器交互工具.

web_scan: 获取浏览器标签页列表和简化 HTML 内容.
web_execute_js: 在浏览器中执行 JavaScript 并捕获结果.

依赖 TMWebDriver 和 simphtml 库，未安装时工具会返回错误提示.
"""

from __future__ import annotations

import importlib
import json
import time
from typing import Any, Dict, Generator, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.text import smart_format, format_error


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


# 模块级 driver 引用（懒加载）
_driver: Any = None


def _init_driver() -> Any:
    """懒初始化浏览器驱动.

    Returns:
        TMWebDriver 实例，或 None（初始化失败时）.
    """
    global _driver
    if _driver is not None:
        return _driver

    try:
        from TMWebDriver import TMWebDriver
    except ImportError:
        return None

    _driver = TMWebDriver()
    for _ in range(20):
        time.sleep(1)
        try:
            sessions = _driver.get_all_sessions()
            if len(sessions) > 0:
                break
        except Exception:
            pass

    if len(_driver.get_all_sessions()) == 0:
        return _driver

    # 单标签页时等待加载
    if len(_driver.get_all_sessions()) == 1:
        time.sleep(3)

    return _driver


def _get_driver() -> Any:
    """获取当前 driver 实例，必要时初始化.

    Returns:
        TMWebDriver 实例.
    """
    global _driver
    if _driver is None:
        _driver = _init_driver()
    return _driver


def web_scan(
    tabs_only: bool = False,
    switch_tab_id: Optional[str] = None,
    text_only: bool = False,
    maxlen: int = 35000,
) -> dict:
    """获取浏览器标签页列表和当前页面简化 HTML 内容.

    简化过程会过滤边栏、浮动元素等非主体内容。
    应优先使用 web_execute_js 进行精细操作，仅在需要全量观察时使用此工具。

    Args:
        tabs_only: True 时仅返回标签页列表，不获取 HTML（节省 token）.
        switch_tab_id: 可选，扫描前切换到指定标签页.
        text_only: True 时仅提取文本，不保留 HTML 结构.
        maxlen: 返回 HTML 内容的最大字符数.

    Returns:
        {"status": "success"|"error", "metadata": {...}, "content": "..."}
    """
    try:
        driver = _get_driver()
        if driver is None:
            return {
                "status": "error",
                "msg": "TMWebDriver 未安装或初始化失败，无法使用浏览器功能",
            }

        sessions = driver.get_all_sessions()
        if len(sessions) == 0:
            return {
                "status": "error",
                "msg": "没有可用的浏览器标签页",
            }

        tabs = []
        for sess in sessions:
            sess.pop("connected_at", None)
            sess.pop("type", None)
            url = sess.get("url", "")
            sess["url"] = url[:50] + ("..." if len(url) > 50 else "")
            tabs.append(sess)

        if switch_tab_id:
            driver.default_session_id = switch_tab_id

        result: Dict[str, Any] = {
            "status": "success",
            "metadata": {
                "tabs_count": len(tabs),
                "tabs": tabs,
                "active_tab": driver.default_session_id,
            },
        }

        if not tabs_only:
            try:
                import simphtml
                importlib.reload(simphtml)
            except ImportError:
                result["status"] = "error"
                result["msg"] = "simphtml 库未安装"
                return result

            content = simphtml.get_html(
                driver, cutlist=True, maxchars=maxlen, text_only=text_only,
            )
            if text_only:
                content = smart_format(
                    content,
                    max_str_len=maxlen // 3,
                    omit_str="\n\n[omitted long content]\n\n",
                )
            result["content"] = content

        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_execute_js(
    script: str,
    switch_tab_id: Optional[str] = None,
    no_monitor: bool = False,
) -> dict:
    """在浏览器中执行 JavaScript 并捕获结果和页面变化.

    这是浏览器交互的优先使用工具，可以实现对浏览器的完全控制。
    支持将结果保存到文件供后续分析。

    Args:
        script: 待执行的 JavaScript 代码.
        switch_tab_id: 可选，执行前切换到指定标签页.
        no_monitor: True 时不监控页面变化.

    Returns:
        {"status": "success"|"error", "js_return": ..., ...}
    """
    try:
        driver = _get_driver()
        if driver is None:
            return {
                "status": "error",
                "msg": "TMWebDriver 未安装或初始化失败，无法使用浏览器功能",
            }

        sessions = driver.get_all_sessions()
        if len(sessions) == 0:
            return {
                "status": "error",
                "msg": "没有可用的浏览器标签页",
            }

        if switch_tab_id:
            driver.default_session_id = switch_tab_id

        try:
            import simphtml
        except ImportError:
            return {"status": "error", "msg": "simphtml 库未安装"}

        result = simphtml.execute_js_rich(script, driver, no_monitor=no_monitor)
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def register_web_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """注册浏览器工具到 ToolRegistry.

    Args:
        registry: 工具注册中心.
        config: Agent 配置.
    """
    from zero_agent.tools.registry import ToolDefinition

    lang = config.resolved_tool_language

    registry.register(ToolDefinition(
        name="web_scan",
        description=_t(
            "获取浏览器标签页列表和当前页面简化 HTML 内容。"
            "简化过程会过滤边栏、浮动元素等非主体内容。"
            "应优先使用 web_execute_js 进行精细操作，仅在需要全量观察时使用此工具。"
            "tabs_only=true 时仅返回标签页列表，不获取 HTML（节省 token）。",
            "Get the browser tab list and simplified HTML content of the current page. "
            "The simplification process filters out sidebars, floating elements, "
            "and other non-body content. Prefer web_execute_js for fine-grained "
            "operations; use this tool only for full-page observation. "
            "tabs_only=true returns only the tab list without HTML (saves tokens).",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "tabs_only": {
                    "type": "boolean",
                    "description": _t(
                        "仅返回标签页列表，不获取 HTML 内容",
                        "Return only tab list, do not fetch HTML content",
                        lang,
                    ),
                },
                "switch_tab_id": {
                    "type": "string",
                    "description": _t(
                        "扫描前切换到指定标签页 ID",
                        "Switch to the specified tab ID before scanning",
                        lang,
                    ),
                },
                "text_only": {
                    "type": "boolean",
                    "description": _t(
                        "仅提取文本内容，不保留 HTML 结构",
                        "Extract text only, do not preserve HTML structure",
                        lang,
                    ),
                },
            },
        },
        handler=_make_web_scan_handler(config),
        category="browser",
    ))

    registry.register(ToolDefinition(
        name="web_execute_js",
        description=_t(
            "在浏览器中执行 JavaScript 代码并捕获结果和页面变化。"
            "这是浏览器交互的优先使用工具，可以实现对浏览器的完全控制。"
            "支持将结果保存到文件供后续读取分析。",
            "Execute JavaScript code in the browser and capture results "
            "and page changes. This is the preferred tool for browser "
            "interaction, providing complete control over the browser. "
            "Supports saving results to a file for later analysis.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": _t(
                        "待执行的 JavaScript 代码",
                        "JavaScript code to execute",
                        lang,
                    ),
                },
                "switch_tab_id": {
                    "type": "string",
                    "description": _t(
                        "执行前切换到指定标签页 ID",
                        "Switch to the specified tab ID before execution",
                        lang,
                    ),
                },
                "save_to_file": {
                    "type": "string",
                    "description": _t(
                        "将 js_return 结果保存到指定文件路径",
                        "Save the js_return result to the specified file path",
                        lang,
                    ),
                },
                "no_monitor": {
                    "type": "boolean",
                    "description": _t(
                        "不监控页面变化",
                        "Do not monitor page changes",
                        lang,
                    ),
                },
            },
            "required": ["script"],
        },
        handler=_make_web_execute_js_handler(config),
        category="browser",
    ))


def _make_web_scan_handler(config: AgentConfig):
    """创建 web_scan 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        tabs_only = args.get("tabs_only", False)
        switch_tab_id = args.get("switch_tab_id")
        text_only = args.get("text_only", False)
        maxlen = 35000 // max(args.get("_tool_num", 1), 1)

        result = web_scan(
            tabs_only=tabs_only,
            switch_tab_id=switch_tab_id,
            text_only=text_only,
            maxlen=maxlen,
        )

        content = result.pop("content", None)
        yield f"[Info] {str(result)}\n"

        if content:
            import json
            output = json.dumps(result, ensure_ascii=False)
            output += f"\n```html\n{content}\n```"
            return {"status": "success", "output": output}

        return result
    return _handler


def _make_web_execute_js_handler(config: AgentConfig):
    """创建 web_execute_js 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict]:
        script = args.get("script", "")
        if not script:
            # 回退: 从 LLM 响应提取 JavaScript 代码块
            script = handler._extract_code_block(_response, "javascript")
        if not script:
            # 二级回退: 从响应提取任意代码块
            script = handler._extract_code_block(_response)
        if not script:
            return {"status": "error", "msg": "缺少 script 参数"}

        # 支持从文件读取脚本
        import os
        if os.path.isfile(script.strip()):
            try:
                with open(script.strip(), "r", encoding="utf-8") as f:
                    script = f.read()
            except Exception:
                pass

        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        no_monitor = args.get("no_monitor", False)

        result = web_execute_js(
            script, switch_tab_id=switch_tab_id, no_monitor=no_monitor,
        )

        # 保存结果到文件
        if save_to_file and "js_return" in result:
            content = str(result["js_return"] or "")
            result["js_return"] = smart_format(content, max_str_len=170)
            try:
                save_path = os.path.join(config.workspace_dir, save_to_file)
                os.makedirs(os.path.dirname(save_path) or config.workspace_dir, exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(content)
                result["js_return"] += f"\n\n[已保存完整内容到 {save_path}]"
            except Exception:
                result["js_return"] += f"\n\n[保存失败，无法写入文件 {save_to_file}]"

        import json
        show = smart_format(
            json.dumps(result, ensure_ascii=False, indent=2),
            max_str_len=300,
        )
        yield f"JS 执行结果:\n{show}\n"

        maxlen = 8000 // max(args.get("_tool_num", 1), 1)
        output = json.dumps(result, ensure_ascii=False)
        return {"status": result.get("status", "success"), "output": smart_format(output, max_str_len=maxlen)}
    return _handler
