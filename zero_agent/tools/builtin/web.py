"""浏览器交互工具.

web_scan: 获取浏览器标签页列表和简化 HTML 内容.
web_execute_js: 在浏览器中执行 JavaScript 并捕获结果.

依赖 ZeroAgent browser runtime 和浏览器扩展.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
from importlib import resources
from typing import Any, Dict, Generator, Optional

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.text import smart_format, format_error


def _t(zh: str, en: str, lang: str) -> str:
    """根据语言选择中文或英文文本."""
    return zh if lang == "zh" else en


# 模块级 driver 引用（懒加载）
_driver: Any = None
_driver_error: Optional[str] = None

_MAX_TOOL_TIMEOUT = 120
_DEFAULT_TOOL_TIMEOUT = 15

_BROWSER_EXTRA_HINT = (
    "浏览器运行时不可用。请安装浏览器扩展依赖: "
    "pip install 'zero-agent[browser]'，并加载 bundled extension: "
    "zero_agent/assets/tmwd_cdp_bridge"
)


def browser_extension_dir() -> str:
    """Return the bundled browser extension resource path when available."""
    try:
        return str(resources.files("zero_agent.assets").joinpath("tmwd_cdp_bridge"))
    except Exception:
        return "zero_agent/assets/tmwd_cdp_bridge"


def _load_tm_webdriver() -> Any:
    """Load the ZeroAgent browser driver class."""
    try:
        from zero_agent.browser.tm_webdriver import TMWebDriver
    except ImportError as exc:
        raise ImportError(f"{_BROWSER_EXTRA_HINT} ({exc})") from exc
    return TMWebDriver


def _load_simphtml() -> Any:
    try:
        from zero_agent.browser import simphtml
    except ImportError as exc:
        raise ImportError(f"{_BROWSER_EXTRA_HINT} ({exc})") from exc

    if not hasattr(simphtml, "BeautifulSoup"):
        raise ImportError(f"{_BROWSER_EXTRA_HINT} (beautifulsoup4 missing)")

    return simphtml


def _init_driver() -> Any:
    """懒初始化浏览器驱动.

    Returns:
        TMWebDriver 实例，或 None（初始化失败时）.
    """
    global _driver, _driver_error
    if _driver is not None:
        return _driver

    try:
        TMWebDriver = _load_tm_webdriver()
    except ImportError as exc:
        _driver_error = str(exc)
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


def _driver_or_error() -> tuple[Any, Optional[dict]]:
    driver = _get_driver()
    if driver is None:
        return None, {
            "status": "error",
            "msg": _driver_error or "TMWebDriver 初始化失败，无法使用浏览器功能",
        }
    return driver, None


def _clamp_timeout(value: Any, default: int = _DEFAULT_TOOL_TIMEOUT) -> int:
    try:
        return max(1, min(int(value), _MAX_TOOL_TIMEOUT))
    except (TypeError, ValueError, OverflowError):
        return default


def _resolve_tab_id(driver: Any, tab_id: Any) -> Any:
    resolved = tab_id if tab_id not in (None, "") else getattr(
        driver, "default_session_id", None,
    )
    if resolved in (None, ""):
        raise ValueError("没有可用的标签页；请先调用 web_scan 或 web_tabs 选择标签页")
    return resolved


def _parse_object(value: Any, name: str) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{name} 必须是 JSON 对象: {exc}") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{name} 必须是 JSON 对象")


def _cdp_eval(driver: Any, tab_id: Any, script: str, timeout: int = 15) -> Any:
    expression = f"(async () => {{ {script}\n}})()"
    response = driver.cdp(
        "Runtime.evaluate",
        {"expression": expression, "awaitPromise": True, "returnByValue": True},
        tab_id=_resolve_tab_id(driver, tab_id),
        timeout=timeout,
    )
    if isinstance(response, dict) and response.get("exceptionDetails"):
        details = response["exceptionDetails"]
        message = details.get("text") or "Runtime.evaluate failed"
        exception = details.get("exception") or {}
        raise ValueError(exception.get("description") or message)
    value = response.get("result") if isinstance(response, dict) else None
    if isinstance(value, dict):
        if value.get("type") == "undefined":
            return None
        if "value" in value:
            return value["value"]
        if "unserializableValue" in value:
            return value["unserializableValue"]
    return value


def _decode_base64_payload(encoded: Any) -> bytes:
    if not isinstance(encoded, str) or not encoded.strip():
        raise ValueError("empty base64 data")
    if encoded.startswith("data:") and "," in encoded:
        encoded = encoded.split(",", 1)[1]
    return base64.b64decode("".join(encoded.split()), validate=True)


def _js_eval(driver: Any, tab_id: Any, script: str, timeout: int = 15) -> Any:
    response = None
    error: Optional[Exception] = None
    try:
        response = driver.execute_js(
            script, timeout=timeout, session_id=_resolve_tab_id(driver, tab_id),
        )
    except Exception as exc:
        error = exc
    data = response.get("data") if isinstance(response, dict) else response
    if data is not None:
        return data
    # chrome.scripting can hang on freshly created or still-loading tabs. The
    # extension ACKs the request but never returns a result; CDP evaluates the
    # same code through the debugger and is reliable in that window.
    if isinstance(response, dict) and response.get("result"):
        return _cdp_eval(driver, tab_id, script, timeout)
    if error is not None:
        raise error
    return data


_MODIFIER_BITS = {
    "alt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "shift": 8,
}


def _modifier_mask(modifiers: Any) -> int:
    if not modifiers:
        return 0
    if isinstance(modifiers, int):
        return modifiers
    if isinstance(modifiers, str):
        modifiers = [
            part for part in modifiers.replace("+", ",").split(",") if part.strip()
        ]
    mask = 0
    for item in modifiers:
        mask |= _MODIFIER_BITS.get(str(item).strip().lower(), 0)
    return mask


_KEY_SPECS = {
    "enter": ("Enter", "Enter", 13, "\r"),
    "tab": ("Tab", "Tab", 9, None),
    "escape": ("Escape", "Escape", 27, None),
    "esc": ("Escape", "Escape", 27, None),
    "backspace": ("Backspace", "Backspace", 8, None),
    "delete": ("Delete", "Delete", 46, None),
    "arrowup": ("ArrowUp", "ArrowUp", 38, None),
    "arrowdown": ("ArrowDown", "ArrowDown", 40, None),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37, None),
    "arrowright": ("ArrowRight", "ArrowRight", 39, None),
    "home": ("Home", "Home", 36, None),
    "end": ("End", "End", 35, None),
    "pageup": ("PageUp", "PageUp", 33, None),
    "pagedown": ("PageDown", "PageDown", 34, None),
    "space": (" ", "Space", 32, " "),
}


def _key_spec(key: Any) -> tuple[str, str, int, Optional[str]]:
    raw = str(key)
    spec = _KEY_SPECS.get(raw.strip().lower())
    if spec:
        return spec
    if len(raw) == 1:
        char = raw
        if char.isalpha():
            code = f"Key{char.upper()}"
            vk = ord(char.upper())
        elif char.isdigit():
            code = f"Digit{char}"
            vk = ord(char)
        else:
            code = ""
            vk = 0
        return char, code, vk, char
    raise ValueError(f"不支持的按键: {key}")


def _dispatch_key(driver: Any, tab_id: Any, key: Any, modifiers: Any = None) -> None:
    name, code, vk, text = _key_spec(key)
    mods = _modifier_mask(modifiers)
    base = {
        "key": name,
        "code": code,
        "windowsVirtualKeyCode": vk,
        "nativeVirtualKeyCode": vk,
        "modifiers": mods,
    }
    down = {"type": "keyDown", **base}
    if text is not None:
        down["text"] = text
        down["unmodifiedText"] = text
    driver.cdp("Input.dispatchKeyEvent", down, tab_id=tab_id)
    driver.cdp("Input.dispatchKeyEvent", {"type": "keyUp", **base}, tab_id=tab_id)


def _dispatch_click(
    driver: Any,
    tab_id: Any,
    x: float,
    y: float,
    button: str = "left",
    click_count: int = 1,
    modifiers: Any = None,
) -> None:
    button = str(button or "left").strip().lower()
    if button not in {"left", "right", "middle"}:
        raise ValueError(f"不支持的鼠标按键: {button}")
    buttons = {"left": 1, "right": 2, "middle": 4}[button]
    mods = _modifier_mask(modifiers)
    count = max(1, min(int(click_count or 1), 3))
    driver.cdp("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
        "buttons": 0, "modifiers": mods,
    }, tab_id=tab_id)
    for index in range(1, count + 1):
        driver.cdp("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y, "button": button,
            "buttons": buttons, "clickCount": index, "modifiers": mods,
        }, tab_id=tab_id)
        driver.cdp("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y, "button": button,
            "buttons": 0, "clickCount": index, "modifiers": mods,
        }, tab_id=tab_id)


def _button_bits(button: Any) -> tuple[str, int]:
    """Normalize a mouse button name to (CDP button, buttons bitmask)."""
    name = str(button or "left").strip().lower()
    if name not in {"left", "right", "middle"}:
        raise ValueError(f"不支持的鼠标按键: {button}")
    return name, {"left": 1, "right": 2, "middle": 4}[name]


def _dispatch_hover(driver: Any, tab_id: Any, x: float, y: float,
                    modifiers: Any = None) -> None:
    """Move the native cursor without pressing a button."""
    driver.cdp("Input.dispatchMouseEvent", {
        "type": "mouseMoved", "x": x, "y": y, "button": "none",
        "buttons": 0, "modifiers": _modifier_mask(modifiers),
    }, tab_id=tab_id)


def _dispatch_wheel(driver: Any, tab_id: Any, x: float, y: float,
                    delta_x: float, delta_y: float,
                    modifiers: Any = None) -> None:
    """Dispatch a native mouse wheel event."""
    driver.cdp("Input.dispatchMouseEvent", {
        "type": "mouseWheel", "x": x, "y": y,
        "deltaX": delta_x, "deltaY": delta_y,
        "button": "none", "buttons": 0,
        "modifiers": _modifier_mask(modifiers),
    }, tab_id=tab_id)


def _dispatch_drag(
    driver: Any,
    tab_id: Any,
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    *,
    button: str = "left",
    modifiers: Any = None,
    duration_ms: int = 500,
    steps: int = 12,
) -> None:
    """Press, move, and release the native cursor to simulate a drag."""
    name, bits = _button_bits(button)
    mods = _modifier_mask(modifiers)
    steps = max(1, min(int(steps or 12), 100))
    duration_ms = max(0, min(int(duration_ms or 0), 10000))
    _dispatch_hover(driver, tab_id, start_x, start_y, mods)
    driver.cdp("Input.dispatchMouseEvent", {
        "type": "mousePressed", "x": start_x, "y": start_y,
        "button": name, "buttons": bits, "clickCount": 1,
        "modifiers": mods,
    }, tab_id=tab_id)
    for index in range(1, steps + 1):
        ratio = index / steps
        x = start_x + (end_x - start_x) * ratio
        y = start_y + (end_y - start_y) * ratio
        driver.cdp("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y,
            "button": name, "buttons": bits, "modifiers": mods,
        }, tab_id=tab_id)
        if duration_ms:
            time.sleep(duration_ms / 1000 / steps)
    driver.cdp("Input.dispatchMouseEvent", {
        "type": "mouseReleased", "x": end_x, "y": end_y,
        "button": name, "buttons": 0, "clickCount": 1,
        "modifiers": mods,
    }, tab_id=tab_id)


def _selector_point(driver: Any, tab_id: Any, selector: str) -> Dict[str, Any]:
    selector_json = json.dumps(selector)
    script = f"""return (() => {{
      const el = document.querySelector({selector_json});
      if (!el) return {{ok: false, error: 'selector not found'}};
      el.scrollIntoView({{block: 'center', inline: 'center', behavior: 'instant'}});
      const r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return {{ok: false, error: 'element has no visible box'}};
      return {{ok: true, x: r.left + r.width / 2, y: r.top + r.height / 2,
               tag: el.tagName, text: String(el.innerText || el.value || '').slice(0, 120)}};
    }})()"""
    data = _js_eval(driver, tab_id, script, timeout=5)
    if not isinstance(data, dict) or not data.get("ok"):
        message = data.get("error") if isinstance(data, dict) else "cannot resolve selector"
        raise ValueError(str(message or "cannot resolve selector"))
    return data


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
                "msg": _driver_error or "TMWebDriver 初始化失败，无法使用浏览器功能",
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
                simphtml = _load_simphtml()
            except ImportError:
                result["status"] = "error"
                result["msg"] = _BROWSER_EXTRA_HINT
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
    timeout_seconds: int = 15,
) -> dict:
    """在浏览器中执行 JavaScript 并捕获结果和页面变化.

    这是浏览器交互的优先使用工具，可以实现对浏览器的完全控制。
    支持将结果保存到文件供后续分析。

    Args:
        script: 待执行的 JavaScript 代码.
        switch_tab_id: 可选，执行前切换到指定标签页.
        no_monitor: True 时不监控页面变化.
        timeout_seconds: JavaScript 等待时间上限，范围 1–120 秒.

    Returns:
        {"status": "success"|"error", "js_return": ..., ...}
    """
    try:
        driver = _get_driver()
        if driver is None:
            return {
                "status": "error",
                "msg": _driver_error or "TMWebDriver 初始化失败，无法使用浏览器功能",
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
            simphtml = _load_simphtml()
        except ImportError:
            return {"status": "error", "msg": _BROWSER_EXTRA_HINT}

        timeout_seconds = _clamp_timeout(timeout_seconds)
        result = simphtml.execute_js_rich(
            script,
            driver,
            no_monitor=no_monitor,
            timeout=timeout_seconds,
        )
        return result
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_screenshot(
    save_to_file: str,
    switch_tab_id: Optional[str] = None,
    image_format: str = "png",
    capture_beyond_viewport: bool = True,
) -> dict:
    """Capture the selected browser tab through CDP and save an image file."""
    image_format = str(image_format or "png").lower().strip()
    if image_format not in {"png", "jpeg", "webp"}:
        return {"status": "error", "msg": f"unsupported screenshot format: {image_format}"}

    tab_id: Any = switch_tab_id
    if switch_tab_id not in (None, "") and str(switch_tab_id).isdigit():
        tab_id = int(switch_tab_id)
    command = {
        "cmd": "cdp",
        "method": "Page.captureScreenshot",
        "params": {
            "format": image_format,
            "captureBeyondViewport": bool(capture_beyond_viewport),
        },
    }
    # Omitting tabId lets the extension fill in the current controlled tab.
    # Sending null would prevent that fallback in the websocket bridge.
    if tab_id is not None:
        command["tabId"] = tab_id
    script = json.dumps(command, ensure_ascii=False)
    result = web_execute_js(script, switch_tab_id=switch_tab_id, no_monitor=True)
    if result.get("status") != "success":
        return result

    payload = result.get("js_return")
    encoded = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(encoded, str) or not encoded.strip():
        return {"status": "error", "msg": "browser returned no screenshot data"}
    try:
        image_bytes = _decode_base64_payload(encoded)
    except (ValueError, binascii.Error) as exc:
        return {"status": "error", "msg": f"invalid screenshot data: {exc}"}

    try:
        os.makedirs(os.path.dirname(save_to_file) or ".", exist_ok=True)
        with open(save_to_file, "wb") as output:
            output.write(image_bytes)
    except OSError as exc:
        return {"status": "error", "msg": f"cannot save screenshot: {exc}"}

    return {
        "status": "success",
        "path": os.path.abspath(save_to_file),
        "format": image_format,
        "bytes": len(image_bytes),
        "tab_id": result.get("tab_id"),
    }


def web_upload(
    selector: str,
    files: list,
    tab_id: Optional[str] = None,
    index: int = 0,
    timeout_seconds: float = 15,
) -> dict:
    """Attach local files to a page file input through CDP."""
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        paths = [str(item) for item in files or [] if str(item).strip()]
        if not paths:
            return {"status": "error", "msg": "files is required"}
        missing = [path for path in paths if not os.path.isfile(path)]
        if missing:
            return {"status": "error", "msg": f"file not found: {missing[0]}"}
        result = driver.upload(
            resolved, str(selector), paths,
            index=int(index or 0), timeout=float(timeout_seconds or 15),
        )
        return {
            "status": "success", "tab_id": str(resolved),
            "selector": str(selector), "files": result.get("files", paths),
            "input": result.get("summary"),
        }
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_download(
    action: str = "list",
    tab_id: Optional[str] = None,
    url: Optional[str] = None,
    filename: Optional[str] = None,
    target: Optional[str] = None,
    since: Optional[int] = None,
    timeout_seconds: float = 20,
    limit: int = 10,
) -> dict:
    """Download through chrome.downloads or wait for a page-triggered download."""
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        action = str(action or "list").strip().lower()
        timeout_ms = max(1000, min(int(float(timeout_seconds or 20) * 1000), 120000))

        if action == "list":
            data = driver.download(method="list", limit=max(1, min(int(limit or 10), 50)))
        elif action == "url":
            if not url:
                return {"status": "error", "msg": "url is required"}
            data = driver.download(
                method="start", url=str(url), filename=filename,
                timeout_ms=timeout_ms,
            )
        elif action == "wait":
            data = driver.download(
                method="wait", since=int(since or (time.time() * 1000 - 60000)),
                timeout_ms=timeout_ms,
            )
        elif action == "click":
            if not target:
                return {"status": "error", "msg": "target is required"}
            clicked = web_input(action="click", selector=str(target), tab_id=str(resolved))
            if clicked.get("status") != "success":
                return clicked
            data = driver.download(
                method="wait", since=int(time.time() * 1000),
                timeout_ms=timeout_ms,
            )
        else:
            return {"status": "error", "msg": f"unknown action: {action}"}

        payload = data if isinstance(data, dict) else {"data": data}
        return {"status": "success", "action": action, "tab_id": str(resolved), **payload}
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def _close_duplicate_tabs(driver: Any, dry_run: bool = True) -> dict:
    tabs = driver.list_tabs(include_all=False)
    groups: Dict[str, list[Dict[str, Any]]] = {}
    for tab in tabs:
        url = str(tab.get("url") or "")
        if not url.lower().startswith(("http://", "https://", "file://")):
            continue
        key = url.split("#", 1)[0]
        groups.setdefault(key, []).append(tab)

    plans = []
    for key, group in groups.items():
        if len(group) < 2:
            continue
        keep = next((tab for tab in group if tab.get("active")), None)
        if keep is None:
            keep = next((tab for tab in group if tab.get("pinned")), None)
        if keep is None:
            keep = max(group, key=lambda tab: float(tab.get("lastAccessed") or 0))
        close_tabs = [
            tab for tab in group
            if tab is not keep and not tab.get("pinned")
        ]
        if not close_tabs:
            continue
        plans.append({
            "url": key,
            "keep": keep.get("id"),
            "close": [tab.get("id") for tab in close_tabs],
        })

    if dry_run:
        return {
            "status": "success",
            "dry_run": True,
            "duplicate_groups": plans,
            "would_close": sum(len(plan["close"]) for plan in plans),
        }

    closed: list[str] = []
    errors: list[dict[str, str]] = []
    for plan in plans:
        for tab_id in plan["close"]:
            try:
                driver.close_tab(tab_id)
                closed.append(str(tab_id))
            except Exception as exc:
                errors.append({"tab_id": str(tab_id), "error": format_error(exc)})
    return {
        "status": "success" if closed or not errors else "error",
        "dry_run": False,
        "duplicate_groups": plans,
        "closed": closed,
        "errors": errors,
    }


def web_tabs(
    action: str = "list",
    tab_id: Optional[str] = None,
    tab_ids: Optional[list] = None,
    url: Optional[str] = None,
    active: bool = False,
    include_all: bool = False,
    dry_run: bool = True,
    ignore_cache: bool = False,
) -> dict:
    """List, create, activate, navigate, reload, navigate history, close, or deduplicate tabs.

    Tab lifecycle lives in the extension, so it works even when the last
    scriptable tab is gone. `close_duplicates` defaults to a dry run.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        action = str(action or "list").strip().lower()

        if action in {"list", "tabs"}:
            tabs = driver.list_tabs(include_all=bool(include_all))
            return {"status": "success", "count": len(tabs), "tabs": tabs}

        if action == "create":
            if not url:
                return {"status": "error", "msg": "url is required"}
            tab = driver.create_tab(str(url), active=bool(active))
            return {"status": "success", "tab": tab}

        if action in {"activate", "switch", "focus"}:
            resolved = _resolve_tab_id(driver, tab_id)
            driver.activate_tab(resolved)
            return {"status": "success", "tab_id": str(resolved)}

        if action == "navigate":
            if not url:
                return {"status": "error", "msg": "url is required"}
            resolved = _resolve_tab_id(driver, tab_id)
            return {"status": "success", "tab": driver.navigate_tab(resolved, str(url))}

        if action in {"reload", "back", "forward"}:
            resolved = _resolve_tab_id(driver, tab_id)
            if action == "reload":
                driver.cdp(
                    "Page.reload", {"ignoreCache": bool(ignore_cache)},
                    tab_id=resolved,
                )
            else:
                driver.cdp(
                    "Runtime.evaluate", {"expression": f"history.{action}()"},
                    tab_id=resolved,
                )
            return {"status": "success", "action": action, "tab_id": str(resolved)}

        if action == "close":
            if isinstance(tab_ids, list) and tab_ids:
                ids = tab_ids
            elif tab_id not in (None, ""):
                ids = [tab_id]
            else:
                ids = []
            if not ids:
                return {"status": "error", "msg": "tab_id or tab_ids is required"}
            closed: list[str] = []
            errors: list[dict[str, str]] = []
            for candidate in ids:
                try:
                    driver.close_tab(candidate)
                    closed.append(str(candidate))
                except Exception as exc:
                    errors.append({"tab_id": str(candidate), "error": format_error(exc)})
            if not closed and errors:
                return {"status": "error", "msg": "没有关闭任何标签页",
                        "closed": closed, "errors": errors}
            return {"status": "success", "closed": closed, "errors": errors}

        if action in {"close_duplicates", "dedupe"}:
            return _close_duplicate_tabs(driver, dry_run=bool(dry_run))

        return {"status": "error", "msg": f"unknown action: {action}"}
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_cdp(
    method: str,
    params: Optional[dict] = None,
    tab_id: Optional[str] = None,
    persistent: bool = False,
    timeout_seconds: int = _DEFAULT_TOOL_TIMEOUT,
    save_to_file: Optional[str] = None,
) -> dict:
    """Send a raw Chrome DevTools Protocol command to one tab.

    `persistent=True` keeps the debugger attached after the call, which is
    required for `Network.enable`, `Fetch.enable`, `Page.enable`, and other
    domains that emit events. Detach explicitly or call `web_network stop`.
    For commands returning base64 (Page.printToPDF/Page.captureScreenshot),
    `save_to_file` decodes and writes the bytes instead of returning them.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        method = str(method or "").strip()
        if not method:
            return {"status": "error", "msg": "method is required"}
        params_obj = _parse_object(params, "params")
        resolved = _resolve_tab_id(driver, tab_id)
        timeout = _clamp_timeout(timeout_seconds)
        result = driver.cdp(
            method, params_obj, tab_id=resolved,
            persistent=bool(persistent), timeout=timeout,
        )

        saved = None
        if save_to_file:
            encoded = result.get("data") if isinstance(result, dict) else result
            if not isinstance(encoded, str) or not encoded.strip():
                return {
                    "status": "error",
                    "msg": "CDP result has no base64 data to save",
                    "method": method,
                }
            try:
                payload = _decode_base64_payload(encoded)
            except (ValueError, binascii.Error) as exc:
                return {"status": "error", "msg": f"invalid base64 data: {exc}"}
            try:
                os.makedirs(os.path.dirname(save_to_file) or ".", exist_ok=True)
                with open(save_to_file, "wb") as output:
                    output.write(payload)
            except OSError as exc:
                return {"status": "error", "msg": f"cannot save CDP result: {exc}"}
            if isinstance(result, dict) and "data" in result:
                result = {key: value for key, value in result.items() if key != "data"}
            saved = {"path": os.path.abspath(save_to_file), "bytes": len(payload)}

        return {
            "status": "success",
            "method": method,
            "tab_id": str(resolved),
            "persistent": bool(persistent),
            "result": result,
            "saved": saved,
        }
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_input(
    action: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    key: Optional[str] = None,
    tab_id: Optional[str] = None,
    x: Optional[float] = None,
    y: Optional[float] = None,
    button: str = "left",
    click_count: int = 1,
    modifiers: Optional[list] = None,
    clear: bool = False,
    submit: bool = False,
    to_selector: Optional[str] = None,
    to_x: Optional[float] = None,
    to_y: Optional[float] = None,
    duration_ms: int = 500,
    steps: int = 12,
    delta_x: float = 0,
    delta_y: float = 0,
) -> dict:
    """Dispatch trusted input events through CDP.

    Unlike `element.dispatchEvent(...)`, these are browser-generated
    `isTrusted=true` mouse/key events and satisfy most anti-bot checks.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        action = str(action or "").strip().lower()

        if action == "click":
            element = None
            if x is None or y is None:
                if not selector:
                    return {"status": "error", "msg": "selector or x/y is required"}
                element = _selector_point(driver, resolved, str(selector))
                x, y = element["x"], element["y"]
            try:
                px, py = float(x), float(y)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid click coordinates: {x!r}, {y!r}") from exc
            _dispatch_click(
                driver, resolved, px, py,
                button=button, click_count=click_count, modifiers=modifiers,
            )
            return {
                "status": "success", "action": "click", "tab_id": str(resolved),
                "x": px, "y": py, "element": element,
            }

        if action == "type":
            if text is None:
                return {"status": "error", "msg": "text is required"}
            focused = None
            if selector:
                selector_json = json.dumps(str(selector))
                script = f"""return (() => {{
                  const el = document.querySelector({selector_json});
                  if (!el) return {{ok: false, error: 'selector not found'}};
                  el.focus();
                  if ({str(bool(clear)).lower()} && typeof el.select === 'function') el.select();
                  return {{ok: true, tag: el.tagName, value: String(el.value ?? '')}};
                }})()"""
                focused = _js_eval(driver, resolved, script, timeout=5)
                if isinstance(focused, dict) and focused.get("ok") is False:
                    return {"status": "error", "msg": focused.get("error") or "cannot focus selector"}
            driver.cdp("Input.insertText", {"text": str(text)}, tab_id=resolved)
            if submit:
                _dispatch_key(driver, resolved, "Enter", modifiers)
            return {
                "status": "success", "action": "type", "tab_id": str(resolved),
                "chars": len(str(text)), "submitted": bool(submit), "element": focused,
            }

        if action == "key":
            if not key:
                return {"status": "error", "msg": "key is required"}
            _dispatch_key(driver, resolved, key, modifiers)
            return {"status": "success", "action": "key", "tab_id": str(resolved), "key": str(key)}

        if action == "hover":
            element = None
            if x is None or y is None:
                if not selector:
                    return {"status": "error", "msg": "selector or x/y is required"}
                element = _selector_point(driver, resolved, str(selector))
                x, y = element["x"], element["y"]
            try:
                px, py = float(x), float(y)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid hover coordinates: {x!r}, {y!r}") from exc
            _dispatch_hover(driver, resolved, px, py, modifiers)
            return {
                "status": "success", "action": "hover", "tab_id": str(resolved),
                "x": px, "y": py, "element": element,
            }

        if action == "wheel":
            element = None
            if x is None or y is None:
                if selector:
                    element = _selector_point(driver, resolved, str(selector))
                    x, y = element["x"], element["y"]
                else:
                    x, y = 0, 0
            try:
                px, py = float(x or 0), float(y or 0)
                dx, dy = float(delta_x or 0), float(delta_y or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid wheel parameters: {exc}") from exc
            if dx == 0 and dy == 0:
                return {"status": "error", "msg": "delta_x or delta_y is required"}
            _dispatch_wheel(driver, resolved, px, py, dx, dy, modifiers)
            return {
                "status": "success", "action": "wheel", "tab_id": str(resolved),
                "x": px, "y": py, "delta_x": dx, "delta_y": dy,
                "element": element,
            }

        if action == "drag":
            start_element = None
            end_element = None
            if x is None or y is None:
                if not selector:
                    return {"status": "error", "msg": "selector or x/y is required"}
                start_element = _selector_point(driver, resolved, str(selector))
                x, y = start_element["x"], start_element["y"]
            if to_x is None or to_y is None:
                if not to_selector:
                    return {"status": "error", "msg": "to_selector or to_x/to_y is required"}
                end_element = _selector_point(driver, resolved, str(to_selector))
                to_x, to_y = end_element["x"], end_element["y"]
            try:
                sx, sy = float(x), float(y)
                ex, ey = float(to_x), float(to_y)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"invalid drag coordinates: {exc}") from exc
            _dispatch_drag(
                driver, resolved, sx, sy, ex, ey,
                button=button, modifiers=modifiers,
                duration_ms=duration_ms, steps=steps,
            )
            return {
                "status": "success", "action": "drag", "tab_id": str(resolved),
                "from": {"x": sx, "y": sy}, "to": {"x": ex, "y": ey},
                "from_element": start_element, "to_element": end_element,
            }

        return {"status": "error", "msg": f"unknown action: {action}"}
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


_NETWORK_METHODS = [
    "Network.requestWillBeSent",
    "Network.responseReceived",
    "Network.loadingFailed",
]


def web_network(
    action: str = "dump",
    tab_id: Optional[str] = None,
    limit: int = 100,
    filter: str = "requests",
    clear: bool = False,
    methods: Optional[list] = None,
) -> dict:
    """Capture and summarize CDP Network events for one tab.

    `start` enables Network persistently; `dump` returns request/response/
    failure summaries; `events` returns raw buffered CDP events (including Fetch
    pauses); `continue` releases paused Fetch requests; `stop` disables Network,
    disables Fetch, and detaches the debugger.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        action = str(action or "dump").strip().lower()

        if action == "start":
            driver.clear_cdp_events(resolved)
            driver.cdp("Network.enable", {}, tab_id=resolved, persistent=True)
            return {
                "status": "success", "action": "start", "tab_id": str(resolved),
                "msg": "Network capture started; call web_network(action='dump')",
            }

        if action == "stop":
            clear = getattr(driver, "clear_intercept", None)
            if callable(clear):
                try:
                    clear(resolved)
                except Exception:
                    pass
            for method in ("Fetch.disable", "Network.disable"):
                try:
                    driver.cdp(method, {}, tab_id=resolved, persistent=True)
                except Exception:
                    pass
            driver.cdp("detach", {}, tab_id=resolved)
            return {"status": "success", "action": "stop", "tab_id": str(resolved)}

        if action == "clear":
            driver.clear_cdp_events(resolved)
            return {"status": "success", "action": "clear", "tab_id": str(resolved)}

        if action == "events":
            cap = max(1, min(int(limit or 100), 500))
            method_list = methods
            if isinstance(method_list, str):
                method_list = [method_list]
            events = driver.get_cdp_events(
                tab_id=resolved, methods=method_list, limit=cap, clear=bool(clear),
            )
            return {
                "status": "success", "action": "events", "tab_id": str(resolved),
                "count": len(events), "events": events,
            }

        if action == "continue":
            paused = driver.get_cdp_events(
                tab_id=resolved, methods=["Fetch.requestPaused"], limit=500,
            )
            continued: list[str] = []
            errors: list[dict[str, str]] = []
            for event in paused:
                request_id = (event.get("params") or {}).get("requestId")
                if not request_id:
                    continue
                try:
                    driver.cdp(
                        "Fetch.continueRequest", {"requestId": request_id},
                        tab_id=resolved,
                    )
                    continued.append(request_id)
                except Exception as exc:
                    errors.append({
                        "request_id": str(request_id), "error": format_error(exc),
                    })
            driver.clear_cdp_events(resolved)
            if not continued and errors:
                return {
                    "status": "error", "action": "continue", "tab_id": str(resolved),
                    "continued": continued, "errors": errors,
                }
            return {
                "status": "success", "action": "continue", "tab_id": str(resolved),
                "continued": continued, "errors": errors,
            }

        if action not in {"dump", "get", "list"}:
            return {"status": "error", "msg": f"unknown action: {action}"}

        cap = max(1, min(int(limit or 100), 500))
        raw_events = driver.get_cdp_events(
            tab_id=resolved, methods=_NETWORK_METHODS, limit=0,
        )
        items: list[dict[str, Any]] = []
        for event in raw_events:
            method = event.get("method")
            params = event.get("params") or {}
            if method == "Network.requestWillBeSent":
                request = params.get("request") or {}
                items.append({
                    "event": "request",
                    "request_id": params.get("requestId"),
                    "url": request.get("url"),
                    "method": request.get("method"),
                    "resource_type": params.get("type"),
                    "ts": event.get("ts"),
                })
            elif method == "Network.responseReceived":
                response = params.get("response") or {}
                items.append({
                    "event": "response",
                    "request_id": params.get("requestId"),
                    "url": response.get("url"),
                    "status": response.get("status"),
                    "mime_type": response.get("mimeType"),
                    "resource_type": params.get("type"),
                    "ts": event.get("ts"),
                })
            elif method == "Network.loadingFailed":
                items.append({
                    "event": "failure",
                    "request_id": params.get("requestId"),
                    "error": params.get("errorText"),
                    "canceled": params.get("canceled"),
                    "ts": event.get("ts"),
                })

        kind = str(filter or "requests").strip().lower()
        wanted = {
            "all": {"request", "response", "failure"},
            "requests": {"request"},
            "responses": {"response"},
            "failures": {"failure"},
        }
        if kind not in wanted:
            return {"status": "error", "msg": f"unknown filter: {filter}"}
        items = [item for item in items if item["event"] in wanted[kind]][-cap:]
        if clear:
            driver.clear_cdp_events(resolved)
        return {
            "status": "success", "action": "dump", "tab_id": str(resolved),
            "filter": kind, "count": len(items), "events": items,
        }
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_intercept(
    action: str = "apply",
    rules: Optional[list] = None,
    tab_id: Optional[str] = None,
) -> dict:
    """Apply per-tab Fetch rules that run without human intervention.

    Rules are evaluated in order; the first match wins. Unmatched requests are
    continued automatically, so enabling interception never hangs a page.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        action = str(action or "apply").strip().lower()

        if action == "list":
            current = driver.list_intercepts(resolved)
            return {
                "status": "success", "action": "list",
                "tab_id": str(resolved), "rules": current,
            }

        if action in {"clear", "disable"}:
            driver.clear_intercept(resolved)
            return {
                "status": "success", "action": "clear",
                "tab_id": str(resolved), "rules": [],
            }

        if action != "apply":
            return {"status": "error", "msg": f"unknown action: {action}"}

        if isinstance(rules, dict):
            rules = [rules]
        if not isinstance(rules, list) or not rules:
            return {"status": "error", "msg": "rules must be a non-empty array"}

        normalized: list[dict[str, Any]] = []
        for item in rules:
            if isinstance(item, str):
                try:
                    item = json.loads(item)
                except json.JSONDecodeError as exc:
                    return {"status": "error", "msg": f"invalid rule JSON: {exc}"}
            if not isinstance(item, dict):
                return {"status": "error", "msg": "each rule must be an object"}
            normalized.append(item)

        data = driver.intercept(resolved, normalized)
        return {
            "status": "success", "action": "apply",
            "tab_id": str(resolved), "result": data,
        }
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def _element_clip(
    driver: Any, tab_id: Any, selector: str, padding: int = 4,
) -> Dict[str, Any]:
    selector_json = json.dumps(selector)
    pad = max(0, min(int(padding or 0), 40))
    script = f"""return (() => {{
      const el = document.querySelector({selector_json});
      if (!el) return {{ok: false, error: 'selector not found'}};
      el.scrollIntoView({{block: 'center', inline: 'center', behavior: 'instant'}});
      const r = el.getBoundingClientRect();
      if (!r || (r.width === 0 && r.height === 0)) return {{ok: false, error: 'element has no visible box'}};
      const x = Math.max(0, r.left + window.scrollX - {pad});
      const y = Math.max(0, r.top + window.scrollY - {pad});
      return {{ok: true,
               x, y,
               width: r.width + {pad} * 2,
               height: r.height + {pad} * 2,
               tag: el.tagName,
               src: el.currentSrc || el.src || ''}};
    }})()"""
    data = _js_eval(driver, tab_id, script, timeout=5)
    if not isinstance(data, dict) or not data.get("ok"):
        message = data.get("error") if isinstance(data, dict) else "cannot resolve selector"
        raise ValueError(str(message or "cannot resolve selector"))
    return data


def web_captcha(
    selector: str,
    tab_id: Optional[str] = None,
    save_to_file: Optional[str] = None,
    engine: str = "auto",
    enhance: bool = False,
    padding: int = 4,
) -> dict:
    """Capture one element (captcha image) and optionally OCR it.

    Returns `text`/`normalized_text` when local OCR succeeds. If OCR is empty
    or unavailable, the saved image path is still returned so the agent can
    call `vision(image_path=..., prompt='读取验证码，只输出字符')` unattended.
    """
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        resolved = _resolve_tab_id(driver, tab_id)
        selector = str(selector or "").strip()
        if not selector:
            return {"status": "error", "msg": "selector is required"}

        clip = _element_clip(driver, resolved, selector, padding=padding)
        target = save_to_file or os.path.join(
            os.getcwd(), "screenshots",
            f"captcha-{resolved}-{int(time.time() * 1000)}.png",
        )
        result = driver.cdp(
            "Page.captureScreenshot",
            {
                "format": "png",
                "captureBeyondViewport": True,
                "clip": {
                    "x": clip["x"], "y": clip["y"],
                    "width": clip["width"], "height": clip["height"], "scale": 1,
                },
            },
            tab_id=resolved,
        )
        encoded = result.get("data") if isinstance(result, dict) else result
        image_bytes = _decode_base64_payload(encoded)
        try:
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "wb") as output:
                output.write(image_bytes)
        except OSError as exc:
            return {"status": "error", "msg": f"cannot save captcha image: {exc}"}

        payload: dict[str, Any] = {
            "status": "success",
            "tab_id": str(resolved),
            "selector": selector,
            "path": os.path.abspath(target),
            "bytes": len(image_bytes),
            "element": clip,
        }
        engine = str(engine or "auto").strip().lower()
        if engine not in {"auto", "ocr", "vision", "none"}:
            return {"status": "error", "msg": f"unknown engine: {engine}"}
        if engine in {"auto", "ocr"}:
            try:
                from zero_agent.utils.ocr_utils import ocr_image
                ocr = ocr_image(target, enhance=bool(enhance))
                text = str(ocr.get("text") or "")
                details = ocr.get("details") or []
                confidences = [
                    float(detail.get("conf") or 0) for detail in details
                    if detail.get("conf") is not None
                ]
                payload["ocr"] = ocr
                payload["text"] = text
                payload["normalized_text"] = re.sub(r"\s+", "", text)
                payload["confidence"] = (
                    sum(confidences) / len(confidences) if confidences else 0.0
                )
                if not text.strip():
                    payload["suggestion"] = (
                        "本地 OCR 为空；调用 vision(image_path=..., "
                        "prompt='读取验证码，只输出字符')"
                    )
            except Exception as exc:
                payload["ocr_error"] = format_error(exc)
                payload["suggestion"] = (
                    "本地 OCR 不可用；调用 vision(image_path=..., "
                    "prompt='读取验证码，只输出字符')"
                )
        elif engine == "vision":
            payload["suggestion"] = (
                "调用 vision(image_path=..., prompt='读取验证码，只输出字符')"
            )
        return payload
    except Exception as e:
        return {"status": "error", "msg": format_error(e)}


def web_wait(
    selector: Optional[str] = None,
    url_contains: Optional[str] = None,
    timeout_seconds: int = 20,
    tab_id: Optional[str] = None,
    poll_interval: float = 0.5,
) -> dict:
    """Wait until a selector exists and/or the URL contains a substring."""
    try:
        driver, error = _driver_or_error()
        if error:
            return error
        if not selector and not url_contains:
            return {"status": "error", "msg": "selector or url_contains is required"}
        resolved = _resolve_tab_id(driver, tab_id)
        timeout = _clamp_timeout(timeout_seconds, 20)
        interval = max(0.1, min(float(poll_interval or 0.5), 5.0))

        conditions = []
        if selector:
            conditions.append(f"document.querySelector({json.dumps(str(selector))}) !== null")
        if url_contains:
            conditions.append(f"String(location.href).includes({json.dumps(str(url_contains))})")
        script = f"return !!({' && '.join(conditions)})"

        started = time.time()
        last_error: Optional[str] = None
        while True:
            try:
                matched = bool(_js_eval(
                    driver, resolved, script,
                    timeout=min(2, max(1, int(timeout))),
                ))
            except Exception as exc:
                matched = False
                last_error = format_error(exc)
            if matched:
                return {
                    "status": "success", "tab_id": str(resolved),
                    "elapsed": round(time.time() - started, 2),
                    "selector": selector, "url_contains": url_contains,
                }
            if time.time() - started >= timeout:
                message = f"等待超时 ({timeout}s)"
                if last_error:
                    message += f": {last_error}"
                return {
                    "status": "error",
                    "msg": message,
                    "tab_id": str(resolved),
                    "selector": selector,
                    "url_contains": url_contains,
                }
            time.sleep(interval)
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
            "获取当前页面的简化HTML内容和标签页列表。会移除隐藏/浮动/被遮盖的元素。切换页面后一般应先调用查看",
            "Get simplified HTML and tab list. Removes hidden/floating/covered elements. "
            "Call after switching pages",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "tabs_only": {
                    "type": "boolean",
                    "description": _t(
                        "仅返回标签页列表和当前标签信息，不获取HTML内容",
                        "Show tab list only, no HTML",
                        lang,
                    ),
                },
                "switch_tab_id": {
                    "type": "string",
                    "description": _t(
                        "可选的标签页 ID。如果提供，系统将在扫描前切换到该标签页",
                        "[Optional] Tab ID to switch to",
                        lang,
                    ),
                },
                "text_only": {
                    "type": "boolean",
                    "description": _t(
                        "只要纯文本不要HTML",
                        "Plain text only, no HTML",
                        lang,
                    ),
                },
            },
        },
        handler=_make_web_scan_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("read", "network"),
    ))

    registry.register(ToolDefinition(
        name="web_execute_js",
        description=_t(
            "执行JS。支持Multi-call，用不同switch_tab_id并行操作多标签页。"
            "异步等待可用 timeout_seconds 设置上限（1-120秒）。"
            "禁止猜测，准确操作以减少 web_scan 调用。无script参数时执行正文 ```javascript 块，以免转义",
            "Execute JS. Multi-call OK with different switch_tab_id. No guessing. "
            "Use timeout_seconds (1-120) to bound awaited page results. "
            "Act accurately to reduce web_scan calls. Execute JS in ```javascript "
            "blocks if no script arg, prefer to avoid escaping",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "script": {
                    "type": "string",
                    "description": _t(
                        "[Optional] JS代码或路径。为免转义建议留空，改用正文代码块（与此参数互斥）",
                        "[Mutually exclusive] JS code or script path. NEVER use this param when use reply code block",
                        lang,
                    ),
                },
                "save_to_file": {
                    "type": "string",
                    "description": _t(
                        "结果存文件，适合返回值较长时",
                        "file path; **only** for long result",
                        lang,
                    ),
                },
                "no_monitor": {
                    "type": "boolean",
                    "description": _t(
                        "跳过页面变更监控，省2-3秒。仅在纯读取信息时设置，页面操作时不要设置",
                        "Skip page change monitoring, saves 2-3s. Only for reads, not for page actions",
                        lang,
                    ),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 15,
                    "description": _t(
                        "等待异步页面结果的超时秒数，默认15，最多120",
                        "Timeout in seconds for awaited page results; defaults to 15, maximum 120",
                        lang,
                    ),
                },
                "switch_tab_id": {
                    "type": "string",
                    "description": _t(
                        "可选的标签页 ID，切换到该标签页执行",
                        "[Optional] Tab ID to switch to before executing",
                        lang,
                    ),
                },
            },
        },
        evidence_kind="web",
        handler=_make_web_execute_js_handler(config),
        category="browser",
        effects=("write", "network"),
        write_path_keys=("save_to_file",),
        # save_to_file 可选：不传时结果只回给模型，不落盘（见 handler）
        optional_write_paths=True,
    ))

    registry.register(ToolDefinition(
        name="web_screenshot",
        description=_t(
            "使用浏览器 CDP 截取当前或指定标签页，并保存为图片文件；适合截图后交给 vision 分析。默认覆盖 workspace/screenshots/browser.png。",
            "Capture the current or selected tab through browser CDP and save an image file for vision analysis. "
            "Defaults to workspace/screenshots/browser.png and overwrites it.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "save_to_file": {
                    "type": "string",
                    "description": _t(
                        "workspace 下的图片路径，默认 screenshots/browser.png",
                        "Image path under workspace; defaults to screenshots/browser.png",
                        lang,
                    ),
                },
                "switch_tab_id": {
                    "type": "string",
                    "description": _t("可选标签页 ID", "Optional browser tab ID", lang),
                },
                "image_format": {
                    "type": "string",
                    "enum": ["png", "jpeg", "webp"],
                    "description": _t("图片格式，默认 png", "Image format; defaults to png", lang),
                },
                "capture_beyond_viewport": {
                    "type": "boolean",
                    "description": _t(
                        "是否截取视口外页面内容，默认 true",
                        "Whether to capture content beyond the viewport; defaults to true",
                        lang,
                    ),
                },
            },
        },
        handler=_make_web_screenshot_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("write",),
        write_path_keys=("save_to_file",),
        # save_to_file 可选：不传时写 workspace/screenshots/browser.png，
        # 目标由 handler 写死，模型控制不到，因此不需要审批
        optional_write_paths=True,
    ))

    registry.register(ToolDefinition(
        name="web_tabs",
        description=_t(
            "管理标签页：list/create/activate/navigate/reload/back/forward/close/close_duplicates。"
            "关闭或切换不依赖当前标签页。close_duplicates 默认 dry_run=true 只预览，"
            "确认后传 dry_run=false 才真正关闭重复页。",
            "Manage tabs: list/create/activate/navigate/reload/back/forward/close/close_duplicates. "
            "Tab lifecycle works without a live tab session. close_duplicates is a dry run "
            "unless dry_run=false is passed.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "create", "activate", "navigate", "reload", "back", "forward", "close", "close_duplicates"],
                    "description": _t("操作类型，默认 list", "Action; defaults to list", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "tab_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": _t("批量关闭的标签页 ID 列表", "Tab IDs to close", lang),
                },
                "url": {
                    "type": "string",
                    "description": _t("create/navigate 的目标 URL", "URL for create/navigate", lang),
                },
                "active": {
                    "type": "boolean",
                    "description": _t("create 时是否前台打开，默认 false", "Open created tab in foreground; default false", lang),
                },
                "include_all": {
                    "type": "boolean",
                    "description": _t("list 时包含 chrome:// 等不可脚本化标签页，默认 false", "Include non-scriptable tabs in list; default false", lang),
                },
                "dry_run": {
                    "type": "boolean",
                    "description": _t("close_duplicates 只预览不关闭，默认 true", "Preview duplicate cleanup without closing; default true", lang),
                },
                "ignore_cache": {
                    "type": "boolean",
                    "description": _t("reload 时忽略缓存，默认 false", "Ignore cache on reload; default false", lang),
                },
            },
        },
        handler=_make_web_tabs_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("control",),
    ))

    registry.register(ToolDefinition(
        name="upload",
        description=_t(
            "给页面的 <input type=file> 写入本机文件（支持多文件）。selector 必须定位到文件输入框；"
            "通过 CDP DOM.setFileInputFiles 写入，页面会收到正常的 input/change 事件。",
            "Attach local files to a page <input type=file> (multi-file supported). selector must "
            "point at the file input; CDP DOM.setFileInputFiles makes the page receive normal "
            "input/change events.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": _t("文件输入框的 CSS 选择器", "CSS selector for the file input", lang),
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": _t("本机文件路径（相对路径基于 workspace）", "Local file paths (relative paths use workspace)", lang),
                },
                "tab_id": {"type": "string", "description": _t("目标标签页 ID", "Target tab ID", lang)},
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": _t("多个匹配时选择第几个，默认 0", "Index among matching inputs; default 0", lang),
                },
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 15,
                    "description": _t("等待写入选文件的秒数", "Timeout in seconds", lang),
                },
            },
            "required": ["selector", "files"],
        },
        handler=_make_web_upload_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("read", "network"),
    ))

    registry.register(ToolDefinition(
        name="download",
        description=_t(
            "通过 chrome.downloads 下载文件：action=url 直接下载并返回绝对保存路径；"
            "action=click 先原生点击目标再等待下载；action=wait 等待页面自己触发的下载；"
            "action=list 列出最近下载。下载目录由 Chrome 默认设置决定。",
            "Download through chrome.downloads: action=url downloads a URL and returns the "
            "absolute path; action=click natively clicks a target then waits; action=wait waits "
            "for a page-triggered download; action=list lists recent downloads. The default "
            "download directory is controlled by Chrome.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["url", "click", "wait", "list"],
                    "description": _t("下载操作，默认 list", "Download action; defaults to list", lang),
                },
                "tab_id": {"type": "string", "description": _t("目标标签页 ID", "Target tab ID", lang)},
                "url": {"type": "string", "description": _t("action=url 的下载地址", "URL for action=url", lang)},
                "filename": {"type": "string", "description": _t("action=url 的保存文件名", "Filename for action=url", lang)},
                "target": {"type": "string", "description": _t("action=click 的 CSS 选择器", "CSS selector for action=click", lang)},
                "since": {"type": "integer", "description": _t("action=wait 的起始 epoch 毫秒", "Epoch ms lower bound for action=wait", lang)},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 20,
                    "description": _t("等待下载完成的秒数", "Download timeout in seconds", lang),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "default": 10,
                    "description": _t("action=list 返回条数", "Number of downloads for action=list", lang),
                },
            },
        },
        handler=_make_web_download_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("network",),
    ))

    registry.register(ToolDefinition(
        name="web_cdp",
        description=_t(
            "向指定标签页发送任意 Chrome DevTools Protocol 命令，例如 Input.dispatchMouseEvent、"
            "Network.enable、Page.printToPDF、Browser.setDownloadBehavior。"
            "persistent=true 时保持调试器附着以便接收事件（Network/Fetch/Page 域需要）。"
            "Fetch.enable 会暂停请求，必须自行 continue/fulfill 或 detach。"
            "printToPDF/captureScreenshot 等 base64 结果可传 save_to_file 直接落盘。",
            "Send any Chrome DevTools Protocol command to a tab, e.g. Input.dispatchMouseEvent, "
            "Network.enable, Page.printToPDF, Browser.setDownloadBehavior. "
            "persistent=true keeps the debugger attached for event domains. "
            "Fetch.enable pauses requests; continue/fulfill them or detach. "
            "Pass save_to_file to decode base64 results such as printToPDF/captureScreenshot.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "method": {
                    "type": "string",
                    "description": _t("CDP 方法名，如 Network.enable", "CDP method, e.g. Network.enable", lang),
                },
                "params": {
                    "type": "object",
                    "description": _t("CDP 参数对象", "CDP params object", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID，默认当前标签页", "Target tab ID; defaults to the active session", lang),
                },
                "persistent": {
                    "type": "boolean",
                    "description": _t("调用后保持附着以便接收事件，默认 false", "Keep the debugger attached after the call; default false", lang),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 15,
                    "description": _t("等待命令结果的秒数", "Command timeout in seconds", lang),
                },
                "save_to_file": {
                    "type": "string",
                    "description": _t(
                        "文件路径（可为绝对路径）；把 base64 结果（PDF/截图）写入该文件",
                        "File path (absolute allowed); decode base64 results (PDF/screenshot) into it",
                        lang,
                    ),
                },
            },
            "required": ["method"],
        },
        handler=_make_web_cdp_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("control", "network", "write"),
        write_path_keys=("save_to_file",),
        optional_write_paths=True,
    ))

    registry.register(ToolDefinition(
        name="web_input",
        description=_t(
            "通过 CDP 发送受信任的原生鼠标/键盘事件（isTrusted=true），适合普通 JS 事件被风控拒绝的登录、"
            "滑块和表单。click 支持 selector 或 x/y；drag 模拟按下-移动-释放，可用 selector/to_selector 或坐标；"
            "hover 只移动光标；wheel 发送滚轮；type 用 Input.insertText；key 支持 Enter/Tab/Escape 等。",
            "Dispatch trusted native mouse/key events through CDP (isTrusted=true), for logins, "
            "sliders and forms that reject synthetic JS events. click accepts selector or x/y; "
            "drag presses, moves and releases; hover moves the cursor; wheel scrolls; "
            "type uses Input.insertText; key supports Enter/Tab/Escape and more.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["click", "type", "key", "hover", "wheel", "drag"],
                    "description": _t("输入类型", "Input action", lang),
                },
                "selector": {
                    "type": "string",
                    "description": _t(
                        "CSS 选择器；click/type/hover/wheel 的定位，drag 的起点",
                        "CSS selector for click/type/hover/wheel, or drag start",
                        lang,
                    ),
                },
                "text": {
                    "type": "string",
                    "description": _t("type 要输入的文本", "Text for type", lang),
                },
                "key": {
                    "type": "string",
                    "description": _t("key 要按下的键，如 Enter/Tab/Escape/ArrowDown", "Key name for key, e.g. Enter/Tab/Escape", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "x": {"type": "number", "description": _t("视口 X 坐标；click/hover/wheel 的位置或 drag 起点", "Viewport X for click/hover/wheel or drag start", lang)},
                "y": {"type": "number", "description": _t("视口 Y 坐标；click/hover/wheel 的位置或 drag 起点", "Viewport Y for click/hover/wheel or drag start", lang)},
                "to_selector": {
                    "type": "string",
                    "description": _t("drag 终点的 CSS 选择器", "CSS selector for the drag end", lang),
                },
                "to_x": {"type": "number", "description": _t("drag 终点的视口 X 坐标", "Viewport X for the drag end", lang)},
                "to_y": {"type": "number", "description": _t("drag 终点的视口 Y 坐标", "Viewport Y for the drag end", lang)},
                "duration_ms": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 10000,
                    "description": _t("drag 的总时长毫秒，默认 500", "Total drag duration in ms; default 500", lang),
                },
                "steps": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "description": _t("drag 的移动步数，默认 12", "Drag movement steps; default 12", lang),
                },
                "delta_x": {"type": "number", "description": _t("wheel 的水平滚动量", "Horizontal wheel delta", lang)},
                "delta_y": {"type": "number", "description": _t("wheel 的垂直滚动量", "Vertical wheel delta", lang)},
                "button": {
                    "type": "string",
                    "enum": ["left", "right", "middle"],
                    "description": _t("鼠标按键，默认 left", "Mouse button; default left", lang),
                },
                "click_count": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": _t("连续点击次数，2 为双击", "Click count; 2 double-clicks", lang),
                },
                "modifiers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": _t("修饰键列表，如 Alt/Control/Meta/Shift", "Modifier keys, e.g. Alt/Control/Meta/Shift", lang),
                },
                "clear": {
                    "type": "boolean",
                    "description": _t("type 前选中已有内容，默认 false", "Select existing content before typing", lang),
                },
                "submit": {
                    "type": "boolean",
                    "description": _t("type 后按 Enter，默认 false", "Press Enter after typing", lang),
                },
            },
            "required": ["action"],
        },
        handler=_make_web_input_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("control",),
    ))

    registry.register(ToolDefinition(
        name="web_network",
        description=_t(
            "捕获指定标签页的 CDP Network 事件。start 开启持久捕获，dump 返回请求/响应/失败摘要，"
            "events 返回原始事件（如 Fetch.requestPaused），continue 放行被 Fetch 暂停的请求，"
            "stop 关闭并 detach。修改请求/响应用 web_cdp 的 Fetch 域配合 events/continue。",
            "Capture CDP Network events for a tab. start enables persistent capture; dump returns "
            "request/response/failure summaries; events returns raw events such as "
            "Fetch.requestPaused; continue releases paused Fetch requests; stop disables and "
            "detaches. Use web_cdp Fetch domain plus events/continue to modify traffic.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "dump", "events", "continue", "stop", "clear"],
                    "description": _t("操作类型，默认 dump", "Action; defaults to dump", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "default": 100,
                    "description": _t("最多返回的事件数", "Maximum events to return", lang),
                },
                "filter": {
                    "type": "string",
                    "enum": ["all", "requests", "responses", "failures"],
                    "description": _t("dump 的事件过滤，默认 requests", "dump event filter; defaults to requests", lang),
                },
                "methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": _t(
                        "events 的 CDP 方法过滤，如 [\"Fetch.requestPaused\"]；默认全部",
                        "CDP method filter for events, e.g. [\"Fetch.requestPaused\"]; defaults to all",
                        lang,
                    ),
                },
                "clear": {
                    "type": "boolean",
                    "description": _t("读取后清空缓冲区，默认 false", "Clear the buffer after reading", lang),
                },
            },
        },
        handler=_make_web_network_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("control", "network"),
    ))

    registry.register(ToolDefinition(
        name="web_intercept",
        description=_t(
            "无人值守改包：按 tab 安装 Fetch 规则，按顺序匹配，首个命中生效；"
            "未匹配请求自动 continue，不会把页面挂住。response_* 规则会自动启用 Response 阶段。"
            "request_headers/response_headers 的值为空字符串表示删除该头。"
            "用 action=clear 关闭，action=list 查看；决策日志用 web_network(action='events', methods=['ZeroAgent.intercept'])。",
            "Unattended traffic rewriting: install per-tab Fetch rules, first match wins; "
            "unmatched requests are continued automatically so pages never hang. response_* "
            "rules enable the Response stage. Empty header values remove a header. "
            "Use action=clear/list; inspect decisions with web_network(action='events', "
            "methods=['ZeroAgent.intercept']).",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["apply", "clear", "list"],
                    "description": _t("操作类型，默认 apply", "Action; defaults to apply", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "rules": {
                    "type": "array",
                    "description": _t("按顺序匹配的规则数组", "Ordered rule array", lang),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": _t("规则标识，便于日志", "Rule id for logs", lang)},
                            "url_contains": {"type": "string", "description": _t("URL 包含子串", "URL substring", lang)},
                            "url_regex": {"type": "string", "description": _t("URL 正则", "URL regex", lang)},
                            "method": {"type": "string", "description": _t("HTTP 方法，如 POST", "HTTP method, e.g. POST", lang)},
                            "request_headers": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": _t("请求头覆盖；空字符串删除", "Request header overrides; empty removes", lang),
                            },
                            "response_status": {
                                "type": "integer",
                                "minimum": 100,
                                "maximum": 599,
                                "description": _t("响应状态码覆盖", "Response status override", lang),
                            },
                            "response_headers": {
                                "type": "object",
                                "additionalProperties": {"type": "string"},
                                "description": _t("响应头覆盖；空字符串删除", "Response header overrides; empty removes", lang),
                            },
                            "response_body": {
                                "type": "string",
                                "description": _t("响应体覆盖（文本，或配合 base64）", "Response body override (text or base64)", lang),
                            },
                            "response_body_base64": {
                                "type": "boolean",
                                "description": _t("response_body 已是 base64", "response_body is already base64", lang),
                            },
                            "abort": {
                                "type": "boolean",
                                "description": _t("直接失败该请求", "Fail the request", lang),
                            },
                            "abort_reason": {
                                "type": "string",
                                "description": _t("失败原因，如 Aborted/AccessDenied", "Failure reason, e.g. Aborted", lang),
                            },
                            "delay_ms": {
                                "type": "integer",
                                "minimum": 0,
                                "maximum": 60000,
                                "description": _t("放行前延迟毫秒", "Delay before continuing, ms", lang),
                            },
                        },
                    },
                },
            },
        },
        handler=_make_web_intercept_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("control", "network"),
    ))

    registry.register(ToolDefinition(
        name="web_captcha",
        description=_t(
            "截取验证码/图片元素并识别，返回 text/normalized_text/path。"
            "engine=auto 先本地 OCR，文本为空时自动调用当前视觉后端；engine=ocr 只本地；"
            "engine=vision 直接用视觉后端；engine=none 只截图。"
            "识别后用 web_input(action='type', selector=输入框, text=验证码, submit=True) 无人值守提交。",
            "Capture a captcha/image element and read it, returning text/normalized_text/path. "
            "engine=auto tries local OCR first, then the active vision backend when OCR is empty. "
            "engine=ocr local only; engine=vision vision only; engine=none capture only. "
            "Then use web_input(action='type', selector=..., text=..., submit=True) unattended.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": _t("验证码元素的 CSS 选择器", "CSS selector for the captcha element", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "save_to_file": {
                    "type": "string",
                    "description": _t("图片路径（相对路径基于 workspace）；默认 screenshots/captcha-*.png", "Image path (relative paths use workspace); defaults to screenshots/captcha-*.png", lang),
                },
                "engine": {
                    "type": "string",
                    "enum": ["auto", "ocr", "vision", "none"],
                    "default": "auto",
                    "description": _t(
                        "auto=本地 OCR 失败后自动用视觉后端；ocr=只本地；vision=只用视觉；none=只截图",
                        "auto=local OCR then vision fallback; ocr=local only; vision=vision only; none=capture only",
                        lang,
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": _t(
                        "视觉识别提示词，默认要求只输出验证码字符",
                        "Vision prompt; defaults to captcha characters only",
                        lang,
                    ),
                },
                "enhance": {
                    "type": "boolean",
                    "description": _t("OCR 前增强对比度", "Enhance contrast before OCR", lang),
                },
                "padding": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": 40,
                    "description": _t("截图外扩像素，默认 4", "Capture padding in pixels; default 4", lang),
                },
            },
            "required": ["selector"],
        },
        handler=_make_web_captcha_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("read", "write"),
        write_path_keys=("save_to_file",),
        optional_write_paths=True,
    ))

    registry.register(ToolDefinition(
        name="web_wait",
        description=_t(
            "等待页面条件成立：元素出现（selector）和/或 URL 包含子串（url_contains）。"
            "替代 sleep 后反复 scan。超时返回 error。",
            "Wait until a selector exists and/or the URL contains a substring. Replaces "
            "sleep-then-scan loops. Returns error on timeout.",
            lang,
        ),
        parameters={
            "type": "object",
            "properties": {
                "selector": {
                    "type": "string",
                    "description": _t("等待出现的 CSS 选择器", "CSS selector to wait for", lang),
                },
                "url_contains": {
                    "type": "string",
                    "description": _t("等待 URL 包含的子串", "Substring the URL must contain", lang),
                },
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "default": 20,
                    "description": _t("最长等待秒数，默认 20", "Timeout in seconds; default 20", lang),
                },
                "tab_id": {
                    "type": "string",
                    "description": _t("目标标签页 ID", "Target tab ID", lang),
                },
                "poll_interval": {
                    "type": "number",
                    "description": _t("轮询间隔秒数，默认 0.5", "Poll interval in seconds; default 0.5", lang),
                },
            },
        },
        handler=_make_web_wait_handler(config),
        evidence_kind="web",
        category="browser",
        effects=("read",),
    ))


def build_web_tools(config: AgentConfig) -> list:
    """Build browser tool definitions without registering them.

    Browser control is an optional capability extension. ``with_builtins()`` no
    longer registers these tools; the extension calls this builder and registers
    the result through ``ExtensionRuntime.register_tool`` so ownership, release
    and registry-rebuild replay keep working.

    Args:
        config: Agent 配置.

    Returns:
        未注册的 ToolDefinition 列表.
    """
    scratch = ToolRegistry()
    register_web_tools(scratch, config)
    return scratch.list_all()


def _make_web_scan_handler(config: AgentConfig):
    """创建 web_scan 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
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

        content = result.get("content")
        display_result = {k: v for k, v in result.items() if k != "content"}
        yield f"[Info] {str(display_result)}\n"

        if content:
            output = json.dumps(display_result, ensure_ascii=False)
            output += f"\n```html\n{content}\n```"
            yield output + "\n"

        return StepOutcome(result, next_prompt="\n", action=StepAction.CONTINUE)
    return _handler


def _make_web_execute_js_handler(config: AgentConfig):
    """创建 web_execute_js 的 ToolHandler 适配器."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        script = args.get("script", "")
        if not script:
            # 回退: 从 LLM 响应提取 JavaScript 代码块
            script = handler._extract_code_block(_response, "javascript")
        if not script:
            # 二级回退: 从响应提取任意代码块
            script = handler._extract_code_block(_response)
        if not script:
            return StepOutcome(
                "[Error] Script missing. Use ```javascript block or 'script' arg.",
                next_prompt="\n",
                action=StepAction.CONTINUE,
            )

        import os
        # 支持从工作目录读取脚本文件。
        script_ref = script.strip()
        candidate = script_ref if os.path.isabs(script_ref) else os.path.join(config.workspace_dir, script_ref)
        if os.path.isfile(candidate):
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    script = f.read()
            except Exception:
                pass

        save_to_file = args.get("save_to_file", "")
        switch_tab_id = args.get("switch_tab_id") or args.get("tab_id")
        no_monitor = args.get("no_monitor", False)
        timeout_seconds = args.get("timeout_seconds")

        execute_kwargs: Dict[str, Any] = {
            "switch_tab_id": switch_tab_id,
            "no_monitor": no_monitor,
        }
        if timeout_seconds is not None:
            try:
                execute_kwargs["timeout_seconds"] = _clamp_timeout(timeout_seconds)
            except (TypeError, ValueError, OverflowError):
                execute_kwargs["timeout_seconds"] = 15
        result = web_execute_js(script, **execute_kwargs)

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
        show = smart_format(
            json.dumps(result, ensure_ascii=False, indent=2),
            max_str_len=300,
        )
        yield f"JS 执行结果:\n{show}\n"

        return StepOutcome(
            result,
            action=StepAction.CONTINUE,
        )
    return _handler


def _make_web_screenshot_handler(config: AgentConfig):
    """Create the screenshot tool adapter and resolve paths inside workspace."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        relative_path = str(args.get("save_to_file") or "screenshots/browser.png")
        save_path = os.path.join(config.workspace_dir, relative_path)
        result = web_screenshot(
            save_path,
            switch_tab_id=args.get("switch_tab_id") or args.get("tab_id"),
            image_format=str(args.get("image_format") or "png"),
            capture_beyond_viewport=bool(args.get("capture_beyond_viewport", True)),
        )
        yield f"浏览器截图结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_tabs_handler(config: AgentConfig):
    """Create the tab lifecycle tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_tabs(
            action=str(args.get("action") or "list"),
            tab_id=args.get("tab_id"),
            tab_ids=args.get("tab_ids"),
            url=args.get("url"),
            active=bool(args.get("active", False)),
            include_all=bool(args.get("include_all", False)),
            dry_run=bool(args.get("dry_run", True)),
            ignore_cache=bool(args.get("ignore_cache", False)),
        )
        yield f"标签页操作结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_upload_handler(config: AgentConfig):
    """Create the browser upload adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        from zero_agent.tools.builtin.file import resolve_file_path

        files = args.get("files") or []
        if isinstance(files, str):
            files = [files]
        resolved = [resolve_file_path(str(item), config) for item in files]
        result = web_upload(
            selector=str(args.get("selector") or ""),
            files=resolved,
            tab_id=args.get("tab_id"),
            index=args.get("index", 0),
            timeout_seconds=args.get("timeout_seconds", 15),
        )
        yield f"上传结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_download_handler(config: AgentConfig):
    """Create the browser download adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_download(
            action=str(args.get("action") or "list"),
            tab_id=args.get("tab_id"),
            url=args.get("url"),
            filename=args.get("filename"),
            target=args.get("target"),
            since=args.get("since"),
            timeout_seconds=args.get("timeout_seconds", 20),
            limit=args.get("limit", 10),
        )
        yield f"下载结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_cdp_handler(config: AgentConfig):
    """Create the raw CDP tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        relative = args.get("save_to_file")
        save_path = os.path.join(config.workspace_dir, str(relative)) if relative else None
        result = web_cdp(
            method=str(args.get("method") or ""),
            params=args.get("params"),
            tab_id=args.get("tab_id"),
            persistent=bool(args.get("persistent", False)),
            timeout_seconds=args.get("timeout_seconds", _DEFAULT_TOOL_TIMEOUT),
            save_to_file=save_path,
        )
        yield f"CDP 执行结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_input_handler(config: AgentConfig):
    """Create the trusted input tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_input(
            action=str(args.get("action") or ""),
            selector=args.get("selector"),
            text=args.get("text"),
            key=args.get("key"),
            tab_id=args.get("tab_id"),
            x=args.get("x"),
            y=args.get("y"),
            button=str(args.get("button") or "left"),
            click_count=args.get("click_count", 1),
            modifiers=args.get("modifiers"),
            clear=bool(args.get("clear", False)),
            submit=bool(args.get("submit", False)),
            to_selector=args.get("to_selector"),
            to_x=args.get("to_x"),
            to_y=args.get("to_y"),
            duration_ms=args.get("duration_ms", 500),
            steps=args.get("steps", 12),
            delta_x=args.get("delta_x", 0),
            delta_y=args.get("delta_y", 0),
        )
        yield f"原生输入结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_network_handler(config: AgentConfig):
    """Create the network capture tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_network(
            action=str(args.get("action") or "dump"),
            tab_id=args.get("tab_id"),
            limit=args.get("limit", 100),
            filter=str(args.get("filter") or "requests"),
            clear=bool(args.get("clear", False)),
            methods=args.get("methods"),
        )
        yield f"网络捕获结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_intercept_handler(config: AgentConfig):
    """Create the unattended interception tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_intercept(
            action=str(args.get("action") or "apply"),
            rules=args.get("rules"),
            tab_id=args.get("tab_id"),
        )
        yield f"拦截规则结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_captcha_handler(config: AgentConfig):
    """Create the captcha capture/OCR tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        tab_id = args.get("tab_id") or "current"
        relative = args.get("save_to_file") or (
            f"screenshots/captcha-{tab_id}-{int(time.time() * 1000)}.png"
        )
        save_path = os.path.join(config.workspace_dir, str(relative))
        engine = str(args.get("engine") or "auto").strip().lower()
        result = web_captcha(
            selector=str(args.get("selector") or ""),
            tab_id=args.get("tab_id"),
            save_to_file=save_path,
            engine=engine,
            enhance=bool(args.get("enhance", False)),
            padding=args.get("padding", 4),
        )
        if result.get("status") == "success" and engine in {"auto", "vision"}:
            text = str(result.get("text") or "").strip()
            confidence = float(result.get("confidence") or 0.0)
            if engine == "vision" or not text or confidence < 0.5:
                try:
                    from zero_agent.tools.builtin.vision import analyze_image
                    prompt = str(
                        args.get("prompt")
                        or "读取图片中的验证码，只输出字符，不要解释。"
                    )
                    vision = analyze_image(
                        config, _handler_obj, str(result.get("path") or ""), prompt,
                    )
                    result["vision"] = vision
                    if vision.get("status") == "success":
                        content = str(vision.get("content") or "").strip()
                        result["text"] = content
                        result["normalized_text"] = re.sub(r"\s+", "", content)
                        result["ocr_engine"] = "vision"
                    else:
                        result["vision_error"] = vision.get("msg")
                except Exception as exc:
                    result["vision_error"] = format_error(exc)
        yield f"验证码识别结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler


def _make_web_wait_handler(config: AgentConfig):
    """Create the page wait tool adapter."""
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        _handler_obj: Any,
    ) -> Generator[str, None, dict | StepOutcome]:
        result = web_wait(
            selector=args.get("selector"),
            url_contains=args.get("url_contains"),
            timeout_seconds=args.get("timeout_seconds", 20),
            tab_id=args.get("tab_id"),
            poll_interval=args.get("poll_interval", 0.5),
        )
        yield f"页面等待结果:\n{json.dumps(result, ensure_ascii=False)}\n"
        return StepOutcome(result, action=StepAction.CONTINUE)
    return _handler
