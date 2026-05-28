"""记忆系统管理.

MemoryManager: 管理文件式分层记忆（L0-L4），提供系统提示词增强。
记忆分层:
    L0 (META-SOP): memory_management_sop.md — 记忆更新的元规则.
    L1 (Insight): global_mem_insight.txt — 极简索引，L2/L3 变更时同步.
    L2 (Facts): global_mem.txt — 验证过的环境事实（路径/凭证/配置）.
    L3 (SOPs): *.md / *.py — 标准操作流程和工具代码.
    L4 (Archive): L4_raw_sessions/ — 历史会话存档.
"""

from __future__ import annotations

import os
from typing import Optional


# 记忆结构说明（注入系统提示词，告知 LLM 记忆层次）
_MEMORY_STRUCTURE_TEMPLATE = """Facts(L2): {memory_dir}/global_mem.txt | SOPs(L3): {memory_dir}/*.md or *.py | META-SOP(L0): {memory_dir}/memory_management_sop.md
L1 Insight是极简索引，L2/L3变更时同步L1，索引必须极简。写记忆前先读META-SOP(L0)。

[CONSTITUTION]
1. 改自身源码先请示；./内可自主实验
2. 决策前查记忆，有SOP/utils必用；多次失败回看SOP；未查证不断言
3. 分步执行，控制粒度，限制失败半径；3次失败请求干预
4. 写任何记忆前读META-SOP核验，memory下文件只能patch修改（除非新建）
5. 密钥/凭证文件(.env,config.yaml,keychain等)仅引用路径，禁止读取内容或移动
6. 安装新Python包前需确认必要性，优先使用标准库和已有依赖"""



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
    ) -> None:
        """初始化 MemoryManager.

        Args:
            memory_dir: 记忆文件存储目录.
            workspace_dir: 工作目录.
        """
        self.memory_dir = os.path.abspath(memory_dir)
        self.workspace_dir = os.path.abspath(workspace_dir)
        # package 内置 SOP 目录
        self._pkg_sops_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "sops"
        )

    def init_memory(self) -> None:
        """初始化记忆目录和默认文件.

        创建 memory_dir 及 L1/L2 默认文件（仅当文件不存在时）。
        已有文件不会被覆盖。
        同时将 package 内置 SOP 文件复制到用户 memory 目录 (仅当不存在时)。
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
            with open(l1_path, "w", encoding="utf-8") as f:
                f.write(
                    "# [Global Memory Insight]\n"
                    "需要时read L2 或 ls ../memory/ 查L3\n"
                    "L0(META-SOP): memory_management_sop\n"
                    "L2: 现空\n"
                    "L3: (暂无)\n"
                    "L4: L4_raw_sessions/ 历史会话\n"
                )

        # L4: 历史会话存档目录
        l4_dir = os.path.join(self.memory_dir, "L4_raw_sessions")
        os.makedirs(l4_dir, exist_ok=True)

        # 从 package 复制默认 SOP 文件 (L3 及 L0)
        self._copy_default_sops()

    def get_global_memory_context(self) -> str:
        """构建系统提示词中的记忆上下文部分.

        读取 L1 索引文件，与记忆结构说明模板组合，
        生成注入到系统提示词中的记忆上下文。

        Returns:
            记忆上下文字符串，可直接拼接到系统提示词末尾.
            若 L1 文件不存在则返回仅含结构说明的字符串.
        """
        parts: list[str] = []

        # 工作目录信息
        parts.append(f"cwd = {self.workspace_dir}")

        # 记忆结构说明
        rel_memory = self.memory_dir
        parts.append(
            _MEMORY_STRUCTURE_TEMPLATE.format(memory_dir=rel_memory)
        )

        # L1 索引内容
        l1_path = os.path.join(self.memory_dir, "global_mem_insight.txt")
        if os.path.exists(l1_path):
            try:
                with open(l1_path, "r", encoding="utf-8", errors="replace") as f:
                    insight = f.read()
                parts.append(f"{rel_memory}/global_mem_insight.txt:\n{insight}")
            except (OSError, UnicodeDecodeError):
                pass

        return "\n".join(parts)

    def get_sop_path(self, sop_name: str) -> Optional[str]:
        """查找 SOP 文件的完整路径.

        先在 memory_dir 下查找，然后回退到 package 内置 sops 目录。
        支持 .md 和 .py 扩展名。

        Args:
            sop_name: SOP 名称（不含扩展名或含扩展名）.

        Returns:
            文件路径，未找到时返回 None.
        """
        for search_dir in (self.memory_dir, self._pkg_sops_dir):
            direct = os.path.join(search_dir, sop_name)
            if os.path.isfile(direct):
                return direct
            for ext in (".md", ".py"):
                candidate = os.path.join(search_dir, f"{sop_name}{ext}")
                if os.path.isfile(candidate):
                    return candidate
        return None

    def list_sops(self) -> list[str]:
        """列出 memory_dir 下所有 SOP 文件（.md / .py）.

        Returns:
            SOP 文件名列表（不含路径）.
        """
        sops: list[str] = []
        if not os.path.isdir(self.memory_dir):
            return sops
        for entry in os.listdir(self.memory_dir):
            if entry.endswith((".md", ".py")) or os.path.isdir(
                os.path.join(self.memory_dir, entry)
            ):
                sops.append(entry)
        return sorted(sops)

    def _copy_default_sops(self) -> None:
        """从 package 内置 sops 目录复制默认 SOP 文件到用户 memory 目录.

        仅复制用户目录中不存在的文件，已有文件不会被覆盖。
        同时递归复制子目录。
        """
        import shutil

        if not os.path.isdir(self._pkg_sops_dir):
            return
        for name in os.listdir(self._pkg_sops_dir):
            src = os.path.join(self._pkg_sops_dir, name)
            dst = os.path.join(self.memory_dir, name)
            if os.path.isdir(src):
                if not os.path.exists(dst):
                    shutil.copytree(src, dst)
            else:
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
