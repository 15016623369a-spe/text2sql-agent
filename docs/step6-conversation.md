# Step 6 · 多轮澄清、会话持久化与人工审批

## 已实现

独立的电商演示以 **2026-11-10** 为数据基准日，使用六条合成订单。营收、销售额和营业额使用同一套口径，支持上月、本月、指定月份及演示地区。独立问题缺少月份或毛净口径时先澄清。短追问只覆盖用户明确改变的条件。超出演示范围的问题返回说明，避免忽略用户指定的条件。

LangGraph 用 SQLite Checkpointer 按会话 ID 保存已确认的查询上下文和待处理的澄清/审批。含糊的营收问题用 `interrupt` 询问月份或是否扣退款，再用 `Command(resume=...)` 恢复原问题。每次 CLI 调用都可以是一个新进程。读取数据复用 Step 5 的 Pydantic SQL 校验、dry run、只读执行与重试预算。产品中的固定演示营收使用本地模板。CLI 演示可以用 `--live` 让已配置的模型生成 SQL。[LangGraph 持久化](https://docs.langchain.com/oss/python/langgraph/persistence) · [中断与恢复](https://docs.langchain.com/oss/python/langgraph/interrupts)

审批演示只支持 **修改一条合成订单的备注**。审批节点暂停后，`decide` 要求相同会话、匹配的审批编号和明确决定。普通聊天即使输入「批准」也仍显示待审批。允许时通过参数化 SQL 更新，备注和去重记录在同一事务提交。拒绝时备注保持原值。读取演示库仍使用只读入口，BIRD 评测的连接和命令没有改动。

## 验收

实际启动独立进程，先问「上月营收多少？」并在澄清时退出。用同一会话重新启动后选择「扣除退款」，得到 **¥475.00**。接着分别在新进程问：

1. 「那华南区呢？」继承上月与净营收，得到 **¥175.00**。
2. 「那本月呢？」继承华南与净营收，得到 **¥90.00**。
3. 「那华东区呢？」继承本月与净营收，得到 **¥0.00**。

离线模板和真实模型均通过这组独立进程对照。不同会话的首次追问无法读取这个会话的上下文。审批验收在临时合成库使用测试决策，覆盖未批准、普通聊天、跨会话、拒绝、批准、重复恢复。另注入进程在备注事务提交后、checkpoint 写入前退出的情形，同一审批恢复后去重记录仍为一条。

早期实现的 35 项回归覆盖以上流程。当前测试另覆盖月份与退款口径的短答、独立问题的范围重置，以及带模型的澄清优先级。

## 运行

项目根目录使用已配置的 `.venv`。首次执行会初始化本地演示库和会话文件。要重新演示，请使用新的 `--session` 值。要运行真实模型，在 `ask` 之前加 `--live`，并在需要模型的每次调用中保留该选项。

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m app.step6_demo --session revenue-demo ask '上月营收多少？'
.venv/bin/python -m app.step6_demo --session revenue-demo ask '扣除退款'
.venv/bin/python -m app.step6_demo --session revenue-demo ask '那华南区呢？'
.venv/bin/python -m app.step6_demo --session revenue-demo ask '那本月呢？'
.venv/bin/python -m app.step6_demo --session revenue-demo ask '那华东区呢？'
```

审批时，先运行申请命令，再把输出的 `approval_id` 传给第二条命令。可用 `--deny` 拒绝。若要检查暂停状态，使用相同的 `--session` 调用 `status`。

```bash
.venv/bin/python -m app.step6_demo --session order-demo ask '将订单 1001 的备注改为已核对'
.venv/bin/python -m app.step6_demo --session order-demo decide --approval-id '<从上条输出复制>' --approve
```

默认本地状态位于已忽略的 `data/step6_demo/`，可用 `--root` 指向独立的演示目录。
