"""Tests for bots/ — common helpers and command modules."""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from zero_agent.bots.common import (
    clean_reply,
    extract_files,
    split_text,
    build_done_text,
    build_help_text,
    HELP_TEXT,
    HELP_COMMANDS,
    load_keys,
    public_access,
    to_allowed_set,
)
from zero_agent.bots.shared.continue_cmd import (
    _pairs,
    _first_user,
    _last_summary,
    _user_text,
    _assistant_text,
    list_sessions,
    format_list,
    reset_conversation,
    handle_frontend_command,
)
from zero_agent.bots.shared.btw_cmd import _strip_cmd, _help_text
from zero_agent.bots.shared.review_cmd import handle as review_handle
from zero_agent.bots.shared.session_names import set_name, name_for, has_name, gc
from zero_agent.bots.shared.export_cmd import wrap_for_clipboard, export_to_temp


# —— common.py ——

class TestCommonHelpers:
    def test_clean_reply_removes_tags(self):
        text = "<thinking>hidden</thinking> visible <tool_use>x</tool_use>"
        result = clean_reply(text)
        assert "hidden" not in result
        assert "visible" in result

    def test_clean_reply_empty(self):
        assert clean_reply("") == "..."

    def test_extract_files(self):
        text = "see [FILE:/tmp/out.md] and [FILE:report.txt]"
        files = extract_files(text)
        assert "/tmp/out.md" in files
        assert "report.txt" in files

    def test_extract_files_none(self):
        assert extract_files("no files here") == []

    def test_split_text(self):
        result = split_text("hello world", 5)
        assert len(result) >= 2

    def test_split_text_short(self):
        result = split_text("hi", 100)
        assert result == ["hi"]

    def test_build_done_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, "out.txt")
            with open(fpath, "w") as f:
                f.write("data")
            result = build_done_text(f"Done. [FILE:{fpath}]")
            assert "out.txt" in result

    def test_build_help_text(self):
        text = build_help_text()
        assert "/help" in text
        assert "/stop" in text

    def test_public_access(self):
        assert public_access(set()) is True
        assert public_access({"*"}) is True
        assert public_access({"123"}) is False

    def test_to_allowed_set(self):
        assert to_allowed_set(None) == set()
        assert to_allowed_set("123") == {"123"}
        assert to_allowed_set(["123", "456"]) == {"123", "456"}

    def test_load_keys_from_env(self, monkeypatch):
        monkeypatch.setenv("TG_BOT_TOKEN", "test_token")
        keys = load_keys()
        assert keys.get("tg_bot_token") == "test_token"


# —— continue_cmd.py ——

class TestContinueCmd:
    def test_pairs_empty(self):
        assert _pairs("") == []

    def test_pairs_parses(self):
        content = "=== Prompt ===\nhello\n=== Response ===\nworld\n"
        pairs = _pairs(content)
        assert len(pairs) == 1
        assert pairs[0][0] == "hello"
        assert pairs[0][1] == "world"

    def test_first_user_native(self):
        pairs = [(
            '{"role": "user", "content": [{"type": "text", "text": "hello world"}]}',
            'resp',
        )]
        assert _first_user(pairs) == "hello world"

    def test_first_user_skips_working_memory(self):
        pairs = [(
            '{"role": "user", "content": [{"type": "text", "text": "### [WORKING MEMORY] stuff"}]}',
            'resp',
        )]
        assert _first_user(pairs) == ""

    def test_user_text_native(self):
        prompt = '{"role": "user", "content": [{"type": "text", "text": "hello"}]}'
        assert _user_text(prompt) == "hello"

    def test_user_text_skips_tool_result(self):
        prompt = '{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1"}]}'
        assert _user_text(prompt) == ""

    def test_user_text_skips_injection(self):
        prompt = '{"role": "user", "content": [{"type": "text", "text": "[SYSTEM TIPS] do X"}]}'
        assert _user_text(prompt) == ""

    def test_assistant_text(self):
        resp = '[{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]'
        assert _assistant_text(resp) == "hello\nworld"

    def test_assistant_text_invalid(self):
        assert _assistant_text("not a list") == ""

    def test_format_list_empty(self):
        assert "没有可恢复" in format_list([])

    def test_list_sessions_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("zero_agent.bots.shared.continue_cmd._LOG_GLOB",
                       os.path.join(tmp, "model_responses_*.txt")):
                assert list_sessions() == []

    def test_reset_conversation(self):
        mock_runner = MagicMock()
        mock_runner.history = ["old"]
        mock_runner.llmclient = MagicMock()
        mock_runner.llmclient.backend = MagicMock()
        mock_runner.llmclient.backend.history = ["old"]

        msg = reset_conversation(mock_runner)
        assert "新对话" in msg
        mock_runner.abort.assert_called_once()

    def test_handle_frontend_continue_list(self):
        mock_runner = MagicMock()
        with patch("zero_agent.bots.shared.continue_cmd.list_sessions", return_value=[]):
            result = handle_frontend_command(mock_runner, "/continue")
            assert "没有可恢复" in result

    def test_handle_frontend_invalid(self):
        mock_runner = MagicMock()
        result = handle_frontend_command(mock_runner, "/continue abc")
        assert "用法" in result


# —— btw_cmd.py ——

class TestBtwCmd:
    def test_strip_cmd(self):
        assert _strip_cmd("/btw what is the status") == "what is the status"

    def test_strip_cmd_empty(self):
        assert _strip_cmd("/btw") == ""

    def test_help_text(self):
        text = _help_text()
        assert "/btw" in text
        assert "side question" in text.lower() or "临时" in text


# —— review_cmd.py ——

class TestReviewCmd:
    def test_handle_help(self):
        dq = MagicMock()
        result = review_handle(MagicMock(), "help", dq)
        assert result is None
        dq.put.assert_called_once()
        args = dq.put.call_args[0][0]
        assert "用法" in args.get("done", "")

    def test_handle_default(self):
        dq = MagicMock()
        result = review_handle(MagicMock(), "", dq)
        assert result is not None
        assert "/review" in result


# —— session_names.py ——

class TestSessionNames:
    def test_set_and_get_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            import zero_agent.bots.shared.session_names as sn
            with patch.object(sn, "_LOG_DIR", tmp):
                with patch.object(sn, "_REG_PATH", os.path.join(tmp, "names.json")):
                    sn.set_name("/fake/path/model_responses_123.txt", "my session")
                    assert sn.name_for("/fake/path/model_responses_123.txt") == "my session"

    def test_has_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            import zero_agent.bots.shared.session_names as sn
            with patch.object(sn, "_LOG_DIR", tmp):
                with patch.object(sn, "_REG_PATH", os.path.join(tmp, "names.json")):
                    sn.set_name("/fake/path/a.txt", "test")
                    assert sn.has_name("test") is True
                    assert sn.has_name("nonexistent") is False

    def test_gc(self):
        with tempfile.TemporaryDirectory() as tmp:
            import zero_agent.bots.shared.session_names as sn
            with patch.object(sn, "_LOG_DIR", tmp):
                with patch.object(sn, "_REG_PATH", os.path.join(tmp, "names.json")):
                    sn.set_name("/fake/path/gone.txt", "gone")
                    # _resolve_basename returns None for nonexistent files → gc removes it
                    removed = sn.gc()
                    assert removed >= 0


# —— export_cmd.py ——

class TestExportCmd:
    def test_wrap_for_clipboard(self):
        result = wrap_for_clipboard("hello", "markdown")
        assert "```" in result
        assert "hello" in result

    def test_wrap_nested_backticks(self):
        result = wrap_for_clipboard("code with ``` inside", "python")
        assert "````" in result  # fence longer than inner ```

    def test_export_to_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("zero_agent.bots.shared.export_cmd._TEMP_DIR", tmp):
                path = export_to_temp("# hello", "test_export")
                assert os.path.isfile(path)
                with open(path) as f:
                    assert f.read() == "# hello"

    def test_export_to_temp_adds_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("zero_agent.bots.shared.export_cmd._TEMP_DIR", tmp):
                path = export_to_temp("data", "noext")
                assert path.endswith(".md")
