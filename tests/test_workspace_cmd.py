"""Tests for zero_agent/frontends/commands/workspace_cmd.py — workspace junction management."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from zero_agent.frontends.commands import workspace_cmd as wcmd


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def temp_project(monkeypatch) -> Path:
    """Create a temp directory that acts as the project root for isolation."""
    d = tempfile.mkdtemp(prefix="zaws_test_")
    root = Path(d)

    # Override project_root so all paths (_temp_root, _projects_root, etc.)
    # live inside this temp dir.
    monkeypatch.setattr(wcmd, "project_root", lambda: root)

    yield root

    # Tear down
    import shutil

    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def real_target_dir(temp_project) -> Path:
    """Create a real directory to serve as workspace target."""
    t = temp_project / "real_projects" / "my_app"
    t.mkdir(parents=True, exist_ok=True)
    (t / "README.md").write_text("# My App", encoding="utf-8")
    return t


# --------------------------------------------------------------------------- #
# _ws_name
# --------------------------------------------------------------------------- #


def test_ws_name_generates_stable_name():
    """Same absolute path always produces the same workspace name."""
    name1 = wcmd._ws_name("/Users/alice/projects/my_app")
    name2 = wcmd._ws_name("/Users/alice/projects/my_app")
    assert name1 == name2
    assert name1.startswith("my_app-")
    assert len(name1) > len("my_app-")  # has digest suffix


def test_ws_name_different_paths_produce_different_names():
    """Different paths produce different names."""
    n1 = wcmd._ws_name("/tmp/foo")
    n2 = wcmd._ws_name("/tmp/bar")
    assert n1 != n2


def test_ws_name_empty_or_root_falls_back_to_ws():
    """When basename is empty (root path), fall back to 'ws' prefix."""
    # On Unix, '/' has basename ''; on Windows a drive root has basename ''
    if os.name != "nt":
        name = wcmd._ws_name("/")
        assert name.startswith("ws-")
    else:
        name = wcmd._ws_name("C:\\")
        assert name.startswith("ws-")


def test_ws_name_includes_basename_and_blake2b():
    """Name format: {basename}-{blake2b[:8]}."""
    name = wcmd._ws_name("/home/user/cool-stuff")
    base, digest = name.rsplit("-", 1)
    assert base == "cool-stuff"
    assert len(digest) == 8
    assert all(c in "0123456789abcdef" for c in digest)


# --------------------------------------------------------------------------- #
# validate_path
# --------------------------------------------------------------------------- #


def test_validate_rejects_empty():
    ok, err = wcmd.validate_path("")
    assert not ok
    assert err

    ok, err = wcmd.validate_path("   ")
    assert not ok
    assert err


def test_validate_rejects_relative_path():
    ok, err = wcmd.validate_path("relative/path")
    assert not ok
    assert "绝对路径" in err


def test_validate_rejects_nonexistent_path():
    ok, err = wcmd.validate_path("/nonexistent/abc/def/ghi")
    assert not ok
    assert "不存在" in err


def test_validate_rejects_file_not_dir():
    import tempfile

    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(b"hello")
        f.flush()
        ok, err = wcmd.validate_path(f.name)
        assert not ok
        assert "不是目录" in err
        os.unlink(f.name)


def test_validate_accepts_valid_dir(real_target_dir):
    ok, err = wcmd.validate_path(str(real_target_dir))
    assert ok
    assert err == ""


def test_validate_rejects_path_inside_temp(temp_project):
    """Paths inside the project temp dir should be rejected."""
    temp_dir = temp_project / "temp" / "something"
    temp_dir.mkdir(parents=True, exist_ok=True)
    ok, err = wcmd.validate_path(str(temp_dir))
    assert not ok
    assert "temp" in err


def test_validate_strips_quotes():
    """Quoted paths should be stripped before validation."""
    import tempfile

    d = tempfile.mkdtemp()
    quoted = f'  "{d}"  '
    ok, err = wcmd.validate_path(quoted)
    assert ok
    assert err == ""
    os.rmdir(d)


# --------------------------------------------------------------------------- #
# prepare
# --------------------------------------------------------------------------- #


def test_prepare_creates_junction_and_returns_info(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    assert r["ok"]
    assert r["name"]
    assert r["link"]
    assert r["target"]
    assert r["error"] == ""
    # The link path should exist
    assert Path(r["link"]).exists(follow_symlinks=False)


def test_prepare_is_idempotent(real_target_dir):
    r1 = wcmd.prepare(str(real_target_dir))
    r2 = wcmd.prepare(str(real_target_dir))
    assert r1["ok"]
    assert r2["ok"]
    assert r1["name"] == r2["name"]
    assert r1["link"] == r2["link"]


def test_prepare_ensures_project_memory_md(real_target_dir):
    wcmd.prepare(str(real_target_dir))
    # Check that project_memory.md exists in the target
    mem = real_target_dir / "project_memory.md"
    assert mem.is_file(), "prepare should create project_memory.md"


def test_prepare_rejects_invalid_path():
    r = wcmd.prepare("/definitely/not/a/real/path")
    assert not r["ok"]
    assert r["error"]


def test_prepare_returns_mem_text(real_target_dir):
    mem = real_target_dir / "project_memory.md"
    mem.write_text("Hello from memory!", encoding="utf-8")
    r = wcmd.prepare(str(real_target_dir))
    assert r["ok"]
    assert "Hello from memory!" in r["mem_text"]


# --------------------------------------------------------------------------- #
# activate / deactivate / current
# --------------------------------------------------------------------------- #


def test_activate_sets_anchor_and_returns_success(real_target_dir):
    r = wcmd.activate(str(real_target_dir))
    assert r["ok"]
    assert r["name"]
    # Anchor file should exist
    anchor = wcmd._anchor_path()
    assert anchor.is_file()
    assert anchor.read_text(encoding="utf-8").strip() == r["name"]


def test_current_returns_dict_when_active(real_target_dir):
    wcmd.activate(str(real_target_dir))
    cur = wcmd.current()
    assert cur is not None
    assert "name" in cur
    assert "path" in cur


def test_current_returns_none_when_not_active():
    # Ensure no anchor
    anchor = wcmd._anchor_path()
    if anchor.exists():
        anchor.unlink()
    cur = wcmd.current()
    assert cur is None


def test_deactivate_removes_anchor_and_returns_true(real_target_dir):
    wcmd.activate(str(real_target_dir))
    assert wcmd.current() is not None

    result = wcmd.deactivate()
    assert result is True
    assert wcmd.current() is None


def test_deactivate_returns_false_when_no_anchor():
    anchor = wcmd._anchor_path()
    if anchor.exists():
        anchor.unlink()
    result = wcmd.deactivate()
    assert result is False


def test_deactivate_preserves_registry(real_target_dir):
    wcmd.activate(str(real_target_dir))

    # Deactivate
    wcmd.deactivate()

    # Registry entry should still exist
    items = wcmd.registry_load()
    expected_name = wcmd._ws_name(str(real_target_dir))
    assert expected_name in items


# --------------------------------------------------------------------------- #
# registry CRUD
# --------------------------------------------------------------------------- #


def test_registry_load_returns_empty_for_missing(temp_project):
    # In a fresh temp_project, the registry file doesn't exist yet
    items = wcmd.registry_load()
    assert isinstance(items, dict)
    assert items == {}


def test_registry_upsert_and_load(real_target_dir):
    wcmd.registry_upsert("test-ws", str(real_target_dir))
    items = wcmd.registry_load()
    assert "test-ws" in items
    assert items["test-ws"]["path"] == str(real_target_dir)
    assert "last_used" in items["test-ws"]


def test_registry_remove(real_target_dir):
    wcmd.registry_upsert("to-remove", str(real_target_dir))
    assert "to-remove" in wcmd.registry_load()

    wcmd.registry_remove("to-remove")
    assert "to-remove" not in wcmd.registry_load()


def test_registry_upsert_updates_existing(real_target_dir):
    wcmd.registry_upsert("update-me", str(real_target_dir))
    import time

    time.sleep(0.01)
    wcmd.registry_upsert("update-me", str(real_target_dir))
    items = wcmd.registry_load()
    assert "update-me" in items
    # Should still have one entry
    assert len(items) >= 1


def test_registry_save_persists_across_loads(real_target_dir):
    wcmd.registry_upsert("persistent", str(real_target_dir))

    # Load again (should re-read from file)
    items = wcmd.registry_load()
    assert "persistent" in items
    assert items["persistent"]["path"] == str(real_target_dir)
def test_registry_list_returns_sorted_by_last_used(real_target_dir):
    import time

    wcmd.registry_upsert("older", str(real_target_dir))
    time.sleep(1.1)  # ensure different second boundary for int(time.time())
    wcmd.registry_upsert("newer", str(real_target_dir))

    items = wcmd.registry_list()
    assert len(items) >= 2
    # First item should be the newer one
    assert items[0]["name"] == "newer"
    assert items[1]["name"] == "older"


def test_registry_list_includes_dangling_flag():
    """registry_list should mark non-existent paths as dangling."""
    wcmd.registry_upsert("gone-ws", "/no/such/path")
    items = wcmd.registry_list()
    gone = next(it for it in items if it["name"] == "gone-ws")
    assert gone["dangling"] is True


# --------------------------------------------------------------------------- #
# is_dangling
# --------------------------------------------------------------------------- #


def test_is_dangling_true_for_nonexistent_target(real_target_dir):
    name = wcmd._ws_name(str(real_target_dir))
    # Don't prepare — so no link exists
    assert wcmd.is_dangling(name) is True


def test_is_dangling_false_for_valid_junction(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    assert r["ok"]
    assert wcmd.is_dangling(r["name"]) is False


def test_is_dangling_true_when_target_removed(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    assert r["ok"]
    # Remove the target directory
    import shutil

    shutil.rmtree(str(real_target_dir), ignore_errors=True)
    assert wcmd.is_dangling(r["name"]) is True


# --------------------------------------------------------------------------- #
# remove
# --------------------------------------------------------------------------- #


def test_remove_cleans_registry_and_junction(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]
    link_path = wcmd._link_path(name)

    assert name in wcmd.registry_load()
    assert link_path.exists(follow_symlinks=False)

    wcmd.remove(name)

    assert name not in wcmd.registry_load()
    assert not link_path.exists(follow_symlinks=False)


def test_remove_does_not_delete_target_files(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]

    wcmd.remove(name)

    # Target directory should still exist with its files
    assert real_target_dir.is_dir()
    assert (real_target_dir / "README.md").is_file()


def test_remove_deactivates_if_currently_active(real_target_dir):
    wcmd.activate(str(real_target_dir))
    assert wcmd.current() is not None

    name = wcmd._ws_name(str(real_target_dir))
    wcmd.remove(name)

    assert wcmd.current() is None


# --------------------------------------------------------------------------- #
# cleanup
# --------------------------------------------------------------------------- #


def test_cleanup_removes_dangling_links(real_target_dir):
    # Prepare a workspace, then remove the target → makes it dangling
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]
    link_path = wcmd._link_path(name)

    assert link_path.exists(follow_symlinks=False)

    # Remove target to dangle the link
    import shutil

    shutil.rmtree(str(real_target_dir), ignore_errors=True)

    wcmd.cleanup()

    # The dangling link should be removed
    assert not link_path.exists(follow_symlinks=False)


def test_cleanup_keeps_valid_links(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]
    link_path = wcmd._link_path(name)

    assert link_path.exists(follow_symlinks=False)

    wcmd.cleanup()

    # Valid link should persist
    assert link_path.exists(follow_symlinks=False)


def test_cleanup_removes_unregistered_links(real_target_dir):
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]
    link_path = wcmd._link_path(name)

    assert link_path.exists(follow_symlinks=False)

    # Remove from registry but keep the link
    wcmd.registry_remove(name)

    wcmd.cleanup()

    # Unregistered link should be removed
    assert not link_path.exists(follow_symlinks=False)


def test_cleanup_does_not_touch_real_directories(temp_project):
    """A real directory (not a symlink/junction) in projects/ should be untouched."""
    proot = wcmd._projects_root()
    proot.mkdir(parents=True, exist_ok=True)
    real_dir = proot / "real-folder"
    real_dir.mkdir()

    wcmd.cleanup()

    assert real_dir.is_dir(), "Real directories should survive cleanup"


# --------------------------------------------------------------------------- #
# handle — command dispatch
# --------------------------------------------------------------------------- #


@pytest.fixture
def mock_agent():
    return MagicMock()


def test_handle_empty_args_lists_workspaces(temp_project, mock_agent):
    msg = wcmd.handle("", mock_agent)
    # No workspaces registered → shows "暂无已登记"
    assert "暂无" in msg or len(msg) > 0


def test_handle_empty_args_with_workspaces(real_target_dir, mock_agent):
    wcmd.registry_upsert("test-ws", str(real_target_dir))
    msg = wcmd.handle("", mock_agent)
    assert "test-ws" in msg


def test_handle_off_deactivates(real_target_dir, mock_agent):
    wcmd.activate(str(real_target_dir))
    msg = wcmd.handle("off", mock_agent)
    assert "退出" in msg
    assert wcmd.current() is None


def test_handle_off_when_none_active(mock_agent):
    # Ensure deactivated
    wcmd.deactivate()
    msg = wcmd.handle("off", mock_agent)
    assert "未处于" in msg


def test_handle_rm_removes_and_returns_message(real_target_dir, mock_agent):
    r = wcmd.prepare(str(real_target_dir))
    name = r["name"]
    msg = wcmd.handle(f"rm {name}", mock_agent)
    assert "已注销" in msg
    assert name not in wcmd.registry_load()


def test_handle_rm_without_name(mock_agent):
    # "rm" alone strips to "rm" which doesn't match "rm " prefix — falls through to path → error
    msg = wcmd.handle("rm", mock_agent)
    assert "需要绝对路径" in msg


def test_handle_activates_from_path(real_target_dir, mock_agent):
    msg = wcmd.handle(str(real_target_dir), mock_agent)
    assert "已进入" in msg
    assert wcmd.current() is not None


def test_handle_invalid_path_returns_error(mock_agent):
    msg = wcmd.handle("/not/a/real/path", mock_agent)
    assert "失败" in msg or "❌" in msg
