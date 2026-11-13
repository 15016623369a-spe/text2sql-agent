"""Streamlit conversation, approvals, and file analysis over the Step 8 API."""
from __future__ import annotations

import hashlib
import json
import os
import re
from functools import partial
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import streamlit as st

from app.file_downloads import csv_table, table_rows, xlsx_table
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
    "sql_semantic_check": "核对查询口径",
    "sql_result_check": "核对结果形状",
    "sql_execute": "执行只读查询",
    "upload_workflow": "读取文件分析请求",
    "file_schema": "检查工作表和列结构",
    "file_preview": "读取工作表和预览行",
    "file_plan": "根据你的问题制定分析计划",
    "sandbox_analysis": "在独立容器中计算",
}
CLARIFICATION_FIELDS = {"period": "时间范围", "region": "地区", "metric": "是否扣除退款"}
DEMO_QUESTIONS = (
    ("上月净营收", "上月净营收是多少？"),
    ("各地区订单数", "按地区统计订单数量"),
    ("上月退款总额", "上月退款总额是多少？"),
)


def render_workspace_header(file_analysis: bool) -> None:
    title, description = (
        ("文件分析", "上传 CSV 或 Excel，确认表头，再做排名、图表与公式核对。")
        if file_analysis else
        ("和数据对话", "用自然语言查数据库，继续追问，并核对结果背后的 SQL。")
    )
    st.html(
        '<header class="jianshu-hero">'
        '<div class="jianshu-hero-copy">'
        f'<h1>{title}</h1><p>{description}</p>'
        '</div><span class="jianshu-hero-pattern" aria-hidden="true"></span>'
        '</header>'
    )


def render_demo_questions() -> str | None:
    with st.container(key="demo-questions"):
        st.markdown("#### 试问演示订单库")
        for column, (label, question) in zip(st.columns(len(DEMO_QUESTIONS)), DEMO_QUESTIONS, strict=True):
            if column.button(label, key=f"example-{label}", width="stretch"):
                return question
    return None


def get_json(path: str, *, params: dict | None = None) -> dict:
    response = httpx.get(API + path, params=params, headers=GATEWAY_HEADERS, timeout=8, trust_env=False)
    response.raise_for_status()
    return response.json()


def invoke(path: str, **kwargs) -> tuple[dict, dict]:
    result, trace, error = {}, {}, None
    with st.status("正在处理", expanded=True) as status:
        try:
            headers = {**kwargs.pop("headers", {}), **GATEWAY_HEADERS}
            with httpx.stream("POST", API + path, headers=headers, timeout=180, trust_env=False, **kwargs) as response:
                if response.is_error:
                    detail = json.loads(response.read())
                    error = {"message": detail.get("detail") or "请求未完成", **{
                        key: detail[key] for key in ("stop_reason", "suggestion", "trace_id") if detail.get(key)
                    }}
                for line in (() if response.is_error else response.iter_lines()):
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
                        error = {key: event[key] for key in ("message", "stop_reason", "suggestion", "trace_id") if key in event}
                    elif event["event"] == "done":
                        trace = event
            if not trace and not error:
                raise ValueError("连接提前结束，请重试或查看当前会话状态")
        except httpx.HTTPError:
            error = {"message": "暂时无法连接 API", "suggestion": "确认服务已启动后重试。"}
        except (KeyError, ValueError) as exc:
            error = {"message": str(exc)}
        if error:
            result = {"kind": "error", **error}
        kind = result.get("kind")
        status.update(
            label={
                "error": "处理未完成", "clarification": "等待你补充", "approval": "等待你审批",
                "unsupported": "需要调整分析任务", "abstained": "查询需要核对",
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


def query_csv(path: str, headers: dict) -> bytes:
    response = httpx.get(API + path, headers=headers, timeout=15, trust_env=False)
    if response.status_code == 404:
        raise ValueError("导出已过期或被清理，请重新执行查询后下载。")
    response.raise_for_status()
    return response.content


def render_file_tables(report: dict, key: str | None) -> None:
    tables = report.get("tables", [])
    if not tables and (profiles := report.get("numeric")):
        rows = [[column[field] for field in ("name", "valid", "missing", "invalid", "minimum", "median", "maximum", "outlier_count")]
                for column in profiles]
        tables = [{"title": "数值概况", "columns": ["列名", "有效值", "空值", "无效值", "最小值", "中位数", "最大值", "疑似异常值"],
                   "rows": rows, "total_groups": len(rows)}]
    if not tables:
        return
    for index, (tab, table) in enumerate(zip(st.tabs([item["title"] for item in tables]), tables, strict=True)):
        with tab:
            rows, columns, title = table["rows"], table["columns"], table["title"]
            st.dataframe(table_data(columns, rows), width="stretch", hide_index=True)
            if table.get("limit") is not None:
                st.caption(f"前 {table['limit']} 条 · 返回 {len(rows)} / {table['total_groups']} 条可排名结果")
            elif table["total_groups"] > len(rows):
                st.caption(f"展示 {len(rows)} / 共 {table['total_groups']} 行")
            full = table_rows(table)
            if full is None:
                st.caption("完整结果超过导出上限，请缩小筛选范围后重新分析。")
                continue
            filename = re.sub(r"[\\/:?*<>|\x00-\x1f]", "-", title)[:70]
            csv_button, xlsx_button = st.columns(2)
            csv_button.download_button("下载 CSV", partial(csv_table, columns, full), f"{filename}.csv", "text/csv",
                                       key=f"{key}-{index}-csv", on_click="ignore")
            xlsx_button.download_button("下载 Excel", partial(xlsx_table, title, columns, full), f"{filename}.xlsx",
                                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                        key=f"{key}-{index}-xlsx", on_click="ignore")


def render(result: dict, trace: dict | None = None, chart_key: str | None = None) -> None:
    kind = result.get("kind")
    if kind == "answer":
        st.metric("营收", f"¥{result['revenue_cny']}")
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
        if suggestion := result.get("suggestion"):
            st.write(suggestion)
        if trace_id := result.get("trace_id") or (trace or {}).get("trace_id"):
            st.caption(f"追踪编号 · {trace_id}")
    elif kind == "abstained":
        st.warning(result.get("message", "当前查询需要核对。"))
        if suggestion := result.get("suggestion"):
            st.write(suggestion)
    else:
        st.text(result.get("message", ""))
    if previous := result.get("refined_from"):
        st.caption(f"基于上一轮 · {previous}")
    if kind == "clarification" and (missing := CLARIFICATION_FIELDS.get(result.get("slot"))):
        st.caption(f"还需确认 · {missing}")
    if kind in {"answer", "clarification", "query"}:
        context = result.get("context") or {}
        slot = result.get("slot")
        time = {"last_month": "上月", "this_month": "本月"}.get(context.get("period"), context.get("period"))
        if kind == "answer":
            time = f"{result['date_start']} 至 {result['date_end_exclusive']}（截止日不含）"
        fields = []
        if time or slot == "period":
            fields.append(f"时间 · {time or '待确认'}")
        if kind == "answer" or context.get("region") or slot == "region":
            region = context.get("region") or ("待确认" if slot == "region" else "全部地区")
            fields.append(f"地区 · {region}")
        if excluded := result.get("excluded_regions"):
            fields.append(f"排除地区 · {'、'.join(excluded)}")
        if kind == "answer" or context.get("metric") or slot == "metric":
            metric = {"net": "净营收（扣除退款）", "gross": "毛营收（未扣除退款）"}.get(context.get("metric"), "待确认")
            fields.append(f"口径 · {metric}")
        if kind == "query":
            scope = result.get("query_scope") or {}
            if not time:
                fields.append("时间 · 全部时间" if scope.get("all_time") else "时间范围见 SQL")
            if not context.get("region") and not result.get("excluded_regions"):
                fields.append("地区 · 全部地区" if scope.get("all_regions") else "地区范围见 SQL")
            if not context.get("metric"):
                fields.append("指标见结果列")
        if source := result.get("data_source"):
            label = "PostgreSQL" if source == "postgres" else source
            fields.append(f"数据源 · {label}")
        if tables := result.get("source_tables"):
            fields.append(f"查询表范围 · {'、'.join(tables)}")
        if fields:
            if kind == "clarification" and any(context.values()):
                st.caption("已解析条件")
            st.caption("　".join(fields))
    if kind == "query" and (signals := (result.get("semantics") or {}).get("signals")):
        with st.expander("待核对的查询口径"):
            for signal in signals:
                st.text(signal["message"])
    if result.get("sql"):
        with st.expander("查看候选 SQL" if kind == "abstained" else "查看 SQL"):
            st.code(result["sql"], language="sql")
    if kind == "query" and result["columns"]:
        st.dataframe(table_data(result["columns"], result["rows"]), hide_index=True)
        if isinstance(total := result.get("total_rows"), int):
            st.caption(f"展示 {len(result['rows']):,} / 共 {total:,} 行")
        elif result["truncated"]:
            st.caption("结果较多，当前展示前 50 行。")
    if (path := result.get("export_url")) and re.fullmatch(r"/api/v1/results/[a-f0-9-]{36}/csv", path):
        st.download_button("下载完整 CSV", partial(query_csv, path, GATEWAY_HEADERS), "查询结果.csv", "text/csv",
                           key=f"query-export-{path}", on_click="ignore")
        st.caption("查询导出最多保留 24 小时。")
    elif reason := result.get("export_unavailable"):
        st.caption(reason)
    if kind == "file":
        report = result["report"]
        render_file_tables(report, chart_key)
        if report["figure"] is not None:
            figure = {**report["figure"], "layout": {
                **report["figure"]["layout"], "paper_bgcolor": "#FFFFFF", "plot_bgcolor": "#FFFFFF",
                "colorway": ["#147D60", "#438A9E", "#7E65A8", "#B26B34", "#5A7A53", "#9F5369"],
                "font": {"family": "Inter, system-ui, sans-serif", "color": "#244A3A"},
            }}
            st.plotly_chart(figure, key=chart_key, width="stretch", config={"displaylogo": False})
    if trace:
        with st.expander("本次追踪"):
            st.caption(f"{trace['trace_id']} · {trace['latency_ms']:.0f} ms · {trace['model_calls']} 次模型调用")
            if trace["model_calls_with_usage"] < trace["model_calls"]:
                st.caption("部分模型调用未返回 token 统计。")
            if (cost := trace.get("estimated_cost_usd")) is not None:
                rate_label = "（按高峰费率估算）" if trace.get("cost_basis") == "deepseek_v4_pro_peak" else "（估算）"
                st.caption(f"模型费用 · US${cost:.6f}{rate_label}")
            if timeline := trace.get("timeline"):
                st.dataframe({
                    "阶段": [STAGES.get(item["name"], item["name"]) for item in timeline],
                    "起始（ms）": [item["start_ms"] for item in timeline],
                    "耗时（ms）": [item["latency_ms"] for item in timeline],
                    "状态": ["失败" if item.get("error_type") else "完成" for item in timeline],
                }, hide_index=True, width="stretch")
            st.json(trace["usage"])


def request_preview(filename: str, data: bytes | None, params: dict) -> dict:
    if data is None:
        return {"kind": "error", "message": "暂时无法读取已保存文件，请刷新或重新上传。"}
    result, _ = invoke("/api/v1/files/preview", params={"filename": filename, **params},
                       headers={"Content-Type": "application/octet-stream"}, content=data)
    return result


def render_file_workspace(session_id: str, model_configured: bool) -> None:
    path = f"/api/v1/sessions/{session_id}/file-analysis"
    try:
        saved = get_json(path)
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法读取已保存的文件分析，请刷新重试。")
        return

    question_key = f"file-question-{session_id}"
    upload_key = f"file-upload-{session_id}"
    if saved:
        st.caption(f"已保存文件 · {saved['filename']}")
        if st.button("移除已保存文件和结果", key=f"remove-file-{session_id}"):
            try:
                response = httpx.delete(API + path, headers=GATEWAY_HEADERS, timeout=8, trust_env=False)
                response.raise_for_status()
            except httpx.HTTPError:
                st.error("移除失败，请重试。")
            else:
                st.session_state.pop(question_key, None)
                st.session_state[upload_key] = st.session_state.get(upload_key, 0) + 1
                st.rerun()

    live = st.checkbox(
        "用已配置的模型理解分析任务", key=f"file-model-{session_id}",
        value=model_configured and (saved["live"] if saved else GATEWAY_KEY is None),
        disabled=not model_configured,
    ) and model_configured
    if live:
        st.caption("需要模型规划时，会发送你的问题、工作表名和列名。原始表格在本机容器内读取和计算。")
    else:
        st.caption("离线可做数值概况、异常值检查、跨年覆盖对照、字段明确的单年排名和公式核对。")

    uploaded = st.file_uploader("上传数据文件", type=["csv", "xlsx", "xls"], max_upload_size=10,
                                key=f"{upload_key}-{st.session_state.get(upload_key, 0)}")
    data = None
    filename = uploaded.name if uploaded is not None else saved["filename"] if saved else None
    digest = hashlib.sha256(uploaded.getbuffer()).hexdigest() if uploaded is not None else saved["digest"] if saved else None

    def file_bytes():
        nonlocal data
        if data is None and uploaded is not None:
            data = uploaded.getvalue()
        if data is None and saved:
            try:
                response = httpx.get(API + path + "/upload", params={"trace_id": saved["trace"]["trace_id"]},
                                     headers=GATEWAY_HEADERS, timeout=15, trust_env=False)
                response.raise_for_status()
                data = response.content
            except httpx.HTTPError:
                return None
        return data

    layout = None
    if uploaded is not None:
        layout = choose_layout(filename, digest, lambda params: request_preview(filename, file_bytes(), params))
    elif saved:
        layout = saved["layout"]
        if st.checkbox("调整工作表或表头", key=f"adjust-layout-{session_id}"):
            layout = choose_layout(filename, digest, lambda params: request_preview(filename, file_bytes(), params),
                                   saved_layout=layout)
    question = st.text_area("你想分析什么？", value=saved["question"] if saved else "", key=question_key,
                            placeholder="例如：2021 年人口最多 5 城市并画图", max_chars=500)
    if st.button("开始分析", type="primary", disabled=layout is None):
        if not question.strip():
            st.warning("请先写下你要分析的问题。")
        elif (body := file_bytes()) is None:
            st.error("暂时无法读取已保存文件，请刷新或重新上传。")
        else:
            result, trace = invoke(
                "/api/v1/files", params={"filename": filename, "question": question, "live": live,
                                         "session_id": session_id, **layout},
                headers={"Content-Type": "application/octet-stream"}, content=body,
            )
            if result["kind"] == "error":
                render(result, trace)
            else:
                if trace.get("history_stored"):
                    st.rerun()
                st.warning("本次分析未保存，刷新后会恢复最近一次已保存的结果。")
                st.caption("本次分析问题")
                st.text(question.strip())
                render(result, trace, chart_key="file-chart")
                return
    if saved:
        st.caption(f"已保存的分析 · {saved['filename']}")
        st.text(saved["question"])
        render(saved["result"], saved["trace"], chart_key="file-chart")


def render_message(role: str, content: str | dict, trace: dict | None = None) -> None:
    with st.chat_message(role):
        if role == "user":
            st.text(content)
        else:
            render(content, trace)


def refresh_after_turn(view: dict, question: str, result: dict, trace: dict) -> None:
    view["before"] = None
    view["unsaved"] = None if trace.get("history_stored") else (question, result, trace)
    st.rerun()


def choose_postgres_tables(session_id: str, saved_tables: list[str]) -> list[str] | None:
    path = "/api/v1/databases/postgres/schema"
    try:
        catalog = get_json(path)["tables"]
    except (httpx.HTTPError, ValueError):
        st.sidebar.error("暂时无法读取 PostgreSQL 表结构。请刷新重试，或切换数据源。")
        return None
    tables = {table["name"]: table for table in catalog}
    if not tables:
        st.sidebar.info("当前 PostgreSQL 数据源没有可查询的表。")
        return None
    selected = st.sidebar.multiselect(
        "查询表范围", list(tables), default=[table for table in saved_tables if table in tables],
        help="留空时可查询当前数据源中的全部可用表。", key=f"postgres-tables-{session_id}",
    )
    with st.sidebar.expander("浏览表结构"):
        table_name = st.selectbox("查看表", list(tables), key=f"postgres-inspect-table-{session_id}")
        table = tables[table_name]
        st.text(f"{table['schema']}.{table_name}")
        if comment := table.get("comment"):
            st.text(comment)
        samples = st.checkbox("显示示例值", key=f"postgres-samples-{session_id}")
        if samples:
            try:
                table = get_json(path, params={"table": table_name, "include_samples": True})["tables"][0]
            except (httpx.HTTPError, ValueError, IndexError):
                st.warning("暂时无法读取示例值。")
        columns = table["columns"]
        details = {
            "列名": [column["name"] for column in columns],
            "类型": [column["type"] for column in columns],
            "注释": [column.get("comment") or "" for column in columns],
        }
        if samples:
            details["示例值"] = ["、".join(str(value) for value in column.get("samples", [])) for column in columns]
        st.dataframe(details, hide_index=True, width="stretch")
        if foreign_keys := table.get("foreign_keys"):
            st.caption("外键关系")
            st.dataframe({
                "列名": [key["column"] for key in foreign_keys],
                "引用表": [key["target_table"] for key in foreign_keys],
                "引用列": [key["target_column"] for key in foreign_keys],
            }, hide_index=True, width="stretch")
    return selected


st.set_page_config(page_title="见数 · Text2SQL Agent", layout="wide")
GATEWAY_KEY = configured_key()
GATEWAY_HEADERS = {}
user = None
if GATEWAY_KEY is not None:
    user = gateway_user(st.context.headers, GATEWAY_KEY)
    if user is None:
        st.error("请通过已配置的 HTTPS 入口登录。")
        st.stop()
    GATEWAY_HEADERS = {KEY_HEADER: GATEWAY_KEY, USER_HEADER: user}

st.html(Path(__file__).with_name("product_ui.css"))
st.sidebar.html(
    '<div class="jianshu-brand"><span class="jianshu-mark" aria-hidden="true">见</span>'
    '<strong>见数</strong></div>'
)

try:
    session_id = str(UUID(st.query_params.get("session", "")))
except ValueError:
    session_id = str(uuid4())
    st.query_params["session"] = session_id

if st.sidebar.button("新建会话"):
    st.query_params["session"] = str(uuid4())
    st.rerun()

try:
    capabilities = get_json("/api/v1/databases")
except (httpx.HTTPError, ValueError):
    st.error("暂时无法连接 API，请确认服务已启动。")
    st.stop()

mode = st.sidebar.radio("工作区", ["对话", "文件分析"],
                        index=int(st.query_params.get("workspace") == "file"), key="workspace-mode")
render_workspace_header(mode == "文件分析")
if mode == "文件分析" and st.query_params.get("workspace") != "file":
    st.query_params["workspace"] = "file"
elif mode == "对话" and st.query_params.get("workspace") == "file":
    del st.query_params["workspace"]
if mode == "文件分析":
    render_file_workspace(session_id, capabilities["model_configured"])
else:
    identity = (user, session_id)
    view = st.session_state.get("conversation_view")
    if view is None or view["identity"] != identity:
        view = {"identity": identity, "before": None, "unsaved": None}
        st.session_state["conversation_view"] = view
    try:
        session_path = f"/api/v1/sessions/{session_id}"
        pending = get_json(session_path)
        cursor = f"?before={view['before']}" if view["before"] is not None else ""
        page = get_json(f"{session_path}/messages{cursor}")
    except (httpx.HTTPError, ValueError):
        st.error("暂时无法读取会话，请刷新重试。")
        st.stop()

    history = page["messages"]
    databases = [None, *capabilities["databases"]]
    postgres_configured = capabilities.get("postgres", False)
    pending_source = pending.get("source") if pending["kind"] == "clarification" else None
    last_query = next((
        message["content"] for message in reversed(history)
        if message["role"] == "assistant" and message["content"].get("data_source")
    ), {})
    last_postgres_query = last_query if postgres_configured and last_query.get("data_source") == "postgres" else {}
    restored_source = pending_source
    if pending["kind"] != "clarification" and last_postgres_query:
        restored_source = "postgres"
    database = st.sidebar.selectbox(
        "数据源", databases, index=databases.index(restored_source) if restored_source in databases else 0,
        format_func=lambda value: "PostgreSQL" if value == "postgres" and postgres_configured else value or "演示订单库",
        key=f"database-{session_id}",
    )
    live = st.sidebar.checkbox(
        "使用已配置的模型", value=capabilities["model_configured"],
        disabled=not capabilities["model_configured"],
    )
    evidence = ""
    tables = []
    postgres_ready = True
    if database:
        evidence = st.sidebar.text_input("补充线索（选填）", max_chars=1500, key=f"evidence-{database}")
        if database == "postgres" and postgres_configured:
            saved = pending if pending_source == database else last_postgres_query or {}
            selected = choose_postgres_tables(session_id, view.get("postgres_tables", saved.get("source_tables", [])))
            postgres_ready = selected is not None
            if postgres_ready:
                tables = selected
                view["postgres_tables"] = selected
            scope = "、".join(tables) or "全部可用表"
            st.caption(f"当前数据源为 PostgreSQL。查询表范围 · {scope}")
        else:
            st.caption(f"当前数据源为 {database}。可以继续追问，也可以在侧栏切换数据源。")
    else:
        st.caption("当前数据源为演示订单库。")
    if view["before"] is not None and st.button("返回最新消息"):
        view["before"] = None
        st.rerun()
    if page["next_before"] is not None and st.button("查看更早消息"):
        view["before"] = page["next_before"]
        st.rerun()
    for message in history:
        render_message(message["role"], message["content"], message.get("trace"))
    unsaved = view["unsaved"]
    showing_pending = bool(history and history[-1]["content"] == pending)
    if unsaved and not any(message["trace_id"] == unsaved[2].get("trace_id") for message in history):
        st.caption("本次结果尚未写入会话历史。")
        render_message("user", unsaved[0])
        render_message("assistant", unsaved[1], unsaved[2])
        showing_pending = showing_pending or unsaved[1] == pending
    clarifying = (pending["kind"] == "clarification" and pending_source == database
                  and sorted(tables) == pending.get("source_tables", []) and postgres_ready)
    if clarifying and not showing_pending:
        render_message("assistant", pending)
    if pending["kind"] in {"approval", "approval_recorded"}:
        if database:
            st.info("待审批操作来自演示订单库。")
        if not showing_pending:
            render_message("assistant", pending)
        yes, no = st.columns(2)
        approve = yes.button("批准更新", type="primary")
        deny = no.button("拒绝更新", disabled=pending["kind"] == "approval_recorded")
        if approve or deny:
            with st.chat_message("assistant"):
                result, trace = invoke("/api/v1/approvals", json={
                    "session_id": session_id, "approval_id": pending["approval_id"], "approve": approve,
                })
            refresh_after_turn(view, "批准更新" if approve else "拒绝更新", result, trace)
    quick_reply = None
    if clarifying:
        options = [*pending.get("options", []), "取消这次澄清"]
        for button, option in zip(st.columns(len(options)), options, strict=True):
            if button.button(option, key=f"clarify-{option}"):
                quick_reply = option
    elif not history and not unsaved and database is None and pending["kind"] == "empty":
        quick_reply = render_demo_questions()
    composer = st.empty()
    question = composer.chat_input("输入你的问题", max_chars=500,
                                   disabled=not postgres_ready or pending["kind"] in {"approval", "approval_recorded"})
    question = quick_reply or question
    if question:
        composer.empty()
        with st.chat_message("user"):
            st.text(question)
        with st.chat_message("assistant"):
            result, trace = invoke("/api/v1/chat", json={
                "session_id": session_id, "question": question, "live": live,
                "database": database, "evidence": evidence,
                **({"tables": tables} if tables else {}),
            })
        refresh_after_turn(view, question, result, trace)
