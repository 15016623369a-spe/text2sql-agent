"""Exercise application-level boundaries behind the HTTPS gateway."""
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class ProductionBoundaryTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        state = self.enterContext(TemporaryDirectory())
        self.key = "a" * 64
        self.enterContext(patch.dict(os.environ, {
            "RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": self.key,
            "RAG_DATA_DIR": state, "RAG_LANGSMITH_EXPORT": "false",
            "RAG_BIRD_ROOT": "", "LLM_API_KEY": "", "RAG_SANDBOX_SOCKET": "",
        }))
        self.client = self.enterContext(TestClient(app))

    def headers(self, user):
        return {"X-Rag-Gateway-Key": self.key, "X-Rag-User": user}

    def test_bad_gateway_identity_and_cross_site_requests(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        self.assertEqual(self.client.get("/api/v1/databases").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/databases", headers={"X-Rag-User": "alice"}).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/databases", headers=self.headers("../alice")).status_code, 401)
        response = self.client.post("/api/v1/chat", headers={**self.headers("alice"), "Sec-Fetch-Site": "cross-site"}, json={})
        self.assertEqual(response.status_code, 403)
        origin = self.client.post("/api/v1/chat", headers={**self.headers("alice"), "Origin": "https://other.invalid"}, json={})
        self.assertEqual(origin.status_code, 403)

    def test_session_approval_and_trace_are_bound_to_account(self):
        session_id = str(uuid4())
        response = self.client.post("/api/v1/chat", headers=self.headers("alice"), json={
            "session_id": session_id, "question": "将订单 1001 的备注改为验收专用",
        })
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines()]
        result = next(event["data"] for event in events if event["event"] == "result")
        self.assertEqual(result["kind"], "approval")

        status_path = "/api/v1/sessions/" + session_id
        self.assertEqual(self.client.get(status_path, headers=self.headers("bob")).json()["kind"], "empty")
        self.assertEqual(self.client.get(status_path, headers=self.headers("alice")).json()["kind"], "approval")
        decision = {"session_id": session_id, "approval_id": result["approval_id"], "approve": True}
        refused = self.client.post("/api/v1/approvals", headers=self.headers("bob"), json=decision)
        self.assertIn('"event":"error"', refused.text)
        applied = self.client.post("/api/v1/approvals", headers=self.headers("alice"), json=decision)
        self.assertIn('"status":"applied"', applied.text)
        trace_path = "/api/v1/traces/" + events[-1]["trace_id"]
        self.assertEqual(self.client.get(trace_path, headers=self.headers("bob")).status_code, 404)
        self.assertEqual(self.client.get(trace_path, headers=self.headers("alice")).status_code, 200)

    def test_request_body_limits_and_unsupported_media_type(self):
        headers = self.headers("alice")
        oversized = self.client.post("/api/v1/chat", headers=headers, json={"anything": "x" * 20000})
        self.assertEqual(oversized.status_code, 413)
        chunked = self.client.post("/api/v1/chat", headers=headers, content=iter([b" " * 9000, b" " * 9000]))
        self.assertEqual(chunked.status_code, 413)
        response = self.client.post(
            "/api/v1/files", headers=headers, params={"filename": "a.csv", "question": "统计概况"}, content=b"a\n1",
        )
        self.assertEqual(response.status_code, 415)


class DeploymentMigrationTests(unittest.TestCase):
    def test_trace_migration_preserves_local_history_without_exposing_it(self):
        from app.telemetry import TraceStore

        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "traces.sqlite"
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE traces (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                conn.execute("INSERT INTO traces VALUES ('old', '{}')")
            store = TraceStore(db)
            self.assertEqual(store.get("old"), {})
            self.assertIsNone(store.get("old", owner="alice"))
            store.put({"trace_id": "new"}, owner="alice")
            self.assertEqual(store.get("new", owner="alice"), {"trace_id": "new"})
            self.assertIsNone(store.get("new"))

    def test_production_rejects_missing_gateway_key(self):
        from app.gateway_identity import configured_key

        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": ""}):
            with self.assertRaises(RuntimeError):
                configured_key()


class QueryBudgetTests(unittest.TestCase):
    def test_preview_width_and_memory_are_bounded(self):
        from app.tools.db import ReadOnlyDB, ReadOnlyViolation

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "fixture.sqlite"
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE test (value TEXT)")
            with ReadOnlyDB(path) as db:
                with self.assertRaisesRegex(ReadOnlyViolation, "too many columns"):
                    db.query("SELECT 1, 2", max_columns=1)
                with self.assertRaisesRegex(ReadOnlyViolation, "preview size"):
                    db.query("SELECT zeroblob(2048)", max_result_bytes=1024)
                with self.assertRaises(sqlite3.DataError):
                    db.query("SELECT zeroblob(100000000)")


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class SandboxReadinessTests(unittest.TestCase):
    def test_missing_worker_image_returns_unavailable(self):
        import subprocess

        from app.sandbox_broker import app

        with TestClient(app) as client, patch(
            "app.sandbox_broker.local_docker", return_value=(["docker"], {}),
        ), patch(
            "app.sandbox_broker.subprocess.run", side_effect=subprocess.CalledProcessError(1, "docker"),
        ):
            self.assertEqual(client.get("/healthz").status_code, 503)
