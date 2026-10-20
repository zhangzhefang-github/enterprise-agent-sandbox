from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


class Role(Enum):
    RECEIVING_CLERK = "receiving_clerk"
    AP_ACCOUNTANT = "ap_accountant"


class Rejection(Enum):
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    DUPLICATE_DOCUMENT = "duplicate_document"
    OVER_RECEIPT = "over_receipt"
    ROLE_NOT_PERMITTED = "role_not_permitted"


@dataclass(frozen=True)
class Identity:
    name: str
    roles: frozenset[Role]


@dataclass(frozen=True)
class PurchaseOrderLine:
    line_no: int
    material: str
    ordered_quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class PurchaseOrder:
    number: str
    supplier: str
    lines: tuple[PurchaseOrderLine, ...]


@dataclass(frozen=True)
class ReceiptLine:
    po_line_no: int
    quantity: int


@dataclass(frozen=True)
class OperationResult:
    accepted: bool
    rejection: Rejection | None = None
    receipt_no: str | None = None
    replayed: bool = False


class Sandbox:
    def __init__(self, purchase_orders: list[PurchaseOrder]):
        self._purchase_orders = {po.number: po for po in purchase_orders}
        self._inventory: dict[str, int] = defaultdict(int)
        # 幂等键 -> (首次请求内容, 首次结果)
        self._idempotency: dict[str, tuple[tuple, OperationResult]] = {}
        self._receipt_count = 0
        # 已成功登记的送货单：(供应商, 送货单号)
        self._registered_delivery_notes: set[tuple[str, str]] = set()
        # (采购订单号, 行号) -> 累计已收数量
        self._received: dict[tuple[str, int], int] = defaultdict(int)

    def register_goods_receipt(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ) -> OperationResult:
        request = ("register_goods_receipt", identity, delivery_note_no, purchase_order_no, lines)
        if idempotency_key in self._idempotency:
            bound_request, bound_result = self._idempotency[idempotency_key]
            if bound_request != request:
                return OperationResult(accepted=False, rejection=Rejection.IDEMPOTENCY_KEY_CONFLICT)
            return replace(bound_result, replayed=True)

        if Role.RECEIVING_CLERK not in identity.roles:
            result = OperationResult(accepted=False, rejection=Rejection.ROLE_NOT_PERMITTED)
        else:
            result = self._register_goods_receipt(delivery_note_no, purchase_order_no, lines)
        # 幂等键绑定首次请求内容，无论成功还是被拒绝
        self._idempotency[idempotency_key] = (request, result)
        return result

    def _register_goods_receipt(
        self,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ) -> OperationResult:
        po = self._purchase_orders[purchase_order_no]
        document = (po.supplier, delivery_note_no)
        if document in self._registered_delivery_notes:
            return OperationResult(accepted=False, rejection=Rejection.DUPLICATE_DOCUMENT)

        po_lines = {line.line_no: line for line in po.lines}
        # 先校验全部行，任一行不合法则整体拒绝，不做部分登记或截断
        for line in lines:
            open_quantity = (
                po_lines[line.po_line_no].ordered_quantity
                - self._received[(po.number, line.po_line_no)]
            )
            if line.quantity > open_quantity:
                return OperationResult(accepted=False, rejection=Rejection.OVER_RECEIPT)

        for line in lines:
            self._received[(po.number, line.po_line_no)] += line.quantity
            self._inventory[po_lines[line.po_line_no].material] += line.quantity
        self._registered_delivery_notes.add(document)
        self._receipt_count += 1
        return OperationResult(accepted=True, receipt_no=f"GR-{self._receipt_count}")

    def inventory_on_hand(self, identity: Identity, material: str) -> int:
        return self._inventory[material]
