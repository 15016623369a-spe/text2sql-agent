"""Exercise PostgreSQL discovery and table scope at the HTTP boundary."""
import json
import os
import sqlite3
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class PostgresProductApiTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import POSTGRES_SOURCE_ID, PostgresSource, app

        root = Path(self.enterContext(TemporaryDirectory()))
        self.id = POSTGRES_SOURCE_ID
        self.schema_url = f"/api/v1/databases/{self.id}/schema"
        self.session = str(uuid4())
        self.source = PostgresSource("postgres.invalid", 5432, "test", "reader", "test-only", "commerce")
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": str(root / "state"), "RAG_BIRD_ROOT": str(root / "bird"),
            "RAG_SANDBOX_SOCKET": "", "RAG_GATEWAY_KEY": "", "RAG_DEPLOYMENT": "local",
            "RAG_LANGSMITH_EXPORT": "false", "LLM_BASE_URL": "https://example.invalid", "LLM_API_KEY": "test-only",
        }))
        sqlite_file = root / "bird" / "schools" / "schools.sqlite"
        sqlite_file.parent.mkdir(parents=True)
        with sqlite3.connect(sqlite_file) as db:
            db.execute("CREATE TABLE schools(id INTEGER)")
        self.enterContext(patch.object(PostgresSource, "from_env", return_value=self.source))
        self.enterContext(patch("app.product_api.PostgresDB", side_effect=self.catalog))
        self.client = self.enterContext(TestClient(app))

    @contextmanager
    def catalog(self, source, *, tables=None):
        self.assertIs(source, self.source)

        class Reader:
            def catalog(self, *, include_samples):
                return [
                    {"schema": "commerce", "name": name, "comment": "测试表", "columns": [
                        {"name": "id", "type": "integer", "comment": "主键", "samples": [1] if include_samples else []},
                    ]}
                    for name in ("customers", "orders") if tables is None or name in tables
                ]

        yield Reader()

    def chat(self, question, tables=None):
        payload = {"session_id": self.session, "database": self.id, "question": question, "live": True}
        if tables is not None:
            payload["tables"] = tables
        response = self.client.post("/api/v1/chat", json=payload)
        self.assertEqual(response.status_code, 200, response.text)
        return json.loads(response.text.splitlines()[-2])["data"]

    @staticmethod
    def query_result():
        return {"kind": "query", "columns": ["count"], "rows": [[3]], "sql": "SELECT COUNT(*) FROM orders",
                "total_rows": 1, "truncated": False}

    def test_discovery_and_opt_in_samples(self):
        sources = self.client.get("/api/v1/databases").json()
        self.assertTrue(sources["postgres"])
        self.assertEqual(set(sources["databases"]), {"schools", self.id})
        catalog = self.client.get(self.schema_url).json()
        self.assertEqual(catalog["database"], self.id)
        self.assertEqual([table["name"] for table in catalog["tables"]], ["customers", "orders"])
        self.assertEqual(catalog["tables"][0]["columns"][0]["samples"], [])
        one = self.client.get(self.schema_url, params={"table": "orders", "include_samples": True}).json()["tables"]
        self.assertEqual([(table["name"], table["columns"][0]["samples"]) for table in one], [("orders", [1])])
        self.assertEqual(self.client.get(self.schema_url, params={"include_samples": True}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/databases/schools/schema").status_code, 404)

    def test_invalid_scope_never_calls_model_or_sql(self):
        with patch("app.product_api.query_database") as execute:
            for names in ([], ["missing"], ["orders", "orders"], ["测" * 22], ["bad\u0000table"]):
                response = self.client.post("/api/v1/query", json={
                    "database": self.id, "question": "统计订单", "tables": names,
                })
                self.assertEqual(response.status_code, 422)
            sqlite_query = self.client.post("/api/v1/query", json={
                "database": "schools", "question": "统计学校", "tables": ["schools"],
            })
            self.assertEqual(sqlite_query.status_code, 422)
            for invalid_source in (
                {"database": "postgresql://example.invalid/other"},
                {"database": self.id, "dsn": "postgresql://example.invalid/other"},
            ):
                response = self.client.post("/api/v1/query", json={"question": "统计订单", **invalid_source})
                self.assertEqual(response.status_code, 422)
        execute.assert_not_called()

    def test_query_carries_selected_tables_into_execution(self):
        with patch("app.product_api.query_database", return_value=self.query_result()) as execute:
            response = self.client.post("/api/v1/query", json={
                "database": self.id, "question": "统计订单", "tables": ["orders", "customers"],
            })
        self.assertEqual(response.status_code, 200)
        self.assertIs(execute.call_args.args[0], self.source)
        self.assertEqual(execute.call_args.kwargs["tables"], ("customers", "orders"))
        result = json.loads(response.text.splitlines()[-2])["data"]
        self.assertEqual(result["source_tables"], ["customers", "orders"])
        self.assertEqual(result["data_source"], self.id)

    def test_table_scope_normalizes_order_and_restores_matching_clarification(self):
        clarify = {"kind": "clarification", "slot": "period", "question": "你想查哪个月？"}
        with patch("app.product_api.answer_data_turn", side_effect=[clarify, self.query_result()]) as answer:
            pending = self.chat("统计那个月的订单", ["orders", "customers"])
            self.assertEqual(pending["source_tables"], ["customers", "orders"])
            self.assertEqual(self.client.get(f"/api/v1/sessions/{self.session}").json(), pending)
            result = self.chat("十月", ["customers", "orders"])
        self.assertEqual(result["source_tables"], ["customers", "orders"])
        self.assertEqual(answer.call_args.args[2]["source_question"], "统计那个月的订单")
        self.assertEqual(answer.call_args.args[2]["answers"], ["十月"])

    def test_table_change_prevents_pending_and_previous_query_reuse(self):
        clarify = {"kind": "clarification", "slot": "period", "question": "你想查哪个月？"}
        with patch("app.product_api.answer_data_turn", side_effect=[
            self.query_result(), self.query_result(), clarify, self.query_result(),
        ]) as answer:
            self.chat("统计订单", ["orders"])
            self.chat("那客户呢", ["customers"])
            self.assertIsNone(answer.call_args.args[1])
            self.chat("统计那个月的订单", ["orders"])
            self.chat("十月", ["customers"])
            self.assertIsNone(answer.call_args.args[2])

    def test_catalog_failure_hides_driver_details(self):
        with patch("app.product_api.PostgresDB", side_effect=sqlite3.OperationalError("hidden service address")):
            response = self.client.get(self.schema_url)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("hidden service address", response.text)
