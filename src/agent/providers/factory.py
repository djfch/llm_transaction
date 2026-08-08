"""LLM provider 工厂：按凭证（CredentialConfig）构造 provider，支持多凭证 + 按 agent 分配。

key 只从环境变量读取（凭证模型只存环境变量名，永不存明文）；缺 key 抛 LLMError，
由调用方决定降级为 None（启动不崩）或保留旧 provider（热重建失败），
待前端经 POST /api/secrets 补齐 key 后再热重建。
真实 provider 统一外裹 RetryingProvider（失败同参重发 ×3，见 providers/retry.py）；
MockProvider 不裹（冒烟/测试要求确定性）。
"""

from __future__ import annotations

import os

from src.agent.providers.anthropic import AnthropicProvider
from src.agent.providers.base import LLMError, LLMProvider
from src.agent.providers.mock import MockProvider
from src.agent.providers.openai_compat import OpenAICompatProvider
from src.agent.providers.openai_responses import OpenAIResponsesProvider
from src.agent.providers.retry import RetryingProvider
from src.audit.logger import get_logger
from src.config import CredentialConfig, Settings

logger = get_logger(__name__)


def resolve_agent_credential(settings: Settings, credential_name: str) -> CredentialConfig:
    """按名取生效凭证；Settings 校验已保证 agent 引用存在，找不到属内部不一致。"""
    for cred in settings.llm.resolve_credentials():
        if cred.name == credential_name:
            return cred
    raise LLMError(f"agent 引用的凭证不存在: {credential_name}")


def create_provider(cred: CredentialConfig) -> LLMProvider:
    """按凭证构造真实 provider（外裹同参重试装饰器）；缺 key 抛 LLMError（点名环境变量名）。"""
    key = os.environ.get(cred.api_key_env, "")
    if not key:
        raise LLMError(f"缺少 {cred.api_key_env} 环境变量，无法初始化 {cred.provider} provider")
    if cred.provider == "anthropic":
        return RetryingProvider(AnthropicProvider(cred, api_key=key))
    if cred.provider == "openai_responses":
        return RetryingProvider(OpenAIResponsesProvider(cred, api_key=key))
    return RetryingProvider(OpenAICompatProvider(cred, api_key=key))


def build_provider(settings: Settings, mock_llm: bool, credential_name: str) -> LLMProvider | None:
    """按 agent 绑定凭证构造 provider：mock 走 MockProvider；缺 key 等 LLMError 降级为 None。

    None 时对应 agent 暂停（决策循环跳轮 / 复盘报未配置，见各自 run 入口）。
    """
    if mock_llm or os.environ.get("LLM_MOCK") == "1":
        return MockProvider()
    try:
        return create_provider(resolve_agent_credential(settings, credential_name))
    except LLMError as exc:
        logger.warning("LLM provider 初始化失败（可经前端配置后热重建）：%s", exc)
        return None
