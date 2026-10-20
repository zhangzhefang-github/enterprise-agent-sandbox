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


def receive(sandbox: Sandbox, key: str, delivery_note: str, quantity: int):
    return sandbox.register_goods_receipt(
        CLERK,
        idempotency_key=key,
        delivery_note_no=delivery_note,
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=quantity),),
    )


def test_goods_receipt_increases_inventory():
    sandbox = new_sandbox()

    result = receive(sandbox, "k1", "DN-1", 6)

    assert result.accepted
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_retry_with_same_key_and_content_replays_original_result():
    sandbox = new_sandbox()
    first = receive(sandbox, "k1", "DN-1", 6)

    retry = receive(sandbox, "k1", "DN-1", 6)

    assert retry.accepted
    assert retry.replayed
    assert retry.receipt_no == first.receipt_no
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_same_key_with_different_content_is_rejected():
    sandbox = new_sandbox()
    receive(sandbox, "k1", "DN-1", 6)

    conflict = receive(sandbox, "k1", "DN-2", 3)

    assert not conflict.accepted
    assert conflict.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_same_delivery_note_number_is_duplicate_for_another_supplier():
    sandbox = Sandbox(
        purchase_orders=[
            PurchaseOrder(
                number="PO-1",
                supplier="SUP-1",
                lines=(PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),),
            ),
            PurchaseOrder(
                number="PO-2",
                supplier="SUP-2",
                lines=(PurchaseOrderLine(1, "M-2", ordered_quantity=10, unit_price=Decimal("50.00")),),
            ),
        ]
    )

    def receive_from(key: str, purchase_order_no: str):
        return sandbox.register_goods_receipt(
            CLERK,
            idempotency_key=key,
            delivery_note_no="DN-1",
            purchase_order_no=purchase_order_no,
            lines=(ReceiptLine(po_line_no=1, quantity=6),),
        )

    assert receive_from("k1", "PO-1").accepted
    duplicate = receive_from("k2", "PO-2")

    assert duplicate.rejection is Rejection.DUPLICATE_DOCUMENT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
    assert sandbox.inventory_on_hand(CLERK, "M-2") == 0


def test_same_po_line_repeated_in_one_request_is_rejected_as_a_whole():
    sandbox = new_sandbox()

    result = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=6), ReceiptLine(po_line_no=1, quantity=6)),
    )

    assert result.rejection is Rejection.OVER_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert receive(sandbox, "k2", "DN-1", 10).accepted


def test_new_key_for_already_registered_delivery_note_is_duplicate_document():
    sandbox = new_sandbox()
    receive(sandbox, "k1", "DN-1", 6)

    duplicate = receive(sandbox, "k2", "DN-1", 6)

    assert not duplicate.accepted
    assert duplicate.rejection is Rejection.DUPLICATE_DOCUMENT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6


def test_over_receipt_is_rejected_atomically_and_corrected_request_succeeds():
    """S10：订购 10 件，送货单 12 件。"""
    sandbox = new_sandbox()

    over = receive(sandbox, "k1", "DN-1", 12)

    assert not over.accepted
    assert over.rejection is Rejection.OVER_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0

    corrected = receive(sandbox, "k2", "DN-1", 10)

    assert corrected.accepted
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 10


def test_key_of_rejected_request_stays_bound_to_its_content():
    sandbox = new_sandbox()
    receive(sandbox, "k1", "DN-1", 12)

    replay = receive(sandbox, "k1", "DN-1", 12)
    reused_for_corrected_content = receive(sandbox, "k1", "DN-1", 10)

    assert replay.rejection is Rejection.OVER_RECEIPT
    assert replay.replayed
    assert reused_for_corrected_content.rejection is Rejection.IDEMPOTENCY_KEY_CONFLICT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0


def test_request_without_receiving_role_is_rejected_and_does_not_occupy_delivery_note():
    sandbox = new_sandbox()
    accountant = Identity("agent-under-test", roles=frozenset({Role.AP_ACCOUNTANT}))

    forbidden = sandbox.register_goods_receipt(
        accountant,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=6),),
    )

    assert not forbidden.accepted
    assert forbidden.rejection is Rejection.ROLE_NOT_PERMITTED
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert receive(sandbox, "k2", "DN-1", 6).accepted


def test_delivery_note_with_one_over_received_line_registers_no_line():
    sandbox = Sandbox(
        purchase_orders=[
            PurchaseOrder(
                number="PO-2",
                supplier="SUP-1",
                lines=(
                    PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),
                    PurchaseOrderLine(2, "M-2", ordered_quantity=5, unit_price=Decimal("20.00")),
                ),
            )
        ]
    )

    result = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-2",
        lines=(ReceiptLine(po_line_no=1, quantity=10), ReceiptLine(po_line_no=2, quantity=6)),
    )

    assert result.rejection is Rejection.OVER_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert sandbox.inventory_on_hand(CLERK, "M-2") == 0


def test_unknown_purchase_order_is_rejected_and_does_not_occupy_the_delivery_note():
    sandbox = new_sandbox()

    rejected = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-MISSING",
        lines=(ReceiptLine(po_line_no=1, quantity=6),),
    )

    assert rejected.rejection is Rejection.UNKNOWN_PURCHASE_ORDER
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert receive(sandbox, "k2", "DN-1", 6).accepted


def test_unknown_purchase_order_line_is_rejected_and_does_not_occupy_the_delivery_note():
    sandbox = new_sandbox()

    rejected = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(po_line_no=1, quantity=4), ReceiptLine(po_line_no=9, quantity=1)),
    )

    assert rejected.rejection is Rejection.UNKNOWN_PURCHASE_ORDER_LINE
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert receive(sandbox, "k2", "DN-1", 6).accepted


def test_empty_goods_receipt_is_rejected_and_does_not_occupy_the_delivery_note():
    sandbox = new_sandbox()

    rejected = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(),
    )

    assert rejected.rejection is Rejection.EMPTY_GOODS_RECEIPT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 0
    assert receive(sandbox, "k2", "DN-1", 6).accepted


def test_repeat_of_registered_delivery_note_with_unknown_purchase_order_is_duplicate():
    """已登记的送货单号再次登记时按重复单据拒绝，即使采购订单不存在。"""
    sandbox = new_sandbox()
    assert receive(sandbox, "k1", "DN-1", 6).accepted

    duplicate = sandbox.register_goods_receipt(
        CLERK,
        idempotency_key="k2",
        delivery_note_no="DN-1",
        purchase_order_no="PO-MISSING",
        lines=(ReceiptLine(po_line_no=1, quantity=6),),
    )

    assert duplicate.rejection is Rejection.DUPLICATE_DOCUMENT
    assert sandbox.inventory_on_hand(CLERK, "M-1") == 6
