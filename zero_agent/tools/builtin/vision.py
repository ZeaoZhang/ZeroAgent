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
                "使用视觉模型理解图片。未指定 backend 时优先使用当前会话模型；当前模型不支持视觉时再回退到已配置的视觉 backend。",
                "Understand an image with a configured vision backend. DeepSeek Flash is text-only; "
                "when backend is omitted, prefer the active session backend and fall back to a configured vision backend only when needed.",
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
            evidence_kind="read",
            category="vision",
        )
    )


def _make_vision_handler(config: AgentConfig):
    def _active_backend_name(handler: Any, sessions: dict[str, Any]) -> str:
        """Resolve the backend currently selected by the running agent."""
        parent = getattr(handler, "parent", None)
        active_name_getter = getattr(parent, "_get_active_backend_name", None)
        if callable(active_name_getter):
            active_name = active_name_getter()
            if active_name in config.llm_backends:
                return str(active_name)
        active_client = getattr(parent, "client", None) or getattr(handler, "client", None)
        if active_client is not None:
            active_config = getattr(active_client, "config", None)
            for name, session in sessions.items():
                if session is active_client:
                    return name
                session_config = getattr(session, "config", None)
                if active_config is not None and session_config is active_config:
                    return name
            client_name = getattr(active_client, "name", None)
            if client_name in config.llm_backends:
                return str(client_name)
            config_name = getattr(active_config, "name", None)
            if config_name in config.llm_backends:
                return str(config_name)
        return config.default_backend

    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, StepOutcome]:
        image_path = str(args.get("image_path") or "")
        requested_backend = args.get("backend")
        if requested_backend:
            backend_name = str(requested_backend)
        else:
            sessions = getattr(getattr(handler, "parent", None), "_sessions", {})
            backend_name = _active_backend_name(handler, sessions)
            default_config = config.llm_backends.get(backend_name)
            if default_config is not None and default_config.vision:
                pass
            else:
                backend_name = next(
                    (
                        name
                        for name in sorted(config.llm_backends)
                        if config.llm_backends[name].vision
                    ),
                    config.default_backend,
                )
        backend_config = config.llm_backends.get(backend_name)
        if backend_config is None:
            return StepOutcome(
                {"status": "error", "msg": f"unknown backend: {backend_name}"},
                next_prompt=None,
                action=StepAction.CONTINUE,
            )
        if not backend_config.vision:
            return StepOutcome(
                {"status": "error", "msg": f"backend does not support vision: {backend_name}"},
                next_prompt=None,
                action=StepAction.CONTINUE,
            )

        sessions = getattr(getattr(handler, "parent", None), "_sessions", {})
        client = sessions.get(backend_name)
        if client is None or not hasattr(client, "vision"):
            return StepOutcome(
                {"status": "error", "msg": f"vision session unavailable: {backend_name}"},
                next_prompt=None,
                action=StepAction.CONTINUE,
            )

        yield f"[Action] Analyzing image with backend: {backend_name}\n"
        try:
            result = client.vision(image_path, str(args.get("prompt") or ""))
        except Exception as exc:
            safe_error = str(exc).replace(backend_config.api_key, "<redacted-api-key>")
            return StepOutcome(
                {"status": "error", "msg": safe_error[:1000]},
                next_prompt=None,
                action=StepAction.CONTINUE,
            )
        return StepOutcome(
            {"status": "success", "backend": backend_name, "content": result},
            next_prompt=None,
            action=StepAction.CONTINUE,
        )
    return _handler
