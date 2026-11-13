"""Apply the product semantic checks to SQL logged with semantics disabled."""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path

from app.config import PROJECT_ROOT
from app.product_sql import _scan_result, database_path
from app.query_export import QueryExportCapture
from app.sql_semantics import check_query
from evaluation.run_product import _fingerprint, _splits
from evaluation.splits.generate_splits import load_questions


def replay_candidate(question: dict, record: dict, db_root: Path) -> dict:
    """Hold the model output fixed and reconstruct the product's SQLite preview."""
    original_ex = record["ex"]
    result = {
        "question_id": record["question_id"], "without_semantics_ex": original_ex,
        "with_semantics_ex": original_ex, "without_semantics_status": record["status"],
        "outcome": "no_completed_candidate", "signals": [],
    }
    sql = record["sql"]
    if not sql or not record["answered"]:
        return result
    check = check_query(question["question"], sql)
    if check.blocked:
        result.update(outcome="blocked_pre_execution", with_semantics_ex=0)
    else:
        try:
            database = database_path(db_root, question["db_id"])
            if database is None:
                raise FileNotFoundError(question["db_id"])
            with QueryExportCapture().active():
                preview = _scan_result(database, sql)
            if list(preview.columns) != record["prediction_columns"] or preview.total != record["prediction_count"]:
                raise ValueError("Replayed preview columns or row count differ from the original run")
        except (OSError, sqlite3.Error, ValueError) as exc:
            result.update(outcome="replay_error", with_semantics_ex=None, error_type=type(exc).__name__)
            return result
        check = check_query(question["question"], sql, rows=preview.rows, total_rows=preview.total)
        if check.blocked:
            result.update(outcome="blocked_post_execution", with_semantics_ex=0)
        else:
            result["outcome"] = "passed"
    result["signals"] = [
        {key: item[key] for key in ("code", "phase", "blocking")} for item in check.as_dict()["signals"]
    ]
    return result


def summarize_replay(records: list[dict]) -> dict:
    outcomes = Counter(record["outcome"] for record in records)
    blocked = [record for record in records if record["outcome"].startswith("blocked_")]
    correct_blocked = sum(record["without_semantics_ex"] for record in blocked)
    valid = not outcomes["replay_error"]
    without_semantics = sum(record["without_semantics_ex"] for record in records)
    with_semantics = without_semantics - correct_blocked if valid else None
    return {
        "questions": len(records), "outcomes": dict(outcomes),
        "without_semantics_correct": without_semantics, "with_semantics_correct": with_semantics,
        "correct_answers_blocked": correct_blocked,
        "wrong_answers_blocked": sum(record["without_semantics_status"] == "mismatch" for record in blocked),
        "valid": valid,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path, help="Completed run with semantics disabled")
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data" / "bird")
    parser.add_argument("--output", type=Path, help="Write a JSON report to this new file")
    args = parser.parse_args()
    try:
        metadata = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
        summary = json.loads((args.run_dir / "summary.json").read_text(encoding="utf-8"))
        if metadata["features"]["semantic_checks"] is not False or summary["benchmark_valid"] is not True:
            raise ValueError("Expected a valid benchmark run with semantics disabled")
        source = _fingerprint()
        checked_files = ("app/sql_semantics.py", "app/product_sql.py", "app/tools/db.py", "app/query_export.py")
        if any(source[name] != metadata["source_sha256"][name] for name in checked_files):
            raise ValueError("Replay code differs from the source of the benchmark run")
        dataset = args.data_root / "mini_dev_sqlite.jsonl"
        ids = _splits(dataset)[metadata["split"]]
        with (args.run_dir / "questions.jsonl").open(encoding="utf-8") as log:
            records = [json.loads(line) for line in log if line.strip()]
        if not records or [record["question_id"] for record in records] != metadata["question_ids"]:
            raise ValueError("Run IDs differ between metadata and recorded questions")
        if metadata["question_ids"] != ids[:len(records)]:
            raise ValueError("Run questions do not match the recorded dataset split")
        questions = {question["question_id"]: question for question in load_questions(dataset)}
        db_root = (args.data_root / "dev_databases").resolve()
        replayed = [replay_candidate(questions[record["question_id"]], record, db_root) for record in records]
        report = {
            "run_id": metadata["run_id"], "profile": metadata["profile"], "split": metadata["split"],
            "full_split": metadata["full_split"], "dataset_sha256": metadata["dataset_sha256"],
            "source_sha256": {name: source[name] for name in checked_files},
            "method": "Replay completed candidate SQL on the same local BIRD data to reconstruct the bounded preview. "
                      "Check columns and row count against the recorded run. Model generation and recovery are held fixed.",
            "limits": "Re-execution does not verify the original raw preview values or unlogged intermediate candidates.",
            "summary": summarize_replay(replayed), "questions": replayed,
        }
        if args.output is None:
            print(json.dumps(report, indent=2))
        else:
            with args.output.open("x", encoding="utf-8") as output:
                json.dump(report, output, indent=2)
                output.write("\n")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    return int(not report["summary"]["valid"])


if __name__ == "__main__":
    raise SystemExit(main())
