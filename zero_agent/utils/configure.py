"""交互式配置向导 — ZeroAgent 终端配置助手.

引导用户完成 LLM 提供商选择、API Key 输入、模型选择等，
生成项目根目录 config.yaml 配置文件。

用法:
    python -m zero_agent.utils.configure          # 交互模式
    python -m zero_agent.utils.configure --auto   # 自动模式（从环境变量）
"""

from __future__ import annotations

import os
import sys
from getpass import getpass
from typing import Optional

from zero_agent.core.config import default_config_path, project_root


# 预定义提供商配置
PROVIDERS: dict[str, dict] = {
    "1": {
        "name": "Anthropic (Claude)",
        "provider": "anthropic",
        "api_base": "https://api.anthropic.com",
        "models": [
            "claude-sonnet-4-6",
            "claude-opus-4-7",
            "claude-haiku-4-5",
        ],
    },
    "2": {
        "name": "OpenAI",
        "provider": "openai",
        "api_base": "https://api.openai.com/v1",
        "models": [
            "gpt-4o",
            "gpt-4o-mini",
            "gpt-4.1",
        ],
    },
    "3": {
        "name": "DeepSeek",
        "provider": "deepseek",
        "api_base": "https://api.deepseek.com/v1",
        "models": [
            "deepseek-chat",
            "deepseek-reasoner",
        ],
    },
    "4": {
        "name": "兼容 OpenAI 的自定义提供商",
        "provider": "openai",
        "api_base": "",
        "models": [],
    },
}


def main(argv: Optional[list[str]] = None) -> None:
    """配置向导主入口.

    Args:
        argv: 命令行参数列表.
    """
    args = sys.argv[1:] if argv is None else argv
    auto_mode = "--auto" in args

    print("ZeroAgent 配置向导")
    print("=" * 40)

    if auto_mode:
        _auto_configure()
    else:
        _interactive_configure()


def _auto_configure() -> None:
    """从环境变量自动生成配置."""
    provider = os.environ.get("ZA_LLM_PROVIDER", "anthropic")
    api_key = os.environ.get("ZA_LLM_API_KEY", "")
    api_base = os.environ.get(
        "ZA_LLM_API_BASE",
        "https://api.anthropic.com" if provider == "anthropic" else "",
    )
    model = os.environ.get("ZA_LLM_MODEL", "")
    workspace = os.environ.get("ZA_WORKSPACE_DIR", str(project_root() / "workspace"))

    if not api_key:
        print("[Error] 未设置 ZA_LLM_API_KEY 环境变量")
        sys.exit(1)
    if not model:
        print("[Error] 未设置 ZA_LLM_MODEL 环境变量")
        sys.exit(1)

    config = _build_config_yaml(provider, api_key, api_base, model, workspace)
    _write_config(config)
    print("  已从环境变量生成配置")


def _interactive_configure() -> None:
    """交互式配置向导."""
    # 步骤 1: 提供商选择
    print("\n可选 LLM 提供商:")
    for key, info in PROVIDERS.items():
        print(f"  [{key}] {info['name']}")

    provider_choice = _prompt("\n选择提供商", default="1")

    if provider_choice in PROVIDERS:
        provider_info = PROVIDERS[provider_choice]
        provider = provider_info["provider"]
        api_base = provider_info["api_base"]
        available_models = provider_info["models"]
    else:
        # 自定义
        provider = _prompt("  提供商标识 (如 openai, anthropic)")
        api_base = _prompt("  API Base URL")
        available_models = []

    # 步骤 2: 自定义 API Base（如需要）
    if provider_choice == "4":
        api_base = _prompt("  API Base URL (如 https://api.openai.com/v1)")
        custom_models = _prompt("  模型 ID（逗号分隔，可选）").strip()
        if custom_models:
            available_models = [m.strip() for m in custom_models.split(",")]

    # 步骤 3: API Key
    print(f"\n提供商: {provider}")
    if api_base:
        print(f"API Base: {api_base}")
    api_key = getpass("API Key (输入不可见): ").strip()
    if not api_key:
        print("[Error] API Key 不能为空")
        sys.exit(1)

    # 步骤 4: 模型选择
    model = _select_model(available_models, provider)

    # 步骤 5: 工作目录
    print()
    default_ws = str(project_root() / "workspace")
    workspace = _prompt("工作目录", default=default_ws)

    # 步骤 6: 生成并写入配置
    print()
    config = _build_config_yaml(provider, api_key, api_base, model, workspace)
    print("\n即将生成的配置:")
    print("-" * 40)
    print(config)
    print("-" * 40)

    confirm = _prompt("\n保存配置? (Y/n)", default="Y").lower()
    if confirm in ("", "y", "yes"):
        _write_config(config)
    else:
        print("已取消")


def _select_model(available_models: list[str], provider: str) -> str:
    """模型选择交互.

    Args:
        available_models: 预定义的可用模型列表.
        provider: 提供商标识.

    Returns:
        选中的模型 ID.
    """
    if available_models:
        print(f"\n推荐模型 ({provider}):")
        for i, m in enumerate(available_models, 1):
            print(f"  [{i}] {m}")
        print(f"  [0] 自定义模型 ID")
        choice = _prompt("选择模型", default="1")

        if choice == "0":
            return _prompt("  模型 ID")
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(available_models):
                return available_models[idx]
        except ValueError:
            pass
        return choice
    else:
        return _prompt("模型 ID（如 gpt-4o）")


def _build_config_yaml(
    provider: str,
    api_key: str,
    api_base: str,
    model: str,
    workspace: str,
) -> str:
    """生成 YAML 配置字符串.

    Args:
        provider: 提供商标识.
        api_key: API 密钥.
        api_base: API 基础 URL.
        model: 模型 ID.
        workspace: 工作目录.

    Returns:
        YAML 格式的配置字符串.
    """
    lines = [
        "# ZeroAgent 配置文件",
        f"# 生成时间: auto-generated",
        "",
        f"default_backend: default",
        f"max_turns: 80",
        f"workspace_dir: {workspace}",
        f"memory_dir: ./memory",
        f"language: auto",
        "",
        "llm_backends:",
        "  default:",
        f"    name: default",
        f"    provider: {provider}",
        f"    api_key: {api_key}",
        f"    api_base: {api_base}",
        f"    model: {model}",
        "    context_window: 30000",
        "    temperature: 1.0",
        "    stream: true",
    ]
    return "\n".join(lines)


def _write_config(content: str) -> None:
    """将配置写入项目根目录 config.yaml.

    如果已存在，先备份为 config.yaml.bak.

    Args:
        content: YAML 配置字符串.
    """
    config_path = str(default_config_path())
    config_dir = os.path.dirname(config_path)
    os.makedirs(config_dir, exist_ok=True)

    # 备份已有配置
    if os.path.isfile(config_path):
        backup_path = config_path + ".bak"
        try:
            os.rename(config_path, backup_path)
            print(f"  已有配置已备份到: {backup_path}")
        except OSError:
            pass

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"\n配置已保存到: {config_path}")
    print("使用方式: zero-agent")


def _prompt(text: str, default: str = "") -> str:
    """显示提示并读取用户输入.

    Args:
        text: 提示文本.
        default: 默认值.

    Returns:
        用户输入字符串.
    """
    if default:
        return input(f"{text} [{default}]: ") or default
    return input(f"{text}: ")


if __name__ == "__main__":
    main()
