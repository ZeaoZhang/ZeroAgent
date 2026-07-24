"""subagent — 本地子 Agent 进程调度.

基于 subprocess 启动子 agent 进程并行执行任务，
复用 --task 文件 I/O 模式传递任务和收集结果.

用法:
    manager = SubAgentManager(max_workers=3)
    manager.submit("task1", "分析 data.csv 并生成报告")
    results = manager.run_all()
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SubAgentTask:
    """子 agent 任务描述.

    Attributes:
        task_id: 任务唯一标识.
        prompt: 任务描述文本.
        io_dir: 任务 I/O 目录.
        status: 当前状态 (pending/running/done/failed).
        result: 子 agent 输出内容.
    """

    task_id: str
    prompt: str
    io_dir: str = ""
    process: Any = None
    status: str = "pending"
    result: str = ""
    error: str = ""


class SubAgentManager:
    """子 Agent 进程管理器.

    负责创建任务目录、启动子进程、监控状态、收集结果.

    Attributes:
        max_workers: 最大并行子进程数.
        tasks: 已提交的任务列表.
        _base_dir: 任务目录的根路径.
    """

    def __init__(
        self,
        max_workers: int = 3,
        base_dir: Optional[str] = None,
    ) -> None:
        """初始化 SubAgentManager.

        Args:
            max_workers: 最大并行子进程数.
            base_dir: 任务 I/O 目录根路径，默认 ./subagent_tasks.
        """
        self.max_workers = max_workers
        self._base_dir = base_dir or os.path.join(
            os.getcwd(), "subagent_tasks"
        )
        self.tasks: List[SubAgentTask] = []

    def submit(self, task_id: str, prompt: str) -> SubAgentTask:
        """提交任务到队列（不立即执行）.

        Args:
            task_id: 任务唯一标识.
            prompt: 任务描述.

        Returns:
            SubAgentTask 实例.
        """
        io_dir = os.path.join(self._base_dir, task_id)
        task = SubAgentTask(task_id=task_id, prompt=prompt, io_dir=io_dir)
        self.tasks.append(task)
        return task

    def run_all(self) -> Dict[str, Any]:
        """执行所有 pending 任务并等待完成.

        最多同时运行 max_workers 个子进程。
        通过轮询 process.poll() 检测进程完成。

        Returns:
            {task_id: {"status": str, "result": str, "error": str}}
        """
        pending = [t for t in self.tasks if t.status == "pending"]
        running: List[SubAgentTask] = []
        results: Dict[str, Any] = {}

        while pending or running:
            # 启动新任务（不超过 max_workers）
            while pending and len(running) < self.max_workers:
                task = pending.pop(0)
                self._launch(task)
                running.append(task)

            # 检查运行中任务
            still_running: List[SubAgentTask] = []
            for task in running:
                if task.process is None:
                    continue
                retcode = task.process.poll()
                if retcode is not None:
                    if retcode == 0:
                        task.status = "done"
                        task.result = self._collect_result(task)
                    else:
                        task.status = "failed"
                        task.error = f"Exit code: {retcode}"
                    results[task.task_id] = {
                        "status": task.status,
                        "result": task.result,
                        "error": task.error,
                    }
                else:
                    still_running.append(task)
            running = still_running

            if running:
                time.sleep(0.5)

        return results

    def _launch(self, task: SubAgentTask) -> None:
        """创建 I/O 目录并启动子进程.

        写入 input.txt (GA 兼容), 使用 --task IODIR --nobg -q 命令.
        如果 zero-agent 可执行文件不存在, 用 python -m zero_agent.runners.cli 回退.

        Args:
            task: 待启动的任务.
        """
        import shutil

        os.makedirs(task.io_dir, exist_ok=True)

        # 写入输入文件 (input.txt, GA 兼容)
        input_path = os.path.join(task.io_dir, "input.txt")
        with open(input_path, "w", encoding="utf-8") as f:
            f.write(task.prompt)

        # 构建命令
        agent_bin = shutil.which("zero-agent")
        if agent_bin:
            cmd = [agent_bin, "--task", task.io_dir, "--nobg", "-q"]
        else:
            cmd = [sys.executable, "-m", "zero_agent.runners.cli",
                   "--task", task.io_dir, "--nobg", "-q"]

        task.process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        task.status = "running"

    @staticmethod
    def _find_agent_bin() -> str:
        """查找 zero-agent 可执行文件.

        优先使用当前 Python 环境中的 zero-agent，
        回退到 python -m zero_agent.runners.cli.

        Returns:
            可执行文件路径或 sys.executable.
        """
        import shutil

        found = shutil.which("zero-agent")
        if found:
            return found
        return sys.executable

    @staticmethod
    def _collect_result(task: SubAgentTask) -> str:
        """读取子 agent 的输出文件.

        优先读 output.txt (GA 兼容), 回退 output.md (旧格式).

        Args:
            task: 已完成的任务.

        Returns:
            输出内容字符串.
        """
        output_txt = os.path.join(task.io_dir, "output.txt")
        if os.path.isfile(output_txt):
            with open(output_txt, "r", encoding="utf-8") as f:
                return f.read()
        output_md = os.path.join(task.io_dir, "output.md")
        if os.path.isfile(output_md):
            with open(output_md, "r", encoding="utf-8") as f:
                return f.read()
        return ""

    def cleanup(self) -> None:
        """清理所有任务目录."""
        import shutil

        for task in self.tasks:
            if os.path.isdir(task.io_dir):
                try:
                    shutil.rmtree(task.io_dir)
                except OSError:
                    pass


def create_subagent_prompt(subtasks: List[Dict[str, str]]) -> str:
    """生成供宿主编排器执行的子任务清单。

    原生工具注册表不暴露 ``subagent_run``。返回文本只用于
    ``SubAgentManager`` 或其他宿主侧 runner 的编排交接，不能让模型
    调用不存在的工具。
    """
    lines = ["[SubAgent] 请由宿主编排器并行执行以下任务:", ""]
    for i, st in enumerate(subtasks, 1):
        lines.append(f"{i}. **{st['id']}**: {st['prompt']}")
    lines.extend(("", f"共 {len(subtasks)} 个子任务。", "宿主完成全部任务后汇总结果。"))
    return "\n".join(lines)
