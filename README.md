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

录入路径验证录入、查询、权限、重复单据、幂等和运行隔离；录入不改变库存或产生应付。

三单匹配通过 `run.match_supplier_invoice(idempotency_key=..., supplier=..., invoice_no=...)`
或 `run.reference_tools().match_supplier_invoice(logical_request_id=..., supplier=..., invoice_no=...)`
调用，要求应付会计角色。系统将已录入或有差异的发票变为已匹配或有差异，结果中的
`invoice_status` 与 `differences` 同时写入执行轨迹，也可以通过发票查询读取。

匹配按采购订单行合计发票数量，单价高于或低于订单价都按绝对相对差比较，恰好 2%
在容差内。容差判断直接比较订单价的 98% 与 102%，避免除法舍入影响边界，计算精度
也不随输入指数差增长。零价订单与零价发票
无单价差异；非零发票价列为单价差异，相对差为 `None`（无定义）。未知订单、未知订单行
或供应商不一致会保留为可读取的对应差异，录入内容不变。非正数量及非有限或负单价
保留为非法数值差异；负数量不会抵消其他行的数量。录入时可通过 `invoice_line_no`
保留原始发票行号；省略时差异明细按录入顺序从 1 编号。

补登收货后重新匹配须使用新的逻辑请求标识或幂等键；旧请求重试仍返回首次匹配结果，
不会把发票退回旧状态。匹配不预占可开票数量，不产生应付，也不改变库存。
同一参考工具实例串行分配和提交幂等键；遇到直接调用已占用键造成的冲突时换键重试，
冲突仍保留在执行轨迹中。

匹配测试：

```bash
uv run pytest -q tests/test_three_way_match.py
```

当前尚无过账操作或已过账初始数据，因此匹配中的已过账数量恒为零，可开票数量等于
已收数量。#11 接入过账时须扣减累计已过账数量并补充验证。#9 的“数量差异不能靠审批
消除”也须待 #10 的审批接口完成后验收。实际拒收、审批、过账、自动评分及真实模型
接入仍待后续实现。
