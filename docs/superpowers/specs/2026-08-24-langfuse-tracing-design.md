# Langfuse LLM Tracing Design

**Date:** 2026-08-24

## Goal

Trace every real LLM request made by ZeroAgent with Langfuse, using the `langfuse` section loaded from the project's existing `config.yaml`, without changing agent behavior when tracing is unavailable.

## Configuration decision

The repository convention is `config.yaml`, not `config.yml`:

- `default_config_path()` loads the project-local `config.yaml`.
- CLI, documentation, examples, and hot reload use the same name.
- The active `config.yaml` is ignored by Git.

This change keeps that convention. When an `AgentConfig` is supplied, Langfuse configuration comes from `AgentConfig.langfuse`. Existing environment/keychain fallback remains available only for callers that do not supply an explicit config, preserving compatibility without overriding project configuration.

The configuration shape remains:

```yaml
langfuse:
  public_key: pk-lf-...
  secret_key: sk-lf-...
  host: https://us.cloud.langfuse.com
```

No credentials are placed in traces, logs, tests, or committed example files.

## Alternatives considered

### LiteLLM global callback

A LiteLLM Langfuse callback can observe completion calls with little code, but it introduces process-global mutable state, complicates config reload and multi-agent isolation, and can duplicate the repository's custom observations. Callback behavior also differs between the legacy SDK integration and current Langfuse 4.x/OpenTelemetry integrations.

### Hook-only instrumentation

The existing HookSystem can observe AgentLoop turns and tools, but it misses direct calls such as `LiteLLMSession.vision()`. The current plugin also calls removed Langfuse APIs and attempts to register `_langfuse_config` as an invalid hook event, so this approach cannot meet the every-call requirement.

### Explicit Langfuse SDK instrumentation (selected)

Inject one optional tracer into all LLM sessions and create a Langfuse 4.x `generation` observation around each `litellm.completion()` invocation. This covers streaming, non-streaming, Vision, failover sessions, and errors deterministically, while avoiding global callback duplication.

## Architecture

### `LangfuseTracer`

`zero_agent/plugins/langfuse_tracing.py` will expose a small tracer abstraction that:

- lazily imports Langfuse;
- validates and normalizes configuration;
- creates a Langfuse client only when credentials are available;
- provides agent, tool, and generation observation helpers;
- converts the project's usage structure to Langfuse usage details;
- marks failed observations without raising into the agent runtime;
- flushes observations at agent completion and cleans per-run state.

The tracer uses the current SDK API:

- `client.start_observation(..., as_type="agent"/"tool"/"generation")`;
- `observation.update(...)`;
- `observation.end()`;
- `client.flush()`.

Agent and tool hooks remain for lifecycle context. The existing custom LLM hook span is removed from the Langfuse plugin because actual generation observations are created at the LLM call boundary.

### Dependency injection

`ZeroAgent` creates or receives the optional tracer from its loaded config and passes it through `LLMFactory` to every `LiteLLMSession`. Sessions created for failover and text-tool protocol wrappers share the same tracer. A missing dependency or invalid Langfuse configuration produces a no-op tracer.

### LLM call boundaries

The following paths each create exactly one generation observation:

- `LiteLLMSession._stream_chat()`;
- `LiteLLMSession._sync_chat()`;
- `LiteLLMSession.vision()`.

The observation starts before `litellm.completion()`, remains open while a stream is consumed, and ends after response normalization. Both completion-call failures and stream-iteration failures update the observation with an error before ending it.

Health checks use an HTTP `/v1/models` request rather than an LLM completion and are not recorded as LLM generations.

## Recorded fields

Each generation records, subject to Langfuse SDK serialization:

- backend name, provider, and model;
- normalized request messages and tools;
- stream mode and model parameters;
- normalized text output and tool calls;
- stop reason and interruption state;
- prompt, completion, total, and available cache token counts;
- exception type and safe error message on failure.

API keys, authorization headers, and other credentials are never included in observation input/output or error text.

## Failure behavior

Tracing is best-effort and must not change the LLM result:

- missing `langfuse` package: no-op;
- absent or incomplete Langfuse config: no-op;
- Langfuse client or export failure: local warning only;
- LLM failure: preserve the existing `LLMError` behavior while ending the generation as an error;
- stream interruption: preserve the existing partial-response behavior while recording the partial output and interruption status.

LiteLLM Langfuse callbacks are not configured, preventing duplicate records for the same call.

## Implementation and verification scope

Expected source changes:

- `zero_agent/plugins/langfuse_tracing.py`;
- `zero_agent/llm/sessions.py`;
- `zero_agent/llm/factory.py`;
- `zero_agent/core/agent.py`;
- `config.example.yaml`;
- `README.md`.

Expected test changes:

- `tests/test_langfuse_plugin.py`;
- session-level tracing tests for streaming, synchronous, Vision, errors, usage mapping, and no-op behavior;
- factory/agent wiring coverage where needed.

Verification will run the focused Langfuse and LLM tests first, followed by the repository test suite and a smoke scenario that exercises a mocked completion through the actual session path.
