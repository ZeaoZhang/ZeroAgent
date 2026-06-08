#!/usr/bin/env python3
"""Restart ZeroAgent backend, desktop frontend, and configured IM channels.

Default behavior:
  - restart backend bridge
  - restart desktop launcher
  - restart IM channels detected from config

Use --im to choose IM channels explicitly, or --all-im to force all known ones.
Logs are written to temp/restart_logs/.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


REPO_ROOT = Path(__file__).resolve().parents[1]
LOG_DIR = REPO_ROOT / "temp" / "restart_logs"


@dataclass(frozen=True)
class Service:
    name: str
    module: str
    patterns: tuple[str, ...]
    required_keys: tuple[str, ...] = ()
    extra_env: dict[str, str] | None = None
    conflict_group: str | None = None


BACKEND = Service(
    name="backend",
    module="zero_agent.frontends.desktop_bridge",
    patterns=("zero_agent.frontends.desktop_bridge",),
    extra_env={"ZA_DESKTOP_BRIDGE_NO_BROWSER": "1"},
)

FRONTEND = Service(
    name="frontend",
    module="zero_agent.frontends.launcher",
    patterns=("zero_agent.frontends.launcher", "zero-agent-launcher"),
)

IM_SERVICES: dict[str, Service] = {
    "telegram": Service(
        name="telegram",
        module="zero_agent.bots.telegram_app",
        patterns=("zero_agent.bots.telegram_app",),
        required_keys=("tg_bot_token",),
    ),
    "discord": Service(
        name="discord",
        module="zero_agent.bots.discord_app",
        patterns=("zero_agent.bots.discord_app",),
        required_keys=("discord_bot_token",),
    ),
    "feishu": Service(
        name="feishu",
        module="zero_agent.bots.feishu_app",
        patterns=("zero_agent.bots.feishu_app",),
        required_keys=("fs_app_id", "fs_app_secret"),
    ),
    "wecom": Service(
        name="wecom",
        module="zero_agent.bots.wecom_app",
        patterns=("zero_agent.bots.wecom_app",),
        required_keys=("wecom_bot_id", "wecom_secret"),
        conflict_group="wechat_wecom_lock_19531",
    ),
    "dingtalk": Service(
        name="dingtalk",
        module="zero_agent.bots.dingtalk_app",
        patterns=("zero_agent.bots.dingtalk_app",),
        required_keys=("dingtalk_client_id", "dingtalk_client_secret"),
    ),
    "qq": Service(
        name="qq",
        module="zero_agent.bots.qq_app",
        patterns=("zero_agent.bots.qq_app",),
        required_keys=("qq_app_id", "qq_app_secret"),
    ),
    "wechat": Service(
        name="wechat",
        module="zero_agent.bots.wechat_app",
        patterns=("zero_agent.bots.wechat_app",),
        conflict_group="wechat_wecom_lock_19531",
    ),
}


def load_config() -> dict:
    config: dict = {}
    for path in (REPO_ROOT / "zero_agent" / "mykey.json", REPO_ROOT / "mykey.json"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                config.update(data)
            break
        except (FileNotFoundError, json.JSONDecodeError):
            continue

    env_map = {
        "tg_bot_token": "TG_BOT_TOKEN",
        "tg_allowed_users": "TG_ALLOWED_USERS",
        "discord_bot_token": "DISCORD_BOT_TOKEN",
        "discord_allowed_users": "DISCORD_ALLOWED_USERS",
        "proxy": "BOT_PROXY",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name, "")
        if value:
            config[key] = value
    return config


def has_value(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def is_configured(service: Service, config: dict) -> bool:
    if service.name == "wechat":
        token_file = Path.home() / ".wxbot" / "token.json"
        if not token_file.exists():
            return False
        try:
            data = json.loads(token_file.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return has_value(data.get("bot_token"))
    return all(has_value(config.get(key)) for key in service.required_keys)


def ps_rows() -> list[tuple[int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command="],
        cwd=str(REPO_ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        pid_text, _, command = stripped.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        rows.append((pid, command.strip()))
    return rows


def find_pids(service: Service) -> list[int]:
    own_pid = os.getpid()
    pids: list[int] = []
    for pid, command in ps_rows():
        if pid == own_pid:
            continue
        if "scripts/restart_za.py" in command:
            continue
        if any(pattern in command for pattern in service.patterns):
            pids.append(pid)
    return pids


def wait_gone(pids: Iterable[int], timeout: float) -> set[int]:
    remaining = set(pids)
    deadline = time.time() + timeout
    while remaining and time.time() < deadline:
        alive = set()
        for pid in remaining:
            try:
                os.kill(pid, 0)
                alive.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                alive.add(pid)
        remaining = alive
        if remaining:
            time.sleep(0.2)
    return remaining


def stop_service(service: Service, dry_run: bool) -> None:
    pids = find_pids(service)
    if not pids:
        print(f"[{service.name}] no existing process")
        return
    print(f"[{service.name}] stopping pids: {', '.join(map(str, pids))}")
    if dry_run:
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    remaining = wait_gone(pids, timeout=5)
    if remaining:
        print(f"[{service.name}] force killing pids: {', '.join(map(str, sorted(remaining)))}")
        for pid in sorted(remaining):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def start_service(service: Service, dry_run: bool) -> None:
    cmd = [sys.executable, "-m", service.module]
    print(f"[{service.name}] starting: {' '.join(cmd)}")
    if dry_run:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{service.name}.log"
    env = os.environ.copy()
    if service.extra_env:
        env.update(service.extra_env)
    log = log_path.open("ab")
    subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    print(f"[{service.name}] log: {log_path}")


def parse_im_list(raw: str) -> list[str]:
    names = [part.strip().lower() for part in raw.split(",") if part.strip()]
    unknown = [name for name in names if name not in IM_SERVICES]
    if unknown:
        known = ", ".join(IM_SERVICES)
        raise SystemExit(f"Unknown IM channel(s): {', '.join(unknown)}. Known: {known}")
    return names


def select_im_services(args: argparse.Namespace, config: dict) -> tuple[list[Service], list[str]]:
    notes: list[str] = []
    if args.no_im:
        return [], notes
    if args.all_im:
        selected_names = list(IM_SERVICES)
    elif args.im:
        selected_names = parse_im_list(args.im)
    else:
        selected_names = [
            name for name, service in IM_SERVICES.items()
            if is_configured(service, config)
        ]

    selected: list[Service] = []
    conflict_seen: dict[str, str] = {}
    for name in selected_names:
        service = IM_SERVICES[name]
        if service.conflict_group:
            previous = conflict_seen.get(service.conflict_group)
            if previous:
                notes.append(
                    f"skip {service.name}: conflicts with {previous} on lock port 19531"
                )
                continue
            conflict_seen[service.conflict_group] = service.name
        if not args.all_im and not args.im and service.required_keys and not is_configured(service, config):
            notes.append(f"skip {service.name}: missing required config")
            continue
        selected.append(service)
    return selected, notes


def build_plan(args: argparse.Namespace) -> tuple[list[Service], list[str]]:
    config = load_config()
    services: list[Service] = []
    if not args.no_backend:
        services.append(BACKEND)
    if not args.no_frontend:
        services.append(FRONTEND)
    im_services, notes = select_im_services(args, config)
    services.extend(im_services)
    return services, notes


def print_status(services: list[Service]) -> None:
    for service in services:
        pids = find_pids(service)
        state = ", ".join(map(str, pids)) if pids else "not running"
        print(f"[{service.name}] {state}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restart ZeroAgent backend, desktop frontend, and IM channels."
    )
    parser.add_argument("--no-backend", action="store_true", help="Do not manage backend bridge")
    parser.add_argument("--no-frontend", action="store_true", help="Do not manage desktop launcher")
    parser.add_argument("--no-im", action="store_true", help="Do not manage IM channels")
    parser.add_argument(
        "--im",
        help="Comma-separated IM channels to manage. Known: " + ", ".join(IM_SERVICES),
    )
    parser.add_argument("--all-im", action="store_true", help="Manage all known IM channels")
    parser.add_argument("--status", action="store_true", help="Only show matching processes")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without changing processes")
    args = parser.parse_args()

    if args.im and args.all_im:
        parser.error("--im and --all-im cannot be used together")

    services, notes = build_plan(args)
    if notes:
        for note in notes:
            print(f"[note] {note}")
    if not services:
        print("No services selected.")
        return 0

    if args.status:
        print_status(services)
        return 0

    print("Services: " + ", ".join(service.name for service in services))
    for service in services:
        stop_service(service, args.dry_run)
    for service in services:
        start_service(service, args.dry_run)
    print("Done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
