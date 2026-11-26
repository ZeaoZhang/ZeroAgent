"""Tests for the interactive configuration wizard."""

from __future__ import annotations

from zero_agent.utils import configure


def _drive(monkeypatch, answers, secret="synthetic-audit-marker"):
    """Run the wizard with scripted answers and capture stdout."""
    remaining = list(answers)
    written = []

    monkeypatch.setattr(configure, "getpass", lambda _prompt="": secret)
    monkeypatch.setattr(
        configure,
        "_prompt",
        lambda _text, default="": remaining.pop(0) if remaining else default,
    )
    monkeypatch.setattr(configure, "_write_config", lambda content: written.append(content))

    configure._interactive_configure()

    return written


def test_interactive_configure_never_echoes_the_api_key(
    monkeypatch, capsys
) -> None:
    written = _drive(monkeypatch, ["1", "1", "./workspace", "n"])

    out = capsys.readouterr().out

    assert "synthetic-audit-marker" not in out
    assert "***" in out or "…" in out
    assert written == []


def test_interactive_configure_writes_the_real_key(monkeypatch, capsys) -> None:
    written = _drive(monkeypatch, ["1", "1", "./workspace", "y"])

    out = capsys.readouterr().out

    assert "synthetic-audit-marker" not in out
    assert len(written) == 1
    assert "synthetic-audit-marker" in written[0]
