# Memory Management SOP

## Core invariants

- Persistent memory is typed local_v1 data, not a prompt file or a new instruction layer.
- Never infer actor, project, persistence, evidence, or observed facts. Use the current trusted `RuntimeContext` and durable source receipts.
- A receipt establishes only its recorded actor, excerpt/result fields, tool contract, status, revision/hash and scope. Tool success alone never supports an arbitrary claim.
- Keep task-local instructions in the current task. Do not persist “this time”, “for this answer”, or other temporary requests.
- Do not read, initialize, migrate, or write legacy `global_mem*`, `project_memory.md`, PID anchors, or file-based SOP directories as runtime memory.

## Discover and verify sources

1. Before citing evidence, call `context_sources` to list authorized non-revoked receipts for this task. Follow `next_cursor` exactly; narrow with `source_type` only when useful.
2. Read a known prior receipt only with its exact `receipt_id`, same trusted actor/account and current scope access. Never create or guess a receipt ID.
3. Check that the excerpt or allowlisted tool field supports the exact proposed value. Keep `source_refs` unchanged. Do not transform a successful command into conclusions the tool did not observe.
4. If actor, revocation state, scope, revision/hash, or claim support is uncertain, stop the write and keep the observation task-local.

## Store preferences

- Use only registered preference keys, value schemas, and scopes. Persist only with an explicit lasting user statement and a user-message receipt.
- Contradictory values without clear same-key correction intent become conflicted and stay out of default context. Do not choose the newest value by timestamp.
- Explicit correction supersedes the previous value. Read the current revision and submit the exact `expected_revision`; on conflict, reread and prepare a fresh request.
- Project preferences require the current authorized project context and its write grant. They never become user-wide preferences.

## Store observations and decisions

- `environment_fact` and `experience` require a registered source policy and exact structured `subject`, `predicate`, `value`, `kind` and scope support. Content must be the canonical claim or omitted for service generation; do not append extra prose.
- A project decision can become active only when an authorized user's receipt states that exact decision for that project. A resource import is a resource, not a memory claim.
- Unsupported free text remains an unverified candidate or is rejected. Legacy imported values remain `legacy_unverified` until independently re-observed with a new receipt.

## Retract and purge

- `retract` removes an object from default use but preserves its history; cite the explicit user retraction receipt and exact current revision.
- `purge` is destructive. Require an explicit deletion request, exact object/revision, authorized receipts, and a single-object batch. Review `purge_pending`, `source_cleanup_complete`, `source_cleanup_reason`, and returned retained session/backup/export identifiers.
- Retry only the exact same idempotent request to resume a recorded pending cleanup. Do not claim complete erasure if provenance is incomplete or any source copy remains. Never delete sessions, archives, exports, or backups as an implicit side effect.

## Project context

- Project identity comes from `RuntimeContext` and `ContextAdminService`; workspace path or directory name does not grant project access.
- Read and write project memories through authorized `za://projects/<id>/memories` and `memory_update`. Workspace files remain ordinary task files, not persistent memory.
- If no project is bound or the needed grant is absent, request the supported binding/grant action. Never fall back to file-based memory.
