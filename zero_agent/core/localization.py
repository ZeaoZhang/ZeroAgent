from __future__ import annotations

import gettext
import struct
from importlib import resources

from zero_agent.core.exceptions import ConfigError
from zero_agent.core.types import TaskMode


PROMPT_REPLY_LANGUAGE = "prompt.reply_language"
PROMPT_TASK_CONTROL_OPEN = "prompt.task_control.open"
PROMPT_TASK_CONTROL_EXECUTING = "prompt.task_control.executing"
PROMPT_TASK_CONTROL_PLAN = "prompt.task_control.plan"
PROMPT_TODAY_LABEL = "prompt.today_label"
PROMPT_PEER_HINT = "prompt.peer_hint"

_TASK_CONTROL_MESSAGE_IDS = {
    TaskMode.OPEN: PROMPT_TASK_CONTROL_OPEN,
    TaskMode.EXECUTING: PROMPT_TASK_CONTROL_EXECUTING,
    TaskMode.PLAN: PROMPT_TASK_CONTROL_PLAN,
}

WEEKDAY_MESSAGE_IDS = (
    "weekday.monday",
    "weekday.tuesday",
    "weekday.wednesday",
    "weekday.thursday",
    "weekday.friday",
    "weekday.saturday",
    "weekday.sunday",
)


def _language_code(language: str) -> str:
    raw = str(language or "").strip().lower().replace("-", "_")
    return raw.split("_", 1)[0] or "en"


def _read_catalog(language: str) -> gettext.NullTranslations | None:
    resource = (
        resources.files("zero_agent.assets")
        .joinpath("locale", language, "LC_MESSAGES", "zero_agent.mo")
    )
    try:
        with resource.open("rb") as stream:
            return gettext.GNUTranslations(stream)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, struct.error) as exc:
        raise ConfigError(
            f"Prompt translation asset for {language} is invalid or unavailable"
        ) from exc


def _load_catalog(language: str) -> gettext.NullTranslations:
    english = _read_catalog("en")
    if english is None:
        raise ConfigError("English prompt translation asset is required but unavailable")

    requested = _language_code(language)
    if requested == "en":
        return english

    localized = _read_catalog(requested)
    if localized is None:
        return english
    localized.add_fallback(english)
    return localized


class PromptLocalizer:
    def __init__(self, language: str) -> None:
        self.language = _language_code(language)
        self._translations = _load_catalog(self.language)

    def text(self, message_id: str) -> str:
        translated = self._translations.gettext(message_id)
        if translated == message_id:
            raise ConfigError(f"Prompt translation is missing: {message_id}")
        return translated

    def task_control(self, mode: TaskMode) -> str:
        """Return the localized control contract for the observed task mode.

        Args:
            mode: Current task state.

        Returns:
            Localized control-contract XML block.

        Raises:
            ConfigError: If ``mode`` has no registered prompt translation.
        """

        try:
            message_id = _TASK_CONTROL_MESSAGE_IDS[mode]
        except KeyError as exc:
            raise ConfigError(f"Unsupported task mode: {mode}") from exc
        return self.text(message_id)
