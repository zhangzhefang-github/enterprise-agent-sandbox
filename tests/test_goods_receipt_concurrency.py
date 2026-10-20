import threading
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


def new_sandbox(*lines: PurchaseOrderLine) -> Sandbox:
    if not lines:
        lines = (PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),)
    return Sandbox(
        purchase_orders=[PurchaseOrder(number="PO-1", supplier="SUP-1", lines=tuple(lines))]
    )


def receive(sandbox: Sandbox, key: str, delivery_note: str, lines: tuple[ReceiptLine, ...]):
    return sandbox.register_goods_receipt(
        CLERK,
        idempotency_key=key,
        delivery_note_no=delivery_note,
        purchase_order_no="PO-1",
        lines=lines,
    )


def run_together(calls):
    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)
    errors: list[BaseException] = []

    def run(index, call):
        try:
            barrier.wait(timeout=5)
            results[index] = call()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(index, call)) for index, call in enumerate(calls)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()
    if errors:
        raise errors[0]
    return results


def folded_delta(sandbox: Sandbox) -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in sandbox.execution_trace():
        for material, delta in entry.inventory_delta:
            totals[material] = totals.get(material, 0) + delta
    return totals


def test_concurrent_legal_receipts_both_increase_inventory():
    sandbox = new_sandbox()
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 6),)),
            lambda: receive(sandbox, "k2", "DN-2", (ReceiptLine(1, 4),)),
        ]
    )

    assert all(result.accepted for result in results)
    assert {result.receipt_no for result in results} == {"GR-1", "GR-2"}
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10
    assert sorted(entry.inventory_delta for entry in sandbox.execution_trace()) == [
        (("M-1", 4),),
        (("M-1", 6),),
    ]
    assert receive(sandbox, "k3", "DN-3", (ReceiptLine(1, 1),)).rejection is Rejection.OVER_RECEIPT


def test_concurrent_retry_of_the_same_request_posts_once():
    sandbox = new_sandbox()
    lines = (ReceiptLine(1, 6),)
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", lines),
            lambda: receive(sandbox, "k1", "DN-1", lines),
        ]
    )

    assert {result.receipt_no for result in results} == {"GR-1"}
    assert sum(result.replayed for result in results) == 1
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    trace = sandbox.execution_trace()
    assert [entry.replayed for entry in trace] == [False, True]
    assert trace[0].inventory_delta == (("M-1", 6),)
    assert trace[1].inventory_delta == ()
    replay = receive(sandbox, "k1", "DN-1", lines)
    assert replay.replayed and replay.receipt_no == "GR-1"
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_concurrent_receipts_on_one_line_reject_the_excess():
    sandbox = new_sandbox()
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 6),)),
            lambda: receive(sandbox, "k2", "DN-2", (ReceiptLine(1, 6),)),
        ]
    )

    assert sum(result.accepted for result in results) == 1
    assert sum(result.rejection is Rejection.OVER_RECEIPT for result in results) == 1
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    assert folded_delta(sandbox) == {"M-1": 6}
    assert receive(sandbox, "k3", "DN-3", (ReceiptLine(1, 5),)).rejection is Rejection.OVER_RECEIPT
    assert receive(sandbox, "k4", "DN-4", (ReceiptLine(1, 4),)).accepted


def test_concurrent_repeat_of_one_delivery_note_posts_once():
    sandbox = new_sandbox()
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 4),)),
            lambda: receive(sandbox, "k2", "DN-1", (ReceiptLine(1, 6),)),
        ]
    )

    assert sum(result.accepted for result in results) == 1
    assert sum(result.rejection is Rejection.DUPLICATE_DOCUMENT for result in results) == 1
    assert sandbox.inventory_on_hand(CLERK, "M-1") in {4, 6}
    assert folded_delta(sandbox) == {"M-1": sandbox.inventory_on_hand(CLERK, "M-1")}
    rejected = next(entry for entry in sandbox.execution_trace() if not entry.accepted)
    assert rejected.rejection is Rejection.DUPLICATE_DOCUMENT
    assert rejected.inventory_delta == ()
    assert receive(sandbox, "k3", "DN-1", (ReceiptLine(1, 1),)).rejection is Rejection.DUPLICATE_DOCUMENT


def test_concurrent_same_key_with_different_content_keeps_one_binding():
    sandbox = new_sandbox()
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 4),)),
            lambda: receive(sandbox, "k1", "DN-2", (ReceiptLine(1, 6),)),
        ]
    )

    assert sum(result.accepted for result in results) == 1
    assert sum(result.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT for result in results) == 1
    winner = next(result for result in results if result.accepted)
    assert sandbox.inventory_on_hand(CLERK, "M-1") in {4, 6}
    assert folded_delta(sandbox) == {"M-1": sandbox.inventory_on_hand(CLERK, "M-1")}

    if winner.receipt_no and sandbox.inventory_on_hand(CLERK, "M-1") == 4:
        replay = receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 4),))
        conflict = receive(sandbox, "k1", "DN-2", (ReceiptLine(1, 6),))
    else:
        replay = receive(sandbox, "k1", "DN-2", (ReceiptLine(1, 6),))
        conflict = receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 4),))

    assert replay.replayed and replay.receipt_no == winner.receipt_no
    assert conflict.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT
    assert sandbox.inventory_on_hand(CLERK, "M-1") in {4, 6}


def test_concurrent_partial_quantities_accept_what_still_fits():
    sandbox = new_sandbox()
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 4),)),
            lambda: receive(sandbox, "k2", "DN-2", (ReceiptLine(1, 4),)),
            lambda: receive(sandbox, "k3", "DN-3", (ReceiptLine(1, 4),)),
        ]
    )

    assert sum(result.accepted for result in results) == 2
    assert sum(result.rejection is Rejection.OVER_RECEIPT for result in results) == 1
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 8
    assert folded_delta(sandbox) == {"M-1": 8}
    assert receive(sandbox, "k4", "DN-4", (ReceiptLine(1, 3),)).rejection is Rejection.OVER_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 8


def test_concurrent_over_receipt_does_not_apply_a_prefix_of_the_request():
    sandbox = new_sandbox(
        PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),
        PurchaseOrderLine(2, "M-2", ordered_quantity=10, unit_price=Decimal("20.00")),
    )
    results = run_together(
        [
            lambda: receive(sandbox, "k1", "DN-1", (ReceiptLine(1, 6), ReceiptLine(2, 4))),
            lambda: receive(sandbox, "k2", "DN-2", (ReceiptLine(1, 6),)),
        ]
    )

    accepted = [result for result in results if result.accepted]
    assert len(accepted) == 1
    if accepted[0] is results[0]:
        assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
        assert sandbox.inventory_on_hand(CLERK, "M-2") == 4
        assert results[1].rejection is Rejection.OVER_RECEIPT
    else:
        assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
        assert sandbox.inventory_on_hand(CLERK, "M-2") == 0
        assert results[0].rejection is Rejection.OVER_RECEIPT
    assert folded_delta(sandbox).get("M-1") == sandbox.inventory_on_hand(CLERK, "M-1")
    assert folded_delta(sandbox).get("M-2", 0) == sandbox.inventory_on_hand(CLERK, "M-2")
