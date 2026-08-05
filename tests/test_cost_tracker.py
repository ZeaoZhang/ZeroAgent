"""Tests for utils/cost_tracker.py — CostTracker token tracking."""

from __future__ import annotations

import tempfile
import os
import threading

from zero_agent.utils.cost_tracker import CostTracker, TokenStats, scan_subagent_logs


class TestTokenStats:
    """TokenStats dataclass 计算."""

    def test_defaults(self) -> None:
        s = TokenStats()
        assert s.requests == 0
        assert s.input == 0
        assert s.output == 0
        assert s.cache_create == 0
        assert s.cache_read == 0
        assert s.total_tokens() == 0
        assert s.cache_hit_rate() == 0.0

    def test_total_tokens(self) -> None:
        s = TokenStats(input=100, output=50, cache_create=20, cache_read=30)
        assert s.total_tokens() == 200

    def test_cache_hit_rate(self) -> None:
        s = TokenStats(input=100, cache_create=0, cache_read=100)
        assert s.cache_hit_rate() == 50.0

    def test_cache_hit_rate_uses_explicit_miss_when_metrics_available(self) -> None:
        s = TokenStats(input=100, cache_read=80, cache_miss=20, metrics_available=True)
        assert s.cache_hit_rate() == 80.0

    def test_cache_creation_is_not_a_hit(self) -> None:
        s = TokenStats(input=100, cache_create=100, metrics_available=True)
        assert s.cache_hit_rate() == 0.0

    def test_elapsed_seconds(self) -> None:
        s = TokenStats()
        assert s.elapsed_seconds() >= 0


class TestCostTracker:
    """CostTracker hook 集成."""

    def test_get_creates_default(self) -> None:
        t = CostTracker()
        s = t.get("thread-1")
        assert isinstance(s, TokenStats)
        assert s.requests == 0

    def test_get_returns_same_tracker(self) -> None:
        t = CostTracker()
        s1 = t.get("thread-1")
        s2 = t.get("thread-1")
        assert s1 is s2

    def test_on_llm_after_accumulates(self) -> None:
        t = CostTracker()
        tname = threading.current_thread().name

        t.on_llm_after("llm_after", {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 30,
            }
        })
        t.on_llm_after("llm_after", {
            "usage": {
                "input_tokens": 200,
                "output_tokens": 100,
            }
        })

        s = t.get(tname)
        assert s.requests == 2
        assert s.input == 300
        assert s.output == 150
        assert s.cache_create == 20
        assert s.cache_read == 30

    def test_on_llm_after_consumes_canonical_cache_aliases(self) -> None:
        t = CostTracker()
        t.on_llm_after("llm_after", {
            "usage": {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_read_input_tokens": 80,
                "cache_creation_input_tokens": 10,
                "cache_miss_input_tokens": 20,
                "cache_metrics_available": True,
            }
        })
        stats = t.get(threading.current_thread().name)
        assert stats.cache_read == 80
        assert stats.cache_create == 10
        assert stats.cache_miss == 20
        assert stats.metrics_available is True
        assert stats.cache_hit_rate() == 80.0

    def test_on_llm_after_without_cache_metadata_is_unavailable(self) -> None:
        t = CostTracker()
        t.on_llm_after("llm_after", {
            "usage": {"input_tokens": 100, "output_tokens": 20}
        })
        stats = t.get(threading.current_thread().name)
        assert stats.metrics_available is False
        assert stats.cache_hit_rate() == 0.0

    def test_legacy_token_stats_cache_read_uses_uncached_input_side(self) -> None:
        stats = TokenStats(input=100, cache_read=100)
        assert stats.metrics_available is False
        assert stats.cache_miss == 0
        assert stats.cache_hit_rate() == 50.0

    def test_on_llm_after_empty_usage(self) -> None:
        t = CostTracker()
        tname = threading.current_thread().name
        t.on_llm_after("llm_after", {"usage": {}})
        s = t.get(tname)
        assert s.requests == 0  # empty usage, 不计数

    def test_on_llm_after_no_usage_key(self) -> None:
        t = CostTracker()
        tname = threading.current_thread().name
        t.on_llm_after("llm_after", {})
        s = t.get(tname)
        assert s.requests == 0

    def test_reset_clears_tracker(self) -> None:
        t = CostTracker()
        tname = threading.current_thread().name
        t.on_llm_after("llm_after", {
            "usage": {"input_tokens": 100, "output_tokens": 50}
        })
        assert t.get(tname).requests == 1
        t.reset(tname)
        assert t.get(tname).requests == 0

    def test_all_trackers(self) -> None:
        t = CostTracker()
        t.on_llm_after("llm_after", {
            "usage": {"input_tokens": 10, "output_tokens": 5}
        })
        all_t = t.all_trackers()
        tname = threading.current_thread().name
        assert tname in all_t
        assert all_t[tname].input == 10

    def test_per_thread_isolation(self) -> None:
        t = CostTracker()
        # 直接操作内部 tracker 模拟多线程
        from zero_agent.utils.cost_tracker import TokenStats
        with t._lock:
            t._trackers["thread-a"] = TokenStats(
                input=100, output=50, requests=1
            )
            t._trackers["thread-b"] = TokenStats(
                input=200, output=100, requests=1
            )
        assert t.get("thread-a").input == 100
        assert t.get("thread-b").input == 200


class TestScanSubagentLogs:
    """scan_subagent_logs 日志解析."""

    def test_empty_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = scan_subagent_logs(tmp)
            assert result == {}

    def test_parses_output_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub_dir = os.path.join(tmp, "sub1")
            os.makedirs(sub_dir)
            with open(os.path.join(sub_dir, "stdout.log"), "w") as f:
                f.write("[Output] tokens=150\n")

            result = scan_subagent_logs(tmp)
            assert "sub1" in result
            assert result["sub1"].output == 150

    def test_parses_cache_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sub_dir = os.path.join(tmp, "sub2")
            os.makedirs(sub_dir)
            with open(os.path.join(sub_dir, "stdout.log"), "w") as f:
                f.write("[Cache] write: 200, read: 500\n")

            result = scan_subagent_logs(tmp)
            assert "sub2" in result
            assert result["sub2"].cache_create == 200
            assert result["sub2"].cache_read == 500
