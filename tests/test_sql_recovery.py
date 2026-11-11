import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from app.config import Settings
from app.recovery_models import RecoveryFeedback
from evaluation import run_v1


class SqlRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        folder = self.root / "example"
        folder.mkdir()
        with closing(sqlite3.connect(folder / "example.sqlite")) as db:
            db.executescript("""
                CREATE TABLE notes(id INTEGER PRIMARY KEY, memo TEXT);
                CREATE TABLE regions(id INTEGER PRIMARY KEY, local_label TEXT);
                CREATE TABLE mapping(note INTEGER REFERENCES notes(id),
                                     region INTEGER REFERENCES regions(id));
                INSERT INTO notes VALUES (1, 'first');
                INSERT INTO regions VALUES (1, 'north');
                INSERT INTO mapping VALUES (1, 1);
            """)
        self.question = {
            "question_id": 1, "db_id": "example", "difficulty": "simple",
            "question": "What is the memo for each note?",
            "evidence": "",
            "SQL": "SELECT memo AS reference_only_alias FROM notes",
        }
        self.settings = Settings("mock", "https://example.invalid", "mock", "mock", 0)

    def evaluate(self, responses, *, budget=3, execute=None):
        calls = []
        candidates = iter(responses)

        def model(_settings, messages, **kwargs):
            calls.append(messages)
            sql = next(candidates)
            if isinstance(sql, Exception):
                raise sql
            content = json.dumps(sql if isinstance(sql, (dict, list)) else {"sql": sql})
            return 200, {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
            }

        with patch.object(run_v1, "DB_ROOT", self.root), patch.object(run_v1, "chat_completion", model):
            if execute is None:
                record = self._evaluate(budget)
            else:
                with patch.object(run_v1.ReadOnlyDB, "query", execute):
                    record = self._evaluate(budget)
        return record, calls

    def _evaluate(self, budget):
        return run_v1.evaluate_question(
            self.question, self.settings, {}, indexes={}, top_k=1, recovery_budget=budget,
        )

    def test_syntax_repair_keeps_first_prompt_and_scope(self):
        v2, _ = self.evaluate(["SELECT FROM notes"], budget=None)
        v3, calls = self.evaluate(["SELECT FROM notes", "SELECT memo FROM notes"])
        self.assertEqual(calls[0], v2["model_input"])
        self.assertEqual(v3["first_ex"], 0)
        self.assertEqual(v3["ex"], 1)
        self.assertEqual(v3["attempts"][1]["action"], "sql_repair")
        self.assertEqual(v3["attempts"][1]["tables"], ["notes"])
        feedback = RecoveryFeedback.model_validate_json(calls[1][1]["content"].split("Execution feedback:\n", 1)[1])
        self.assertEqual(feedback.sqlite_error.phase, "dry_run")
        self.assertEqual(feedback.sqlite_error.kind, "sql_repair")
        self.assertEqual(feedback.retry_count, 1)
        self.assertNotIn("reference_only_alias", calls[1][1]["content"])
        self.assertEqual(v3["usage"]["total_tokens"], 26)

    def test_missing_column_expands_scope_and_adds_join_bridge(self):
        self.question["SQL"] = "SELECT local_label FROM regions"
        row, calls = self.evaluate([
            "SELECT local_label FROM notes",
            "SELECT r.local_label FROM notes n JOIN mapping m ON m.note=n.id JOIN regions r ON r.id=m.region",
        ])
        self.assertEqual(row["schema_linking"]["tables"], ["notes"])
        self.assertEqual(row["schema_linking"]["missing_tables"], ["regions"])
        self.assertEqual(row["attempts"][0]["dry_run"]["error"]["identifier"], "local_label")
        self.assertEqual(row["attempts"][1]["action"], "schema_expansion")
        self.assertEqual(row["attempts"][1]["added_tables"], ["mapping", "regions"])
        self.assertIn("CREATE TABLE regions", calls[1][1]["content"])
        self.assertEqual(row["final_schema_linking"]["missing_tables"], [])
        self.assertEqual(row["ex"], 1)

    def test_missing_table_and_repair_share_budget(self):
        row, _ = self.evaluate(["SELECT * FROM missing_table", "SELECT * FORM regions"], budget=1)
        self.assertEqual(row["model_calls"], 2)
        self.assertEqual(row["recovery_stop_reason"], "recovery_budget_exhausted")
        self.assertEqual(row["ex"], 0)

        row, _ = self.evaluate(["SELECT * FROM regons", "SELECT * FORM regions"], budget=1)
        self.assertEqual(row["attempts"][1]["action"], "schema_expansion")
        self.assertEqual(row["model_calls"], 2)
        self.assertEqual(row["recovery_stop_reason"], "recovery_budget_exhausted")

    def test_visible_column_error_repairs_the_sql_without_expanding(self):
        row, _ = self.evaluate(["SELECT absent_alias.memo FROM notes", "SELECT memo FROM notes"])
        self.assertEqual(row["ex"], 1)
        self.assertEqual(row["attempts"][1]["action"], "sql_repair")
        self.assertEqual(row["attempts"][1]["added_tables"], [])

    def test_default_budget_caps_distinct_failures_at_four_calls(self):
        row, _ = self.evaluate([
            "SELECT FROM notes", "SELECT WHERE notes", "SELECT FROM mapping", "SELECT WHERE mapping",
        ])
        self.assertEqual(row["model_calls"], 4)
        self.assertEqual(row["recovery_attempts"], 3)
        self.assertEqual(row["recovery_stop_reason"], "recovery_budget_exhausted")
        self.assertEqual([attempt["retry_count"] for attempt in row["attempts"]], [0, 1, 2, 3])

    def test_third_retry_is_validated_and_executed(self):
        row, _ = self.evaluate([
            "SELECT FROM notes", "SELECT WHERE notes", "SELECT FROM mapping", "SELECT memo FROM notes",
        ])
        self.assertEqual(row["ex"], 1)
        self.assertEqual(row["recovery_attempts"], 3)
        self.assertEqual(row["attempts"][-1]["dry_run"]["status"], "passed")

    def test_dry_run_failure_does_not_execute_the_candidate(self):
        original = run_v1.ReadOnlyDB.query
        seen = []

        def trace(db, sql, limit=20):
            seen.append(sql)
            return original(db, sql, limit)

        bad = "SELECT FROM notes"
        good = "SELECT memo FROM notes"
        row, _ = self.evaluate([bad, good], execute=trace)
        self.assertIn("EXPLAIN " + bad, seen)
        self.assertNotIn(bad, seen)
        self.assertLess(seen.index("EXPLAIN " + good), seen.index(good))
        self.assertEqual(row["attempts"][0]["status"], "dry_run_error")
        self.assertEqual(row["attempts"][1]["dry_run"]["status"], "passed")

    def test_pydantic_rejects_blank_wrong_type_and_extra_keys_before_sql(self):
        for payload in ({"sql": "  "}, {"sql": 123}, {"sql": "SELECT 1", "answer": 1}, ["SELECT 1"], {}):
            with self.subTest(payload=payload):
                row, _ = self.evaluate([payload])
                self.assertEqual(row["status"], "model_output_error")
                self.assertEqual(row["model_calls"], 1)
                self.assertTrue(row["attempts"][0]["error"]["validation_issues"])
                self.assertNotIn("dry_run", row["attempts"][0])

    def test_repeated_sql_error_stops_before_budget(self):
        row, _ = self.evaluate(["SELECT FROM notes", "SELECT FROM notes;"])
        self.assertEqual(row["model_calls"], 2)
        self.assertEqual(row["recovery_stop_reason"], "repeated_sql_error")

    def test_policy_and_timeout_failures_are_terminal(self):
        for sql in ("DELETE FROM notes", "SELECT memo FROM notes; SELECT 1", "PRAGMA query_only=OFF",
                    "VACUUM", "REINDEX", "SELECT load_extension('other')"):
            with self.subTest(sql=sql):
                row, _ = self.evaluate([sql])
                self.assertEqual(row["model_calls"], 1)
                self.assertEqual(row["recovery_stop_reason"], "policy_blocked")
                self.assertEqual(row["attempts"][0]["error"]["phase"], "dry_run")

        real_query = run_v1.ReadOnlyDB.query

        def execute(db, sql, limit=20):
            if sql == "SELECT memo FROM notes":
                raise TimeoutError("deadline")
            return real_query(db, sql, limit)

        row, _ = self.evaluate(["SELECT memo FROM notes"], execute=execute)
        self.assertEqual(row["model_calls"], 1)
        self.assertEqual(row["recovery_stop_reason"], "timeout")
        self.assertEqual(row["attempts"][0]["dry_run"]["status"], "passed")
        self.assertEqual(row["attempts"][0]["error"]["phase"], "execute")

    def test_successful_sql_including_empty_result_is_not_retried(self):
        row, _ = self.evaluate(["SELECT memo FROM notes WHERE id < 0"])
        self.assertEqual(row["status"], "mismatch")
        self.assertTrue(row["first_sql_executed"])
        self.assertEqual(row["model_calls"], 1)

    def test_model_transport_failure_is_terminal(self):
        row, _ = self.evaluate([URLError("offline")])
        self.assertEqual(row["model_calls"], 1)
        self.assertEqual(row["status"], "model_request_error")


    def test_explain_does_not_run_the_original_query(self):
        with run_v1.ReadOnlyDB(self.root / "example" / "example.sqlite") as db:
            overflow = "SELECT abs(-9223372036854775808)"
            db.dry_run(overflow)
            with self.assertRaisesRegex(sqlite3.OperationalError, "integer overflow"):
                db.query(overflow)
            db.dry_run("/* leading comment */ WITH c AS (SELECT 1) SELECT * FROM c")
            self.assertEqual(db.query("SELECT 1")[1], [(1,)])
