import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.telemetry import span
from app.tools.db import ReadOnlyDB
from evaluation import compare_product_profiles, replay_product_semantics, run_product


class ProductBenchmarkTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve()
        database = self.root / "fixture" / "fixture.sqlite"
        database.parent.mkdir()
        with sqlite3.connect(database) as conn:
            conn.execute("CREATE TABLE samples (value INTEGER NOT NULL)")
            conn.executemany("INSERT INTO samples VALUES (?)", [(n,) for n in range(75)] + [(1,)])
        self.settings = Settings("mock", "https://model.example", "fixture", "fixture", 0)
        self.question = {
            "question_id": 99, "db_id": "fixture", "difficulty": "moderate",
            "question": "List every sample", "evidence": "fixture evidence",
            "SQL": "SELECT value FROM samples ORDER BY value",
        }

    def run_sql(self, sql, *, profile="full"):
        scanned = []
        actual_scan = ReadOnlyDB.scan

        @contextmanager
        def tracked(db, statement, **kwargs):
            scanned.append(statement)
            with actual_scan(db, statement, **kwargs) as rows:
                yield rows

        def completion(*_args, **_kwargs):
            with span("model_call") as call:
                call["usage"] = {"prompt_tokens": 12, "completion_tokens": 2}
                return 200, {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}

        with patch.object(ReadOnlyDB, "scan", tracked), patch.object(run_product.product_sql, "chat_completion", completion):
            return run_product.evaluate_question(
                self.question, self.settings, self.root, features=run_product.PROFILES[profile],
            ), scanned

    def test_full_raw_set_equality_reuses_single_real_scan(self):
        sql = "SELECT DISTINCT value FROM samples ORDER BY value DESC"
        record, scanned = self.run_sql(sql)
        self.assertEqual(record["status"], "correct")
        self.assertEqual((record["prediction_count"], record["reference_count"]), (75, 76))
        self.assertEqual(scanned.count(sql), 1)
        self.assertEqual(record["usage"]["total_tokens"], 14)
        self.assertEqual(record["semantic_checks"], {"pre_execution": 1, "post_execution": 1})
        self.assertNotIn("prediction_rows", record)
        self.assertNotIn("reference_rows", record)

    def test_mismatch_after_product_preview_is_detected(self):
        record, _ = self.run_sql("SELECT value FROM samples WHERE value < 70 ORDER BY value")
        self.assertEqual((record["status"], record["ex"]), ("mismatch", 0))
        summary = run_product.summarize([record])
        self.assertEqual(summary["wrong_answer_rate"], 1.0)
        self.assertEqual(summary["execution_success_rate"], 1.0)
        self.assertEqual(summary["by_database"]["fixture"]["correct"], 0)

    def test_product_row_limit_is_not_scored_from_preview(self):
        with patch.object(run_product.product_sql, "_MAX_COUNTED_ROWS", 51):
            record, scanned = self.run_sql("SELECT value FROM samples")
        self.assertEqual(record["status"], "result_limit")
        self.assertEqual(record["ex"], 0)
        self.assertIsNone(record["prediction_count"])
        self.assertNotIn(self.question["SQL"], scanned)

    def test_ablation_profiles_change_only_declared_features(self):
        features = run_product.PROFILES
        for name, expected in {
            "full": (True, True, True), "baseline": (False, False, False),
            "no-value-hints": (False, True, True), "no-prompt": (True, False, True),
            "no-semantics": (True, True, False),
        }.items():
            with self.subTest(profile=name):
                flags = features[name]
                self.assertEqual((flags.value_hints, flags.prompt_guidance, flags.semantic_checks), expected)

    def test_replay_blocks_wrong_candidate_from_same_logged_sql(self):
        self.question.update(question="How many samples are there?", SQL="SELECT COUNT(*) FROM samples")
        record, _ = self.run_sql("SELECT value FROM samples WHERE value < 0", profile="no-semantics")
        self.assertEqual(record["semantic_checks"], {"pre_execution": 0, "post_execution": 0})
        self.assertEqual(record["status"], "mismatch")
        replayed = replay_product_semantics.replay_candidate(self.question, record, self.root)
        self.assertEqual(replayed["outcome"], "blocked_post_execution")
        self.assertEqual(replayed["signals"], [{
            "code": "missing_count_result", "phase": "post_execution", "blocking": True,
        }])
        summary = replay_product_semantics.summarize_replay([replayed])
        self.assertEqual(summary["wrong_answers_blocked"], 1)
        self.assertEqual(summary["without_semantics_correct"], summary["with_semantics_correct"])

    def test_matrix_compares_matching_question_ids_and_distinct_runs(self):
        correct, _ = self.run_sql("SELECT value FROM samples", profile="full")
        wrong, _ = self.run_sql("SELECT value FROM samples WHERE value > 0", profile="baseline")
        common = {
            "protocol": {"workers": 3}, "source_sha256": {"runner": "source"},
            "prompt_version": "fixture", "system_prompt_sha256": "prompt",
        }
        full = dict(common, profile="full", run_id="full-1", metrics=run_product._metrics([correct]), scores={99: 1})
        baseline = dict(common, profile="baseline", run_id="baseline-1", metrics=run_product._metrics([wrong]), scores={99: 0})
        report = compare_product_profiles.compare_runs([baseline, full], repeats=1, legacy=False)
        compare = report["full_vs_profile"]["baseline"]
        self.assertEqual(compare["paired_deltas_pp"]["median"], 100)
        self.assertEqual(compare["paired_runs"][0]["full_only_correct_ids"], [99])
        self.assertEqual(report["profiles"]["full"]["token_totals"]["total_tokens"]["median"], 14)
        with self.assertRaisesRegex(ValueError, "Duplicate benchmark run"):
            compare_product_profiles.compare_runs([baseline, full, full], repeats=1, legacy=False)
