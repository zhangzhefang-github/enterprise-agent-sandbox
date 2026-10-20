from decimal import Decimal

from enterprise_agent_sandbox import (
    Identity,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    ReferenceToolLayer,
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


def test_idempotency_key_is_separate_from_the_document_number():
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)

    receipt = tools.register_goods_receipt(
        logical_request_id="INV-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=6),),
    )

    assert receipt.idempotency_key not in {"DN-1", "INV-1"}
    assert receipt.result.accepted
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_new_logical_request_uses_a_new_idempotency_key():
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)

    first = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=6),),
    )
    second = tools.register_goods_receipt(
        logical_request_id="req-2",
        delivery_note_no="DN-2",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=4),),
    )

    assert second.idempotency_key != first.idempotency_key
    assert second.result.accepted
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10


def test_retry_of_same_logical_request_reuses_key_and_returns_original_result():
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)
    lines = (ReceiptLine(po_line_no=1, quantity=6),)

    first = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=lines,
    )
    retry = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=lines,
    )

    assert retry.idempotency_key == first.idempotency_key
    assert retry.result.replayed
    assert retry.result.receipt_no == first.result.receipt_no
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_changed_content_uses_a_new_idempotency_key():
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)

    original = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=12),),
    )
    corrected = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=10),),
    )

    assert original.result.rejection is Rejection.OVER_RECEIPT
    assert corrected.idempotency_key != original.idempotency_key
    assert corrected.idempotency_key != "DN-1"
    assert corrected.result.accepted
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10


def test_s10_rejected_twelve_then_ten_succeeds():
    """S10：12 件被拒绝后，改为 10 件并使用新的逻辑请求。"""
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)

    over = tools.register_goods_receipt(
        logical_request_id="req-12",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=12),),
    )

    assert over.result.rejection is Rejection.OVER_RECEIPT
    assert over.idempotency_key != "DN-1"
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0

    corrected = tools.register_goods_receipt(
        logical_request_id="req-10",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=10),),
    )

    assert corrected.result.accepted
    assert corrected.idempotency_key != over.idempotency_key
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10


def test_s12_retry_does_not_increase_inventory_twice():
    """S12 的重试一半：同一逻辑请求再次提交，库存只增加一次。故障注入不在本切片。"""
    sandbox = new_sandbox()
    tools = ReferenceToolLayer(sandbox, CLERK)
    lines = (ReceiptLine(po_line_no=1, quantity=6),)

    first = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=lines,
    )
    retry = tools.register_goods_receipt(
        logical_request_id="req-1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=lines,
    )

    assert retry.idempotency_key == first.idempotency_key
    assert retry.result.replayed
    assert retry.result.receipt_no == first.result.receipt_no
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
