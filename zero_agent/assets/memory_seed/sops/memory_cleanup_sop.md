# Memory Cleanup SOP

## Purpose

Keep persistent local_v1 memory accurate and small. Never edit or reconstruct file-layer `global_mem*` indexes at runtime.

## Review and cleanup

1. Use `context_search` or `context_list` under the current authorized URI; inspect an exact object and revision with `context_read` before proposing a change.
2. Preserve typed value, scope, revision and source references. Do not merge across principals/projects or activate candidate, conflicted, superseded, retracted or legacy-unverified objects without new supported evidence.
3. Prefer `retract` when the user wants the memory no longer used but retained in history. Require an explicit user retraction receipt and the current expected revision.
4. Use `purge` only for an explicit request to delete a specific object. It must be a single-object batch with its exact current revision and authorized deletion receipt. Never infer a deletion target by basename or memory age.
5. Read the result. A `purge_pending` result may be retried only with the exact same idempotency key and payload. Report source-cleanup status, unresolved legacy provenance, and retained session/backup/export identifiers; do not claim complete erasure when any remain.

## Prohibitions

- No automatic cleanup, bulk rewriting, or file deletion.
- No direct edits to ContextStore revision files or manifest.
- If source provenance, object identity or authority is uncertain, stop and ask for the owner/user to resolve it.
