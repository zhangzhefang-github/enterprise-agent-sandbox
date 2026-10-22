# enterprise-agent-sandbox
A sandbox for testing and evaluating enterprise AI agents against realistic business workflows.

运行测试（Python 3.12+）：

```bash
uv run pytest -q
```

发票录入可以通过评测运行和参考工具层执行。完整的脚本化示例与验收测试见
[tests/test_invoice_evaluation.py](tests/test_invoice_evaluation.py)：

```bash
uv run pytest -q tests/test_invoice_evaluation.py
```

任务通过 `Task.define(invoice_documents=...)` 接收供应商发票单据，
`task.render_documents()` 将其渲染为固定的不含税文本。单据行包含供应商物料描述、
数量和十进制单价；Agent 录入时自行提交采购订单行的对应关系。单据中的行号是
发票行号，不是采购订单行号。同一张单据可以出现多份，用于考察重复单据处理。

`initial_supplier_invoices` 单独声明系统中已经存在的发票（包括预置已拒收发票）。
提供发票单据本身不会把发票录入系统。每次 `EvaluationRun.start(task)` 都重建初始
业务数据、执行轨迹和工具层的幂等键记忆。

通过 `run.reference_tools()` 取得当前运行的参考工具层，使用
`enter_supplier_invoice(logical_request_id=..., supplier=..., invoice_no=..., lines=...)`
录入，并通过 `supplier_invoices()` 和 `payables()` 查询。工具层使用任务声明的身份；
录入和查询要求应付会计角色。同一逻辑请求且内容不变时复用幂等键，内容改变时使用新键。
`EvaluationRun` 也提供相同业务操作；直接录入时由调用方提供 `idempotency_key`。

当前切片验证录入、查询、权限、重复单据、幂等和运行隔离；录入不改变库存或产生应付。
三单匹配、实际拒收、审批、过账、自动评分及真实模型接入仍待后续实现。
