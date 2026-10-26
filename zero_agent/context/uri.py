"""Parser and canonicalizer for ZeroAgent logical context URIs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from zero_agent.context.errors import ContextError
from zero_agent.context.types import RuntimeContext

_BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_AUTHORITIES = {"users", "projects", "skills", "sessions"}


@dataclass(frozen=True)
class ParsedContextURI:
    authority: str
    segments: tuple[str, ...]

    @property
    def canonical(self) -> str:
        suffix = "/".join(quote(part, safe="-._~") for part in self.segments)
        return f"za://{self.authority}/{suffix}" if suffix else f"za://{self.authority}"


def _decode_segment(raw: str) -> str:
    if not raw or _BAD_ESCAPE.search(raw):
        raise ContextError("INVALID_URI", "The context URI contains an invalid path segment.")
    try:
        decoded = unquote(raw, encoding="utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ContextError("INVALID_URI", "The context URI is not valid UTF-8.") from exc
    if (
        decoded in {".", ".."}
        or "/" in decoded
        or "\\" in decoded
        or "\x00" in decoded
        or any(ord(char) < 0x20 for char in decoded)
    ):
        raise ContextError("INVALID_URI", "The context URI contains a forbidden path segment.")
    return decoded


def parse_context_uri(uri: str, runtime: RuntimeContext) -> ParsedContextURI:
    """Parse one URI, expand ``me`` using trusted runtime identity, and canonicalize."""

    if not isinstance(uri, str) or not uri or "\\" in uri or "\x00" in uri:
        raise ContextError("INVALID_URI", "A valid logical context URI is required.")
    try:
        parts = urlsplit(uri)
        port = parts.port
    except ValueError as exc:
        raise ContextError("INVALID_URI", "The context URI is malformed.") from exc
    authority = parts.netloc.lower()
    if (
        parts.scheme.lower() != "za"
        or authority not in _AUTHORITIES
        or parts.query
        or parts.fragment
        or parts.username is not None
        or parts.password is not None
        or port is not None
    ):
        raise ContextError("INVALID_URI", "The context URI uses an unsupported address form.")

    raw_segments = parts.path.split("/")[1:] if parts.path.startswith("/") else []
    if not raw_segments or any(not segment for segment in raw_segments):
        raise ContextError("INVALID_URI", "The context URI contains an empty path segment.")
    segments = tuple(_decode_segment(segment) for segment in raw_segments)
    if authority == "users" and segments[0] == "me":
        segments = (runtime.user_id, *segments[1:])

    if not _valid_shape(authority, segments):
        raise ContextError("INVALID_URI", "The context URI does not name a supported context object.")
    return ParsedContextURI(authority, segments)


def _valid_shape(authority: str, segments: tuple[str, ...]) -> bool:
    if authority == "users":
        return (
            len(segments) == 1
            or (len(segments) == 2 and segments[1] == "memories")
            or (len(segments) == 3 and segments[1] == "memories")
            or (len(segments) == 4 and segments[1] == "memories")
        )
    if authority == "projects":
        return len(segments) == 1 or (
            len(segments) in {2, 3, 4}
            and segments[1] in {"memories", "resources"}
        )
    if authority == "skills":
        return (segments[0] == "builtin" and len(segments) in {1, 2}) or (
            segments[0] == "users" and len(segments) in {2, 3}
        )
    if authority == "sessions":
        return len(segments) == 1 or (
            segments[1] == "archives" and len(segments) in {2, 3}
        )
    return False


def uri_is_within(uri: ParsedContextURI, prefix: ParsedContextURI) -> bool:
    """Whether a parsed object address is beneath a parsed collection address."""

    return uri.authority == prefix.authority and uri.segments[: len(prefix.segments)] == prefix.segments
