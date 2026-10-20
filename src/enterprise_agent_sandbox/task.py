from dataclasses import dataclass

from enum import Enum

from enterprise_agent_sandbox.sandbox import Identity, PurchaseOrder, ReceiptLine, Sandbox


class BusinessOperation(Enum):
    """业务操作接口契约中的操作名。这里只作为评测任务的声明，不实现尚未完成的操作。"""

    QUERY_PURCHASE_ORDER = "query_purchase_order"
    QUERY_GOODS_RECEIPT = "query_goods_receipt"
    QUERY_INVENTORY = "query_inventory"
    QUERY_INVOICE = "query_invoice"
    QUERY_APPROVAL = "query_approval"
    QUERY_PAYABLE = "query_payable"
    REGISTER_GOODS_RECEIPT = "register_goods_receipt"
    ENTER_INVOICE = "enter_invoice"
    MATCH = "match"
    REQUEST_APPROVAL = "request_approval"
    REJECT_INVOICE = "reject_invoice"
    POST = "post"


# 第一期只有一套固定规则。任务必须声明这个版本，结果才能放在一起比较。
P2P_RULE_SET_VERSION = "phase-1"
DEFAULT_REPETITIONS = 5


class InvalidTaskDefinition(Exception):
    """评测任务定义不完整，不能开始运行。"""


@dataclass(frozen=True)
class ExecutionBudget:
    decision_rounds: int = 30
    business_operations: int = 50
    wall_clock_seconds: int = 600


@dataclass(frozen=True)
class DeliveryNoteLine:
    po_line_no: int
    supplier_material_description: str
    quantity: int


@dataclass(frozen=True)
class DeliveryNote:
    number: str
    purchase_order_no: str
    lines: tuple[DeliveryNoteLine, ...]


@dataclass(frozen=True)
class Task:
    rule_set_version: str
    identity: Identity
    purchase_orders: tuple[PurchaseOrder, ...]
    delivery_notes: tuple[DeliveryNote, ...]
    business_goal: str
    final_state_assertions: tuple[str, ...]
    allowed_operations: tuple[BusinessOperation, ...]
    forbidden_operations: tuple[BusinessOperation, ...]
    execution_budget: ExecutionBudget
    repetitions: int

    @staticmethod
    def define(
        *,
        rule_set_version: str,
        identity: Identity,
        purchase_orders: tuple[PurchaseOrder, ...],
        delivery_notes: tuple[DeliveryNote, ...],
        business_goal: str,
        final_state_assertions: tuple[str, ...],
        allowed_operations: tuple[BusinessOperation, ...],
        forbidden_operations: tuple[BusinessOperation, ...] = (),
        execution_budget: ExecutionBudget | None = None,
        repetitions: int | None = None,
    ) -> "Task":
        if rule_set_version != P2P_RULE_SET_VERSION:
            raise InvalidTaskDefinition("规则集版本必须是第一期固定版本")
        if not identity.name.strip() or not identity.roles:
            raise InvalidTaskDefinition("身份必须有名称和角色")
        if not purchase_orders:
            raise InvalidTaskDefinition("评测任务必须给出初始采购订单")
        orders = _index_purchase_orders(purchase_orders)
        if not delivery_notes:
            raise InvalidTaskDefinition("评测任务必须给出送货单")
        for document in delivery_notes:
            _require_text(document.number, "送货单号不能为空")
            order = orders.get(document.purchase_order_no)
            if order is None:
                raise InvalidTaskDefinition("送货单引用了任务中不存在的采购订单")
            if not document.lines:
                raise InvalidTaskDefinition("送货单必须有行")
            for line in document.lines:
                if line.po_line_no not in order:
                    raise InvalidTaskDefinition("送货单引用了采购订单上不存在的行")
                _require_text(line.supplier_material_description, "供应商物料描述不能为空")
        goal = _require_text(business_goal, "业务目标不能为空")
        if not final_state_assertions:
            raise InvalidTaskDefinition("评测任务必须给出最终状态断言")
        assertions = tuple(_require_text(item, "最终状态断言不能为空") for item in final_state_assertions)
        _require_operations(allowed_operations, allow_empty=False)
        _require_operations(forbidden_operations, allow_empty=True)
        budget = execution_budget or ExecutionBudget()
        if budget.decision_rounds < 1 or budget.business_operations < 1 or budget.wall_clock_seconds < 1:
            raise InvalidTaskDefinition("执行预算的每一项都必须大于零")
        repeat_count = DEFAULT_REPETITIONS if repetitions is None else repetitions
        if repeat_count < 1:
            raise InvalidTaskDefinition("重复次数必须大于零")
        return Task(
            rule_set_version=rule_set_version,
            identity=identity,
            purchase_orders=purchase_orders,
            delivery_notes=delivery_notes,
            business_goal=goal,
            final_state_assertions=assertions,
            allowed_operations=allowed_operations,
            forbidden_operations=forbidden_operations,
            execution_budget=budget,
            repetitions=repeat_count,
        )

    def render_documents(self) -> str:
        blocks = []
        for document in self.delivery_notes:
            lines = [
                f"送货单 {document.number}",
                f"采购订单 {document.purchase_order_no}",
                "不含税",
            ]
            lines.extend(
                f"{line.po_line_no} {line.supplier_material_description} {line.quantity}"
                for line in document.lines
            )
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks)


class EvaluationRun:
    """一次评测运行。每次开始都从任务的初始采购订单重建环境。"""

    def __init__(self, task: Task):
        self._task = task
        self._sandbox = Sandbox(purchase_orders=list(task.purchase_orders))

    @classmethod
    def start(cls, task: Task) -> "EvaluationRun":
        return cls(task)

    def register_goods_receipt(
        self,
        *,
        idempotency_key: str,
        delivery_note_no: str,
        purchase_order_no: str,
        lines: tuple[ReceiptLine, ...],
    ):
        return self._sandbox.register_goods_receipt(
            self._task.identity,
            idempotency_key=idempotency_key,
            delivery_note_no=delivery_note_no,
            purchase_order_no=purchase_order_no,
            lines=lines,
        )

    def inventory_on_hand(self, material: str) -> int:
        return self._sandbox.inventory_on_hand(self._task.identity, material)

    def execution_trace(self):
        return self._sandbox.execution_trace()


def _index_purchase_orders(purchase_orders: tuple[PurchaseOrder, ...]) -> dict[str, set[int]]:
    orders: dict[str, set[int]] = {}
    for order in purchase_orders:
        if order.number in orders:
            raise InvalidTaskDefinition("初始采购订单号不能重复")
        if not order.lines:
            raise InvalidTaskDefinition("采购订单必须有行")
        lines: set[int] = set()
        for line in order.lines:
            if line.line_no in lines:
                raise InvalidTaskDefinition("同一采购订单的行号不能重复")
            lines.add(line.line_no)
        orders[order.number] = lines
    return orders


def _require_text(value: str, message: str) -> str:
    text = value.strip()
    if not text:
        raise InvalidTaskDefinition(message)
    return text


def _require_operations(operations: tuple[BusinessOperation, ...], *, allow_empty: bool) -> None:
    if not operations and not allow_empty:
        raise InvalidTaskDefinition("评测任务必须声明允许操作")
    for operation in operations:
        if not isinstance(operation, BusinessOperation):
            raise InvalidTaskDefinition("允许操作和禁止操作必须是业务操作")
