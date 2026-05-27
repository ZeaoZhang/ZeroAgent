"""vision — 图片理解和视觉分析工具.

调用 Vision API 对图片进行描述和分析.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from zero_agent.tools.registry import ToolDefinition


def vision_tool(image_path: str, prompt: str = "") -> Dict[str, Any]:
    """对图片进行视觉分析.

    Args:
        image_path: 图片文件路径.
        prompt: 分析提示（默认为详细描述）.

    Returns:
        {"description": str, "backend": str}
    """
    try:
        from zero_agent.memory.vision_api import ask_vision
    except ImportError:
        return {
            "description": "[Error] vision_api module not available. "
                          "Install requests and Pillow.",
            "backend": "none",
        }

    description = ask_vision(image_path, prompt=prompt)
    return {
        "description": description,
        "backend": os.environ.get("ZA_VISION_BACKEND", "claude"),
    }


def register_vision_tool(registry: Any) -> None:
    """向 ToolRegistry 注册 vision 工具.

    Args:
        registry: ToolRegistry 实例.
    """
    tool_def = ToolDefinition(
        name="vision",
        description="分析图片内容，返回图片的详细描述。/ Analyze image content and return description.",
        parameters={
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "图片文件路径 / Image file path",
                },
                "prompt": {
                    "type": "string",
                    "description": "分析提示（可选，默认详细描述图片）/ Analysis prompt (optional)",
                },
            },
            "required": ["image_path"],
        },
        handler=lambda args, resp, hs: vision_tool(
            image_path=args.get("image_path", ""),
            prompt=args.get("prompt", ""),
        ),
        category="vision",
    )
    registry.register(tool_def)
