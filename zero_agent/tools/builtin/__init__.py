"""Builtin tool implementations for ZeroAgent's core tool set.

Core tools (auto-registered by with_builtins()):
    code_run              — Execute Python/Shell code in isolated subprocess
    file_read             — Read files with line range and keyword search
    file_patch            — Exact string replacement in files
    file_write            — Create/overwrite/append files
    web_scan              — Fetch simplified HTML and tab list
    web_execute_js        — Execute JavaScript in browser
    update_working_checkpoint — Short-term working memory (key_info + related_sop)
    ask_user              — Interrupt task to ask user a question
    start_long_term_update    — Trigger long-term memory distillation

Virtual/internal tools (handled by engine/Handler, NOT in schema):
    no_tool  — Triggered by engine when LLM produces no tool call
    bad_json — Triggered by engine when LLM produces malformed JSON arguments

Other capabilities (NOT exposed as standalone tools):
    vision   — Via SOP + code_run: LLM writes Python to call zero_agent.utils.vision_api.ask_vision()
    IM send  — Handled by bot processes, not agent tool
"""
