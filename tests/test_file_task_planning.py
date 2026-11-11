"""Exercise the model/worker boundary for question-specific upload analysis."""
import json
import unittest
from unittest.mock import patch

from app.config import Settings
from app.file_analysis import analyze_upload
from app.file_formula import ratio_hints
from app.file_planner import binary_one, expected_mode
from app.telemetry import Trace
from sandbox.step7.protocol import AuditPlan, CoveragePlan, FileLayout, FileSchema, InspectRequest, ProvenancePlan, SheetSchema, TaskRequest, TaskResult

SETTINGS = Settings("deepseek", "https://api.deepseek.com", "deepseek-v4-pro", "test-only", 0)
QUESTION = "按年份和省份统计真实观测、估算填充和未知的比例，有没有异常集中？"
SCHEMA = FileSchema(sheets=[SheetSchema(
    index=1, name="数据", columns=["年份", "省份", "业务量", "业务量来源"], numeric_columns=[0, 2],
)])


def provider(payload):
    return {
        "choices": [{"message": {"content": json.dumps(payload)}}],
        "usage": {"prompt_tokens": 14, "completion_tokens": 6, "total_tokens": 20},
    }


class FileTaskPlanningTests(unittest.TestCase):
    def test_only_schema_and_question_go_to_model(self):
        reply = provider({
            "kind": "run", "sheet": 1, "plan": {
                "mode": "provenance", "fields": [{"metric": 2, "marker": 3}], "groups": [[0], [1]],
            },
        })
        raw = b"this spreadsheet row must stay in Docker"
        trace = Trace("file_analysis", lambda _: None)
        with patch("app.file_analysis._dispatch", side_effect=[
            SCHEMA, TaskResult(message="已按年份和省份计算真实观测与估算填充比例"),
        ]) as worker, patch("app.llm.urllib.request.urlopen") as request, trace.active():
            response = request.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = json.dumps(reply).encode()
            result = analyze_upload("data.xlsx", raw, QUESTION, SETTINGS)
        self.assertEqual(result.kind, "file")
        self.assertEqual(worker.call_args_list[0].args, (InspectRequest(format="xlsx"), raw))
        task, data = worker.call_args_list[1].args
        self.assertIsInstance(task, TaskRequest)
        self.assertEqual(task.sheet, 1)
        self.assertIsInstance(task.plan, ProvenancePlan)
        self.assertEqual(task.plan.groups, [[0], [1]])
        self.assertEqual(data, raw)
        sent = json.loads(request.call_args.args[0].data)
        self.assertEqual(json.loads(sent["messages"][1]["content"]), {
            "question": QUESTION, "schema": SCHEMA.model_dump(mode="json"), "required_mode": "provenance",
        })
        self.assertNotIn(raw.decode(), str(sent))
        self.assertEqual(trace.finish("ok", None)["usage"]["total_tokens"], 20)

    def test_clarification_and_invalid_code_do_not_run_calculation(self):
        with patch("app.file_analysis._dispatch", return_value=SCHEMA) as worker, patch(
            "app.file_planner.chat_completion",
            return_value=(200, provider({"kind": "clarification", "message": "哪个字段记录来源标记？"})),
        ):
            result = analyze_upload("table.xlsx", b"raw", QUESTION, SETTINGS)
        self.assertEqual(result.kind, "clarification")
        self.assertIsNone(result.report)
        self.assertEqual(worker.call_count, 1)
        with patch("app.file_analysis._dispatch", return_value=SCHEMA) as worker, patch(
            "app.file_planner.chat_completion",
            return_value=(200, provider({"kind": "run", "sheet": 1, "plan": {"mode": "python", "code": "arbitrary"}})),
        ), self.assertRaisesRegex(ValueError, "有效的分析计划"):
            analyze_upload("table.xlsx", b"raw", QUESTION, SETTINGS)
        self.assertEqual(worker.call_count, 1)

    def test_offline_does_not_substitute_a_provenance_request_with_iqr(self):
        with patch("app.file_analysis._dispatch") as worker, self.assertRaisesRegex(ValueError, "启用模型"):
            analyze_upload("table.csv", b"raw", QUESTION)
        worker.assert_not_called()

    def test_audit_question_cannot_execute_a_provenance_plan(self):
        question = '验证"人均电信业务总量"和"每百人移动电话用户数"两个派生列算得对不对，写出换算公式；若有偏差列出偏差最大的记录。'
        wrong_plan = {"kind": "run", "sheet": 1, "plan": {
            "mode": "provenance", "fields": [{"metric": 2, "marker": 3}],
        }}
        with patch("app.file_analysis._dispatch", return_value=SCHEMA) as worker, patch(
            "app.file_planner.chat_completion", return_value=(200, provider(wrong_plan)),
        ):
            result = analyze_upload("table.xlsx", b"raw", question, SETTINGS)
        self.assertEqual(result.kind, "clarification")
        self.assertIn("公式和单位", result.narrative)
        self.assertEqual(worker.call_count, 1)

    def test_audit_calibrates_named_units_and_requires_both_targets(self):
        question = '验证"人均电信业务总量"和"每百人移动电话用户数"两个派生列算得对不对，写出换算公式。'
        columns = [
            "年份", "省份", "电信业务总量（亿元）", "常住人口（万人）",
            "人均电信业务总量（元/人）", "移动电话用户（万户）", "每百人移动电话用户数",
        ]
        schema = FileSchema(sheets=[SheetSchema(index=1, name="数据", columns=columns, numeric_columns=[0, 2, 3, 4, 5, 6])])
        hints = ratio_hints(schema, question)[1]
        self.assertEqual([hint.target for hint in hints], [4, 6])
        self.assertEqual([hint.formula.right.value for hint in hints], [10000, 100])
        with patch("app.file_analysis._dispatch", side_effect=[schema, TaskResult(message="已核对")]) as worker, patch(
            "app.file_planner.chat_completion",
        ) as model:
            result = analyze_upload("table.xlsx", b"raw", question, SETTINGS)
        self.assertEqual(result.kind, "file")
        model.assert_not_called()
        plan = worker.call_args.args[0].plan
        self.assertIsInstance(plan, AuditPlan)
        self.assertEqual(plan.checks, hints)
        self.assertEqual([check.unit for check in plan.checks], ["元/人", "户/百人"])
        incomplete = schema.model_copy(update={"sheets": [schema.sheets[0].model_copy(update={
            "columns": [*columns[:4], "人均电信业务总量", *columns[5:]],
        })]})
        with patch("app.file_analysis._dispatch", return_value=incomplete) as worker:
            result = analyze_upload("table.xlsx", b"raw", question, SETTINGS)
        self.assertEqual(result.kind, "clarification")
        worker.assert_called_once()

    def test_audit_executes_only_explicit_formula_with_confirmed_units(self):
        sheet = SheetSchema(index=0, name="CSV", columns=["金额", "数量", "单价"], numeric_columns=[0, 1, 2])
        schema = FileSchema(sheets=[sheet])
        question = "核对金额 = 数量 × 单价"
        with patch("app.file_analysis._dispatch", return_value=schema) as worker:
            result = analyze_upload("table.csv", b"raw", question)
        self.assertEqual(result.kind, "clarification")
        self.assertIn("单位", result.narrative)
        worker.assert_called_once()
        with patch("app.file_analysis._dispatch", side_effect=[schema, TaskResult(message="已核对")]) as worker, patch(
            "app.file_planner.chat_completion",
        ) as model:
            result = analyze_upload("table.csv", b"raw", question + "，单位：金额=元、数量=件、单价=元/件")
        self.assertEqual(result.kind, "file")
        check = worker.call_args.args[0].plan.checks[0]
        self.assertEqual(check.formula.kind, "multiply")
        self.assertEqual(check.unit, "元")
        self.assertEqual([item.unit for item in check.input_units], ["件", "元/件"])
        self.assertEqual(check.basis, "explicit")
        model.assert_not_called()

    def test_multiple_workbooks_require_sheet_selection_before_planning(self):
        sheets = [SCHEMA.sheets[0], SCHEMA.sheets[0].model_copy(update={"index": 0, "name": "第二张数据表"})]
        schema = FileSchema(sheets=sheets)
        with patch("app.file_analysis._dispatch", return_value=schema) as worker, patch(
            "app.file_planner.chat_completion",
        ) as model:
            result = analyze_upload("table.xlsx", b"raw", QUESTION, SETTINGS)
        self.assertEqual(result.kind, "clarification")
        self.assertIn("选择要分析的工作表", result.narrative)
        worker.assert_called_once()
        model.assert_not_called()

    def test_grounded_coverage_request_runs_locally(self):
        question = '为什么2014年有347个城市、2015年起只有322个？分析全国总量时序时该怎么处理才不会得出指标暴跌的错误结论？'
        schema = FileSchema(sheets=[
            SheetSchema(index=0, name="填充数据", columns=[
                "时间", "城市", "省份", "常住人口（万人）", "电信业务总量（亿元）", "移动电话用户（万户）",
                "每百人移动电话用户数", "常住人口（万人）_是否填充",
            ], numeric_columns=[0, 3, 4, 5, 6]),
            SheetSchema(index=1, name="原始数据", columns=["时间", "城市", "常住人口（万人）"], numeric_columns=[0, 2]),
        ])
        with patch("app.file_analysis._dispatch", side_effect=[schema, TaskResult(message="覆盖变化已核对")]) as worker, patch(
            "app.file_planner.chat_completion",
        ) as provider_call:
            result = analyze_upload("table.xlsx", b"raw", question, layout=FileLayout(sheet=0))
        provider_call.assert_not_called()
        request, _ = worker.call_args.args
        self.assertEqual(result.kind, "file")
        self.assertIn("填充数据", result.narrative)
        self.assertEqual(request.sheet, 0)
        self.assertEqual(request.plan, CoveragePlan(mode="coverage", year=0, keys=[2, 1], labels=[2, 1], years=[2014, 2015], measures=[3, 4, 5]))

    def test_city_metric_question_does_not_force_a_coverage_plan(self):
        self.assertIsNone(expected_mode("按城市计算2014和2015的电信业务总量同比"))

    def test_coverage_request_with_no_identity_asks_instead_of_counting(self):
        question = "2014到2015的城市覆盖变化怎样影响全国总量？"
        schema = FileSchema(sheets=[SheetSchema(index=0, name="数据", columns=["年份", "总量"], numeric_columns=[0, 1])])
        wrong = {"kind": "run", "sheet": 0, "plan": {"mode": "aggregate", "statistic": "rows", "groups": [[0]]}}
        with patch("app.file_analysis._dispatch", return_value=schema) as worker, patch(
            "app.file_planner.chat_completion", return_value=(200, provider(wrong)),
        ):
            result = analyze_upload("table.xlsx", b"raw", question, SETTINGS)
        self.assertEqual(result.kind, "clarification")
        self.assertIn("城市标识", result.narrative)
        self.assertEqual(worker.call_count, 1)

    def test_binary_encoding_must_be_explicit_and_unambiguous(self):
        self.assertEqual(binary_one("0=真实观测，1＝估算填充"), "imputed")
        self.assertEqual(binary_one("1 代表原始观测，0 表示插值"), "observed")
        self.assertIsNone(binary_one("1表示真实观测"))
        self.assertIsNone(binary_one("0=原始值，1=填充，也可能 1=真实"))

    def test_simple_single_year_rankings_are_grounded_without_provider(self):
        sheet = SheetSchema(
            index=0, name="填充数据", columns=["时间", "城市", "省份", "常住人口（万人）", "电信业务总量（亿元）"],
            numeric_columns=[0, 3, 4],
        )
        for question, statistic, metric, groups, labels, limit in (
            ("2021 年电信业务前 10 省份并画图", "sum", 4, [[2]], [], 10),
            ("2021 年人口最多的五个城市并画图", "records", 3, [], [1], 5),
        ):
            with self.subTest(question=question), patch(
                "app.file_analysis._dispatch", side_effect=[FileSchema(sheets=[sheet]), TaskResult(message="完成")],
            ) as worker, patch("app.file_planner.chat_completion") as model:
                result = analyze_upload("data.xlsx", b"raw", question, SETTINGS)
                self.assertEqual(result.kind, "file")
                model.assert_not_called()
                plan = worker.call_args.args[0].plan
                self.assertEqual((plan.statistic, plan.groups, plan.labels, plan.limit), (statistic, groups, labels, limit))
                self.assertEqual(plan.order_by.column, metric)
                self.assertEqual(plan.order_by.direction, "desc")
                self.assertEqual(plan.filters[0].model_dump(), {"column": 0, "operator": "eq", "value": 2021})
                self.assertTrue(plan.chart)

    def test_model_must_preserve_year_rank_direction_and_count(self):
        question = "把 2021 年的业务量按省份汇总，列前 2 名"
        valid = {
            "mode": "aggregate", "statistic": "sum", "columns": [2], "groups": [[1]],
            "filters": [{"column": 0, "operator": "eq", "value": 2021}],
            "order_by": {"column": 2, "direction": "desc"}, "limit": 2,
        }
        for change, text in (
            ({"filters": []}, "筛选条件"),
            ({"filters": [{"column": 2, "operator": "eq", "value": 2021}]}, "年份"),
            ({"filters": [{"column": 49, "operator": "eq", "value": 2021}]}, "不存在"),
            ({"filters": [{"column": 1, "operator": "gt", "value": 1}]}, "数值列"),
            ({"limit": 3}, "前 2 名"),
            ({"order_by": None}, "排序"),
            ({"order_by": {"column": 2, "direction": "asc"}}, "排名方向"),
            ({"order_by": {"column": 1, "direction": "desc"}}, "排序指标"),
            ({}, "完成"),
        ):
            answer = provider({"kind": "run", "sheet": 1, "plan": valid | change})
            with self.subTest(change=change), patch(
                "app.file_analysis._dispatch", side_effect=[SCHEMA, TaskResult(message="完成")],
            ) as worker, patch("app.file_planner.chat_completion", return_value=(200, answer)):
                result = analyze_upload("table.xlsx", b"raw", question, SETTINGS)
                self.assertIn(text, result.narrative)
                self.assertEqual(worker.call_count, 1 if change else 2)
                self.assertEqual(result.kind, "clarification" if change else "file")

    def test_rank_over_limit_and_exact_ordinal_have_explicit_response(self):
        for question in ("2021 年各省业务量前 101 名", "排名第 5 名的省份"):
            with self.subTest(question=question), patch(
                "app.file_analysis._dispatch", return_value=SCHEMA,
            ) as worker, patch("app.file_planner.chat_completion") as model:
                result = analyze_upload("table.xlsx", b"raw", question)
                self.assertEqual(result.kind, "unsupported")
                worker.assert_called_once()
                model.assert_not_called()

    def test_explicit_coverage_scope_keeps_both_comparison_years(self):
        schema = FileSchema(sheets=[SheetSchema(
            index=0, name="数据", columns=["年份", "城市", "人口"], numeric_columns=[0, 2],
        )])
        with patch("app.file_analysis._dispatch", side_effect=[schema, TaskResult(message="完成")]) as worker:
            result = analyze_upload("table.xlsx", b"raw", "只比较 2014 到 2015 年的城市覆盖变化")
        self.assertEqual(result.kind, "file")
        plan = worker.call_args.args[0].plan
        self.assertEqual(plan.years, [2014, 2015])
        self.assertEqual(plan.filters[0].model_dump(), {"column": 0, "operator": "between", "value": [2014, 2015]})
