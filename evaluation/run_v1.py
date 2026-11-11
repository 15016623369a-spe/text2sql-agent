"""Run the V1 baseline against the frozen BIRD Mini-Dev development split."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import urllib.error
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import local
from time import perf_counter
from typing import TYPE_CHECKING

from app import auto_column_notes
from app.config import PROJECT_ROOT, Settings, load_settings
from app.llm import chat_completion, completion_text
from app.schema_linking import DENSE_TABLE_BUDGET, VERSION as SCHEMA_VERSION, SchemaIndex
from app.schema_prompt import render_scope as _render_scope
from app.text2sql import PROMPT_VERSION, build_messages, full_schema, parse_sql, schema_objects
from app.tools.db import ReadOnlyDB
from evaluation.bird_column_notes import (
    AutoBirdColumnNotes, BirdColumnNotes, MAX_CHARS, MAX_NOTES, VERSION as NOTES_VERSION,
)
from evaluation.splits.generate_splits import JSONL, load_questions

if TYPE_CHECKING:
    from app.dense_schema import DenseSchemaIndex

DB_ROOT = PROJECT_ROOT / "data" / "bird" / "dev_databases"
DEV_IDS = Path(__file__).with_name("splits") / "dev_ids.json"
RESULTS = Path(__file__).with_name("results")
SQL_TIMEOUT_SECONDS = 5.0
MAX_OUTPUT_TOKENS = 1024
TOKEN_FIELDS = (
    "prompt_tokens", "completion_tokens", "total_tokens",
    "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
)


def evaluate_question(
    question: dict,
    settings: Settings,
    schemas: dict[str, str],
    *,
    indexes: dict[str, SchemaIndex] | None = None,
    top_k: int = 4,
    notes_cache: dict[str, BirdColumnNotes | AutoBirdColumnNotes] | None = None,
    notes_factory: type[BirdColumnNotes] | type[AutoBirdColumnNotes] = BirdColumnNotes,
    recovery_budget: int | None = None,
    dense: DenseSchemaIndex | None = None,
) -> dict:
    db_id = question["db_id"]
    db_path = DB_ROOT / db_id / f"{db_id}.sqlite"
    record = {
        "question_id": question["question_id"],
        "db_id": db_id,
        "difficulty": question["difficulty"],
        "model_input": None,
        "sql": None,
        "prediction_rows": None,
        "reference_rows": None,
        "usage": {},
        "model_calls": 0,
        "ex": 0,
    }
    started = perf_counter()
    stage = "schema"
    index = None
    try:
        with ReadOnlyDB(db_path, timeout=SQL_TIMEOUT_SECONDS) as db:
            evidence = question["evidence"] or ""
            if indexes is None:
                if db_id not in schemas:
                    schemas[db_id] = full_schema(db)
                schema = schemas[db_id]
            else:
                if db_id not in indexes:
                    indexes[db_id] = SchemaIndex(db_path, schema_objects(db))
                index = indexes[db_id]
                query = question["question"] + "\n" + evidence
                dense_tables = dense.rank(db_path, index, query) if dense is not None else None
                schema, selection = index.select(query, top_k, dense_tables=dense_tables)
                record["schema_linking"] = selection
                renderer = None
                if notes_cache is not None:
                    if db_id not in notes_cache:
                        notes_cache[db_id] = notes_factory(db_path)
                    renderer = notes_cache[db_id]
                    schema, record["column_notes"] = _render_scope(index, selection, query, renderer)

            messages = build_messages(schema, question["question"], evidence)
            record["model_input"] = messages

            if recovery_budget is None:
                stage = "model_request"
                record["model_calls"] = 1
                _, response = chat_completion(settings, messages, max_tokens=MAX_OUTPUT_TOKENS, json_output=True)
                usage = response.get("usage")
                record["usage"] = usage if isinstance(usage, dict) else {}
                stage = "model_output"
                record["sql"] = parse_sql(completion_text(response))

                stage = "prediction"
                columns, predicted = db.query(record["sql"], limit=None)
            else:
                from app.sql_recovery import run_recovery

                if index is None:
                    raise ValueError("Recovery requires schema linking")

                def request_model(prompt):
                    return chat_completion(settings, prompt, max_tokens=MAX_OUTPUT_TOKENS, json_output=True)[1]

                def validate_sql(sql):
                    with ReadOnlyDB(db_path, timeout=SQL_TIMEOUT_SECONDS) as attempt_db:
                        attempt_db.dry_run(sql)

                def execute_sql(sql):
                    with ReadOnlyDB(db_path, timeout=SQL_TIMEOUT_SECONDS) as attempt_db:
                        return attempt_db.query(sql, limit=None)

                def expand_scope(scope, kind, identifier):
                    return index.expand_for_identifier(scope, query, kind, identifier)

                def build_prompt(scope):
                    current_schema, _ = _render_scope(index, scope, query, renderer)
                    return build_messages(current_schema, question["question"], evidence)

                recovery, prediction = run_recovery(
                    messages, selection, request_model=request_model, validate_sql=validate_sql, execute_sql=execute_sql,
                    expand_scope=expand_scope, build_prompt=build_prompt, max_recoveries=recovery_budget,
                )
                record.update(recovery)
                record["final_schema_linking"] = dict(record["final_schema_linking"])
                if prediction is None:
                    record["latency_seconds"] = round(perf_counter() - started, 6)
                    return record
                columns, predicted = prediction
            record["prediction_columns"] = columns
            record["prediction_rows"] = predicted
            record["latency_seconds"] = round(perf_counter() - started, 6)

            stage = "reference"
            _, reference = db.query(question["SQL"], limit=None)
            record["reference_rows"] = reference
        record["ex"] = int(set(predicted) == set(reference))
        if record.get("first_sql_executed"):
            record["first_ex"] = record["ex"]
        record["status"] = "correct" if record["ex"] else "mismatch"
    except (sqlite3.Error, OSError, ValueError, IndexError, KeyError, TypeError) as exc:
        record["status"] = f"{stage}_error"
        record["error"] = type(exc).__name__
        if isinstance(exc, urllib.error.HTTPError):
            record["http_status"] = exc.code
        elif isinstance(exc, (sqlite3.Error, TimeoutError)):
            record["error_detail"] = str(exc)
    finally:
        record.setdefault("latency_seconds", round(perf_counter() - started, 6))
        if index is not None and "schema_linking" in record:
            try:
                with ReadOnlyDB(db_path, timeout=SQL_TIMEOUT_SECONDS) as db:
                    reads = db.references(question["SQL"])
                linking = record["schema_linking"]
                linking.update(index.coverage(linking, reads))
                if "final_schema_linking" in record:
                    final_linking = record["final_schema_linking"]
                    final_linking.update(index.coverage(final_linking, reads))
            except (sqlite3.Error, OSError) as exc:
                record["schema_linking"]["diagnostic_error"] = type(exc).__name__
    return record


def summarize(records: list[dict]) -> dict:
    count = len(records)
    latencies = sorted(r["latency_seconds"] for r in records)
    correct = sum(r["ex"] for r in records)
    summary = {
        "questions": count,
        "correct": correct,
        "execution_accuracy": correct / count,
        "p95_latency_seconds": latencies[math.ceil(count * 0.95) - 1],
        "statuses": dict(Counter(r["status"] for r in records)),
        "model_calls": sum(r["model_calls"] for r in records),
        "usage_reported": sum(bool(r["usage"]) for r in records),
        "token_totals": {
            key: sum(r["usage"].get(key, 0) or 0 for r in records) for key in TOKEN_FIELDS
        },
    }
    selections = [r["schema_linking"] for r in records if "schema_linking" in r]
    if selections:
        summary["schema_linking"] = summarize_schema_linking(selections)
    if recoveries := [r for r in records if "attempts" in r]:
        first_failures = [
            r for r in recoveries if r["attempts"][0]["status"] in {"dry_run_error", "prediction_error"}
        ]
        recovered = sum(r["ex"] for r in first_failures)
        retries = [attempt for r in recoveries for attempt in r["attempts"][1:]]
        checks = [attempt["dry_run"] for r in recoveries for attempt in r["attempts"] if "dry_run" in attempt]
        summary["recovery"] = {
            "first_correct": sum(r["first_ex"] for r in recoveries),
            "first_execution_accuracy": sum(r["first_ex"] for r in recoveries) / count,
            "first_execution_failures": len(first_failures),
            "recovered_execution_failures": recovered,
            "recovery_rate": recovered / len(first_failures) if first_failures else None,
            "first_dry_run_failures": sum(r["attempts"][0]["status"] == "dry_run_error" for r in first_failures),
            "dry_run_checks": len(checks),
            "dry_run_failures": sum(check["status"] == "failed" for check in checks),
            "total_retries": len(retries),
            "mean_retries_per_question": len(retries) / count,
            "retry_token_totals": {
                key: sum(a["usage"].get(key, 0) or 0 for a in retries) for key in TOKEN_FIELDS
            },
            "retry_actions": dict(Counter(attempt["action"] for attempt in retries)),
            "stop_reasons": dict(Counter(r["recovery_stop_reason"] for r in recoveries)),
            "final_schema_linking": summarize_schema_linking([r["final_schema_linking"] for r in recoveries]),
        }
    return summary


def summarize_schema_linking(selections: list[dict]) -> dict:
    measured = [entry for entry in selections if "reference_tables" in entry]
    recall = {}
    for name in ("table", "column"):
        total = sum(len(entry[f"reference_{name}s"]) for entry in measured)
        top_hits = sum(entry[f"top_{name}_hits"] for entry in measured)
        missing = sum(len(entry[f"missing_{name}s"]) for entry in measured)
        recall[f"{name}_recall_at_k"] = top_hits / total if total else None
        recall[f"{name}_recall_after_bridges"] = (total - missing) / total if total else None
    return {
        **recall,
        "questions_measured": len(measured),
        "questions_missing_reference_tables_at_k": sum(
            entry["top_table_hits"] < len(entry["reference_tables"]) for entry in measured
        ),
        "questions_missing_reference_tables": sum(bool(entry["missing_tables"]) for entry in measured),
        "questions_missing_declared_fk_pairs": sum(
            bool(entry["missing_declared_fk_pairs"]) for entry in measured
        ),
        "mean_prompt_tables": sum(len(entry["tables"]) for entry in selections) / len(selections),
        "mean_full_schema_tables": sum(entry["total_tables"] for entry in selections) / len(selections),
    }


def main(*, schema_top_k: int | None = None, recovery: bool = False) -> int:
    version = "v1" if schema_top_k is None else "v2"
    if recovery:
        from app.sql_recovery import MAX_RECOVERIES, VERSION as RECOVERY_VERSION

        version = "v3"
    parser = argparse.ArgumentParser(description=f"Run the {version.upper()} BIRD benchmark")
    parser.add_argument("--split", choices=("dev", "eval"), default="dev", help="Use the fixed dev or holdout IDs")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent requests")
    parser.add_argument("--limit", type=int, help="Run the first N IDs from the chosen split")
    parser.add_argument("--output-dir", type=Path, help="New directory for JSON and JSONL results")
    if schema_top_k is not None:
        parser.add_argument("--top-k", type=int, default=schema_top_k, help="Number of seed tables")
        parser.add_argument("--dense-schema", action="store_true", help="Supplement V2 with local BGE-M3/Chroma")
        parser.add_argument("--dense-root", type=Path, help="Directory containing the prepared model and Chroma store")
        notes_group = parser.add_mutually_exclusive_group()
        notes_group.add_argument("--column-notes", action="store_true", help="Use the frozen manual notes")
        notes_group.add_argument("--auto-column-notes", action="store_true", help="Generate notes from metadata and SQLite samples")
    if recovery:
        parser.add_argument("--max-recoveries", type=int, choices=range(MAX_RECOVERIES + 1), default=MAX_RECOVERIES)
    args = parser.parse_args()
    dense = None
    if getattr(args, "dense_schema", False):
        from app.dense_schema import DENSE_ROOT, DenseSchemaIndex

        try:
            dense = DenseSchemaIndex(args.dense_root or DENSE_ROOT)
        except (ImportError, OSError, ValueError) as exc:
            parser.error(f"Cannot initialize dense retrieval: {exc}")
        version += "-dense"
    notes_factory = None
    if getattr(args, "column_notes", False):
        version += "-notes"
        notes_factory = BirdColumnNotes
    if getattr(args, "auto_column_notes", False):
        version += "-auto-notes"
        notes_factory = AutoBirdColumnNotes
    if args.workers < 1:
        parser.error("--workers must be positive")
    if schema_top_k is not None and args.top_k < 1:
        parser.error("--top-k must be positive")

    ids = json.loads(DEV_IDS.with_name(f"{args.split}_ids.json").read_text())
    if args.limit is not None:
        if not 1 <= args.limit <= len(ids):
            parser.error(f"--limit must be between 1 and {len(ids)}")
        ids = ids[:args.limit]
    questions = {q["question_id"]: q for q in load_questions(JSONL)}
    settings = load_settings()
    if not settings.llm_configured or not settings.llm_base_url:
        parser.error("Set LLM_API_KEY and LLM_BASE_URL in .env first")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir or RESULTS / f"{version}-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    run = {
        "version": version,
        "split": args.split,
        "workers": args.workers,
        "question_ids": ids,
        "dataset_sha256": hashlib.sha256(JSONL.read_bytes()).hexdigest(),
        "prompt_version": PROMPT_VERSION,
        "model": settings.llm_model,
        "provider": settings.llm_provider,
        "temperature": settings.llm_temperature,
        "sql_timeout_seconds": SQL_TIMEOUT_SECONDS,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "uses_official_evidence": True,
        "execution_metric": "BIRD EX set equality",
    }
    if schema_top_k is not None:
        run["schema_linking"] = {
            "version": SCHEMA_VERSION, "method": "BM25 plus table and lookup evidence",
            "top_k": args.top_k, "bridge": "declared foreign keys",
        }
    if dense is not None:
        run["schema_linking"]["dense"] = {**dense.metadata(), "added_table_budget": DENSE_TABLE_BUDGET}
    if recovery:
        run["recovery"] = {"version": RECOVERY_VERSION, "max_recoveries": args.max_recoveries}
    if notes_factory is BirdColumnNotes:
        run["column_notes"] = {"version": NOTES_VERSION, "max_notes": MAX_NOTES, "max_chars": MAX_CHARS}
    if notes_factory is AutoBirdColumnNotes:
        run["column_notes"] = {
            "version": auto_column_notes.VERSION, "max_notes": auto_column_notes.MAX_NOTES,
            "max_chars": auto_column_notes.MAX_CHARS, "sample_rows": auto_column_notes.SAMPLE_ROWS,
        }
    (output / "run.json").write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")

    cache = local()

    def evaluate(question_id: int) -> dict:
        if not hasattr(cache, "schemas"):
            cache.schemas = {}
            cache.indexes = {} if schema_top_k is not None else None
            cache.notes = {} if notes_factory is not None else None
        return evaluate_question(
            questions[question_id], settings, cache.schemas,
            indexes=cache.indexes, top_k=getattr(args, "top_k", 4),
            notes_cache=cache.notes, notes_factory=notes_factory or BirdColumnNotes,
            recovery_budget=args.max_recoveries if recovery else None, dense=dense,
        )

    records = []
    with (output / "questions.jsonl").open("x", encoding="utf-8") as log, ThreadPoolExecutor(args.workers) as pool:
        for index, record in enumerate(pool.map(evaluate, ids), 1):
            question_id = record["question_id"]
            records.append(record)
            log.write(json.dumps(record, ensure_ascii=False, default=repr) + "\n")
            log.flush()
            print(f"[{index}/{len(ids)}] ID {question_id}: {record['status']}", flush=True)

    summary = summarize(records)
    summary["benchmark_valid"] = not {"model_request_error", "schema_error"}.intersection(summary["statuses"])
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Results: {output.resolve()}")
    if not summary["benchmark_valid"]:
        print("Benchmark incomplete: see request or schema errors in the run log.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
