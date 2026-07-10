"""飞书/Lark 通知插件示例：通过 incoming webhook 发送卡片消息。

notify() — 使用 urllib（标准库）调用 Lark/飞书 incoming webhook，发送
          markdown 交互消息卡片（标题 + 正文 + 跳转按钮）。
check()  — 使用 lark-cli 轮询 config.local.json 中的 lark_chat_ids，
          检测 INTERVAL 内是否有新消息。

notify() 期望的 config 字段:
    webhook_url — Lark/飞书 incoming webhook 完整地址
                   （如 https://open.feishu.cn/open-apis/bot/v2/hook/xxx）
    secret      — (可选) 签名密钥，用于消息校验

check() 期望的配置:
    本插件目录下的 config.local.json 含 {"lark_chat_ids": ["oc_xxx", ...]}。
    且系统需安装 lark-cli（通过 npm: @larksuite/lark-cli）。
"""

from __future__ import annotations

import glob
import hashlib
import hmac
import json
import os
import subprocess
import time
import urllib.request
from typing import Any


INTERVAL = 300

PROMPT = """\
你是飞书采集subagent。先读记忆中用户画像，再用 lark-cli 查看最近消息\
（详见 lark_cli_sop；会话ID取本插件目录 config.local.json 的 lark_chat_ids，\
逐个 `lark-cli im +chat-messages-list --as user --chat-id <id> --sort desc \
--page-size 10`），挑出刚出现的新消息，过滤后汇报值得关注项并补全上下文。
不执行外部动作；无值得关注的就一句话说明。"""


# ---- notify() 相关 ----

def _gen_sign(timestamp: str, secret: str) -> str:
    """生成飞书 webhook 消息签名。

    Args:
        timestamp: 当前秒级时间戳字符串。
        secret: 签名密钥。

    Returns:
        Base64 编码的 HMAC-SHA256 签名。
    """
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        key=string_to_sign.encode("utf-8"),
        msg=b"",
        digestmod=hashlib.sha256,
    )
    # 飞书签名需要在空消息后再 update
    hmac_code.update(string_to_sign.encode("utf-8"))
    return hmac_code.hexdigest()


def notify(data: str, config: dict[str, Any], label: str = "") -> bool:
    """通过飞书/Lark incoming webhook 发送交互消息卡片。

    Args:
        data: 消息正文（可含 markdown，卡片正文内容）。
        config: 含 webhook_url（必需），可选 secret 用于签名。
        label: 可选来源标签，写入卡片标题前缀。

    Returns:
        True 表示发送成功，False 表示失败。
    """
    try:
        webhook_url: str = config["webhook_url"]
        secret: str | None = config.get("secret")
        timestamp = str(int(time.time()))

        title = f"ZeroAgent{' · ' + label if label else ''}"
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": title},
                    "template": "blue",
                },
                "elements": [
                    {
                        "tag": "markdown",
                        "content": data,
                    },
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "View Conductor"},
                                "type": "default",
                                "url": "http://localhost:8900",
                            }
                        ],
                    },
                ],
            },
        }

        body = json.dumps(card, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )

        if secret:
            sign = _gen_sign(timestamp, secret)
            req.add_header("X-Lark-Request-Timestamp", timestamp)
            req.add_header("X-Lark-Signature", sign)

        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return result.get("code") == 0
    except Exception:
        return False


# ---- check() 相关 ----

_CFG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.local.json")
_NODE = glob.glob(
    os.path.expandvars(r"%APPDATA%\fnm\node-versions\*\installation")
)  # lark-cli 在 fnm node 下


def check() -> bool:
    """通过 lark-cli 检查指定会话是否有新消息。

    读取 config.local.json 中的 lark_chat_ids，逐个查询最近消息，
    若有 INTERVAL 内的新消息则返回 True。

    Returns:
        True 表示存在新消息，False 表示无或出错。
    """
    cfg = json.load(open(_CFG, encoding="utf-8")) if os.path.exists(_CFG) else {}
    env = {**os.environ, "PATH": os.pathsep.join(_NODE + [os.environ.get("PATH", "")])}
    start = int(time.time()) - INTERVAL - 5
    for cid in cfg.get("lark_chat_ids", []):
        r = subprocess.run(
            f"lark-cli im +chat-messages-list --as user --chat-id {cid} --start {start} --page-size 1",
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        try:
            if json.loads(r.stdout)["data"]["total"]:
                return True
        except Exception:
            pass
    return False
