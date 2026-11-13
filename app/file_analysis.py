"""LangGraph upload analysis with a bounded local tool and narration."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.config import Settings
from app.file_formula import normalize
from app.file_planner import binary_one, expected_mode, plan_file, uses_shared_marker
from app.file_sandbox import run_sandbox
from app.file_selection import selection
from app.telemetry import span
from sandbox.step7.protocol import (
    MAX_UPLOAD_BYTES, AnalysisRequest, AnalysisResult, FileFormat, FileLayout, FilePreview, FileSchema, InspectRequest, PreviewRequest,
    ProfilePlan, TaskRequest, TaskResult, WorkerRequest, WorkerResult,
)


class _State(TypedDict, total=False):
    filename: str
    layout: FileLayout
    data: bytes
    question: str
    plan: AnalysisRequest
    result: AnalysisResult | TaskResult
    narrative: str


@dataclass(frozen=True)
class FileAnalysis:
    report: AnalysisResult | TaskResult | None
    narrative: str
    kind: str = "file"


def _validate_upload(filename: str, data: bytes, question: str | None = None) -> FileFormat:
    suffix = Path(filename).suffix.lower().removeprefix(".")
    if suffix not in {"csv", "xlsx", "xls"}:
        raise ValueError("请选择 CSV、XLSX 或 XLS 文件")
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("文件为空或超过 10 MiB")
    if question is not None and not 1 <= len(question.strip()) <= 500:
        raise ValueError("问题长度应为 1–500 个字符")
    return suffix


def _plan(state: _State) -> _State:
    suffix = _validate_upload(state["filename"], state["data"], state["question"])
    question = state["question"].strip().casefold()
    if expected_mode(question) or selection(question).filtering or selection(question).sorting or any(word in question for word in (
        "真实观测", "来源标记", "填充", "插补", "估算", "分组", "按年", "按月", "按省", "按地区",
        "占比", "同比", "环比", "排名", "group by", "imput", "provenance",
    )):
        raise ValueError("这个任务需要启用模型规划。离线模式支持基础数值概况和异常值检查")
    if any(word in question for word in ("异常", "离群", "outlier", "anomal")):
        action = "outliers"
    elif any(word in question for word in ("统计", "清洗", "概况", "分布", "summary", "profile")):
        action = "summary"
    else:
        raise ValueError("离线模式支持基础数值概况和异常值检查。具体任务可启用模型规划")
    chart = action == "outliers" or any(word in question for word in ("图", "chart", "plot"))
    return {"plan": AnalysisRequest(format=suffix, action=action, chart=chart, layout=state["layout"])}


def _dispatch(request: WorkerRequest, data: bytes) -> WorkerResult:
    socket = os.getenv("RAG_SANDBOX_SOCKET")
    if socket:
        from app.sandbox_rpc import analyze_via_broker

        return analyze_via_broker(Path(socket), request, data)
    return run_sandbox(request, data)


def _analyze(state: _State) -> _State:
    with span("sandbox_analysis"):
        result = _dispatch(state["plan"], state["data"])
    if not isinstance(result, AnalysisResult) and not (isinstance(result, TaskResult) and result.status == "clarification"):
        raise ValueError("分析容器返回了意外的结果")
    return {"result": result}


def _narrate(state: _State) -> _State:
    report = state["result"]
    if isinstance(report, TaskResult):
        return {"narrative": report.message}
    lines = [f"表头从第 {report.header_row} 行起，共 {report.header_depth} 行。",
             f"分析了 {report.rows:,} 行、{report.columns} 列，其中 {len(report.numeric)} 列可做数值统计。"]
    if report.dropped_blank_rows:
        lines.append(f"排除了 {report.dropped_blank_rows:,} 个全空数据行。")
    if report.duplicate_rows:
        lines.append(f"发现 {report.duplicate_rows:,} 个重复数据行，保留在本次统计中。")
    invalid = sum(column.invalid for column in report.numeric)
    if invalid:
        lines.append(f"数值统计排除了 {invalid:,} 个无法转换或非有限的值。")
    eligible = [column for column in report.numeric if column.q1 is not None]
    if state["plan"].action == "summary":
        for column in report.numeric[:5]:
            lines.append(f"{column.name}：中位数 {column.median:g}，范围 {column.minimum:g} 到 {column.maximum:g}。")
        if not report.numeric:
            lines.append("没有可统计的数值列。")
    elif not eligible:
        lines.append("当前没有足够数据可用 IQR 检查异常值，每列至少需要 4 个有效数值。")
    else:
        unusual = sorted((column for column in eligible if column.outlier_count), key=lambda column: -column.outlier_count)
        cells = sum(column.outlier_count for column in unusual)
        lines.append(f"用 1.5 倍 IQR 检查了 {len(eligible)} 列，标记了 {cells:,} 个疑似异常值。")
        for column in unusual[:5]:
            samples = "、".join(f"数据行 {sample.row} 的 {sample.value:g}" for sample in column.examples[:3])
            lines.append(
                f"{column.name}：{column.outlier_count:,} 个。IQR 判定区间为 "
                f"[{column.lower_fence:g}, {column.upper_fence:g}]，例如 {samples}。"
            )
    return {"narrative": "\n".join(lines)}


builder = StateGraph(_State)
builder.add_node("plan", _plan)
builder.add_node("sandbox_analyze", _analyze)
builder.add_node("narrate", _narrate)
builder.add_edge(START, "plan")
builder.add_edge("plan", "sandbox_analyze")
builder.add_edge("sandbox_analyze", "narrate")
builder.add_edge("narrate", END)
_graph = builder.compile()


def preview_upload(filename: str, data: bytes, layout: FileLayout) -> FilePreview:
    file_format = _validate_upload(filename, data)
    with span("file_preview"):
        result = _dispatch(PreviewRequest(format=file_format, layout=layout), data)
    if not isinstance(result, FilePreview):
        raise ValueError("分析容器未返回有效的文件预览")
    return result


def _resolve_sheet(schema: FileSchema, layout: FileLayout, question: str):
    if layout.sheet is not None:
        return next((sheet for sheet in schema.sheets if sheet.index == layout.sheet), None)
    if (schema.sheet_count or len(schema.sheets)) == 1 and not schema.extra_sheets:
        return schema.sheets[0]
    text = normalize(question)
    named = []
    for sheet in schema.sheets:
        name = normalize(sheet.name)
        escaped = re.escape(name)
        if name and (re.search(rf'["“「]{escaped}["”」]', text)
                     or re.search(rf"(?:在|从|使用|选择|工作表){escaped}(?:工作表|表(?:里|内|中)?|里|内|中|$)", text)):
            named.append(sheet)
    if not named:
        named = [sheet for sheet in schema.sheets if len(normalize(sheet.name)) >= 3 and normalize(sheet.name) in text]
    return named[0] if len(named) == 1 else None


def _analyze_planned(file_format: FileFormat, data: bytes, question: str, settings: Settings | None,
                     schema: FileSchema) -> FileAnalysis:
    with span("file_plan"):
        decision = plan_file(settings, schema, question)
    if decision.kind != "run":
        return FileAnalysis(None, decision.message.strip(), decision.kind)

    sheet = next(item for item in schema.sheets if item.index == decision.sheet)
    request = TaskRequest(
        format=file_format, sheet=decision.sheet, plan=decision.plan,
        header_row=sheet.header_row, header_depth=sheet.header_depth,
        binary_one=binary_one(question), shared_marker=uses_shared_marker(question),
    )
    with span("sandbox_analysis"):
        result = _dispatch(request, data)
    if isinstance(result, TaskResult):
        if result.status == "clarification":
            return FileAnalysis(None, result.message, "clarification")
        lines = [f"分析工作表「{sheet.name}」，表头从第 {sheet.header_row} 行起，共 {sheet.header_depth} 行。"]
        if scope := result.scope:
            if scope.conditions:
                conditions = " 且 ".join(scope.conditions)
                lines.append(f"筛选条件「{conditions}」，参与计算 {scope.matched_rows:,} / {scope.input_rows:,} 行。")
            else:
                lines.append(f"全量数据参与计算，共 {scope.input_rows:,} 行。")
        lines.append(result.message)
        lines.extend(
            f"「{table.title}」请求前 {table.limit} 条，返回 {len(table.rows)} / {table.total_groups} 条可排名结果。"
            if table.limit is not None else
            f"「{table.title}」展示 {len(table.rows)} / {table.total_groups} 条结果。"
            for table in result.tables if table.limit is not None or table.total_groups > len(table.rows)
        )
        return FileAnalysis(result, "\n".join(lines))
    if isinstance(request.plan, ProfilePlan) and isinstance(result, AnalysisResult):
        profile = AnalysisRequest(format=file_format, action=request.plan.mode, chart=request.plan.chart)
        text = _narrate({"plan": profile, "result": result})["narrative"]
        return FileAnalysis(result, f"分析工作表「{sheet.name}」。\n{text}")
    raise ValueError("分析容器返回了意外的结果")


def analyze_upload(filename: str, data: bytes, question: str, settings: Settings | None = None,
                   *, layout: FileLayout | None = None) -> FileAnalysis:
    file_format = _validate_upload(filename, data, question)
    layout = layout or FileLayout()
    with span("upload_workflow"):
        mode = expected_mode(question)
        wanted = selection(question, coverage=mode == "coverage", audit=mode == "audit")
        planned = settings is not None or mode in {"audit", "coverage"} or (
            mode is None and (wanted.sorting or wanted.unsupported)
        )
        if planned or file_format != "csv" or layout != FileLayout():
            with span("file_schema"):
                schema = _dispatch(InspectRequest(format=file_format, layout=layout), data)
            if not isinstance(schema, FileSchema):
                raise ValueError("分析容器未返回有效的工作表结构")
            sheet = _resolve_sheet(schema, layout, question)
            if sheet is None:
                return FileAnalysis(None, "文件含多个工作表。请先在预览中选择要分析的工作表。", "clarification")
            if sheet.issue:
                return FileAnalysis(None, sheet.issue, "clarification")
            layout = FileLayout(sheet=sheet.index, header_row=sheet.header_row, header_depth=sheet.header_depth)
            if planned:
                return _analyze_planned(file_format, data, question, settings, FileSchema(sheets=[sheet]))
        state = _graph.invoke({"filename": filename, "data": data, "question": question, "layout": layout})
    if isinstance(state["result"], TaskResult):
        return FileAnalysis(None, state["narrative"], "clarification")
    return FileAnalysis(report=state["result"], narrative=state["narrative"])
