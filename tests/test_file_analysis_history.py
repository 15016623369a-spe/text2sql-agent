"""Check file analysis persistence through the authenticated product endpoints."""
import hashlib
import json
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from tempfile import TemporaryDirectory
from threading import Event
from unittest.mock import patch
from uuid import uuid4

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class FileAnalysisHistoryTests(unittest.TestCase):
    upload = "类别,金额\n甲,20\n乙,30\n".encode()
    filename = "测试.xlsx"

    def setUp(self):
        from app.file_analysis import FileAnalysis
        from app.product_api import app
        from sandbox.step7.protocol import ResultTable, TaskResult

        self.app = app
        self.session_id = str(uuid4())
        self.path = f"/api/v1/sessions/{self.session_id}/file-analysis"
        self.directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": self.directory, "RAG_DEPLOYMENT": "local", "RAG_GATEWAY_KEY": "",
            "RAG_LANGSMITH_EXPORT": "false", "LLM_API_KEY": "fixture-only",
            "LLM_BASE_URL": "https://api.deepseek.com", "RAG_SANDBOX_SOCKET": "",
        }))
        table = ResultTable.from_rows("金额", ["类别", "金额"], [["甲", 20], ["乙", 30]], preview_rows=1)
        figure = {"data": [{"type": "bar", "x": ["甲", "乙"], "y": [20, 30]}]}
        report = TaskResult(message="统计完成", tables=[table], figure=figure)
        self.analysis = FileAnalysis(report, "统计完成")

    def analyze(self, client, question="汇总金额", *, session=True, headers=None, **params):
        request = {"filename": self.filename, "question": question, **params}
        if session:
            request["session_id"] = self.session_id
        response = client.post("/api/v1/files", params=request, content=self.upload,
                               headers={"Content-Type": "application/octet-stream", **(headers or {})})
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[-1]["event"], "done")
        return events[-2], events[-1]

    def test_saved_file_chart_layout_and_export_rows_survive_restart_and_reuse(self):
        with TestClient(self.app) as client, patch("app.product_api.analyze_upload", return_value=self.analysis):
            self.assertIsNone(client.get(self.path).json())
            result, done = self.analyze(client, sheet=1, header_row=3, header_depth=2, live=True)
            self.assertTrue(done["history_stored"])
            saved = client.get(self.path).json()
            self.assertEqual(saved["layout"], {"sheet": 1, "header_row": 3, "header_depth": 2})
            self.assertEqual(saved["filename"], self.filename)
            self.assertEqual(saved["question"], "汇总金额")
            self.assertTrue(saved["live"])
            self.assertEqual(saved["result"], result["data"])
            self.assertEqual(saved["result"]["report"]["tables"][0]["additional_rows"], [["乙", 30]])
            self.assertEqual(saved["result"]["report"]["figure"], self.analysis.report.figure)
            self.assertEqual(saved["digest"], hashlib.sha256(self.upload).hexdigest())
            self.assertEqual(saved["trace"]["trace_id"], done["trace_id"])
        with TestClient(self.app) as client, patch("app.product_api.analyze_upload", return_value=self.analysis) as worker:
            self.assertEqual(client.get(self.path).json(), saved)
            restored = client.get(f"{self.path}/upload", params={"trace_id": done["trace_id"]})
            self.assertEqual(restored.content, self.upload)
            self.assertEqual(restored.headers["Cache-Control"], "no-store")
            self.upload = restored.content
            _, newer = self.analyze(client, "再次分析", **saved["layout"])
            self.assertEqual(worker.call_args.args[1], restored.content)
            self.assertEqual(client.get(self.path).json()["question"], "再次分析")
            self.assertNotEqual(newer["trace_id"], done["trace_id"])
            self.assertEqual(client.get(f"{self.path}/upload", params={"trace_id": done["trace_id"]}).status_code, 404)

    def test_owner_isolation_deletion_and_no_store(self):
        key = "a" * 64
        owner = {"X-Rag-Gateway-Key": key, "X-Rag-User": "alice"}
        other = {**owner, "X-Rag-User": "bob"}
        with patch.dict(os.environ, {"RAG_DEPLOYMENT": "production", "RAG_GATEWAY_KEY": key}), \
             TestClient(self.app) as client, patch("app.product_api.analyze_upload", return_value=self.analysis):
            _, done = self.analyze(client, headers=owner)
            upload = f"{self.path}/upload?trace_id={done['trace_id']}"
            self.assertEqual(client.get(self.path).status_code, 401)
            self.assertIsNone(client.get(self.path, headers=other).json())
            self.assertEqual(client.get(upload, headers=other).status_code, 404)
            self.assertEqual(client.delete(self.path, headers=other).status_code, 204)
            self.assertEqual(client.get(upload, headers=owner).content, self.upload)
            self.assertEqual(client.get(self.path, headers=owner).headers["Cache-Control"], "no-store")
            self.assertEqual(client.delete(self.path, headers=owner).status_code, 204)
            self.assertIsNone(client.get(self.path, headers=owner).json())
            self.assertEqual(client.get(upload, headers=owner).status_code, 404)

    def test_legacy_request_and_failed_analysis_keep_previous_snapshot(self):
        with TestClient(self.app) as client, patch(
            "app.product_api.analyze_upload", side_effect=[self.analysis, self.analysis, ValueError("文件无效")],
        ):
            self.analyze(client, "已保存")
            original = client.get(self.path).json()
            _, unsaved = self.analyze(client, "旧调用", session=False)
            self.assertNotIn("history_stored", unsaved)
            failed, done = self.analyze(client, "无效文件")
            self.assertEqual(failed["event"], "error")
            self.assertFalse(done["history_stored"])
            self.assertEqual(client.get(self.path).json(), original)

    def test_overwrite_and_delete_cannot_restore_an_inflight_result(self):
        started = Event()
        release = Event()

        def block(_filename, _data, question, *_args, **_kwargs):
            if question == "较早请求":
                started.set()
                if not release.wait(timeout=10):
                    raise TimeoutError("test request did not resume")
            return self.analysis

        with TestClient(self.app) as client, patch("app.product_api.analyze_upload", side_effect=block), \
             ThreadPoolExecutor(max_workers=1) as workers:
            for delete in (False, True):
                with self.subTest(delete=delete):
                    started.clear()
                    release.clear()
                    first = workers.submit(self.analyze, client, "较早请求")
                    try:
                        self.assertTrue(started.wait(timeout=5))
                        if delete:
                            self.assertEqual(client.delete(self.path).status_code, 204)
                        else:
                            _, newest = self.analyze(client, "较新请求")
                            self.assertTrue(newest["history_stored"])
                    finally:
                        release.set()
                    self.assertFalse(first.result(timeout=5)[1]["history_stored"])
                    saved = client.get(self.path).json()
                    if delete:
                        self.assertIsNone(saved)
                    else:
                        self.assertEqual(saved["question"], "较新请求")
