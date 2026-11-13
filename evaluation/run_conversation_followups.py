"""Verify dialogue refinements through a running local API with a configured model."""
from __future__ import annotations

import argparse
import json
import unittest
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


class FollowupAcceptance(unittest.TestCase):
    endpoint = "http://127.0.0.1:8000/api/v1/chat"

    def setUp(self):
        self.session = str(uuid4())
        self.opener = build_opener(ProxyHandler({}))

    def ask(self, question):
        body = {"session_id": self.session, "question": question, "live": True}
        request = Request(self.endpoint, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with self.opener.open(request, timeout=90) as response:
            events = [json.loads(line) for line in response if line.strip()]
        results = [event["data"] for event in events if event["event"] == "result"]
        self.assertEqual(events[-1]["event"], "done", events)
        self.assertEqual(len(results), 1, events)
        return results[0], events[-1]

    def test_month_and_region_keep_order_count_grouping(self):
        original, _ = self.ask("每个月各地区的订单数")
        self.assertEqual(original["kind"], "query")
        self.assertEqual(len(original["rows"]), 5)
        october, _ = self.ask("只看十月")
        self.assertCountEqual(october["rows"], [["2026-10", "华东", 1], ["2026-10", "华北", 1], ["2026-10", "华南", 2]])
        south, _ = self.ask("只看华南")
        self.assertEqual(south["rows"], [["2026-10", "华南", 2]])
        self.assertEqual(south["context"]["period"], "2026-10")

    def test_region_keeps_unbounded_order_count(self):
        original, _ = self.ask("按地区统计订单数量")
        self.assertCountEqual(original["rows"], [["华东", 2], ["华北", 1], ["华南", 3]])
        refined, _ = self.ask("只看华南")
        self.assertEqual(refined["rows"], [["华南", 3]])

    def test_region_keeps_revenue_period_and_refund_policy(self):
        pending, _ = self.ask("上月营收多少？")
        self.assertEqual(pending["slot"], "metric")
        answer, _ = self.ask("扣除退款")
        self.assertEqual(answer["revenue_cents"], 47500)
        refined, _ = self.ask("只看华南")
        self.assertEqual(refined["revenue_cents"], 17500)
        self.assertEqual(refined["date_start"], "2026-10-01")
        self.assertEqual(refined["context"]["metric"], "net")

    def test_refine_actual_model_query(self):
        original, trace = self.ask("列出订单编号、地区和下单日期，按订单编号升序排列")
        self.assertGreater(trace["model_calls"], 0)
        self.assertEqual(len(original["rows"]), 6)
        south, _ = self.ask("只看华南")
        self.assertEqual(south["columns"], original["columns"])
        self.assertEqual(len(south["rows"]), 3)
        self.assertEqual(south["rows"], [row for row in original["rows"] if "华南" in row])
        october, _ = self.ask("只看十月")
        self.assertEqual(october["columns"], original["columns"])
        self.assertEqual(len(october["rows"]), 2)
        self.assertEqual(october["rows"], [row for row in south["rows"] if any(
            isinstance(cell, str) and cell.startswith("2026-10") for cell in row
        )])

    def test_cancel_clarification_in_same_session(self):
        pending, _ = self.ask("统计那个月的订单")
        self.assertEqual(pending["kind"], "clarification")
        canceled, _ = self.ask("取消这次澄清")
        self.assertIn("已取消", canceled["message"])
        answer, _ = self.ask("一共有多少笔订单？")
        self.assertEqual(answer["rows"], [[6]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.path.rstrip("/"):
        parser.error("--base-url must be a local HTTP origin")
    FollowupAcceptance.endpoint = f"{args.base_url.rstrip('/')}/api/v1/chat"
    tests = unittest.defaultTestLoader.loadTestsFromTestCase(FollowupAcceptance)
    return 0 if unittest.TextTestRunner(verbosity=2).run(tests).wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
