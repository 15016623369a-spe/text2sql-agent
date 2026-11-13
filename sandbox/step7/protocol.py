"""Bounded data contracts shared by the host and Docker worker."""
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 50
MAX_CHART_COLUMNS = 6
MAX_EXAMPLES = 20
MAX_PREVIEW_REPLY_BYTES = 512 * 1024
MAX_REPLY_BYTES = 8 * 1024 * 1024

FileFormat = Literal["csv", "xlsx", "xls"]
MAX_SHEETS = 8
MAX_HEADER_ROW = 100
MAX_HEADER_DEPTH = 3
MAX_PREVIEW_ROWS = 10
SheetId = Annotated[int, Field(ge=0, lt=MAX_SHEETS)]
HeaderRow = Annotated[int, Field(ge=1, le=MAX_HEADER_ROW)]
HeaderDepth = Annotated[int, Field(ge=1, le=MAX_HEADER_DEPTH)]


class StrictPayload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class FileLayout(StrictPayload):
    sheet: SheetId | None = None
    header_row: HeaderRow | None = None
    header_depth: HeaderDepth | None = None


class AnalysisRequest(StrictPayload):
    format: FileFormat
    action: Literal["outliers", "summary"]
    chart: bool
    layout: FileLayout = Field(default_factory=FileLayout, exclude_if=lambda value: value == FileLayout())


class Outlier(StrictPayload):
    row: int = Field(ge=1)
    value: float


class ColumnProfile(StrictPayload):
    name: str
    valid: int = Field(ge=1)
    missing: int = Field(ge=0)
    invalid: int = Field(ge=0)
    minimum: float
    maximum: float
    median: float
    q1: float | None = None
    q3: float | None = None
    iqr: float | None = Field(default=None, ge=0)
    lower_fence: float | None = None
    upper_fence: float | None = None
    lower_whisker: float | None = None
    upper_whisker: float | None = None
    outlier_count: int = Field(ge=0)
    examples: list[Outlier] = Field(max_length=MAX_EXAMPLES)


class AnalysisResult(StrictPayload):
    rows: int = Field(ge=0, le=MAX_ROWS)
    columns: int = Field(ge=1, le=MAX_COLUMNS)
    dropped_blank_rows: int = Field(ge=0)
    duplicate_rows: int = Field(ge=0)
    numeric: list[ColumnProfile] = Field(max_length=MAX_COLUMNS)
    chart_columns: list[str] = Field(max_length=MAX_CHART_COLUMNS)
    figure: dict | None = None
    header_row: HeaderRow = 1
    header_depth: HeaderDepth = 1


class WorkerFailure(StrictPayload):
    error: str


MAX_PLAN_BYTES = 8192
MAX_GROUP_ROWS = 180
MAX_RANK_ROWS = 100

ColumnId = Annotated[int, Field(ge=0, lt=MAX_COLUMNS)]
ColumnIds = Annotated[list[ColumnId], Field(max_length=8)]
Group = Annotated[list[ColumnId], Field(min_length=1, max_length=2)]
Groupings = Annotated[list[Group], Field(max_length=3)]
Cell = str | int | float | None


class InspectRequest(StrictPayload):
    format: FileFormat
    action: Literal["inspect"] = "inspect"
    layout: FileLayout = Field(default_factory=FileLayout)


class PreviewRequest(StrictPayload):
    format: FileFormat
    action: Literal["preview"] = "preview"
    layout: FileLayout = Field(default_factory=FileLayout)


class SheetSchema(StrictPayload):
    index: int = Field(ge=0, lt=MAX_SHEETS)
    name: str = Field(max_length=120)
    columns: list[str] = Field(max_length=MAX_COLUMNS)
    numeric_columns: list[ColumnId] = Field(max_length=MAX_COLUMNS)
    header_row: HeaderRow = 1
    header_depth: HeaderDepth = 1
    issue: str | None = None


class FileSchema(StrictPayload):
    sheet_count: int | None = Field(default=None, ge=1, le=MAX_SHEETS)
    sheets: list[SheetSchema] = Field(max_length=MAX_SHEETS)
    extra_sheets: bool = False


class SheetPreview(StrictPayload):
    index: SheetId
    name: str = Field(max_length=120)
    header_row: HeaderRow
    header_depth: HeaderDepth
    columns: list[str] = Field(max_length=MAX_COLUMNS)
    sample_start: int = Field(ge=1)
    rows: list[list[Cell]] = Field(max_length=MAX_PREVIEW_ROWS)
    sample_included: bool | None = None
    issue: str | None = None
    warnings: list[str] = Field(default_factory=list, max_length=5)
    needs_confirmation: bool = False


class FilePreview(StrictPayload):
    sheets: list[SheetPreview] = Field(max_length=MAX_SHEETS)
    extra_sheets: bool = False


class ProfilePlan(StrictPayload):
    mode: Literal["summary", "outliers"]
    columns: ColumnIds = Field(default_factory=list)
    chart: bool = False


FilterNumber = Annotated[int, Field(ge=-10**15, le=10**15)] | Annotated[float, Field(ge=-10**15, le=10**15)]
FilterText = Annotated[str, Field(min_length=1, max_length=80)]
FilterValue = FilterNumber | FilterText


class Filter(StrictPayload):
    column: ColumnId
    operator: Literal["eq", "ne", "gt", "ge", "lt", "le", "in", "between", "contains"]
    value: FilterValue | Annotated[list[FilterValue], Field(min_length=1, max_length=20)]

    @model_validator(mode="after")
    def check_value(self):
        value = self.value
        if self.operator == "in":
            if not isinstance(value, list):
                raise ValueError("in 需要一个值列表")
        elif self.operator == "between":
            if (not isinstance(value, list) or len(value) != 2
                    or any(isinstance(item, str) for item in value) or value[0] > value[1]):
                raise ValueError("between 需要两个由小到大排列的数值")
        elif isinstance(value, list):
            raise ValueError("此筛选运算需要单个值")
        elif self.operator in {"gt", "ge", "lt", "le"} and isinstance(value, str):
            raise ValueError("大小比较需要数值")
        elif self.operator == "contains" and not isinstance(value, str):
            raise ValueError("contains 需要文本")
        values = value if isinstance(value, list) else [value]
        if any(isinstance(item, str) and not item.strip() for item in values):
            raise ValueError("筛选文本不能为空")
        return self


Filters = Annotated[list[Filter], Field(max_length=5)]


class OrderBy(StrictPayload):
    column: ColumnId | None
    direction: Literal["asc", "desc"]


class ProvenanceField(StrictPayload):
    metric: ColumnId
    marker: ColumnId


class ProvenancePlan(StrictPayload):
    mode: Literal["provenance"]
    filters: Filters = Field(default_factory=list)
    fields: list[ProvenanceField] = Field(min_length=1, max_length=6)
    groups: Groupings = Field(default_factory=list)
    chart: bool = False


class AggregatePlan(StrictPayload):
    mode: Literal["aggregate"]
    statistic: Literal["rows", "count", "sum", "mean", "median", "min", "max", "missing", "records"]
    columns: ColumnIds = Field(default_factory=list)
    groups: Groupings = Field(default_factory=list)
    chart: bool = False
    filters: Filters = Field(default_factory=list)
    labels: Annotated[list[ColumnId], Field(max_length=2)] = Field(default_factory=list)
    order_by: OrderBy | None = None
    limit: int | None = Field(default=None, ge=1, le=MAX_RANK_ROWS)

    def rank_problem(self) -> str | None:
        if self.limit is not None and self.order_by is None:
            return "请指定要按哪个指标排序，再选取前几名。"
        if self.statistic == "records":
            if self.groups or self.order_by is None or self.order_by.column not in self.columns:
                return "明细排名需要指定要排序的数值列，明细排名不使用分组。"
            if len(set(self.labels)) != len(self.labels) or set(self.labels) & set(self.columns):
                return "请为明细排名指定独立且不重复的名称列和指标列。"
        elif self.labels or (self.order_by is not None and not self.groups):
            return "请指定排名的分组，或按原表明细排序。"
        if self.order_by is not None:
            if self.statistic == "rows":
                if self.order_by.column is not None:
                    return "按样本行数排名时，请以行数为排序指标。"
            elif self.order_by.column not in self.columns:
                return "排序指标必须同时出现在统计指标中。"
        return None


class FormulaColumn(StrictPayload):
    kind: Literal["column"]
    column: ColumnId


class FormulaNumber(StrictPayload):
    kind: Literal["number"]
    value: float = Field(ge=-1e12, le=1e12)


class FormulaBinary(StrictPayload):
    kind: Literal["add", "subtract", "multiply", "divide"]
    left: "Formula"
    right: "Formula"


Formula = Annotated[FormulaColumn | FormulaNumber | FormulaBinary, Field(discriminator="kind")]
FormulaBinary.model_rebuild()


class AuditInputUnit(StrictPayload):
    column: ColumnId
    unit: str = Field(min_length=1, max_length=24)


class AuditCheck(StrictPayload):
    target: ColumnId
    formula: Formula
    unit: Annotated[str, Field(min_length=1, max_length=24)] | None = None
    input_units: list[AuditInputUnit] = Field(default_factory=list, max_length=8)
    basis: Literal["inferred", "explicit"] | None = None


class AuditPlan(StrictPayload):
    mode: Literal["audit"]
    checks: list[AuditCheck] = Field(min_length=1, max_length=3)
    labels: list[ColumnId] = Field(default_factory=list, max_length=3)
    tolerance: float | None = Field(default=None, ge=0, exclude_if=lambda value: value is None)


Year = Annotated[int, Field(ge=1000, le=2999)]


class CoveragePlan(StrictPayload):
    mode: Literal["coverage"]
    filters: Filters = Field(default_factory=list)
    year: ColumnId
    keys: list[ColumnId] = Field(min_length=1, max_length=2)
    labels: list[ColumnId] = Field(default_factory=list, max_length=2)
    years: list[Year] = Field(default_factory=list, max_length=2)
    measures: list[ColumnId] = Field(default_factory=list, max_length=3)


Plan = Annotated[ProfilePlan | ProvenancePlan | AggregatePlan | AuditPlan | CoveragePlan, Field(discriminator="mode")]


class TaskRequest(StrictPayload):
    format: FileFormat
    action: Literal["task"] = "task"
    sheet: int = Field(ge=0, lt=MAX_SHEETS)
    plan: Plan
    binary_one: Literal["observed", "imputed"] | None = None
    shared_marker: bool = False
    header_row: HeaderRow | None = None
    header_depth: HeaderDepth = 1


class ResultScope(StrictPayload):
    input_rows: int = Field(ge=0, le=MAX_ROWS)
    matched_rows: int = Field(ge=0, le=MAX_ROWS)
    conditions: list[str] = Field(max_length=5)


class ResultTable(StrictPayload):
    title: str = Field(max_length=250)
    columns: list[str] = Field(min_length=1, max_length=18)
    rows: list[list[Cell]] = Field(max_length=MAX_GROUP_ROWS)
    total_groups: int = Field(ge=0)
    limit: int | None = Field(default=None, ge=1, le=MAX_RANK_ROWS)
    additional_rows: list[list[Cell]] | None = Field(default=None, max_length=MAX_ROWS, exclude_if=lambda value: value is None)

    @classmethod
    def from_rows(cls, title: str, columns: list[str], rows: list[list[Cell]], *, limit: int | None = None,
                  preview_rows: int = MAX_GROUP_ROWS) -> "ResultTable":
        selected = rows[:limit] if limit is not None else rows
        return cls(title=title, columns=columns, rows=selected[:preview_rows], total_groups=len(rows), limit=limit,
                   additional_rows=selected[preview_rows:] or None)


class TaskResult(StrictPayload):
    status: Literal["ok", "clarification"] = "ok"
    message: str = Field(max_length=12000)
    scope: ResultScope | None = None
    tables: list[ResultTable] = Field(default_factory=list, max_length=4)
    figure: dict | None = None


WorkerRequest = AnalysisRequest | InspectRequest | PreviewRequest | TaskRequest
WorkerResult = AnalysisResult | FileSchema | FilePreview | TaskResult
