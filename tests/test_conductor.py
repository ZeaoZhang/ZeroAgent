"""Tests for frontends/conductor.py — Conductor backend and helpers."""

import pytest


def test_conductor_import_succeeds():
    """Importing zero_agent.frontends.conductor should succeed without starting server."""
    import zero_agent.frontends.conductor  # noqa: F401


def test_clean_log_text_removes_turn_splits():
    from zero_agent.frontends.conductor import clean_log_text

    text = "Hello\n***LLM Running (Turn 1) ...***\nWorld"
    result = clean_log_text(text)
    assert "LLM Running" not in result
    assert "Hello" in result
    assert "World" in result


def test_clean_log_text_removes_fenced_code():
    from zero_agent.frontends.conductor import clean_log_text

    text = "Before\n```python\nprint(1)\n```\nAfter"
    result = clean_log_text(text)
    assert "```" not in result
    assert "Before" in result
    assert "After" in result


def test_extract_last_summary_finds_summary():
    from zero_agent.frontends.conductor import extract_last_summary

    text = "<summary>key finding</summary>Then more text"
    result = extract_last_summary(text)
    assert "key finding" in result


def test_extract_last_summary_no_tags_uses_tail():
    from zero_agent.frontends.conductor import extract_last_summary

    text = "x" * 1500
    result = extract_last_summary(text)
    assert len(result) <= 1000


def test_short_id_is_hex():
    from zero_agent.frontends.conductor import short_id

    sid = short_id()
    assert len(sid) == 8
    assert all(c in "0123456789abcdef" for c in sid)
