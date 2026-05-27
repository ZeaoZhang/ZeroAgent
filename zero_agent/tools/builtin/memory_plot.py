"""memory_plot — 记忆统计可视化工具.

使用 matplotlib 生成记忆访问统计图表.

依赖: pip install matplotlib
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from zero_agent.tools.registry import ToolDefinition


def memory_plot(
    output_path: Optional[str] = None,
    stats_path: Optional[str] = None,
) -> Dict[str, Any]:
    """生成记忆文件访问统计图表.

    Args:
        output_path: 图表输出路径（默认 memory/file_access_stats.png）.
        stats_path: 统计 JSON 路径（默认 memory/file_access_stats.json）.

    Returns:
        {"path": str, "file_count": int, "total_accesses": int}
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return {"error": "matplotlib not installed. Install with: pip install matplotlib"}

    if stats_path is None:
        stats_path = os.path.join("memory", "file_access_stats.json")

    if not os.path.isfile(stats_path):
        return {"error": f"Stats file not found: {stats_path}", "path": ""}

    with open(stats_path, "r", encoding="utf-8") as f:
        stats = json.load(f)

    if not stats:
        return {"error": "Empty stats", "path": ""}

    files = list(stats.keys())
    accesses = [
        stats[f].get("count", 1) if isinstance(stats[f], dict) else 1
        for f in files
    ]

    # 截断显示标签
    labels = [os.path.basename(f)[:30] for f in files]

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5))

    # 柱状图
    colors = plt.cm.Blues([0.3 + 0.7 * (a / max(accesses, default=1))
                            for a in accesses])
    ax1.barh(labels, accesses, color=colors)
    ax1.set_xlabel("Access Count")
    ax1.set_title("Memory File Access Frequency")

    # 饼图（Top 8）
    if len(files) > 8:
        top_indices = sorted(
            range(len(accesses)), key=lambda i: accesses[i], reverse=True
        )[:7]
        other_count = sum(
            a for i, a in enumerate(accesses) if i not in top_indices
        )
        pie_labels = [labels[i] for i in top_indices] + ["Others"]
        pie_values = [accesses[i] for i in top_indices] + [other_count]
    else:
        pie_labels = labels
        pie_values = accesses

    ax2.pie(pie_values, labels=pie_labels, autopct="%1.1f%%",
            colors=plt.cm.Set3(range(len(pie_labels))))
    ax2.set_title("Memory Access Distribution")

    plt.tight_layout()

    if output_path is None:
        output_path = os.path.join("memory", "file_access_stats.png")

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    plt.savefig(output_path, dpi=100, bbox_inches="tight")
    plt.close()

    return {
        "path": os.path.abspath(output_path),
        "file_count": len(files),
        "total_accesses": sum(accesses),
    }


def register_memory_plot_tool(registry: Any) -> None:
    """向 ToolRegistry 注册 memory_plot 工具.

    Args:
        registry: ToolRegistry 实例.
    """
    tool_def = ToolDefinition(
        name="memory_plot",
        description="生成记忆文件访问统计图表。/ Generate memory access statistics chart.",
        parameters={
            "type": "object",
            "properties": {
                "output_path": {
                    "type": "string",
                    "description": "图表输出路径（可选）/ Output path (optional)",
                },
                "stats_path": {
                    "type": "string",
                    "description": "统计 JSON 路径（可选）/ Stats JSON path (optional)",
                },
            },
            "required": [],
        },
        handler=lambda args, resp, hs: memory_plot(
            output_path=args.get("output_path"),
            stats_path=args.get("stats_path"),
        ),
        category="memory",
    )
    registry.register(tool_def)
