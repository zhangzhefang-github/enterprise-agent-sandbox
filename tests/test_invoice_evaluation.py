from dataclasses import replace
from decimal import Decimal

import pytest

from enterprise_agent_sandbox import (
    BusinessOperation,
    EvaluationRun,
    Identity,
    InvalidTaskDefinition,
    P2P_RULE_SET_VERSION,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    SupplierInvoice,
    SupplierInvoiceDocument,
    SupplierInvoiceDocumentLine,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
    Task,
)


ACCOUNTANT = Identity("agent-under-test", frozenset({Role.AP_ACCOUNTANT}))
CLERK = Identity("agent-under-test", frozenset({Role.RECEIVING_CLERK}))
ORDER = PurchaseOrder(
    "PO-1",
    "SUP-1",
    (
        PurchaseOrderLine(1, "M-1", 10, Decimal("100.00")),
        PurchaseOrderLine(2, "M-2", 10, Decimal("20.00")),
    ),
)
DOCUMENT = SupplierInvoiceDocument(
    "SUP-1",
    "INV-1",
    (
        SupplierInvoiceDocumentLine(1, "PO-1", "垫片", 4, Decimal("20.001")),
        SupplierInvoiceDocumentLine(2, "PO-1", "螺栓", 6, Decimal("100.00")),
    ),
)
LINES = (
    SupplierInvoiceLine("PO-1", 2, 4, Decimal("20.001")),
    SupplierInvoiceLine("PO-1", 1, 6, Decimal("100.00")),
)
EXPECTED_TEXT = "\n".join(
    [
        "供应商发票 INV-1",
        "供应商 SUP-1",
        "不含税",
        "1 采购订单 PO-1 垫片 数量 4 单价 20.001",
        "2 采购订单 PO-1 螺栓 数量 6 单价 100.00",
    ]
)


def define_task(**overrides):
    fields = dict(
        rule_set_version=P2P_RULE_SET_VERSION,
        identity=ACCOUNTANT,
        purchase_orders=(ORDER,),
        invoice_documents=(DOCUMENT,),
        business_goal="按供应商发票录入，暂不形成应付",
        final_state_assertions=("发票已录入且数量单价与单据一致", "无应付"),
        allowed_operations=(
            BusinessOperation.ENTER_INVOICE,
            BusinessOperation.QUERY_INVOICE,
            BusinessOperation.QUERY_PAYABLE,
        ),
    )
    fields.update(overrides)
    return Task.define(**fields)


def enter(tools, **overrides):
    fields = dict(
        logical_request_id="entry-1", supplier="SUP-1", invoice_no="INV-1", lines=LINES
    )
    fields.update(overrides)
    return tools.enter_supplier_invoice(**fields)


def scripted_invoice_agent(text, tools):
    """脚本从固定模板取值，并按物料描述对应订单行，不把单据行号当成订单行号。"""
    header, supplier, _, *document_lines = text.splitlines()
    po_lines = {"螺栓": 1, "垫片": 2}
    lines = []
    for document_line in document_lines:
        _, _, order_no, description, _, quantity, _, unit_price = document_line.split()
        lines.append(
            SupplierInvoiceLine(order_no, po_lines[description], int(quantity), Decimal(unit_price))
        )
    entry = tools.enter_supplier_invoice(
        logical_request_id="entry-1",
        supplier=supplier.split()[1],
        invoice_no=header.split()[1],
        lines=tuple(lines),
    )
    return entry, tools.supplier_invoices(), tools.payables()


def test_scripted_agent_reads_invoice_text_and_enters_without_inventory_or_payables():
    task = define_task()
    assert task.render_documents() == EXPECTED_TEXT
    assert task.render_documents() == task.render_documents()
    run = EvaluationRun.start(task)
    assert run.supplier_invoices().records == ()

    entry, invoices, payables = scripted_invoice_agent(task.render_documents(), run.reference_tools())

    assert entry.result.accepted
    assert invoices.accepted and invoices.records == (
        SupplierInvoice("SUP-1", "INV-1", LINES, SupplierInvoiceStatus.ENTERED),
    )
    assert payables.accepted and payables.records == ()
    assert run.inventory_on_hand("M-1") == run.inventory_on_hand("M-2") == 0
    trace = run.execution_trace()
    assert [event.operation for event in trace] == [
        "query_invoice", "enter_invoice", "query_invoice", "query_payable"
    ]
    assert all(event.identity == ACCOUNTANT and event.inventory_delta == () for event in trace)


def test_invoice_entry_preserves_existing_inventory():
    identity = Identity("agent-under-test", frozenset({Role.AP_ACCOUNTANT, Role.RECEIVING_CLERK}))
    run = EvaluationRun.start(define_task(identity=identity))
    receipt = run.reference_tools().register_goods_receipt(
        logical_request_id="entry-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )
    invoice = enter(run.reference_tools())

    assert receipt.result.accepted and invoice.result.accepted
    assert receipt.idempotency_key != invoice.idempotency_key
    assert run.inventory_on_hand("M-1") == 6
    assert run.payables().records == ()
    assert run.execution_trace()[1].inventory_delta == ()


def test_duplicate_document_copies_do_not_seed_business_state():
    task = define_task(invoice_documents=(DOCUMENT, DOCUMENT))
    assert task.render_documents() == f"{EXPECTED_TEXT}\n\n{EXPECTED_TEXT}"
    run = EvaluationRun.start(task)
    assert run.supplier_invoices().records == ()
    first = enter(run.reference_tools())
    duplicate = enter(run.reference_tools(), logical_request_id="entry-2")

    assert first.result.accepted
    assert duplicate.idempotency_key != first.idempotency_key
    assert duplicate.result.rejection is Rejection.DUPLICATE_DOCUMENT
    assert len(run.supplier_invoices().records) == 1


@pytest.mark.parametrize("via_tools", [False, True])
def test_task_identity_denies_invoice_operations_without_leaking_seeded_data(via_tools):
    seed = SupplierInvoice("SUP-1", "INV-0", LINES, SupplierInvoiceStatus.REJECTED)
    run = EvaluationRun.start(define_task(identity=CLERK, initial_supplier_invoices=(seed,)))
    api = run.reference_tools() if via_tools else run
    if via_tools:
        result = enter(api).result
    else:
        result = api.enter_supplier_invoice(
            idempotency_key="k1", supplier="SUP-1", invoice_no="INV-1", lines=LINES
        )
    invoices = api.supplier_invoices()
    payables = api.payables()

    assert result.rejection is Rejection.ROLE_NOT_PERMITTED
    for query in (invoices, payables):
        assert not query.accepted
        assert query.rejection is Rejection.ROLE_NOT_PERMITTED
        assert query.records == ()
    trace = run.execution_trace()
    assert [event.operation for event in trace] == ["enter_invoice", "query_invoice", "query_payable"]
    assert all(event.identity == CLERK and not event.accepted for event in trace)
    assert all(event.inventory_delta == () for event in trace)


def test_direct_run_entry_and_queries_use_the_same_business_state():
    run = EvaluationRun.start(define_task())
    first = run.enter_supplier_invoice(
        idempotency_key="k1", supplier="SUP-1", invoice_no="INV-1", lines=LINES
    )
    assert first.accepted
    assert run.supplier_invoices() == run.reference_tools().supplier_invoices()
    assert run.supplier_invoices().records[0].lines == LINES
    assert run.payables().accepted and run.payables().records == ()


def test_same_request_reuses_key_even_when_retrieving_tools_again():
    run = EvaluationRun.start(define_task())
    first = enter(run.reference_tools(), logical_request_id="ref-1", invoice_no="ref-2")
    retry = enter(run.reference_tools(), logical_request_id="ref-1", invoice_no="ref-2")

    assert first.idempotency_key not in {"ref-1", "ref-2"}
    assert first.result.accepted and not first.result.replayed
    assert retry.idempotency_key == first.idempotency_key
    assert retry.result.accepted and retry.result.replayed
    assert len(run.supplier_invoices().records) == 1


def test_changed_content_gets_new_key_and_original_rejection_remains_replayable():
    run = EvaluationRun.start(define_task())
    tools = run.reference_tools()
    rejected = enter(tools, lines=())
    corrected = enter(tools)
    replay = enter(tools, lines=())

    assert rejected.result.rejection is Rejection.EMPTY_SUPPLIER_INVOICE
    assert corrected.idempotency_key != rejected.idempotency_key
    assert corrected.result.accepted
    assert replay.idempotency_key == rejected.idempotency_key
    assert replay.result.replayed
    assert replay.result.rejection is Rejection.EMPTY_SUPPLIER_INVOICE
    assert run.supplier_invoices().records[0].lines == LINES


@pytest.mark.parametrize(
    "changed_line",
    [replace(LINES[0], quantity=5), replace(LINES[0], unit_price=Decimal("21"))],
)
def test_quantity_or_price_change_uses_new_key_but_cannot_modify_entered_invoice(changed_line):
    run = EvaluationRun.start(define_task())
    first = enter(run.reference_tools())
    changed = enter(run.reference_tools(), lines=(changed_line, LINES[1]))

    assert changed.idempotency_key != first.idempotency_key
    assert changed.result.rejection is Rejection.DUPLICATE_DOCUMENT
    assert run.supplier_invoices().records[0].lines == LINES


@pytest.mark.parametrize("change", [{"supplier": "SUP-2"}, {"invoice_no": "INV-2"}])
def test_supplier_or_invoice_number_change_is_a_new_logical_request(change):
    run = EvaluationRun.start(define_task())
    first = enter(run.reference_tools())
    second = enter(run.reference_tools(), **change)

    assert second.idempotency_key != first.idempotency_key
    assert first.result.accepted and second.result.accepted
    assert len(run.supplier_invoices().records) == 2


def test_runs_restore_seeded_rejected_invoices_and_reset_entries_keys_and_trace():
    seed = SupplierInvoice("SUP-1", "INV-0", LINES, SupplierInvoiceStatus.REJECTED)
    task = define_task(initial_supplier_invoices=(seed,))
    first = EvaluationRun.start(task)
    first_entry = enter(first.reference_tools())
    second = EvaluationRun.start(task)

    assert second.execution_trace() == ()
    assert second.supplier_invoices().records == (seed,)
    assert second.inventory_on_hand("M-1") == 0
    second_entry = enter(second.reference_tools())
    assert second_entry.idempotency_key == first_entry.idempotency_key
    assert second_entry.result.accepted and not second_entry.result.replayed
    duplicate = enter(second.reference_tools(), invoice_no="INV-0")
    assert duplicate.result.rejection is Rejection.DUPLICATE_DOCUMENT
    assert len(first.supplier_invoices().records) == 2
    assert task.initial_supplier_invoices == (seed,)


def test_same_script_produces_identical_results_and_traces_across_runs():
    task = define_task()
    runs = [EvaluationRun.start(task), EvaluationRun.start(task)]
    results = [scripted_invoice_agent(task.render_documents(), run.reference_tools()) for run in runs]

    assert results[0] == results[1]
    assert runs[0].execution_trace() == runs[1].execution_trace()


def test_unknown_order_mapping_is_recorded_for_judgment_instead_of_blocked():
    run = EvaluationRun.start(define_task())
    wrong_lines = (SupplierInvoiceLine("unknown", 999, 4, Decimal("20.001")),)
    result = enter(run.reference_tools(), lines=wrong_lines)

    assert result.result.accepted
    assert run.supplier_invoices().records[0].lines == wrong_lines
    assert run.execution_trace()[0].request.lines == wrong_lines


@pytest.mark.parametrize(
    "document",
    [
        replace(DOCUMENT, supplier=" "),
        replace(DOCUMENT, number=" "),
        replace(DOCUMENT, lines=()),
        replace(DOCUMENT, lines=(DOCUMENT.lines[0], DOCUMENT.lines[0])),
        replace(DOCUMENT, lines=(replace(DOCUMENT.lines[0], purchase_order_no=" "),)),
        replace(DOCUMENT, lines=(replace(DOCUMENT.lines[0], supplier_material_description=" "),)),
    ],
)
def test_incomplete_invoice_document_is_rejected(document):
    with pytest.raises(InvalidTaskDefinition):
        define_task(invoice_documents=(document,))


def test_task_still_requires_source_documents():
    with pytest.raises(InvalidTaskDefinition):
        define_task(invoice_documents=())


def test_duplicate_initial_invoice_keys_are_rejected_instead_of_silently_overwritten():
    seed = SupplierInvoice("SUP-1", "INV-0", LINES, SupplierInvoiceStatus.REJECTED)
    with pytest.raises(InvalidTaskDefinition):
        define_task(initial_supplier_invoices=(seed, replace(seed, status=SupplierInvoiceStatus.ENTERED)))
