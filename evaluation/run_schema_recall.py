"""Measure schema retrieval before and after evidence expansion, without model calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path

from app.schema_linking import DENSE_TABLE_BUDGET, VERSION, SchemaIndex
from app.text2sql import schema_objects
from app.tools.db import ReadOnlyDB
from evaluation.run_v1 import DB_ROOT, DEV_IDS, summarize_schema_linking
from evaluation.splits.generate_splits import JSONL, load_questions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=("dev", "eval", "unused"), default="dev")
    parser.add_argument("--top-k", nargs="+", type=int, default=[2, 3, 4, 5, 6])
    parser.add_argument("--dense-schema", action="store_true", help="Also measure local BGE-M3/Chroma")
    parser.add_argument("--dense-root", type=Path, help="Directory containing the prepared model and Chroma store")
    parser.add_argument("--output", type=Path, help="Write metrics and per-question coverage to a new file")
    args = parser.parse_args()
    if min(args.top_k) < 1:
        parser.error("--top-k must be positive")

    questions = {q["question_id"]: q for q in load_questions(JSONL)}
    dev_ids = set(json.loads(DEV_IDS.read_text()))
    eval_ids = set(json.loads(DEV_IDS.with_name("eval_ids.json").read_text()))
    splits = {"dev": dev_ids, "eval": eval_ids, "unused": questions.keys() - dev_ids - eval_ids}
    ids = sorted(splits[args.split])
    result = {
        "split": args.split, "question_ids": ids,
        "dataset_sha256": hashlib.sha256(JSONL.read_bytes()).hexdigest(),
        "retrieval_version": VERSION, "results": {},
    }
    dense = None
    if args.dense_schema:
        from app.dense_schema import DENSE_ROOT, DenseSchemaIndex

        try:
            dense = DenseSchemaIndex(args.dense_root or DENSE_ROOT)
        except (ImportError, OSError, ValueError) as exc:
            parser.error(f"Cannot initialize dense retrieval: {exc}")
        result["dense"] = {**dense.metadata(), "added_table_budget": DENSE_TABLE_BUDGET}
    with ExitStack() as stack:
        databases = {}
        cases = []
        for question_id in ids:
            q = questions[question_id]
            name = q["db_id"]
            if name not in databases:
                path = DB_ROOT / name / f"{name}.sqlite"
                db = stack.enter_context(ReadOnlyDB(path))
                index = SchemaIndex(path, schema_objects(db))
                full_bytes = len("\n\n".join(index.ddl.values()).encode())
                databases[name] = (db, index, full_bytes, path)
            db, index, full_bytes, path = databases[name]
            query = q["question"] + "\n" + (q["evidence"] or "")
            dense_tables = dense.rank(path, index, query) if dense is not None else None
            cases.append((question_id, name, index, query, db.references(q["SQL"]), full_bytes, dense_tables))
        for top_k in dict.fromkeys(args.top_k):
            variants = {}
            modes = ["legacy", "expanded"] + (["dense"] if dense is not None else [])
            for mode in modes:
                rows = []
                for question_id, name, index, query, reads, full_bytes, dense_tables in cases:
                    schema, selection = index.select(
                        query, top_k, expand=mode != "legacy",
                        dense_tables=dense_tables if mode == "dense" else None,
                    )
                    rows.append({
                        "question_id": question_id, "db_id": name,
                        **selection, **index.coverage(selection, reads),
                        "ddl_bytes": len(schema.encode()), "full_ddl_bytes": full_bytes,
                    })
                summary = summarize_schema_linking(rows)
                summary["ddl_bytes"] = sum(row["ddl_bytes"] for row in rows)
                summary["full_ddl_bytes"] = sum(row["full_ddl_bytes"] for row in rows)
                variants[mode] = {"summary": summary, "questions": rows}
            result["results"][top_k] = variants
            print(json.dumps({top_k: {mode: entry["summary"] for mode, entry in variants.items()}}, indent=2))
    if args.output:
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
