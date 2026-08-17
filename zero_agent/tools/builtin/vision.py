"""Configured multimodal vision tool."""

from __future__ import annotations

from typing import Any, Dict, Generator

from zero_agent.core.config import AgentConfig
from zero_agent.core.types import StepAction, StepOutcome
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


def _t(zh: str, en: str, lang: str) -> str:
    return zh if lang == "zh" else en


def register_vision_tools(registry: ToolRegistry, config: AgentConfig) -> None:
    """Register vision only when at least one backend supports images."""
    backend_names = sorted(
        name for name, backend in config.llm_backends.items() if backend.vision
    )
    if not backend_names:
        return

    lang = config.resolved_tool_language
    registry.register(
        ToolDefinition(
            name="vision",
            description=_t(
                "使用已配置的视觉模型理解图片。DeepSeek Flash 不支持视觉；backend 留空时使用默认 backend。",
                "Understand an image with a configured vision backend. DeepSeek Flash is text-only; "
                "omit backend to use the configured default backend.",
                lang,
            ),
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": _t("图片文件路径", "Path to the image file", lang),
                    },
                    "prompt": {
                        "type": "string",
                        "description": _t("图片理解问题", "Question about the image", lang),
                    },
                    "backend": {
                        "type": "string",
                        "enum": backend_names,
                        "description": _t(
                            "可选视觉 backend；只能选择支持视觉的配置项",
                            "Optional vision backend; choose a configured vision backend",
                            lang,
                        ),
                    },
                },
                "required": ["image_path"],
            },
            handler=_make_vision_handler(config),
            category="vision",
        )
    )


def _make_vision_handler(config: AgentConfig):
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, StepOutcome]:
        image_path = str(args.get("image_path") or "")
        backend_name = args.get("backend") or config.default_backend
        backend_config = config.llm_backends.get(backend_name)
        if backend_config is None:
            return StepOutcome(
                {"status": "error", "msg": f"unknown backend: {backend_name}"},
                action=StepAction.CONTINUE,
            )
        if not backend_config.vision:
            return StepOutcome(
                {"status": "error", "msg": f"backend does not support vision: {backend_name}"},
                action=StepAction.CONTINUE,
            )

        sessions = getattr(getattr(handler, "parent", None), "_sessions", {})
        client = sessions.get(backend_name)
        if client is None or not hasattr(client, "vision"):
            return StepOutcome(
                {"status": "error", "msg": f"vision session unavailable: {backend_name}"},
                action=StepAction.CONTINUE,
            )

        yield f"[Action] Analyzing image with backend: {backend_name}\n"
        try:
            result = client.vision(image_path, str(args.get("prompt") or ""))
        except Exception as exc:
            safe_error = str(exc).replace(backend_config.api_key, "<redacted-api-key>")
            return StepOutcome(
                {"status": "error", "msg": safe_error[:1000]},
                action=StepAction.CONTINUE,
            )
        return StepOutcome(
            {"status": "success", "backend": backend_name, "content": result},
            action=StepAction.CONTINUE,
        )

    return _handler
