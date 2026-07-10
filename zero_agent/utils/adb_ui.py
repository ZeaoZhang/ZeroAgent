"""Android ADB UI 自动化工具.

ui: 一键 dump + 解析 Android UI 层级 (uiautomator2 优先，原生 fallback)。
tap: 模拟点击屏幕坐标。
swipe: 模拟滑动操作。
screenshot: 截取当前屏幕。

使用说明:
- dump 配合 ui_detect，微信/支付宝小程序常需 detect 补盲
- 搜索框一般直接输入拼音/首字母即可，别硬啃 adb 中文输入
- u2 (uiautomator2) 不受 idle 限制，适合动画密集 app（如美团）
- 弹窗检测: ui(clickable_only=True, raw=True) 找全屏 FrameLayout + 底部小 ImageView(关闭 X)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Optional

ADB = shutil.which("adb") or "adb"
LOCAL_XML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui_mt.xml")


def _dump_u2() -> Optional[str]:
    """用 uiautomator2 dump，不受 idle 限制.

    Returns:
        XML 层级字符串，失败时返回 None.
    """
    try:
        import uiautomator2 as u2  # type: ignore[import-untyped]

        d = u2.connect()
        xml_str = d.dump_hierarchy()
        if xml_str and len(xml_str) > 100:
            return xml_str
    except Exception as e:
        print(f"[u2 fallback] {e}")
    return None


def _dump_native() -> Optional[str]:
    """原生 uiautomator dump（需 idle 状态）.

    Returns:
        XML 层级字符串，失败时返回 None.
    """
    subprocess.run(
        [ADB, "shell", "rm", "-f", "/sdcard/ui.xml"], capture_output=True
    )
    r = subprocess.run(
        [ADB, "shell", "uiautomator", "dump", "--compressed", "/sdcard/ui.xml"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if "dumped" not in r.stdout.lower() and "dumped" not in r.stderr.lower():
        print(f"dump failed: {r.stdout}{r.stderr}")
        return None
    subprocess.run(
        [ADB, "pull", "/sdcard/ui.xml", LOCAL_XML],
        capture_output=True,
        timeout=10,
    )
    with open(LOCAL_XML, encoding="utf-8") as f:
        return f.read()


def _parse_xml(
    xml_str: str,
    keyword: Optional[str] = None,
    clickable_only: bool = False,
    raw: bool = False,
) -> list[dict[str, Any]]:
    """解析 UI 层级 XML 字符串为节点列表.

    Args:
        xml_str: UI 层级 XML 字符串.
        keyword: 过滤含关键词的节点（匹配 text 或 content-desc）.
        clickable_only: 只返回可点击节点.
        raw: 返回所有节点（包括无标签节点）.

    Returns:
        节点字典列表，每项包含 text, click, edit, cx, cy, cls, rid.
    """
    root = ET.fromstring(xml_str)
    nodes: list[dict[str, Any]] = []
    for n in root.iter("node"):
        pkg = n.get("package", "")
        if "termux" in pkg.lower():
            continue
        text = n.get("text", "")
        desc = n.get("content-desc", "")
        bounds = n.get("bounds", "")
        click = n.get("clickable") == "true"
        cls = n.get("class", "").split(".")[-1]
        rid = n.get("resource-id", "")
        label = text or desc
        if not label and not click and not raw:
            continue
        if clickable_only and not click:
            continue
        if keyword and keyword.lower() not in label.lower():
            continue
        cx, cy = 0, 0
        if bounds:
            m = re.findall(r"\[(\d+),(\d+)\]", bounds)
            if len(m) == 2:
                cx = (int(m[0][0]) + int(m[1][0])) // 2
                cy = (int(m[0][1]) + int(m[1][1])) // 2
        edit = cls == "EditText"
        nodes.append(
            {
                "text": text or desc,
                "click": click,
                "edit": edit,
                "cx": cx,
                "cy": cy,
                "cls": cls,
                "rid": rid,
            }
        )
    return nodes


def ui(
    keyword: Optional[str] = None,
    clickable_only: bool = False,
    raw: bool = False,
) -> list[dict[str, Any]]:
    """一键 dump + 解析 Android UI (u2 优先).

    Args:
        keyword: 过滤含关键词的节点（匹配 text 或 content-desc）.
        clickable_only: 只显示可点击节点.
        raw: 返回原始节点列表而非打印.

    Returns:
        节点字典列表.
    """
    xml_str = _dump_u2() or _dump_native()
    if not xml_str:
        print("dump failed (both u2 and native)")
        return []
    nodes = _parse_xml(xml_str, keyword, clickable_only, raw)
    if not raw:
        for n in nodes:
            flag = "E" if n.get("edit") else ("Y" if n["click"] else " ")
            coord = f"({n['cx']},{n['cy']})" if n["cx"] else ""
            display_text: str = n["text"]
            if not display_text:
                hint = n.get("rid", "").split("/")[-1] or n.get("cls", "icon")
                display_text = f"<{hint}>"
            print(f"[{flag}] {display_text}  {coord}")
        print(f"\ntotal: {len(nodes)} nodes")
    return nodes


def tap(x: int, y: int) -> None:
    """模拟点击屏幕坐标.

    Args:
        x: 横坐标.
        y: 纵坐标.
    """
    subprocess.run(
        [ADB, "shell", "input", "tap", str(x), str(y)], capture_output=True
    )
    print(f"tap({x},{y}) ok")


def swipe(
    x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300
) -> None:
    """模拟滑动操作.

    Args:
        x1: 起始横坐标.
        y1: 起始纵坐标.
        x2: 结束横坐标.
        y2: 结束纵坐标.
        duration_ms: 滑动持续时间（毫秒）.
    """
    subprocess.run(
        [
            ADB,
            "shell",
            "input",
            "swipe",
            str(x1),
            str(y1),
            str(x2),
            str(y2),
            str(duration_ms),
        ],
        capture_output=True,
    )
    print(f"swipe({x1},{y1}) -> ({x2},{y2}) ok")


def screenshot(output_path: Optional[str] = None) -> Optional[str]:
    """截取当前屏幕.

    Args:
        output_path: 截图保存路径，默认保存到 LOCAL_XML 同目录下的 ui_screenshot.png.

    Returns:
        截图文件的绝对路径，失败时返回 None.
    """
    if output_path is None:
        output_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "ui_screenshot.png"
        )
    r = subprocess.run(
        [ADB, "shell", "screencap", "-p", "/sdcard/ui_screenshot.png"],
        capture_output=True,
    )
    if r.returncode != 0:
        print(f"screencap failed: {r.stderr}")
        return None
    subprocess.run(
        [ADB, "pull", "/sdcard/ui_screenshot.png", output_path],
        capture_output=True,
        timeout=10,
    )
    print(f"screenshot saved: {output_path}")
    return os.path.abspath(output_path)


def find_element(
    keyword: str,
    clickable_only: bool = True,
) -> Optional[dict[str, Any]]:
    """查找包含关键词的第一个可交互 UI 元素.

    Args:
        keyword: 要匹配的关键词.
        clickable_only: 是否只查找可点击元素.

    Returns:
        匹配的节点字典，含 text, click, cx, cy 等字段；未找到返回 None.
    """
    nodes = ui(keyword=keyword, clickable_only=clickable_only, raw=True)
    return nodes[0] if nodes else None


if __name__ == "__main__":
    ui()
