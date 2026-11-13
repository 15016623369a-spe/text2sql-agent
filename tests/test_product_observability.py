"""Verify user-facing failure details and redacted trace estimates."""
import json
import os
import unittest
import urllib.error
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from app import telemetry

try:
    from fastapi.testclient import TestClient
except ImportError:
    TestClient = None


@unittest.skipUnless(TestClient, "install requirements-step8.txt")
class ProductObservabilityTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        state = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": state, "RAG_LANGSMITH_EXPORT": "false", "RAG_DEPLOYMENT": "local",
            "RAG_GATEWAY_KEY": "", "RAG_BIRD_ROOT": "", "RAG_MODEL_PRICE_USD_PER_MILLION": "",
            "LLM_BASE_URL": "https://api.deepseek.com", "LLM_API_KEY": "test-only", "LLM_MODEL": "deepseek-v4-pro",
        }))
        self.app = app
        self.client = self.enterContext(TestClient(app))
        self.request = {"session_id": str(uuid4()), "question": "演示库共有多少笔订单？", "live": True}

    def test_preflight_keeps_status_and_adds_owned_trace(self):
        cases = (
            (422, "invalid_request", {"json": {"question": ""}}),
            (404, "database_unavailable", {"json": {**self.request, "database": "missing"}}),
            (413, "request_too_large", {"json": {"question": "x" * 20_000}}),
        )
        for status, reason, request in cases:
            with self.subTest(status=status):
                response = self.client.post("/api/v1/chat", **request)
                error = response.json()
                self.assertEqual(response.status_code, status)
                self.assertTrue(error["detail"])
                self.assertTrue(error["suggestion"])
                self.assertEqual(error["stop_reason"], reason)
                self.assertEqual(error["trace_id"], response.headers["X-Trace-Id"])
                trace = self.client.get("/api/v1/traces/" + error["trace_id"]).json()
                self.assertEqual(trace["status"], "error")
                self.assertEqual(trace["error_type"], reason)

    def test_timeout_and_model_failure_omit_private_details(self):
        with patch("app.llm.urllib.request.urlopen", side_effect=urllib.error.URLError("test-only private provider detail")):
            response = self.client.post("/api/v1/chat", json=self.request)
        events = [json.loads(line) for line in response.text.splitlines()]
        error, trace = events[-2]["data"], events[-1]
        self.assertEqual(error["kind"], "error")
        self.assertEqual(error["stop_reason"], "model_request_error")
        self.assertEqual(error["trace_id"], trace["trace_id"])
        self.assertTrue(error["suggestion"])
        self.assertIsNone(trace["estimated_cost_usd"])
        self.assertTrue(any(item["name"] == "model_call" and item["error_type"] == "URLError"
                            for item in trace["timeline"]))
        self.assertNotIn("private provider detail", response.text)

    def test_sql_timeline_and_deepseek_peak_estimate(self):
        payload = {
            "choices": [{"message": {"content": '{"sql":"SELECT COUNT(*) AS 订单笔数 FROM orders"}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 100,
                      "prompt_cache_hit_tokens": 800, "prompt_cache_miss_tokens": 200},
        }
        with patch("app.llm.urllib.request.urlopen") as request:
            provider = request.return_value.__enter__.return_value
            provider.status = 200
            provider.read.return_value = json.dumps(payload).encode()
            response = self.client.post("/api/v1/chat", json=self.request)
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[-2]["data"]["rows"], [[6]])
        done = events[-1]
        self.assertEqual(done["cost_basis"], "deepseek_v4_pro_peak")
        self.assertAlmostEqual(done["estimated_cost_usd"], 0.0006952)
        self.assertEqual(done["usage"]["total_tokens"], 1100)
        timeline = done["timeline"]
        self.assertTrue({"schema_retrieval", "model_call", "sql_dry_run", "sql_execute"}
                        <= {item["name"] for item in timeline})
        offsets = [item["start_ms"] for item in timeline]
        self.assertEqual(offsets, sorted(offsets))
        self.assertTrue(all(item["latency_ms"] >= 0 for item in timeline))
        detail = self.client.get("/api/v1/traces/" + done["trace_id"]).json()
        self.assertEqual(detail["timeline"], timeline)
        self.assertNotIn("SELECT COUNT", str(detail))


class TelemetryTests(unittest.TestCase):
    def test_configured_rate_and_unknown_or_incomplete_usage(self):
        def record(price, *, incomplete=False):
            trace = telemetry.Trace("chat", lambda _: None)
            with trace.active(), telemetry.span("model_call") as call, patch.dict(os.environ, {
                "RAG_MODEL_PRICE_USD_PER_MILLION": price,
            }):
                call["usage"] = {"prompt_tokens": 100, "prompt_cache_miss_tokens": 80}
                if not incomplete:
                    call["usage"]["completion_tokens"] = 50
                telemetry.price_model_call(call, "https://custom.example", "custom")
            return trace.finish("ok", None)

        estimate = record("1,2,3")
        self.assertEqual(estimate["cost_basis"], "configured")
        self.assertAlmostEqual(estimate["estimated_cost_usd"], 0.00033)
        self.assertIsNone(record("")["estimated_cost_usd"])
        self.assertIsNone(record("bad")["estimated_cost_usd"])
        self.assertIsNone(record("1,2,3", incomplete=True)["estimated_cost_usd"])
        self.assertEqual(telemetry.Trace("chat", lambda _: None).finish("ok", None)["estimated_cost_usd"], 0.0)

    def test_langsmith_breaker_resets_on_success_and_stops_once(self):
        failures = [OSError("private URL"), OSError(), None, OSError(), OSError(), OSError()]
        with patch.dict(os.environ, {"RAG_LANGSMITH_EXPORT": "true"}), patch.object(
            telemetry, "_export_failures", 0,
        ), patch.object(telemetry, "_send_langsmith", side_effect=failures) as send, self.assertLogs(
            "app.telemetry", level="WARNING",
        ) as logs:
            for _ in range(9):
                telemetry.export_langsmith({})
            self.assertEqual(send.call_count, 6)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("paused after 3 consecutive failures", logs.output[0])
        self.assertNotIn("private URL", logs.output[0])
