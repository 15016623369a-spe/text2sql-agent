"""只读执行 BIRD 官方样例（question_id=0），打印结果。

用法: python -m evaluation.run_official_sample
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import PROJECT_ROOT
from app.tools.db import ReadOnlyDB

JSONL = PROJECT_ROOT / "data" / "bird" / "mini_dev_sqlite.jsonl"
DB_ROOT = PROJECT_ROOT / "data" / "bird" / "dev_databases"


def load_question(path: Path, qid: int = 0) -> dict:
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj["question_id"] == qid:
                return obj
    raise ValueError(f"question_id {qid} 未找到")


def main() -> int:
    q = load_question(JSONL, 0)
    db_path = DB_ROOT / q["db_id"] / f"{q['db_id']}.sqlite"
    print(f"question_id : {q['question_id']}")
    print(f"db_id       : {q['db_id']}")
    print(f"difficulty  : {q['difficulty']}")
    print(f"question    : {q['question']}")
    print(f"db          : {db_path}")
    with ReadOnlyDB(db_path) as db:
        cols, rows = db.query(q["SQL"], limit=None)
    print(f"columns     : {cols}")
    print(f"row count   : {len(rows)}")
    print("result      :", rows[0] if rows else "(空结果)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
