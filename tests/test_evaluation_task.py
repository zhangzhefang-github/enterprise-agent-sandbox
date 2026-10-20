import pytest
from decimal import Decimal

from enterprise_agent_sandbox import (
    BusinessOperation,
    DeliveryNote,
    DeliveryNoteLine,
    EvaluationRun,
    ExecutionBudget,
    Identity,
    InvalidTaskDefinition,
    P2P_RULE_SET_VERSION,
    PurchaseOrder,
    PurchaseOrderLine,
    ReceiptLine,
    Rejection,
    Role,
    Task,
)

CLERK = Identity("agent-under-test", roles=frozenset({Role.RECEIVING_CLERK}))
ACCOUNTANT = Identity("agent-under-test", roles=frozenset({Role.AP_ACCOUNTANT}))

ORDER = PurchaseOrder(
    number="PO-1",
    supplier="SUP-1",
    lines=(
        PurchaseOrderLine(1, "M-1", ordered_quantity=10, unit_price=Decimal("100.00")),
        PurchaseOrderLine(2, "M-2", ordered_quantity=10, unit_price=Decimal("20.00")),
    ),
)

NOTE = DeliveryNote(
    number="DN-1",
    purchase_order_no="PO-1",
    lines=(
        DeliveryNoteLine(po_line_no=1, supplier_material_description="供应商称之为螺栓", quantity=6),
        DeliveryNoteLine(po_line_no=2, supplier_material_description="供应商称之为垫片", quantity=4),
    ),
)

EXPECTED_TEXT = "\n".join(
    [
        "送货单 DN-1",
        "采购订单 PO-1",
        "不含税",
        "1 供应商称之为螺栓 6",
        "2 供应商称之为垫片 4",
    ]
)


def define_task(**overrides):
    fields = dict(
        rule_set_version=P2P_RULE_SET_VERSION,
        identity=CLERK,
        purchase_orders=(ORDER,),
        delivery_notes=(NOTE,),
        business_goal="按送货单登记收货",
        final_state_assertions=("库存与送货单一致",),
        allowed_operations=(BusinessOperation.REGISTER_GOODS_RECEIPT,),
        forbidden_operations=(),
    )
    fields.update(overrides)
    return Task.define(**fields)


def test_blank_business_goal_is_rejected():
    with pytest.raises(InvalidTaskDefinition):
        define_task(business_goal="  ")


def test_wrong_rule_set_version_is_rejected():
    with pytest.raises(InvalidTaskDefinition):
        define_task(rule_set_version="custom-rules")


def test_document_line_must_reference_a_purchase_order_line():
    note = DeliveryNote(
        number="DN-1",
        purchase_order_no="PO-1",
        lines=(DeliveryNoteLine(po_line_no=9, supplier_material_description="未知行", quantity=1),),
    )
    with pytest.raises(InvalidTaskDefinition):
        define_task(delivery_notes=(note,))


def test_omitted_budget_and_repetitions_use_the_phase1_defaults():
    task = define_task()

    assert task.execution_budget == ExecutionBudget(
        decision_rounds=30,
        business_operations=50,
        wall_clock_seconds=600,
    )
    assert task.repetitions == 5


def test_same_task_renders_the_same_tax_exclusive_text():
    task = define_task()

    assert task.render_documents() == task.render_documents() == EXPECTED_TEXT
    assert task.purchase_orders[0].lines[0].material == "M-1"
    assert "M-1" not in task.render_documents()


def test_next_run_starts_from_the_task_initial_data():
    task = define_task()
    first = EvaluationRun.start(task)
    receipt = first.register_goods_receipt(
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )

    assert receipt.accepted and not receipt.replayed
    assert first.inventory_on_hand("M-1") == 6
    assert len(first.execution_trace()) == 1

    second = EvaluationRun.start(task)

    assert second.inventory_on_hand("M-1") == 0
    assert second.execution_trace() == ()
    again = second.register_goods_receipt(
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )
    assert again.accepted and not again.replayed and again.receipt_no == "GR-1"
    assert second.inventory_on_hand("M-1") == 6
    assert len(second.execution_trace()) == 1
    assert first.inventory_on_hand("M-1") == 6


def test_task_identity_is_used_for_the_permission_check():
    denied = EvaluationRun.start(define_task(identity=ACCOUNTANT))
    refused = denied.register_goods_receipt(
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )

    allowed = EvaluationRun.start(define_task(identity=CLERK))
    accepted = allowed.register_goods_receipt(
        idempotency_key="k1",
        delivery_note_no="DN-1",
        purchase_order_no="PO-1",
        lines=(ReceiptLine(1, 6),),
    )

    assert refused.rejection is Rejection.ROLE_NOT_PERMITTED
    assert denied.inventory_on_hand("M-1") == 0
    assert accepted.accepted and accepted.receipt_no == "GR-1"
    assert allowed.inventory_on_hand("M-1") == 6
