"""Read one uploaded file from stdin and write analysis JSON to stdout."""
from __future__ import annotations

import json
import math
import re
import sys
from html import escape

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pydantic import TypeAdapter, ValidationError

from protocol import (
    MAX_CHART_COLUMNS, MAX_COLUMNS, MAX_EXAMPLES, MAX_PLAN_BYTES, MAX_ROWS, MAX_UPLOAD_BYTES,
    AnalysisRequest, AnalysisResult, AuditPlan, CoveragePlan, ColumnProfile, FileLayout, InspectRequest, Outlier, PreviewRequest,
    ProfilePlan, WorkerFailure, WorkerRequest,
)
from audit import audit_frame
from panel import analyze_panel
from tabular import inspect_layout, load_frame
from tasks import analyze_task, clean_label

_MIN_IQR_ROWS = 4
_MISSING_MARKERS = {"", "na", "n/a", "none", "null", "nan"}


_REQUEST = TypeAdapter(WorkerRequest)
def _numeric_profile(name: str, column: pd.Series) -> ColumnProfile | None:
    if pd.api.types.is_bool_dtype(column) or pd.api.types.is_datetime64_any_dtype(column):
        return None
    text = column.astype("string").str.strip()
    missing = text.isna() | text.str.lower().isin(_MISSING_MARKERS)
    text = text.mask(missing)
    grouped = text.str.fullmatch(r"[+-]?[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?", na=False)
    numeric = pd.to_numeric(text.mask(grouped, text.str.replace(",", "", regex=False)), errors="coerce")
    values = numeric.dropna().astype("float64")
    values = values[values.map(math.isfinite)]
    nonblank = len(column) - int(missing.sum())
    if not len(values) or (not pd.api.types.is_numeric_dtype(column) and len(values) < 0.8 * nonblank):
        return None

    result = dict(
        name=name, valid=len(values), missing=int(missing.sum()), invalid=nonblank - len(values),
        minimum=float(values.min()), maximum=float(values.max()), median=float(values.median()),
        outlier_count=0, examples=[],
    )
    if len(values) >= _MIN_IQR_ROWS:
        q1, q3 = map(float, values.quantile([0.25, 0.75]))
        spread = q3 - q1
        low, high = q1 - 1.5 * spread, q3 + 1.5 * spread
        if all(math.isfinite(v) for v in (q1, q3, low, high)):
            flagged = (values < low) | (values > high)
            unusual = values[flagged]
            within_fences = values[~flagged]
            examples = [
                Outlier(row=int(index) + 1, value=float(value))
                for index, value in unusual.iloc[:MAX_EXAMPLES].items()
            ]
            result.update(q1=q1, q3=q3, iqr=spread, lower_fence=low, upper_fence=high,
                          lower_whisker=float(within_fences.min()), upper_whisker=float(within_fences.max()),
                          outlier_count=len(unusual), examples=examples)
    return ColumnProfile(**result)


def _chart(profiles: list[ColumnProfile]) -> tuple[dict | None, list[str]]:
    eligible = [profile for profile in profiles if profile.q1 is not None]
    selected = sorted(eligible, key=lambda profile: -profile.outlier_count)[:MAX_CHART_COLUMNS]
    if not selected:
        return None, []
    cols = min(2, len(selected))
    rows = math.ceil(len(selected) / cols)
    figure = make_subplots(rows=rows, cols=cols, subplot_titles=[escape(p.name) for p in selected])
    for index, profile in enumerate(selected):
        row, col = divmod(index, cols)
        row += 1
        col += 1
        label = escape(profile.name)
        figure.add_trace(go.Box(
            x=[label], q1=[profile.q1], q3=[profile.q3], median=[profile.median],
            lowerfence=[profile.lower_whisker], upperfence=[profile.upper_whisker],
            boxpoints=False, name=label, marker_color="#168B84", showlegend=False,
        ), row=row, col=col)
        if profile.examples:
            figure.add_trace(go.Scatter(
                x=[label] * len(profile.examples), y=[p.value for p in profile.examples],
                customdata=[p.row for p in profile.examples], mode="markers", name="疑似异常值",
                marker={"size": 9, "color": "#C24828"}, showlegend=False,
                hovertemplate="数据行 %{customdata}<br>值 %{y}<extra></extra>",
            ), row=row, col=col)
    figure.update_layout(
        title="数值列分布与疑似异常值", height=300 * rows + 65, template="plotly_white",
        margin={"t": 78, "b": 35, "l": 35, "r": 25},
    )
    return json.loads(figure.to_json()), [p.name for p in selected]


def _clean_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not 1 <= len(frame.columns) <= MAX_COLUMNS or len(frame) > MAX_ROWS:
        raise ValueError(f"单张工作表最多分析 {MAX_ROWS:,} 行、{MAX_COLUMNS} 列")
    return frame.replace(r"^\s*$", None, regex=True).dropna(how="all")


def _profile_frame(frame: pd.DataFrame, cleaned: pd.DataFrame, columns: list[int], chart: bool) -> AnalysisResult:
    indices = columns or list(range(len(frame.columns)))
    if len(set(indices)) != len(indices) or any(index >= len(frame.columns) for index in indices):
        raise ValueError("分析计划引用了不存在或重复的列")
    profiles = []
    for index in indices:
        name = clean_label(frame.columns[index]) or f"列 {index + 1}"
        if profile := _numeric_profile(name, cleaned.iloc[:, index]):
            profiles.append(profile)
    figure, chart_columns = _chart(profiles) if chart else (None, [])
    return AnalysisResult(
        rows=len(cleaned), columns=len(frame.columns), dropped_blank_rows=len(frame) - len(cleaned),
        duplicate_rows=int(cleaned.duplicated().sum()), numeric=profiles, chart_columns=chart_columns, figure=figure,
    )


def analyze(data: bytes, request: WorkerRequest):
    if isinstance(request, (InspectRequest, PreviewRequest)):
        return inspect_layout(data, request.format, request.layout, preview=isinstance(request, PreviewRequest))
    layout = request.layout if isinstance(request, AnalysisRequest) else FileLayout(
        sheet=request.sheet, header_row=request.header_row, header_depth=request.header_depth,
    )
    frame, data_offset = load_frame(data, request.format, layout)
    cleaned = _clean_frame(frame)
    if isinstance(request, AnalysisRequest):
        return _profile_frame(frame, cleaned, [], request.chart)
    if isinstance(request.plan, ProfilePlan):
        return _profile_frame(frame, cleaned, request.plan.columns, request.plan.chart)
    if isinstance(request.plan, AuditPlan):
        return audit_frame(cleaned, request.plan, data_offset=data_offset)
    if isinstance(request.plan, CoveragePlan):
        return analyze_panel(cleaned, request.plan)
    return analyze_task(cleaned, request)


def main() -> int:
    try:
        header = sys.stdin.buffer.readline(MAX_PLAN_BYTES + 2)
        if len(header) > MAX_PLAN_BYTES + 1 or not header.endswith(b"\n"):
            raise ValueError("文件请求格式无效")
        request = _REQUEST.validate_json(header)
        data = sys.stdin.buffer.read(MAX_UPLOAD_BYTES + 1)
        if not data or len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("文件为空或超过 10 MiB")
        print(analyze(data, request).model_dump_json())
    except ValidationError:
        print(WorkerFailure(error="分析请求或结果格式无效").model_dump_json())
    except ValueError as exc:
        print(WorkerFailure(error=str(exc)).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
