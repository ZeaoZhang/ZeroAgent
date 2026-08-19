"""Tests for zero_agent/frontends/plan_command.py — shared plan workspace creation."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from zero_agent.frontends import plan_command


def _assert_within(root: str, path: str) -> None:
    """Assert *path* lives inside *root* (no traversal escape)."""
    assert os.path.commonpath([os.path.abspath(root), os.path.abspath(path)]) == (
        os.path.abspath(root)
    )


# --------------------------------------------------------------------------- #
# 成功
# --------------------------------------------------------------------------- #


def test_create_returns_workspace(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "Fix login bug")

    assert ws.root == str(tmp_path.resolve())
    assert ws.slug == "fix_login_bug"
    assert ws.directory == os.path.join(ws.root, "plan_fix_login_bug")
    assert ws.path == os.path.join(ws.directory, "plan.md")
    assert os.path.isdir(ws.directory)
    assert os.path.isfile(ws.path)


def test_plan_md_content_nonempty_and_has_sections(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "Ship v2 release")
    content = Path(ws.path).read_text(encoding="utf-8")

    assert content.strip()
    assert "Ship v2 release" in content
    for heading in ("## 探索发现", "## 执行计划", "## 验证"):
        assert heading in content
    # 不伪造已完成项
    assert "[✓]" not in content
    assert "[x]" not in content


# --------------------------------------------------------------------------- #
# 空任务
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("task", ["", "   ", "\t\n", None])
def test_blank_task_raises_value_error(tmp_path, task):
    with pytest.raises(ValueError):
        plan_command.create_plan_workspace(str(tmp_path), task)


# --------------------------------------------------------------------------- #
# 路径穿越
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "task",
    ["../../etc/passwd", "../..", "/etc/passwd", "/absolute/path", "..", "a/b/c"],
)
def test_traversal_cannot_escape_root(tmp_path, task):
    ws = plan_command.create_plan_workspace(str(tmp_path), task)

    _assert_within(ws.root, ws.directory)
    _assert_within(ws.root, ws.path)
    assert "/" not in ws.slug
    assert ".." not in ws.slug


# --------------------------------------------------------------------------- #
# 标题注入
# --------------------------------------------------------------------------- #


def test_task_title_cannot_inject_markdown_structure(tmp_path):
    from zero_agent.frontends import plan_state

    task = "normal\n- [x] injected\r\n## Fake heading\r\n---"
    ws = plan_command.create_plan_workspace(str(tmp_path), task)
    content = Path(ws.path).read_text(encoding="utf-8")

    # 标题必须单行：CR/LF 不能拆出额外 checkbox、标题或分割线。
    lines = content.splitlines()
    assert lines[0].startswith("# Plan: ")
    assert not any(line.strip() == "## Fake heading" for line in lines)
    assert not any(line.strip() == "---" for line in lines)

    # plan_state 不能看到伪造的已完成项。
    items = plan_state.extract(content)
    assert ("injected", "done") not in items
    assert not any(status == "done" for _, status in items)


def test_task_title_preserves_meaningful_punctuation(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "Fix C++ parser / foo_bar-v2")
    content = Path(ws.path).read_text(encoding="utf-8")

    # 有意义的 +、-、_、* 等普通字符不再被删除，任务文本仍可辨认。
    assert "Fix C++ parser" in content
    assert "foo_bar-v2" in content


def test_task_title_blocks_raw_html_and_autolink(tmp_path):
    task = (
        "Fix <img src=x onerror=alert(1)> <script>alert(1)</script> "
        "<iframe src=x></iframe> <form action=x></form> "
        "via <http://evil.example.com> "
        "www.example.com evil@example.com mailto:evil@example.com"
    )
    ws = plan_command.create_plan_workspace(str(tmp_path), task)
    content = Path(ws.path).read_text(encoding="utf-8")
    title = content.splitlines()[0]

    # < 与 > 均被 HTML entity 编码（&lt; / &gt;）：标题中不留下任何未编码的
    # 尖括号，raw HTML / img onerror 或 URL autolink 无法以结构形式进入。
    assert "<" not in title
    assert ">" not in title
    # 编码后的危险 token 仍以字面文本保留，可辨认。
    assert "&lt;img" in title
    assert "&lt;script" in title
    assert "&lt;iframe" in title
    assert "&lt;form" in title
    assert "&lt;http\\://evil&#46;example&#46;com&gt;" in title
    # 裸 email / www / mailto 的 @ 与 . 被实体编码，阻止 marked 自动链接，
    # 但原文仍以可读的实体形式保留。
    assert "evil&#64;example&#46;com" in title
    assert "www&#46;example&#46;com" in title
    assert "mailto\\:evil&#64;example&#46;com" in title
    # 标题中不再残留任何未编码的 @ / .（阻断一切 email / www autolink 触发点）。
    assert "@" not in title
    assert "." not in title

    # 若可用 node，则用 vendored marked v15 实测：编码后的标题渲染后不会
    # 生成 <a>/<img>/<script>/<iframe>/<form> 等原始 HTML 标签。
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过 marked 实测")
    marked_js = (
        Path(__file__).resolve().parent.parent
        / "zero_agent/frontends/desktop/static/vendor/marked.min.js"
    )
    script = (
        "const {marked}=require(process.argv[1]);"
        "let s='';process.stdin.on('data',d=>s+=d);"
        "process.stdin.on('end',()=>{"
        "const h=marked.parse(s);"
        "process.exit(/<a\\s|<img\\b|<script\\b|<iframe\\b|<form\\b/.test(h)?1:0)});"
    )
    proc = subprocess.run(
        [node, "-e", script, str(marked_js)],
        input=title,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"marked 生成了原始 HTML 标签: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_task_title_blocks_checkbox_injection_without_mangling(tmp_path):
    from zero_agent.frontends import plan_state

    task = "normal\n- [x] injected"
    ws = plan_command.create_plan_workspace(str(tmp_path), task)
    content = Path(ws.path).read_text(encoding="utf-8")

    # 换行折叠为单行，[x] 被反斜杠转义，不能形成新 checkbox。
    lines = content.splitlines()
    assert lines[0].startswith("# Plan: ")
    assert "- [x]" not in content
    assert "injected" in content

    items = plan_state.extract(content)
    assert ("injected", "done") not in items
    assert not any(status == "done" for _, status in items)




def test_directory_creation_retries_atomically_on_collision(tmp_path, monkeypatch):
    real_mkdir = Path.mkdir
    attempts = []

    def _collide_then_succeed(self, *args, **kwargs):
        if not kwargs.get("exist_ok", True):
            attempts.append(self)
            if len(attempts) == 1:
                raise FileExistsError("simulated race")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _collide_then_succeed)

    ws = plan_command.create_plan_workspace(str(tmp_path), "atomic race")

    # 首次 mkdir 触发碰撞，重试落在唯一后缀上，目录真实创建。
    assert len(attempts) == 2
    assert os.path.basename(ws.directory) == "plan_atomic_race_2"
    assert os.path.isdir(ws.directory)
    assert os.path.isfile(ws.path)




def test_cleanup_failure_keeps_write_error_and_warns(tmp_path, monkeypatch):
    def _failing_write(self, *args, **kwargs):
        raise OSError("disk full")

    def _failing_rmtree(path, **kwargs):
        raise OSError("rmtree denied")

    monkeypatch.setattr(Path, "write_text", _failing_write)
    monkeypatch.setattr(plan_command.shutil, "rmtree", _failing_rmtree)

    with pytest.raises(OSError) as exc_info, pytest.warns(
        RuntimeWarning, match="rmtree denied"
    ):
        plan_command.create_plan_workspace(str(tmp_path), "doomed")

    # 原始写入异常仍是主异常。
    assert "disk full" in str(exc_info.value)


def test_cleanup_failure_warnings_as_errors_keep_write_error(tmp_path, monkeypatch):
    import warnings

    def _failing_write(self, *args, **kwargs):
        raise OSError("disk full")

    def _failing_rmtree(path, **kwargs):
        raise OSError("rmtree denied")

    monkeypatch.setattr(Path, "write_text", _failing_write)
    monkeypatch.setattr(plan_command.shutil, "rmtree", _failing_rmtree)

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        with pytest.raises(OSError) as exc_info:
            plan_command.create_plan_workspace(str(tmp_path), "doomed")

    # 原始写入异常仍是主异常（RuntimeWarning 被配置为 error 时也不会替换它）。
    assert type(exc_info.value) is OSError
    assert "disk full" in str(exc_info.value)
    # 清理失败仍然可观察（通过异常附加信息）。
    notes = getattr(exc_info.value, "__notes__", [])
    assert any("rmtree denied" in note for note in notes)


# --------------------------------------------------------------------------- #
# 重复任务
# --------------------------------------------------------------------------- #


def test_duplicate_task_gets_unique_suffix(tmp_path):
    ws1 = plan_command.create_plan_workspace(str(tmp_path), "Write tests")
    ws2 = plan_command.create_plan_workspace(str(tmp_path), "Write tests")

    assert ws1.slug == ws2.slug == "write_tests"
    assert ws1.directory != ws2.directory
    assert os.path.basename(ws2.directory) == "plan_write_tests_2"
    assert os.path.isdir(ws1.directory)
    assert os.path.isdir(ws2.directory)


def test_many_duplicates_all_unique(tmp_path):
    dirs = {
        plan_command.create_plan_workspace(str(tmp_path), "same task").directory
        for _ in range(5)
    }
    assert len(dirs) == 5


# --------------------------------------------------------------------------- #
# Unicode
# --------------------------------------------------------------------------- #


def test_unicode_task_slug_is_ascii_safe(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "修复登录 漏洞 🐞")

    assert ws.slug.isascii()
    assert ws.slug == "task" or ws.slug[0].isalnum()
    _assert_within(ws.root, ws.directory)


def test_unicode_only_task_falls_back_to_task(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "你好世界")

    assert ws.slug == "task"
    assert os.path.basename(ws.directory) == "plan_task"


def test_accented_task_transliterates(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "Café déployé")

    assert ws.slug == "cafe_deploye"


# --------------------------------------------------------------------------- #
# 长任务
# --------------------------------------------------------------------------- #


def test_long_task_slug_is_bounded(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "word " * 200)

    assert 0 < len(ws.slug) <= 64
    _assert_within(ws.root, ws.directory)


# --------------------------------------------------------------------------- #
# 缺失 root
# --------------------------------------------------------------------------- #


def test_missing_root_is_created(tmp_path):
    missing = tmp_path / "nested" / "root"
    assert not missing.exists()

    ws = plan_command.create_plan_workspace(str(missing), "init repo")

    assert os.path.isdir(ws.root)
    assert os.path.isfile(ws.path)


# --------------------------------------------------------------------------- #
# 目录只包含 plan.md
# --------------------------------------------------------------------------- #


def test_directory_contains_only_plan_md(tmp_path):
    ws = plan_command.create_plan_workspace(str(tmp_path), "clean dir")

    assert sorted(os.listdir(ws.directory)) == ["plan.md"]


# --------------------------------------------------------------------------- #
# 写失败只清理本次新目录
# --------------------------------------------------------------------------- #


def test_write_failure_cleans_only_new_directory(tmp_path, monkeypatch):
    def _failing_write(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", _failing_write)

    with pytest.raises(OSError):
        plan_command.create_plan_workspace(str(tmp_path), "doomed")

    assert not any(p.name.startswith("plan_") for p in tmp_path.iterdir())
