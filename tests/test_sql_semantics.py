import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.sql_semantics import check_query


class SqlSemanticTests(unittest.TestCase):
    def test_explicit_grouped_question_blocks_single_aggregate(self):
        for question in ("Show the number of orders for each region", "按地区分组，统计订单数"):
            with self.subTest(question=question):
                sql = "SELECT COALESCE(COUNT(*), 0) AS total FROM orders"
                check = check_query(question, sql)
                reply = check.abstention(sql, executed=False)
                self.assertTrue(check.blocked)
                self.assertEqual(reply["kind"], "abstained")
                self.assertEqual(reply["stop_reason"], "semantic_check_failed")
                self.assertFalse(reply["executed"])
                self.assertEqual(reply["semantics"]["confidence"], "low")
                self.assertEqual(reply["semantics"]["signals"][0]["code"], "missing_group_by")

    def test_quoted_or_commented_group_by_does_not_satisfy_the_check(self):
        sql = "SELECT COUNT(*) AS \"group by\" FROM orders /* GROUP BY region */ WHERE note = 'GROUP BY'"
        self.assertTrue(check_query("Calculate the order count for each region", sql).blocked)

    def test_aggregation_shape_keeps_valid_and_unproven_queries(self):
        grouped = "Show the number of orders for each region"
        cases = (
            (grouped, "SELECT region, COUNT(*) FROM orders GROUP BY region"),
            (grouped, "SELECT DISTINCT region, COUNT(*) OVER (PARTITION BY region) FROM orders"),
            (grouped, "SELECT region, (SELECT COUNT(*) FROM items) FROM orders"),
            (grouped, "SELECT COUNT(*) FROM orders UNION SELECT COUNT(*) FROM archived"),
            ("What is the average number of orders for each region, overall?", "SELECT AVG(value) FROM rates"),
            ("How many of the schools have more than 10 students each?", "SELECT COUNT(*) FROM schools"),
        )
        for question, sql in cases:
            with self.subTest(question=question, sql=sql):
                check = check_query(question, sql)
                self.assertFalse(check.blocked)
                self.assertEqual(check.as_dict()["confidence"], "unverified")

    def test_scalar_count_distinguishes_zero_empty_null_and_incomplete(self):
        question = "How many orders were placed in October?"
        sql = "SELECT COUNT(*) FROM orders WHERE month = 10"
        cases = (([], 0, True), ([(None,)], 1, True), ([(-1,)], 1, True), ([(0,)], 1, False), ([], None, False))
        for rows, total, blocked in cases:
            with self.subTest(rows=rows, total=total):
                self.assertEqual(check_query(question, sql, rows=rows, total_rows=total).blocked, blocked)
        self.assertFalse(check_query("List orders placed in October", sql, rows=[], total_rows=0).blocked)

    def test_date_id_and_ranking_are_non_blocking_signals(self):
        sql = "SELECT * FROM orders WHERE customer_id LIKE 'A%' AND order_date >= '01/01/2026' ORDER BY total DESC LIMIT 5"
        check = check_query("Find the top 5 orders", sql)
        self.assertFalse(check.blocked)
        self.assertEqual({signal["code"] for signal in check.as_dict()["signals"]}, {
            "ranking_full_rows", "id_text_match", "date_encoding_check",
        })

    def test_targeted_product_cases(self):
        from evaluation.run_semantic_cases import run_cases

        report = run_cases()
        self.assertEqual(report["issues"], [])

    def test_product_returns_abstention_before_executing_wrong_grain(self):
        from app.config import Settings
        from app.product_sql import query_database

        with TemporaryDirectory() as folder:
            path = Path(folder) / "example.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE orders(region TEXT)")
            settings = Settings("fixture", "https://example.invalid", "fixture", "test-only", 0)
            sql = "SELECT COUNT(*) FROM orders"
            response = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
            with patch("app.product_sql.chat_completion", return_value=(200, response)), patch(
                "app.product_sql._scan_result"
            ) as scan:
                reply = query_database(path, "Show the order count for each region", "", settings)
            self.assertEqual(reply["kind"], "abstained")
            self.assertFalse(reply["executed"])
            scan.assert_not_called()
