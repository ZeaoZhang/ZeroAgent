"""AutoFailoverSession — 多后端自动容错包装器.

包装多个 LiteLLMSession 实例，提供:
    - 主 session 异常时自动 fallback 到备用 session
    - 定期健康检查，主 session 恢复后自动 spring-back
    - 故障转移时迁移 history 保持对话连续
    - 与 LiteLLMSession 兼容的 chat() 接口
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Generator, List, Optional

from zero_agent.core.config import LLMBackendConfig
from zero_agent.core.exceptions import LLMError
from zero_agent.llm.base import MockResponse
from zero_agent.llm.sessions import LiteLLMSession


class AutoFailoverSession:
    """多后端自动容错会话包装器.

    包装一个主 session 和多个备用 session，
    在主 session 失败时自动切换到备用 session，
    并通过定期健康检查在主 session 恢复时自动切回.

    Attributes:
        primary: 主 LiteLLMSession.
        backups: 备用 LiteLLMSession 列表.
        _active: 当前活跃的 session.
        _active_name: 当前活跃 session 的名称.
        _health_check_interval: 健康检查间隔秒数.
        _last_health_check: 上次健康检查的时间戳.
        _lock: 线程锁.
        _fallback_count: 故障转移次数统计.
    """

    def __init__(
        self,
        primary: LiteLLMSession,
        backups: List[LiteLLMSession],
        health_check_interval: int = 60,
    ) -> None:
        """初始化 AutoFailoverSession.

        Args:
            primary: 主 LiteLLMSession.
            backups: 备用 LiteLLMSession 列表（按优先级排序）.
            health_check_interval: 健康检查间隔秒数，默认 60s.
        """
        self.primary = primary
        self.backups = backups
        self._active: LiteLLMSession = primary
        self._active_name: str = primary.name
        self._health_check_interval = health_check_interval
        self._last_health_check: float = 0.0
        self._lock = threading.Lock()
        self._fallback_count: int = 0
        self._is_fallback_active: bool = False

    @property
    def name(self) -> str:
        """当前活跃 session 的名称."""
        return self._active_name

    @property
    def config(self) -> LLMBackendConfig:
        """当前活跃 session 的配置."""
        return self._active.config

    @property
    def history(self) -> List[Dict[str, Any]]:
        """当前活跃 session 的对话历史."""
        return self._active.history

    @history.setter
    def history(self, value: List[Dict[str, Any]]) -> None:
        """设置当前活跃 session 的对话历史."""
        self._active.history = value

    @property
    def system(self) -> str:
        """当前活跃 session 的系统提示词."""
        return self._active.system

    @system.setter
    def system(self, value: str) -> None:
        """设置当前活跃 session 的系统提示词."""
        for session in [self.primary] + self.backups:
            session.system = value

    @property
    def fallback_count(self) -> int:
        """故障转移次数."""
        return self._fallback_count

    @property
    def is_fallback_active(self) -> bool:
        """当前是否在使用备用 session."""
        return self._is_fallback_active

    def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[str, None, MockResponse]:
        """发送消息到当前活跃的 LLM 后端.

        如果主 session 调用失败，自动 fallback 到备用 session.
        在 fallback 模式下，定期检查主 session 是否恢复.

        Generator 协议与 LiteLLMSession.chat() 完全兼容:
            yield: 流式文本块
            return: MockResponse

        Args:
            messages: 消息列表.
            tools: 工具 schema 列表.

        Yields:
            流式文本块.

        Returns:
            MockResponse 实例.
        """
        self._try_health_check()

        try:
            mock = yield from self._active.chat(messages=messages, tools=tools)
            # 流中断/错误检测：即使未抛异常，content 中也可能包含流中断标记
            # 与 GenericAgent MixinSession 对齐：检测到部分失败时主动切换
            content = getattr(mock, "content", "") or ""
            if ("[!!! 流异常中断" in content or "!!!Error:" in content):
                if self.backups:
                    yield (
                        f"\n[Fallback] 检测到流中断 ({self._active.name})，"
                        f"切换到备用后端...\n"
                    )
                    return (
                        yield from self._fallback(
                            messages, tools,
                            RuntimeError(f"stream_interrupted: {content[-200:]}")
                        )
                    )
            return mock
        except Exception as primary_error:
            if not self.backups:
                raise
            return (yield from self._fallback(messages, tools, primary_error))

    def _fallback(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]],
        original_error: Exception,
    ) -> Generator[str, None, MockResponse]:
        """执行故障转移，依次尝试备用 session.

        Args:
            messages: 原始消息列表.
            tools: 工具 schema 列表.
            original_error: 主 session 的原始异常.

        Yields:
            流式文本块（含 fallback 状态提示）.

        Returns:
            MockResponse 实例.

        Raises:
            LLMError: 所有备用 session 均失败.
        """
        with self._lock:
            self._fallback_count += 1
            self._is_fallback_active = True

        yield f"\n[Fallback] 主后端 ({self.primary.name}) 不可用，切换到备用后端...\n"

        last_error = original_error
        for backup in self.backups:
            try:
                with self._lock:
                    self._migrate_history(self._active, backup)
                    backup.system = self._active.system
                    self._active = backup
                    self._active_name = backup.name

                yield f"[Fallback] 使用备用后端: {backup.name}\n"
                mock = yield from backup.chat(messages=messages, tools=tools)
                return mock
            except Exception as e:
                last_error = e
                yield f"[Fallback] 备用后端 {backup.name} 也失败了\n"
                continue

        raise LLMError(
            f"所有 LLM 后端均不可用（共 {1 + len(self.backups)} 个）"
        ) from last_error

    def _try_health_check(self) -> None:
        """如果已 fallback 且超过健康检查间隔，探测主 session.

        若主 session 恢复，自动 spring-back 切回.
        """
        if not self._is_fallback_active:
            return

        now = time.time()
        if now - self._last_health_check < self._health_check_interval:
            return

        self._last_health_check = now
        if self._probe_session(self.primary):
            with self._lock:
                self._migrate_history(self._active, self.primary)
                self.primary.system = self._active.system
                self._active = self.primary
                self._active_name = self.primary.name
                self._is_fallback_active = False

            print(
                f"[Info] 主后端 ({self.primary.name}) 已恢复，"
                f"已自动切回。累计 fallback {self._fallback_count} 次."
            )

    @staticmethod
    def _probe_session(session: LiteLLMSession, timeout: int = 5) -> bool:
        """通过轻量 API 调用探测 session 健康状态.

        使用模型列表 API (GET /v1/models) 检查后端可达性.

        Args:
            session: 待探测的 LiteLLMSession.
            timeout: 超时秒数.

        Returns:
            True 表示 session 健康可用.
        """
        import requests as _requests

        try:
            url = session.config.api_base.rstrip("/") + "/v1/models"
            headers = {"Authorization": f"Bearer {session.config.api_key}"}
            resp = _requests.get(url, headers=headers, timeout=timeout)
            return resp.status_code < 500
        except Exception:
            return False

    @staticmethod
    def _migrate_history(
        source: LiteLLMSession,
        target: LiteLLMSession,
    ) -> None:
        """将对话历史从源 session 迁移到目标 session.

        Args:
            source: 源 LiteLLMSession.
            target: 目标 LiteLLMSession.
        """
        target.history = list(source.history)
        target.system = source.system

    @property
    def usage_stats(self) -> dict:
        """获取当前活跃 session 的累计资源使用统计.

        Returns:
            包含 input/output/cached/requests 的统计字典.
        """
        return self._active.usage_stats

    @property
    def all_usage_stats(self) -> Dict[str, dict]:
        """获取所有 session 的资源使用统计.

        Returns:
            session 名称 → usage_stats 的字典.
        """
        result: Dict[str, dict] = {}
        for session in [self.primary] + self.backups:
            result[session.name] = session.usage_stats
        return result

    @property
    def extra_sys_prompt(self) -> str:
        """额外系统提示词（委托给活跃 session）."""
        return getattr(self._active, "extra_sys_prompt", "")

    @extra_sys_prompt.setter
    def extra_sys_prompt(self, value: str) -> None:
        """设置额外系统提示词（设置到所有 session）."""
        for session in [self.primary] + self.backups:
            setattr(session, "extra_sys_prompt", value)

    @property
    def last_tools(self) -> str:
        """工具描述缓存（委托给活跃 session）."""
        return getattr(self._active, "last_tools", "")

    @last_tools.setter
    def last_tools(self, value: str) -> None:
        """设置工具描述缓存（设置到活跃 session）."""
        setattr(self._active, "last_tools", value)

    @property
    def temperature(self) -> float:
        return self._active.temperature

    @temperature.setter
    def temperature(self, value: float) -> None:
        for session in [self.primary] + self.backups:
            session.temperature = value

    @property
    def max_tokens(self) -> Optional[int]:
        return self._active.max_tokens

    @max_tokens.setter
    def max_tokens(self, value: Optional[int]) -> None:
        for session in [self.primary] + self.backups:
            session.max_tokens = value
