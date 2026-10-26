"""Validated metadata for immutable package-provided skills."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml

from zero_agent.context.errors import ContextError
from zero_agent.context.types import ContextObject

_CATEGORIES = {
    "always_rule",
    "triggered_rule",
    "on_demand_skill",
    "reference_resource",
    "superseded",
}
_CATEGORY_TARGETS = {
    "always_rule": "always-rule",
    "triggered_rule": "triggered-rule",
    "on_demand_skill": "complete-skill",
    "reference_resource": "reference-resource",
    "superseded": "reviewed-alternative",
}
_PROTECTED_ACTIONS = {
    "workspace.write",
    "code.execute",
    "browser.access",
    "memory.write",
    "skill.execute",
    "vision.read",
    "task.complete",
}
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_REQUIREMENT_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,95}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOP_PREFIX = "memory_seed/sops/"


@dataclass(frozen=True)
class SkillRequirement:
    requirement_id: str
    source_section: str
    source_line: int
    target: str


@dataclass(frozen=True)
class BuiltinSkill:
    skill_id: str
    title: str
    tags: tuple[str, ...]
    source_path: str
    source_sha256: str
    category: str
    trigger: str
    protected_actions: tuple[str, ...]
    trigger_terms: tuple[str, ...]
    requirements: tuple[SkillRequirement, ...]
    content: str
    entrypoint: dict[str, Any] | None = None

    @property
    def uri(self) -> str:
        return f"za://skills/builtin/{self.skill_id}"

    @property
    def revision(self) -> str:
        return self.source_sha256

    def as_context_object(self) -> ContextObject:
        return ContextObject(
            object_id=self.skill_id,
            uri=self.uri,
            kind="skill",
            owner_id="builtin",
            content=self.content,
            revision=1,
            title=self.title,
            tags=(
                *self.tags,
                f"category:{self.category}",
                f"trigger:{self.trigger}",
                *(f"action:{action}" for action in self.protected_actions),
                *(f"trigger_term:{term}" for term in self.trigger_terms),
            ),
            status="active",
            source_refs=(),
        )


def load_builtin_catalog() -> tuple[BuiltinSkill, ...]:
    """Load and verify every cataloged SOP against its packaged source hash."""

    try:
        asset_root = resources.files("zero_agent.assets")
        catalog_bytes = asset_root.joinpath("context/builtin_catalog.yaml").read_bytes()
        catalog = yaml.safe_load(catalog_bytes)
    except (FileNotFoundError, ModuleNotFoundError, OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContextError("DEPENDENCY_MISSING", "The built-in skill catalog is unavailable or invalid.") from exc
    if not isinstance(catalog, dict) or catalog.get("catalog_version") != 2:
        raise ContextError("INTEGRITY_ERROR", "The built-in skill catalog version is unsupported.")
    raw_skills = catalog.get("skills")
    if not isinstance(raw_skills, list):
        raise ContextError("INTEGRITY_ERROR", "The built-in skill catalog has no skill entries.")

    result: list[BuiltinSkill] = []
    seen_ids: set[str] = set()
    seen_sources: set[str] = set()
    seen_requirements: set[str] = set()
    for raw in raw_skills:
        skill = _parse_skill(asset_root, raw, seen_ids, seen_sources, seen_requirements)
        result.append(skill)

    sop_root = asset_root.joinpath("memory_seed/sops")
    packaged_sources = {
        f"{_SOP_PREFIX}{item.name}"
        for item in sop_root.iterdir()
        if item.is_file() and item.name.endswith(".md")
    }
    if packaged_sources != seen_sources:
        raise ContextError("INTEGRITY_ERROR", "Every packaged SOP must have exactly one catalog entry.")
    return tuple(result)


def builtin_context_objects() -> tuple[ContextObject, ...]:
    return tuple(skill.as_context_object() for skill in load_builtin_catalog())


def _parse_skill(
    asset_root,
    raw: object,
    seen_ids: set[str],
    seen_sources: set[str],
    seen_requirements: set[str],
) -> BuiltinSkill:
    if not isinstance(raw, dict):
        raise ContextError("INTEGRITY_ERROR", "A built-in skill entry is malformed.")
    skill_id = raw.get("id")
    source_path = raw.get("source")
    digest = raw.get("sha256")
    category = raw.get("category")
    title = raw.get("title")
    trigger = raw.get("trigger")
    protected_actions = raw.get("protected_actions")
    if (
        not isinstance(skill_id, str)
        or not _IDENTIFIER.fullmatch(skill_id)
        or skill_id in seen_ids
        or not isinstance(source_path, str)
        or not source_path.startswith(_SOP_PREFIX)
        or ".." in source_path.split("/")
        or source_path in seen_sources
        or not isinstance(digest, str)
        or not _SHA256.fullmatch(digest)
        or category not in _CATEGORIES
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(trigger, str)
        or not trigger.strip()
        or not isinstance(protected_actions, list)
        or any(action not in _PROTECTED_ACTIONS for action in protected_actions)
        or len(set(protected_actions)) != len(protected_actions)
    ):
        raise ContextError("INTEGRITY_ERROR", "A built-in skill identity or routing contract is invalid.")
    source = asset_root.joinpath(source_path)
    try:
        content_bytes = source.read_bytes()
        content = content_bytes.decode("utf-8", errors="strict")
    except (FileNotFoundError, OSError, UnicodeError) as exc:
        raise ContextError("DEPENDENCY_MISSING", "A cataloged built-in skill source is unavailable.") from exc
    if hashlib.sha256(content_bytes).hexdigest() != digest:
        raise ContextError("INTEGRITY_ERROR", "A built-in skill source hash does not match its catalog revision.")

    raw_trigger_terms = raw.get("trigger_terms")
    if (
        not isinstance(raw_trigger_terms, list)
        or any(not isinstance(term, str) or not term.strip() for term in raw_trigger_terms)
        or (category == "triggered_rule" and not raw_trigger_terms)
    ):
        raise ContextError("INTEGRITY_ERROR", "A triggered skill must declare non-empty routing terms.")
    trigger_terms = tuple(dict.fromkeys(term.strip() for term in raw_trigger_terms))

    raw_tags = raw.get("tags", [])
    raw_requirements = raw.get("requirements")
    if (
        not isinstance(raw_tags, list)
        or any(not isinstance(tag, str) or not tag.strip() for tag in raw_tags)
        or not isinstance(raw_requirements, list)
        or not raw_requirements
    ):
        raise ContextError("INTEGRITY_ERROR", "A built-in skill has invalid tags or no requirement mapping.")
    requirements = []
    mapped_source_lines: set[int] = set()
    expected_target = _CATEGORY_TARGETS[category]
    for raw_requirement in raw_requirements:
        if not isinstance(raw_requirement, dict):
            raise ContextError("INTEGRITY_ERROR", "A built-in skill requirement mapping is malformed.")
        requirement_id = raw_requirement.get("id")
        section = raw_requirement.get("section")
        source_line = raw_requirement.get("source_line")
        target = raw_requirement.get("target")
        if (
            not isinstance(requirement_id, str)
            or not _REQUIREMENT_ID.fullmatch(requirement_id)
            or requirement_id in seen_requirements
            or not isinstance(section, str)
            or not section.strip()
            or isinstance(source_line, bool)
            or not isinstance(source_line, int)
            or source_line < 1
            or target != expected_target
        ):
            raise ContextError("INTEGRITY_ERROR", "A built-in skill requirement is missing a unique source, line, or target.")
        if source_line in mapped_source_lines:
            raise ContextError("INTEGRITY_ERROR", "A source section is mapped more than once.")
        mapped_source_lines.add(source_line)
        lines = content.splitlines()
        if source_line > len(lines):
            raise ContextError("INTEGRITY_ERROR", "A cataloged requirement line is outside its source.")
        if section != "full_source" and _markdown_heading(lines[source_line - 1]) != section:
            raise ContextError("INTEGRITY_ERROR", "A cataloged requirement line no longer identifies its source section.")
        requirements.append(SkillRequirement(requirement_id, section, source_line, target))
        seen_requirements.add(requirement_id)

    expected_sections = _requirement_sections(content)
    actual_sections = {
        (requirement.source_line, requirement.source_section)
        for requirement in requirements
        if requirement.source_section != "full_source"
    }
    if expected_sections != actual_sections:
        raise ContextError("INTEGRITY_ERROR", "Every SOP section must have exactly one requirement target mapping.")
    if category == "superseded":
        alternative = raw.get("reviewed_alternative")
        if not isinstance(alternative, str) or not alternative.strip():
            raise ContextError("INTEGRITY_ERROR", "A superseded SOP must record its reviewed replacement.")
    entrypoint = raw.get("entrypoint")
    if entrypoint is not None:
        _validate_entrypoint(skill_id, entrypoint)
    seen_ids.add(skill_id)
    seen_sources.add(source_path)
    return BuiltinSkill(
        skill_id=skill_id,
        title=title.strip(),
        tags=tuple(raw_tags),
        source_path=source_path,
        source_sha256=digest,
        trigger=trigger.strip(),
        category=category,
        protected_actions=tuple(protected_actions),
        trigger_terms=trigger_terms,
        requirements=tuple(requirements),
        content=content,
        entrypoint=entrypoint,
    )


def _markdown_heading(line: str) -> str | None:
    match = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
    return match.group(2).strip() if match else None


def _requirement_sections(content: str) -> set[tuple[int, str]]:
    lines = content.splitlines()
    headings: list[tuple[int, int, str]] = []
    in_fence = False
    for line_number, line in enumerate(lines, start=1):
        if re.match(r"^\s{0,3}(```|~~~)", line):
            in_fence = not in_fence
            continue
        heading = _markdown_heading(line) if not in_fence else None
        if heading:
            match = re.match(r"^ {0,3}(#{1,6})\s+", line)
            headings.append((line_number, len(match.group(1)), heading))
    if headings and headings[0][1] == 1 and all(
        not line.strip() for line in lines[: headings[0][0] - 1]
    ):
        headings = headings[1:]
    return {(line, section) for line, _level, section in headings}


def _validate_entrypoint(skill_id: str, entrypoint: object) -> None:
    if not isinstance(entrypoint, dict):
        raise ContextError("INTEGRITY_ERROR", "A skill entrypoint manifest is malformed.")
    module = entrypoint.get("module")
    callable_name = entrypoint.get("callable")
    entrypoint_id = entrypoint.get("id")
    dependency_hash = entrypoint.get("dependency_sha256")
    permissions = entrypoint.get("permissions")
    if (
        skill_id != "skill_search"
        or entrypoint_id != "search"
        or module != "zero_agent.utils.skill_search"
        or callable_name != "search"
        or not isinstance(dependency_hash, str)
        or not _SHA256.fullmatch(dependency_hash)
        or not isinstance(permissions, list)
        or set(permissions) != {"network", "environment:read"}
    ):
        raise ContextError("INTEGRITY_ERROR", "The skill entrypoint is not an approved packaged module.")
    parameters = entrypoint.get("parameters")
    if not isinstance(parameters, dict) or set(parameters) != {"query", "category", "top_k"}:
        raise ContextError("INTEGRITY_ERROR", "The skill entrypoint parameters are malformed.")
    expected = {
        "query": {"type": "string", "required": True, "max_length": 500},
        "category": {"type": "string", "required": False, "max_length": 80},
        "top_k": {"type": "integer", "required": False, "minimum": 1, "maximum": 20},
    }
    if parameters != expected:
        raise ContextError("INTEGRITY_ERROR", "The skill entrypoint parameters are not approved.")
