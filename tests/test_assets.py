"""Tests for bundled package assets."""

from importlib import resources

from zero_agent.tools.builtin import code as code_tools


def test_code_run_header_resource_is_readable() -> None:
    header = resources.files("zero_agent.assets").joinpath("code_run_header.py")

    assert header.is_file()
    assert "subprocess.run" in header.read_text(encoding="utf-8")
    assert code_tools._load_code_run_header()


def test_code_run_header_missing_is_non_fatal(monkeypatch) -> None:
    def _missing_files(_package: str):
        raise FileNotFoundError("missing package resource")

    monkeypatch.setattr(code_tools.resources, "files", _missing_files)

    assert code_tools._load_code_run_header() == ""
