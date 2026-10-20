from dataclasses import dataclass

from enterprise_agent_sandbox.sandbox import (
    Identity,
    OperationResult,
    ReceiptLine,
    Sandbox,
)


@dataclass(frozen=True)
class ReferenceReceipt:
    idempotency_key: str
    result: OperationResult


class ReferenceToolLayer:
    """参考工具层：把登记收货映射为业务操作，并自行选择幂等键。"""

    def __init__(self, sandbox: Sandbox, identity: Identity):
        self._sandbox = sandbox
        self._identity = identity
        self._seq = 0
        # (逻辑请求标识, 请求内容) -> 幂等键。内容改变是另一次逻辑请求，原内容仍可重试。
        self._logical: dict[tuple[str, tuple], str] = {}

    def register_goods_receipt(
        self,
        *,
        logical_request_id: str,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ) -> ReferenceReceipt:
        content = (delivery_note_no, purchase_order_no, lines)
        slot = (logical_request_id, content)
        key = self._logical.get(slot)
        if key is None:
            # 幂等键不得使用送货单号，也不得直接沿用调用方传入的逻辑请求标识
            key = self._mint({delivery_note_no, logical_request_id})
            self._logical[slot] = key
        result = self._sandbox.register_goods_receipt(
            self._identity,
            idempotency_key=key,
            delivery_note_no=delivery_note_no,
            purchase_order_no=purchase_order_no,
            lines=lines,
        )
        return ReferenceReceipt(idempotency_key=key, result=result)

    def _mint(self, forbidden: set[str]) -> str:
        while True:
            self._seq += 1
            key = f"ref-{self._seq}"
            if key not in forbidden:
                return key
