"""思考程度统一翻译：配置档位 → 各厂商 wire 参数（纯函数，无 IO）。

档位语义：空串 = 不传任何参数（跟随模型默认，默认思考的模型即思考）；
on = 显式开启；off = 关闭；low~max = 具体强度。OpenAI 兼容协议下
reasoning_effort 是 DeepSeek（api-docs.deepseek.com/guides/thinking_mode）、
GLM-5.2+（docs.bigmodel.cn 思考模式）、Kimi k3（platform.kimi.com
use-reasoning-effort）、GPT 的通用字段；厂商私有字段（Qwen enable_thinking、
DeepSeek/旧 Kimi k2 的 thinking 对象）按模型名前缀分派（能力按模型名自动
匹配）。字段组合依据各家官方文档，禁止猜测。
"""

from __future__ import annotations

# 旧 k2 系只有开关二态（enabled/disabled），无 reasoning_effort 档位
_KIMI_K2_LEGACY = ("kimi-k2.5", "kimi-k2.6")

# Anthropic 协议无 effort 概念：档位映射成思考 token 预算（官方要求
# budget_tokens 严格小于 max_tokens 且 ≥1024，见 Anthropic extended thinking 文档）
_EFFORT_BUDGET = {"low": 4000, "medium": 8000, "high": 16000, "xhigh": 24000, "max": 32000}
_DEFAULT_BUDGET = 16000
_BUDGET_MIN = 1024


def thinking_wire_kwargs(model: str, effort: str) -> dict:
    """统一档位 → OpenAI 兼容 chat.completions 的 wire 参数（含 extra_body）。

    - 空串/on：默认不传；Qwen 需显式 enable_thinking 开启（老 qwen 系默认关）
    - off：DeepSeek/旧 Kimi k2.5/k2.6 走 thinking 对象；Qwen 走 enable_thinking；
    始终思考模型（kimi-k2.7 系）官方不可关，降级为不传；
    其余（GPT/GLM/Kimi k3 等）走 reasoning_effort: "none"
    - low~max：reasoning_effort 原样透传（DeepSeek/GLM/Kimi k3/GPT 通用字段）；
    Qwen 的 chat 接口只认 enable_thinking 开关（强度由 thinking_budget 控制，
    本层不做预算映射），档位退化为"开"；旧 Kimi k2.5/k2.6 无强度概念，仅开启

    参数：
        model: str，模型名称
        effort: str，统一思考强度档位

    返回：
        dict，对应档位的 reasoning_effort 与 extra_body 参数

    """
    m = model.lower()
    if effort == "on":
        return {"extra_body": {"enable_thinking": True}} if m.startswith("qwen") else {}
    if not effort:
        return {}
    if effort == "off":
        if m.startswith("qwen"):
            return {"extra_body": {"enable_thinking": False}}
        if m.startswith("kimi-k2.7"):
            return {}  # 始终思考模式官方不可关，降级为不传
        if m.startswith("deepseek") or m.startswith(_KIMI_K2_LEGACY):
            return {"extra_body": {"thinking": {"type": "disabled"}}}
        return {"reasoning_effort": "none"}
    # 档位：off 之后只剩 low/medium/high/xhigh/max
    if m.startswith(_KIMI_K2_LEGACY):
        return {"extra_body": {"thinking": {"type": "enabled"}}}
    if m.startswith("qwen"):
        return {"extra_body": {"enable_thinking": True}}  # 档位仅开关语义
    return {"reasoning_effort": effort}


def anthropic_thinking(effort: str, max_tokens: int) -> dict | None:
    """统一档位 → Anthropic 协议 thinking 参数。

    空串/off 返回 None（不传 = Claude 默认不思考）；on/档位返回
    {"type": "enabled", "budget_tokens": N}。档位映射为思考 token 预算，并按
    max_tokens 裁剪：官方要求 budget_tokens 严格小于 max_tokens（且 ≥1024），
    max_tokens < 2048 时无合法解，降级为不传（避免请求 400）。

    参数：
        effort: str，统一思考强度档位
        max_tokens: int，模型最大输出 token 数

    返回：
        dict | None，对应档位的 Anthropic thinking 参数；关闭时为 None

    """
    if not effort or effort == "off":
        return None
    budget = _EFFORT_BUDGET.get(effort, _DEFAULT_BUDGET)
    cap = max_tokens - _BUDGET_MIN
    if cap < _BUDGET_MIN:
        return None  # max_tokens 太小，thinking 无合法预算，降级不传
    return {"type": "enabled", "budget_tokens": min(budget, cap)}
