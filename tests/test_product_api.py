"""Regression coverage for the Step 8 API contracts and trace redaction."""
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
class ProductApiTests(unittest.TestCase):
    def setUp(self):
        from app.product_api import app

        directory = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "RAG_DATA_DIR": directory, "RAG_LANGSMITH_EXPORT": "false",
            "RAG_BIRD_ROOT": "", "RAG_SANDBOX_SOCKET": "",
            "LLM_BASE_URL": "https://api.deepseek.com", "LLM_API_KEY": "test-only",
        }))
        self.client = self.enterContext(TestClient(app))
        self.session = str(uuid4())

    def events(self, path, **request):
        response = self.client.post("/api/v1/" + path, **request)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/x-ndjson")
        events = [json.loads(line) for line in response.text.splitlines()]
        self.assertEqual(events[0]["event"], "started")
        self.assertEqual(events[-1]["event"], "done")
        self.assertTrue(all(event["trace_id"] == response.headers["x-trace-id"] for event in events))
        return events

    def ask(self, question, live=False, **fields):
        return self.events("chat", json={
            "session_id": self.session, "question": question, "live": live, **fields,
        })

    def test_approval_requires_own_session_and_structured_decision(self):
        result = self.ask("将订单 1001 的备注改为验收专用")[-2]["data"]
        self.assertEqual(result["kind"], "approval")
        self.assertEqual(self.ask("批准")[-2]["data"]["approval_id"], result["approval_id"])
        decision = {"approval_id": result["approval_id"], "approve": True}
        self.assertEqual(self.events("approvals", json={**decision, "session_id": str(uuid4())})[-2]["event"], "error")
        reply = self.events("approvals", json={**decision, "session_id": self.session})[-2]["data"]
        self.assertEqual(reply["status"], "applied")
        self.assertEqual(reply, self.events("approvals", json={**decision, "session_id": self.session})[-2]["data"])

    def test_provider_usage_and_trace_redaction(self):
        sql = "SELECT COUNT(*) AS 订单笔数 FROM orders"
        model = {
            "choices": [{"message": {"content": json.dumps({"sql": sql})}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "ignore": "private"},
        }
        with patch("app.llm.urllib.request.urlopen") as request:
            provider = request.return_value.__enter__.return_value
            provider.status = 200
            provider.read.return_value = json.dumps(model).encode()
            events = self.ask("演示库共有多少笔订单？", live=True)
        self.assertEqual(events[-2]["data"]["rows"], [[6]])
        trace = self.client.get("/api/v1/traces/" + events[-1]["trace_id"]).json()
        self.assertEqual(trace["usage"]["total_tokens"], 27)
        self.assertEqual(trace["model_calls_with_usage"], 1)
        self.assertTrue({"conversation_ask", "model_call", "sql_dry_run", "sql_execute"} <=
                        {item["name"] for item in trace["spans"]})
        self.assertNotIn(sql, str(trace))
        self.assertNotIn("private", str(trace))
        self.assertNotIn("演示库共有多少笔订单", str(trace))

    def test_capabilities_and_subject_change_during_revenue_clarification(self):
        self.assertEqual(self.ask("上月营收多少？")[-2]["data"]["kind"], "clarification")
        for question in ("介绍一下你的功能", "单纯介绍功能"):
            result = self.ask(question)
            self.assertEqual(result[-2]["data"]["kind"], "help")
            self.assertIn("CSV", result[-2]["data"]["message"])
            self.assertEqual(result[-1]["model_calls"], 0)
        self.assertEqual(self.ask("上月净营收是多少？")[-2]["data"]["revenue_cents"], 47500)

    def test_read_only_query_follow_up_and_data_source_boundary(self):
        from app.product_api import app

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = root / "schools" / "schools.sqlite"
            fixture.parent.mkdir()
            with sqlite3.connect(fixture) as connection:
                connection.execute("CREATE TABLE schools(id INTEGER PRIMARY KEY)")
            replies = (
                {"sql": "SELECT COUNT(*) AS 订单笔数 FROM orders"},
                {"sql": "SELECT COUNT(*) AS 订单笔数 FROM orders WHERE region = '华南'"},
                {"sql": "SELECT COUNT(*) AS 学校数量 FROM schools"},
                {"sql": "SELECT COUNT(*) FROM applied_approvals"},
                {"unsupported": True},
            )
            responses = [{"choices": [{"message": {"content": json.dumps(reply)}}]} for reply in replies]
            with patch.object(app.state, "bird", root), patch(
                "app.product_sql.chat_completion", side_effect=[(200, response) for response in responses],
            ) as call:
                first = self.ask("演示库有几笔订单？", live=True)[-2]["data"]
                self.assertEqual(first["rows"], [[6]])
                second = self.ask("那华南呢？", live=True)[-2]["data"]
                self.assertEqual(second["rows"], [[3]])
                third = self.ask("有多少所学校？", live=True, database="schools")[-2]["data"]
                self.assertEqual(third["rows"], [[0]])
                blocked = self.ask("读取审批记录表", live=True)[-2]["data"]
                self.assertEqual(blocked["kind"], "error")
                outside = self.ask("给我写一首诗", live=True)[-2]["data"]
                self.assertEqual(outside["kind"], "rejected")
                self.assertIn("演示库有几笔订单", call.call_args_list[1].args[1][1]["content"])
                self.assertNotIn("演示库有几笔订单", call.call_args_list[2].args[1][1]["content"])
                self.assertEqual(self.ask("将订单 1001 的备注改为验收专用", database="schools")[-2]["data"]["kind"], "rejected")

    def test_model_clarification_keeps_question_for_follow_up(self):
        outputs = [
            {"clarification": "你想查哪个月的订单？"},
            {"sql": "SELECT COUNT(*) AS 订单笔数 FROM orders WHERE ordered_on LIKE '2026-10-%'"},
        ]
        with patch("app.product_sql.chat_completion", side_effect=[
            (200, {"choices": [{"message": {"content": json.dumps(output)}}]}) for output in outputs
        ]) as call:
            self.assertEqual(self.ask("统计那个月的订单", live=True)[-2]["data"]["kind"], "clarification")
            reply = self.ask("十月", live=True)[-2]["data"]
            self.assertEqual(reply["rows"], [[4]])
            prompt = call.call_args.args[1][1]["content"]
            self.assertIn("统计那个月的订单", prompt)
            self.assertIn("你想查哪个月的订单", prompt)

    def test_upload_limit_and_database_path_validation(self):
        from app.product_sql import database_path
        from sandbox.step7.protocol import MAX_UPLOAD_BYTES

        response = self.client.post(
            "/api/v1/files", params={"filename": "large.csv", "question": "有没有异常值"},
            content=b"x" * (MAX_UPLOAD_BYTES + 1),
        )
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/api/v1/query", json={"database": "../other", "question": "test"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("../other", response.text)
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "untrusted").symlink_to(root.parent, target_is_directory=True)
            self.assertIsNone(database_path(root, "untrusted"))

    def test_data_source_question_reuses_last_query_without_calling_model(self):
        sql = "SELECT COUNT(*) AS 订单笔数 FROM orders"
        model = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            self.assertEqual(self.ask("一共有多少订单？", live=True)[-2]["data"]["rows"], [[6]])
            for live in (True, False):
                with self.subTest(live=live):
                    events = self.ask("这些订单数量的数据从哪里来呢", live=live)
                    result = events[-2]["data"]
                    self.assertEqual(result["kind"], "source")
                    self.assertIn("合成演示订单", result["message"])
                    self.assertIn("orders", result["message"])
                    self.assertEqual(result["sql"], sql)
                    self.assertEqual(events[-1]["model_calls"], 0)
            call.assert_called_once()

    def test_detailed_capabilities_answer_is_local_and_varies_with_request(self):
        with patch("app.product_sql.chat_completion") as call:
            short = self.ask("你能做什么？", live=True)[-2]["data"]
            detail = self.ask("你能够完整的介一你的功能么", live=True)
            detail_result = detail[-2]["data"]
            self.assertEqual(detail_result["kind"], "help")
            self.assertNotEqual(short["message"], detail_result["message"])
            for term in ("只读 SQLite", "TopN", "审批"):
                self.assertIn(term, detail_result["message"])
            self.assertEqual(detail[-1]["model_calls"], 0)
            call.assert_not_called()

    def test_model_help_response_does_not_replace_data_request_with_intro(self):
        model = {"choices": [{"message": {"content": '{"help":true}'}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            answer = self.ask("统计订单来源渠道", live=True)[-2]["data"]
        self.assertEqual(answer["kind"], "clarification")
        self.assertIn("指标", answer["question"])
        call.assert_called_once()

    def test_named_source_answer_never_reuses_query_from_another_database(self):
        from app.product_api import app

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = root / "schools" / "schools.sqlite"
            fixture.parent.mkdir()
            with sqlite3.connect(fixture) as connection:
                connection.execute("CREATE TABLE schools(id INTEGER PRIMARY KEY)")
            sql = "SELECT COUNT(*) AS 订单笔数 FROM orders"
            model = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
            with patch.object(app.state, "bird", root), patch(
                "app.product_sql.chat_completion", return_value=(200, model),
            ) as call:
                self.assertEqual(self.ask("一共有多少订单？", live=True)[-2]["data"]["rows"], [[6]])
                result = self.ask("刚才的查询结果来自哪里？", live=True, database="schools")[-2]["data"]
            self.assertEqual(result["kind"], "source")
            self.assertIn("schools", result["message"])
            self.assertNotIn(str(root), result["message"])
            self.assertNotIn("sql", result)
            call.assert_called_once()


    def test_pending_revenue_short_answers_preserve_original_month_and_region(self):
        for short, metric, amount in (("净营收", "net", 9000), ("扣除退款", "net", 9000),
                                      ("毛营收", "gross", 9000)):
            with self.subTest(short=short):
                self.session = str(uuid4())
                first = self.ask("本月华南营业额多少？", live=True)[-2]["data"]
                self.assertEqual(first["kind"], "clarification")
                self.assertEqual(first["original_question"], "本月华南营业额多少？")
                result = self.ask(short, live=True)
                answer = result[-2]["data"]
                self.assertEqual(answer["revenue_cents"], amount)
                self.assertEqual(answer["date_start"], "2026-11-01")
                self.assertEqual(answer["context"], {"period": "this_month", "metric": metric, "region": "华南"})
                self.assertEqual(result[-1]["model_calls"], 0)

    def test_independent_revenue_asks_for_missing_period_and_metric(self):
        self.assertEqual(self.ask("本月华南净营收多少？")[-2]["data"]["revenue_cents"], 9000)
        first = self.ask("销售额是多少？")[-2]["data"]
        self.assertEqual(first["context"], {"period": None, "region": None, "metric": None})
        self.assertIn("哪个月", first["question"])
        second = self.ask("十月")[-2]["data"]
        self.assertEqual(second["original_question"], "销售额是多少？")
        self.assertEqual(second["context"]["period"], "2026-10")
        self.assertIn("扣除退款", second["question"])
        answer = self.ask("毛营收")[-2]["data"]
        self.assertEqual(answer["revenue_cents"], 55000)
        self.assertEqual(answer["date_start"], "2026-10-01")
        self.assertEqual(answer["date_end_exclusive"], "2026-11-01")
        followup = self.ask("那华南呢？")[-2]["data"]
        self.assertEqual(followup["revenue_cents"], 20000)
        self.assertEqual(followup["context"]["period"], "2026-10")

    def test_model_clarification_precedes_local_metric_match(self):
        original = "分别统计十月各地区的营业额"
        replies = [
            {"clarification": "要用净营收还是毛营收？"},
            {"sql": "SELECT region, SUM(amount_cents - refund_cents) FROM orders "
                    "WHERE ordered_on >= '2026-10-01' AND ordered_on < '2026-11-01' GROUP BY region"},
        ]
        with patch("app.product_sql.chat_completion", side_effect=[
            (200, {"choices": [{"message": {"content": json.dumps(reply)}}]}) for reply in replies
        ]) as call:
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["kind"], "clarification")
            answer = self.ask("净营收", live=True)[-2]["data"]
        self.assertEqual(answer["kind"], "query")
        self.assertEqual(sum(row[1] for row in answer["rows"]), 47500)
        self.assertIn(original, call.call_args.args[1][1]["content"])
        self.assertIn("要用净营收还是毛营收？", call.call_args.args[1][1]["content"])

    def test_file_preview_and_analysis_receive_same_layout(self):
        from sandbox.step7.protocol import FileLayout, FilePreview, SheetPreview
        from app.file_analysis import FileAnalysis

        preview = FilePreview(sheets=[SheetPreview(
            index=1, name="订单", header_row=3, header_depth=2, columns=["订单 / 金额（元）"],
            sample_start=1, rows=[["报表"], [], ["订单"], ["金额（元）"], [42]],
        )])
        raw = b"test bytes"
        common = {"filename": "orders.xlsx", "sheet": 1, "header_row": 3, "header_depth": 2}
        request = {"content": raw, "headers": {"Content-Type": "application/octet-stream"}}
        with patch("app.product_api.preview_upload", return_value=preview) as read, patch(
            "app.product_api.analyze_upload", return_value=FileAnalysis(None, "完成"),
        ) as analyze:
            reply = self.events("files/preview", params=common, **request)[-2]["data"]
            self.assertEqual(reply["preview"]["sheets"][0]["rows"][-1], [42])
            self.assertEqual(reply["kind"], "preview")
            self.events("files", params={**common, "question": "概况"}, **request)
        layout = FileLayout(sheet=1, header_row=3, header_depth=2)
        read.assert_called_once_with("orders.xlsx", raw, layout)
        analyze.assert_called_once_with("orders.xlsx", raw, "概况", None, layout=layout)
