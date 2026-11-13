"""Run 30 synthetic conversation scenarios against a live or replayed product API."""
from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from uuid import uuid4


def turn(question: str, kind: str, **expected) -> tuple[str, dict]:
    return question, {"kind": kind, **expected}


def pending(question: str, slot: str, **context) -> tuple[str, dict]:
    return turn(question, "clarification", slot=slot, context=context)


def revenue(question: str, cents: int, **context) -> tuple[str, dict]:
    filters = ["from orders", "ordered_on >=", "ordered_on <"]
    if context.get("metric") == "net":
        filters.append("sum(amount_cents - refund_cents)")
    elif context.get("metric") == "gross":
        filters.append("sum(amount_cents)")
    if context.get("region"):
        filters.append(f"region = '{context['region']}'")
    return turn(question, "answer", revenue_cents=cents, context=context, sql_contains=filters)


def query(question: str, rows: list | None = None, **checks) -> tuple[str, dict]:
    if rows is not None:
        checks["rows"] = rows
    return turn(question, "query", **checks)


SCENARIOS = {
    "net-revenue-region": (
        pending("上月营收多少？", "metric"), revenue("扣除退款", 47500, metric="net"),
        revenue("只看华南", 17500, metric="net", region="华南"),
    ),
    "month-then-region-order-count": (
        query("每个月各地区的订单数", row_count=5, sql_contains=["group by", "count(*)"]),
        query("只看十月", [["2026-10", "华东", 1], ["2026-10", "华北", 1], ["2026-10", "华南", 2]],
              context={"period": "2026-10"}, sql_contains=["2026-10-01", "2026-11-01", "group by"]),
        query("只看华南", [["2026-10", "华南", 2]], context={"period": "2026-10", "region": "华南"}),
    ),
    "unbounded-regional-count": (
        query("按地区统计订单数量", [["华东", 2], ["华北", 1], ["华南", 3]]), query("只看华南", [["华南", 3]]),
    ),
    "model-sql-multiple-filters": (
        query("列出订单编号、地区和下单日期，按订单编号升序排列", model_required=True, row_count=6),
        query("只看华南", ids=[1001, 1002, 1005]), query("只看十月", ids=[1001, 1002]),
    ),
    "cancel-clarification": (
        pending("统计那个月的订单", "period"), turn("取消这次澄清", "help", message_contains="已取消"),
        query("一共有多少笔订单？", [[6]]),
    ),
    "pending-month-override": (
        pending("9 月营收是多少？", "metric", period="2026-09"), pending("十月", "metric", period="2026-10"),
        revenue("净营收", 47500, period="2026-10", metric="net"),
    ),
    "pending-relative-month-override": (
        pending("9 月营收是多少？", "metric"), pending("本月", "metric", period="this_month"),
        revenue("净营收", 9000, metric="net"),
    ),
    "pending-net-short-answer": (
        pending("本月华南营业额多少？", "metric", region="华南"), revenue("净营收", 9000, metric="net", region="华南"),
    ),
    "pending-gross-short-answer": (
        pending("本月华南营业额多少？", "metric", region="华南"), revenue("毛营收", 9000, metric="gross", region="华南"),
    ),
    "standalone-query-does-not-inherit-period": (
        revenue("本月华南净营收多少？", 9000), pending("销售额是多少？", "period", period=None),
        pending("十月", "metric", period="2026-10"), revenue("毛营收", 55000, metric="gross"),
    ),
    "pending-replaced-by-new-count": (
        pending("统计那个月的订单", "period"), query("一共有多少笔订单？", [[6]]),
    ),
    "pending-replaced-by-new-revenue": (
        pending("统计那个月的订单", "period"), pending("上月销售额是多少？", "metric"), revenue("净营收", 47500),
    ),
    "month-short-answer": (
        pending("统计那个月的订单", "period"), query("十月", [[4]]),
    ),
    "numeric-month-short-answer": (
        pending("统计那个月的订单", "period"), query("10月", [[4]]),
    ),
    "iso-month-short-answer": (
        pending("统计那个月的订单", "period"), query("2026-10", [[4]]),
    ),
    "grouped-net-revenue": (
        pending("分别统计十月各地区的营业额", "metric", period="2026-10"),
        query("净营收", [["华东", 250], ["华北", 50], ["华南", 175]], unit="元"),
    ),
    "grouped-count-excludes-and-replaces-region": (
        query("按地区统计订单数量", row_count=3), query("只看华南", [["华南", 3]]),
        query("去掉华南", []), query("换成华北", [["华北", 1]]),
    ),
    "model-sql-refund-filter-preserved": (
        query("只统计发生过退款的订单，各地区有多少笔？", [["华东", 1], ["华南", 2]], model_required=True),
        query("只看十月", [["华东", 1], ["华南", 2]]), query("去掉华南", [["华东", 1]]),
    ),
    "model-sql-month-resume": (
        pending("列出那个月原始订单金额最低的两笔订单的编号和金额", "period"),
        query("十月", ids=[1004, 1002], model_required=True),
    ),
    "refinement-updates-month-only": (
        revenue("上月华南净营收多少？", 17500, metric="net", region="华南"),
        revenue("改成十一月", 9000, period="2026-11", metric="net", region="华南"),
    ),
    "clarification-sets-month-and-metric-together": (
        pending("营收是多少？", "period", period=None),
        revenue("十月、净营收", 47500, period="2026-10", metric="net"),
    ),
    "demo-schema-explains-scope": (
        turn("列出数据库的所有表", "error", stop_reason="demo_scope", model_required=False),
        revenue("上月净营收多少？", 47500, period="last_month", metric="net"),
    ),
    "help-clears-clarification": (
        pending("上月营收多少？", "metric"), turn("你能做什么？", "help", model_required=False),
        pending("净营收多少？", "period", period=None),
    ),
    "source-explains-last-query-without-losing-it": (
        query("按地区统计订单数量", row_count=3),
        turn("数据来源", "source", model_required=False, sql_contains=["group by region"]),
        query("只看华南", [["华南", 3]], context={"region": "华南"}),
    ),
    "standalone-refinement-asks-for-an-original-query": (
        turn("只看华南", "help", message_contains="没有成功的查询", model_required=False),
        query("按地区统计订单数量", row_count=3),
    ),
    "refund-total-and-average-amount-use-yuan": (
        query("上月退款总额", [[75]], unit="元", sql_contains=["sum(refund_cents)"]),
        query("上月订单平均金额", [[137.5]], unit="元", sql_contains=["avg(amount_cents)"]),
    ),
    "refinement-clears-region-filter": (
        revenue("上月华南毛营收多少？", 20000, metric="gross", region="华南"),
        revenue("只看全部地区", 55000, metric="gross", region=None),
    ),
    "model-refund-ranking-orders-by-value": (
        query("列出退款金额最多的两笔订单的订单编号和退款金额，按退款金额降序", ids=[1003, 1001],
              model_required=True, sql_contains=["order by", "desc", "limit 2"]),
        query("只看华南", ids=[1001, 1002], model_required=False),
    ),
    "model-rejects-an-unrelated-request": (
        turn("给我写一首诗", "rejected", model_required=True),
        query("按地区统计订单数量", row_count=3),
    ),
    "note-change-requires-explicit-approval": (
        turn("将订单 1001 的备注改为回归用备注", "approval", order_id=1001, note="回归用备注", model_required=False),
        turn("批准", "approval", order_id=1001, note="回归用备注", model_required=False),
    ),
}


def observe(result: dict, trace: dict, expected: dict) -> dict:
    actual = {}
    for field, value in expected.items():
        if field == "context":
            actual[field] = {key: (result.get("context") or {}).get(key) for key in value}
        elif field == "model_required":
            actual[field] = trace.get("model_calls", 0) > 0
        elif field == "row_count":
            actual[field] = len(result.get("rows", []))
        elif field == "ids":
            # The fixture's order IDs and amounts occupy distinct numeric ranges.
            actual[field] = [next((cell for cell in row if type(cell) is int and 1000 <= cell <= 1006), None)
                             for row in result.get("rows", [])]
        elif field == "message_contains":
            actual[field] = value if value in result.get("message", "") else None
        elif field == "sql_contains":
            sql = " ".join((result.get("sql") or "").casefold().split())
            actual[field] = [fragment for fragment in value if fragment.casefold() in sql]
        elif field == "rows" and all(row and isinstance(row[0], str) for row in value):
            rows = result.get("rows")
            actual[field] = value if rows is not None and sorted(rows, key=repr) == sorted(value, key=repr) else rows
        else:
            actual[field] = result.get(field)
    return actual


def live_request(endpoint: str) -> Callable[[dict], list[dict]]:
    opener = build_opener(ProxyHandler({}))

    def send(payload: dict) -> list[dict]:
        request = Request(endpoint, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=120) as response:
            return [json.loads(line) for line in response if line.strip()]

    return send


def run(send: Callable[[dict], list[dict]], *, transport: str = "live") -> dict:
    cases = []
    for name, turns in SCENARIOS.items():
        session = str(uuid4())
        checks = []
        for question, expected in turns:
            payload = {"session_id": session, "question": question, "live": True}
            check = {"question": question, "expected": expected, "passed": False}
            try:
                events = send(payload)
                result = next(event["data"] for event in events if event["event"] == "result")
                trace = events[-1]
                if trace["event"] != "done":
                    raise ValueError("incomplete stream")
                actual = observe(result, trace, expected)
                check.update(actual=actual, passed=actual == expected, latency_ms=trace.get("latency_ms"),
                             model_calls=trace.get("model_calls", 0))
                for field in ("context", "rows"):
                    if field in result:
                        check[field] = result[field]
            except (OSError, ValueError, KeyError, IndexError, StopIteration) as exc:
                check["error"] = type(exc).__name__
            checks.append(check)
            if not check["passed"]:
                break
        passed = len(checks) == len(turns) and all(check["passed"] for check in checks)
        cases.append({"case": name, "passed": passed, "turns": checks})
        print(f"{'PASS' if passed else 'FAIL'} {name}", flush=True)
        if not passed:
            print(json.dumps(checks[-1], ensure_ascii=False), flush=True)
    return {
        "transport": transport,
        "scenarios": len(cases), "passed": sum(case["passed"] for case in cases),
        "model_calls": sum(turn.get("model_calls", 0) for case in cases for turn in case["turns"]),
        "cases": cases,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--base-url", default="http://127.0.0.1:8000")
    mode.add_argument("--record", type=Path, help="Save sanitized model completions after all scenarios pass")
    mode.add_argument("--replay", type=Path, help="Run through the product API with a recorded model cassette")
    parser.add_argument("--env-file", type=Path, help="Load model credentials for a local recording")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.env_file and not args.record:
        parser.error("--env-file can only be used with --record")
    if args.record or args.replay:
        from evaluation.conversation_replay import run_recorded

        report = run_recorded(run, record=args.record, replay=args.replay, env_file=args.env_file)
    else:
        url = urlsplit(args.base_url)
        if (url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.path.rstrip("/")
                or url.username or url.query or url.fragment):
            parser.error("--base-url must be a local HTTP origin")
        report = run(live_request(f"{args.base_url.rstrip('/')}/api/v1/chat"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{report['passed']}/{report['scenarios']} scenarios, {report['model_calls']} model calls")
    for error in report.get("cassette_errors", []):
        print(error)
    passed = report["passed"] == report["scenarios"] and report["model_calls"] and not report.get("cassette_errors")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
