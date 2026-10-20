import threading

import pytest
from decimal import Decimal

from enterprise_agent_sandbox import (
    Identity,
    LostGoodsReceiptResponse,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    Sandbox,
)

CLERK = Identity("agent-under-test", roles=frozenset({Role.RECEIVING_CLERK}))


def new_sandbox() -> Sandbox:
    return Sandbox(
        purchase_orders=[
            PurchaseOrder(
                number="PO-1",
                supplier="SUP-1",
                lines=(PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),),
            )
        ]
    )


def receive(sandbox: Sandbox, key: str, delivery_note: str, quantity: int):
    return sandbox.register_goods_receipt(
        CLERK,
        idempotency_key=key,
        delivery_note_no=delivery_note,
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, quantity),),
    )


def test_lost_response_commits_the_receipt_before_the_caller_sees_it():
    sandbox = new_sandbox()
    sandbox.declare_lost_goods_receipt_response()

    with pytest.raises(LostGoodsReceiptResponse):
        receive(sandbox, "k1", "DN-1", 6)

    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    trace = sandbox.execution_trace()
    assert len(trace) == 1
    assert trace[0].accepted and trace[0].receipt_no == "GR-1" and not trace[0].replayed
    assert trace[0].inventory_delta == (("M-1", 6),)
    assert trace[0].request.delivery_note_no == "DN-1"


def test_retry_after_lost_response_returns_the_original_receipt():
    sandbox = new_sandbox()
    sandbox.declare_lost_goods_receipt_response()
    with pytest.raises(LostGoodsReceiptResponse):
        receive(sandbox, "k1", "DN-1", 6)

    replay = receive(sandbox, "k1", "DN-1", 6)

    assert replay.accepted and replay.replayed and replay.receipt_no == "GR-1"
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    trace = sandbox.execution_trace()
    assert [entry.replayed for entry in trace] == [False, True]
    assert trace[1].receipt_no == "GR-1"
    assert trace[1].inventory_delta == ()


def test_rejected_receipt_does_not_consume_the_lost_response():
    sandbox = new_sandbox()
    sandbox.declare_lost_goods_receipt_response()

    rejected = receive(sandbox, "k1", "DN-1", 12)

    assert rejected.rejection is Rejection.OVER_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    with pytest.raises(LostGoodsReceiptResponse):
        receive(sandbox, "k2", "DN-2", 6)
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_success_is_returned_when_lost_response_is_not_declared():
    sandbox = new_sandbox()

    result = receive(sandbox, "k1", "DN-1", 6)

    assert result.accepted and result.receipt_no == "GR-1" and not result.replayed
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_replaying_an_existing_receipt_does_not_consume_the_lost_response():
    sandbox = new_sandbox()
    first = receive(sandbox, "k1", "DN-1", 6)
    sandbox.declare_lost_goods_receipt_response()

    replay = receive(sandbox, "k1", "DN-1", 6)

    assert replay.replayed and replay.receipt_no == first.receipt_no
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    with pytest.raises(LostGoodsReceiptResponse):
        receive(sandbox, "k2", "DN-2", 4)
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10


def test_lost_response_and_concurrent_retry_register_once():
    sandbox = new_sandbox()
    sandbox.declare_lost_goods_receipt_response()
    barrier = threading.Barrier(2)
    outcomes: list = [None, None]

    def attempt(index):
        barrier.wait(timeout=5)
        try:
            outcomes[index] = receive(sandbox, "k1", "DN-1", 6)
        except LostGoodsReceiptResponse as lost:
            outcomes[index] = lost

    threads = [threading.Thread(target=attempt, args=(index,)) for index in (0, 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    losses = [item for item in outcomes if isinstance(item, LostGoodsReceiptResponse)]
    results = [item for item in outcomes if getattr(item, "accepted", False)]
    assert len(losses) == 1 and len(results) == 1
    assert results[0].replayed and results[0].receipt_no == "GR-1"
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    trace = sandbox.execution_trace()
    assert [entry.replayed for entry in trace] == [False, True]
    assert trace[0].inventory_delta == (("M-1", 6),)
    assert trace[1].inventory_delta == ()
