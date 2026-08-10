"""Gate SDK 异常到领域异常的统一映射。"""

from gate_api.exceptions import GateApiException

from .base import ContractNotFound, GatewayError, OrderNotFound, PositionNotFound

_LABEL_EXCEPTIONS: dict[str, type[GatewayError]] = {
    "ORDER_NOT_FOUND": OrderNotFound,
    "POSITION_NOT_FOUND": PositionNotFound,
    "CONTRACT_NOT_FOUND": ContractNotFound,
}


def wrap_gate_exception(exc: GateApiException) -> GatewayError:
    """按 GateApiException.label 分类包装成自定义异常。"""
    label = getattr(exc, "label", "") or ""
    message = getattr(exc, "message", "") or str(exc)
    status = getattr(exc, "status", None)
    exc_type = _LABEL_EXCEPTIONS.get(label, GatewayError)
    return exc_type(f"[{label or 'UNKNOWN'}] {message}", label=label, status=status)
