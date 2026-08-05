"""Tests for core/handler.py — BaseHandler dispatch and do_no_tool."""

import pytest

from zero_agent.core.handler import BaseHandler
from zero_agent.core.types import EvidenceLedger, EvidenceRecord, StepAction, StepOutcome, TaskContract, TaskMode, TerminalStatus
from zero_agent.llm.base import MockResponse
from zero_agent.tools.registry import ToolDefinition, ToolRegistry


class TestBaseHandlerDispatch:
    """BaseHandler.dispatch() tests."""

    def test_dispatch_do_method(self, mock_handler: BaseHandler) -> None:
        """通过 do_<name> 方法分发."""
        # 注册一个 do_test 方法
        def do_test(self, args, response):
            yield "running test\n"
            return StepOutcome(
                {"result": "ok"},
                next_prompt="continue",
                action=StepAction.CONTINUE,
            )

        mock_handler.do_test = do_test.__get__(mock_handler)

        gen = mock_handler.dispatch("test", {}, MockResponse())
        result = _exhaust(gen)
        assert result.data == {"result": "ok"}
        assert result.next_prompt == "continue"

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
        assert "不存在的工具" in result.next_prompt
        assert "工具未执行" in result.next_prompt
        assert "provider-native tool_call" in result.next_prompt
        assert "不要猜工具名" in result.next_prompt

    def test_bad_json_retry_prompt_requires_regeneration(self, mock_handler: BaseHandler) -> None:
        """非法 tool_call JSON 不做 runtime 补正，只要求模型重发合法 native call."""
        result = _exhaust(mock_handler.do_bad_json(
            {"msg": "Failed to parse tool call JSON arguments: Expecting ','"},
            MockResponse(content=""),
        ))

        assert result.next_prompt is not None
        assert "工具未执行" in result.next_prompt
        assert "function.arguments 必须是合法 JSON object string" in result.next_prompt
        assert "不要在正文中解释或写工具协议" in result.next_prompt

    def test_dispatch_injects_meta(self, mock_handler: BaseHandler) -> None:
        """dispatch 注入 _index 和 _tool_num 元信息."""
        captured_args = {}

        def do_capture(self, args, response):
            captured_args.update(args)
            return StepOutcome(
                {"ok": True},
                next_prompt="continue",
                action=StepAction.CONTINUE,
            )

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
        """Registry handlers preserve explicit wait control."""

        def custom_handler(args, _response, _handler):
            yield "done\n"
            return StepOutcome(
                {"result": "interrupt"},
                action=StepAction.WAIT_FOR_USER,
                reason="human_intervention",
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
        assert result.next_prompt is None
        assert result.action is StepAction.WAIT_FOR_USER

    def test_real_registry_ask_user_waits(self, mock_config) -> None:
        """The built-in ask_user handler explicitly waits for the user."""
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "ask_user",
            {"question": "继续吗？", "candidates": ["yes", "no"]},
            MockResponse(),
        ))

        assert result.action is StepAction.WAIT_FOR_USER
        assert result.next_prompt is None
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

    def test_file_write_repeated_overwrite_uses_last_content(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """重复 overwrite 是显式重写，最终内容应来自最后一次调用。"""
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        _exhaust(handler.dispatch(
            "file_write",
            {"path": "out.txt", "content": "first", "mode": "overwrite"},
            None,
        ))
        _exhaust(handler.dispatch(
            "file_write",
            {"path": "out.txt", "content": "second", "mode": "overwrite"},
            None,
        ))

        assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "second"

    def test_file_write_repeated_append_and_prepend_are_not_deduplicated(
        self,
        mock_config,
        tmp_path,
    ) -> None:
        """append/prepend 的重复调用有语义，不能按路径去重。"""
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        _exhaust(handler.dispatch(
            "file_write",
            {"path": "append.txt", "content": "a", "mode": "append"},
            None,
        ))
        _exhaust(handler.dispatch(
            "file_write",
            {"path": "append.txt", "content": "b", "mode": "append"},
            None,
        ))
        _exhaust(handler.dispatch(
            "file_write",
            {"path": "prepend.txt", "content": "a", "mode": "prepend"},
            None,
        ))
        _exhaust(handler.dispatch(
            "file_write",
            {"path": "prepend.txt", "content": "b", "mode": "prepend"},
            None,
        ))

        assert (tmp_path / "append.txt").read_text(encoding="utf-8") == "ab"
        assert (tmp_path / "prepend.txt").read_text(encoding="utf-8") == "ba"

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

    @pytest.mark.parametrize("fence", [
        "```python\nprint('canonical fence')\n```",
        "``` python\nprint('space after fence')\n```",
    ])
    def test_real_registry_code_run_executes_common_reply_fences(
        self,
        mock_config,
        tmp_path,
        fence: str,
    ) -> None:
        """无 script 的常见 Markdown 围栏必须执行正文代码。"""
        mock_config.workspace_dir = str(tmp_path)
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=str(tmp_path))

        result = _exhaust(handler.dispatch(
            "code_run",
            {"type": "python"},
            MockResponse(content=fence),
        ))

        assert result.data["status"] == "success"
        assert "fence" in result.data["stdout"]

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

    def test_complete_task_finishes_open_answer_without_evidence(
        self,
        mock_config,
    ) -> None:
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)

        result = _exhaust(handler.dispatch(
            "complete_task",
            {"answer": "Here is the answer.", "evidence_refs": []},
            MockResponse(),
        ))

        assert result.action is StepAction.REQUEST_COMPLETION
        assert handler.completion_certificate is not None
        assert handler.completion_certificate.reason == "open_answer_completed"
        assert handler.task_contract.mode is TaskMode.OPEN

    def test_complete_task_requires_referenced_evidence_after_real_tool(
        self,
        mock_config,
    ) -> None:
        registry = ToolRegistry.with_builtins(mock_config)
        handler = BaseHandler(registry=registry, cwd=mock_config.workspace_dir)
        handler._record_evidence(
            "file_read",
            {"path": "config.py"},
            StepOutcome("content", next_prompt="continue"),
        )
        handler._mark_executing("file_read")

        rejected = _exhaust(handler.dispatch(
            "complete_task",
            {"answer": "Read it.", "evidence_refs": []},
            MockResponse(),
        ))
        accepted = _exhaust(handler.dispatch(
            "complete_task",
            {"answer": "Read it.", "evidence_refs": [1]},
            MockResponse(),
        ))

        assert rejected.action is StepAction.CONTINUE
        assert "evidence_refs" in rejected.next_prompt
        assert accepted.action is StepAction.REQUEST_COMPLETION
        assert handler.completion_certificate is not None
        assert handler.task_contract.mode is TaskMode.EXECUTING

    def test_dispatch_real_tool_promotes_open_to_executing(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        assert mock_handler.task_contract.mode is TaskMode.OPEN

        _exhaust(mock_handler.dispatch("echo", {"message": "hello"}, MockResponse()))

        assert mock_handler.task_contract.mode is TaskMode.EXECUTING

    def test_unknown_tool_does_not_promote_open_state(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _exhaust(mock_handler.dispatch("missing", {}, MockResponse()))

        assert mock_handler.task_contract.mode is TaskMode.OPEN


def _set_open_contract(handler: BaseHandler) -> None:
    handler.task_contract = TaskContract(
        task_id=handler.task_contract.task_id,
        user_request=handler.task_contract.user_request,
        mode=TaskMode.OPEN,
        plan_path=handler.task_contract.plan_path,
    )


def _set_execution_contract(handler: BaseHandler) -> None:
    handler.task_contract = TaskContract(
        task_id=handler.task_contract.task_id,
        user_request=handler.task_contract.user_request,
        mode=TaskMode.EXECUTING,
        plan_path=handler.task_contract.plan_path,
    )


def _add_success_evidence(handler: BaseHandler) -> None:
    handler._record_evidence(
        "file_read",
        {"path": "config.py"},
        StepOutcome("content", next_prompt="continue"),
    )


class TestBaseHandlerDoNoTool:
    """BaseHandler.do_no_tool() tests."""

    def test_empty_response_retries(self, mock_handler: BaseHandler) -> None:
        """空响应触发重试."""
        gen = mock_handler.do_no_tool({}, MockResponse(content=""))
        result = _exhaust(gen)
        assert result.next_prompt is not None
        assert "regenerate" in result.next_prompt.lower()

    def test_normal_response_requests_completion(self, mock_handler: BaseHandler) -> None:
        """A deliverable chat response requests typed completion."""
        _set_open_contract(mock_handler)
        gen = mock_handler.do_no_tool(
            {}, MockResponse(content="Task is done, here is the result."),
        )
        result = _exhaust(gen)
        assert result.next_prompt is None
        assert result.action is StepAction.REQUEST_COMPLETION
        assert mock_handler.completion_certificate is not None

    def test_normal_response_with_action_word_completes(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """普通最终回答里的 reading/checking 不应被误判为工具意图."""
        _set_open_contract(mock_handler)
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
        assert result.action is StepAction.REQUEST_COMPLETION

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
        assert "provider-native" in result.next_prompt
        assert result.data == {}

    def test_text_protocol_retry_prompt_excludes_provider_native(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        response = MockResponse(
            content='<tool_use>{"name":"file_read","arguments":[]}</tool_use>',
        )
        response.tool_protocol = "text"

        result = _exhaust(mock_handler.do_no_tool({}, response))

        assert result.next_prompt is not None
        assert '<tool_use>{"name":"file_read","arguments":{"path":"example.txt"}}</tool_use>' in result.next_prompt
        assert "provider-native" not in result.next_prompt.lower()
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

    def test_bare_chinese_action_with_file_names_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """省略主语的“看文件:”动作句不是最终答案，必须要求模型重发 tool_call."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(content="看后端核心文件 chat_context.py 和 config_registry.py："),
        )
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "没有任何工具被执行" in result.next_prompt
        assert "provider-native tool_call" in result.next_prompt

    def test_bare_chinese_continue_reading_file_retries(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """继续/再/先 + 动作 + 文件名 的短句应按契约错误重试."""
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(content="继续看 ChatPage.tsx 的加载逻辑："),
        )
        result = _exhaust(gen)

        assert result.next_prompt is not None
        assert "不要只重复计划" in result.next_prompt

    def test_completion_analysis_reply_does_not_trigger_tool_retry(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """Quoted protocol and action examples in a final answer are not instructions."""
        _set_open_contract(mock_handler)
        response = MockResponse(content=(
            "结论：当前结束逻辑包含以下规则。\n\n"
            "`CONTINUE` 表示继续下一轮；工具正常执行后进入下一状态。\n"
            "协议残留检测会拦截 `<tool_use>` 和 `<function_call>`。\n"
            "动作承诺检测会识别 `我将要查看/读取/执行`。\n"
            "以上是完整的判断逻辑。"
        ))

        result = _exhaust(mock_handler.do_no_tool({}, response))

        assert result.next_prompt is None
        assert result.action is StepAction.REQUEST_COMPLETION
        assert result.data is response

    def test_ambiguous_visible_chat_reply_completes_without_judge(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """A visible chat reply ends directly without a second LLM judgment."""
        _set_open_contract(mock_handler)
        response = MockResponse(content="我需要进一步判断这个问题。")

        result = _exhaust(mock_handler.do_no_tool({}, response))

        assert result.next_prompt is None
        assert result.action is StepAction.REQUEST_COMPLETION

    def test_greeting_help_reply_completes_without_retry(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """普通问候/能力介绍是可交付回复，不应被拉成第二轮."""
        _set_open_contract(mock_handler)
        response = MockResponse(content=(
            "你好！我是你的物理级全能执行者。\n"
            "我已就绪，可以帮你完成浏览器操控、文件系统操作、代码执行和 Web 搜索。\n"
            "有什么需要我帮忙的吗？直接说任务就行。"
        ))

        result = _exhaust(mock_handler.do_no_tool({}, response))

        assert result.next_prompt is None
        assert result.action is StepAction.REQUEST_COMPLETION
        assert result.data is response

    def test_user_clarification_question_completes_without_retry(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """向用户澄清/提问本身是可交付 no-tool 回复."""
        _set_open_contract(mock_handler)
        response = MockResponse(content="请问你想让我修改哪个文件？请提供路径或模块名。")

        result = _exhaust(mock_handler.do_no_tool({}, response))

        assert result.next_prompt is None
        assert result.action is StepAction.REQUEST_COMPLETION
        assert result.data is response

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

    def test_thinking_only_retries_with_specific_reason(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        args: dict = {}
        result = _exhaust(mock_handler.do_no_tool(
            args,
            MockResponse(content="", thinking="I should inspect files."),
        ))

        assert result.action is StepAction.CONTINUE
        assert result.next_prompt is not None
        assert args["_completion_gate"]["reason"] == "thinking_only_no_deliverable"

    def test_unknown_stop_reason_retries_without_completion(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        args: dict = {}
        result = _exhaust(mock_handler.do_no_tool(
            args,
            MockResponse(content="Done.", stop_reason="unknown:mystery"),
        ))

        assert result.action is StepAction.CONTINUE
        assert result.next_prompt is not None
        assert args["_completion_gate"]["reason"] == "unsafe_stop_reason"
        assert mock_handler.completion_certificate is None

    def test_execution_allow_uses_completion_evaluator_and_requires_evidence(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        result = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="Task is done, here is the result."),
        ))

        assert result.action is StepAction.CONTINUE
        assert result.next_prompt is not None
        assert "complete_task" in result.next_prompt
        assert mock_handler.completion_certificate is None

    def test_execution_plain_text_requires_explicit_complete_task(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)

        result = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="Config is valid."),
        ))

        assert result.action is StepAction.CONTINUE
        assert result.next_prompt is not None
        assert "complete_task" in result.next_prompt
        assert mock_handler.completion_certificate is None

    def test_dispatch_records_success_evidence_for_real_tools(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _exhaust(mock_handler.dispatch(
            "echo",
            {"message": "hello"},
            MockResponse(),
        ))

        assert len(mock_handler.evidence_ledger.records) == 1
        record = mock_handler.evidence_ledger.records[0]
        assert record.tool_name == "echo"
        assert record.status == "unknown"
        assert record.kind == "system"

    def test_bad_json_does_not_record_evidence(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _exhaust(mock_handler.do_bad_json(
            {"msg": "bad"},
            MockResponse(),
        ))

        assert mock_handler.evidence_ledger.records == []

    def test_enter_plan_mode_updates_task_contract(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        result = mock_handler.enter_plan_mode("plan.md")

        assert result == "plan.md"
        assert mock_handler.task_contract.mode is TaskMode.PLAN
        assert mock_handler.task_contract.plan_path == "plan.md"
        assert mock_handler.plan_verify_status == "missing"

    def test_plan_state_uses_contract_as_single_source_of_truth(
        self,
        mock_handler: BaseHandler,
        tmp_path,
    ) -> None:
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("- [ ] pending\n", encoding="utf-8")
        mock_handler.enter_plan_mode(str(plan_path))

        assert mock_handler._in_plan_mode() is True
        assert mock_handler._check_plan_completion() == 1
        assert mock_handler.task_contract.plan_path == str(plan_path)

    def test_plan_is_terminal_upgrade_without_exit_method(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        mock_handler.enter_plan_mode("plan.md")
        mock_handler._mark_executing("file_read")

        assert mock_handler.task_contract.mode is TaskMode.PLAN
        assert not hasattr(mock_handler, "_exit_plan_mode")

    def test_plan_plain_text_requires_explicit_complete_task(
        self,
        mock_handler: BaseHandler,
        tmp_path,
    ) -> None:
        plan_path = tmp_path / "plan.md"
        plan_path.write_text("- [x] implemented\n", encoding="utf-8")
        mock_handler.enter_plan_mode(str(plan_path))

        result = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="Plan verified and complete."),
        ))

        assert result.action is StepAction.CONTINUE
        assert result.next_prompt is not None
        assert "complete_task" in result.next_prompt
        assert mock_handler.completion_certificate is None

    def test_anchor_prompt_includes_contract_and_recent_evidence(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)

        prompt = mock_handler._build_anchor_prompt()

        assert "<task_contract>" in prompt
        assert "state: TaskMode.EXECUTING" in prompt
        assert "recent_evidence:" in prompt
        assert "tool=file_read" in prompt
        assert "ref=1" in prompt

    def test_completion_evidence_catalog_formats_global_refs(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        mock_handler.evidence_ledger = EvidenceLedger(records=[
            EvidenceRecord(1, "file_read", "success", "read", "read config"),
            EvidenceRecord(2, "code_run", "error", "execute", "failed tests"),
            EvidenceRecord(3, "file_write", "success", "write", "write output"),
            EvidenceRecord(4, "echo", "success", "system", "ignored system"),
        ])

        assert mock_handler._completion_evidence_catalog() == (
            "Available successful evidence_refs:\n"
            "ref=1 turn=1 tool=file_read kind=read summary=read config\n"
            "ref=3 turn=3 tool=file_write kind=write summary=write output"
        )

        mock_handler.evidence_ledger = EvidenceLedger(records=[{
            "turn": 5,
            "tool_name": "web_scan",
            "status": "success",
            "kind": "web",
            "summary": "dict result",
        }])
        assert mock_handler._completion_evidence_catalog() == (
            "Available successful evidence_refs:\n"
            "ref=1 turn=5 tool=web_scan kind=web summary=dict result"
        )
        checkpoint = mock_handler._contract_checkpoint()
        assert (
            "ref=1 turn=5 tool=web_scan status=success kind=web "
            "summary=dict result"
        ) in checkpoint

        mock_handler.evidence_ledger = EvidenceLedger()
        assert mock_handler._completion_evidence_catalog() == (
            "Available successful evidence_refs: none. Collect successful "
            "read/write/execute/web/verify evidence before retrying."
        )

    def test_complete_task_rejection_budget_exits_and_successful_tool_resets(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)

        first = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))
        second = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))
        third = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))

        assert first.action is StepAction.CONTINUE
        assert second.action is StepAction.CONTINUE
        assert third.action is StepAction.FAIL
        assert third.terminal_status is TerminalStatus.PROTOCOL_ERROR
        assert third.reason == "complete_task_retry_limit"

        def failed_correction(_args, _response, _handler):
            if False:
                yield None
            return {"status": "error"}

        mock_handler.registry.register(ToolDefinition(
            name="failed_correction",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=failed_correction,
        ))
        _exhaust(mock_handler.dispatch("failed_correction", {}, MockResponse()))
        after_failed = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))
        assert after_failed.action is StepAction.FAIL

        def successful_correction(_args, _response, _handler):
            if False:
                yield None
            return {"status": "success"}

        mock_handler.registry.register(ToolDefinition(
            name="successful_correction",
            description="",
            parameters={"type": "object", "properties": {}},
            handler=successful_correction,
        ))
        _exhaust(mock_handler.dispatch("successful_correction", {}, MockResponse()))
        after_success = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))
        assert after_success.action is StepAction.CONTINUE

    def test_echo_does_not_reset_completion_rejection_budget(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)
        for _ in range(2):
            outcome = _exhaust(mock_handler.dispatch(
                "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
            ))
            assert outcome.action is StepAction.CONTINUE

        _exhaust(mock_handler.dispatch("echo", {"message": "retry"}, MockResponse()))
        third = _exhaust(mock_handler.dispatch(
            "complete_task", {"answer": "Done", "evidence_refs": [99]}, MockResponse(),
        ))

        assert third.action is StepAction.FAIL
        assert third.terminal_status is TerminalStatus.PROTOCOL_ERROR
        assert third.reason == "complete_task_retry_limit"

    def test_non_array_completion_refs_use_rejection_budget(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        _set_execution_contract(mock_handler)
        _add_success_evidence(mock_handler)

        outcomes = [
            _exhaust(mock_handler.dispatch(
                "complete_task", {"answer": "Done", "evidence_refs": value}, MockResponse(),
            ))
            for value in (99, {"ref": 1}, "1")
        ]

        assert outcomes[0].action is StepAction.CONTINUE
        assert "Available successful evidence_refs:" in outcomes[0].next_prompt
        assert outcomes[1].action is StepAction.CONTINUE
        assert outcomes[2].action is StepAction.FAIL
        assert outcomes[2].terminal_status is TerminalStatus.PROTOCOL_ERROR
        assert outcomes[2].reason == "complete_task_retry_limit"

    def test_action_intent_retry_budget_exits(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """动作承诺纠正最多重试两次，第三次硬停止（action_retry_limit=2）."""
        first = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        ))
        second = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="我来查看配置文件。"),
        ))
        third = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="需要读取配置文件。"),
        ))

        assert first.action is StepAction.CONTINUE
        assert first.next_prompt is not None
        assert second.action is StepAction.CONTINUE
        assert second.next_prompt is not None
        assert third.action is StepAction.FAIL
        assert third.terminal_status is TerminalStatus.BUDGET_EXHAUSTED
        assert third.reason == "promissory_action_limit"


    def test_narrative_with_action_phrase_completes(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """叙述性回复含动作短语但后续有大段正文 → 不应被误判为未执行动作."""
        _set_open_contract(mock_handler)
        gen = mock_handler.do_no_tool(
            {},
            MockResponse(
                content=(
                    "好的，让我检查一下最终结果...\n\n"
                    "经过验证，所有文件已正确写入，测试全部通过。"
                    "这是本次任务的完整总结：1) 修复了 config 解析问题；"
                    "2) 新增了单元测试覆盖边界情况。任务已完成。"
                ),
            ),
        )
        result = _exhaust(gen)

        assert result.next_prompt is None, (
            "叙述性回复含大段正文不应被当作未执行动作意图"
        )
        assert result.action is StepAction.REQUEST_COMPLETION

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

        assert first.action is StepAction.CONTINUE
        assert second.action is StepAction.CONTINUE
        assert third.action is StepAction.FAIL
        assert third.terminal_status is TerminalStatus.PROTOCOL_ERROR
        assert third.reason == "text_tool_protocol_limit"

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
        assert second.action is StepAction.CONTINUE
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
            terminal=None,
        )

        assert "promissory_action" in mock_handler.history_info[-1]
        assert "直接回答" not in mock_handler.history_info[-1]

    def test_three_empty_retries_fail_budget(self, mock_handler: BaseHandler) -> None:
        """Three empty responses explicitly fail the retry budget."""
        mock_handler._empty_ct = 2
        result = mock_handler._retry_or_exit("retry")
        assert result.action is StepAction.FAIL
        assert result.terminal_status is TerminalStatus.BUDGET_EXHAUSTED
        assert result.reason == "blank_response_limit"
        assert mock_handler._empty_ct == 3

    def test_retry_or_exit_increments(self, mock_handler: BaseHandler) -> None:
        """_retry_or_exit 递增计数器."""
        mock_handler._empty_ct = 0
        result = mock_handler._retry_or_exit("retry")
        assert result.action is StepAction.CONTINUE
        assert mock_handler._empty_ct == 1

    def test_reset_session_state_clears_empty_ct_and_budget(
        self,
        mock_handler: BaseHandler,
    ) -> None:
        """新任务开始时 _empty_ct 和 completion_gate retry 预算被归零."""
        mock_handler._empty_ct = 2
        # 消耗 promissory_action 预算
        _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        ))

        mock_handler.reset_session_state()

        # _empty_ct 归零
        assert mock_handler._empty_ct == 0
        # completion_gate retry 计数清空，下一轮不应趁势退出
        result = _exhaust(mock_handler.do_no_tool(
            {},
            MockResponse(content="让我检查项目配置情况。"),
        ))
        assert result.action is StepAction.CONTINUE


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
