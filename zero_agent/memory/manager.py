"""记忆系统管理.

MemoryManager: 管理文件式分层记忆（L0-L4），提供系统提示词增强。
记忆分层:
    L0 (META-SOP): sops/memory_management_sop.md — 记忆更新的元规则.
    L1 (Insight): global_mem_insight.txt — 极简索引，L2/L3 变更时同步.
    L2 (Facts): global_mem.txt — 验证过的环境事实（路径/凭证/配置）.
    L3 (SOPs): sops/*.md — 标准操作流程.
    L4 (Archive): L4_raw_sessions/ — 历史会话存档.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional

from zero_agent.core.exceptions import ConfigError


_ASSETS_DIR = Path(__file__).resolve().parents[1] / "assets"
_SOP_SEED_DIR = _ASSETS_DIR / "memory_seed" / "sops"


def _asset_text(name: str) -> str:
    try:
        return (_ASSETS_DIR / name).read_text(encoding="utf-8")
    except OSError:
        return ""


class MemoryManager:
    """记忆系统管理器.

    管理文件式分层记忆目录，提供初始化、系统提示词增强等功能。
    不参与记忆内容的实际读写——这些由 LLM 通过 file_read/file_patch 工具完成。

    Attributes:
        memory_dir: 记忆文件存储目录的绝对路径.
        workspace_dir: 工作目录的绝对路径.
    """

    def __init__(
        self,
        memory_dir: str = "./memory",
        workspace_dir: str = "./workspace",
        language: str | None = None,
    ) -> None:
        """初始化 MemoryManager.

        Args:
            memory_dir: 记忆文件存储目录.
            workspace_dir: 工作目录.
            language: 记忆上下文语言 ("zh" 或 "en").
        """
        self.memory_dir = os.path.abspath(memory_dir)
        self.workspace_dir = os.path.abspath(workspace_dir)
        self.language = "en" if language == "en" else "zh"

    def init_memory(self) -> None:
        """初始化记忆目录和默认文件.

        创建 memory_dir 及 L1/L2 默认文件（仅当文件不存在时）。
        已有文件不会被覆盖。
        SOP 文件只从 memory_dir/sops 读取；不维护第二套 package fallback。
        """
        os.makedirs(self.memory_dir, exist_ok=True)

        # L2: 验证事实存储
        l2_path = os.path.join(self.memory_dir, "global_mem.txt")
        if not os.path.exists(l2_path):
            with open(l2_path, "w", encoding="utf-8") as f:
                f.write("# [Global Memory - L2]\n")

        # L1: 极简索引
        l1_path = os.path.join(self.memory_dir, "global_mem_insight.txt")
        if not os.path.exists(l1_path):
            template = _asset_text(
                f"global_mem_insight_template{self._lang_suffix()}.txt"
            )
            with open(l1_path, "w", encoding="utf-8") as f:
                f.write(template)

        # L4: 历史会话存档目录
        l4_dir = os.path.join(self.memory_dir, "L4_raw_sessions")
        os.makedirs(l4_dir, exist_ok=True)
        self._seed_sops()

    def get_global_memory_context(self) -> str:
        """构建系统提示词中的记忆上下文部分.

        读取 L1 索引文件，与记忆结构说明模板组合，
        生成注入到系统提示词中的记忆上下文。

        Returns:
            记忆上下文字符串，可直接拼接到系统提示词末尾.
            若 L1 文件不存在则返回仅含结构说明的字符串.
        """
        prompt = "\n"
        prompt += f"cwd = {self.workspace_dir} (./)\n"
        prompt += f"\n[Memory] ({self.memory_dir})\n"
        prompt += _asset_text(f"insight_fixed_structure{self._lang_suffix()}.txt")
        l1_path = os.path.join(self.memory_dir, "global_mem_insight.txt")
        prompt += f"\n{l1_path}:\n"

        # L1 索引内容
        l1_path = os.path.join(self.memory_dir, "global_mem_insight.txt")
        if os.path.exists(l1_path):
            try:
                with open(l1_path, "r", encoding="utf-8", errors="replace") as f:
                    prompt += f.read()
                prompt += "\n"
            except (OSError, UnicodeDecodeError):
                pass

        return prompt

    def _lang_suffix(self) -> str:
        return "_en" if self.language == "en" else ""

    def _seed_sops(self) -> None:
        """Copy bundled SOP seeds into memory_dir/sops without overwriting."""
        if not _SOP_SEED_DIR.is_dir():
            raise ConfigError(f"Memory SOP seed assets are required: {_SOP_SEED_DIR}")

        target_root = Path(self.memory_dir) / "sops"
        target_root.mkdir(parents=True, exist_ok=True)
        for src in _SOP_SEED_DIR.rglob("*"):
            rel = src.relative_to(_SOP_SEED_DIR)
            dst = target_root / rel
            if src.is_dir():
                dst.mkdir(parents=True, exist_ok=True)
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if not dst.exists():
                shutil.copy2(src, dst)

    def get_sop_path(self, sop_name: str) -> Optional[str]:
        """查找 SOP 文件的完整路径.

        只在 memory_dir/sops 下查找，支持 .md 扩展名。

        Args:
            sop_name: SOP 名称（不含扩展名或含扩展名）.

        Returns:
            文件路径，未找到时返回 None.
        """
        sops_dir = os.path.join(self.memory_dir, "sops")
        direct = os.path.join(sops_dir, sop_name)
        if os.path.isfile(direct):
            return direct
        candidate = os.path.join(sops_dir, f"{sop_name}.md")
        if os.path.isfile(candidate):
            return candidate
        return None

    def list_sops(self) -> list[str]:
        """列出 memory_dir/sops 下所有 SOP 文件（.md）.

        Returns:
            SOP 文件名列表（不含路径）.
        """
        sops: list[str] = []
        sops_dir = os.path.join(self.memory_dir, "sops")
        if not os.path.isdir(sops_dir):
            return sops
        for entry in os.listdir(sops_dir):
            if entry.endswith(".md") or os.path.isdir(os.path.join(sops_dir, entry)):
                sops.append(entry)
        return sorted(sops)
