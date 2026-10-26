"""Byte-bounded context blocks for the existing system prompt assembly."""

from __future__ import annotations

import json
from dataclasses import replace
from importlib import resources
from typing import Any
from urllib.parse import quote

from zero_agent.context.errors import ContextError
from zero_agent.context.service import ContextService
from zero_agent.core.exceptions import ConfigError
from zero_agent.memory.service import PreferenceKeyRegistry


ContextEntry = dict[str, Any]

ContextEntryPairs = list[tuple[str, ContextEntry]]
ContextBlockResult = tuple[str, list[str], list[str]]

_RULE_ASSET = "context/always_rules.md"


class ContextAssembler:
    """Assemble fixed rules and authorized data blocks within a UTF-8 byte budget."""

    def __init__(
        self,
        service: ContextService,
        *,
        max_bytes: int = 8000,
        preference_keys: PreferenceKeyRegistry | None = None,
    ) -> None:
        """Create a context assembler for one authorized service.

        Args:
            service: Runtime-scoped context reader.
            max_bytes: Total UTF-8 byte budget for injected context blocks.
            preference_keys: Optional registry for validating stored preferences.

        Raises:
            ValueError: If the budget is not positive.
        """
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.service = service
        self.max_bytes = max_bytes
        self.preference_keys = preference_keys or PreferenceKeyRegistry()
        self.last_stats: dict[str, Any] = {}

    def build(self, *, working: dict[str, Any] | None = None) -> dict[str, str]:
        """Build prompt blocks within the configured UTF-8 byte budget.

        Args:
            working: Optional task-local working-memory fields.

        Returns:
            Named system-prompt blocks and their assembly statistics on the instance.

        Raises:
            ConfigError: If required rules or the complete skill route catalog do not fit.
        """
        allocations = {
            "context_rules": self.max_bytes // 4,
            "context_user": self.max_bytes * 15 // 100,
            "context_project": self.max_bytes * 15 // 100,
            "context_working": self.max_bytes // 10,
        }
        allocations["context_candidates"] = self.max_bytes - sum(allocations.values())
        rules = self._load_required_rules()
        rule_bytes = len(rules.encode("utf-8"))
        if rule_bytes > allocations["context_rules"]:
            raise ConfigError(
                "The required context rules exceed their prompt byte allocation."
            )
        user_objects, project_objects = self._memory_objects()
        project_objects = self._use_current_project_overviews(project_objects)
        user_block, user_used, user_dropped = self._object_block(
            "user_memory",
            user_objects,
            allocations["context_user"],
        )
        project_block, project_used, project_dropped = self._object_block(
            "project_memory",
            project_objects,
            allocations["context_project"],
        )
        candidate_items = self._skill_candidates()
        note = _working_note(working or {})
        note_block = _fit_working_block(note, allocations["context_working"])
        candidate_block, candidate_used, candidate_dropped = self._candidate_block(
            candidate_items,
            allocations["context_candidates"],
        )
        if candidate_dropped:
            raise ConfigError(
                "The full skill trigger catalog exceeds its prompt byte allocation."
            )
        blocks = {
            "context_rules": rules,
            "context_user": user_block,
            "context_project": project_block,
            "context_candidates": candidate_block,
            "context_working": note_block,
        }
        byte_counts = {name: len(content.encode("utf-8")) for name, content in blocks.items()}
        total = sum(byte_counts.values())
        if total > self.max_bytes:
            raise ConfigError("The assembled context exceeded its UTF-8 byte budget.")
        self.last_stats = {
            "budget_bytes": self.max_bytes,
            "total_bytes": total,
            "estimated_tokens": (total + 3) // 4,
            "sections": byte_counts,
            "included_objects": {
                "user": user_used,
                "project": project_used,
                "skills": candidate_used,
                "working_note": bool(note.strip() and note_block.strip()),
            },
            "dropped_uris": user_dropped + project_dropped + candidate_dropped,
        }
        return blocks

    def _use_current_project_overviews(self, objects: list[Any]) -> list[Any]:
        summarized = []
        for obj in objects:
            if obj.kind != "project_decision":
                summarized.append(obj)
                continue
            view = self.service.read_view(
                obj.uri,
                view="overview",
                revision=obj.revision,
            )
            if view["summary_status"] == "current" and view["content"].strip():
                summarized.append(replace(obj, content=view["content"]))
            else:
                # A missing or stale summary never hides the current exact body.
                summarized.append(obj)
        return summarized

    def _load_required_rules(self) -> str:
        try:
            content = (
                resources.files("zero_agent.assets")
                .joinpath(_RULE_ASSET)
                .read_text(encoding="utf-8")
            ).strip()
        except (FileNotFoundError, ModuleNotFoundError, UnicodeDecodeError) as exc:
            raise ConfigError("The bundled context rules are unavailable.") from exc
        return f"[Context rules: versioned system asset]\n{content}\n"

    def _memory_objects(self) -> tuple[list[Any], list[Any]]:
        user_uri = "za://users/me/memories"
        user_items = self._list_all(user_uri)
        project_items = []
        if self.service.runtime.project_id:
            project_id = quote(self.service.runtime.project_id, safe="-._~")
            project_items = self._list_all(f"za://projects/{project_id}/memories")
        active = [item for item in (*user_items, *project_items) if item.status == "active"]
        preferences: dict[tuple[str, str], list[Any]] = {}
        decisions: list[Any] = []
        for item in active:
            if item.kind == "user_preference" and item.preference_key and item.scope_key:
                try:
                    self.preference_keys.validate(
                        item.preference_key,
                        item.value,
                        scope_key=item.scope_key,
                    )
                except ContextError:
                    continue
                identity = (item.scope_key, item.preference_key)
                preferences.setdefault(identity, []).append(item)
            elif item.kind == "project_decision" and item.project_id == self.service.runtime.project_id:
                decisions.append(item)

        active_preferences: list[Any] = []
        for (scope_key, _), items in preferences.items():
            unique_values = {json.dumps(item.value, sort_keys=True, ensure_ascii=False) for item in items}
            if len(unique_values) == 1:
                active_preferences.append(max(items, key=lambda item: item.updated_at))
        project_keys = {
            item.preference_key
            for item in active_preferences
            if item.scope_key == f"project:{self.service.runtime.project_id}"
        }
        user_preferences = [
            item for item in active_preferences
            if item.scope_key == "user" and item.preference_key not in project_keys
        ]
        project_preferences = [
            item for item in active_preferences
            if item.scope_key == f"project:{self.service.runtime.project_id}"
        ]
        user_preferences.sort(key=lambda item: item.preference_key or "")
        project_objects = sorted(
            (*project_preferences, *decisions),
            key=lambda item: (item.updated_at, item.uri),
            reverse=True,
        )
        return user_preferences, project_objects

    def _list_all(self, uri: str) -> list[Any]:
        result = self.service.list(uri, limit=100)
        items = list(result["items"])
        cursor = result["next_cursor"]
        while cursor is not None:
            page = self.service.list(uri, cursor=cursor, limit=100)
            items.extend(page["items"])
            cursor = page["next_cursor"]
        return items

    def _skill_candidates(self) -> list[Any]:
        candidates = self._list_all("za://skills/builtin")
        user_id = quote(self.service.runtime.user_id, safe="-._~")
        candidates.extend(self._list_all(f"za://skills/users/{user_id}"))
        return sorted(
            (obj for obj in candidates if obj.status == "active" and obj.kind == "skill"),
            key=lambda obj: (obj.title.casefold(), obj.uri),
        )

    def _object_block(
        self,
        name: str,
        objects: list[Any],
        budget: int,
    ) -> ContextBlockResult:
        return _fit_objects(name, objects, budget)

    def _candidate_block(
        self,
        candidates: list[Any],
        budget: int,
    ) -> ContextBlockResult:
        category_codes = {
            "always_rule": "A",
            "triggered_rule": "T",
            "on_demand_skill": "S",
            "reference_resource": "R",
            "superseded": "X",
        }
        lines = ["[context_skill_routes: id|class(A/T/S/R/X)|trigger]"]
        for obj in candidates:
            category = _tag_value(obj.tags, "category")
            code = category_codes.get(category, "?")
            trigger = _tag_value(obj.tags, "trigger")
            label = trigger or obj.title
            lines.append(f"{obj.uri.rsplit('/', 1)[-1]}|{code}|{label}")
        block = "\n".join(lines) + "\n"
        if len(block.encode("utf-8")) <= budget:
            return block, [obj.uri for obj in candidates], []
        return lines[0] + "\n", [], [obj.uri for obj in candidates]


def _fit_objects(
    name: str,
    objects: list[Any],
    budget: int,
) -> ContextBlockResult:
    entries: ContextEntryPairs = []
    for obj in objects:
        content = obj.content
        entry: ContextEntry = {
            "uri": obj.uri,
            "revision": obj.revision,
            "kind": obj.kind,
            "title": obj.title,
            "content": content,
        }
        if obj.kind == "user_preference":
            entry["preference_key"] = obj.preference_key
            entry["scope_key"] = obj.scope_key
            entry["value"] = obj.value
        entries.append((obj.uri, entry))
    return _fit_entry_pairs(name, entries, budget)





def _fit_entry_pairs(
    name: str,
    entries: ContextEntryPairs,
    budget: int,
) -> ContextBlockResult:
    included: list[ContextEntry] = []
    used: list[str] = []
    dropped: list[str] = []
    for uri, entry in entries:
        candidate = [*included, entry]
        block = _json_block(name, candidate)
        if len(block.encode("utf-8")) <= budget:
            included.append(entry)
            used.append(uri)
            continue
        if "content" in entry and isinstance(entry["content"], str) and len(entry["content"]) > 240:
            compact = dict(entry)
            compact["content"] = entry["content"][:240]
            compact["truncated"] = True
            if len(_json_block(name, [*included, compact]).encode("utf-8")) <= budget:
                included.append(compact)
                used.append(uri)
                continue
        dropped.append(uri)
    return _json_block(name, included), used, dropped


def _json_block(name: str, entries: list[dict[str, Any]]) -> str:
    return f"[{name}: untrusted data]\n" + json.dumps(
        entries,
        ensure_ascii=False,
        separators=(",", ":"),
    ) + "\n"


def _tag_value(tags: tuple[str, ...], prefix: str) -> str:
    marker = prefix + ":"
    return next((tag[len(marker):] for tag in tags if tag.startswith(marker)), "")


def _working_note(working: dict[str, Any]) -> str:
    note = working.get("key_info", "")
    if not isinstance(note, str):
        return ""
    return note.strip()


def _working_block(note: str) -> str:
    if not note:
        return "[context_working: no saved working note]\n"
    return "[context_working: untrusted task note]\n" + json.dumps(
        {"content": note}, ensure_ascii=False, separators=(",", ":")
    ) + "\n"


def _fit_working_block(note: str, budget: int) -> str:
    empty = _working_block("")
    if not note or len(empty.encode("utf-8")) > budget:
        return "" if budget < len(empty.encode("utf-8")) else empty
    if len(_working_block(note).encode("utf-8")) <= budget:
        return _working_block(note)
    low, high = 0, len(note)
    best = empty
    while low <= high:
        middle = (low + high) // 2
        candidate = _working_block(note[:middle].rstrip() + "…")
        if len(candidate.encode("utf-8")) <= budget:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return best
