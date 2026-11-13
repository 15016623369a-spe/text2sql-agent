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
                self.assertEqual(first["query_scope"], {"all_time": True, "all_regions": True})
                second = self.ask("那华南呢？", live=True)[-2]["data"]
                self.assertEqual(second["rows"], [[3]])
                self.assertEqual(second["query_scope"], {"all_time": True, "all_regions": False})
                self.assertEqual(second["refined_from"], "演示库有几笔订单？")
                call.assert_called_once()
                third = self.ask("有多少所学校？", live=True, database="schools")[-2]["data"]
                self.assertEqual(third["rows"], [[0]])
                blocked = self.ask("读取审批记录表", live=True)[-2]["data"]
                self.assertEqual(blocked["kind"], "error")
                self.assertEqual(blocked["stop_reason"], "demo_scope")
                outside = self.ask("给我写一首诗", live=True)[-2]["data"]
                self.assertEqual(outside["kind"], "rejected")
                self.assertNotIn("演示库有几笔订单", call.call_args_list[1].args[1][1]["content"])
                self.assertEqual(self.ask("将订单 1001 的备注改为验收专用", database="schools")[-2]["data"]["kind"], "rejected")

    def test_demo_table_inventory_explains_scope_without_model_or_write_advice(self):
        with patch("app.product_sql.chat_completion") as model:
            events = self.ask("列出数据库的所有表", live=True)
        model.assert_not_called()
        error = events[-2]["data"]
        self.assertEqual(error["kind"], "error")
        self.assertEqual(error["stop_reason"], "demo_scope")
        self.assertEqual(error["message"], "演示库只暴露 orders 表。")
        self.assertIn("BIRD", error["suggestion"])
        self.assertNotIn("审批", error["suggestion"])
        self.assertEqual(error["trace_id"], events[-1]["trace_id"])

    def test_demo_model_filtered_result_keeps_unknown_scope_and_writes_stay_blocked(self):
        def response(sql):
            return 200, {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}

        with patch("app.product_sql.chat_completion", side_effect=[
            response("SELECT COUNT(*) AS 订单笔数 FROM orders WHERE ordered_on >= '2026-10-01'"),
            response("DELETE FROM orders"),
        ]):
            filtered = self.ask("一共有多少笔订单？", live=True)[-2]["data"]
            self.assertEqual(filtered["rows"], [[5]])
            self.assertNotIn("query_scope", filtered)
            blocked = self.ask("列出订单明细", live=True)[-2]["data"]
            self.assertEqual(blocked["stop_reason"], "policy_blocked")
            self.assertIn("只读", blocked["message"])

    def test_scope_refinement_preserves_demo_groupings_and_revenue_metric(self):
        with patch("app.product_sql.chat_completion") as call:
            original = "每个月各地区的订单数"
            grouped = self.ask(original, live=True)[-2]["data"]
            self.assertEqual(len(grouped["rows"]), 5)
            self.assertEqual(grouped["query_scope"], {"all_time": True, "all_regions": True})
            month = self.ask("只看十月", live=True)[-2]["data"]
            self.assertEqual(month["refined_from"], original)
            self.assertEqual(month["query_scope"], {"all_time": False, "all_regions": True})
            self.assertEqual(month["columns"], ["月份", "地区", "订单笔数"])
            self.assertEqual(month["rows"], [["2026-10", "华东", 1], ["2026-10", "华北", 1], ["2026-10", "华南", 2]])
            region = self.ask("仅看华南", live=True)[-2]["data"]
            self.assertEqual(region["rows"], [["2026-10", "华南", 2]])
            self.assertEqual(region["context"]["period"], "2026-10")
            self.assertEqual(region["refined_from"], "2026-10 各月各地区订单笔数")

            original = "按地区统计订单数量"
            self.assertEqual(dict(self.ask(original, live=True)[-2]["data"]["rows"]), {"华东": 2, "华北": 1, "华南": 3})
            self.assertEqual(self.ask("只看华南", live=True)[-2]["data"]["rows"], [["华南", 3]])
            self.assertEqual(self.ask("去掉华南", live=True)[-2]["data"]["rows"], [])
            self.assertEqual(self.ask("换成华北", live=True)[-2]["data"]["rows"], [["华北", 1]])

            original = "上月营收多少？"
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "metric")
            self.assertEqual(self.ask("扣除退款", live=True)[-2]["data"]["revenue_cny"], "475.00")
            answer = self.ask("只看华南", live=True)[-2]["data"]
            self.assertEqual(answer["revenue_cny"], "175.00")
            self.assertEqual(answer["context"], {"period": "last_month", "region": "华南", "metric": "net"})
            self.assertEqual(answer["refined_from"], "2026-10 净营收")
            self.assertEqual(self.ask("改成十一月", live=True)[-2]["data"]["revenue_cny"], "90.00")
            call.assert_not_called()

    def test_model_query_refinement_constrains_source_before_original_aggregation(self):
        original = "只统计发生过退款的订单，各地区有多少笔？"
        sql = "SELECT region, COUNT(*) FROM orders WHERE refund_cents > 0 GROUP BY region ORDER BY region"
        model = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            first = self.ask(original, live=True)[-2]["data"]
            self.assertEqual(first["rows"], [["华东", 1], ["华南", 2]])
            month = self.ask("只看十月", live=True)[-2]["data"]
            self.assertTrue(month["sql"].endswith(sql))
            self.assertEqual(month["columns"], first["columns"])
            self.assertEqual(month["rows"], first["rows"])
            excluded = self.ask("去掉华南", live=True)[-2]["data"]
            self.assertEqual(excluded["excluded_regions"], ["华南"])
            self.assertEqual(excluded["rows"], [["华东", 1]])
            replacement = self.ask("只看九月", live=True)[-2]["data"]
            self.assertEqual(replacement["rows"], [])
            self.assertIn("2026-09-01", replacement["sql"])
            self.assertNotIn("ordered_on >= '2026-10-01'", replacement["sql"])
            self.assertEqual(replacement["refined_from"], original)
            call.assert_called_once()

    def test_scope_refinement_rejects_model_sql_bypassing_filter(self):
        sql = "SELECT COUNT(*) FROM main.orders"
        model = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            self.assertIn("想", self.ask("只看华南", live=True)[-2]["data"]["message"])
            self.assertEqual(self.ask("演示库有几笔订单？", live=True)[-2]["data"]["rows"], [[6]])
            refused = self.ask("只看华南", live=True)[-2]["data"]
            self.assertEqual(refused["kind"], "help")
            self.assertNotIn("sql", refused)
            call.assert_called_once()

    def test_demo_aggregate_money_is_displayed_in_yuan(self):
        for question, expected, column in (
            ("上月订单平均金额", 137.5, "平均订单金额（元）"),
            ("上月退款总额", 75, "退款总额（元）"),
        ):
            with self.subTest(question=question):
                answer = self.ask(question, live=True)[-2]["data"]
                self.assertEqual(answer["rows"], [[expected]])
                self.assertEqual(answer["columns"], [column])
                self.assertEqual(answer["unit"], "元")
                self.assertIn(f"{expected:.2f}", answer["message"])

    def test_known_demo_month_clarification_and_resume_are_deterministic(self):
        for answer in ("十月", "10月", "2026-10"):
            with self.subTest(answer=answer), patch("app.product_sql.chat_completion") as call:
                self.session = str(uuid4())
                reply = self.ask("统计那个月的订单", live=True)[-2]["data"]
                self.assertEqual(reply["kind"], "clarification")
                self.assertEqual(reply["slot"], "period")
                self.assertEqual(reply["original_question"], "统计那个月的订单")
                self.assertIsNone(reply["source"])
                result = self.ask(answer, live=True)[-2]["data"]
                self.assertEqual(result["rows"], [[4]])
                self.assertIn("2026-10-01", result["sql"])
                call.assert_not_called()

    def test_unresolved_demo_month_blocks_unscoped_sql_and_grounds_complex_request(self):
        original = "列出那个月原始订单金额最低的两笔订单的编号和金额"
        sql = ("SELECT id, amount_cents FROM orders WHERE ordered_on >= '2026-10-01' "
               "AND ordered_on < '2026-11-01' ORDER BY amount_cents LIMIT 2")
        model = {"choices": [{"message": {"content": json.dumps({"sql": sql})}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            first = self.ask(original, live=True)[-2]["data"]
            self.assertEqual(first["slot"], "period")
            call.assert_not_called()
            second = self.ask("十月", live=True)[-2]["data"]
            self.assertEqual(second["rows"], [[1004, 5000], [1002, 8000]])
            prompt = call.call_args.args[1][1]["content"]
            self.assertIn("Question:\n列出2026年10月原始订单金额最低的两笔订单的编号和金额", prompt)
            self.assertIn(f'"original_question": "{original}"', prompt)

    def test_model_clarification_repetition_stops_and_clears_pending(self):
        original = "列出一段时间内原始订单金额最高的两笔订单的编号和金额"
        outputs = [
            {"clarification": "你想查哪个月的订单？", "slot": "period"},
            {"clarification": "请提供要统计的时间范围。"},
            {"sql": "SELECT note FROM orders ORDER BY id"},
        ]
        with patch("app.product_sql.chat_completion", side_effect=[
            (200, {"choices": [{"message": {"content": json.dumps(output)}}]}) for output in outputs
        ]) as call:
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "period")
            partial = self.ask("净营收", live=True)[-2]["data"]
            self.assertEqual(partial["slot"], "period")
            self.assertEqual(partial["context"]["metric"], "net")
            call.assert_called_once()
            result = self.ask("十月", live=True)[-2]["data"]
            self.assertEqual(result["kind"], "help")
            self.assertIn("已结束澄清", result["message"])
            prompt = call.call_args.args[1][1]["content"]
            self.assertIn(f"Question:\n{original}", prompt)
            self.assertIn('"resolved_slots": {"metric": "net", "period": "2026-10"}', prompt)
            self.assertEqual(self.ask("列出订单备注", live=True)[-2]["data"]["kind"], "query")
            self.assertNotIn(original, call.call_args.args[1][1]["content"])
            self.assertNotIn("2026-10", call.call_args.args[1][1]["content"])

    def test_named_source_accepts_region_names_outside_demo_slots(self):
        from app.product_api import app

        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            fixture = root / "schools" / "schools.sqlite"
            fixture.parent.mkdir()
            with sqlite3.connect(fixture) as connection:
                connection.executescript("CREATE TABLE schools(city TEXT); INSERT INTO schools VALUES ('上海');")
            outputs = [
                {"clarification": "请指定要查询的地区。"},
                {"sql": "SELECT COUNT(*) FROM schools WHERE city = '上海'"},
            ]
            with patch.object(app.state, "bird", root), patch(
                "app.product_sql.chat_completion", side_effect=[
                    (200, {"choices": [{"message": {"content": json.dumps(output)}}]}) for output in outputs
                ],
            ) as call:
                first = self.ask("那边有多少所学校？", live=True, database="schools")[-2]["data"]
                self.assertEqual(first["slot"], "region")
                self.assertEqual(first["source"], "schools")
                result = self.ask("上海", live=True, database="schools")[-2]["data"]
                self.assertEqual(result["rows"], [[1]])
                prompt = call.call_args.args[1][1]["content"]
                self.assertIn("那边有多少所学校？", prompt)
                self.assertIn('"clarification_answers": ["上海"]', prompt)

    def test_new_count_and_revenue_questions_replace_model_pending(self):
        model = {"choices": [{"message": {"content": '{"clarification":"请提供时间范围"}'}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            original = "列出一段时间内原始订单金额最高的两笔订单的编号和金额"
            for fresh in ("一共有多少笔订单？", "订单总数"):
                self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "period")
                result = self.ask(fresh, live=True)[-2]["data"]
                self.assertEqual(result["rows"], [[6]])
                self.assertNotIn("ordered_on", result["sql"])
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "period")
            fresh = self.ask("上月销售额", live=True)[-2]["data"]
            self.assertEqual(fresh["slot"], "metric")
            self.assertEqual(fresh["original_question"], "上月销售额")
            self.assertEqual(self.ask("净营收", live=True)[-2]["data"]["revenue_cents"], 47500)
            self.assertEqual(call.call_count, 3)

    def test_pending_clarifications_can_cancel_or_stop_after_bad_answers(self):
        model = {"choices": [{"message": {"content": '{"clarification":"请提供月份"}'}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            original = "列出一段时间内原始订单金额最高的两笔订单的编号和金额"
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "period")
            self.assertEqual(self.ask("不清楚", live=True)[-2]["data"]["slot"], "period")
            self.assertEqual(self.ask("不知道", live=True)[-2]["data"]["kind"], "help")
            call.assert_called_once()
            self.assertEqual(self.ask(original, live=True)[-2]["data"]["slot"], "period")
            self.assertIn("已取消", self.ask("取消这次澄清", live=True)[-2]["data"]["message"])
            self.assertEqual(self.ask("上月营收", live=True)[-2]["data"]["slot"], "metric")
            self.assertIn("已取消", self.ask("取消这次澄清", live=True)[-2]["data"]["message"])
            self.assertEqual(call.call_count, 2)

    def test_explicit_new_write_is_not_an_answer_to_model_clarification(self):
        model = {"choices": [{"message": {"content": '{"clarification":"请提供月份"}'}}]}
        with patch("app.product_sql.chat_completion", return_value=(200, model)) as call:
            self.assertEqual(self.ask("列出一段时间内金额最高的订单", live=True)[-2]["data"]["slot"], "period")
            pending = self.ask("将订单1001的备注改为验收专用", live=True)[-2]["data"]
            self.assertEqual(pending["kind"], "approval")
            self.assertEqual(self.ask("取消这次澄清", live=True)[-2]["data"], pending)
            call.assert_called_once()

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

    def test_grouped_demo_clarification_preserves_period_and_operation(self):
        original = "分别统计十月各地区的营业额"
        with patch("app.product_sql.chat_completion") as call:
            pending = self.ask(original, live=True)[-2]["data"]
            self.assertEqual(pending["slot"], "metric")
            self.assertEqual(pending["context"]["period"], "2026-10")
            answer = self.ask("净营收", live=True)[-2]["data"]
            call.assert_not_called()
        self.assertEqual(answer["kind"], "query")
        self.assertEqual(dict(answer["rows"]), {"华南": 175, "华东": 250, "华北": 50})
        self.assertEqual(answer["columns"], ["地区", "净营收（元）"])
        self.assertEqual(answer["unit"], "元")
        self.assertIn("GROUP BY region", answer["sql"])
        self.assertIn("2026-10-01", answer["sql"])

    def test_month_correction_overwrites_previous_month_during_revenue_clarification(self):
        with patch("app.product_sql.chat_completion") as call:
            first = self.ask("9 月营收是多少？", live=True)[-2]["data"]
            self.assertEqual(first["context"]["period"], "2026-09")
            corrected = self.ask("十月", live=True)[-2]["data"]
            self.assertEqual(corrected["context"]["period"], "2026-10")
            self.assertEqual(corrected["slot"], "metric")
            result = self.ask("净营收", live=True)[-2]["data"]
            self.assertEqual(result["date_start"], "2026-10-01")
            self.assertEqual(result["revenue_cents"], 47500)
            southern = self.ask("只看华南", live=True)[-2]["data"]
            self.assertEqual(southern["revenue_cents"], 17500)
            self.assertEqual(southern["refined_from"], "2026-10 净营收")
            self.assertIn("region = '华南'", southern["sql"])
            next_month = self.ask("改成十一月", live=True)[-2]["data"]
            self.assertEqual(next_month["revenue_cents"], 9000)
            self.assertEqual(next_month["refined_from"], "2026-10 华南 净营收")
            self.assertEqual(next_month["date_start"], "2026-11-01")
            self.assertEqual(self.ask("上月营收（扣除退款）是多少？", live=True)[-2]["data"]["revenue_cents"], 47500)
            call.assert_not_called()

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
