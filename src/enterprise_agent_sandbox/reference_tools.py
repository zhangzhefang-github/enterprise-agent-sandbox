from collections.abc import Callable
from dataclasses import dataclass
import threading

from enterprise_agent_sandbox.sandbox import (
    Identity,
    OperationResult,
    QueryResult,
    ReceiptLine,
    Rejection,
    Sandbox,
    SupplierInvoiceLine,
)


@dataclass(frozen=True)
class ReferenceReceipt:
    idempotency_key: str
    result: OperationResult


@dataclass(frozen=True)
class ReferenceInvoice:
    idempotency_key: str
    result: OperationResult


@dataclass(frozen=True)
class ReferenceMatch:
    idempotency_key: str
    result: OperationResult


@dataclass(frozen=True)
class ReferenceApproval:
    idempotency_key: str
    result: OperationResult


class ReferenceToolLayer:
    """参考工具层：把收货和发票工具映射为业务操作，并自行选择幂等键。"""

    def __init__(self, sandbox: Sandbox, identity: Identity):
        self._sandbox = sandbox
        self._identity = identity
        self._seq = 0
        self._request_lock = threading.Lock()
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
        content = ("register_goods_receipt", delivery_note_no, purchase_order_no, lines)
        key, result = self._execute(
            logical_request_id, content, delivery_note_no,
            lambda key: self._sandbox.register_goods_receipt(
                self._identity,
                idempotency_key=key,
                delivery_note_no=delivery_note_no,
                purchase_order_no=purchase_order_no,
                lines=lines,
            ),
        )
        return ReferenceReceipt(idempotency_key=key, result=result)

    def enter_supplier_invoice(
        self,
        *,
        logical_request_id: str,
        supplier: str,
        invoice_no: str,
        lines: tuple[SupplierInvoiceLine, ...],
    ) -> ReferenceInvoice:
        content = ("enter_invoice", supplier, invoice_no, lines)
        key, result = self._execute(
            logical_request_id, content, invoice_no,
            lambda key: self._sandbox.enter_supplier_invoice(
                self._identity,
                idempotency_key=key,
                supplier=supplier,
                invoice_no=invoice_no,
                lines=lines,
            ),
        )
        return ReferenceInvoice(idempotency_key=key, result=result)

    def supplier_invoices(self) -> QueryResult:
        return self._sandbox.supplier_invoices(self._identity)

    def match_supplier_invoice(
        self, *, logical_request_id: str, supplier: str, invoice_no: str
    ) -> ReferenceMatch:
        key, result = self._execute(
            logical_request_id, ("match", supplier, invoice_no), invoice_no,
            lambda key: self._sandbox.match_supplier_invoice(
                self._identity, idempotency_key=key, supplier=supplier, invoice_no=invoice_no
            ),
        )
        return ReferenceMatch(idempotency_key=key, result=result)

    def payables(self) -> QueryResult:
        return self._sandbox.payables(self._identity)

    def request_invoice_approval(
        self, *, logical_request_id: str, supplier: str, invoice_no: str
    ) -> ReferenceApproval:
        key, result = self._execute(
            logical_request_id,
            ("request_approval", supplier, invoice_no),
            invoice_no,
            lambda key: self._sandbox.request_invoice_approval(
                self._identity,
                idempotency_key=key,
                supplier=supplier,
                invoice_no=invoice_no,
            ),
        )
        return ReferenceApproval(idempotency_key=key, result=result)

    def approvals(self) -> QueryResult:
        return self._sandbox.approvals(self._identity)

    def _execute(
        self,
        logical_request_id: str,
        content: tuple,
        document_no: str,
        operation: Callable[[str], OperationResult],
    ) -> tuple[str, OperationResult]:
        # 查找、分配和提交共用锁，同一工具实例的并发重试不会分配不同键。
        # 提交前记住键，因此响应丢失后仍可安全重试。
        with self._request_lock:
            slot = (logical_request_id, content)
            key = self._logical.get(slot)
            while True:
                if key is None:
                    key = self._mint({document_no, logical_request_id})
                    self._logical[slot] = key
                result = operation(key)
                if result.rejection is not Rejection.IDEMPOTENCY_KEY_CONFLICT:
                    return key, result
                # 直接调用或另一个工具实例已使用该键；冲突证明本请求没有生效。
                # 换一个键再提交，冲突本身仍由沙箱记录在执行轨迹中。
                key = None

    def _mint(self, forbidden: set[str]) -> str:
        while True:
            self._seq += 1
            key = f"ref-{self._seq}"
            if key not in forbidden:
                return key
