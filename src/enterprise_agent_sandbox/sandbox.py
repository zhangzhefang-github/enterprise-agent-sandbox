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
    UNKNOWN_PURCHASE_ORDER = "unknown_purchase_order"
    UNKNOWN_PURCHASE_ORDER_LINE = "unknown_purchase_order_line"
    EMPTY_GOODS_RECEIPT = "empty_goods_receipt"


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
        # 已成功登记的送货单号。重复单据只看送货单号，不看供应商。
        self._registered_delivery_notes: set[str] = set()
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
        # 已成功登记的送货单只看单号，先于采购订单是否存在
        if delivery_note_no in self._registered_delivery_notes:
            return OperationResult(accepted=False, rejection=Rejection.DUPLICATE_DOCUMENT)
        if not lines:
            return OperationResult(accepted=False, rejection=Rejection.EMPTY_GOODS_RECEIPT)
        po = self._purchase_orders.get(purchase_order_no)
        if po is None:
            return OperationResult(accepted=False, rejection=Rejection.UNKNOWN_PURCHASE_ORDER)

        po_lines = {line.line_no: line for line in po.lines}
        quantity_by_line: dict[int, int] = defaultdict(int)
        for line in lines:
            if line.po_line_no not in po_lines:
                return OperationResult(accepted=False, rejection=Rejection.UNKNOWN_PURCHASE_ORDER_LINE)
            quantity_by_line[line.po_line_no] += line.quantity
        # 先核对全部行，任一行不合法则整体拒绝，不做部分登记或截断
        for po_line_no, quantity in quantity_by_line.items():
            open_quantity = po_lines[po_line_no].ordered_quantity - self._received[(po.number, po_line_no)]
            if quantity > open_quantity:
                return OperationResult(accepted=False, rejection=Rejection.OVER_RECEIPT)

        for po_line_no, quantity in quantity_by_line.items():
            self._received[(po.number, po_line_no)] += quantity
            self._inventory[po_lines[po_line_no].material] += quantity
        self._registered_delivery_notes.add(delivery_note_no)
        self._receipt_count += 1
        return OperationResult(accepted=True, receipt_no=f"GR-{self._receipt_count}")

    def inventory_on_hand(self, identity: Identity, material: str) -> int:
        return self._inventory[material]
