"""Streamlit conversation, approvals, and file analysis over the Step 8 API."""
from __future__ import annotations

import hashlib
import json
import os
from uuid import UUID, uuid4

import httpx
import streamlit as st

from app.file_preview_ui import choose_layout
from app.gateway_identity import KEY_HEADER, USER_HEADER, configured_key, gateway_user

API = os.getenv("RAG_API_URL", "http://127.0.0.1:8000")
STAGES = {
    "conversation_ask": "读取会话并理解问题",
    "conversation_decide": "核对审批并记录结果",
    "schema_retrieval": "检索表和字段",
    "column_notes": "整理字段补注",
    "model_call": "调用模型生成 SQL",
    "sql_dry_run": "验证 SQL",
    "sql_execute": "执行只读查询",
    "upload_workflow": "读取文件分析请求",
    "file_schema": "检查工作表和列结构",
    "file_preview": "读取工作表和预览行",
    "file_plan": "根据你的问题制定分析计划",
    "sandbox_analysis": "在独立容器中计算",
}


def get_json(path: str) -> dict:
    response = httpx.get(API + path, headers=GATEWAY_HEADERS, timeout=8, trust_env=False)
    response.raise_for_status()
    return response.json()


def invoke(path: str, **kwargs) -> tuple[dict, dict]:
    result, trace, error = {}, {}, None
    with st.status("正在处理", expanded=True) as status:
        try:
            headers = {**kwargs.pop("headers", {}), **GATEWAY_HEADERS}
            with httpx.stream("POST", API + path, headers=headers, timeout=180, trust_env=False, **kwargs) as response:
                if response.is_error:
                    detail = json.loads(response.read()).get("detail")
                    raise ValueError(detail or "请求未完成")
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event["event"] == "stage":
                        stage = event["stage"]
                        label = "调用模型规划表格分析" if path == "/api/v1/files" and stage == "model_call" else STAGES.get(stage, stage)
                        st.write(label)
                    elif event["event"] == "result":
                        result = event["data"]
                    elif event["event"] == "error":
                        error = event["message"]
                    elif event["event"] == "done":
                        trace = event
            if not trace:
                raise ValueError("连接提前结束，请重试或查看当前会话状态")
        except httpx.HTTPError:
            error = "暂时无法连接 API"
        except (KeyError, ValueError) as exc:
            error = str(exc)
        if error:
            result = {"kind": "error", "message": error}
        kind = result.get("kind")
        status.update(
            label={
                "error": "处理未完成", "clarification": "等待你补充", "approval": "等待你审批",
                "unsupported": "需要调整分析任务",
            }.get(kind, "处理完成"),
            state="error" if kind == "error" else "complete", expanded=False,
        )
    return result, trace


def table_data(columns: list[str], rows: list[list]) -> dict:
    table = {}
    for position, name in enumerate(columns):
        label = name or f"列 {position + 1}"
        suffix = 2
        unique = label
        while unique in table:
            unique = f"{label} ({suffix})"
            suffix += 1
        table[unique] = [row[position] for row in rows]
    return table


def render(result: dict, trace: dict | None = None, chart_key: str | None = None) -> None:
    kind = result.get("kind")
    if kind == "answer":
        st.markdown(f"营收 **¥{result['revenue_cny']}**")
    elif kind == "clarification":
        if original := result.get("original_question"):
            st.caption(f"待确认的问题 · {original}")
        st.text(result.get("question") or result["message"])
    elif kind == "approval":
        st.text(f"订单 {result['order_id']}，新备注「{result['note']}」。{result['question']}")
    elif kind == "write_result":
        labels = {"applied": "已更新备注", "denied": "已拒绝", "not_found": "未找到订单"}
        st.text(f"订单 {result['order_id']}，{labels.get(result['status'], result['status'])}。")
    elif kind == "error":
        st.error(result.get("message", "处理失败"))
    else:
        st.text(result.get("message", ""))
    if kind in {"answer", "clarification"} and (context := result.get("context")):
        time = {"last_month": "上月", "this_month": "本月"}.get(context.get("period"), context.get("period"))
        if kind == "answer":
            time = f"{result['date_start']} 至 {result['date_end_exclusive']}（截止日不含）"
        region = context.get("region") or "全部地区"
        metric = {"net": "净营收（扣除退款）", "gross": "毛营收（未扣除退款）"}.get(context.get("metric"), "待确认")
        st.caption(f"时间 · {time or '待确认'}　地区 · {region}　口径 · {metric}")
    if result.get("sql"):
        with st.expander("查看 SQL"):
            st.code(result["sql"], language="sql")
    if kind == "query" and result["columns"]:
        st.dataframe(table_data(result["columns"], result["rows"]), hide_index=True)
        if result["truncated"]:
            st.caption("结果较多，当前展示前 50 行。")
    if kind == "file":
        report = result["report"]
        tables = report.get("tables", [])
        if tables:
            for tab, table in zip(st.tabs([item["title"] for item in tables]), tables, strict=True):
                with tab:
                    rows = table["rows"]
                    st.dataframe(table_data(table["columns"], rows), width="stretch", hide_index=True)
                    if table.get("limit") is not None:
                        st.caption(f"前 {table['limit']} 条 · 返回 {len(rows)} / {table['total_groups']} 条可排名结果")
                    elif table["total_groups"] > len(rows):
                        st.caption(f"展示 {len(rows)} / {table['total_groups']} 条结果")
        if report["figure"] is not None:
            st.plotly_chart(report["figure"], key=chart_key, width="stretch", config={"displaylogo": False})
    if trace:
        with st.expander("本次追踪"):
            st.caption(f"{trace['trace_id']} · {trace['latency_ms']:.0f} ms · {trace['model_calls']} 次模型调用")
            if trace["model_calls_with_usage"] < trace["model_calls"]:
                st.caption("部分模型调用未返回 token 统计。")
            st.json(trace["usage"])


def request_preview(uploaded, params: dict) -> dict:
    result, _ = invoke("/api/v1/files/preview", params={"filename": uploaded.name, **params},
                       headers={"Content-Type": "application/octet-stream"}, content=uploaded.getvalue())
    return result


st.set_page_config(page_title="Text2SQL Agent", layout="wide")
GATEWAY_KEY = configured_key()
GATEWAY_HEADERS = {}
if GATEWAY_KEY is not None:
    user = gateway_user(st.context.headers, GATEWAY_KEY)
    if user is None:
        st.error("请通过已配置的 HTTPS 入口登录。")
        st.stop()
    GATEWAY_HEADERS = {KEY_HEADER: GATEWAY_KEY, USER_HEADER: user}

st.title("和数据对话")
st.caption("查询数据库、继续上轮问题，或上传表格分析。")

try:
    session_id = str(UUID(st.query_params.get("session", "")))
except ValueError:
    session_id = str(uuid4())
    st.query_params["session"] = session_id
st.session_state.setdefault("conversations", {})
st.session_state.conversations.setdefault(session_id, [])
history = st.session_state.conversations[session_id]

if st.sidebar.button("新建会话"):
    st.query_params["session"] = str(uuid4())
    st.rerun()

try:
    capabilities = get_json("/api/v1/databases")
except (httpx.HTTPError, ValueError):
    st.error("暂时无法连接 API，请确认服务已启动。")
    st.stop()

mode = st.sidebar.radio("工作区", ["对话", "文件分析"])
if mode == "文件分析":
    live = st.checkbox("用已配置的模型理解分析任务", value=capabilities["model_configured"] and GATEWAY_KEY is None,
                       disabled=not capabilities["model_configured"])
    if live:
        st.caption("需要模型规划时，会发送你的问题、工作表名和列名。原始表格在本机容器内读取和计算。")
    else:
        st.caption("离线可做数值概况、异常值检查、跨年覆盖对照和字段明确的单年排名。")
    uploaded = st.file_uploader("上传数据文件", type=["csv", "xlsx", "xls"], max_upload_size=10)
    digest = hashlib.sha256(uploaded.getbuffer()).hexdigest() if uploaded is not None else None
    layout = choose_layout(uploaded.name, digest, lambda params: request_preview(uploaded, params)) if uploaded else None
    question = st.text_area("你想分析什么？", placeholder="例如：2021 年人口最多 5 城市并画图", max_chars=500)
    fingerprint = (
        (uploaded.name, digest, question.strip(), live, layout)
        if uploaded is not None else None
    )
    if st.button("开始分析", type="primary", disabled=layout is None):
        st.session_state.pop("file_task_result", None)
        if uploaded is None:
            st.error("请先上传数据文件。")
        elif not question.strip():
            st.warning("请先写下你要分析的问题。")
        else:
            result, trace = invoke(
                "/api/v1/files", params={"filename": uploaded.name, "question": question, "live": live, **layout},
                headers={"Content-Type": "application/octet-stream"}, content=uploaded.getvalue(),
            )
            st.session_state["file_task_result"] = (fingerprint, result, trace)
    saved = st.session_state.get("file_task_result")
    if saved and saved[0] == fingerprint:
        st.caption("本次分析问题")
        st.text(saved[0][2])
        render(saved[1], saved[2], chart_key="file-chart")
    elif saved and uploaded is not None:
        st.info("文件或问题已修改。点击「开始分析」查看这次的结果。")
else:
    database = st.sidebar.selectbox(
        "数据源", [None, *capabilities["databases"]],
        format_func=lambda value: value if value else "演示订单库",
    )
    live = st.sidebar.checkbox(
        "使用已配置的模型", value=capabilities["model_configured"],
        disabled=not capabilities["model_configured"],
    )
    evidence = ""
    if database:
        evidence = st.sidebar.text_input("补充线索（选填）", max_chars=1500, key=f"evidence-{database}")
        st.caption(f"当前数据源为 {database}。可以继续追问，也可以在侧栏切换数据源。")
    else:
        st.caption("可以问订单数量、退款、地区对比或订单明细，也可以直接问「你能做什么？」。")
    for role, content, trace in history:
        with st.chat_message(role):
            if role == "user":
                st.text(content)
            else:
                render(content, trace)
    try:
        pending = get_json(f"/api/v1/sessions/{session_id}")
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法读取会话状态。")
        st.stop()
    if pending["kind"] == "clarification" and not history:
        with st.chat_message("assistant"):
            render(pending)
    if pending["kind"] in {"approval", "approval_recorded"}:
        if database:
            st.info("待审批操作来自演示订单库。")
        if not history:
            render(pending)
        yes, no = st.columns(2)
        approve = yes.button("批准更新", type="primary")
        deny = no.button("拒绝更新", disabled=pending["kind"] == "approval_recorded")
        if approve or deny:
            with st.chat_message("assistant"):
                result, trace = invoke("/api/v1/approvals", json={
                    "session_id": session_id, "approval_id": pending["approval_id"], "approve": approve,
                })
                history.append(("assistant", result, trace))
            st.rerun()
    quick_reply = None
    options = pending.get("options", []) if pending["kind"] == "clarification" and database is None else []
    if options:
        for button, option in zip(st.columns(len(options)), options, strict=True):
            if button.button(option, key=f"clarify-{option}"):
                quick_reply = option
    composer = st.empty()
    question = composer.chat_input("输入你的问题", max_chars=500, disabled=pending["kind"] in {"approval", "approval_recorded"})
    question = quick_reply or question
    if question:
        composer.empty()
        with st.chat_message("user"):
            st.text(question)
        with st.chat_message("assistant"):
            result, trace = invoke("/api/v1/chat", json={
                "session_id": session_id, "question": question, "live": live,
                "database": database, "evidence": evidence,
            })
        history.extend([("user", question, None), ("assistant", result, trace)])
        st.rerun()
