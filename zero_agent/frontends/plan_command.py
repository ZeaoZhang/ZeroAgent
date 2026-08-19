"""Plan 命令 — shared plan workspace creation.

Creates a ``plan_<slug>`` directory under a resolved ``root`` with a seeded
``plan.md``.  Slug derivation is pure-stdlib, ASCII-safe, bounded, and immune
to path traversal: every non-alphanumeric run (including ``/`` and ``..``) is
collapsed to ``_``, so the directory name can never escape ``root``.

Duplicate tasks reuse the same slug but get a unique ``_2``, ``_3``, ... suffix
on the directory name.
"""

from __future__ import annotations

import re
import shutil
import unicodedata
import warnings
from dataclasses import dataclass
from pathlib import Path

# Maximum slug length (matches the ``_slug`` bound used in ga_ultraplan).
_SLUG_MAX = 64
# Fallback slug when a task transliterates to nothing (e.g. Unicode-only).
_SLUG_FALLBACK = "task"


@dataclass(frozen=True)
class PlanWorkspace:
    """Result of :func:`create_plan_workspace`."""

    root: str       # resolved absolute root directory
    directory: str  # created plan_<slug> directory
    path: str       # created plan.md file path
    slug: str       # safe slug derived from the task


def _slugify(task: str) -> str:
    """Return a safe, bounded, ASCII slug for *task*.

    Accented letters are transliterated via NFKD; CJK and other non-Latin
    scripts are dropped.  A task with no Latin/digit content falls back to
    ``task`` so the slug is never empty.
    """
    transliterated = unicodedata.normalize("NFKD", task)
    transliterated = transliterated.encode("ascii", "ignore").decode("ascii", "ignore")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", transliterated).strip("_").lower()
    slug = slug[:_SLUG_MAX].rstrip("_")
    return slug or _SLUG_FALLBACK


# Line breaks (including Unicode separators) that would split a task title
# into extra Markdown lines.  They are replaced with spaces so a hostile task
# cannot start a new heading, checkbox, or horizontal rule on a second line.
_TITLE_LINE_BREAK_RE = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")
# Markdown/HTML structure characters that stay meaningful on a single line
# (links, images, code spans, ATX headings, raw HTML, autolinks).  ``<``,
# ``>`` and ``&`` are HTML-entity-encoded because backslash-escaping them does
# NOT stop marked v15's GFM bare-URL autolink (``\<http://evil\>`` still
# becomes ``<a>``).  ``:`` is backslash-escaped so ``http://``/``mailto:`` can
# no longer be recognised as a URL.  ``@`` and ``.`` are HTML-entity-encoded
# (``&#64;``/``&#46;``) so marked v15's bare email / www autolink cannot match
# them either — the browser still renders them readably as ``@``/``.``, but no
# ``<a>`` is generated.  Ordinary punctuation such as ``+``, ``-``, ``_``,
# ``*``, ``/`` is preserved as-is so task text stays recognizable.
_TITLE_ESCAPE_TRANS = str.maketrans(
    {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        ":": "\\:",
        "\\": "\\\\",
        "`": "\\`",
        "[": "\\[",
        "]": "\\]",
        "(": "\\(",
        ")": "\\)",
        "#": "\\#",
        "@": "&#64;",
        ".": "&#46;",
    }
)

def _sanitize_title(task: str) -> str:
    """Return *task* collapsed to a single, Markdown-structure-free line.
    CR/LF and Unicode line separators are replaced with spaces.  ``<``, ``>``
    and ``&`` are HTML-entity-encoded, ``:`` is backslash-escaped, ``@`` and
    ``.`` are HTML-entity-encoded (``&#64;``/``&#46;``), and ``[]()#`` plus
    backticks are backslash-escaped so a hostile task (e.g. ``"- [x] done"``,
    ``"## Fake"``, ``"<img onerror=...>"``, ``"<http://evil>"``,
    ``"evil@example.com"``, ``"www.example.com"``) cannot inject extra
    checkboxes, headings, links, code spans, raw HTML, or autolinks into
    plan.md.  Meaningful punctuation such as ``+``, ``-``, ``_``, ``*`` is
    preserved as-is.
    """
    title = _TITLE_LINE_BREAK_RE.sub(" ", task)
    title = title.translate(_TITLE_ESCAPE_TRANS)
    return " ".join(title.split())


def _plan_content(task: str) -> str:
    """Seed plan.md content — never fabricates completed items."""
    return (
        f"# Plan: {_sanitize_title(task)}\n\n"
        "## 探索发现\n\n"
        "（待填写）\n\n"
        "## 执行计划\n\n"
        "- [ ] 待规划\n\n"
        "## 验证\n\n"
        "（待填写）\n"
    )


def _create_unique_directory(root: Path, slug: str) -> Path:
    """Atomically create a ``plan_<slug>`` directory under *root* and return it.

    ``mkdir(exist_ok=False)`` is the atomic primitive: on ``FileExistsError``
    we advance to the next ``_2``, ``_3``, ... suffix and retry, so concurrent
    creators can never win the same path (no check-then-create race).
    """
    candidate = root / f"plan_{slug}"
    index = 2
    while True:
        try:
            candidate.mkdir(parents=False, exist_ok=False)
            return candidate
        except FileExistsError:
            candidate = root / f"plan_{slug}_{index}"
            index += 1


def _attach_note(exc: BaseException, note: str) -> None:
    """Record *note* on *exc* without ever masking the original exception.

    ``BaseException.add_note`` only exists on Python 3.11+.  On 3.10 the note
    is surfaced via a ``RuntimeWarning``; if warnings are configured to raise
    (``simplefilter("error")``), it is instead stored on a plain ``__notes__``
    attribute so the cleanup failure stays observable while the original
    exception remains primary.
    """
    add_note = getattr(exc, "add_note", None)
    if callable(add_note):
        add_note(note)
        return
    try:
        warnings.warn(note, RuntimeWarning, stacklevel=3)
    except Exception:
        notes = getattr(exc, "__notes__", None)
        if notes is None:
            try:
                exc.__notes__ = [note]
            except Exception:
                pass
        else:
            notes.append(note)


def create_plan_workspace(root: str, task: str) -> PlanWorkspace:
    """Create a shared plan workspace and return its metadata.

    Args:
        root: parent directory (created if missing; resolved to an absolute
            real path).
        task: human-readable task description, also recorded in ``plan.md``.

    Returns:
        PlanWorkspace describing the created directory and plan file.

    Raises:
        ValueError: if *task* is blank.
        OSError: if the directory or ``plan.md`` cannot be written; in that
            case only the directory created by this call is removed.

    Warns:
        RuntimeWarning: if cleanup of the just-created directory fails; the
            original exception is still re-raised.
    """
    if task is None or not str(task).strip():
        raise ValueError("task must be a non-empty string")

    task_text = str(task).strip()
    root_path = Path(root).resolve()
    root_path.mkdir(parents=True, exist_ok=True)

    slug = _slugify(task_text)
    directory = _create_unique_directory(root_path, slug)
    plan_path = directory / "plan.md"

    try:
        plan_path.write_text(_plan_content(task_text), encoding="utf-8")
    except Exception as write_err:
        # Only clean up the directory this call actually created.  Do NOT use
        # ignore_errors=True: a failed cleanup must stay observable.
        try:
            shutil.rmtree(directory)
        except OSError as cleanup_err:
            try:
                warnings.warn(
                    f"failed to clean up plan workspace {directory}: {cleanup_err}",
                    RuntimeWarning,
                    stacklevel=2,
                )
            except Exception:
                # Warnings may be configured to raise (``simplefilter("error")``);
                # the original write exception must stay the primary exception,
                # so keep the cleanup failure observable without masking it.
                _attach_note(write_err, f"cleanup also failed: {cleanup_err}")
        raise

    return PlanWorkspace(
        root=str(root_path),
        directory=str(directory),
        path=str(plan_path),
        slug=slug,
    )
