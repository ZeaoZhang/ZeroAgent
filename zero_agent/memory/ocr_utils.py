"""OCR 工具 — 基于 RapidOCR 的本地 OCR 识别.

支持截图 OCR 和图片文件 OCR，返回识别文本和位置信息.

依赖: pip install rapidocr-onnxruntime Pillow
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Union

try:
    from PIL import Image, ImageGrab, ImageEnhance
except ImportError:  # pragma: no cover
    Image = None  # type: ignore
    ImageGrab = None  # type: ignore
    ImageEnhance = None  # type: ignore

_RAPID_OCR = None


def _get_ocr():
    """获取 RapidOCR 单例."""
    global _RAPID_OCR
    if _RAPID_OCR is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
            _RAPID_OCR = RapidOCR()
        except ImportError:
            raise ImportError(
                "RapidOCR not installed. Install with: "
                "pip install rapidocr-onnxruntime"
            )
    return _RAPID_OCR


def ocr_image(
    image_input: Union[str, Any],
    enhance: bool = False,
) -> Dict[str, Any]:
    """对图片执行 OCR 识别.

    Args:
        image_input: PIL Image 对象或图片文件路径.
        enhance: 是否启用对比度增强（可能损害可读文本）.

    Returns:
        {"text": 全文, "lines": [行文本列表], "details": [{bbox, text, conf}]}
    """
    if Image is None:
        raise ImportError("Pillow not installed. Install with: pip install Pillow")

    if isinstance(image_input, str):
        if not os.path.isfile(image_input):
            return {"text": "", "lines": [], "details": [],
                    "error": f"File not found: {image_input}"}
        img = Image.open(image_input)
    else:
        img = image_input

    if enhance:
        img = _preprocess(img)

    engine = _get_ocr()
    result = engine(img)

    lines: list[str] = []
    details: list[dict] = []
    full_text_parts: list[str] = []

    if result is not None:
        for item in result:
            text = str(item[1]) if item[1] else ""
            conf = float(item[2]) if len(item) > 2 and item[2] else 0.0
            bbox = item[0] if item[0] else []
            full_text_parts.append(_strip_cjk_spaces(text))
            lines.append(text)
            details.append({"bbox": bbox, "text": text, "conf": conf})

    return {
        "text": "".join(full_text_parts),
        "lines": lines,
        "details": details,
    }


def ocr_screen(
    bbox: Optional[tuple] = None,
    enhance: bool = False,
) -> Dict[str, Any]:
    """截取屏幕并执行 OCR.

    Args:
        bbox: 截取区域 (x1, y1, x2, y2)，None 表示全屏.
        enhance: 是否启用对比度增强.

    Returns:
        {"text": 全文, "lines": [行文本列表], "details": [{bbox, text, conf}]}
    """
    if ImageGrab is None:
        raise ImportError("Pillow not installed. Install with: pip install Pillow")

    img = ImageGrab.grab(bbox=bbox)
    return ocr_image(img, enhance=enhance)


def ocr_window(hwnd: int, enhance: bool = False) -> Dict[str, Any]:
    """截取窗口并执行 OCR（使用 PrintWindow API，支持远程桌面断开场景）.

    ImageGrab.grab() 在 RDP 断开后返回全黑截图，PrintWindow 不受此限制.

    Args:
        hwnd: 窗口句柄（Windows HWND）.
        enhance: 是否启用对比度增强.

    Returns:
        {"text": 全文, "lines": [行文本列表], "details": [{bbox, text, conf}]}

    Raises:
        NotImplementedError: 非 Windows 平台上调用.
    """
    if sys.platform != "win32":
        raise NotImplementedError("ocr_window() is only available on Windows")

    import win32gui
    import win32ui
    from ctypes import windll

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width, height = right - left, bottom - top
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    save_bitmap = win32ui.CreateBitmap()
    save_bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
    save_dc.SelectObject(save_bitmap)
    windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 3)
    bmpinfo = save_bitmap.GetInfo()
    bmpstr = save_bitmap.GetBitmapBits(True)
    img = Image.frombuffer(
        "RGB",
        (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
        bmpstr,
        "raw",
        "BGRX",
        0,
        1,
    )
    win32gui.DeleteObject(save_bitmap.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return ocr_image(img, enhance=enhance)


def _preprocess(img: Any) -> Any:
    """图像预处理：3x 缩放 + 3.0 对比度增强.

    Args:
        img: PIL Image 对象.

    Returns:
        处理后的 PIL Image.
    """
    w, h = img.size
    img = img.resize((w * 3, h * 3), Image.LANCZOS)
    enhancer = ImageEnhance.Contrast(img)
    return enhancer.enhance(3.0)


def _strip_cjk_spaces(text: str) -> str:
    """移除 CJK 字符之间的多余空格.

    Args:
        text: 原始文本.

    Returns:
        清理后的文本.
    """
    import re
    return re.sub(
        r'(?<=[一-鿿぀-ゟ゠-ヿ])\s+'
        r'(?=[一-鿿぀-ゟ゠-ヿ])',
        '', text,
    )
