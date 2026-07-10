"""邮件通知插件示例：通过 SMTP 发送纯文本邮件，并可通过 Gmail 检查未读邮件。

notify() — 使用 smtplib（标准库）发送邮件通知。
check()  — 使用 ezgmail 检查 Gmail 未读邮件（需先 `pip install ezgmail` 并授权）。

notify() 期望的 config 字段:
    smtp_host  — SMTP 服务器地址，如 "smtp.gmail.com"
    smtp_port  — SMTP 端口，如 587 (TLS)
    from_addr  — 发件人邮箱
    to_addr    — 收件人邮箱
    smtp_user  — SMTP 登录用户名（通常同 from_addr）
    smtp_pass  — SMTP 登录密码或应用专用密码
"""

from __future__ import annotations

import smtplib
import time
from email.mime.text import MIMEText
from typing import Any

INTERVAL = 7200

PROMPT = """\
你是邮件采集subagent。先读记忆中用户画像，再用 ezgmail 检查未读邮件，\
过滤后汇报值得关注项并补全上下文（发件人身份/线程/附件摘要）。
过滤营销和自动通知，不确定的标"低优先级观察"。不回复不执行外部动作；\
无值得关注的就一句话说明。"""


def notify(data: str, config: dict[str, Any], label: str = "") -> bool:
    """通过 SMTP 发送邮件通知。

    Args:
        data: 邮件正文（纯文本）。
        config: 含 SMTP 凭据。必需字段：smtp_host, smtp_port, from_addr,
                to_addr, smtp_user, smtp_pass。
        label: 可选来源标签，会写入邮件主题前缀。

    Returns:
        True 表示邮件已发送，False 表示失败。
    """
    try:
        subject = f"[ZeroAgent{(' ' + label) if label else ''}] Notification"
        msg = MIMEText(data, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = config["from_addr"]
        msg["To"] = config["to_addr"]
        msg["Date"] = time.strftime("%a, %d %b %Y %H:%M:%S %z")

        with smtplib.SMTP(config["smtp_host"], config["smtp_port"], timeout=30) as smtp:
            smtp.starttls()
            smtp.login(config["smtp_user"], config["smtp_pass"])
            smtp.send_message(msg)
        return True
    except Exception:
        return False


def check() -> bool:
    """检查 Gmail 是否有未读邮件。

    Returns:
        True 表示存在未读邮件，False 表示无或出错。
    """
    try:
        import ezgmail
        return bool(ezgmail.unread())
    except Exception:
        return False
