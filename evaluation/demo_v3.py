"""Demonstrate a rejected SQL candidate, one repair, and successful execution."""
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from app.config import load_settings
from app.llm import chat_completion
from app.sql_recovery import MAX_RECOVERIES, run_recovery
from app.text2sql import build_messages, full_schema
from app.tools.db import ReadOnlyDB

_BAD_SQL = "SELECT FROM notes"
_FIXED_SQL = "SELECT memo FROM notes WHERE id = 1"


def _response(sql: str) -> dict:
    return {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Use the configured model for the repair request")
    args = parser.parse_args()
    settings = load_settings() if args.live else None
    if settings is not None and not (settings.llm_configured and settings.llm_base_url):
        parser.error("Set LLM_API_KEY and LLM_BASE_URL before using --live")

    with tempfile.TemporaryDirectory() as directory:
        database = Path(directory) / "demo.sqlite"
        with closing(sqlite3.connect(database)) as db:
            db.executescript("""
                CREATE TABLE notes(id INTEGER PRIMARY KEY, memo TEXT);
                INSERT INTO notes VALUES (1, 'first');
            """)
        with ReadOnlyDB(database) as db:
            messages = build_messages(full_schema(db), "Give the memo for note id 1.", "")

        first = True

        def request_model(prompt):
            nonlocal first
            if first:
                first = False
                return _response(_BAD_SQL)
            if settings is None:
                return _response(_FIXED_SQL)
            return chat_completion(settings, prompt, max_tokens=1024, json_output=True)[1]

        def validate_sql(sql):
            with ReadOnlyDB(database) as db:
                db.dry_run(sql)

        def execute_sql(sql):
            with ReadOnlyDB(database) as db:
                return db.query(sql, limit=None)

        record, prediction = run_recovery(
            messages, {"tables": ["notes"]}, request_model=request_model,
            validate_sql=validate_sql, execute_sql=execute_sql, expand_scope=lambda *_: None,
            build_prompt=lambda _: messages, max_recoveries=MAX_RECOVERIES,
        )
    rows = prediction[1] if prediction else None
    print(json.dumps({
        "first_candidate": "injected invalid SQL",
        "repair_source": settings.llm_model if settings is not None else "scripted demo",
        "retry_count": record["recovery_attempts"],
        "attempts": [
            {key: attempt[key] for key in ("sql", "status", "dry_run", "retry_count", "usage") if key in attempt}
            for attempt in record["attempts"]
        ],
        "stop_reason": record["recovery_stop_reason"], "rows": rows,
    }, ensure_ascii=False, indent=2))
    return int(rows != [("first",)])


if __name__ == "__main__":
    raise SystemExit(main())
