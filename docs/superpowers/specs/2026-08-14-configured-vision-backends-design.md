# Configured Vision Backends Design

## Goal

Route image understanding through the configured LLM backends instead of the standalone provider-specific vision API. DeepSeek Flash is text-only; GPT 5.6 Luna and Grok 4.6 are configured as vision-capable backends.

## Architecture

`LiteLLMSession.vision()` is the single multimodal request path. It uses the selected backend's API key, base URL, model override, timeout, retry, proxy, headers, TLS, temperature, reasoning, and token settings. The built-in `vision` tool selects a configured backend and invokes that session without adding the image to the normal conversation history.

The old `zero_agent.utils.vision_api` module and its provider-specific environment-variable routing are removed. Image paths are converted to OpenAI-compatible `image_url` data URLs; the existing provider message conversion remains responsible for alternate wire formats.

## Configuration

`LLMBackendConfig` gains:

- `vision`: whether the backend accepts image requests; defaults to `false`.
- `vision_model`: optional model override; null uses `model`.
- `vision_max_tokens`: output cap for image requests; defaults to 1024.
- `vision_detail`: image detail hint (`auto`, `low`, or `high`); defaults to `auto`.
- `vision_max_pixels`: local image resize ceiling; defaults to 1,440,000.

Active configuration:

- `shuai-deepseek-flash`: `vision: false`, thinking enabled with a 4096-token budget.
- `shuai-gpt-5.6-luna`: `vision: true`, `reasoning_effort: high`.
- `shuai-grok-4.6`: `vision: true`, `reasoning_effort: high`.

`default_backend` is corrected to the existing `shuai-deepseek-flash` key.

## Call flow

1. The agent exposes `vision` only when at least one configured backend has `vision: true`.
2. The tool accepts an image path, prompt, and optional backend name. Missing backend uses `default_backend`; an invalid or text-only backend fails before any network call.
3. The selected session creates an image data URL and calls `litellm.completion(..., stream=False)` with the backend configuration.
4. The response text is returned as the tool result. Configuration, image preparation, and provider failures return a normal tool error result without leaking API keys.

## Compatibility

The existing chat path continues to use the same backend fields. Explicit `thinking_type` is forwarded whenever configured, including OpenAI-compatible relays, because DeepSeek is configured through such a relay. The standalone `ZA_VISION_*`, provider-specific API keys, and ModelScope fallback are no longer used.

## Verification

Tests cover backend field parsing, multimodal request construction, model override, disabled-backend rejection, thinking forwarding, and conditional tool registration. The project config is loaded and the focused test set plus the full Python suite are run.
