"""/review 命令: in-session adversarial code reviewer.

用户输入整段作为 user_request 注入 inline prompt; 主 agent 在当前 session 内按 prompt
协议自取审阅范围并 echo 报告, 不开 subagent、不写落盘文件。
"""

from __future__ import annotations

import os
from typing import Optional

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROMPT_DIR = "review_sop"
_INLINE_PROMPT_ZH = "review_inline_prompt.txt"
_INLINE_PROMPT_EN = "review_inline_prompt.en.txt"
_STUB_FALLBACK = (
    "[/review in-session] (⚠️ prompt 文件缺失: {fpath} → {err})\n\n"
    "# 本轮用户请求\n{user_request}\n\n"
    "请按 memory/code_review_principles.md 评审, 直接 echo 报告到对话。\n"
    "不要写 review.md, 不要打 [ROUND END]。"
)


def _render_prompt(user_request: str) -> str:
    """加载 /review inline prompt 并注入 user_request."""
    lang = os.environ.get("GA_LANG", "").strip().lower()
    fname = _INLINE_PROMPT_EN if lang == "en" else _INLINE_PROMPT_ZH
    fpath = os.path.join(_PROJECT_ROOT, "memory", _PROMPT_DIR, fname)
    ga_root = _PROJECT_ROOT.replace("\\", "/")
    try:
        with open(fpath, "r", encoding="utf-8") as f:
            return f.read().format(user_request=user_request, ga_root=ga_root)
    except Exception as e:
        return _STUB_FALLBACK.format(fpath=fpath, err=e, user_request=user_request)


def _help_text() -> str:
    return (
        "**/review 用法**: in-session adversarial code reviewer\n\n"
        "`/review                  ` # 默认审本次 uncommitted 改动(主 agent 跑 git diff)\n"
        "`/review <自然语言请求>   ` # 主 agent 按你描述的范围去审\n\n"
        "例:\n"
        "  `/review`\n"
        "  `/review 我刚改了 review_cmd.py, 关注 prompt 注入`\n"
        "  `/review 审 frontends 目录下所有改过的文件`\n\n"
        "产出: 直接对话 markdown(不写文件、不开 subagent)。\n"
        "协议: `memory/review_sop/review_inline_prompt.txt` + `memory/code_review_principles.md`"
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


def handle(agent, body: str, display_queue) -> Optional[str]:
    """body 是已剥离 `/review` 前缀的纯参数文本.

    help → 推 done; 否则注入 user_request 到 inline prompt return 给主 agent.
    """
    if body in ("help", "?", "-h", "--help"):
        display_queue.put({"done": _help_text(), "source": "system"})
        return None
    en = os.environ.get("GA_LANG", "").strip().lower() == "en"
    user_request = body or (_DEFAULT_REQUEST_EN if en else _DEFAULT_REQUEST_ZH)
    header = _HEADER_EN if en else _HEADER_ZH
    return header + _render_prompt(user_request)
