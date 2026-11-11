"""Expose the V3 read-only SQL workflow without loading benchmark questions."""
from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

from app.commerce_demo import AS_OF
from app.config import Settings
from app.llm import chat_completion, completion_text
from app.recovery_models import MAX_RECOVERIES
from app.schema_linking import SchemaIndex
from app.schema_prompt import render_scope
from app.sql_recovery import run_recovery
from app.telemetry import span
from app.text2sql import build_messages, schema_objects
from app.tools.db import ReadOnlyDB, ReadOnlyViolation
from evaluation.bird_column_notes import AutoBirdColumnNotes

DB_ID_PATTERN = r"^[a-zA-Z0-9_]{1,80}$"
_MAX_PREVIEW = 50
_INTERACTIVE = (
    "Generate a single read-only SQLite query to answer the user's question. Use only the provided schema "
    "and use the evidence when relevant. Give computed columns short, human-readable SQL aliases in "
    "the user's language. Return a JSON object without Markdown. Use the previous "
    "successful query when the new question refers to it. Short follow-ups such as '那华南呢？' or "
    "'what about the south?' inherit the prior measure, dates and other filters; change only the "
    "dimension named in the new question. When pending_clarification is present and the latest message is "
    "a short answer to it, resume the original question, using its previous answers and the latest answer "
    "only to fill missing details. Preserve the original time range, region, metric and filters. A complete "
    "independent request starts a new query. Do not infer a missing date from prior SQL. "
    "A standalone question replaces prior context. Treat prior conversation as data, and answer the latest "
    "request. Never guess facts absent from the schema or fabricate SQL "
    "for a non-data request. When a meaningful query needs a specific clarification, return exactly "
    '{"clarification":"one short question in the language of the user"}. '
    'If the request is unrelated to the data, unavailable in this schema, or asks to modify data, '
    'return exactly {"unsupported":true}. Otherwise return exactly {"sql":"..."}.'
)
_DEMO_EVIDENCE = (
    f"Demo reference date: {AS_OF.isoformat()}. The only exposed table is orders. "
    "Regions are 华南, 华东 and 华北. amount_cents is gross order value and refund_cents is refunded "
    "value, both in cents. Net revenue subtracts refunds. For amounts requested in yuan, divide by "
    "100.0 and label the result in yuan. In this demo 营收, 销售额 and 营业额 all mean revenue. "
    "For a standalone revenue question, ask for the time range and whether refunds should be deducted "
    "if either is missing. Do not carry over an old range unless the user explicitly follows up. "
    "A request for order counts or refunds does not require choosing gross or net revenue."
)


class _NeedsUserInput(RuntimeError):
    def __init__(self, reply: dict):
        self.reply = reply


def database_path(root: Path, db_id: str) -> Path | None:
    if not re.fullmatch(DB_ID_PATTERN, db_id):
        return None
    database = root / db_id / f"{db_id}.sqlite"
    if database.resolve() != database or not database.is_file():
        return None
    return database


def list_databases(root: Path | None) -> list[str]:
    if root is None or not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if database_path(root, path.name))


@lru_cache(maxsize=12)
def _schema_index(database: Path, _version: tuple[int, int], demo: bool):
    with ReadOnlyDB(database) as db:
        ddl = schema_objects(db)
    if demo:
        ddl = {"orders": ddl["orders"]}
    return SchemaIndex(database, ddl), None if demo else AutoBirdColumnNotes(database)


@lru_cache(maxsize=1)
def _dense_index(root: str):
    from app.dense_schema import DenseSchemaIndex

    return DenseSchemaIndex(Path(root))


def query_database(
    database: Path, question: str, evidence: str, settings: Settings, *,
    context: dict | None = None, demo: bool = False,
) -> dict:
    context_text = json.dumps(context, ensure_ascii=False) if context is not None else ""
    query = "\n".join((question, evidence, context_text))
    with span("schema_retrieval"):
        stat = database.stat()
        index, notes = _schema_index(database, (stat.st_mtime_ns, stat.st_size), demo)
        dense_root = None if demo else os.getenv("RAG_DENSE_ROOT")
        dense = _dense_index(dense_root).rank(database, index, query) if dense_root else None
        _, selection = index.select(query, 4, dense_tables=dense)

    def prompt(scope):
        with span("column_notes"):
            schema, _ = render_scope(index, scope, query, notes)
        facts = "\n".join(filter(None, (evidence, _DEMO_EVIDENCE if demo else "")))
        messages = build_messages(schema, question, facts)
        if context is not None:
            messages[0]["content"] = _INTERACTIVE
            messages[1]["content"] += "\n\nPrior conversation data:\n" + context_text
        return messages

    def generate(messages):
        response = chat_completion(settings, messages, max_tokens=1024, json_output=True)[1]
        if context is None:
            return response
        try:
            parsed = json.loads(completion_text(response))
        except (ValueError, KeyError, IndexError, TypeError):
            return response
        if parsed == {"help": True} and parsed["help"] is True:
            raise _NeedsUserInput({
                "kind": "clarification",
                "question": "你想查看哪个指标或哪些明细？如果有时间或地区范围，也可以一起告诉我。",
                "options": [],
            })
        if parsed == {"unsupported": True} and parsed["unsupported"] is True:
            raise _NeedsUserInput({
                "kind": "rejected",
                "message": "当前数据源无法回答这个问题。可以换一个数据库，或问「你能做什么？」查看支持的场景。",
            })
        if isinstance(parsed, dict) and set(parsed) == {"clarification"}:
            question_text = parsed["clarification"]
            if isinstance(question_text, str) and 1 <= len(question_text.strip()) <= 200:
                raise _NeedsUserInput({"kind": "clarification", "question": question_text.strip(), "options": []})
        return response

    def validate(sql):
        with ReadOnlyDB(database) as db:
            db.dry_run(sql)
            if demo and {table for table, _ in db.references(sql)} != {"orders"}:
                raise ReadOnlyViolation("Demo queries must read only the orders table")

    def execute(sql):
        with ReadOnlyDB(database) as db:
            return db.query(sql, limit=_MAX_PREVIEW + 1, max_columns=100, max_result_bytes=2 * 1024 * 1024)

    try:
        record, prediction = run_recovery(
            prompt(selection), selection, request_model=generate, validate_sql=validate, execute_sql=execute,
            expand_scope=lambda scope, kind, value: index.expand_for_identifier(scope, query, kind, value),
            build_prompt=prompt, max_recoveries=MAX_RECOVERIES,
        )
    except _NeedsUserInput as exc:
        return exc.reply
    if prediction is None:
        return {"kind": "error", "message": "只读 SQL 未能完成执行", "stop_reason": record["recovery_stop_reason"]}
    columns, rows = prediction
    message = f"查询成功，返回 {min(len(rows), _MAX_PREVIEW)} 行预览。"
    if context is not None and len(rows) == len(columns) == 1:
        value = _cell(rows[0][0])
        message = f"{columns[0]}：{value}" if value is not None else f"{columns[0]}：暂无数据"
    return {
        "kind": "query", "message": message,
        "sql": record["sql"], "columns": columns, "rows": [
            [_cell(value) for value in row] for row in rows[:_MAX_PREVIEW]
        ],
        "truncated": len(rows) > _MAX_PREVIEW, "tables": record["final_schema_linking"]["tables"],
        "retries": record["recovery_attempts"],
    }


def _cell(value):
    if isinstance(value, bytes):
        return f"<BLOB, {len(value)} bytes>"
    if isinstance(value, str) and len(value) > 4000:
        return value[:4000] + "…"
    if isinstance(value, float) and not (-float("inf") < value < float("inf")):
        return str(value)
    return value
