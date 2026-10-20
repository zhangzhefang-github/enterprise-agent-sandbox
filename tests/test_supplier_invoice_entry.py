from decimal import Decimal

from enterprise_agent_sandbox import (
    Identity,
    PurchaseOrder,
    PurchaseOrderLine,
    Rejection,
    Role,
    Sandbox,
    SupplierInvoice,
    SupplierInvoiceLine,
    SupplierInvoiceStatus,
)

ACCOUNTANT = Identity("agent-under-test", roles=frozenset({Role.AP_ACCOUNTANT}))


def purchase_order() -> PurchaseOrder:
    return PurchaseOrder(
        number="PO-1",
        supplier="SUP-1",
        lines=(PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),),
    )


def new_sandbox() -> Sandbox:
    return Sandbox(purchase_orders=[purchase_order()])


def enter(sandbox: Sandbox, key: str, quantity: int = 6, unit_price: str = "100.00", invoice_no: str = "INV-1"):
    return sandbox.enter_supplier_invoice(
        ACCOUNTANT,
        idempotency_key=key,
        supplier="SUP-1",
        invoice_no=invoice_no,
        lines=(
            SupplierInvoiceLine(
                purchase_order_no="PO-1",
                po_line_no=1,
                quantity=quantity,
                unit_price=Decimal(unit_price),
            ),
        ),
    )


def test_entered_invoice_can_be_read_and_creates_no_payable():
    sandbox = new_sandbox()

    result = enter(sandbox, "k1")

    assert result.accepted and not result.replayed
    invoices = sandbox.supplier_invoices(ACCOUNTANT)
    assert len(invoices) == 1
    assert invoices[0].supplier == "SUP-1"
    assert invoices[0].number == "INV-1"
    assert invoices[0].status is SupplierInvoiceStatus.ENTERED
    assert invoices[0].lines[0].quantity == 6
    assert invoices[0].lines[0].unit_price == Decimal("100.00")
    assert sandbox.payables(ACCOUNTANT) == ()
    assert sandbox.inventory_on_hand(ACCOUNTANT, "M-1") == 0
    trace = sandbox.execution_trace()
    assert len(trace) == 1
    assert trace[0].operation == "enter_supplier_invoice"
    assert trace[0].accepted and trace[0].inventory_delta == ()
    assert trace[0].request.invoice_no == "INV-1"
    assert trace[0].request.lines[0].quantity == 6


def test_same_supplier_and_invoice_number_cannot_be_entered_twice():
    sandbox = new_sandbox()
    assert enter(sandbox, "k1").accepted

    second = enter(sandbox, "k2")

    assert second.rejection is Rejection.DUPLICATE_DOCUMENT
    assert len(sandbox.supplier_invoices(ACCOUNTANT)) == 1
    assert sandbox.payables(ACCOUNTANT) == ()
    replay = enter(sandbox, "k1")
    assert replay.replayed and replay.accepted
    assert len(sandbox.supplier_invoices(ACCOUNTANT)) == 1


def test_rejected_invoice_number_stays_occupied():
    original = SupplierInvoice(
        supplier="SUP-1",
        number="INV-1",
        lines=(
            SupplierInvoiceLine(
                purchase_order_no="PO-1",
                po_line_no=1,
                quantity=6,
                unit_price=Decimal("100.00"),
            ),
        ),
        status=SupplierInvoiceStatus.REJECTED,
    )
    sandbox = Sandbox(purchase_orders=[purchase_order()], supplier_invoices=(original,))

    second = enter(sandbox, "k2", quantity=4, unit_price="80.00")

    assert second.rejection is Rejection.DUPLICATE_DOCUMENT
    assert sandbox.supplier_invoices(ACCOUNTANT) == (original,)
    assert sandbox.payables(ACCOUNTANT) == ()


def test_same_invoice_number_is_allowed_for_a_different_supplier():
    sandbox = new_sandbox()
    assert enter(sandbox, "k1").accepted

    other_supplier = sandbox.enter_supplier_invoice(
        ACCOUNTANT,
        idempotency_key="k2",
        supplier="SUP-2",
        invoice_no="INV-1",
        lines=(
            SupplierInvoiceLine(
                purchase_order_no="PO-1",
                po_line_no=1,
                quantity=6,
                unit_price=Decimal("100.00"),
            ),
        ),
    )

    assert other_supplier.accepted
    assert {invoice.supplier for invoice in sandbox.supplier_invoices(ACCOUNTANT)} == {"SUP-1", "SUP-2"}


def test_unknown_purchase_order_line_is_still_entered():
    sandbox = new_sandbox()
    result = sandbox.enter_supplier_invoice(
        ACCOUNTANT,
        idempotency_key="k1",
        supplier="SUP-1",
        invoice_no="INV-9",
        lines=(
            SupplierInvoiceLine(
                purchase_order_no="PO-1",
                po_line_no=9,
                quantity=6,
                unit_price=Decimal("100.00"),
            ),
        ),
    )

    assert result.accepted
    invoice = sandbox.supplier_invoices(ACCOUNTANT)[0]
    assert invoice.lines[0].po_line_no == 9
    assert sandbox.payables(ACCOUNTANT) == ()
    assert enter(sandbox, "k2", invoice_no="INV-9").rejection is Rejection.DUPLICATE_DOCUMENT


def test_empty_invoice_is_rejected_and_does_not_occupy_the_invoice_number():
    sandbox = new_sandbox()
    rejected = sandbox.enter_supplier_invoice(
        ACCOUNTANT,
        idempotency_key="k1",
        supplier="SUP-1",
        invoice_no="INV-9",
        lines=(),
    )

    assert rejected.rejection is Rejection.EMPTY_SUPPLIER_INVOICE
    assert sandbox.supplier_invoices(ACCOUNTANT) == ()
    assert enter(sandbox, "k2", invoice_no="INV-9").accepted


def test_quantity_and_price_cannot_be_changed_after_entry():
    sandbox = new_sandbox()
    assert enter(sandbox, "k1", quantity=6, unit_price="100.00").accepted

    changed = enter(sandbox, "k2", quantity=7, unit_price="90.00")

    assert changed.rejection is Rejection.DUPLICATE_DOCUMENT
    invoice = sandbox.supplier_invoices(ACCOUNTANT)[0]
    assert invoice.lines[0].quantity == 6
    assert invoice.lines[0].unit_price == Decimal("100.00")
    assert invoice.status is SupplierInvoiceStatus.ENTERED
    assert sandbox.payables(ACCOUNTANT) == ()
