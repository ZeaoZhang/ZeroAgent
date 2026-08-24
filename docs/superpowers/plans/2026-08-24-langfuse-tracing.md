# Langfuse LLM Tracing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Trace every real ZeroAgent LLM request through Langfuse 4.x using the loaded `config.yaml` configuration, without changing runtime behavior when Langfuse is unavailable.

**Architecture:** Create one best-effort `LangfuseTracer` per `ZeroAgent`, register its Agent/Turn/Tool lifecycle hooks, and inject the same tracer into every LLM session. Instrument the three concrete `litellm.completion()` paths (`_stream_chat`, `_sync_chat`, and `vision`) directly so each request produces exactly one generation observation, including failures and stream interruptions. Do not configure LiteLLM's global Langfuse callback.

**Tech Stack:** Python 3.10–3.13, Langfuse 4.x Python SDK, LiteLLM, pytest, existing HookSystem and LLMFactory abstractions.

---

## File map

- **Modify `zero_agent/plugins/langfuse_tracing.py`:** Replace removed `Langfuse.trace()`/`.span()` calls with a `LangfuseTracer` abstraction based on `start_observation`; make hook registration valid and idempotent; keep optional dependency and failure isolation.
- **Modify `zero_agent/llm/sessions.py`:** Accept an injected tracer and wrap every direct `litellm.completion()` call with one generation observation; preserve existing stream and error semantics.
- **Modify `zero_agent/llm/factory.py`:** Thread the optional tracer through every session constructor, including failover backups and text-tool wrappers.
- **Modify `zero_agent/core/agent.py`:** Create the tracer from `AgentConfig.langfuse`, register hooks with that instance, pass it into sessions, and keep the same tracer object across config reloads so bound hooks remain valid.
- **Modify `config.example.yaml`:** Document the `langfuse` YAML section without committing credentials.
- **Modify `README.md`:** Make `config.yaml` the primary Langfuse setup and retain environment fallback documentation only as compatibility behavior.
- **Modify `tests/test_langfuse_plugin.py`:** Test configuration, valid hook registration, current SDK observation lifecycle, error isolation, and absence of LiteLLM callback mutation.
- **Create `tests/test_langfuse_sessions.py`:** Test stream, sync, Vision, error, usage, no-op, and exact-one-generation behavior through the real session methods with mocked LiteLLM responses.

## Interfaces fixed before implementation

Use these method names and keyword meanings in every task:

| Method | Contract |
| --- | --- |
| `LangfuseTracer.from_config(config)` | Build an enabled or no-op tracer from an optional `AgentConfig`. |
| `start_agent(context)` / `finish_agent(context)` | Open or close the Agent observation and flush completed data. |
| `start_turn_metadata(context)` / `finish_turn_metadata(context)` | Record optional turn metadata under the active Agent observation. |
| `start_tool(context)` / `finish_tool(observation, context)` | Open or close a child tool observation. |
| `start_generation(name, model, input, model_parameters)` | Open one child generation observation and return its handle. |
| `finish_generation(observation, output, usage, level, status_message)` | Update token/output/error fields and end the observation. |
| `flush()` | Flush the current client without raising. |
| `reconfigure(config)` | Apply a later YAML configuration while keeping existing hook callbacks bound to this tracer. |

A no-op tracer implements the same methods. `LiteLLMSession` receives `tracer: Any | None = None` and treats `None` as the no-op tracer. `LLMFactory.create_session`, `create_all_sessions`, `create_from_config`, `_get_primary_session`, and `_wrap_failover` accept/pass `tracer` without changing existing positional call behavior.

### Task 1: Add failing tracer and registration tests

**Files:**
- Modify: `tests/test_langfuse_plugin.py`

- [ ] **Step 1: Replace the weak registration assertions with behavior-focused tests.**

Add fakes that record observation creation, updates, endings, and flushes:

```python
class FakeObservation:
    _next_id = 0

    def __init__(self, name, as_type, **kwargs):
        type(self)._next_id += 1
        self.id = f"obs-{type(self)._next_id}"
        self.trace_id = "trace-1"
        self.name = name
        self.as_type = as_type
        self.kwargs = kwargs
        self.updates = []
        self.ended = False

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True


class FakeLangfuse:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.observations = []
        self.flush_count = 0
        type(self).instances.append(self)

    def start_observation(self, **kwargs):
        observation = FakeObservation(**kwargs)
        self.observations.append(observation)
        return observation

    def flush(self):
        self.flush_count += 1
```

Add tests that assert:

- `LangfuseTracer.from_config()` passes `public_key`, `secret_key`, and `host` from `AgentConfig.langfuse` to `FakeLangfuse`.
- `_get_config(explicit_config)` returns the YAML config even when conflicting environment variables exist.
- `register(hooks, config, tracer)` registers only valid HookSystem events and never attempts `_langfuse_config`.
- Agent and tool callbacks create/end observations and `flush()` is called.
- Registration does not set or overwrite `litellm.success_callback` or `litellm.failure_callback`.
- Missing SDK/config and client exceptions return a no-op tracer and never raise.

- [ ] **Step 2: Add a failing test for generation parent context and usage mapping.**

Start an agent observation, start one generation, finish it with:

```python
usage = {
    "input_tokens": 12,
    "output_tokens": 7,
    "cache_read_tokens": 3,
    "cache_creation_tokens": 2,
}
```

Assert the generation uses `as_type == "generation"`, the same `trace_id`, the agent observation as parent, and an update containing:

```python
{
    "usage_details": {
        "prompt_tokens": 12,
        "completion_tokens": 7,
        "total_tokens": 19,
        "input_cached": 3,
        "input_cache_creation": 2,
    }
}
```

- [ ] **Step 3: Run the focused tests and verify they fail for the current implementation.**

Run:

```bash
.venv/bin/pytest tests/test_langfuse_plugin.py -q
```

Expected: failures showing the old `trace()`/`span()` API and invalid `_langfuse_config` registration path.

- [ ] **Step 4: Commit the red tests.**

```bash
git add tests/test_langfuse_plugin.py
git commit -m "test: define Langfuse tracer behavior"
```

### Task 2: Implement the Langfuse 4.x tracer and lifecycle hooks

**Files:**
- Modify: `zero_agent/plugins/langfuse_tracing.py`
- Test: `tests/test_langfuse_plugin.py`

- [ ] **Step 1: Add safe configuration and client construction.**

Keep `_get_langfuse()` lazy. Make `_get_config(config)` obey this rule:

```python
if config is not None:
    return normalized_config_from_config_or_none
return keychain_config_or_environment_config_or_none
```

For an explicit `AgentConfig` with no valid `langfuse` mapping, return `None` rather than silently using an unrelated environment project. Accept `public_key`/`secret_key` and their uppercase aliases, and default the host to `https://cloud.langfuse.com`.

`LangfuseTracer.from_config()` must catch SDK import and constructor failures, log a warning through the module logger, and return a tracer whose methods are no-ops.

- [ ] **Step 2: Implement the observation wrapper and parent context.**

Use `contextvars.ContextVar` for the active Agent observation and a stack keyed by tool call identity. Import `TraceContext` lazily from `langfuse.types` when starting a child observation. Build child context as:

```python
TraceContext(
    trace_id=parent.trace_id,
    parent_span_id=parent.id,
)
```

Create observations using the current SDK API:

```python
client.start_observation(
    trace_context=trace_context,
    name="llm-call",
    as_type="generation",
    input=input_value,
    model=model,
    model_parameters=model_parameters,
)
```

Every SDK call (`start_observation`, `update`, `end`, `flush`) must be guarded so tracing failures cannot escape into AgentLoop or session code.

- [ ] **Step 3: Implement hook callbacks without pseudo-events or LiteLLM callbacks.**

`register(hook_system, config=None, tracer=None)` must use a provided tracer or construct one, then register only real events:

```python
EVENT_AGENT_BEFORE -> tracer.start_agent
EVENT_TURN_BEFORE  -> tracer.start_turn_metadata (no-op is acceptable)
EVENT_TOOL_BEFORE  -> tracer.start_tool
EVENT_TOOL_AFTER   -> tracer.finish_tool
EVENT_TURN_AFTER   -> tracer.finish_turn_metadata (no-op is acceptable)
EVENT_AGENT_AFTER  -> tracer.finish_agent
```

Do not register custom LLM generation handlers; session code owns generation observations. Do not import or mutate `litellm.success_callback`, `litellm.failure_callback`, or any global LiteLLM callback.

Use `HookSystem.has()` before registering each callback so repeated plugin setup does not duplicate handlers. Preserve the public `register()` boolean contract: `True` only when a real client is enabled, `False` for a no-op tracer.

- [ ] **Step 4: Run the tracer tests and commit the green unit implementation.**

Run:

```bash
.venv/bin/pytest tests/test_langfuse_plugin.py -q
```

Expected: all tracer tests pass.

```bash
git add zero_agent/plugins/langfuse_tracing.py tests/test_langfuse_plugin.py
git commit -m "feat: add safe Langfuse 4 tracing lifecycle"
```

### Task 3: Add failing session-level tests for every LLM call path

**Files:**
- Create: `tests/test_langfuse_sessions.py`
- Test fixtures: `tests/conftest.py` only if a shared fixture is required

- [ ] **Step 1: Add a recording tracer test double.**

Implement a test tracer with `start_generation()` returning a recording observation and `finish_generation()` storing output, usage, level, and status. Use a minimal `LLMBackendConfig` with `stream=True` or `stream=False` and no network calls.

- [ ] **Step 2: Add a stream success test.**

Monkeypatch `zero_agent.llm.sessions.litellm.completion` to return two response chunks containing text deltas and a final usage object. Exhaust `LiteLLMSession.chat()` using `next()` until `StopIteration`, then assert:

- exactly one generation was started;
- input contains the messages and tools passed to `chat()`;
- model is the backend model;
- output contains the normalized response content;
- usage contains input/output/total metrics;
- observation is finished without an error level.

- [ ] **Step 3: Add sync success, Vision success, and failure tests.**

For sync, return one LiteLLM response object and assert one finished generation. For Vision, call `session.vision()` with a mocked image conversion helper and a response containing text; assert Vision creates one generation even though AgentLoop hooks are not involved. For failures, make `litellm.completion` raise and assert the existing `LLMError`/Vision error is preserved while the generation finishes with `level="ERROR"` and a redacted status message.

- [ ] **Step 4: Add stream interruption and no-op tests.**

Return a stream whose iterator raises after yielding one chunk. Assert the existing partial-response marker remains unchanged and the generation records the partial output with `level="ERROR"`. Instantiate `LiteLLMSession(config, tracer=None)` and assert all existing response behavior works with zero tracer calls.


- [ ] **Step 5: Run the new tests and verify they fail before session wiring.**

Run:

```bash
.venv/bin/pytest tests/test_langfuse_sessions.py -q
```

Expected: constructor/tracer assertions fail because `LiteLLMSession` does not yet accept or use a tracer.

- [ ] **Step 6: Commit the red session tests.**

```bash
git add tests/test_langfuse_sessions.py
git commit -m "test: cover Langfuse tracing for all LLM paths"
```

### Task 4: Instrument `LiteLLMSession` and wire the tracer through the factory

**Files:**
- Modify: `zero_agent/llm/sessions.py`
- Modify: `zero_agent/llm/factory.py`
- Test: `tests/test_langfuse_sessions.py`
- Test: existing factory/session tests when constructor expectations need updates

- [ ] **Step 1: Add the optional tracer constructor parameter and factory plumbing.**

Add `tracer: Any | None = None` to `LiteLLMSession.__init__()` after existing optional log arguments and store `self._tracer = tracer`. Add the same keyword-only plumbing to every factory method and pass it to all primary and backup sessions. Keep existing callers valid by defaulting to `None`.

- [ ] **Step 2: Add session helper methods for safe generation lifecycle.**

Implement these private helpers in `LiteLLMSession`:

```python
def _start_generation(
    self,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]],
    *,
    stream: bool,
) -> Any:
    if self._tracer is None:
        return None
    return self._tracer.start_generation(
        name="llm-call",
        model=self.config.model,
        input={"messages": messages, "tools": tools or []},
        model_parameters={
            "temperature": self.config.temperature,
            "stream": stream,
            "max_tokens": self.config.max_tokens,
        },
    )


def _finish_generation(
    self,
    observation: Any,
    *,
    output: Any,
    usage: Any,
    level: Optional[str] = None,
    status_message: Optional[str] = None,
) -> None:
    if observation is None or self._tracer is None:
        return
    self._tracer.finish_generation(
        observation,
        output=output,
        usage=usage,
        level=level,
        status_message=status_message,
    )
```

The input must never include `api_key`, `api_base`, request headers, or proxy values. `_finish_generation()` catches tracer exceptions so tracing cannot alter session behavior.

Map canonical metrics inside `LangfuseTracer.finish_generation()` using the existing `extract_usage_metrics()` helper:

```python
metrics = extract_usage_metrics(usage)
usage_details = {
    "prompt_tokens": metrics["input_tokens"],
    "completion_tokens": metrics["output_tokens"],
    "total_tokens": metrics["input_tokens"] + metrics["output_tokens"],
}
if metrics["cache_read_tokens"]:
    usage_details["input_cached"] = metrics["cache_read_tokens"]
if metrics["cache_creation_tokens"]:
    usage_details["input_cache_creation"] = metrics["cache_creation_tokens"]
```

- [ ] **Step 3: Wrap `_stream_chat()` around both completion creation and iterator consumption.**

At the beginning of `_stream_chat()`, start one generation and initialize `stream_error`, `generation_output`, and `generation_usage` to `None`. Move the existing `litellm.completion(**kwargs)` call into the method's protected block. Keep the existing chunk parsing, text yields, tool-call assembly, interruption markers, stop-reason normalization, and usage recovery unchanged. In the existing stream exception handler, assign the exception to `stream_error` before preserving the current partial-response behavior. After `mock.usage` and `mock.tool_calls` are finalized, set `generation_output` to the normalized content/tool-call/stop-reason payload and `generation_usage` to `mock.usage`. In a `finally` block, call `_finish_generation()` exactly once with `level="ERROR"` and a `_redact_error()` status when `stream_error` is set. If completion creation itself raises, finish with `output=None` and re-raise through the existing `chat()` error path; do not convert a new pre-stream failure into a successful response.

Do not re-raise an iterator error that the current method intentionally converts to a partial response.

- [ ] **Step 4: Wrap `_sync_chat()` and `vision()` exactly once each.**

For `_sync_chat()`, start before `litellm.completion()`, finish after `MockResponse.from_litellm_response()` and interruption normalization, and on any exception finish with `level="ERROR"` and re-raise so `chat()` retains current `LLMError` wrapping. For `vision()`, start before its completion call, use `self.config.vision_model or self.config.model` as the generation model, include the provider-normalized image request in the input, finish after text extraction, and preserve existing image preparation, response validation, redaction, and `LLMError` behavior.

- [ ] **Step 5: Run session and factory tests.**

Run:

```bash
.venv/bin/pytest tests/test_langfuse_sessions.py tests/test_sessions.py tests/test_factory.py -q
```

Expected: all focused tracing tests pass and existing session/factory behavior remains green.

```bash
git add zero_agent/llm/sessions.py zero_agent/llm/factory.py tests/test_langfuse_sessions.py
git commit -m "feat: trace every LiteLLM session call"
```


### Task 5: Wire Agent lifecycle, failover, and config reload

**Files:**
- Modify: `zero_agent/core/agent.py`
- Modify: `zero_agent/llm/factory.py` if reload call signatures need propagation
- Modify: `tests/test_agent.py` or a focused agent test file

- [ ] **Step 1: Add a failing construction/wiring test.**

Monkeypatch `zero_agent.core.agent.LLMFactory.create_all_sessions` and the Langfuse tracer factory. Construct `ZeroAgent` with a minimal config and assert the exact same tracer object is passed to every factory call and that plugin registration receives it.

- [ ] **Step 2: Create and register one tracer before sessions are built.**

In `ZeroAgent.__init__()`:

```python
self._langfuse_tracer = LangfuseTracer.from_config(self.config)
self._register_builtin_plugins(self._langfuse_tracer)
```

Pass `tracer=self._langfuse_tracer` into the initial `LLMFactory.create_all_sessions()` call. Change `_register_builtin_plugins()` to accept the tracer and call `register(self.hooks, config=self.config, tracer=tracer)`.

Do not let Langfuse construction failure prevent session creation.

- [ ] **Step 3: Pass the same tracer into hot-reloaded sessions.**

In `reload_config()`, pass `tracer=self._langfuse_tracer` to the new session factory calls. After the new config/sessions are committed, call `self._langfuse_tracer.reconfigure(new_config)` so future calls use the latest YAML credentials while existing hook callbacks remain bound to the same tracer object. `reconfigure()` must defer client replacement while an Agent observation is active and flush the old client before swapping it.

On reload failure, retain the old tracer/config/session state exactly as before.

- [ ] **Step 4: Verify failover and text-tool wrappers share tracing.**

Add assertions that sessions created with `failover_backends` and `tool_protocol: text` ultimately expose the same tracer on each wrapped `LiteLLMSession`. Run:

```bash
.venv/bin/pytest tests/test_agent.py tests/test_failover.py tests/test_factory.py -q
```

Expected: focused agent, failover, and factory tests pass.

```bash
git add zero_agent/core/agent.py zero_agent/llm/factory.py tests/test_agent.py tests/test_failover.py
git commit -m "feat: inject Langfuse tracer into agents"
```
 

### Task 6: Update configuration and user documentation

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`

- [ ] **Step 1: Add a safe YAML example.**

Append a commented section to `config.example.yaml`:

```yaml
# Optional Langfuse tracing. Replace placeholders locally; never commit secrets.
# langfuse:
#   public_key: pk-lf-REPLACE_ME
#   secret_key: sk-lf-REPLACE_ME
#   host: https://us.cloud.langfuse.com
```

Do not use `${LANGFUSE_PUBLIC_KEY}` here unless the Langfuse config parser is explicitly changed to resolve it; literal placeholders must not be sent as credentials.

- [ ] **Step 2: Rewrite README Langfuse instructions around config.yaml.**

Document the YAML section as the primary setup, explain that the active `config.yaml` is ignored by Git, state that tracing is optional/no-op when the package or config is absent, and list environment variables only as fallback for direct plugin callers without an explicit `AgentConfig`. Remove wording that claims environment variables are the primary setup.

- [ ] **Step 3: Run documentation/config tests and commit.**

Run:

```bash
.venv/bin/pytest tests/test_config.py tests/test_langfuse_plugin.py -q
```

Expected: configuration parsing and Langfuse setup tests pass without exposing credentials.

```bash
git add config.example.yaml README.md
git commit -m "docs: configure Langfuse through YAML"
```

### Task 7: Full verification and smoke test

**Files:**
- No planned source changes; fix only regressions directly caused by this feature.

- [ ] **Step 1: Run the complete Python test suite.**

Run:

```bash
.venv/bin/pytest -q
```

Expected: all repository tests pass. If the environment lacks a test dependency, install the repository's documented development dependency and rerun; do not weaken tests.

- [ ] **Step 2: Run a real session-path smoke test with a mocked provider.**

Run this self-contained smoke command after the implementation:

```bash
.venv/bin/python - <<'PY'
from types import SimpleNamespace

import zero_agent.llm.sessions as sessions_module
from zero_agent.core.config import LLMBackendConfig
from zero_agent.llm.sessions import LiteLLMSession


class RecordingTracer:
    def __init__(self):
        self.started = 0
        self.finished = []

    def start_generation(self, **kwargs):
        self.started += 1
        return object()

    def finish_generation(self, observation, **kwargs):
        self.finished.append(kwargs)


response = SimpleNamespace(
    choices=[
        SimpleNamespace(
            message=SimpleNamespace(content="ok", tool_calls=None),
            finish_reason="stop",
        )
    ],
    usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3),
)
sessions_module.litellm.completion = lambda **kwargs: response
config = LLMBackendConfig(
    name="smoke",
    provider="openai",
    api_key="local-test-key",
    api_base="http://127.0.0.1",
    model="smoke-model",
    stream=False,
)
tracer = RecordingTracer()
generation = LiteLLMSession(config, tracer=tracer).chat(
    [{"role": "user", "content": "ping"}]
)
while True:
    try:
        next(generation)
    except StopIteration:
        break
assert tracer.started == 1
assert len(tracer.finished) == 1
assert tracer.finished[0]["usage"]["prompt_tokens"] == 2
print("generations=1 model=smoke-model prompt_tokens=2")
PY
```

Expected output: `generations=1 model=smoke-model prompt_tokens=2`.

- [ ] **Step 3: Review the final diff for security and duplication.**

Check that:

- no API keys or secret values appear in tracked files;
- no `litellm.success_callback`, `failure_callback`, or `callbacks` mutation remains in the plugin;
- no old `Langfuse.trace()`/`.span()` calls remain;
- no `_langfuse_config` pseudo-event registration remains;
- stream, sync, and Vision each start exactly one generation.

- [ ] **Step 4: Commit any directly required test/doc fixes and report evidence.**

Use a focused commit message for any regression fix. Report the exact test commands and observed results; do not claim remote Langfuse ingestion without credentials/network verification.
