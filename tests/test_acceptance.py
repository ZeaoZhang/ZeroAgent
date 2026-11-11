"""验收清单（docs/extension-design.md 第十三节）的直接对应用例.

V1/V3/V4/V5/V8 已在其它测试文件中覆盖，这里补齐缺口：
V2、V6、V7、V9、V10。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from zero_agent.extensions.builtin import mcp_client, skills_index
from zero_agent.extensions.context import ExtensionContext
from zero_agent.extensions.runtime import ExtensionRuntime
from zero_agent.tools.registry import ToolRegistry


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _runtime(home: Path, registry: ToolRegistry | None = None) -> ExtensionRuntime:
    return ExtensionRuntime(registry=registry or ToolRegistry(), home=home)


def _agent(tmp_path: Path, enabled: bool = True):
    from zero_agent.core.agent import ZeroAgent
    from zero_agent.core.config import AgentConfig, LLMBackendConfig

    home = tmp_path / "home"
    config = AgentConfig(
        llm_backends={
            "d": LLMBackendConfig(
                name="d",
                provider="openai",
                api_key="k",
                api_base="https://api.openai.com/v1",
                model="gpt-4o",
            )
        },
        default_backend="d",
        workspace_dir=str(tmp_path / "ws"),
        memory_dir=str(tmp_path / "mem"),
        sessions_dir=str(tmp_path / "sessions"),
        extensions_home=str(home),
        extensions_enabled=enabled,
    )
    return ZeroAgent(config)


class TestV2NoExtensionHome:
    """V2：扩展目录不存在时，核心工具与循环照常工作."""

    def test_missing_extension_home_keeps_core_tools(self, tmp_path: Path) -> None:
        agent = _agent(tmp_path, enabled=True)

        names = {tool.name for tool in agent.registry.list_all()}

        # 核心工具必须在
        assert "code_run" in names
        assert "file_read" in names
        assert "complete_task" in names
        # 扩展初始化失败也不该返回 None（目录不存在不算失败）
        assert agent.extensions is not None


class TestV6SingleBrokenServer:
    """V6：单个 MCP server 配置错误，其余 server 仍注册."""

    def test_broken_server_does_not_block_healthy_one(self, tmp_path: Path) -> None:
        fake = tmp_path / "fake_server.py"
        fake.write_text(
            "import json,sys\n"
            "for line in sys.stdin:\n"
            "    msg=json.loads(line)\n"
            "    if msg.get('method')=='initialize':\n"
            "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':"
            "{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'f','version':'0'}}}),flush=True)\n"
            "    elif msg.get('method')=='tools/list':\n"
            "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'tools':"
            "[{'name':'echo','description':'d','inputSchema':{'type':'object','properties':{}}}]}}),flush=True)\n",
            encoding="utf-8",
        )
        config = {
            "mcpServers": {
                "broken": {"command": "/nonexistent/binary", "timeout": 2},
                "good": {"command": "python3", "args": [str(fake)], "timeout": 10},
            }
        }
        (tmp_path / "mcp.json").write_text(json.dumps(config), encoding="utf-8")

        runtime = _runtime(tmp_path)
        mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert runtime.registry.get("mcp__good__echo") is not None
        assert runtime.registry.get("mcp__broken__echo") is None


class TestV7NoMcpDependency:
    """V7：未配置 MCP 时不导入 MCP SDK（本实现根本不依赖它）."""

    def test_setup_without_config_imports_nothing(self, tmp_path: Path) -> None:
        sys.modules.pop("mcp", None)
        runtime = _runtime(tmp_path)

        mcp_client.setup(ExtensionContext("mcp_client", runtime))

        assert "mcp" not in sys.modules


class TestV9SopPrecedence:
    """V9：skill 索引必须声明 SOP 优先."""

    def test_skill_index_states_sop_precedence(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "skills" / "demo" / "SKILL.md",
            "---\nname: demo\ndescription: 演示\n---\nbody\n",
        )
        runtime = _runtime(tmp_path)
        skills_index.setup(ExtensionContext("skills_index", runtime))

        blocks = runtime.collect_prompt_blocks()
        combined = "\n".join(block.text for block in blocks)

        assert "SOP" in combined
        assert "memory/sops" in combined


class TestV10RuntimeRebuildReplays:
    """V10：运行时配置变更触发 registry 重建后，插件注册必须被重新应用."""

    def test_pending_runtime_config_rebuild_keeps_plugin_tools(
        self, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        _write(
            home / "extensions" / "hello.py",
            "def setup(ctx):\n"
            "    @ctx.tool(name='hello_ext', description='d',"
            " parameters={'type': 'object', 'properties': {}})\n"
            "    def hello(args):\n"
            "        return 'hi'\n",
        )
        agent = _agent(tmp_path, enabled=True)
        assert "hello_ext" in {t.name for t in agent.registry.list_all()}

        import copy

        agent.pending_runtime_config = copy.deepcopy(agent.config)
        agent._apply_pending_runtime_config()

        names = {t.name for t in agent.registry.list_all()}
        assert "hello_ext" in names
        assert "code_run" in names
