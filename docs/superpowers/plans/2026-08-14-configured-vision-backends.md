# Configured Vision Backends Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route vision requests through configured LLM backends, keeping DeepSeek Flash text-only and enabling GPT 5.6 Luna/Grok 4.6 vision.

**Architecture:** Add explicit vision capability and request settings to `LLMBackendConfig`. Add a stateless `LiteLLMSession.vision()` call that reuses backend transport settings. Register a conditional built-in `vision` tool and remove provider-specific `vision_api` code and SOP references.

**Tech Stack:** Python 3.10+, dataclasses, LiteLLM, PyYAML, pytest, optional Pillow.

---

### Task 1: Extend backend configuration and active YAML

**Files:**
- Modify: `zero_agent/core/config.py:85-133`
- Modify: `config.yaml:7,31-155`
- Modify: `config.example.yaml:37-95`
- Test: `tests/test_config.py:15-55`

- [ ] Add `vision`, `vision_model`, `vision_max_tokens`, `vision_detail`, and `vision_max_pixels` dataclass fields with defaults and docstring descriptions.
- [ ] Add assertions to the existing YAML parsing test for `vision: true`, `vision_model`, and vision limits.
- [ ] Run `.venv/bin/pytest tests/test_config.py::test_from_yaml_loads_failover_thinking_and_log_dir -q`; expect failure before implementation, then pass.
- [ ] Add documented fields to the example backend.
- [ ] Update active config: use `default_backend: shuai-deepseek-flash`; DeepSeek `vision: false`, `thinking_type: enabled`, `thinking_budget_tokens: 4096`; GPT/Grok `vision: true`, `reasoning_effort: high`; set explicit vision defaults on all three.
- [ ] Load active config with `AgentConfig.from_yaml("config.yaml")` and call `validate()`.

### Task 2: Implement stateless multimodal session call

**Files:**
- Modify: `zero_agent/llm/sessions.py:1-25,442-525`
- Modify: `zero_agent/core/config.py` if validation helpers are needed
- Test: `tests/test_vision.py`

- [ ] Add `_image_to_data_url(image_input, max_pixels)` to accept a path or PIL image, resize to the configured pixel ceiling, convert to RGB JPEG, and return a data URL; raise a clear error when Pillow is unavailable or input is invalid.
- [ ] Add `LiteLLMSession.vision(image_input, prompt, *, max_pixels=None, timeout=None)` that rejects `config.vision == false`, builds an OpenAI-compatible user content list with text and `image_url`, selects `vision_model` or `model`, and invokes `litellm.completion` non-streaming.
- [ ] Reuse connection fields (`api_key`, `api_base`, provider, retry, timeout, proxy, headers, TLS, temperature, service tier, reasoning, thinking); do not mutate chat history.
- [ ] Extract response text from normal LiteLLM responses and raise `LLMError` with redacted details for request/response failures.
- [ ] Run `.venv/bin/pytest tests/test_vision.py -q`; expect all tests to pass.
- [ ] Run `.venv/bin/pytest tests/test_sessions.py -q` to protect existing chat behavior.

### Task 3: Expose conditional built-in vision tool

**Files:**
- Create: `zero_agent/tools/builtin/vision.py`
- Modify: `zero_agent/tools/registry.py:197-240`
- Modify: `zero_agent/tools/builtin/__init__.py:14-20`
- Test: `tests/test_vision.py`, `tests/test_registry.py:141-173`

- [ ] Register `vision` only when `any(backend.vision for backend in config.llm_backends.values())`.
- [ ] Define schema fields: required `image_path`; optional `prompt`, `backend`; describe backend keys and text-only rejection.
- [ ] Resolve backend to the configured session by name or default backend, call `.vision()`, and return a `StepOutcome`/tool result with status and text while preserving the existing generator handler contract.
- [ ] Add tests proving a config with no vision backend does not register the tool and a config with one vision backend does.
- [ ] Run `.venv/bin/pytest tests/test_registry.py tests/test_vision.py -q`.

### Task 4: Remove standalone provider-specific vision API and update guidance

**Files:**
- Delete: `zero_agent/utils/vision_api.py`
- Delete: `zero_agent/assets/templates/vision_api.template.py`
- Modify: `zero_agent/assets/memory_seed/sops/vision_sop.md`
- Modify: `memory/sops/vision_sop.md`
- Modify: `zero_agent/tools/builtin/__init__.py`
- Modify: `tests/test_extension_tools.py` references if required

- [ ] Replace SOP examples with the built-in `vision` tool and configured backend names; state DeepSeek Flash is text-only and do not document `ZA_VISION_*` variables.
- [ ] Remove stale template and module references; retain only the backend-driven call path.
- [ ] Run `grep`-equivalent repository search using the project search tool for `vision_api`, `ZA_VISION`, and ModelScope vision routing; expect no runtime/SOP references.

### Task 5: Verify end-to-end behavior

**Files:**
- Test: `tests/test_vision.py`, `tests/test_config.py`, `tests/test_registry.py`

- [ ] Run `.venv/bin/pytest tests/test_config.py tests/test_sessions.py tests/test_registry.py tests/test_vision.py -q`.
- [ ] Run `.venv/bin/python -c 'from zero_agent.core.config import AgentConfig; c=AgentConfig.from_yaml("config.yaml"); c.validate(); print([(n,b.vision,b.reasoning_effort,b.thinking_type) for n,b in c.llm_backends.items()])'` and confirm DeepSeek is non-vision while GPT/Grok are vision-enabled.
- [ ] Run `.venv/bin/pytest -q` and fix only regressions caused by this change.
- [ ] Review the final diff for stale provider-specific vision paths and secret leakage.
