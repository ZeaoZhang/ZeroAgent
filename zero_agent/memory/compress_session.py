"""L4 会话日志压缩与归档.

将会话日志压缩为简化格式，提取历史摘要，归档到月度 zip.

用法:
    from zero_agent.memory.compress_session import compress_session, batch_process
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import zipfile
from collections import defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple


def compress_session(
    src: str,
    dst_dir: str,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """压缩单个会话日志文件.

    支持两种格式:
        - Format A (JSON): 保留原始 JSON 结构
        - Format B (Raw): 去除系统提示词和助手的 echo 部分

    Args:
        src: 原始日志文件路径.
        dst_dir: 输出目录.

    Returns:
        (dst_path, stats) — dst_path 为 None 表示压缩失败,
        stats 包含 total_lines, compressed_lines, format 等.
    """
    if not os.path.isfile(src):
        return None, {"error": "Source file not found", "src": src}

    with open(src, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    stats: Dict[str, Any] = {
        "src": src,
        "total_bytes": len(content),
    }

    # 检测格式
    first_prompt_idx = content.find("=== Prompt ===")
    if first_prompt_idx >= 0:
        after_prompt = content[first_prompt_idx:]
        # JSON 格式: Prompt 后的内容以 '{' 开头
        try:
            json.loads(after_prompt.split("\n", 1)[1].strip()[:1] or "x")
        except Exception:
            pass
        is_json = after_prompt.strip().startswith("{")
        stats["format"] = "json" if is_json else "raw"
    else:
        stats["format"] = "unknown"

    # 压缩: 去除系统提示词区段和助手回声
    compressed = _strip_system_and_echo(content)
    stats["compressed_bytes"] = len(compressed)

    # 太小则拒绝
    if len(compressed) < 4500:
        return None, {**stats, "error": "Compressed output too small (< 4500 bytes)"}

    # 生成输出文件名: MMDD_HHMM-MMDD_HHMM.txt
    mtime = os.path.getmtime(src)
    dt = datetime.fromtimestamp(mtime)
    dst_name = f"{dt.strftime('%m%d_%H%M')}-{dt.strftime('%m%d_%H%M')}_compressed.txt"
    dst_path = os.path.join(dst_dir, dst_name)

    os.makedirs(dst_dir, exist_ok=True)
    with open(dst_path, "w", encoding="utf-8") as f:
        f.write(compressed)

    return dst_path, stats


def extract_history(src: str, session_name: str = "") -> Tuple[List[str], Dict]:
    """从压缩后的 session 中提取 [USER]/[Agent] 历史行.

    Args:
        src: 压缩后的日志文件路径.
        session_name: 会话名称（用于合并去重）.

    Returns:
        (history_lines, metadata)
    """
    if not os.path.isfile(src):
        return [], {}

    with open(src, "r", encoding="utf-8", errors="replace") as f:
        content = f.read()

    # 提取 <history> 块
    history_pattern = re.compile(r"<history>(.*?)</history>", re.DOTALL)
    lines: list[str] = []
    for match in history_pattern.finditer(content):
        block = match.group(1)
        # 处理 JSON 转义的换行
        block = block.replace("\\n", "\n")
        for line in block.split("\n"):
            line = line.strip()
            if line.startswith("[USER]") or line.startswith("[Agent]"):
                lines.append(line)

    # 去重（基于后缀-前缀重叠检测）
    deduped = _deduplicate_history(lines)

    metadata = {
        "session_name": session_name,
        "total_lines": len(lines),
        "deduped_lines": len(deduped),
    }

    return deduped, metadata


def format_history_block(
    session_name: str,
    history_lines: List[str],
) -> str:
    """格式化历史块，用于追加到 all_histories.txt.

    Args:
        session_name: 会话名称.
        history_lines: 历史行列表.

    Returns:
        格式化的字符串.
    """
    parts = [f"=== {session_name} ==="]
    parts.extend(history_lines)
    parts.append("")
    return "\n".join(parts)


def batch_process(
    src_dir: str,
    l4_dir: str,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """批量压缩、提取、归档流程.

    Args:
        src_dir: 原始日志目录.
        l4_dir: L4 归档目录.
        dry_run: True 时仅预览不执行.

    Returns:
        处理统计字典.
    """
    results: Dict[str, Any] = {
        "processed": 0,
        "skipped": 0,
        "archived": 0,
        "errors": [],
    }

    os.makedirs(l4_dir, exist_ok=True)

    log_pattern = os.path.join(src_dir, "*.log")
    log_files = sorted(glob.glob(log_pattern))

    for log_file in log_files:
        # 跳过 2 小时内修改的文件
        mtime = os.path.getmtime(log_file)
        if datetime.now().timestamp() - mtime < 7200:
            results["skipped"] += 1
            continue

        if dry_run:
            results["processed"] += 1
            continue

        dst_path, stats = compress_session(log_file, l4_dir)
        if dst_path:
            history_lines, _ = extract_history(dst_path, os.path.basename(log_file))
            if history_lines:
                hist_block = format_history_block(
                    os.path.basename(log_file), history_lines
                )
                all_path = os.path.join(l4_dir, "all_histories.txt")
                with open(all_path, "a", encoding="utf-8") as f:
                    f.write(hist_block)

            # 月归档
            _archive_to_monthly(log_file, l4_dir)
            results["archived"] += 1
        else:
            results["skipped"] += 1
            if "error" in stats:
                results["errors"].append(stats["error"])

        results["processed"] += 1

    return results


def _strip_system_and_echo(content: str) -> str:
    """去除系统提示词段和助手回声.

    Args:
        content: 原始日志内容.

    Returns:
        压缩后的内容.
    """
    sections = re.split(r"(=== \w+ ===)", content)
    result_parts: list[str] = []

    skip_next = False
    for i, section in enumerate(sections):
        stripped = section.strip()
        if stripped in ("=== Prompt ===", "=== Response ==="):
            result_parts.append(stripped + "\n")
            skip_next = False
        elif stripped == "=== ASSISTANT ===":
            skip_next = True
        elif skip_next:
            skip_next = False
            continue
        else:
            if not stripped.startswith("==="):
                result_parts.append(section)

    return "".join(result_parts)


def _deduplicate_history(lines: List[str]) -> List[str]:
    """去重历史行（基于内容完全匹配）.

    Args:
        lines: 历史行列表.

    Returns:
        去重后的列表.
    """
    seen: set[str] = set()
    result: list[str] = []
    for line in lines:
        if line not in seen:
            seen.add(line)
            result.append(line)
    return result


def _archive_to_monthly(file_path: str, l4_dir: str) -> None:
    """将日志文件归档到月 zip.

    Args:
        file_path: 源日志文件路径.
        l4_dir: L4 归档根目录.
    """
    mtime = os.path.getmtime(file_path)
    dt = datetime.fromtimestamp(mtime)
    archive_name = f"{dt.strftime('%Y-%m')}.zip"
    archive_path = os.path.join(l4_dir, archive_name)

    mode = "a" if os.path.isfile(archive_path) else "w"
    with zipfile.ZipFile(archive_path, mode, zipfile.ZIP_DEFLATED) as zf:
        arcname = os.path.basename(file_path)
        if arcname not in zf.namelist():
            zf.write(file_path, arcname)
