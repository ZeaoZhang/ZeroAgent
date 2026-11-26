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
    backend_names = [
        name for name, backend in config.llm_backends.items() if backend.vision
    ]
    if not backend_names:
        return

    lang = config.resolved_tool_language
    registry.register(
        ToolDefinition(
            name="vision",
            description=_t(
                "使用当前选中的模型理解图片；当前模型不支持视觉时，按配置顺序选择第一个视觉模型。",
                "Understand an image with the currently selected model; if it does not support vision, use the first vision-capable model in configuration order.",
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
                },
                "required": ["image_path"],
            },
            handler=_make_vision_handler(config),
            evidence_kind="read",
            category="vision",
            effects=("read",),
        )
    )


def _active_backend_name(
    config: AgentConfig, handler: Any, sessions: dict[str, Any],
) -> str:
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


def _resolve_vision_target(
    config: AgentConfig, handler: Any, backend: Any = None,
):
    sessions = getattr(getattr(handler, "parent", None), "_sessions", {})
    if backend:
        backend_name = str(backend)
    else:
        backend_name = _active_backend_name(config, handler, sessions)
    selected_config = config.llm_backends.get(backend_name)
    if not backend and (selected_config is None or not selected_config.vision):
        backend_name = next(
            (name for name, item in config.llm_backends.items() if item.vision),
            config.default_backend,
        )
    backend_config = config.llm_backends.get(backend_name)
    if backend_config is None:
        return backend_name, None, None, {
            "status": "error", "msg": f"unknown backend: {backend_name}",
        }
    if not backend_config.vision:
        return backend_name, backend_config, None, {
            "status": "error",
            "msg": f"backend does not support vision: {backend_name}",
        }
    client = sessions.get(backend_name)
    if client is None or not hasattr(client, "vision"):
        return backend_name, backend_config, None, {
            "status": "error", "msg": f"vision session unavailable: {backend_name}",
        }
    return backend_name, backend_config, client, None


def _vision_result(
    backend_name: str, backend_config: Any, client: Any,
    image_path: str, prompt: str,
) -> Dict[str, Any]:
    try:
        result = client.vision(image_path, prompt)
    except Exception as exc:
        safe_error = str(exc).replace(backend_config.api_key, "<redacted-api-key>")
        return {"status": "error", "msg": safe_error[:1000]}
    return {"status": "success", "backend": backend_name, "content": result}


def analyze_image(
    config: AgentConfig,
    handler: Any,
    image_path: str,
    prompt: str = "",
    backend: Any = None,
) -> Dict[str, Any]:
    """Run one stateless vision request through the active/selected backend."""
    backend_name, backend_config, client, error = _resolve_vision_target(
        config, handler, backend,
    )
    if error:
        return error
    return _vision_result(backend_name, backend_config, client, image_path, prompt)


def _make_vision_handler(config: AgentConfig):
    def _handler(
        args: Dict[str, Any],
        _response: Any,
        handler: Any,
    ) -> Generator[str, None, StepOutcome]:
        image_path = str(args.get("image_path") or "")
        backend_name, backend_config, client, error = _resolve_vision_target(
            config, handler, args.get("backend"),
        )
        if error:
            return StepOutcome(error, next_prompt=None, action=StepAction.CONTINUE)
        yield f"[Action] Analyzing image with backend: {backend_name}\n"
        result = _vision_result(
            backend_name, backend_config, client, image_path,
            str(args.get("prompt") or ""),
        )
        return StepOutcome(result, next_prompt=None, action=StepAction.CONTINUE)
    return _handler
