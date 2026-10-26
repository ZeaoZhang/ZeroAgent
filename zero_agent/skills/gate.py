"""Task-local routing and action gates for triggered SOPs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from zero_agent.context.errors import ContextError
from zero_agent.skills.manifest import BuiltinSkill


@dataclass(frozen=True)
class SkillRoute:
    skill_ids: tuple[str, ...]
    evidence: tuple[tuple[str, str], ...]


class SkillActionGate:
    """Require an explicit SOP routing decision and exact loaded revisions."""

    def __init__(self, catalog: Iterable[BuiltinSkill], task_text: str) -> None:
        self._catalog = {skill.skill_id: skill for skill in catalog}
        self._task_text = task_text
        self._route: SkillRoute | None = None
        self._loaded_revisions: dict[str, str] = {}
        self._action_started = False

    @property
    def routed(self) -> bool:
        """Whether the current task has submitted an explicit skill route."""

        return self._route is not None

    @property
    def has_matching_skills(self) -> bool:
        """Whether the task text matches a declared skill trigger."""

        return bool(self._matching_skills())


    def _matching_skills(self) -> tuple[BuiltinSkill, ...]:
        normalized = self._task_text.casefold()
        return tuple(
            skill
            for skill in self._catalog.values()
            if skill.category in {"triggered_rule", "on_demand_skill", "reference_resource"}
            and any(term.casefold() in normalized for term in skill.trigger_terms)
        )
    def _routing_error(self, missing: Iterable[BuiltinSkill]) -> ContextError:
        missing_ids = ", ".join(sorted(skill.skill_id for skill in missing))
        return ContextError(
            "SKILL_ROUTING_REQUIRED",
            "Route every skill whose declared trigger matches the user request."
            + (f" Missing skill IDs: {missing_ids}." if missing_ids else ""),
        )

    def route(
        self,
        skill_ids: object,
        trigger_evidence: object,
    ) -> dict[str, object]:
        """Validate and publish the complete task skill route.

        Args:
            skill_ids: Skill IDs selected for this task.
            trigger_evidence: Exact user-request excerpts keyed by skill ID.

        Returns:
            Routed skill metadata with immutable URIs and revisions.

        Raises:
            ContextError: If a required skill is omitted, evidence is invalid, or the route is locked.
        """
        if not isinstance(skill_ids, list) or any(not isinstance(item, str) for item in skill_ids):
            raise ContextError("INVALID_SOURCE", "skills must be an array of skill IDs.")
        if len(skill_ids) != len(set(skill_ids)):
            raise ContextError("INVALID_SOURCE", "A skill may be routed only once per task.")
        if not isinstance(trigger_evidence, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in trigger_evidence.items()
        ):
            raise ContextError("INVALID_SOURCE", "trigger_evidence must map skill IDs to quoted task text.")
        if set(trigger_evidence) != set(skill_ids):
            raise ContextError("INVALID_SOURCE", "Each triggered skill needs one matching task-text excerpt.")

        if self._route is not None and tuple(skill_ids) != self._route.skill_ids and self._action_started:
            raise ContextError("CONFLICT", "The skill route is locked after a protected action starts.")
        required_by_trigger = {skill.skill_id for skill in self._matching_skills()}
        omitted = required_by_trigger - set(skill_ids)
        if omitted:
            raise self._routing_error(self._catalog[skill_id] for skill_id in sorted(omitted))

        required: list[BuiltinSkill] = []
        for skill_id in skill_ids:
            skill = self._catalog.get(skill_id)
            if skill is None or skill.category in {"always_rule", "superseded"}:
                raise ContextError("NOT_FOUND", "A routed skill is unavailable or not task-triggered.")
            excerpt = trigger_evidence[skill_id].strip()
            if not excerpt or len(excerpt) > 500 or excerpt not in self._task_text:
                raise ContextError("INVALID_SOURCE", "Trigger evidence must quote the current user request.")
            required.append(skill)

        route = SkillRoute(
            tuple(skill.skill_id for skill in required),
            tuple((skill_id, trigger_evidence[skill_id].strip()) for skill_id in skill_ids),
        )
        if self._route is not None and route != self._route:
            if self._action_started:
                raise ContextError("CONFLICT", "The skill route is locked after a protected action starts.")
            self._loaded_revisions = {
                skill_id: revision
                for skill_id, revision in self._loaded_revisions.items()
                if skill_id in route.skill_ids
            }
        self._route = route
        return {
            "status": "routed",
            "required_skills": [
                {
                    "skill_id": skill.skill_id,
                    "skill_uri": skill.uri,
                    "revision": skill.revision,
                    "category": skill.category,
                    "trigger": skill.trigger,
                    "protected_actions": list(skill.protected_actions),
                }
                for skill in required
            ],
            "no_triggered_skills": not required,
        }

    def mark_loaded(self, skill_uri: object, revision: object) -> None:
        """Record a successfully loaded pinned revision for the current route.

        Args:
            skill_uri: Exact URI of a routed skill.
            revision: Exact immutable skill revision loaded by the runtime.
        """
        if not self.routed or not isinstance(skill_uri, str) or not isinstance(revision, str):
            return
        skill = next((item for item in self._catalog.values() if item.uri == skill_uri), None)
        if skill is None or skill.skill_id not in self._route.skill_ids:
            return
        if revision == skill.revision:
            self._loaded_revisions[skill.skill_id] = revision

    def check(self, action: str, *, tool_name: str, args: Mapping[str, object]) -> dict[str, object] | None:
        """Return a blocking error when an action lacks its required loaded skills.

        Args:
            action: Registered protected action name.
            tool_name: Tool about to be dispatched.
            args: Tool arguments used for action-specific exemptions.

        Returns:
            A structured blocking payload, or ``None`` when the action may proceed.
        """
        if tool_name == "skill_run" and args.get("entrypoint") == "load":
            return None

        matching_skills = self._matching_skills()
        if not self.routed:
            if action == "task.complete" and not matching_skills:
                return None
            error = self._routing_error(matching_skills)
            return {"status": "error", "code": error.code, "message": str(error)}

        selected_skills = (
            list(matching_skills)
            if action == "task.complete"
            else [
                self._catalog[skill_id]
                for skill_id in self._route.skill_ids
                if action in self._catalog[skill_id].protected_actions
            ]
        )
        missing = [
            skill
            for skill in selected_skills
            if self._loaded_revisions.get(skill.skill_id) != skill.revision
        ]
        if missing:
            return {
                "status": "error",
                "code": "TRIGGERED_SKILL_NOT_LOADED",
                "message": "Load each triggered skill at its pinned URI and revision before this action.",
                "required_skills": [
                    {"skill_uri": skill.uri, "revision": skill.revision}
                    for skill in missing
                ],
            }
        self._action_started = True
        return None

