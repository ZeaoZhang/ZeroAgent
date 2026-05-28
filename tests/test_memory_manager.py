"""Tests for memory/manager.py — MemoryManager."""

import os
import tempfile

from zero_agent.memory.manager import MemoryManager


class TestMemoryManager:
    """MemoryManager tests."""

    def test_init_creates_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(
                memory_dir=os.path.join(tmp, "memory"),
                workspace_dir=os.path.join(tmp, "workspace"),
            )
            mgr.init_memory()

            assert os.path.isdir(os.path.join(tmp, "memory"))
            assert os.path.isfile(os.path.join(tmp, "memory", "global_mem.txt"))
            assert os.path.isfile(os.path.join(tmp, "memory", "global_mem_insight.txt"))
            assert os.path.isdir(os.path.join(tmp, "memory", "L4_raw_sessions"))

    def test_init_idempotent(self) -> None:
        """重复 init 不会覆盖已有文件."""
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(memory_dir=os.path.join(tmp, "memory"))
            mgr.init_memory()

            # 写入自定义内容
            l2_path = os.path.join(tmp, "memory", "global_mem.txt")
            with open(l2_path, "w") as f:
                f.write("custom content")

            # 再次 init
            mgr.init_memory()

            # 内容不应被覆盖
            with open(l2_path) as f:
                assert f.read() == "custom content"

    def test_get_global_memory_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(
                memory_dir=os.path.join(tmp, "memory"),
                workspace_dir=os.path.join(tmp, "workspace"),
            )
            mgr.init_memory()

            ctx = mgr.get_global_memory_context()
            assert "cwd =" in ctx
            assert "CONSTITUTION" in ctx
            assert "global_mem_insight.txt" in ctx

    def test_get_sop_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = os.path.join(tmp, "memory")
            mgr = MemoryManager(memory_dir=mem_dir)
            mgr.init_memory()

            # 创建一个 SOP 文件
            sop_path = os.path.join(mem_dir, "test_sop.md")
            with open(sop_path, "w") as f:
                f.write("# Test SOP")

            # 不含扩展名查找
            found = mgr.get_sop_path("test_sop")
            assert found == sop_path

            # 含扩展名查找
            found = mgr.get_sop_path("test_sop.md")
            assert found == sop_path

    def test_get_sop_path_nonexistent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mgr = MemoryManager(memory_dir=os.path.join(tmp, "memory"))
            mgr.init_memory()
            assert mgr.get_sop_path("nonexistent") is None

    def test_list_sops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            mem_dir = os.path.join(tmp, "memory")
            mgr = MemoryManager(memory_dir=mem_dir)
            mgr.init_memory()

            # 创建测试文件
            for name in ["sop_a.md", "sop_b.md", "utils.py", "notes.txt"]:
                with open(os.path.join(mem_dir, name), "w") as f:
                    f.write("test")

            sops = mgr.list_sops()
            assert "sop_a.md" in sops
            assert "sop_b.md" in sops
            assert "utils.py" in sops
            assert "notes.txt" not in sops
            # init_memory() 复制了 package 内置 SOP, 所以总数 > 3
            assert len(sops) > 3

    def test_list_sops_nonexistent_dir(self) -> None:
        mgr = MemoryManager(memory_dir="/nonexistent/path")
        assert mgr.list_sops() == []
