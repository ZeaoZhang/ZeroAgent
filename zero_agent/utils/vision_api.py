"""Vision API — 图片理解和视觉分析.

支持 Claude / OpenAI / ModelScope 三种后端，可通过 DEFAULT_BACKEND 切换.

依赖: pip install requests Pillow
"""

from __future__ import annotations

import base64
import io
import os
from typing import Any, Optional, Union

import requests

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None  # type: ignore

# 默认后端: "claude" | "openai" | "modelscope"
DEFAULT_BACKEND: str = os.environ.get("ZA_VISION_BACKEND", "claude")

# ModelScope 默认模型
MODELSCOPE_MODEL: str = os.environ.get(
    "ZA_VISION_MODEL", "Qwen/Qwen3-VL-235B-A22B-Instruct"
)


def ask_vision(
    image_input: Union[str, Any],
    prompt: str = "",
    max_pixels: int = 1440000,
    backend: Optional[str] = None,
    timeout: int = 60,
) -> str:
    """向 Vision API 发送图片并获取描述.

    Args:
        image_input: 图片路径或 PIL Image 对象.
        prompt: 提问文本（默认为 "详细描述这张图片的内容"）.
        max_pixels: 图片最大像素数（超出则缩放）.
        backend: 后端选择 ("claude"|"openai"|"modelscope")，None 使用默认.
        timeout: 请求超时秒数.

    Returns:
        API 返回的文本描述，失败则返回错误信息.
    """
    if not prompt:
        prompt = "详细描述这张图片的内容"

    try:
        image_b64 = _prepare_image(image_input, max_pixels)
    except Exception as e:
        return f"[Vision Error] Image preparation failed: {e}"

    backend = backend or DEFAULT_BACKEND

    if backend == "claude":
        return _ask_claude(image_b64, prompt, timeout)
    elif backend == "openai":
        return _ask_openai(image_b64, prompt, timeout)
    elif backend == "modelscope":
        return _ask_modelscope(image_b64, prompt, timeout)
    else:
        return f"[Vision Error] Unknown backend: {backend}"


def _prepare_image(
    image_input: Union[str, Any],
    max_pixels: int,
) -> str:
    """准备图片：加载、缩放、编码为 base64 JPEG.

    Args:
        image_input: 图片路径或 PIL Image.
        max_pixels: 最大像素数.

    Returns:
        base64 编码的 JPEG 图片字符串.
    """
    if Image is None:
        raise ImportError("Pillow not installed. Install with: pip install Pillow")

    if isinstance(image_input, str):
        img = Image.open(image_input)
    else:
        img = image_input

    # 缩放
    w, h = img.size
    if w * h > max_pixels:
        scale = (max_pixels / (w * h)) ** 0.5
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = img.resize((new_w, new_h), Image.LANCZOS)

    # 转为 RGB
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _ask_claude(image_b64: str, prompt: str, timeout: int) -> str:
    """通过 Anthropic Messages API 调用 vision.

    Args:
        image_b64: base64 图片.
        prompt: 提问文本.
        timeout: 超时秒数.

    Returns:
        响应文本.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return "[Vision Error] ANTHROPIC_API_KEY not set"

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": os.environ.get("ZA_VISION_CLAUDE_MODEL", "claude-sonnet-4-6"),
            "max_tokens": 1024,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
        },
        timeout=timeout,
    )

    if resp.status_code == 200:
        data = resp.json()
        for block in data.get("content", []):
            if block.get("type") == "text":
                return block["text"]
        return "[Vision] No text in response"
    return f"[Vision Error] Claude API: {resp.status_code} {resp.text[:200]}"


def _ask_openai(image_b64: str, prompt: str, timeout: int) -> str:
    """通过 OpenAI-compatible API 调用 vision.

    Args:
        image_b64: base64 图片.
        prompt: 提问文本.
        timeout: 超时秒数.

    Returns:
        响应文本.
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    api_base = os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
    if not api_key:
        return "[Vision Error] OPENAI_API_KEY not set"

    resp = requests.post(
        f"{api_base.rstrip('/')}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": os.environ.get("ZA_VISION_OPENAI_MODEL", "gpt-4o"),
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": 1024,
        },
        timeout=timeout,
    )

    if resp.status_code == 200:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    return f"[Vision Error] OpenAI API: {resp.status_code} {resp.text[:200]}"


def _ask_modelscope(image_b64: str, prompt: str, timeout: int) -> str:
    """通过 ModelScope API 调用 vision.

    Args:
        image_b64: base64 图片.
        prompt: 提问文本.
        timeout: 超时秒数.

    Returns:
        响应文本.
    """
    api_key = os.environ.get("MODELSCOPE_API_KEY", "")
    if not api_key:
        return "[Vision Error] MODELSCOPE_API_KEY not set"

    resp = requests.post(
        f"https://api-inference.modelscope.cn/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODELSCOPE_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_b64}"
                        },
                    },
                    {"type": "text", "text": prompt},
                ],
            }],
            "max_tokens": 1024,
        },
        timeout=timeout,
    )

    if resp.status_code == 200:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    return f"[Vision Error] ModelScope API: {resp.status_code} {resp.text[:200]}"
