"""Builtin tool implementations for ZeroAgent's core tool set.

Core tools (auto-registered by with_builtins()):
    code_run              — Execute Python/Shell code in isolated subprocess
    file_read             — Read files with line range and keyword search
    file_patch            — Exact string replacement in files
    file_write            — Create/overwrite/append files
    find                  — Glob and content search in workspace
    read_doc              — Extract text from PDF/Office/CSV/HTML
    fetch                 — HTTP request with SSRF/size guards
    web_search            — DuckDuckGo/Brave web search
    update_working_checkpoint — Short-term working memory (key_info + related_sop)
    ask_user              — Interrupt task to ask user a question
    start_long_term_update    — Trigger long-term memory distillation
    enter_plan_mode       — Bind a plan.md and switch the task contract to PLAN
    add_done_hook         — Queue a self-check turn before completion is accepted
    memory_profile        — Maintain the injected user profile (preferences/commitments)

Virtual/internal tools (handled by engine/Handler, NOT in schema):
    no_tool  — Triggered by engine when LLM produces no tool call
    bad_json — Triggered by engine when LLM produces malformed JSON arguments

Optional browser tools (loaded by the built-in browser_tools extension when
extensions.browser is auto/on):
    web_scan, web_execute_js, web_screenshot, web_tabs, web_cdp, web_input,
    web_network, web_intercept, web_captcha, web_wait, upload, download

Other capabilities:
    vision   — A configured multimodal backend exposed as a conditional builtin tool.
    IM send  — Handled by bot processes, not agent tool.
"""
