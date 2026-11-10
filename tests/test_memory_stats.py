"""Tests for utils/memory_stats.py — memory file access statistics."""

import json
import os
import tempfile

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.handler import BaseHandler
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolRegistry
from zero_agent.utils.memory_stats import log_memory_access


class TestLogMemoryAccess:
    """log_memory_access() tests."""

    def test_records_when_memory_in_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stats_dir = os.path.join(tmpdir, "memory")
            path = os.path.join(tmpdir, "memory", "global_mem.txt")
            os.makedirs(os.path.dirname(path), exist_ok=True)

            log_memory_access(path, stats_dir=stats_dir)

            stats_file = os.path.join(stats_dir, "file_access_stats.json")
            assert os.path.isfile(stats_file)
            with open(stats_file, "r", encoding="utf-8") as f:
                stats = json.load(f)
            assert "global_mem.txt" in stats
            assert stats["global_mem.txt"]["count"] == 1
            assert "last" in stats["global_mem.txt"]

    def test_noop_when_memory_not_in_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stats_dir = os.path.join(tmpdir, "memory")
            path = os.path.join(tmpdir, "other", "file.txt")

            log_memory_access(path, stats_dir=stats_dir)

            stats_file = os.path.join(stats_dir, "file_access_stats.json")
            assert not os.path.isfile(stats_file)

    def test_creates_stats_file_on_first_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stats_dir = os.path.join(tmpdir, "memory")
            path = os.path.join(tmpdir, "memory", "sop.md")

            stats_file = os.path.join(stats_dir, "file_access_stats.json")
            assert not os.path.isfile(stats_file)

            log_memory_access(path, stats_dir=stats_dir)
            assert os.path.isfile(stats_file)

    def test_increments_count_on_repeated_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            stats_dir = os.path.join(tmpdir, "memory")
            path = os.path.join(tmpdir, "memory", "frequent.md")

            for _ in range(3):
                log_memory_access(path, stats_dir=stats_dir)

            stats_file = os.path.join(stats_dir, "file_access_stats.json")
            with open(stats_file, "r", encoding="utf-8") as f:
                stats = json.load(f)
            assert stats["frequent.md"]["count"] == 3

    def test_file_read_records_access_in_config_memory_dir(self, tmp_path, monkeypatch) -> None:
        workspace_dir = tmp_path / "workspace"
        memory_dir = tmp_path / "configured-memory"
        cwd_dir = tmp_path / "cwd"
        workspace_dir.mkdir()
        memory_dir.mkdir()
        (memory_dir / "sops").mkdir()
        cwd_dir.mkdir()
        target = memory_dir / "sops" / "memory_management_sop.md"
        target.write_text("SOP body\n", encoding="utf-8")

        config = AgentConfig(
            llm_backends={
                "default": LLMBackendConfig(
                    name="default",
                    provider="openai",
                    api_key="test-key",
                    api_base="https://api.openai.com/v1",
                    model="test-model",
                ),
            },
            default_backend="default",
            workspace_dir=str(workspace_dir),
            memory_dir=str(memory_dir),
        )
        registry = ToolRegistry.with_builtins(config)
        handler = BaseHandler(registry=registry, cwd=config.workspace_dir)
        monkeypatch.chdir(cwd_dir)

        result = _exhaust(handler.dispatch(
            "file_read",
            {"path": str(target), "show_linenos": False},
            MockResponse(),
        ))

        assert "SOP body" in result.data
        assert (memory_dir / "file_access_stats.json").is_file()
        assert not (cwd_dir / "memory" / "file_access_stats.json").exists()


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
