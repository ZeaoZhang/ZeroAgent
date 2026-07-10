"""/review command: in-session adversarial code reviewer.

Loads the review SOP inline prompt and injects the user's review request.
The returned prompt instructs the agent to perform the review in the current
session, echoing the report directly into the conversation.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from zero_agent.core.agent import ZeroAgent

_PROMPT_DIR = "review_sop"
_INLINE_PROMPT_ZH = "review_inline_prompt.txt"
_INLINE_PROMPT_EN = "review_inline_prompt.en.txt"

_STUB_FALLBACK_ZH = (
    "[/review in-session] (⚠️ prompt 文件缺失: {fpath} → {err})\n\n"
    "# 本轮用户请求\n{user_request}\n\n"
    "请按 memory/sops/code_review_principles.md 评审, 直接 echo 报告到对话。\n"
    "不要写 review.md, 不要打 [ROUND END]。"
)
_STUB_FALLBACK_EN = (
    "[/review in-session] (⚠️ prompt file missing: {fpath} → {err})\n\n"
    "# User request\n{user_request}\n\n"
    "Please review against memory/sops/code_review_principles.md, "
    "echo the report inline. No review.md, no [ROUND END]."
)

_DEFAULT_REQUEST_ZH = (
    "(无具体请求 — 默认审本次 uncommitted 改动: 用 code_run 跑 "
    "`git diff --stat HEAD` 与 `git diff HEAD`)"
)
_DEFAULT_REQUEST_EN = (
    "(no specific request — default to uncommitted diff: run "
    "`git diff --stat HEAD` and `git diff HEAD`)"
)
_HEADER_ZH = "> 🔍 /review (in-session) → 主 agent 当场审, 直接 echo 报告\n\n"
_HEADER_EN = "> 🔍 /review (in-session) → main agent reviews here, echoes the report inline\n\n"


def _render_prompt(user_request: str, agent: "ZeroAgent") -> str:
    """Load the /review inline prompt and inject user_request.

    Resolves language from agent.config.resolved_language ("zh" or "en")
    and loads the corresponding prompt template.  Falls back to a stub
    when the prompt file is missing.

    Args:
        user_request: the text the user typed after /review.
        agent: the ZeroAgent instance.

    Returns:
        The formatted inline prompt ready to inject as a user message.
    """
    en = agent.config.resolved_language == "en"
    fname = _INLINE_PROMPT_EN if en else _INLINE_PROMPT_ZH
    stub = _STUB_FALLBACK_EN if en else _STUB_FALLBACK_ZH
    fpath = resources.files("zero_agent.assets").joinpath(_PROMPT_DIR, fname)
    principles_path = (
        Path(agent.config.memory_dir).resolve() / "sops" / "code_review_principles.md"
    ).as_posix()

    try:
        return fpath.read_text(encoding="utf-8").format(
            user_request=user_request,
            principles_path=principles_path,
        )
    except Exception as e:
        return stub.format(fpath=fpath, err=e, user_request=user_request)


def handle(args: str, agent: "ZeroAgent") -> str:
    """Handle /review slash command.

    Args:
        args: user request text after /review (may be empty for default diff review).
        agent: the ZeroAgent instance.

    Returns:
        The review prompt to inject into the conversation as the next user message.
    """
    en = agent.config.resolved_language == "en"
    user_request = args.strip() or (_DEFAULT_REQUEST_EN if en else _DEFAULT_REQUEST_ZH)
    header = _HEADER_EN if en else _HEADER_ZH
    return header + _render_prompt(user_request, agent)
