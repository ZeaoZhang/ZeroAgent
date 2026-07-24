"""CL(folder) — one-stop task checklist supporting checklist and mapreduce modes.

checklist mode (workers=0):
    Master executes tasks sequentially without launching BBS.

mapreduce mode (workers>0):
    Launches BBS + N workers for parallel execution.

mapreduce mode 依赖:
    - agent_bbs.py（外部 BBS 服务资产，ZeroAgent 不默认打包）
    - zero_agent.reflect.agent_team_worker (exists in ZeroAgent)
    - zero_agent.reflect.checklist_master (exists in ZeroAgent)
    - zero_agent.runners.cli
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_R = Path(__file__).resolve().parent.parent

# Reflect modules exist in ZeroAgent
_W_RE = _R / "reflect" / "agent_team_worker.py"
_M_RE = _R / "reflect" / "checklist_master.py"

# BBS 二进制路径：需要外部兼容的 agent_bbs.py。
# 若要启用 mapreduce 模式，请放到 zero_agent/assets/ 或调整此路径。
_BBS_PATH = _R / "assets" / "agent_bbs.py"

_CLI = [sys.executable, "-m", "zero_agent.runners.cli"]

_PK: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
if sys.platform == "win32":
    _PK["creationflags"] = 0x200


class CL:
    """Task checklist manager backed by a state.json file.

    Usage:
        cl = CL("path/to/checklist_folder", goal="My Goal")
        cl.add(["Task 1", "Task 2"])
        cl.mark(1, "done")
        print(cl.look())
        cl.close()
    """

    def __init__(self, folder: str | Path, goal: str = "", workers: int = 0) -> None:
        """Initialize checklist in *folder*.

        Args:
            folder: Directory for state.json and optional BBS data.
            goal: Description of the overall goal.
            workers: 0 for checklist mode, >0 for mapreduce mode.
        """
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.path = self.folder / "state.json"
        self.workers = workers

        if self.path.exists():
            self._d = json.loads(self.path.read_text("utf-8"))
        else:
            self._d = {"closed": False, "goal": goal, "bbs": None, "tasks": []}
            self._save()
            if workers > 0:
                self._ensure_bbs()
                self.start_worker(workers)

    @property
    def tasks(self) -> list[dict]:
        return self._d["tasks"]

    @property
    def closed(self) -> bool:
        return self._d.get("closed", False)

    @property
    def has_open(self) -> bool:
        return any(t["result"] is None for t in self.tasks)

    @property
    def bbs_url(self) -> str | None:
        return self._d["bbs"]["url"] if self._d["bbs"] else None

    @property
    def bbs_key(self) -> str | None:
        return self._d["bbs"]["key"] if self._d["bbs"] else None

    @property
    def mode(self) -> str:
        return "mapreduce" if self._d["bbs"] else "checklist"

    def _save(self) -> None:
        """Persist current state to state.json."""
        self.path.write_text(
            json.dumps(self._d, ensure_ascii=False, indent=1), "utf-8"
        )

    def _ensure_bbs(self) -> None:
        """在空闲端口启动 BBS 服务。

        需要在 _BBS_PATH 提供与 ZeroAgent 兼容的外部 agent_bbs.py。
        """
        if self._d["bbs"]:
            return
        if not _BBS_PATH.exists():
            raise FileNotFoundError(
                f"BBS server not found at {_BBS_PATH}. "
                "请将兼容的 agent_bbs.py 放到 zero_agent/assets/ 后再启用 mapreduce 模式。"
            )
        with socket.socket() as s:
            s.bind(("", 0))
            port = s.getsockname()[1]
        key = f"cl_{int(time.time()) % 1000}"
        (self.folder / "bbs").mkdir(exist_ok=True)
        subprocess.Popen(
            [
                sys.executable,
                str(_BBS_PATH),
                "--cwd",
                str(self.folder / "bbs"),
                "--port",
                str(port),
                "--key",
                key,
            ],
            **_PK,
        )
        time.sleep(1)
        self._d["bbs"] = {"url": f"http://127.0.0.1:{port}", "key": key}
        self._save()

    def add(self, texts: list[str]) -> list[int]:
        """Add tasks to the checklist.

        Args:
            texts: Task description strings.

        Returns:
            List of assigned task IDs.
        """
        nid = max((t["id"] for t in self.tasks), default=0) + 1
        ids: list[int] = []
        for text in texts:
            self.tasks.append(
                {"id": nid, "text": text, "result": None, "ts": int(time.time())}
            )
            ids.append(nid)
            nid += 1
        self._save()
        print("task added, must reread checklist SOP before start executing ...")
        return ids

    def mark(self, tid: int, result: str) -> None:
        """Mark a task with a result string.

        Args:
            tid: Task ID to mark.
            result: Result description (e.g. "done", "blocked: reason").
        """
        for t in self.tasks:
            if t["id"] == tid:
                t["result"] = result
                t["ts"] = int(time.time())
                break
        self._save()

    def look(self) -> str:
        """Return a human-readable summary of the checklist state."""
        done = sum(1 for t in self.tasks if t["result"] is not None)
        lines = [f"[{done}/{len(self.tasks)}] mode={self.mode}"]
        for t in self.tasks:
            status = "✓" if t["result"] else "○"
            line = f'{status} #{t["id"]} {t["text"][:60]}'
            if t["result"]:
                line += f'  → {t["result"][:60]}'
            lines.append(line)
        return "\n".join(lines)

    def close(self) -> None:
        """Close the checklist (all tasks must be marked first)."""
        if self.has_open:
            raise AssertionError("has open tasks — mark all tasks before closing")
        self._d["closed"] = True
        self._save()

    def start_worker(self, n: int | None = None) -> None:
        """Launch N worker processes for mapreduce mode.

        Requires a running BBS (self.bbs_url / self.bbs_key must be set).

        Args:
            n: Number of workers. Defaults to self.workers or 1.
        """
        n = n or self.workers or 1
        if n <= 0:
            return
        for i in range(n):
            subprocess.Popen(
                _CLI
                + [
                    "--reflect",
                    str(_W_RE),
                    "--reflect-arg",
                    f"base_url={self.bbs_url}",
                    "--reflect-arg",
                    f"board_key={self.bbs_key}",
                    "--reflect-arg",
                    f"name=w{i + 1}",
                ],
                **_PK,
            )
            if i < n - 1:
                time.sleep(5)

    @staticmethod
    def _pid_alive(pid: int | None) -> bool:
        """Check if a process is still running (Windows only via tasklist)."""
        if not pid:
            return False
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            return str(pid) in result.stdout
        except Exception:
            return False

    def start_master(self) -> None:
        """Launch a checklist master process.

        Requires zero_agent.reflect.checklist_master and zero_agent.runners.cli.
        """
        old_pid = self._d.get("master_pid")
        if old_pid and self._pid_alive(old_pid):
            print(f"[CL] master already running (PID {old_pid}), skip")
            return
        p = subprocess.Popen(
            _CLI
            + [
                "--reflect",
                str(_M_RE),
                "--reflect-arg",
                f"mr_folder={self.folder.resolve()}",
            ],
            **_PK,
        )
        self._d["master_pid"] = p.pid
        self._save()
