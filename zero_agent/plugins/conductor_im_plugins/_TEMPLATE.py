"""新IM插件模板：复制为 yourim.py（_开头的不加载），填好下面三件套即可被自动发现。

契约：
  notify(data, config, label) → bool
      向外推送通知。data 是要发送的消息内容（可含 markdown），config 是用户配置
      字典（含 API key / URL 等），label 是可选来源标签。返回 True 表示发送成功。
  check() → bool
      检查是否有新消息（必须廉价，毫秒级，无 LLM 调用）。框架另有冷却，宁可误报。
  INTERVAL : check() 的轮询间隔（秒）
  PROMPT   : 派给采集 subagent 的完整指令。它是有记忆/工具的完整 agent，
             所以只需给三样：用什么工具看新消息（指向 SOP）、按什么标准过滤、
             汇报边界。注意限量：让它用"天然有界"的工具（如最近N条），
             禁止开放式全量扫描。

notify() 是 ZeroAgent 新增的通知出口（用于将 conductor 事件推送到 IM）。
check() 保持与 ZeroAgent conductor 的轮询约定兼容（用于从 IM 拉取新消息唤醒
conductor）。
"""

from __future__ import annotations

from typing import Any

INTERVAL = 60

PROMPT = """\
你是XX采集subagent。先读记忆中用户画像，再用<工具/SOP名>查看最近消息，\
过滤后汇报值得关注项并补全上下文。
不执行外部动作；无值得关注的就一句话说明。"""


def notify(data: str, config: dict[str, Any], label: str = "") -> bool:
    """向外推送通知。

    Args:
        data: 要发送的消息内容（可含 markdown）。
        config: 用户配置字典，通常含 API key、webhook URL 等凭据。
        label: 可选来源标签，标识是哪个 conductor 事件触发的。

    Returns:
        True 表示发送成功，False 表示失败。

    示例 config 字段（按具体平台填写）：
        - webhook_url:  incoming webhook 地址
        - api_key:      API 密钥
        - secret:       签名密钥（HMAC）
        - smtp_host / smtp_port / from_addr / to_addr / smtp_user / smtp_pass
    """
    return False


def check() -> bool:
    """检查是否有新消息。必须廉价（毫秒级，无 LLM 调用）。"""
    return False
