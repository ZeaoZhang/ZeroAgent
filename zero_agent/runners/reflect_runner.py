"""Reflect runner — 反射式任务调度器.

周期性调用 reflect 模块的 check() 方法，当返回任务字符串时自动唤醒 agent 执行。
支持模块热重载、ONCE 模式、/exit 优雅退出。

Usage:
    from zero_agent.runners.reflect_runner import ReflectRunner
    runner = ReflectRunner(agent, "reflect/autonomous.py")
    runner.run_loop()
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class ReflectRunner:
    """反射式任务运行器.

    包装 ZeroAgent，以固定间隔轮询 reflect 模块的 check() 接口。
    当 check() 返回任务字符串时，启动 agent.run(task) 执行任务，
    完成后根据 ONCE 标志决定是否退出。

    Attributes:
        agent: ZeroAgent 实例 (需要有 run(task) 方法).
        module_path: reflect 模块的文件路径.
        name: reflect 模块名称.
        _module: 当前加载的 reflect 模块对象.
        _mtime: 模块文件的最后修改时间（用于热重载检测）.
        _running: 是否在运行中.
    """

    def __init__(self, agent: Any, module_path: str) -> None:
        """初始化 reflect runner.

        Args:
            agent: ZeroAgent 实例，必须有 run(task) 方法.
            module_path: reflect 模块文件路径，可以是绝对路径或相对于 cwd 的路径.
        """
        self.agent = agent
        self.module_path = os.path.abspath(module_path)
        self.name = os.path.splitext(os.path.basename(self.module_path))[0]
        self._module: Any = None
        self._mtime: float = 0.0
        self._running = False

    def _load_module(self) -> Any:
        """加载或热重载 reflect 模块.

        Returns:
            加载的模块对象.
        """
        if not os.path.isfile(self.module_path):
            raise FileNotFoundError(f"Reflect 模块不存在: {self.module_path}")

        # 将模块所在目录加入 sys.path 以支持用户自定义模块的相对导入.
        # 注意: 这不是为了导入 zero_agent 自身 (zero_agent 已通过 pyproject.toml 安装),
        # 而是为了让用户 reflect 脚本能 import 同目录下的其他模块.
        mod_dir = os.path.dirname(self.module_path)
        if mod_dir not in sys.path:
            sys.path.insert(0, mod_dir)

        if self._module is None:
            spec = importlib.util.spec_from_file_location(
                self.name, self.module_path
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[self.name] = mod  # register for future reload()
            spec.loader.exec_module(mod)
        else:
            mod = importlib.reload(self._module)

        self._module = mod
        self._mtime = os.path.getmtime(self.module_path)
        return mod

    def _maybe_reload(self) -> None:
        """检测模块文件是否变更，变更时热重载."""
        try:
            current_mtime = os.path.getmtime(self.module_path)
        except OSError:
            return
        if current_mtime > self._mtime:
            logger.info("Reflect 模块已变更，热重载 %s", self.module_path)
            self._module = self._load_module()

    def _get_interval(self) -> int:
        """获取模块定义的 check 间隔.

        Returns:
            间隔秒数，默认 5.
        """
        return getattr(self._module, "INTERVAL", 5)

    def _should_exit_after_run(self) -> bool:
        """检查是否触发后退出.

        Returns:
            True 如果 ONCE 模式.
        """
        return getattr(self._module, "ONCE", False)

    def _call_check(self) -> Optional[str]:
        """调用模块的 check() 方法.

        Returns:
            task 字符串 / None / '/exit'.

        Raises:
            Exception: check() 抛出异常时捕获并记录.
        """
        check_fn: Optional[Callable[[], Optional[str]]] = getattr(
            self._module, "check", None
        )
        if check_fn is None:
            logger.warning("Reflect 模块 %s 没有 check() 方法", self.name)
            return None
        return check_fn()

    def _call_init(self, args: Optional[dict] = None) -> None:
        """调用模块的 init() 方法（如果存在）.

        Args:
            args: 传递给 init() 的配置字典.
        """
        init_fn: Optional[Callable[[dict], None]] = getattr(
            self._module, "init", None
        )
        if init_fn is not None:
            init_fn(args or {})

    def _call_on_done(self, result: Any) -> None:
        """调用模块的 on_done() 方法（如果存在）.

        Args:
            result: 任务执行结果.
        """
        on_done_fn: Optional[Callable[[Any], None]] = getattr(
            self._module, "on_done", None
        )
        if on_done_fn is not None:
            on_done_fn(result)

    def _run_task(self, task: str) -> Any:
        """执行单个 reflect 任务.

        调用 agent.run(task) 并消费 generator 直到完成。

        Args:
            task: 任务描述字符串.

        Returns:
            agent.run() 的 return value.
        """
        gen = self.agent.run(task)
        result = None
        try:
            while True:
                try:
                    chunk = next(gen)
                    # 将文本 chunk 输出到 stdout（与 CLI 行为一致）
                    if isinstance(chunk, str):
                        import sys
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                except StopIteration as e:
                    result = e.value
                    break
        except KeyboardInterrupt:
            self.agent.abort()
            logger.info("Reflect 任务被中断: %s", task[:80])
        return result

    def run_loop(self, init_args: Optional[dict] = None) -> None:
        """启动 reflect 主循环.

        1. 加载 reflect 模块
        2. 调用 init() 初始化
        3. 以 INTERVAL 秒间隔循环:
           a. 热重载检测
           b. check() 获取任务
           c. 有任务 → agent.run(task), on_done()
           d. ONCE 模式 → 退出
           e. '/exit' → 退出
           f. None → 休眠

        Args:
            init_args: 传递给 init() 的参数字典.
        """
        self._module = self._load_module()
        self._call_init(init_args)
        self._running = True

        logger.info(
            "Reflect runner 启动: %s (INTERVAL=%ds, ONCE=%s)",
            self.name,
            self._get_interval(),
            self._should_exit_after_run(),
        )

        while self._running:
            try:
                self._maybe_reload()
                task = self._call_check()
            except Exception:
                logger.exception("check() 调用失败")
                time.sleep(self._get_interval())
                continue

            if task is None:
                # 无任务，休眠
                time.sleep(self._get_interval())
                continue

            if task == "/exit":
                logger.info("Reflect 模块返回 /exit，退出循环")
                self._running = False
                break

            # 有任务，执行
            logger.info("Reflect 触发任务: %s", task[:120])
            result = self._run_task(task)
            try:
                self._call_on_done(result)
            except Exception:
                logger.exception("on_done() 调用失败")

            if self._should_exit_after_run():
                logger.info("ONCE 模式，任务完成后退出")
                self._running = False
                break

        logger.info("Reflect runner 退出: %s", self.name)

    def stop(self) -> None:
        """停止 reflect 循环.（可从其他线程调用）"""
        self._running = False
