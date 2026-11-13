"""Measure semantic checks on paired SQLite fixtures with an identical model response."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal
from unittest.mock import patch

from app import product_sql
from app.config import Settings


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    sql: str
    expected_rows: tuple[tuple, ...]
    gate: Literal["before_execution", "after_execution", "answer", "observe"]
    signals: tuple[str, ...] = ()


_GROUP_QUESTION = "Show the order count for each region"
_GROUP_COUNTS = (("North", 3), ("South", 2))
_DECEMBER_QUESTION = "How many orders were placed in December?"

CASES = (
    Case(
        "group-count-missing-group", _GROUP_QUESTION,
        "SELECT COUNT(*) FROM orders", _GROUP_COUNTS, "before_execution", ("missing_group_by",),
    ),
    Case(
        "group-count-commented-group", "按地区分组，统计订单数",
        'SELECT COUNT(*) AS "GROUP BY" FROM orders /* GROUP BY region */',
        _GROUP_COUNTS, "before_execution", ("missing_group_by",),
    ),
    Case(
        "count-missing-row", _DECEMBER_QUESTION,
        "SELECT COUNT(*) FROM orders WHERE order_date LIKE '12/%' GROUP BY region",
        ((0,),), "after_execution", ("missing_count_result",),
    ),
    Case(
        "count-null", _DECEMBER_QUESTION,
        "SELECT SUM(amount) FROM orders WHERE order_date LIKE '12/%'",
        ((0,),), "after_execution", ("invalid_count_result",),
    ),
    Case(
        "count-negative", "十二月有多少笔订单？",
        "SELECT COUNT(*) - 1 FROM orders WHERE order_date LIKE '12/%'",
        ((0,),), "after_execution", ("invalid_count_result",),
    ),
    Case(
        "count-zero", _DECEMBER_QUESTION,
        "SELECT COUNT(*) FROM orders WHERE order_date LIKE '12/%'", ((0,),), "answer",
    ),
    Case(
        "empty-detail-list", "List order IDs for December",
        "SELECT id FROM orders WHERE order_date LIKE '12/%'", (), "answer",
    ),
    Case(
        "empty-grouped-list", "Show the order count for each region that had orders in December",
        "SELECT region, COUNT(*) FROM orders WHERE order_date LIKE '12/%' GROUP BY region", (), "answer",
    ),
    Case(
        "group-count-valid", _GROUP_QUESTION,
        "SELECT region, COUNT(*) FROM orders GROUP BY region ORDER BY region", _GROUP_COUNTS, "answer",
    ),
    Case(
        "group-count-window", _GROUP_QUESTION,
        "SELECT DISTINCT region, COUNT(*) OVER (PARTITION BY region) FROM orders ORDER BY region",
        _GROUP_COUNTS, "answer",
    ),
    Case(
        "overall-count-with-per-region-condition", "How many orders are from regions that have at least three orders each?",
        "SELECT COUNT(*) FROM orders WHERE region IN (SELECT region FROM orders GROUP BY region HAVING COUNT(*) >= 3)",
        ((3,),), "answer",
    ),
    Case(
        "rank-id-date-advisories", "Show full records for the top 2 orders since October 1, 2026 with customer ID starting with A",
        "SELECT * FROM orders WHERE customer_id LIKE 'A%' AND order_date >= '10/01/2026' ORDER BY amount DESC LIMIT 2",
        ((2, "North", "A02", "10/05/2026", 21), (1, "North", "A01", "10/02/2026", 12)), "answer",
        ("ranking_full_rows", "id_text_match", "date_encoding_check"),
    ),
    Case(
        "count-valid-shape-wrong-value", "How many orders were placed in October?",
        "SELECT COUNT(*) FROM orders", ((3,),), "observe",
    ),
    Case(
        "group-count-single-scoped-region", "Show the order count for each region. Include only North.",
        "SELECT 'North' AS region, COUNT(*) FROM orders WHERE region = 'North'", (("North", 3),), "observe",
    ),
)


def _seed_database(database: Path) -> None:
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, customer_id TEXT, order_date TEXT, amount INTEGER)")
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?, ?, ?)", (
            (1, "North", "A01", "10/02/2026", 12),
            (2, "North", "A02", "10/05/2026", 21),
            (3, "South", "B01", "09/01/2026", 8),
            (4, "South", "B02", "10/01/2026", 7),
            (5, "North", "A03", "09/20/2026", 5),
        ))


def _run_case(database: Path, case: Case, enabled: bool) -> tuple[dict, list]:
    response = {"choices": [{"message": {"content": json.dumps({"sql": case.sql})}}]}
    settings = Settings("fixture", "https://example.invalid", "fixture", "test-only", 0)
    with patch.object(product_sql, "chat_completion", return_value=(200, response)) as model, patch.object(
        product_sql, "_scan_result", wraps=product_sql._scan_result,
    ) as scan:
        reply = product_sql.query_database(
            database, case.question, "", settings, features=product_sql.ProductFeatures(semantic_checks=enabled),
        )
    rows = reply.get("rows")
    expected = [list(row) for row in case.expected_rows]
    return {
        "kind": reply["kind"], "sql": reply.get("sql"), "rows": rows,
        "correct": rows == expected if reply["kind"] == "query" else None,
        "executed": scan.called,
        "reported_executed": reply.get("executed"),
        "stop_reason": reply.get("stop_reason"),
        "signals": reply.get("semantics", {}).get("signals", []),
    }, model.call_args_list


def _validate_pair(case: Case, off: dict, on: dict, off_calls: list, on_calls: list) -> list[str]:
    issues = []
    if len(off_calls) != 1 or off_calls != on_calls or off["sql"] is None or off["sql"] != on["sql"]:
        issues.append("expected one identical model request and identical SQL in both runs")
    if off["kind"] != "query" or not off["executed"] or off["signals"]:
        issues.append("disabled control did not return an unchecked SQL result")
    if on["kind"] == "abstained":
        if on["stop_reason"] != "semantic_check_failed":
            issues.append("abstained for a reason other than the semantic check")
        if on["reported_executed"] is not on["executed"]:
            issues.append("abstention execution metadata differs from the actual scan")
    if on["kind"] == "query" and on["rows"] != off["rows"]:
        issues.append("semantic checks altered the returned SQLite rows")
    if on["kind"] not in {"query", "abstained"}:
        issues.append(f"unexpected enabled outcome {on['kind']}")
    if case.gate == "answer" and on["correct"] is not True:
        issues.append("did not return the expected correct answer")
    if case.gate in {"before_execution", "after_execution"}:
        phase = "pre_execution" if case.gate == "before_execution" else "post_execution"
        if off["correct"] is not False or on["kind"] != "abstained" or on["executed"] != (phase == "post_execution"):
            issues.append(f"did not block an incorrect answer at {phase}")
        blockers = {signal["code"] for signal in on["signals"] if signal["blocking"] and signal["phase"] == phase}
        if not set(case.signals).issubset(blockers):
            issues.append(f"missing expected blocking signals at {phase}")
    actual_signals = {signal["code"] for signal in on["signals"]}
    if not set(case.signals).issubset(actual_signals):
        issues.append(f"missing signals {sorted(set(case.signals) - actual_signals)}")
    return [f"{case.name}: {issue}" for issue in issues]


def run_cases() -> dict:
    records, issues = [], []
    with TemporaryDirectory() as directory, patch.dict(os.environ, {"RAG_DENSE_ROOT": ""}):
        database = Path(directory) / "semantic-fixture.sqlite"
        _seed_database(database)
        for case in CASES:
            off, off_calls = _run_case(database, case, False)
            on, on_calls = _run_case(database, case, True)
            issues.extend(_validate_pair(case, off, on, off_calls, on_calls))
            records.append({
                "id": case.name, "question": case.question, "fixture_sql": case.sql, "gate": case.gate,
                "expected_rows": [list(row) for row in case.expected_rows], "checks_off": off, "checks_on": on,
            })
    blocked = [record for record in records if record["checks_on"]["kind"] == "abstained"]
    return {
        "dataset": "synthetic SQLite fixtures, deterministic model responses",
        "summary": {
            "total_cases": len(records),
            "correct_answers_checks_off": sum(record["checks_off"]["correct"] is True for record in records),
            "correct_answers_checks_on": sum(record["checks_on"]["correct"] is True for record in records),
            "prevented_wrong_answers": sum(record["checks_off"]["correct"] is False for record in blocked),
            "blocked_correct_answers": sum(record["checks_off"]["correct"] is True for record in blocked),
            "remaining_wrong_answers": sum(record["checks_on"]["correct"] is False for record in records),
            "abstained_before_execution": sum(not record["checks_on"]["executed"] for record in blocked),
            "abstained_after_execution": sum(record["checks_on"]["executed"] for record in blocked),
            "nonblocking_advisory_cases": sum(
                record["checks_on"]["kind"] == "query" and bool(record["checks_on"]["signals"]) for record in records
            ),
        },
        "issues": issues,
        "cases": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the per-case JSON report to this path")
    args = parser.parse_args()
    report = run_cases()
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("dataset", "summary", "issues")}, ensure_ascii=False, indent=2))
    return int(bool(report["issues"]))


if __name__ == "__main__":
    raise SystemExit(main())
