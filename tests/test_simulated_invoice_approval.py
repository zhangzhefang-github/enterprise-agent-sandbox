from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_agent_sandbox import (
    ApprovalDecision,
    ApprovalScript,
    ApprovalStatus,
    BusinessOperation,
    EvaluationRun,
    Identity,
    InvalidTaskDefinition,
    MatchDifference,
    MatchDifferenceKind,
    P2P_RULE_SET_VERSION,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    SIMULATED_APPROVER,
    SupplierInvoice,
    SupplierInvoiceDocument,
    SupplierInvoiceDocumentLine,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    Task,
)


BOTH = Identity("agent", frozenset({Role.RECEIVING_CLERK, Role.AP_ACCOUNTANT}))
CLERK = Identity("clerk", frozenset({Role.RECEIVING_CLERK}))
ORDER = PurchaseOrder(
    "PO-1", "SUP-1", (PurchaseOrderLine(1, "M-1", 10, Decimal("100")),)
)
DOCUMENT = SupplierInvoiceDocument(
    "SUP-1",
    "INV-1",
    (SupplierInvoiceDocumentLine(1, "PO-1", "螺栓", 10, Decimal("103")),),
)
LINE = SupplierInvoiceLine("PO-1", 1, 10, Decimal("103"), invoice_line_no=1)


def define_task(decision=ApprovalDecision.APPROVE, **overrides):
    fields = dict(
        rule_set_version=P2P_RULE_SET_VERSION,
        identity=BOTH,
        purchase_orders=(ORDER,),
        invoice_documents=(DOCUMENT,),
        approval_script=(ApprovalScript("SUP-1", "INV-1", decision),),
        business_goal="登记收货、匹配发票，并按模拟审批结果处理单价差异",
        final_state_assertions=("审批结果与脚本一致", "无应付"),
        allowed_operations=(
            BusinessOperation.REGISTER_GOODS_RECEIPT,
            BusinessOperation.ENTER_INVOICE,
            BusinessOperation.MATCH,
            BusinessOperation.REQUEST_APPROVAL,
            BusinessOperation.QUERY_APPROVAL,
            BusinessOperation.QUERY_INVOICE,
            BusinessOperation.QUERY_PAYABLE,
        ),
    )
    fields.update(overrides)
    return Task.define(**fields)


def prepare_price_difference(task=None):
    run = EvaluationRun.start(task or define_task())
    tools = run.reference_tools()
    receipt = tools.register_goods_receipt(
        logical_request_id="receipt-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 10),),
    )
    entered = tools.enter_supplier_invoice(
        logical_request_id="entry-1",
        supplier="SUP-1",
        invoice_no="INV-1",
        lines=(LINE,),
    )
    matched = tools.match_supplier_invoice(
        logical_request_id="match-1", supplier="SUP-1", invoice_no="INV-1"
    )
    assert receipt.result.accepted and entered.result.accepted
    assert matched.result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert {item.kind for item in matched.result.differences} == {
        MatchDifferenceKind.UNIT_PRICE
    }
    return run


@pytest.mark.parametrize(
    "decision,approval_status,invoice_status",
    [
        (ApprovalDecision.APPROVE, ApprovalStatus.APPROVED, SupplierInvoiceStatus.APPROVED),
        (
            ApprovalDecision.REJECT,
            ApprovalStatus.REJECTED,
            SupplierInvoiceStatus.APPROVAL_REJECTED,
        ),
    ],
)
def test_price_only_difference_uses_deterministic_simulated_approver(
    decision, approval_status, invoice_status
):
    run = prepare_price_difference(define_task(decision))
    tools = run.reference_tools()

    response = tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )

    assert response.result.accepted and not response.result.replayed
    assert response.result.approval_status is approval_status
    assert response.result.invoice_status is invoice_status
    invoice, = tools.supplier_invoices().records
    approval, = tools.approvals().records
    assert invoice.status is invoice_status
    assert invoice.lines == (LINE,)
    assert invoice.differences[0].kind is MatchDifferenceKind.UNIT_PRICE
    assert approval.status is approval_status and approval.requester == BOTH
    assert tools.payables().records == ()
    assert run.inventory_on_hand("M-1") == 10

    request, result = run.execution_trace()[3:5]
    assert request.operation == BusinessOperation.REQUEST_APPROVAL.value
    assert request.identity == BOTH
    assert request.approval_status is ApprovalStatus.PENDING
    assert request.invoice_status is SupplierInvoiceStatus.PENDING_APPROVAL
    assert result.operation == "approval_decision"
    assert result.identity == SIMULATED_APPROVER
    assert result.request.decision is decision
    assert result.approval_status is approval_status
    assert result.invoice_status is invoice_status
    assert request.inventory_delta == result.inventory_delta == ()


def test_same_approval_request_replays_final_result_without_second_decision():
    run = prepare_price_difference()
    tools = run.reference_tools()
    first = tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )
    retry = tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )

    assert retry.idempotency_key == first.idempotency_key
    assert retry.result == replace(first.result, replayed=True)
    trace = run.execution_trace()
    assert sum(event.operation == "approval_decision" for event in trace) == 1
    assert trace[-1].operation == "request_approval" and trace[-1].replayed
    assert len(tools.approvals().records) == 1


def test_rejected_invoice_cannot_request_approval_again_with_a_new_key():
    run = prepare_price_difference(define_task(ApprovalDecision.REJECT))
    tools = run.reference_tools()
    first = tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )
    again = tools.request_invoice_approval(
        logical_request_id="approval-2", supplier="SUP-1", invoice_no="INV-1"
    )

    assert first.result.approval_status is ApprovalStatus.REJECTED
    assert again.result.rejection is Rejection.APPROVAL_ALREADY_REJECTED
    assert tools.supplier_invoices().records[0].status is SupplierInvoiceStatus.APPROVAL_REJECTED
    assert tools.approvals().records[0].status is ApprovalStatus.REJECTED
    assert tools.payables().records == ()
    assert sum(event.operation == "approval_decision" for event in run.execution_trace()) == 1


def test_approved_invoice_cannot_request_another_approval():
    run = prepare_price_difference()
    tools = run.reference_tools()
    tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )
    again = tools.request_invoice_approval(
        logical_request_id="approval-2", supplier="SUP-1", invoice_no="INV-1"
    )
    assert again.result.rejection is Rejection.INVALID_INVOICE_STATE
    assert tools.approvals().records[0].status is ApprovalStatus.APPROVED


@pytest.mark.parametrize(
    "differences",
    [
        (MatchDifference(MatchDifferenceKind.QUANTITY, 1, "PO-1", 1, 10, Decimal("100"), 10, 6),),
        (
            MatchDifference(MatchDifferenceKind.UNIT_PRICE, 1, "PO-1", 1, 10, Decimal("103"), 10),
            MatchDifference(MatchDifferenceKind.QUANTITY, 1, "PO-1", 1, 10, Decimal("103"), 10, 6),
        ),
        (MatchDifference(MatchDifferenceKind.INVALID_UNIT_PRICE, 1, "PO-1", 1, 10, Decimal("NaN"), 10),),
    ],
)
def test_non_price_only_differences_cannot_request_approval(differences):
    invoice = SupplierInvoice(
        "SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.HAS_DIFFERENCES, differences
    )
    run = EvaluationRun.start(define_task(initial_supplier_invoices=(invoice,)))
    result = run.request_invoice_approval(
        idempotency_key="approval-key", supplier="SUP-1", invoice_no="INV-1"
    )

    assert result.rejection is Rejection.APPROVAL_NOT_PRICE_ONLY
    assert run.approvals().records == ()
    current, = run.supplier_invoices().records
    assert current == invoice
    assert run.payables().records == ()
    event = run.execution_trace()[0]
    assert event.operation == "request_approval" and not event.accepted
    assert event.inventory_delta == ()


def test_quantity_difference_from_matching_cannot_request_approval():
    run = EvaluationRun.start(define_task())
    tools = run.reference_tools()
    tools.register_goods_receipt(
        logical_request_id="receipt-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )
    tools.enter_supplier_invoice(
        logical_request_id="entry-1",
        supplier="SUP-1",
        invoice_no="INV-1",
        lines=(replace(LINE, unit_price=Decimal("100")),),
    )
    matched = tools.match_supplier_invoice(
        logical_request_id="match-1", supplier="SUP-1", invoice_no="INV-1"
    )
    approval = tools.request_invoice_approval(
        logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
    )

    assert matched.result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert {item.kind for item in matched.result.differences} == {
        MatchDifferenceKind.QUANTITY
    }
    assert approval.result.rejection is Rejection.APPROVAL_NOT_PRICE_ONLY
    assert tools.approvals().records == ()
    assert tools.supplier_invoices().records[0].status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert tools.payables().records == ()
    assert run.inventory_on_hand("M-1") == 6


@pytest.mark.parametrize(
    "invoice,expected",
    [
        (None, Rejection.UNKNOWN_SUPPLIER_INVOICE),
        (
            SupplierInvoice("SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.ENTERED),
            Rejection.INVALID_INVOICE_STATE,
        ),
        (
            SupplierInvoice("SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.MATCHED),
            Rejection.INVALID_INVOICE_STATE,
        ),
        (
            SupplierInvoice("SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.REJECTED),
            Rejection.INVALID_INVOICE_STATE,
        ),
    ],
)
def test_missing_or_ineligible_invoice_is_rejected_without_approval(invoice, expected):
    seeded = () if invoice is None else (invoice,)
    run = EvaluationRun.start(define_task(initial_supplier_invoices=seeded))
    result = run.request_invoice_approval(
        idempotency_key="approval-key", supplier="SUP-1", invoice_no="INV-1"
    )
    assert result.rejection is expected
    assert run.approvals().records == ()
    assert run.payables().records == ()


def test_missing_script_rejects_request_without_creating_pending_approval():
    difference = MatchDifference(
        MatchDifferenceKind.UNIT_PRICE, 1, "PO-1", 1, 10, Decimal("103"), 10
    )
    invoice = SupplierInvoice(
        "SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.HAS_DIFFERENCES, (difference,)
    )
    run = EvaluationRun.start(
        define_task(initial_supplier_invoices=(invoice,), approval_script=())
    )
    result = run.request_invoice_approval(
        idempotency_key="approval-key", supplier="SUP-1", invoice_no="INV-1"
    )
    assert result.rejection is Rejection.APPROVAL_SCRIPT_MISSING
    assert run.approvals().records == ()
    assert run.supplier_invoices().records == (invoice,)


def test_clerk_cannot_request_or_read_approvals_and_no_data_is_leaked():
    difference = MatchDifference(
        MatchDifferenceKind.UNIT_PRICE, 1, "PO-1", 1, 10, Decimal("103"), 10
    )
    invoice = SupplierInvoice(
        "SUP-1", "INV-1", (LINE,), SupplierInvoiceStatus.HAS_DIFFERENCES, (difference,)
    )
    run = EvaluationRun.start(
        define_task(identity=CLERK, initial_supplier_invoices=(invoice,))
    )
    request = run.request_invoice_approval(
        idempotency_key="approval-key", supplier="SUP-1", invoice_no="INV-1"
    )
    query = run.approvals()
    assert request.rejection is Rejection.ROLE_NOT_PERMITTED
    assert query.rejection is Rejection.ROLE_NOT_PERMITTED and query.records == ()
    assert [event.operation for event in run.execution_trace()] == [
        "request_approval", "query_approval"
    ]
    assert all(event.identity == CLERK and not event.accepted for event in run.execution_trace())


def test_scripted_approval_is_identical_across_runs():
    task = define_task(ApprovalDecision.REJECT)
    results = []
    traces = []
    for _ in range(2):
        run = prepare_price_difference(task)
        results.append(run.reference_tools().request_invoice_approval(
            logical_request_id="approval-1", supplier="SUP-1", invoice_no="INV-1"
        ))
        traces.append(run.execution_trace())
    assert results[0] == results[1]
    assert traces[0] == traces[1]


def test_duplicate_approval_script_keys_are_rejected():
    script = ApprovalScript("SUP-1", "INV-1", ApprovalDecision.APPROVE)
    with pytest.raises(InvalidTaskDefinition):
        define_task(approval_script=(script, replace(script, decision=ApprovalDecision.REJECT)))


@pytest.mark.parametrize(
    "script",
    [
        ApprovalScript(" ", "INV-1", ApprovalDecision.APPROVE),
        ApprovalScript("SUP-1", " ", ApprovalDecision.APPROVE),
    ],
)
def test_approval_script_requires_document_identity(script):
    with pytest.raises(InvalidTaskDefinition):
        define_task(approval_script=(script,))


def test_approval_script_requires_a_known_decision_enum():
    script = ApprovalScript("SUP-1", "INV-1", "approve")  # type: ignore[arg-type]
    with pytest.raises(InvalidTaskDefinition):
        define_task(approval_script=(script,))
