"""git 元数据写保护：safety 硬拒绝 + sandbox profile 的 deny 规则.

两条防线各管一段：
- safety 层（工具调用前）拦 ``.git/hooks/**`` 与 ``.git/config``，防止植入
  持久化后门或改 ``core.hooksPath``。
- sandbox 层（进程级）把 ``.git`` 并入可写根，让 ``git add/commit`` 能写
  index/objects，同时在 profile 末尾 deny 掉同样的敏感条目兜底。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_safety_floor import _config
from zero_agent.core import safety
from zero_agent.extensions import sandbox


@pytest.mark.parametrize(
    "rel",
    [
        ".git/hooks",
        ".git/hooks/pre-commit",
        ".git/config",
    ],
)
def test_git_sensitive_entries_denied(tmp_path: Path, rel: str) -> None:
    """hooks / config 是唯一能植入持久化行为的入口，必须硬拒绝."""
    config = _config(tmp_path)
    target = Path(config.workspace_dir) / "repo" / rel

    denial = safety.write_denial(str(target), config)

    assert denial != ""
    assert ".git" in denial


@pytest.mark.parametrize(
    "rel",
    [
        ".git/index",
        ".git/HEAD",
        ".git/COMMIT_EDITMSG",
        ".git/refs/heads/main",
        ".git/objects/ab/cdef0123",
        ".git/logs/HEAD",
    ],
)
def test_git_normal_operation_paths_allowed(tmp_path: Path, rel: str) -> None:
    """正常 git 操作要写的位置不能被自己拦死，否则 code_run 里跑不了 git."""
    config = _config(tmp_path)
    target = Path(config.workspace_dir) / "repo" / rel

    assert safety.write_denial(str(target), config) == ""


def test_git_metadata_dir_found_upwards(tmp_path: Path) -> None:
    """从子目录向上应能找到仓库的 .git，供 sandbox 并入可写根."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    nested = repo / "a" / "b"
    nested.mkdir(parents=True)

    found = sandbox._git_metadata_dir(str(nested))

    assert found is not None
    assert Path(found).name == ".git"
    assert Path(found).parent == repo


def test_git_metadata_dir_absent_outside_repo(tmp_path: Path) -> None:
    """非 git 目录不应误判，避免把无关目录提权成可写根."""
    plain = tmp_path / "plain"
    nested = plain / "x"
    nested.mkdir(parents=True)

    assert sandbox._git_metadata_dir(str(nested)) is None


def test_macos_profile_denies_git_metadata(tmp_path: Path) -> None:
    """workspace 在 git 仓库内时，profile 必须 allow .git 又 deny 敏感条目."""
    repo = tmp_path / "repo"
    (repo / ".git" / "hooks").mkdir(parents=True)
    work = repo / "work"
    work.mkdir()

    profile = sandbox._macos_profile(str(work))
    git_dir = repo / ".git"

    # .git 整体可写（git add/commit 需要）
    assert str(git_dir) in profile
    # 但敏感条目在 allow 之后被 deny 覆盖（sandbox 规则后写优先）
    assert "(deny file-write*" in profile
    deny_block = profile[profile.rindex("(deny file-write*") :]
    assert str(git_dir / "hooks") in deny_block
    assert str(git_dir / "config") in deny_block


def test_macos_profile_plain_dir_has_no_git_deny(tmp_path: Path) -> None:
    """非仓库目录不应产生 .git 相关规则."""
    plain = tmp_path / "plain"
    plain.mkdir()

    profile = sandbox._macos_profile(str(plain))

    assert ".git" not in profile
