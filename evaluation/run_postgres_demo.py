"""Verify ten PostgreSQL business questions through the product API."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
from decimal import Decimal, InvalidOperation
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx

from app.tools.postgres import POSTGRES_SOURCE_ID

_CASES = Path(__file__).with_name("postgres_demo_cases.json")
_QUERY = "/api/v1/query"
_SCHEMA = f"/api/v1/databases/{POSTGRES_SOURCE_ID}/schema"
_ROW_LIMIT_QUESTION = "列出用于分页压力测试的订单编号。"
_ROW_LIMIT_SQL = "SELECT a0.order_id FROM " + " CROSS JOIN ".join(f"orders a{i}" for i in range(5))
_TIMEOUT_SQL = "SELECT COUNT(*) FROM " + " CROSS JOIN ".join(f"orders a{i}" for i in range(11))
_POLICY_CASES = (
    ("read-outside-selection", "查询当前范围内的订单数量。", "SELECT COUNT(*) FROM retail.payments", "postgres_scope"),
    ("reject-write", "查询演示库中的订单。", "DELETE FROM retail.orders RETURNING order_id", "policy_blocked"),
    ("statement-deadline", "计算订单编号的笛卡尔积数量。", _TIMEOUT_SQL, "timeout"),
)


def _same_rows(observed: list, expected: list) -> bool:
    if len(observed) != len(expected):
        return False
    for actual_row, expected_row in zip(observed, expected):
        if len(actual_row) != len(expected_row):
            return False
        for actual, wanted in zip(actual_row, expected_row):
            if isinstance(wanted, (float, int)) and not isinstance(wanted, bool):
                try:
                    if Decimal(str(actual)) != Decimal(str(wanted)):
                        return False
                except InvalidOperation:
                    return False
            elif actual != wanted:
                return False
    return True


def _query(client, question: str, tables: list[str]) -> tuple[dict, dict]:
    with client.stream("POST", _QUERY, json={
        "database": POSTGRES_SOURCE_ID, "question": question, "tables": tables,
    }) as response:
        response.raise_for_status()
        events = [json.loads(line) for line in response.iter_lines() if line]
    done = next(event for event in events if event["event"] == "done")
    result = next((event["data"] for event in events if event["event"] == "result"), None)
    if result is None:
        error = next((event for event in events if event["event"] == "error"), {})
        result = {"kind": "error", "stop_reason": error.get("stop_reason"), "message": error.get("message")}
    return result, done


def _check_schema(client, cases: list[dict]) -> dict:
    expected = {table for case in cases for table in case["tables"]}
    discovery = client.get("/api/v1/databases").raise_for_status().json()
    catalog = client.get(_SCHEMA).raise_for_status().json()["tables"]
    sample = client.get(_SCHEMA, params={"table": "orders", "include_samples": "true"}).raise_for_status().json()["tables"]
    unknown = client.post(_QUERY, json={
        "database": POSTGRES_SOURCE_ID, "question": "查订单", "tables": ["unknown_table"],
    })
    checks = {
        "discovered": discovery["postgres"] and POSTGRES_SOURCE_ID in discovery["databases"],
        "table_inventory": {table["name"] for table in catalog} == expected,
        "columns_and_comments": all(table["columns"] for table in catalog) and any(
            column["comment"] for table in catalog for column in table["columns"]
        ),
        "samples_require_opt_in": not any(
            column["samples"] for table in catalog for column in table["columns"]
        ) and len(sample) == 1 and sample[0]["name"] == "orders" and any(
            column["samples"] for column in sample[0]["columns"]
        ),
        "unknown_table_rejected": unknown.status_code == 422,
    }
    return {"table_count": len(catalog), "checks": checks, "passed": all(checks.values())}


def _check_case(client, case: dict) -> dict:
    result, done = _query(client, case["question"], case["tables"])
    checked = result.get("kind") == "query"
    rows = result.get("rows", [])
    csv_ok = False
    if checked and result.get("export_url"):
        export = client.get(result["export_url"]).raise_for_status()
        columns, *body = csv.reader(io.StringIO(export.content.decode("utf-8-sig")))
        csv_ok = columns == result["columns"] and _same_rows(body, case["expected_rows"])
    trace = client.get(f"/api/v1/traces/{done['trace_id']}").raise_for_status().json()
    spans = {item["name"] for item in trace["spans"]}
    ok = (
        checked and result.get("data_source") == POSTGRES_SOURCE_ID
        and set(result.get("source_tables", [])) == set(case["tables"])
        and result.get("total_rows") == len(case["expected_rows"])
        and _same_rows(rows, case["expected_rows"]) and csv_ok
        and done["model_calls"] > 0 and {"schema_retrieval", "model_call", "sql_dry_run", "sql_execute"} <= spans
    )
    return {
        "id": case["id"], "passed": ok, "kind": result.get("kind"), "sql": result.get("sql"),
        "rows": rows, "expected_rows": case["expected_rows"], "stop_reason": result.get("stop_reason"),
        "trace_id": done["trace_id"], "model_calls": done["model_calls"],
        "latency_ms": done["latency_ms"], "csv_matches": csv_ok,
    }


def _run(client, cases: list[dict], *, replay: bool) -> dict:
    schema = _check_schema(client, cases)
    records = [_check_case(client, case) for case in cases]
    policy = []
    if replay:
        for name, question, _sql, reason in _POLICY_CASES:
            result, _ = _query(client, question, ["orders"])
            policy.append({"id": name, "passed": result.get("kind") == "error" and result.get("stop_reason") == reason})
        limited, _ = _query(client, _ROW_LIMIT_QUESTION, ["orders"])
        policy.append({"id": "bounded-result", "passed": (
            limited.get("kind") == "query" and len(limited["rows"]) == 50
            and limited["total_rows"] is None and limited["truncated"] and "export_url" not in limited
        )})
    return {
        "transport": "fixed SQL responses" if replay else "live model",
        "schema": schema, "cases": records, "policy": policy,
        "passed": sum(case["passed"] for case in records), "total": len(records),
    }


def _replay(cases: list[dict]) -> dict:
    from fastapi.testclient import TestClient

    from app import product_sql
    from app.product_api import app
    from app.telemetry import span

    by_question = {case["question"]: case["sql"] for case in cases}
    by_question.update({question: sql for _, question, sql, _ in _POLICY_CASES})
    by_question[_ROW_LIMIT_QUESTION] = _ROW_LIMIT_SQL

    def respond(_settings, messages, **_kwargs):
        question = messages[-1]["content"].split("\nQuestion:\n", 1)[1].splitlines()[0]
        with span("model_call"):
            return 200, {"choices": [{"message": {"content": json.dumps({"sql": by_question[question]})}}]}

    with TemporaryDirectory() as state, patch.dict(os.environ, {
        "RAG_DATA_DIR": state, "RAG_GATEWAY_KEY": "", "RAG_DEPLOYMENT": "local",
        "RAG_LANGSMITH_EXPORT": "false", "LANGSMITH_TRACING": "false",
        "LLM_API_KEY": "fixture", "LLM_BASE_URL": "https://fixture.invalid",
    }), patch.object(product_sql, "chat_completion", side_effect=respond), TestClient(app) as client:
        return _run(client, cases, replay=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    transport = parser.add_mutually_exclusive_group(required=True)
    transport.add_argument("--base-url", help="Running product API for a live model test")
    transport.add_argument("--replay", action="store_true", help="Use fixed model SQL and a real configured PostgreSQL")
    parser.add_argument("--output", type=Path, help="Write the acceptance evidence")
    args = parser.parse_args()

    cases = json.loads(_CASES.read_text(encoding="utf-8"))
    if args.replay:
        report = _replay(cases)
    else:
        with httpx.Client(base_url=args.base_url, timeout=150, trust_env=False) as client:
            report = _run(client, cases, replay=False)
    if args.output:
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, indent=2)
            target.write("\n")
    print(f"{report['transport']}: {report['passed']}/{report['total']} questions; schema {report['schema']['passed']}")
    for case in [*report["cases"], *report["policy"]]:
        print(f"{'PASS' if case['passed'] else 'FAIL'} {case['id']}")
    return int(not (report["passed"] == report["total"] == 10
                    and report["schema"]["passed"] and all(case["passed"] for case in report["policy"])))


if __name__ == "__main__":
    raise SystemExit(main())
