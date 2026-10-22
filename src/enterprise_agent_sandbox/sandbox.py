import threading
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Context, Decimal, MAX_EMAX, MIN_EMIN, Overflow, localcontext
from enum import Enum


class Role(Enum):
    RECEIVING_CLERK = "receiving_clerk"
    AP_ACCOUNTANT = "ap_accountant"
    SIMULATED_APPROVER = "simulated_approver"


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
    UNKNOWN_SUPPLIER_INVOICE = "unknown_supplier_invoice"
    INVALID_INVOICE_STATE = "invalid_invoice_state"
    APPROVAL_NOT_PRICE_ONLY = "approval_not_price_only"
    APPROVAL_SCRIPT_MISSING = "approval_script_missing"
    APPROVAL_ALREADY_REJECTED = "approval_already_rejected"


@dataclass(frozen=True)
class Identity:
    name: str
    roles: frozenset[Role]


SIMULATED_APPROVER = Identity("simulated-approver", frozenset({Role.SIMULATED_APPROVER}))


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
    MATCHED = "matched"
    HAS_DIFFERENCES = "has_differences"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    APPROVAL_REJECTED = "approval_rejected"
    REJECTED = "rejected"


class ApprovalDecision(Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalStatus(Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class ApprovalScript:
    supplier: str
    invoice_no: str
    decision: ApprovalDecision


@dataclass(frozen=True)
class ApprovalRecord:
    supplier: str
    invoice_no: str
    requester: Identity
    status: ApprovalStatus


class MatchDifferenceKind(Enum):
    QUANTITY = "quantity"
    UNIT_PRICE = "unit_price"
    UNKNOWN_PURCHASE_ORDER = "unknown_purchase_order"
    UNKNOWN_PURCHASE_ORDER_LINE = "unknown_purchase_order_line"
    SUPPLIER_MISMATCH = "supplier_mismatch"
    INVALID_QUANTITY = "invalid_quantity"
    INVALID_UNIT_PRICE = "invalid_unit_price"


@dataclass(frozen=True)
class MatchDifference:
    kind: MatchDifferenceKind
    invoice_line_no: int
    purchase_order_no: str
    po_line_no: int
    invoice_quantity: int
    invoice_unit_price: Decimal
    total_invoice_quantity: int
    available_quantity: int | None = None
    order_unit_price: Decimal | None = None
    relative_price_difference: Decimal | None = None


@dataclass(frozen=True)
class SupplierInvoiceLine:
    purchase_order_no: str
    po_line_no: int
    quantity: int
    unit_price: Decimal
    invoice_line_no: int | None = None


@dataclass(frozen=True)
class SupplierInvoice:
    supplier: str
    number: str
    lines: tuple[SupplierInvoiceLine, ...]
    status: SupplierInvoiceStatus
    differences: tuple[MatchDifference, ...] = ()


@dataclass(frozen=True)
class MatchRequest:
    supplier: str
    invoice_no: str


@dataclass(frozen=True)
class ApprovalRequest:
    supplier: str
    invoice_no: str


@dataclass(frozen=True)
class ApprovalDecisionEvent:
    supplier: str
    invoice_no: str
    decision: ApprovalDecision


@dataclass(frozen=True)
class SupplierInvoiceRequest:
    """录入发票时提交的业务请求。"""

    supplier: str
    invoice_no: str
    lines: tuple[SupplierInvoiceLine, ...]


@dataclass(frozen=True)
class BusinessQueryRequest:
    """查询请求。轨迹只记录这次查询，不复制被查询的单据内容。"""


@dataclass(frozen=True)
class QueryResult:
    """查询的结构化结果。拒绝时 records 为空，不返回业务数据。"""

    accepted: bool
    rejection: Rejection | None = None
    records: tuple = ()


@dataclass(frozen=True)
class OperationResult:
    accepted: bool
    rejection: Rejection | None = None
    receipt_no: str | None = None
    replayed: bool = False
    invoice_status: SupplierInvoiceStatus | None = None
    differences: tuple[MatchDifference, ...] = ()
    approval_status: ApprovalStatus | None = None


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
    request: (
        GoodsReceiptRequest
        | SupplierInvoiceRequest
        | BusinessQueryRequest
        | MatchRequest
        | ApprovalRequest
        | ApprovalDecisionEvent
    )
    accepted: bool
    rejection: Rejection | None
    receipt_no: str | None
    replayed: bool
    inventory_delta: tuple[tuple[str, int], ...]
    invoice_status: SupplierInvoiceStatus | None = None
    differences: tuple[MatchDifference, ...] = ()
    approval_status: ApprovalStatus | None = None


class Sandbox:
    def __init__(
        self,
        purchase_orders: list[PurchaseOrder],
        supplier_invoices: tuple[SupplierInvoice, ...] = (),
        approval_script: tuple[ApprovalScript, ...] = (),
    ):
        self._purchase_orders = {po.number: po for po in purchase_orders}
        # (供应商, 发票号) -> 发票。初始数据可以预置已拒收的发票，录入不会把已录入改成已拒收。
        self._supplier_invoices: dict[tuple[str, str], SupplierInvoice] = {
            (invoice.supplier, invoice.number): invoice for invoice in supplier_invoices
        }
        self._approval_script = {
            (entry.supplier, entry.invoice_no): entry.decision for entry in approval_script
        }
        self._approvals: dict[tuple[str, str], ApprovalRecord] = {}
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
        submitted: (
            GoodsReceiptRequest
            | SupplierInvoiceRequest
            | BusinessQueryRequest
            | MatchRequest
            | ApprovalRequest
            | ApprovalDecisionEvent
        ),
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
                invoice_status=result.invoice_status,
                differences=result.differences,
                approval_status=result.approval_status,
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
        request = ("enter_invoice", identity, supplier, invoice_no, lines)
        submitted = SupplierInvoiceRequest(supplier, invoice_no, lines)
        inventory_before = {material: quantity for material, quantity in self._inventory.items()}
        if idempotency_key in self._idempotency:
            bound_request, bound_result = self._idempotency[idempotency_key]
            if bound_request != request:
                conflict = OperationResult(accepted=False, rejection=Rejection.IDEMPOTENCY_KEY_CONFLICT)
                return self._record(identity, submitted, conflict, inventory_before, "enter_invoice")
            return self._record(
                identity,
                submitted,
                replace(bound_result, replayed=True),
                inventory_before,
                "enter_invoice",
            )

        # 录入发票要求应付会计。仅有收货员或没有任何角色都拒绝。同时持有两个角色可以录入。
        if Role.AP_ACCOUNTANT not in identity.roles:
            result = OperationResult(accepted=False, rejection=Rejection.ROLE_NOT_PERMITTED)
        else:
            result = self._enter_new_supplier_invoice(supplier, invoice_no, lines)
        self._idempotency[idempotency_key] = (request, result)
        return self._record(identity, submitted, result, inventory_before, "enter_invoice")

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

    def match_supplier_invoice(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        supplier: str,
        invoice_no: str,
    ) -> OperationResult:
        with self._serialization:
            request = ("match", identity, supplier, invoice_no)
            submitted = MatchRequest(supplier, invoice_no)
            inventory_before = dict(self._inventory)
            if idempotency_key in self._idempotency:
                bound_request, bound_result = self._idempotency[idempotency_key]
                if bound_request != request:
                    result = OperationResult(False, Rejection.IDEMPOTENCY_KEY_CONFLICT)
                else:
                    result = replace(bound_result, replayed=True)
            else:
                if Role.AP_ACCOUNTANT not in identity.roles:
                    result = OperationResult(False, Rejection.ROLE_NOT_PERMITTED)
                else:
                    result = self._match_supplier_invoice(supplier, invoice_no)
                self._idempotency[idempotency_key] = (request, result)
            return self._record(identity, submitted, result, inventory_before, "match")

    def _match_supplier_invoice(self, supplier: str, invoice_no: str) -> OperationResult:
        invoice = self._supplier_invoices.get((supplier, invoice_no))
        if invoice is None:
            return OperationResult(False, Rejection.UNKNOWN_SUPPLIER_INVOICE)
        if invoice.status not in {SupplierInvoiceStatus.ENTERED, SupplierInvoiceStatus.HAS_DIFFERENCES}:
            return OperationResult(False, Rejection.INVALID_INVOICE_STATE)
        if not invoice.lines:
            return OperationResult(False, Rejection.EMPTY_SUPPLIER_INVOICE)

        quantities: dict[tuple[str, int], int] = defaultdict(int)
        for line in invoice.lines:
            if line.quantity > 0:
                quantities[(line.purchase_order_no, line.po_line_no)] += line.quantity

        differences = []
        for index, line in enumerate(invoice.lines, start=1):
            key = (line.purchase_order_no, line.po_line_no)
            detail = dict(
                invoice_line_no=line.invoice_line_no if line.invoice_line_no is not None else index,
                purchase_order_no=line.purchase_order_no,
                po_line_no=line.po_line_no,
                invoice_quantity=line.quantity,
                invoice_unit_price=line.unit_price,
                total_invoice_quantity=quantities[key],
            )
            if line.quantity <= 0:
                differences.append(MatchDifference(MatchDifferenceKind.INVALID_QUANTITY, **detail))
            order = self._purchase_orders.get(line.purchase_order_no)
            if order is None:
                differences.append(MatchDifference(MatchDifferenceKind.UNKNOWN_PURCHASE_ORDER, **detail))
                continue
            order_line = next((item for item in order.lines if item.line_no == line.po_line_no), None)
            if order_line is None:
                differences.append(MatchDifference(MatchDifferenceKind.UNKNOWN_PURCHASE_ORDER_LINE, **detail))
                continue
            # 本切片尚无过账操作或预置已过账数据，已过账数量恒为零。
            # #11 接入过账时，须改为已收数量减累计已过账数量，并覆盖扣减测试。
            available = self._received.get(key, 0)
            detail.update(available_quantity=available, order_unit_price=order_line.unit_price)
            if order.supplier != supplier:
                differences.append(MatchDifference(MatchDifferenceKind.SUPPLIER_MISMATCH, **detail))
            if quantities[key] > available:
                differences.append(MatchDifference(MatchDifferenceKind.QUANTITY, **detail))
            if any(not price.is_finite() or price < 0 for price in (line.unit_price, order_line.unit_price)):
                differences.append(MatchDifference(MatchDifferenceKind.INVALID_UNIT_PRICE, **detail))
                continue
            outside_tolerance, relative = _price_difference(line.unit_price, order_line.unit_price)
            if outside_tolerance:
                differences.append(MatchDifference(
                    MatchDifferenceKind.UNIT_PRICE, relative_price_difference=relative, **detail
                ))

        status = SupplierInvoiceStatus.HAS_DIFFERENCES if differences else SupplierInvoiceStatus.MATCHED
        details = tuple(differences)
        self._supplier_invoices[(supplier, invoice_no)] = replace(invoice, status=status, differences=details)
        return OperationResult(True, invoice_status=status, differences=details)

    def supplier_invoices(self, identity: Identity) -> QueryResult:
        with self._serialization:
            return self._query(identity, "query_invoice", tuple(self._supplier_invoices.values()))

    def request_invoice_approval(
        self,
        identity: Identity,
        *,
        idempotency_key: str,
        supplier: str,
        invoice_no: str,
    ) -> OperationResult:
        with self._serialization:
            request = ("request_approval", identity, supplier, invoice_no)
            submitted = ApprovalRequest(supplier, invoice_no)
            inventory_before = dict(self._inventory)
            if idempotency_key in self._idempotency:
                bound_request, bound_result = self._idempotency[idempotency_key]
                if bound_request != request:
                    result = OperationResult(False, Rejection.IDEMPOTENCY_KEY_CONFLICT)
                else:
                    result = replace(bound_result, replayed=True)
                return self._record(identity, submitted, result, inventory_before, "request_approval")

            rejection = self._approval_rejection(identity, supplier, invoice_no)
            if rejection is not None:
                result = OperationResult(False, rejection)
                self._idempotency[idempotency_key] = (request, result)
                return self._record(identity, submitted, result, inventory_before, "request_approval")

            key = (supplier, invoice_no)
            invoice = self._supplier_invoices[key]
            pending = ApprovalRecord(supplier, invoice_no, identity, ApprovalStatus.PENDING)
            self._approvals[key] = pending
            self._supplier_invoices[key] = replace(
                invoice, status=SupplierInvoiceStatus.PENDING_APPROVAL
            )
            self._record(
                identity,
                submitted,
                OperationResult(
                    True,
                    invoice_status=SupplierInvoiceStatus.PENDING_APPROVAL,
                    approval_status=ApprovalStatus.PENDING,
                ),
                inventory_before,
                "request_approval",
            )

            decision = self._approval_script[key]
            if decision is ApprovalDecision.APPROVE:
                approval_status = ApprovalStatus.APPROVED
                invoice_status = SupplierInvoiceStatus.APPROVED
            else:
                approval_status = ApprovalStatus.REJECTED
                invoice_status = SupplierInvoiceStatus.APPROVAL_REJECTED
            self._approvals[key] = replace(pending, status=approval_status)
            self._supplier_invoices[key] = replace(invoice, status=invoice_status)
            result = OperationResult(
                True, invoice_status=invoice_status, approval_status=approval_status
            )
            self._idempotency[idempotency_key] = (request, result)
            self._record(
                SIMULATED_APPROVER,
                ApprovalDecisionEvent(supplier, invoice_no, decision),
                result,
                inventory_before,
                "approval_decision",
            )
            return result

    def _approval_rejection(
        self, identity: Identity, supplier: str, invoice_no: str
    ) -> Rejection | None:
        if Role.AP_ACCOUNTANT not in identity.roles:
            return Rejection.ROLE_NOT_PERMITTED
        invoice = self._supplier_invoices.get((supplier, invoice_no))
        if invoice is None:
            return Rejection.UNKNOWN_SUPPLIER_INVOICE
        if invoice.status is SupplierInvoiceStatus.APPROVAL_REJECTED:
            return Rejection.APPROVAL_ALREADY_REJECTED
        if invoice.status is not SupplierInvoiceStatus.HAS_DIFFERENCES:
            return Rejection.INVALID_INVOICE_STATE
        if not invoice.differences or any(
            difference.kind is not MatchDifferenceKind.UNIT_PRICE
            for difference in invoice.differences
        ):
            return Rejection.APPROVAL_NOT_PRICE_ONLY
        if (supplier, invoice_no) not in self._approval_script:
            return Rejection.APPROVAL_SCRIPT_MISSING
        return None

    def approvals(self, identity: Identity) -> QueryResult:
        with self._serialization:
            return self._query(identity, "query_approval", tuple(self._approvals.values()))

    def payables(self, identity: Identity) -> QueryResult:
        with self._serialization:
            return self._query(identity, "query_payable", ())

    def _query(self, identity: Identity, operation: str, records: tuple) -> QueryResult:
        inventory_before = {material: quantity for material, quantity in self._inventory.items()}
        if Role.AP_ACCOUNTANT not in identity.roles:
            result = QueryResult(accepted=False, rejection=Rejection.ROLE_NOT_PERMITTED)
        else:
            result = QueryResult(accepted=True, records=records)
        self._record(
            identity,
            BusinessQueryRequest(),
            OperationResult(accepted=result.accepted, rejection=result.rejection),
            inventory_before,
            operation,
        )
        return result

    def inventory_on_hand(self, identity: Identity, material: str) -> int:
        with self._serialization:
            return self._inventory[material]


def _price_difference(invoice_price: Decimal, order_price: Decimal) -> tuple[bool, Decimal | None]:
    # 直接比较订单价的 98% 和 102%，乘法精度只由有效数字数决定，
    # 不随两输入的指数差增长。相对差仅供展示，不参与容差判断。
    precision = max(28, len(invoice_price.as_tuple().digits), len(order_price.as_tuple().digits)) + 4
    with localcontext(Context(prec=precision, Emax=MAX_EMAX, Emin=MIN_EMIN)) as context:
        context.traps[Overflow] = False
        outside_tolerance = not order_price * Decimal("0.98") <= invoice_price <= order_price * Decimal("1.02")
        # 零价订单的相对差无定义；同为零价无差异，非零发票价保留为单价差异。
        relative = (invoice_price / order_price - 1).copy_abs() if order_price else None
        if relative is not None and not relative.is_finite():
            relative = None
        return outside_tolerance, relative
