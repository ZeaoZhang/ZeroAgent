"""/review 命令: in-session adversarial code reviewer.

用户输入整段作为 user_request 注入 inline prompt; 主 agent 在当前 session 内按 prompt
协议自取审阅范围并 echo 报告, 不开 subagent、不写落盘文件。
"""

from __future__ import annotations

import os
from importlib import resources
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROMPT_DIR = "review_sop"
_INLINE_PROMPT_ZH = "review_inline_prompt.txt"
_INLINE_PROMPT_EN = "review_inline_prompt.en.txt"


def _render_prompt(user_request: str, memory_dir: str) -> str:
    """加载 /review inline prompt 并注入 user_request."""
    lang = os.environ.get("ZA_LANG", "").strip().lower()
    fname = _INLINE_PROMPT_EN if lang == "en" else _INLINE_PROMPT_ZH
    fpath = resources.files("zero_agent.assets").joinpath(_PROMPT_DIR, fname)
    project_root = str(_PROJECT_ROOT).replace("\\", "/")
    principles_path = (
        Path(memory_dir).resolve() / "sops" / "code_review_principles.md"
    ).as_posix()
    return fpath.read_text(encoding="utf-8").format(
        user_request=user_request,
        project_root=project_root,
        principles_path=principles_path,
    )


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
        "协议: `zero_agent/assets/review_sop/review_inline_prompt.txt` + "
        "`memory/sops/code_review_principles.md`"
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
    en = os.environ.get("ZA_LANG", "").strip().lower() == "en"
    user_request = body or (_DEFAULT_REQUEST_EN if en else _DEFAULT_REQUEST_ZH)
    header = _HEADER_EN if en else _HEADER_ZH
    return header + _render_prompt(user_request, agent.config.memory_dir)
