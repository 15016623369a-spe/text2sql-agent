"""Measure BIRD EX on the read-only product SQL path used by POST /api/v1/query."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app import auto_column_notes, product_sql, schema_linking, sql_recovery, text2sql
from app.config import PROJECT_ROOT, Settings, _load_dotenv, load_settings
from app.query_export import QueryExportCapture
from app.telemetry import TOKEN_FIELDS, Trace
from app.tools.db import ReadOnlyDB, result_row_bytes
from evaluation.splits.generate_splits import DEV_N, EVAL_N, load_questions

_SPLITS = Path(__file__).with_name("splits")
_DATASET_HASH = "926af2a0d1b47e5d0d15a7aa8a34d4721007f4550a881555b6a89496b54a2606"
_OBSERVATION_BYTES = 32 * 1024 * 1024
_INFRASTRUCTURE_ERRORS = {"model_request_error", "schema_error", "reference_error", "observation_error"}
_FULL = product_sql.ProductFeatures()
PROFILES = {
    "full": _FULL,
    "baseline": product_sql.ProductFeatures(value_hints=False, prompt_guidance=False, semantic_checks=False),
    "no-value-hints": replace(_FULL, value_hints=False),
    "no-prompt": replace(_FULL, prompt_guidance=False),
    "no-semantics": replace(_FULL, semantic_checks=False),
}
_SEMANTIC_SPANS = {"pre_execution": "sql_semantic_check", "post_execution": "sql_result_check"}


class _Rows:
    """Collect typed values for BIRD set equality without storing rows in run artifacts."""

    def __init__(self):
        self.begin()

    def begin(self):
        self.rows: set[tuple] = set()
        self.bytes = 0
        self.count = 0
        self.exceeded = False
        return self.add

    def add(self, row: tuple) -> None:
        self.count += 1
        if self.exceeded:
            return
        self.bytes += result_row_bytes(row)
        if self.bytes > _OBSERVATION_BYTES:
            self.exceeded = True
            self.rows.clear()
            return
        self.rows.add(row)


def evaluate_question(question: dict, settings: Settings, db_root: Path, *, features: product_sql.ProductFeatures = _FULL) -> dict:
    record = {
        "question_id": question["question_id"], "db_id": question["db_id"], "difficulty": question["difficulty"],
        "reference_sql": question["SQL"], "sql": None, "ex": 0, "answered": False, "executed": False,
        "prediction_count": None, "reference_count": None,
    }
    observed = _Rows()
    trace = Trace("text2sql", lambda _: None)
    try:
        database = product_sql.database_path(db_root, question["db_id"])
        if database is None:
            raise FileNotFoundError(question["db_id"])
        with trace.active(), QueryExportCapture().active():
            reply = product_sql.query_database(database, question["question"], question["evidence"] or "", settings,
                                               observe_rows=observed.begin, features=features)
    except (OSError, ValueError, sqlite3.Error, IndexError, KeyError, TypeError, RuntimeError, ImportError) as exc:
        record.update(status="schema_error", stop_reason="schema_error", error_type=type(exc).__name__)
        reply = {}

    report = trace.finish("error" if reply.get("kind", "error") == "error" else "ok", None)
    record.update(latency_seconds=round(report["latency_ms"] / 1000, 6), usage=report["usage"], model_calls=report["model_calls"],
                  model_calls_with_usage=report["model_calls_with_usage"], estimated_cost_usd=report["estimated_cost_usd"])
    span_counts = Counter(item["name"] for item in report["spans"])
    record["semantic_checks"] = {phase: span_counts[name] for phase, name in _SEMANTIC_SPANS.items()}
    if not reply:
        return record
    kind = reply["kind"]
    record.update(kind=kind, stop_reason=reply.get("stop_reason"), sql=reply.get("sql"), retries=reply.get("retries", 0))
    if semantics := reply.get("semantics"):
        record["semantic_signals"] = [
            {key: item[key] for key in ("code", "phase", "blocking")} for item in semantics["signals"]
        ]
    record["executed"] = kind == "query" or any(span["name"] == "sql_execute" and span["error_type"] is None
                                                for span in report["spans"])
    if kind != "query":
        record["status"] = record["stop_reason"] if kind == "error" and record["stop_reason"] else kind
        return record

    record.update(answered=True, prediction_columns=reply["columns"], prediction_count=reply["total_rows"],
                  prediction_unique_count=len(observed.rows) if not observed.exceeded else None)
    if reply["total_rows"] is None:
        record.update(status="result_limit", stop_reason="result_limit")
        return record
    if observed.exceeded or observed.count != reply["total_rows"]:
        record.update(status="observation_error", stop_reason="observation_error")
        return record

    reference = _Rows()
    try:
        with ReadOnlyDB(database) as db, db.scan(question["SQL"]) as (columns, cursor):
            for row in cursor:
                reference.add(row)
                if reference.exceeded:
                    raise ValueError("Reference observation exceeds the memory limit")
    except (sqlite3.Error, OSError, ValueError) as exc:
        record.update(status="reference_error", stop_reason="reference_error", error_type=type(exc).__name__)
        return record

    ex = int(observed.rows == reference.rows)
    record.update(ex=ex, status="correct" if ex else "mismatch", stop_reason="executed", reference_columns=columns,
                  reference_count=reference.count, reference_unique_count=len(reference.rows))
    return record


def _metrics(records: list[dict]) -> dict:
    count = len(records)
    correct = sum(record["ex"] for record in records)
    answered = sum(record["answered"] for record in records)
    executed = sum(record["executed"] for record in records)
    wrong = sum(record["status"] == "mismatch" for record in records)
    latencies = sorted(record["latency_seconds"] for record in records)
    statuses = Counter(record["status"] for record in records)
    errors = Counter((record.get("stop_reason") or record["status"]) if record["status"] != "mismatch" else "mismatch"
                     for record in records if not record["ex"])
    return {
        "questions": count, "correct": correct, "answered": answered, "executed": executed, "wrong_answers": wrong,
        "execution_accuracy": correct / count, "execution_success_rate": executed / count,
        "wrong_answer_rate": wrong / count, "answer_rate": answered / count,
        "accuracy_on_answered": correct / answered if answered else None,
        "p50_latency_seconds": latencies[math.ceil(count * .50) - 1],
        "p95_latency_seconds": latencies[math.ceil(count * .95) - 1],
        "statuses": dict(statuses), "error_types": dict(errors),
        "model_calls": sum(record["model_calls"] for record in records),
        "model_calls_with_usage": sum(record["model_calls_with_usage"] for record in records),
        "semantic_checks": {
            phase: sum(record["semantic_checks"][phase] for record in records) for phase in _SEMANTIC_SPANS
        },
        "token_totals": {name: sum(record["usage"][name] for record in records) for name in TOKEN_FIELDS},
        "benchmark_valid": not (_INFRASTRUCTURE_ERRORS & statuses.keys()),
    }


def summarize(records: list[dict]) -> dict:
    summary = _metrics(records)
    for field, output in (("db_id", "by_database"), ("difficulty", "by_difficulty")):
        groups = defaultdict(list)
        for record in records:
            groups[record[field]].append(record)
        summary[output] = {key: _metrics(group) for key, group in sorted(groups.items())}
    return summary


def _splits(dataset: Path) -> dict[str, list[int]]:
    manifest = json.loads((_SPLITS / "manifest.json").read_text())
    splits = {name: json.loads((_SPLITS / f"{name}_ids.json").read_text()) for name in ("dev", "eval")}
    for name, expected in (("dev", DEV_N), ("eval", EVAL_N)):
        ids = splits[name]
        if (manifest[f"{name}_n"] != expected or len(ids) != expected or any(type(item) is not int for item in ids)
                or ids != sorted(set(ids))):
            raise ValueError(f"Invalid fixed {name} split")
    if set(splits["dev"]) & set(splits["eval"]):
        raise ValueError("Development and holdout IDs overlap")
    if hashlib.sha256(dataset.read_bytes()).hexdigest() != _DATASET_HASH:
        raise ValueError("Dataset differs from the checked-in BIRD Mini-Dev SHA-256")
    return splits


def _fingerprint() -> dict[str, str]:
    paths = (
        "app/product_sql.py", "app/text2sql.py", "app/schema_linking.py", "app/schema_prompt.py",
        "app/auto_column_notes.py", "evaluation/bird_column_notes.py", "app/sql_recovery.py",
        "app/recovery_models.py", "app/tools/db.py", "app/llm.py", "app/query_export.py",
        "app/sql_semantics.py", "evaluation/run_product.py",
    )
    return {name: hashlib.sha256((PROJECT_ROOT / name).read_bytes()).hexdigest()
            for name in paths if (PROJECT_ROOT / name).is_file()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "eval"), default="dev")
    parser.add_argument("--data-root", type=Path, default=PROJECT_ROOT / "data" / "bird")
    parser.add_argument("--env-file", type=Path, help="Optional existing credentials file")
    parser.add_argument("--output-dir", type=Path, help="New output directory")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--limit", type=int, help="Run a prefix for a smoke check; not a full-split metric")
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--profile", choices=PROFILES, default="full", help="Product feature configuration")
    parser.add_argument("--dense-schema", action="store_true")
    parser.add_argument("--dense-root", type=Path)
    args = parser.parse_args()
    if args.workers < 1 or not math.isfinite(args.temperature) or not 0 <= args.temperature <= 2:
        parser.error("Expected positive --workers and 0 <= --temperature <= 2")
    if args.dense_root is not None and not args.dense_schema:
        parser.error("--dense-root requires --dense-schema")
    if args.env_file is not None:
        if not args.env_file.is_file():
            parser.error("--env-file must be an existing file")
        _load_dotenv(args.env_file)
    settings = replace(load_settings(), llm_model=args.model, llm_temperature=args.temperature)
    if not settings.llm_configured or not settings.llm_base_url:
        parser.error("Set LLM_API_KEY and LLM_BASE_URL or use --env-file")

    dataset = args.data_root / "mini_dev_sqlite.jsonl"
    db_root = (args.data_root / "dev_databases").resolve()
    try:
        splits = _splits(dataset)
        loaded = load_questions(dataset)
        questions = {question["question_id"]: question for question in loaded}
        if len(questions) != len(loaded) or set(splits["dev"] + splits["eval"]) - questions.keys():
            raise ValueError("Dataset has duplicate or missing question IDs")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    full_ids = splits[args.split]
    if args.limit is not None and not 1 <= args.limit <= len(full_ids):
        parser.error(f"--limit must be between 1 and {len(full_ids)}")
    ids = full_ids[:args.limit] if args.limit is not None else full_ids
    dense = None
    if args.dense_schema:
        os.environ["RAG_DENSE_ROOT"] = str((args.dense_root or (args.data_root / ".retrieval")).resolve())
        try:
            dense = product_sql._dense_index(os.environ["RAG_DENSE_ROOT"]).metadata()
        except (OSError, ImportError, ValueError, RuntimeError) as exc:
            parser.error(f"Cannot initialize pinned dense retrieval: {exc}")
    else:
        os.environ.pop("RAG_DENSE_ROOT", None)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = args.output_dir or Path(__file__).with_name("results") / f"product-{args.split}-{stamp}"
    output.mkdir(parents=True, exist_ok=False)
    endpoint = urlsplit(settings.llm_base_url)
    features = PROFILES[args.profile]
    system_prompt = text2sql.SYSTEM_PROMPT + (product_sql.PRODUCT_GUIDANCE if features.prompt_guidance else "")
    metadata = {
        "version": "product", "run_id": stamp, "path": "POST /api/v1/query: app.product_sql.query_database",
        "split": args.split, "question_ids": ids, "full_split": ids == full_ids, "dataset_sha256": _DATASET_HASH,
        "split_sha256": hashlib.sha256((_SPLITS / f"{args.split}_ids.json").read_bytes()).hexdigest(),
        "model": settings.llm_model, "provider": settings.llm_provider, "provider_host": endpoint.hostname,
        "temperature": settings.llm_temperature, "workers": args.workers, "sqlite_version": sqlite3.sqlite_version,
        "profile": args.profile, "features": asdict(features),
        "prompt_version": product_sql.PRODUCT_PROMPT_VERSION if features.prompt_guidance else text2sql.PROMPT_VERSION,
        "system_prompt_sha256": hashlib.sha256(system_prompt.encode()).hexdigest(),
        "source_sha256": _fingerprint(), "uses_official_evidence": True,
        "execution_metric": "BIRD EX set equality of typed SQLite rows",
        "sql_timeout_seconds": 5.0, "model_timeout_seconds": 30, "max_output_tokens": 1024,
        "max_result_rows": product_sql._MAX_COUNTED_ROWS, "observation_max_bytes": _OBSERVATION_BYTES,
        "schema_linking": {"version": schema_linking.VERSION, "top_k": 4, "dense": dense},
        "column_notes": {"version": auto_column_notes.VERSION},
        "recovery": {"version": sql_recovery.VERSION, "max_recoveries": product_sql.MAX_RECOVERIES},
    }
    (output / "run.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    records = []
    with (output / "questions.jsonl").open("x", encoding="utf-8") as log, ThreadPoolExecutor(args.workers) as pool:
        results = pool.map(lambda key: evaluate_question(questions[key], settings, db_root, features=features), ids)
        for index, record in enumerate(results, 1):
            records.append(record)
            log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            log.flush()
            print(f"[{index}/{len(ids)}] ID {record['question_id']}: {record['status']}", flush=True)
    summary = summarize(records)
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in summary.items() if not key.startswith("by_")}, indent=2))
    print(f"Results: {output.resolve()}")
    if not summary["benchmark_valid"]:
        print("Benchmark incomplete: see infrastructure errors in the run log.", file=sys.stderr)
    return int(not summary["benchmark_valid"])


if __name__ == "__main__":
    raise SystemExit(main())
