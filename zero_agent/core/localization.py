from __future__ import annotations

import gettext
import struct
from importlib import resources

from zero_agent.core.exceptions import ConfigError


PROMPT_REPLY_LANGUAGE = "prompt.reply_language"
PROMPT_TASK_CONTROL = "prompt.task_control_protocol"
PROMPT_TODAY_LABEL = "prompt.today_label"
PROMPT_PEER_HINT = "prompt.peer_hint"

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


def _load_catalog(language: str) -> gettext.NullTranslations:
    requested = _language_code(language)
    candidates = [requested]
    if requested != "en":
        candidates.append("en")

    for candidate in candidates:
        resource = (
            resources.files("zero_agent.assets")
            .joinpath("locale", candidate, "LC_MESSAGES", "zero_agent.mo")
        )
        try:
            with resource.open("rb") as stream:
                return gettext.GNUTranslations(stream)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, struct.error) as exc:
            raise ConfigError(
                f"Prompt translation asset for {candidate} is invalid or unavailable"
            ) from exc

    raise ConfigError("Prompt translation asset is required but unavailable")


class PromptLocalizer:
    def __init__(self, language: str) -> None:
        self.language = _language_code(language)
        self._translations = _load_catalog(self.language)

    def text(self, message_id: str) -> str:
        translated = self._translations.gettext(message_id)
        if translated == message_id:
            raise ConfigError(f"Prompt translation is missing: {message_id}")
        return translated
