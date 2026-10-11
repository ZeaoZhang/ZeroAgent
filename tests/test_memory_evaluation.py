"""Offline regressions for source-aware memory evaluation; no model calls."""

import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest

from zero_agent.core.config import AgentConfig, LLMBackendConfig
from zero_agent.core.hooks import HookSystem
from zero_agent.core.types import TerminalEvent, TerminalStatus
from zero_agent.evaluation import memory as evaluation
from zero_agent.evaluation.evidence import (
    historical_tool_evidence,
    observed_tool_results,
    trace_context,
)


@pytest.fixture
def case():
    cases, _ = evaluation.load_cases()
    return copy.deepcopy(cases[0])


def phase(response="", *, trace=None, status="completed"):
    return {
        "response": response, "terminal_status": status, "terminal_reason": "test",
        "elapsed_ms": 1, "usage": {"requests": 1, "input_tokens": 2, "output_tokens": 3},
        "trace": trace or [], "events": ["stream text"],
    }


def read_trace(content):
    return [{
        "event": "turn_after", "turn": 1,
        "tool_calls": [{"id": "read", "tool_name": "file_read", "args": {"path": "global_mem.txt"}}],
        "tool_results": [{"tool_use_id": "read", "content": content}],
    }]


def run_case(case, tmp_path, monkeypatch, runner, metric="keyword-recall"):
    monkeypatch.setattr(evaluation, "_run_agent_phase", runner)
    return evaluation._run_case(
        case, base_config=AgentConfig(language="zh"), run_dir=tmp_path,
        metric=metric, judge_backend_name="judge", judge_backend_config=SimpleNamespace(model="judge"),
    )


def test_bundled_cases_have_verified_sources_and_no_seed_answers():
    cases, hashes = evaluation.load_cases()
    assert Counter(c["category"] for c in cases) == {"layer1": 3, "layer2": 3, "layer3": 3}
    assert hashes and evaluation.validate_dataset(cases) == {}
    assert evaluation.validate_dataset(cases, "en") == {}
    for case in cases:
        for history in case["conversation_histories"]:
            assert historical_tool_evidence(history)
            if history["memory_action"] != "discard":
                assert history["requires_tool_evidence"]


@pytest.mark.parametrize("result", [
    {"status": "error", "exit_code": 0}, {"status": "failed"},
    {"status": "success", "exit_code": 1}, {"exit_code": False},
    {"status": "interrupt", "exit_code": 0},
    {"stdout": "I verified it"},
])
def test_failed_or_unverified_tools_are_not_success(case, result):
    history = case["conversation_histories"][0]
    history["messages"][2]["content"] = json.dumps(result)
    assert historical_tool_evidence(history) == []


@pytest.mark.parametrize("mutation", ["unmatched", "duplicate_result", "duplicate_call", "bad_call"])
def test_tool_evidence_requires_unique_linked_calls(case, mutation):
    history = case["conversation_histories"][0]
    if mutation == "unmatched":
        history["messages"][2]["tool_call_id"] = "absent"
    elif mutation == "duplicate_result":
        history["messages"].append(copy.deepcopy(history["messages"][2]))
    elif mutation == "duplicate_call":
        history["messages"].append(copy.deepcopy(history["messages"][1]))
    else:
        history["messages"][1]["tool_calls"] = ["invalid"]
    with pytest.raises(ValueError):
        historical_tool_evidence(history)


def test_loader_rejects_prose_in_place_of_required_tool_evidence(case, tmp_path):
    case["conversation_histories"][0]["messages"] = [{"role": "assistant", "content": "I checked this."}]
    path = tmp_path / "case.json"
    path.write_text(json.dumps(case))
    with pytest.raises(ValueError, match="successful tool result"):
        evaluation.load_cases(path)


@pytest.mark.parametrize("data", [[], ["invalid case"]])
def test_loader_rejects_empty_or_malformed_case_lists(data, tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="case objects"):
        evaluation.load_cases(path)


def test_preflight_detects_policy_conflict_and_seed_answer(case):
    case["conversation_histories"][0]["messages"] = [{"role": "assistant", "content": "Verified."}]
    case["required_memory_facts"] = ["memory_management_sop.md"]
    assert set(evaluation.validate_dataset([case])[case["test_id"]]) == {
        "required_facts_present_in_initial_memory", "retention_history_without_successful_tool_evidence",
    }
    case["evaluation_kind"] = "architecture"
    assert evaluation.validate_dataset([case]) == {}


def test_prompts_do_not_leak_gold_and_judge_has_separate_sources(case):
    case["expected_behavior"] = "GOLD_ONLY_SENTINEL"
    case["required_answer_facts"] = ["GOLD_ONLY_SENTINEL"]
    assert "GOLD_ONLY_SENTINEL" not in evaluation._history_prompt(case["conversation_histories"][0], 1)
    assert "GOLD_ONLY_SENTINEL" not in evaluation._question_prompt(case)
    messages = evaluation._judge_prompt(
        case, "No saved fact", "generated memory", initial_memory="SEED_SENTINEL",
        question_memory="EMPTY_STATE_SENTINEL", observations=[{"content": "READ_SENTINEL"}],
    )
    payload = json.loads(messages[1]["content"].split("评测数据（JSON）：\n", 1)[1])
    assert payload["authoritative_initial_memory"] == "SEED_SENTINEL"
    assert payload["question_memory_state_non_authoritative"] == "EMPTY_STATE_SENTINEL"
    assert payload["memory_artifact_non_authoritative"] == "generated memory"
    assert payload["question_tool_observations"][0]["content"] == "READ_SENTINEL"
    assert "不属于幻觉" in messages[0]["content"]
    assert "引用初始化规则" in messages[1]["content"]


def test_observations_exclude_model_claims_and_write_arguments():
    trace = read_trace("actual read")
    trace[0]["response"] = {"content": "hallucinated fact"}
    trace[0]["tool_calls"].append({"id": "write", "tool_name": "file_write", "args": {"content": "invented"}})
    trace[0]["tool_results"].append({"tool_use_id": "write", "content": "write result"})
    assert [r["content"] for r in observed_tool_results(trace)] == ["actual read"]


def test_trace_is_immutable_and_excludes_raw_credentials():
    context = {"messages": [{"content": "before"}], "api_key": "SECRET", "raw_headers": {"token": "SECRET"}}
    trace = trace_context("llm_before", context)
    context["messages"][0]["content"] = "after"
    assert trace["messages"][0]["content"] == "before"
    assert "SECRET" not in json.dumps(trace)


def test_agent_phase_captures_hooks_and_every_yield_before_compaction(tmp_path, monkeypatch):
    class FakeAgent:
        def __init__(self, config):
            self.hooks = HookSystem()
            self._sessions = {}
            assert config.failover_backends == []
            assert config.sessions_dir == "" and config.langfuse is None

        def set_config_path(self, path):
            assert path is None

        def run(self, prompt):
            context = {"messages": [{"content": "original"}]}
            self.hooks.trigger("llm_before", context)
            yield "first"
            self.hooks.trigger("turn_after", read_trace("observed")[0])
            context["messages"].clear()
            yield "second"
            return TerminalEvent(status=TerminalStatus.COMPLETED, text="answer")

    monkeypatch.setattr(evaluation, "ZeroAgent", FakeAgent)
    result = evaluation._run_agent_phase(
        AgentConfig(failover_backends=["fallback"]), workspace_dir=tmp_path / "work",
        memory_dir=tmp_path / "memory", prompt="prompt",
    )
    assert result["events"] == ["first", "second"]
    assert result["trace"][0]["messages"] == [{"content": "original"}]
    assert observed_tool_results(result["trace"])[0]["content"] == "observed"


def test_stage_diagnostics_distinguish_write_read_and_answer_gaps(case):
    case["required_memory_facts"] = ["unique-A", "unique-B", "unique-C"]
    case["required_answer_facts"] = ["unique-A", "unique-B", "unique-C"]
    result = evaluation._stage_diagnostics(
        case, initial_memory="", question_memory="unique-B unique-C", answer="",
        observations=[{"content": "unique-C"}], question_insight="",
    )
    assert [f["stage"] for f in result["fact_stages"]] == [
        "missing_before_question", "not_observed_in_question_reads", "observed_in_question_reads",
    ]
    assert result["new_memory_fact_recall"] == pytest.approx(2 / 3)
    assert result["answer_facts_missing"] == ["unique-A", "unique-B", "unique-C"]
    assert "does not prove retrieval failure" in result["observation_note"]


def test_case_preserves_initial_and_prequestion_snapshots(case, tmp_path, monkeypatch):
    workspaces = []

    def runner(config, *, workspace_dir, memory_dir, prompt):
        workspaces.append(workspace_dir)
        if workspace_dir.name != "question":
            (memory_dir / "global_mem.txt").write_text("OrionEval artifact_retention_days = 43")
            return phase()
        assert "conversation_id" not in prompt and "evaluation_criteria" not in prompt
        return phase("43 days", trace=read_trace("artifact_retention_days = 43"))

    result, receipt = run_case(case, tmp_path, monkeypatch, runner)
    assert result["passed"] and result["evaluation_valid"] and result["execution_complete"]
    assert len(set(workspaces)) == 2
    assert result["keyword_metrics"]["memory_fact_recall"] == 1
    assert result["diagnostics"]["facts_observed_in_question_reads"]
    assert "artifact_retention_days = 43" not in (tmp_path / result["initial_memory_artifact"] / "global_mem.txt").read_text()
    assert "artifact_retention_days = 43" in (tmp_path / result["question_memory_artifact"] / "global_mem.txt").read_text()
    for p in receipt["phases"]:
        trace_path = tmp_path / p["trace_artifact"]
        assert evaluation._sha256_file(trace_path) == p["trace_sha256"]
        assert "trace" not in p
    assert receipt["phases"][0]["historical_successful_tool_results"] == 1


def test_question_cannot_write_missing_facts_and_gain_memory_recall(case, tmp_path, monkeypatch):
    def runner(config, *, workspace_dir, memory_dir, prompt):
        if workspace_dir.name == "question":
            (memory_dir / "global_mem.txt").write_text("artifact_retention_days = 43")
            return phase("43 days")
        return phase()

    result, _ = run_case(case, tmp_path, monkeypatch, runner)
    assert not result["passed"] and not result["evaluation_valid"]
    assert result["diagnostics"]["question_memory_mutated"]
    assert result["keyword_metrics"]["memory_fact_recall"] == 0


def test_forbidden_fact_written_then_removed_still_fails(case, tmp_path, monkeypatch):
    case["forbidden_memory_facts"] = ["FORBIDDEN_PORT"]
    case["conversation_histories"].append(copy.deepcopy(case["conversation_histories"][0]))

    def runner(config, *, workspace_dir, memory_dir, prompt):
        if workspace_dir.name != "question":
            value = "artifact_retention_days = 43" + (" FORBIDDEN_PORT" if workspace_dir.name == "session_1" else "")
            (memory_dir / "global_mem.txt").write_text(value)
        return phase("43 days")

    result, _ = run_case(case, tmp_path, monkeypatch, runner)
    assert result["keyword_metrics"]["forbidden_memory_fact_count"] == 0
    assert result["diagnostics"]["forbidden_facts_during_updates"] == ["FORBIDDEN_PORT"]
    assert not result["passed"]


def test_incomplete_update_cannot_pass_even_with_high_judge_score(case, tmp_path, monkeypatch):
    def runner(config, *, workspace_dir, memory_dir, prompt):
        if workspace_dir.name != "question":
            (memory_dir / "global_mem.txt").write_text("artifact_retention_days = 43")
            return phase(status="waiting")
        return phase("43 days")

    monkeypatch.setattr(evaluation, "_run_judge", lambda *a, **k: ({"passed": True}, {}))
    result, _ = run_case(case, tmp_path, monkeypatch, runner, "rubric")
    assert result["judge"]["passed"] and not result["execution_complete"] and not result["passed"]


def test_obsolete_temporal_values_are_not_banned():
    cases, _ = evaluation.load_cases()
    case = next(c for c in cases if c["test_id"] == "layer3_01_updated_python_range")
    result = evaluation._keyword_metrics(
        case, ">=3.10,<3.14; old value superseded", ">=3.10,<3.14; obsolete: >=3.9,<3.13",
    )
    assert result["memory_fact_recall"] == 1 and result["forbidden_memory_fact_count"] == 0


def test_campaign_hashes_all_evidence_and_saves_case_incrementally(tmp_path, monkeypatch):
    config = AgentConfig(language="zh", llm_backends={"default": LLMBackendConfig(
        name="default", provider="openai", api_key="test-key", api_base="http://unused.invalid", model="test-model",
    )})
    monkeypatch.setattr(evaluation, "load_default_config", lambda: config)

    def runner(config, *, workspace_dir, memory_dir, prompt):
        if workspace_dir.name != "question":
            (memory_dir / "global_mem.txt").write_text("artifact_retention_days = 43")
        return phase("43 days", trace=read_trace("artifact_retention_days = 43"))

    monkeypatch.setattr(evaluation, "_run_agent_phase", runner)
    assert evaluation.main(["--limit", "1", "--metric", "keyword-recall", "--output-dir", str(tmp_path)]) == 0
    latest = json.loads((tmp_path / "validation/latest.json").read_text())
    run_dir = tmp_path / latest["run_dir"]
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["schema_version"] == 2 and manifest["status"] == "partial"
    assert manifest["rubric_version"] == "source-aware-v2"
    assert any("memory_initial" in path for path in manifest["artifacts"])
    assert any("trace_question.json" in path for path in manifest["artifacts"])
    assert any(path.startswith("cases/") for path in manifest["artifacts"])
    for path, artifact in manifest["artifacts"].items():
        assert evaluation._sha256_file(run_dir / path) == artifact["sha256"]


def test_invalid_dataset_stops_before_agent_calls(case, tmp_path, monkeypatch):
    case["conversation_histories"][0]["messages"] = [{"role": "user", "content": "unverified"}]
    case["conversation_histories"][0]["requires_tool_evidence"] = False
    dataset = tmp_path / "cases.json"
    dataset.write_text(json.dumps([case]))
    monkeypatch.setattr(evaluation, "load_default_config", lambda: AgentConfig(language="zh"))

    def no_calls(*args, **kwargs):
        pytest.fail("An invalid dataset must not call the model")

    monkeypatch.setattr(evaluation, "_run_agent_phase", no_calls)
    assert evaluation.main(["--dataset", str(dataset), "--metric", "keyword-recall"]) == 2
