"""Skill 索引扩展：发现外部 skill 目录，注入描述并按需加载正文.

约定与 ``~/.dsh/skills`` 完全同构：

- ``~/.zeroagent/skills/<name>/SKILL.md``
- ``SKILL.md`` 顶部可用 YAML frontmatter 声明 ``name`` 与 ``description``

提示词只注入"名字 + 一句话描述"，正文由模型按需调用 ``skill_load`` 读取。
SOP 位于 ``memory/sops/``，优先级高于 skill；本扩展不读取也不修改 SOP。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

_MAX_DESCRIPTION_CHARS = 160
_MAX_INDEX_ENTRIES = 60
_MAX_BODY_CHARS = 20000

# 元信息缓存：键为 SKILL.md 路径，值为 (mtime_ns, size, (名称, 描述, 正文))。
# 提示词每轮都会重新渲染索引，缓存避免反复读盘；文件变化后签名失效即重读。
_META_CACHE: Dict[str, Tuple[int, int, Tuple[str, str, str]]] = {}


def _skills_root(runtime: Any) -> Optional[Path]:
    """返回 skill 根目录；未配置时返回 None."""
    home = getattr(runtime, "home", None)
    if home is None:
        return None
    return Path(home) / "skills"


def iter_skills(root: Optional[Path]) -> Iterator[Tuple[str, Path]]:
    """遍历 skill 目录.

    Args:
        root: skill 根目录；None 时返回空迭代.

    Yields:
        ``(目录名, SKILL.md 路径)``.
    """
    if root is None or not root.is_dir():
        return
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.startswith(("_", ".")):
            continue
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.is_file():
            yield entry.name, skill_md


def split_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    """拆分 YAML frontmatter 与正文.

    Args:
        text: SKILL.md 全文.

    Returns:
        ``(元数据字典, 正文)``；没有 frontmatter 时元数据为空.
    """
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    header = text[3:end]
    body = text[end + 4 :]
    try:
        import yaml

        data = yaml.safe_load(header) or {}
    except Exception:  # noqa: BLE001 - frontmatter 损坏时按无元数据处理
        return {}, text
    if not isinstance(data, dict):
        return {}, text
    return data, body


def _read_meta(name: str, path: Path) -> Tuple[str, str, str]:
    """读取一个 skill 的元信息与正文.

    Args:
        name: 目录名（兜底技能名）.
        path: SKILL.md 路径.

    Returns:
        ``(技能名, 描述, 正文)``.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    data, body = split_frontmatter(text)
    skill_name = str(data.get("name") or name).strip() or name
    description = str(data.get("description") or "").strip()
    return skill_name, description, body.strip()


def _read_meta_cached(name: str, path: Path) -> Tuple[str, str, str]:
    """按文件签名缓存读取 skill 元信息与正文.

    Args:
        name: 目录名（兜底技能名）.
        path: SKILL.md 路径.

    Returns:
        ``(技能名, 描述, 正文)``.
    """
    try:
        stat = path.stat()
    except OSError:
        return _read_meta(name, path)
    key = str(path)
    cached = _META_CACHE.get(key)
    if (
        cached is not None
        and cached[0] == stat.st_mtime_ns
        and cached[1] == stat.st_size
    ):
        return cached[2]
    meta = _read_meta(name, path)
    _META_CACHE[key] = (stat.st_mtime_ns, stat.st_size, meta)
    return meta


def setup(ctx: Any) -> None:
    """安装 skill 索引扩展.

    Args:
        ctx: 插件上下文（ExtensionContext）.
    """
    root = _skills_root(ctx.runtime)

    ctx.prompt_block("skills_index", lambda: _render_index(root))

    @ctx.tool(
        name="skill_load",
        description=(
            "读取一个技能的完整说明。提示词里只有技能名和一句话描述，"
            "需要具体步骤时调用本工具。SOP（memory/sops）优先于技能。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "技能名，来自提示词中的技能索引",
                },
            },
            "required": ["name"],
        },
        evidence_kind="read",
        category="extension",
        promotes_task_state=False,
    )
    def skill_load(args: Dict[str, Any]) -> str:
        """读取技能正文.

        Args:
            args: 含 ``name`` 的参数字典.

        Returns:
            技能正文，或未找到时的提示文本.
        """
        requested = str(args.get("name") or "").strip()
        if not requested:
            return "缺少参数 name。"
        if root is None:
            return "未配置 skill 目录。"
        for dir_name, skill_md in iter_skills(root):
            skill_name, _description, body = _read_meta_cached(dir_name, skill_md)
            if requested in (skill_name, dir_name):
                if len(body) > _MAX_BODY_CHARS:
                    body = body[:_MAX_BODY_CHARS] + "\n\n…（已截断）"
                return f"# Skill: {skill_name}\n\n{body}"
        available = [name for name, _path in iter_skills(root)]
        return (
            f"未找到技能 {requested!r}。可用技能: "
            + ", ".join(available[:20])
        )


def _render_index(root: Optional[Path]) -> str:
    """渲染技能索引提示词块.

    Args:
        root: skill 根目录.

    Returns:
        提示词文本；没有技能时返回空串.
    """
    if root is None or not root.is_dir():
        return ""
    entries: List[str] = []
    for dir_name, skill_md in iter_skills(root):
        try:
            skill_name, description, _body = _read_meta_cached(dir_name, skill_md)
        except Exception:  # noqa: BLE001 - 单个条目失败不影响索引
            continue
        if len(description) > _MAX_DESCRIPTION_CHARS:
            description = description[:_MAX_DESCRIPTION_CHARS].rstrip() + "…"
        entries.append(f"  {skill_name} — {description}" if description else f"  {skill_name}")
        if len(entries) >= _MAX_INDEX_ENTRIES:
            break
    if not entries:
        return ""
    return (
        "[Skills · 参考] 可用技能（与 memory/sops 中的 SOP 冲突时以 SOP 为准；"
        "需要具体步骤时调用 skill_load 读取正文）：\n"
        + "\n".join(entries)
    )
