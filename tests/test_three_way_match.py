from dataclasses import replace
from decimal import Decimal, ROUND_UP, localcontext

import pytest

from enterprise_agent_sandbox import (
    BusinessOperation,
    EvaluationRun,
    Identity,
    MatchDifferenceKind,
    P2P_RULE_SET_VERSION,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    Sandbox,
    SupplierInvoice,
    SupplierInvoiceDocument,
    SupplierInvoiceDocumentLine,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    Task,
)


BOTH = Identity("agent", frozenset({Role.RECEIVING_CLERK, Role.AP_ACCOUNTANT}))
CLERK = Identity("clerk", frozenset({Role.RECEIVING_CLERK}))
ACCOUNTANT = Identity("accountant", frozenset({Role.AP_ACCOUNTANT}))


def define_task(order_price="100", invoice_price="100", quantity=10, **overrides):
    fields = dict(
        rule_set_version=P2P_RULE_SET_VERSION,
        identity=BOTH,
        purchase_orders=(PurchaseOrder(
            "PO-1", "SUP-1", (PurchaseOrderLine(1, "M-1", 10, Decimal(order_price)),)
        ),),
        invoice_documents=(SupplierInvoiceDocument(
            "SUP-1", "INV-1", (SupplierInvoiceDocumentLine(
                1, "PO-1", "螺栓", quantity, Decimal(invoice_price)
            ),)
        ),),
        business_goal="登记收货、录入供应商发票并查看系统匹配结果",
        final_state_assertions=("发票匹配状态与差异符合单据和收货事实",),
        allowed_operations=(
            BusinessOperation.REGISTER_GOODS_RECEIPT, BusinessOperation.ENTER_INVOICE,
            BusinessOperation.MATCH, BusinessOperation.QUERY_INVOICE, BusinessOperation.QUERY_PAYABLE,
        ),
    )
    fields.update(overrides)
    return Task.define(**fields)


def receive(run, quantity=10, request="receipt-1", order="PO-1", po_line_no=1):
    return run.reference_tools().register_goods_receipt(
        logical_request_id=request,
        delivery_note_no=request,
        purchase_order_no=order,
        lines=(ReceiptLine(po_line_no, quantity),),
    )


def enter(run, price="100", quantity=10, lines=None, invoice_no="INV-1", supplier="SUP-1"):
    return run.reference_tools().enter_supplier_invoice(
        logical_request_id=f"enter-{invoice_no}", supplier=supplier, invoice_no=invoice_no,
        lines=lines if lines is not None else (SupplierInvoiceLine("PO-1", 1, quantity, Decimal(price)),),
    )


def match(run, request="match-1", invoice_no="INV-1", supplier="SUP-1"):
    return run.reference_tools().match_supplier_invoice(
        logical_request_id=request, supplier=supplier, invoice_no=invoice_no,
    )


@pytest.mark.parametrize(
    "order_price,invoice_price,relative",
    [
        ("100", "100", None),
        ("100", "102", None), ("100", "98", None),
        ("100", "103", "0.03"), ("100", "97", "0.03"),
        ("0.1", "0.102", None), ("0.1", "0.098", None),
        ("0.1", "0.103", "0.03"), ("0.1", "0.097", "0.03"),
        ("0.03", "0.0306", None),
        ("100", "102.000000000000000000000000000001", "0.02000000000000000000000000000001"),
        ("100", "97.999999999999999999999999999999", "0.02000000000000000000000000000001"),
    ],
)
def test_scripted_receipt_entry_and_matching_obey_exact_decimal_tolerance(order_price, invoice_price, relative):
    task = define_task(order_price, invoice_price)
    run = EvaluationRun.start(task)
    assert receive(run).result.accepted
    assert enter(run, price=invoice_price).result.accepted

    # 即使调用方降低 Decimal 精度，容差判断也不应改变。
    with localcontext() as context:
        context.prec = 6
        result = match(run).result

    assert result.accepted
    expected = SupplierInvoiceStatus.MATCHED if relative is None else SupplierInvoiceStatus.HAS_DIFFERENCES
    assert result.invoice_status is expected
    invoice = run.supplier_invoices().records[0]
    assert invoice.status is expected and invoice.differences == result.differences
    if relative is None:
        assert result.differences == ()
    else:
        difference, = result.differences
        assert difference.kind is MatchDifferenceKind.UNIT_PRICE
        assert difference.relative_price_difference == Decimal(relative)
        assert difference.order_unit_price == Decimal(order_price)
        assert difference.invoice_unit_price == Decimal(invoice_price)
        assert difference.invoice_line_no == 1
    assert run.inventory_on_hand("M-1") == 10
    assert run.payables().records == ()
    event = run.execution_trace()[2]
    assert event.operation == BusinessOperation.MATCH.value and event.accepted
    assert event.request.supplier == "SUP-1" and event.request.invoice_no == "INV-1"
    assert event.invoice_status is expected and event.differences == result.differences
    assert event.inventory_delta == ()


def test_partial_invoice_matches_within_received_quantity():
    run = EvaluationRun.start(define_task(quantity=6))
    receive(run, 6)
    enter(run, quantity=6)
    assert match(run).result.invoice_status is SupplierInvoiceStatus.MATCHED


def test_quantity_and_price_differences_are_both_readable():
    run = EvaluationRun.start(define_task(invoice_price="103"))
    receive(run, 6)
    enter(run, price="103")
    result = match(run).result

    assert result.accepted and result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert [detail.kind for detail in result.differences] == [
        MatchDifferenceKind.QUANTITY, MatchDifferenceKind.UNIT_PRICE,
    ]
    quantity = result.differences[0]
    assert quantity.invoice_quantity == quantity.total_invoice_quantity == 10
    assert quantity.available_quantity == 6
    assert run.supplier_invoices().records[0].differences == result.differences
    assert run.inventory_on_hand("M-1") == 6
    assert run.payables().records == ()


def test_invoice_lines_for_same_order_line_share_available_quantity():
    run = EvaluationRun.start(define_task())
    receive(run, 6)
    enter(run, lines=(
        SupplierInvoiceLine("PO-1", 1, 4, Decimal("100")),
        SupplierInvoiceLine("PO-1", 1, 4, Decimal("100")),
    ))
    result = match(run).result

    assert result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert [item.invoice_line_no for item in result.differences] == [1, 2]
    assert all(item.kind is MatchDifferenceKind.QUANTITY for item in result.differences)
    assert all(item.total_invoice_quantity == 8 and item.available_quantity == 6 for item in result.differences)


def test_additional_receipt_allows_new_match_but_retry_keeps_original_result():
    run = EvaluationRun.start(define_task())
    receive(run, 6)
    enter(run)
    original = match(run)
    receive(run, 4, request="receipt-2")
    replay = match(run)

    assert replay.idempotency_key == original.idempotency_key
    assert replay.result == replace(original.result, replayed=True)
    assert run.supplier_invoices().records[0].status is SupplierInvoiceStatus.HAS_DIFFERENCES
    fresh = match(run, request="match-2")
    assert fresh.idempotency_key != original.idempotency_key
    assert fresh.result.invoice_status is SupplierInvoiceStatus.MATCHED
    assert fresh.result.differences == ()
    late_replay = match(run)
    assert late_replay.result == replay.result
    invoice = run.supplier_invoices().records[0]
    assert invoice.status is SupplierInvoiceStatus.MATCHED and invoice.differences == ()
    assert invoice.lines == (SupplierInvoiceLine("PO-1", 1, 10, Decimal("100")),)


@pytest.mark.parametrize("status", [SupplierInvoiceStatus.REJECTED, SupplierInvoiceStatus.MATCHED])
def test_matching_rejects_invalid_state_without_changing_invoice(status):
    invoice = SupplierInvoice("SUP-1", "INV-1", (SupplierInvoiceLine("PO-1", 1, 10, Decimal("100")),), status)
    run = EvaluationRun.start(define_task(initial_supplier_invoices=(invoice,)))
    result = match(run).result

    assert result.rejection is Rejection.INVALID_INVOICE_STATE
    assert result.invoice_status is None and result.differences == ()
    assert run.supplier_invoices().records == (invoice,)
    assert run.execution_trace()[0].inventory_delta == ()


def test_matching_missing_invoice_rejection_is_bound_to_key_even_after_entry():
    run = EvaluationRun.start(define_task())
    rejected = match(run)
    assert rejected.result.rejection is Rejection.UNKNOWN_SUPPLIER_INVOICE
    enter(run)
    assert match(run).result == replace(rejected.result, replayed=True)
    assert match(run, request="match-2").result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES


def test_direct_match_reuses_idempotency_contract_and_rejects_key_content_conflicts():
    run = EvaluationRun.start(define_task())
    receive(run)
    entry = enter(run)
    conflict = run.match_supplier_invoice(
        idempotency_key=entry.idempotency_key, supplier="SUP-1", invoice_no="INV-1"
    )
    assert conflict.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT
    first = run.match_supplier_invoice(idempotency_key="match-key", supplier="SUP-1", invoice_no="INV-1")
    retry = run.match_supplier_invoice(idempotency_key="match-key", supplier="SUP-1", invoice_no="INV-1")
    assert retry == replace(first, replayed=True)
    conflict = run.match_supplier_invoice(idempotency_key="match-key", supplier="SUP-1", invoice_no="INV-2")
    assert conflict.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT


@pytest.mark.parametrize("via_tools", [True, False])
def test_matching_uses_task_identity_and_denies_clerk_without_exposing_invoice(via_tools):
    invoice = SupplierInvoice(
        "SUP-1", "INV-1", (SupplierInvoiceLine("PO-1", 1, 10, Decimal("100")),), SupplierInvoiceStatus.ENTERED
    )
    run = EvaluationRun.start(define_task(identity=CLERK, initial_supplier_invoices=(invoice,)))
    result = match(run).result if via_tools else run.match_supplier_invoice(
        idempotency_key="k1", supplier="SUP-1", invoice_no="INV-1"
    )
    assert result.rejection is Rejection.ROLE_NOT_PERMITTED
    assert result.invoice_status is None and result.differences == ()
    event, = run.execution_trace()
    assert event.identity == CLERK and not event.accepted and event.inventory_delta == ()


def test_accountant_without_receiving_role_can_match_and_no_role_cannot():
    invoice = SupplierInvoice(
        "SUP-1", "INV-1", (SupplierInvoiceLine("PO-1", 1, 10, Decimal("100")),), SupplierInvoiceStatus.ENTERED
    )
    task = define_task(identity=ACCOUNTANT, initial_supplier_invoices=(invoice,))
    run = EvaluationRun.start(task)
    assert match(run).result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    sandbox = Sandbox(list(task.purchase_orders), supplier_invoices=(invoice,))
    denied = sandbox.match_supplier_invoice(
        Identity("no-role", frozenset()), idempotency_key="k1", supplier="SUP-1", invoice_no="INV-1"
    )
    assert denied.rejection is Rejection.ROLE_NOT_PERMITTED
    assert sandbox.supplier_invoices(ACCOUNTANT).records == (invoice,)


@pytest.mark.parametrize(
    "order_no,line_no,supplier,kind",
    [
        ("missing", 1, "SUP-1", MatchDifferenceKind.UNKNOWN_PURCHASE_ORDER),
        ("PO-1", 99, "SUP-1", MatchDifferenceKind.UNKNOWN_PURCHASE_ORDER_LINE),
        ("PO-1", 1, "SUP-2", MatchDifferenceKind.SUPPLIER_MISMATCH),
    ],
)
def test_wrong_order_mapping_can_be_entered_but_never_silently_matches(order_no, line_no, supplier, kind):
    run = EvaluationRun.start(define_task())
    receive(run)
    lines = (SupplierInvoiceLine(order_no, line_no, 10, Decimal("100")),)
    assert enter(run, lines=lines, supplier=supplier).result.accepted
    result = match(run, supplier=supplier).result
    assert result.accepted and result.invoice_status is SupplierInvoiceStatus.HAS_DIFFERENCES
    assert result.differences[0].kind is kind
    assert run.supplier_invoices().records[0].lines == lines


def test_matching_does_not_reserve_quantity_for_other_invoices():
    run = EvaluationRun.start(define_task())
    receive(run)
    enter(run)
    enter(run, invoice_no="INV-2")
    first = match(run)
    second = match(run, invoice_no="INV-2")
    assert first.result.invoice_status is second.result.invoice_status is SupplierInvoiceStatus.MATCHED
    assert first.idempotency_key != second.idempotency_key
    assert run.payables().records == ()


def test_same_script_repeats_with_identical_match_results_and_trace():
    task = define_task(invoice_price="103")
    results = []
    traces = []
    for _ in range(2):
        run = EvaluationRun.start(task)
        assert run.execution_trace() == ()
        receive(run, 6)
        enter(run, price="103")
        results.append(match(run))
        traces.append(run.execution_trace())
    assert results[0] == results[1]
    assert traces[0] == traces[1]


def test_relative_difference_and_trace_do_not_depend_on_decimal_rounding_mode():
    task = define_task(order_price="3", invoice_price="4")
    results = []
    for rounding in (None, ROUND_UP):
        run = EvaluationRun.start(task)
        receive(run)
        enter(run, price="4")
        with localcontext() as context:
            if rounding is not None:
                context.rounding = rounding
                context.prec = 2
            results.append(match(run).result)
    assert results[0] == results[1]


def test_order_line_quantities_and_prices_are_matched_independently_across_orders():
    orders = (
        PurchaseOrder("PO-1", "SUP-1", (PurchaseOrderLine(1, "M-1", 10, Decimal("100")),)),
        PurchaseOrder("PO-2", "SUP-1", (PurchaseOrderLine(1, "M-2", 10, Decimal("20")),)),
    )
    run = EvaluationRun.start(define_task(purchase_orders=orders))
    receive(run, 6)
    receive(run, 4, order="PO-2", request="receipt-2")
    enter(run, lines=(
        SupplierInvoiceLine("PO-2", 1, 4, Decimal("20")),
        SupplierInvoiceLine("PO-1", 1, 6, Decimal("100")),
    ))
    result = match(run).result
    assert result.invoice_status is SupplierInvoiceStatus.MATCHED
    assert result.differences == ()


@pytest.mark.parametrize("price,expected", [("0", SupplierInvoiceStatus.MATCHED), ("1", SupplierInvoiceStatus.HAS_DIFFERENCES)])
def test_zero_order_price_does_not_divide_by_zero(price, expected):
    run = EvaluationRun.start(define_task(order_price="0", invoice_price=price))
    receive(run)
    enter(run, price=price)
    result = match(run).result
    assert result.invoice_status is expected
    if result.differences:
        assert result.differences[0].kind is MatchDifferenceKind.UNIT_PRICE
        assert result.differences[0].relative_price_difference is None
