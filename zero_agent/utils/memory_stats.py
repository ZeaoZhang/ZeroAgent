"""Memory file access statistics — 记忆文件访问统计.

记录记忆文件的访问频次和最后访问日期，输出到 file_access_stats.json。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional


def log_memory_access(path: str, stats_dir: Optional[str] = None) -> None:
    """记录一次记忆文件访问到统计 JSON 文件.

    Record a memory file access to the statistics JSON.
    仅当路径中包含 "memory" 才记录。写入失败静默忽略。

    Args:
        path: 被访问的文件路径.
        stats_dir: 统计文件输出目录，默认为 memory/ 目录.
    """
    if "memory" not in path:
        return

    if stats_dir is None:
        stats_dir = os.path.join(os.getcwd(), "memory")
    stats_file = os.path.join(stats_dir, "file_access_stats.json")

    try:
        with open(stats_file, "r", encoding="utf-8") as f:
            stats: dict = json.load(f)
    except Exception:
        stats = {}

    fname = os.path.basename(path)
    prev = stats.get(fname, {})
    stats[fname] = {
        "count": prev.get("count", 0) + 1,
        "last": datetime.now().strftime("%Y-%m-%d"),
    }

    try:
        os.makedirs(stats_dir, exist_ok=True)
        with open(stats_file, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2, ensure_ascii=False)
    except Exception:
        pass  # 写入失败静默忽略
