import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import Enum


class Role(Enum):
    RECEIVING_CLERK = "receiving_clerk"
    AP_ACCOUNTANT = "ap_accountant"


class LostGoodsReceiptResponse(Exception):
    """登记收货已经成功，但这次调用没有拿到成功结果。"""


class Rejection(Enum):
    IDEMPOTENCY_KEY_CONFLICT = "idempotency_key_conflict"
    DUPLICATE_DOCUMENT = "duplicate_document"
    OVER_RECEIPT = "over_receipt"
    ROLE_NOT_PERMITTED = "role_not_permitted"
    UNKNOWN_PURCHASE_ORDER = "unknown_purchase_order"
    UNKNOWN_PURCHASE_ORDER_LINE = "unknown_purchase_order_line"
    EMPTY_GOODS_RECEIPT = "empty_goods_receipt"
    EMPTY_SUPPLIER_INVOICE = "empty_supplier_invoice"


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


class SupplierInvoiceStatus(Enum):
    ENTERED = "entered"
    REJECTED = "rejected"


@dataclass(frozen=True)
class SupplierInvoiceLine:
    purchase_order_no: str
    po_line_no: int
    quantity: int
    unit_price: Decimal


@dataclass(frozen=True)
class SupplierInvoice:
    supplier: str
    number: str
    lines: tuple[SupplierInvoiceLine, ...]
    status: SupplierInvoiceStatus


@dataclass(frozen=True)
class SupplierInvoiceRequest:
    """录入发票时提交的业务请求。"""

    supplier: str
    invoice_no: str
    lines: tuple[SupplierInvoiceLine, ...]


@dataclass(frozen=True)
class OperationResult:
    accepted: bool
    rejection: Rejection | None = None
    receipt_no: str | None = None
    replayed: bool = False


@dataclass(frozen=True)
class GoodsReceiptRequest:
    """登记收货时提交的业务请求。轨迹保留它，库存仍按收货结果计算。"""

    delivery_note_no: str
    purchase_order_no: str
    lines: tuple[ReceiptLine, ...]


@dataclass(frozen=True)
class TraceEntry:
    identity: Identity
    operation: str
    request: GoodsReceiptRequest | SupplierInvoiceRequest
    accepted: bool
    rejection: Rejection | None
    receipt_no: str | None
    replayed: bool
    inventory_delta: tuple[tuple[str, int], ...]


class Sandbox:
    def __init__(
        self,
        purchase_orders: list[PurchaseOrder],
        supplier_invoices: tuple[SupplierInvoice, ...] = (),
    ):
        self._purchase_orders = {po.number: po for po in purchase_orders}
        # (供应商, 发票号) -> 发票。初始数据可以预置已拒收的发票，录入不会把已录入改成已拒收。
        self._supplier_invoices: dict[tuple[str, str], SupplierInvoice] = {
            (invoice.supplier, invoice.number): invoice for invoice in supplier_invoices
        }
        self._inventory: dict[str, int] = defaultdict(int)
        # 幂等键 -> (首次请求内容, 首次结果)
        self._idempotency: dict[str, tuple[tuple, OperationResult]] = {}
        self._receipt_count = 0
        # 已成功登记的送货单号。重复单据只看送货单号，不看供应商。
        self._registered_delivery_notes: set[str] = set()
        # (采购订单号, 行号) -> 累计已收数量
        self._received: dict[tuple[str, int], int] = defaultdict(int)
        self._trace: list[TraceEntry] = []
        # 并发登记按进入临界区的顺序整笔处理，避免检查和写入交错。
        self._serialization = threading.Lock()
        # 默认关闭。只让下一次本来会成功的新登记对调用方表现为响应丢失。
        self._lose_next_successful_goods_receipt = False

    def register_goods_receipt(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ) -> OperationResult:
        with self._serialization:
            return self._register_goods_receipt_serialized(
                identity,
                idempotency_key=idempotency_key,
                delivery_note_no=delivery_note_no,
                purchase_order_no=purchase_order_no,
                lines=lines,
            )

    def _register_goods_receipt_serialized(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ) -> OperationResult:
        request = ("register_goods_receipt", identity, delivery_note_no, purchase_order_no, lines)
        submitted = GoodsReceiptRequest(delivery_note_no, purchase_order_no, lines)
        inventory_before = {material: quantity for material, quantity in self._inventory.items()}
        if idempotency_key in self._idempotency:
            bound_request, bound_result = self._idempotency[idempotency_key]
            if bound_request != request:
                conflict = OperationResult(accepted=False, rejection=Rejection.IDEMPOTENCY_KEY_CONFLICT)
                return self._record(identity, submitted, conflict, inventory_before)
            return self._record(identity, submitted, replace(bound_result, replayed=True), inventory_before)

        if Role.RECEIVING_CLERK not in identity.roles:
            result = OperationResult(accepted=False, rejection=Rejection.ROLE_NOT_PERMITTED)
        else:
            result = self._register_goods_receipt(delivery_note_no, purchase_order_no, lines)
        # 幂等键绑定首次请求内容，无论成功还是被拒绝
        self._idempotency[idempotency_key] = (request, result)
        recorded = self._record(identity, submitted, result, inventory_before)
        if result.accepted and self._lose_next_successful_goods_receipt:
            self._lose_next_successful_goods_receipt = False
            raise LostGoodsReceiptResponse
        return recorded

    def declare_lost_goods_receipt_response(self) -> None:
        with self._serialization:
            self._lose_next_successful_goods_receipt = True

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

    def execution_trace(self) -> tuple[TraceEntry, ...]:
        with self._serialization:
            return tuple(self._trace)

    def _record(
        self,
        identity: Identity,
        submitted: GoodsReceiptRequest | SupplierInvoiceRequest,
        result: OperationResult,
        inventory_before: dict[str, int],
        operation: str = "register_goods_receipt",
    ) -> OperationResult:
        materials = set(inventory_before) | set(self._inventory)
        delta = tuple(
            (material, self._inventory.get(material, 0) - inventory_before.get(material, 0))
            for material in sorted(materials)
            if self._inventory.get(material, 0) != inventory_before.get(material, 0)
        )
        self._trace.append(
            TraceEntry(
                identity=identity,
                operation=operation,
                request=submitted,
                accepted=result.accepted,
                rejection=result.rejection,
                receipt_no=result.receipt_no,
                replayed=result.replayed,
                inventory_delta=delta,
            )
        )
        return result

    def enter_supplier_invoice(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        supplier: str,
        invoice_no: str,
        lines: tuple[SupplierInvoiceLine, ...],
    ) -> OperationResult:
        with self._serialization:
            return self._enter_supplier_invoice(
                identity,
                idempotency_key=idempotency_key,
                supplier=supplier,
                invoice_no=invoice_no,
                lines=lines,
            )

    def _enter_supplier_invoice(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        supplier: str,
        invoice_no: str,
        lines: tuple[SupplierInvoiceLine, ...],
    ) -> OperationResult:
        request = ("enter_supplier_invoice", identity, supplier, invoice_no, lines)
        submitted = SupplierInvoiceRequest(supplier, invoice_no, lines)
        inventory_before = {material: quantity for material, quantity in self._inventory.items()}
        if idempotency_key in self._idempotency:
            bound_request, bound_result = self._idempotency[idempotency_key]
            if bound_request != request:
                conflict = OperationResult(accepted=False, rejection=Rejection.IDEMPOTENCY_KEY_CONFLICT)
                return self._record(identity, submitted, conflict, inventory_before, "enter_supplier_invoice")
            return self._record(
                identity,
                submitted,
                replace(bound_result, replayed=True),
                inventory_before,
                "enter_supplier_invoice",
            )

        # 第一期不另加「收货员不得录入发票」。没有角色才拒绝。
        if not identity.roles:
            result = OperationResult(accepted=False, rejection=Rejection.ROLE_NOT_PERMITTED)
        else:
            result = self._enter_new_supplier_invoice(supplier, invoice_no, lines)
        self._idempotency[idempotency_key] = (request, result)
        return self._record(identity, submitted, result, inventory_before, "enter_supplier_invoice")

    def _enter_new_supplier_invoice(
        self,
        supplier: str,
        invoice_no: str,
        lines: tuple[SupplierInvoiceLine, ...],
    ) -> OperationResult:
        # 已录入和已拒收都占用供应商加发票号。发票行对应属于判断层，这里不拒绝未知订单行。
        if (supplier, invoice_no) in self._supplier_invoices:
            return OperationResult(accepted=False, rejection=Rejection.DUPLICATE_DOCUMENT)
        if not lines:
            return OperationResult(accepted=False, rejection=Rejection.EMPTY_SUPPLIER_INVOICE)
        self._supplier_invoices[(supplier, invoice_no)] = SupplierInvoice(
            supplier, invoice_no, lines, SupplierInvoiceStatus.ENTERED
        )
        return OperationResult(accepted=True)

    def supplier_invoices(self, identity: Identity) -> tuple[SupplierInvoice, ...]:
        with self._serialization:
            return tuple(self._supplier_invoices.values())

    def payables(self, identity: Identity) -> tuple[()]:
        with self._serialization:
            return ()

    def inventory_on_hand(self, identity: Identity, material: str) -> int:
        with self._serialization:
            return self._inventory[material]
