"""Tests for core/handler.py — BaseHandler dispatch and do_no_tool."""

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import StepOutcome
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


class TestBaseHandlerDispatch:
    """BaseHandler.dispatch() tests."""

    def test_dispatch_do_method(self, mock_handler: BaseHandler) -> None:
        """通过 do_<name> 方法分发."""
        # 注册一个 do_test 方法
        def do_test(self, args, response):
            yield "running test\n"
            return StepOutcome({"result": "ok"}, next_prompt="")

        mock_handler.do_test = do_test.__get__(mock_handler)

        gen = mock_handler.dispatch("test", {}, MockResponse())
        result = _exhaust(gen)
        assert result.data == {"result": "ok"}
        assert result.next_prompt == ""

    def test_dispatch_registry_fallback(self, mock_handler: BaseHandler) -> None:
        """回退到 ToolRegistry 中的 handler."""
        gen = mock_handler.dispatch(
            "echo", {"message": "hello"}, MockResponse(),
        )
        result = _exhaust(gen)
        assert result.data == {"result": "hello"}

    def test_dispatch_unknown_tool(self, mock_handler: BaseHandler) -> None:
        """未知工具返回错误提示."""
        gen = mock_handler.dispatch(
            "nonexistent_tool", {}, MockResponse(),
        )
        result = _exhaust(gen)
        assert result.data is None
        assert "未知工具" in result.next_prompt

    def test_dispatch_injects_meta(self, mock_handler: BaseHandler) -> None:
        """dispatch 注入 _index 和 _tool_num 元信息."""
        captured_args = {}

        def do_capture(self, args, response):
            captured_args.update(args)
            return StepOutcome({"ok": True}, next_prompt="")

        mock_handler.do_capture = do_capture.__get__(mock_handler)

        _exhaust(mock_handler.dispatch(
            "capture", {"custom": "val"}, MockResponse(),
            index=2, tool_num=5,
        ))
        assert captured_args["custom"] == "val"
        assert captured_args["_index"] == 2
        assert captured_args["_tool_num"] == 5

    def test_dispatch_registry_za_next_prompt(self, mock_handler: BaseHandler) -> None:
        """registry tool handler 通过 _za_next_prompt 设置自定义 next_prompt."""
        def custom_handler(args, _response, _handler):
            yield "done\n"
            return {"result": "ok", "_za_next_prompt": "custom prompt"}

        mock_handler.registry.register(ToolDefinition(
            name="custom",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=custom_handler,
        ))

        gen = mock_handler.dispatch("custom", {}, MockResponse())
        result = _exhaust(gen)
        assert result.next_prompt == "custom prompt"

    def test_dispatch_preserves_registry_step_outcome(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """registry handler 可直接返回 StepOutcome 并保留 should_exit."""
        def custom_handler(args, _response, _handler):
            yield "done\n"
            return StepOutcome(
                {"result": "interrupt"},
                next_prompt="",
                should_exit=True,
            )

        mock_handler.registry.register(ToolDefinition(
            name="custom_exit",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=custom_handler,
        ))

        result = _exhaust(mock_handler.dispatch(
            "custom_exit", {}, MockResponse(),
        ))

        assert result.data == {"result": "interrupt"}
        assert result.next_prompt == ""
        assert result.should_exit is True

    def test_real_registry_ask_user_exits(self, mock_config) -> None:
        """真实 ToolRegistry 分发 ask_user 时必须让 loop 退出."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "ask_user",
            {"question": "继续吗？", "candidates": ["yes", "no"]},
            MockResponse(),
        ))

        assert result.should_exit is True
        assert result.next_prompt == ""
        assert result.data["status"] == "INTERRUPT"
        assert result.data["data"]["question"] == "继续吗？"

    def test_real_registry_file_write_with_native_content_succeeds(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """file_write 只接受 native tool arguments 中的 content."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)
        target = tmp_path / "out.txt"

        result = _exhaust(handler.dispatch(
            "file_write",
            {
                "path": str(target),
                "mode": "overwrite",
                "content": "native content\n",
            },
            MockResponse(content=""),
        ))

        assert result.data["status"] == "success"
        assert "### [WORKING MEMORY]" in result.next_prompt
        assert target.read_text(encoding="utf-8") == "native content\n"

    def test_real_registry_file_write_missing_content_requires_native_arg(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """file_write 缺 content 时不再从正文或代码块回退提取."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)
        target = tmp_path / "out.txt"

        result = _exhaust(handler.dispatch(
            "file_write",
            {"path": str(target), "mode": "overwrite"},
            MockResponse(
                content=(
                    "我准备写入文件。\n"
                    "<file_content>should not write</file_content>\n"
                    "```text\nalso should not write\n```"
                ),
            ),
        ))

        assert result.data["status"] == "error"
        assert "content argument is required" in result.data["msg"]
        assert result.next_prompt == "\n"
        assert not target.exists()

    def test_real_registry_code_run_missing_script_matches_ga(
        self,
        mock_config,
    ) -> None:
        """code_run 缺 script/代码块时应返回错误并轻量续写."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "code_run",
            {},
            MockResponse(content="我准备运行代码。"),
        ))

        assert result.data == (
            "[Error] Code missing. Must use reply code block or 'script' arg."
        )
        assert result.next_prompt == "\n"

    def test_real_registry_code_run_does_not_accept_code_alias(
        self,
        mock_config,
    ) -> None:
        """ZA 不再保留 code_run 的 code 参数别名."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "code_run",
            {"code": "print('alias should not execute')"},
            MockResponse(content="我准备运行代码。"),
        ))

        assert result.data == (
            "[Error] Code missing. Must use reply code block or 'script' arg."
        )
        assert result.next_prompt == "\n"

    def test_real_registry_file_patch_bad_ref_uses_blank_next_prompt(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """file_patch 引用展开失败时应使用空白续写提示."""
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "file_patch",
            {
                "path": str(tmp_path / "target.txt"),
                "old_content": "old",
                "new_content": "{{file:missing.txt:1:2}}",
            },
            MockResponse(content=""),
        ))

        assert result.data["status"] == "error"
        assert result.next_prompt == "\n"

    def test_real_registry_file_read_memory_tip_is_next_prompt(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """读取 memory/SOP 文件时，提示应进 next_prompt 而非污染工具结果."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        sops_dir = memory_dir / "sops"
        sops_dir.mkdir()
        sop = sops_dir / "plan_sop.md"
        sop.write_text("step one\nstep two\n", encoding="utf-8")
        mock_config.memory_dir = str(memory_dir)
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "file_read",
            {"path": str(sop), "count": 5},
            MockResponse(content=""),
        ))

        assert isinstance(result.data, str)
        assert "step one" in result.data
        assert "SYSTEM TIPS" not in result.data
        assert "SYSTEM TIPS" in result.next_prompt
        assert result.next_prompt.startswith("\n### [WORKING MEMORY]")

    def test_real_registry_file_read_uses_line_number_prefix(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        target = tmp_path / "source.txt"
        target.write_text("alpha\nbeta\n", encoding="utf-8")
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "file_read",
            {"path": str(target), "count": 5, "show_linenos": True},
            MockResponse(content=""),
        ))

        assert isinstance(result.data, str)
        assert result.data.startswith(
            "由于设置了show_linenos，以下返回信息为：(行号|)内容 。\n"
        )

    def test_real_registry_file_read_sop_path_tip_uses_memory_heuristic(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        sop_dir = tmp_path / "outside_sop"
        sop_dir.mkdir()
        sop = sop_dir / "guide.md"
        sop.write_text("follow this\n", encoding="utf-8")
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        mock_config.memory_dir = str(memory_dir)
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "file_read",
            {"path": str(sop), "count": 5},
            MockResponse(content=""),
        ))

        assert "SYSTEM TIPS" not in result.data
        assert "SYSTEM TIPS" in result.next_prompt
        assert result.next_prompt.startswith("\n### [WORKING MEMORY]")

    def test_file_read_memory_alias_resolves_outside_workspace(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """memory/... paths should resolve to config.memory_dir, not workspace/memory."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        memory_dir = tmp_path / "memory"
        sop_dir = memory_dir / "sops"
        sop_dir.mkdir(parents=True)
        (sop_dir / "goal_mode_sop.md").write_text("goal body\n", encoding="utf-8")
        mock_config.workspace_dir = str(workspace)
        mock_config.memory_dir = str(memory_dir)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(workspace))

        result = _exhaust(handler.dispatch(
            "file_read",
            {"path": "memory/sops/goal_mode_sop.md", "count": 5},
            MockResponse(content=""),
        ))

        assert "goal body" in result.data
        assert "File not found" not in result.data

    def test_real_registry_start_long_term_update_includes_global_memory(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """start_long_term_update 的结算 prompt 应包含全局记忆上下文."""
        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()
        sops_dir = memory_dir / "sops"
        sops_dir.mkdir()
        (sops_dir / "memory_management_sop.md").write_text(
            "# Memory SOP\nread first\n",
            encoding="utf-8",
        )
        (memory_dir / "global_mem_insight.txt").write_text(
            "# Insight\nL2: facts\n",
            encoding="utf-8",
        )
        mock_config.memory_dir = str(memory_dir)
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "start_long_term_update",
            {},
            MockResponse(content=""),
        ))

        assert "This is L0" in result.data
        assert "总结提炼经验" in result.next_prompt
        assert "global_mem_insight.txt" in result.next_prompt
        assert "# Insight" in result.next_prompt


class TestBaseHandlerDoNoTool:
    """BaseHandler.do_no_tool() tests."""

    def test_empty_response_retries(self, mock_handler: BaseHandler) -> None:
        """空响应触发重试."""
        gen = mock_handler.do_no_tool({}, MockResponse(content=""))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "regenerate" in result.next_prompt.lower()

    def test_normal_response_completes(self, mock_handler: BaseHandler) -> None:
        """正常文本回复 → 任务完成（next_prompt=None）."""
        gen = mock_handler.do_no_tool(
            {}, MockResponse(content="Task is done, here is the result."),
        )
        result = _exhaust(gen)
        assert result.next_prompt is None

    def test_normal_response_with_action_word_completes(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """普通最终回答里的 reading/checking 不应被误判为工具意图."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(
                content=(
                    "Reading the existing context, the requested change is "
                    "complete. The checking logic is now covered by tests."
                ),
            ),
        )
        result = _exhaust(gen)

        assert result.next_prompt is None

    def test_incomplete_response_retries(self, mock_handler: BaseHandler) -> None:
        """流异常中断触发重试."""
        content = "some text [!!! 流异常中断 in the end"
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "incomplete" in result.next_prompt.lower()

    def test_long_immediate_error_retries(self, mock_handler: BaseHandler) -> None:
        """长错误响应也由共享中断判定触发重试."""
        content = "!!!Error: backend failed " + ("x" * 200)
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "incomplete" in result.next_prompt.lower()

    def test_max_tokens_retries(self, mock_handler: BaseHandler) -> None:
        """max_tokens 截断触发重试."""
        content = "some text max_tokens !!!] in the last part"
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "max_tokens" in result.next_prompt.lower()

    def test_length_stop_reason_retries(self, mock_handler: BaseHandler) -> None:
        """stop_reason=length 也视为 max_tokens 类截断."""
        gen = mock_handler.do_no_tool(
            {}, MockResponse(content="partial answer", stop_reason="length")
        )
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "max_tokens" in result.next_prompt.lower()

    def test_code_block_without_tool_triggers_prompt(self, mock_handler: BaseHandler) -> None:
        """大代码块未调用工具时提示 LLM 调用工具."""
        # 需要 50+ 字符的代码内容才能匹配 code_block_pattern
        content = (
            "```python\n"
            + "import os\nimport sys\nimport json\nprint('hello world')\nprint('done')\n"
            + "```"
        )
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)
        # 应该触发提示
        assert result.next_prompt is not None
        assert "代码" in result.next_prompt

    def test_text_tool_protocol_without_native_call_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """正文里的工具协议 tag 不能被当成完成回复."""
        content = (
            '准备读取。\n<tool_use>{"name":"file_read",'
            '"arguments":{"path":"config.py"}}</tool_use>'
        )
        gen = mock_handler.do_no_tool({}, MockResponse(content=content))
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "native" in result.next_prompt.lower()
        assert result.data == {}

    def test_file_content_without_native_call_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """裸 <file_content> 是协议错误，不是可执行 side-channel."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(content="<file_content>x = 1</file_content>"),
        )
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "file_content" in result.next_prompt
        assert result.data == {}

    def test_action_intent_without_native_call_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """只说要检查/读取但未发出 native tool_calls 时继续催促工具调用."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        )
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "tool" in result.next_prompt.lower()
        assert result.data == {}

    def test_english_future_action_intent_without_native_call_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """明确未来动作意图仍会被 native-only guard 拦截."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(content="Sure, let me check the project config."),
        )
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "native" in result.next_prompt.lower()
        assert result.data == {}

    def test_action_intent_retry_budget_exits(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """动作承诺纠正只重试一次，避免无工具纠正循环."""
        first = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        ))
        second = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="我来查看配置文件。"),
        ))

        assert first.should_exit is False
        assert first.next_prompt is not None
        assert second.should_exit is True
        assert second.next_prompt is None

    def test_text_tool_protocol_retry_budget_exits(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """旧文本工具协议最多纠正两次，第三次硬停止."""
        content = (
            '<tool_use>{"name":"file_read","arguments":{"path":"x"}}</tool_use>'
        )

        first = _exhaust(mock_handler.do_no_tool({}, MockResponse(content=content)))
        second = _exhaust(mock_handler.do_no_tool({}, MockResponse(content=content)))
        third = _exhaust(mock_handler.do_no_tool({}, MockResponse(content=content)))

        assert first.should_exit is False
        assert second.should_exit is False
        assert third.should_exit is True

    def test_native_tool_call_resets_completion_gate_budget(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """成功进入 native 工具分发后，completion gate 的纠正预算重置."""
        first = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        ))
        assert first.next_prompt is not None

        _exhaust(mock_handler.dispatch(
            "echo",
            {"message": "ok"},
            MockResponse(content="", tool_calls=[]),
        ))

        second = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="我来查看配置文件。"),
        ))
        assert second.should_exit is False
        assert second.next_prompt is not None

    def test_no_tool_retry_is_annotated_for_turn_summary(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """gate retry 的 no_tool 轮次不应记为直接回答用户."""
        args: dict = {}
        result = _exhaust(mock_handler.do_no_tool(
            args,
            MockResponse(content="让我检查项目配置情况。"),
        ))
        assert result.next_prompt is not None

        mock_handler.turn_end_callback(
            MockResponse(content="让我检查项目配置情况。"),
            [{"tool_name": "no_tool", "args": args}],
            [],
            turn=1,
            next_prompt=result.next_prompt,
            exit_reason={},
        )

        assert "promissory_action" in mock_handler.history_info[-1]
        assert "直接回答" not in mock_handler.history_info[-1]

    def test_three_empty_retries_exits(self, mock_handler: BaseHandler) -> None:
        """连续 3 次空响应 → should_exit=True."""
        mock_handler._empty_ct = 2
        result = mock_handler._retry_or_exit("retry")
        # _retry_or_exit 直接返回 StepOutcome（不是 generator）
        assert result.should_exit is True
        assert mock_handler._empty_ct == 3

    def test_retry_or_exit_increments(self, mock_handler: BaseHandler) -> None:
        """_retry_or_exit 递增计数器."""
        mock_handler._empty_ct = 0
        result = mock_handler._retry_or_exit("retry")
        assert result.should_exit is False
        assert mock_handler._empty_ct == 1


class TestBaseHandlerWorking:
    """BaseHandler working memory tests."""

    def test_default_next_prompt_with_key_info(self, mock_handler: BaseHandler) -> None:
        mock_handler.working["key_info"] = "Important context"
        prompt = mock_handler._default_next_prompt({})
        assert prompt.startswith("\n### [WORKING MEMORY]")
        assert "Important context" in prompt
        assert "<key_info>" in prompt
        assert "<history>\n\n</history>" in prompt

    def test_default_next_prompt_skip(self, mock_handler: BaseHandler) -> None:
        """_index > 0 时仅返回空白续写提示."""
        mock_handler.working["key_info"] = "ctx"
        prompt = mock_handler._default_next_prompt({"_index": 1})
        assert "[System] Continue" not in prompt
        assert prompt == "\n"

    def test_build_anchor_prompt_with_history(self, mock_handler: BaseHandler) -> None:
        """_build_anchor_prompt 包含历史和 working memory."""
        mock_handler.history_info = ["[Agent] did something"]
        mock_handler.working["key_info"] = "test ctx"
        anchor = mock_handler._build_anchor_prompt()
        assert anchor.startswith("\n### [WORKING MEMORY]")
        assert "did something" in anchor
        assert "<key_info>test ctx</key_info>" in anchor
        assert "<history>" in anchor

    def test_fold_history_keeps_tail_limit(self) -> None:
        lines = [f"[USER] task {i}" for i in range(75)]
        folded = BaseHandler._fold_history(lines)
        folded_lines = folded.splitlines()

        assert "[USER] task 4" not in folded_lines
        assert "[USER] task 5" in folded_lines
        assert "[USER] task 74" in folded_lines

    def test_fold_history_compresses(self, mock_handler: BaseHandler) -> None:
        """_fold_history 压缩连续 agent 轮次."""
        lines = [
            "[Agent] called echo",
            "[Agent] called echo",
            "[Agent] called echo",
            "[USER] new task",
            "[Agent] called add",
        ]
        folded = BaseHandler._fold_history(lines)
        assert "3 turns" in folded
        assert "[USER] new task" in folded


def _exhaust(gen):
    """消费 generator 并返回最终值."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value
