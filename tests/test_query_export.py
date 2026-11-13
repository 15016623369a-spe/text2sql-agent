"""Exercise complete, owner-bound SQL exports through the API."""
import csv
import io
import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class QueryExportTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        root = Path(self.enterContext(TemporaryDirectory()))
        self.key = "b" * 64
        self.database = root / "sample" / "sample.sqlite"
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, note TEXT)")
            conn.executemany("INSERT INTO items VALUES (?, ?)", (
                (number, '=HYPERLINK("example")' if number == 97 else f"条目,{number}") for number in range(1, 101)
            ))
        self.enterContext(patch.dict(os.environ, {
            "RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": self.key,
            "RAG_DATA_DIR": str(root / "state"), "RAG_BIRD_ROOT": str(root),
            "RAG_LANGSMITH_EXPORT": "false", "LLM_API_KEY": "test-only", "LLM_BASE_URL": "https://api.deepseek.com",
        }))
        self.client = self.enterContext(TestClient(app))
        self.session = str(uuid4())

    def headers(self, user="alice"):
        return {"X-Rag-Gateway-Key": self.key, "X-Rag-User": user}

    def query(self, sql, *, path="chat"):
        body = {"database": "sample", "question": "列出条目及备注"}
        if path == "chat":
            body.update({"session_id": self.session, "live": True})
        completion = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, completion)):
            response = self.client.post(f"/api/v1/{path}", headers=self.headers(), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[-2]["event"], "result", events)
        return events[-2]["data"]

    def test_exact_count_and_download_are_from_the_same_ordered_snapshot(self):
        result = self.query("SELECT id, note FROM items ORDER BY id DESC LIMIT 72 OFFSET 3")
        self.assertEqual(result["total_rows"], 72)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["data_source"], "sample")
        self.assertEqual(len(result["rows"]), 50)
        self.assertEqual([row[0] for row in result["rows"]], list(range(97, 47, -1)))
        path = result["export_url"]
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers=self.headers("bob")).status_code, 404)
        with closing(sqlite3.connect(self.database)) as conn, conn:
            conn.execute("DELETE FROM items")
        downloaded = self.client.get(path, headers=self.headers())
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.headers["cache-control"], "no-store")
        self.assertIn("attachment", downloaded.headers["content-disposition"])
        exported = list(csv.reader(io.StringIO(downloaded.content.decode("utf-8-sig"))))
        self.assertEqual(exported[0], ["id", "note"])
        self.assertEqual([int(row[0]) for row in exported[1:]], list(range(97, 25, -1)))
        self.assertEqual(exported[1][1], "'=HYPERLINK(\"example\")")
        self.assertEqual(exported[-1][1], "条目,26")

    def test_budget_limit_never_exposes_a_partial_csv_as_complete(self):
        with patch("app.product_sql._MAX_COUNTED_ROWS", 55):
            result = self.query("SELECT id, note FROM items ORDER BY id", path="query")
        self.assertIsNone(result["total_rows"])
        self.assertEqual(len(result["rows"]), 50)
        self.assertNotIn("export_url", result)
        self.assertIn("执行上限", result["export_unavailable"])

        with patch("app.query_export.MAX_CSV_BYTES", 55):
            result = self.query("SELECT id, note FROM items ORDER BY id", path="query")
        self.assertEqual(result["total_rows"], 100)
        self.assertNotIn("export_url", result)
        self.assertIn("缩小范围", result["export_unavailable"])

    def test_demo_aggregates_keep_scope_and_download_display_amount(self):
        response = self.client.post("/api/v1/chat", headers=self.headers(), json={
            "session_id": self.session, "question": "上月净营收是多少？",
        })
        events = [json.loads(line) for line in response.text.splitlines()]
        result = events[-2]["data"]
        self.assertEqual(result["context"]["metric"], "net")
        self.assertEqual(result["data_source"], "演示订单库")
        self.assertEqual(result["total_rows"], 1)
        downloaded = self.client.get(result["export_url"], headers=self.headers())
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(list(csv.reader(io.StringIO(downloaded.content.decode("utf-8-sig")))),
                         [["营收（元）"], ["475.00"]])


if __name__ == "__main__":
    unittest.main()
