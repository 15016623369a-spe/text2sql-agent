"""The isolated database and tools used by the Step 6 commerce demo."""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.config import Settings
from app.llm import chat_completion
from app.sql_recovery import MAX_RECOVERIES, run_recovery
from app.telemetry import span
from app.text2sql import build_messages, schema_objects
from app.tools.db import ReadOnlyDB

AS_OF = date(2026, 11, 10)
APPLICATION_ID = 0x52414736
Metric = Literal["gross", "net"]
Period = Literal["last_month", "this_month"] | Annotated[str, StringConstraints(pattern=r"^20[0-9]{2}-(?:0[1-9]|1[0-2])$")]
Region = Literal["华南", "华东", "华北"]
ApprovalID = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]

_SCHEMA = """
CREATE TABLE orders(
    id INTEGER PRIMARY KEY,
    ordered_on TEXT NOT NULL,
    region TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    refund_cents INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE applied_approvals(
    approval_id TEXT PRIMARY KEY,
    order_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('applied', 'not_found'))
);
"""
_ORDERS = (
    (1001, "2026-10-03", "华南", 12000, 2000),
    (1002, "2026-10-21", "华南", 8000, 500),
    (1003, "2026-10-11", "华东", 30000, 5000),
    (1004, "2026-10-20", "华北", 5000, 0),
    (1005, "2026-11-02", "华南", 9000, 0),
    (1006, "2026-09-29", "华东", 7000, 0),
)


class QueryContext(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    period: Period | None = None
    metric: Metric | None = None
    region: Region | None = None


class NoteChange(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    approval_id: ApprovalID
    order_id: int = Field(gt=0, le=999_999_999)
    note: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


def _assert_demo_database(db: sqlite3.Connection) -> None:
    app_id, = db.execute("PRAGMA application_id").fetchone()
    if app_id != APPLICATION_ID:
        raise ValueError("该 SQLite 文件不是 Step 6 电商演示库")


def prepare_database(root: Path) -> Path:
    database = root / "commerce.sqlite"
    if database.exists():
        with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            _assert_demo_database(db)
        return database

    with tempfile.TemporaryDirectory(dir=root) as directory:
        staged = Path(directory) / database.name
        with closing(sqlite3.connect(staged)) as db, db:
            db.executescript(_SCHEMA)
            db.executemany(
                "INSERT INTO orders(id, ordered_on, region, amount_cents, refund_cents) VALUES (?, ?, ?, ?, ?)",
                _ORDERS,
            )
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        os.link(staged, database)
    return database


def apply_note(database: Path, change: NoteChange) -> dict:
    """Commit a fixed operation and its replay marker in the same transaction."""
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)) as db, db:
        _assert_demo_database(db)
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute(
            "SELECT order_id, status FROM applied_approvals WHERE approval_id = ?",
            (change.approval_id,),
        ).fetchone()
        if previous is not None:
            if previous[0] != change.order_id:
                raise ValueError("审批编号与已记录的订单不一致")
            status = previous[1]
        else:
            changed = db.execute("UPDATE orders SET note = ? WHERE id = ?", (change.note, change.order_id))
            status = "applied" if changed.rowcount == 1 else "not_found"
            db.execute(
                "INSERT INTO applied_approvals(approval_id, order_id, status) VALUES (?, ?, ?)",
                (change.approval_id, change.order_id, status),
            )
    return {"kind": "write_result", "approval_id": change.approval_id,
            "order_id": change.order_id, "status": status}


def _month_bounds(period: Period) -> tuple[str, str]:
    current = AS_OF.replace(day=1)
    if period == "last_month":
        previous = date(current.year - (current.month == 1), 12 if current.month == 1 else current.month - 1, 1)
        return previous.isoformat(), current.isoformat()
    if period != "this_month":
        current = date.fromisoformat(period + "-01")
    following = date(current.year + (current.month == 12), 1 if current.month == 12 else current.month + 1, 1)
    return current.isoformat(), following.isoformat()


def demo_filters(context: QueryContext, excluded_regions: tuple[Region, ...] = ()) -> list[str]:
    filters = []
    if context.period:
        start, end = _month_bounds(context.period)
        filters.extend((f"ordered_on >= '{start}'", f"ordered_on < '{end}'"))
    if context.region:
        filters.append(f"region = '{context.region}'")
    for region in excluded_regions:
        validated = QueryContext(region=region).region
        filters.append(f"region <> '{validated}'")
    return filters


def query_revenue(
    database: Path, context: QueryContext, settings: Settings | None, *, excluded_regions: tuple[Region, ...] = (),
) -> dict:
    if context.metric is None or context.period is None:
        raise ValueError("查询前需要确认月份和营收口径")
    start, end = _month_bounds(context.period)
    expression = "amount_cents - refund_cents" if context.metric == "net" else "amount_cents"
    conditions = " AND ".join(demo_filters(context, excluded_regions))
    sql = f"SELECT COALESCE(SUM({expression}), 0) AS revenue_cents FROM orders WHERE {conditions}"
    with ReadOnlyDB(database) as db:
        schema = schema_objects(db)["orders"]
    messages = build_messages(
        schema,
        f"Return one integer in cents: sum {expression} for all orders with {conditions}. "
        "Return zero if no orders match.",
        "Use the region and date predicates exactly as given.",
    )

    def request_model(prompt):
        if settings is not None:
            return chat_completion(settings, prompt, max_tokens=512, json_output=True)[1]
        return {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}

    def validate_sql(candidate):
        with ReadOnlyDB(database) as db:
            db.dry_run(candidate)

    def execute_sql(candidate):
        with ReadOnlyDB(database) as db:
            return db.query(candidate, limit=2)

    result, prediction = run_recovery(
        messages, {"tables": ["orders"]}, request_model=request_model, validate_sql=validate_sql,
        execute_sql=execute_sql, expand_scope=lambda *_: None, build_prompt=lambda _: messages,
        max_recoveries=MAX_RECOVERIES,
    )
    rows = prediction[1] if prediction else []
    if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not int:
        return {"kind": "error", "message": "营收查询未返回一项整数金额",
                "stop_reason": result["recovery_stop_reason"]}
    cents = rows[0][0]
    return {
        "kind": "answer", "context": context.model_dump(), "date_start": start, "date_end_exclusive": end,
        "revenue_cents": cents, "revenue_cny": f"{Decimal(cents) / 100:.2f}", "total_rows": 1,
        "sql": result["sql"], "model": settings.llm_model if settings is not None else "local_template",
        "retries": result["recovery_attempts"], "usage": result["usage"], "unit": "元",
        "demo_plan": {"intent": "revenue", "excluded_regions": list(excluded_regions)},
    }


def query_demo_aggregate(
    database: Path, intent: str, context: QueryContext, *, excluded_regions: tuple[Region, ...] = (),
) -> dict:
    """Execute a fixed aggregate for recognized demo questions and confirmed slots."""
    if intent == "regional_revenue" and (context.period is None or context.metric is None):
        raise ValueError("查询前需要确认月份和营收口径")
    expressions = {
        "refund_total": ("COALESCE(SUM(refund_cents), 0) / 100.0", "退款总额（元）"),
        "average_amount": ("ROUND(AVG(amount_cents) / 100.0, 2)", "平均订单金额（元）"),
    }
    groups = []
    group_columns = []
    if intent in {"monthly_order_count", "monthly_regional_order_count"}:
        groups.append("substr(ordered_on, 1, 7)")
        group_columns.append(f"{groups[-1]} AS 月份")
    if intent in {"regional_order_count", "monthly_regional_order_count", "regional_revenue"}:
        groups.append("region")
        group_columns.append("region AS 地区")
    if intent == "regional_revenue":
        metric = "amount_cents - refund_cents" if context.metric == "net" else "amount_cents"
        label = "净营收（元）" if context.metric == "net" else "毛营收（元）"
        expression = f"COALESCE(SUM({metric}), 0) / 100.0"
    elif intent in {"order_count", "regional_order_count", "monthly_order_count", "monthly_regional_order_count"}:
        expression, label = "COUNT(*)", "订单笔数"
    else:
        expression, label = expressions[intent]
    columns = ", ".join([*group_columns, f'{expression} AS "{label}"'])
    sql = f"SELECT {columns} FROM orders"
    filters = demo_filters(context, excluded_regions)
    if filters:
        sql += " WHERE " + " AND ".join(filters)
    if groups:
        sql += " GROUP BY " + ", ".join(groups) + " ORDER BY " + ", ".join(groups)
    with span("sql_dry_run"), ReadOnlyDB(database) as db:
        db.dry_run(sql)
    with span("sql_execute"), ReadOnlyDB(database) as db:
        columns, rows = db.query(sql, limit=50)
    monetary = "（元）" in label
    message = f"查询成功，返回 {len(rows)} 行预览。"
    if not groups and rows:
        value = rows[0][0]
        formatted = "暂无数据" if value is None else f"{value:.2f}" if monetary else str(value)
        message = f"{label}：{formatted}"
    return {
        "kind": "query", "message": message, "sql": sql, "columns": columns, "rows": rows,
        "truncated": False, "total_rows": len(rows), "tables": ["orders"], "retries": 0, "context": context.model_dump(),
        "query_scope": {"all_time": context.period is None, "all_regions": context.region is None and not excluded_regions},
        "unit": "元" if monetary else "笔",
        "demo_plan": {"intent": intent, "excluded_regions": list(excluded_regions)},
    }
