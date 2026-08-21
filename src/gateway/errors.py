"""Gate SDK 异常到领域异常的统一映射。"""

from gate_api.exceptions import GateApiException

from .base import (
    ContractNotFound,
    GatewayError,
    GatewayTransportError,
    OrderNotFound,
    PositionNotFound,
)

_LABEL_EXCEPTIONS: dict[str, type[GatewayError]] = {
    "ORDER_NOT_FOUND": OrderNotFound,
    "POSITION_NOT_FOUND": PositionNotFound,
    "CONTRACT_NOT_FOUND": ContractNotFound,
}


def wrap_gate_exception(exc: GateApiException) -> GatewayError:
    """按 GateApiException.label 分类包装成自定义异常。

    参数：
        exc: GateApiException，捕获到的原始异常

    返回：
        GatewayError：按 GateApiException.label 分类包装成自定义异常
    """
    label = getattr(exc, "label", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    status = getattr(exc, "status", None)
    exc_type = _LABEL_EXCEPTIONS.get(label, GatewayError)
    return exc_type(f"[{label or 'UNKNOWN'}] {message}", label=label, status=status)


def wrap_transport_exception(exc: Exception) -> GatewayTransportError:
    """把传输层/网关层异常归一化为 GatewayTransportError（稳定 label=TRANSPORT_UNKNOWN）。

    覆盖 urllib3 传输异常（连接/读取超时、重试耗尽）、Gate SDK 原始 ApiException
    （无 label 的 502/504 代理响应、SSL 失败 status=0）以及 SDK 对 body=None
    解码产生的 AttributeError：请求可能已到达交易所，一律按"结果未知"处理。

    参数：
        exc: Exception，传输层/网关层原始异常

    返回：
        GatewayTransportError：消息含原始异常类型名，label=TRANSPORT_UNKNOWN
    """
    return GatewayTransportError(
        f"gate 传输层异常（{type(exc).__name__}）：请求未获交易所确认，结果未知",
        label="TRANSPORT_UNKNOWN",
    )
