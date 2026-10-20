from decimal import Decimal

from enterprise_agent_sandbox import (
    Identity,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    Sandbox,
)

CLERK = Identity("agent-under-test", roles=frozenset({Role.RECEIVING_CLERK}))


def new_sandbox() -> Sandbox:
    """采购订单 PO-1：物料 M-1 订购 10 件。"""
    return Sandbox(
        purchase_orders=[
            PurchaseOrder(
                number="PO-1",
                supplier="SUP-1",
                lines=(
                    PurchaseOrderLine(
                        line_no=1,
                        material="M-1",
                        ordered_quantity=10,
                        unit_price=Decimal("100.00"),
                    ),
                ),
            )
        ]
    )


def receive(sandbox: Sandbox, key: str, quantity: int):
    return sandbox.register_goods_receipt(
        CLERK,
        idempotency_key=key,
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=quantity),),
    )


def test_successful_goods_receipt_is_on_the_trace():
    sandbox = new_sandbox()

    result = receive(sandbox, "k1", 6)
    entry = sandbox.execution_trace()[0]

    assert entry.identity == CLERK
    assert entry.operation == "register_goods_receipt"
    assert entry.receipt_no == result.receipt_no
    assert entry.accepted
    assert entry.inventory_delta == (("M-1", 6),)
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_rejected_goods_receipt_is_on_the_trace_without_inventory_change():
    sandbox = new_sandbox()

    rejected = receive(sandbox, "k1", 12)
    entry = sandbox.execution_trace()[0]

    assert rejected.rejection is Rejection.OVER_RECEIPT
    assert entry.rejection is Rejection.OVER_RECEIPT
    assert entry.inventory_delta == ()
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0


def test_idempotent_replay_is_a_separate_trace_entry_and_does_not_increase_inventory():
    sandbox = new_sandbox()
    first = receive(sandbox, "k1", 6)

    replay = receive(sandbox, "k1", 6)
    trace = sandbox.execution_trace()

    assert replay.replayed
    assert replay.receipt_no == first.receipt_no
    assert len(trace) == 2
    assert trace[1].replayed
    assert trace[1].receipt_no == first.receipt_no
    assert trace[1].inventory_delta == ()
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_trace_keeps_the_order_of_goods_receipts():
    sandbox = new_sandbox()

    receive(sandbox, "k-over", 12)
    receive(sandbox, "k-ok", 6)
    receive(sandbox, "k-ok", 6)
    trace = sandbox.execution_trace()

    assert [entry.rejection for entry in trace] == [Rejection.OVER_RECEIPT, None, None]
    assert [entry.replayed for entry in trace] == [False, False, True]
    assert [entry.inventory_delta for entry in trace] == [(), (("M-1", 6),), ()]
