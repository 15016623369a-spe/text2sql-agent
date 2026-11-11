import json
import os
import subprocess
import unittest
from unittest.mock import patch

from app.file_analysis import analyze_upload
from app.file_sandbox import IMAGE, SandboxError, local_docker, run_sandbox
from sandbox.step7.protocol import MAX_UPLOAD_BYTES, AnalysisRequest, AnalysisResult, ColumnProfile, Outlier


def report():
    return AnalysisResult(
        rows=7, columns=2, dropped_blank_rows=0, duplicate_rows=0, chart_columns=["收入"], figure={"data": []},
        numeric=[ColumnProfile(
            name="收入", valid=6, missing=1, invalid=0, minimum=10, maximum=110, median=11,
            q1=10.25, q3=11.75, lower_fence=8, upper_fence=14,
            lower_whisker=10, upper_whisker=12, outlier_count=1,
            examples=[Outlier(row=6, value=110)],
        )],
    )


class FileAnalysisTests(unittest.TestCase):
    def test_graph_routes_file_through_sandbox_and_narrates_outlier(self):
        with patch("app.file_analysis.run_sandbox", return_value=report()) as sandbox:
            result = analyze_upload("../../sample.CSV", b"test", "有没有异常值并画图")
        request, data = sandbox.call_args.args
        self.assertEqual(request, AnalysisRequest(format="csv", action="outliers", chart=True))
        self.assertEqual(data, b"test")
        self.assertIn("1 个疑似异常值", result.narrative)
        self.assertIn("数据行 6 的 110", result.narrative)
        self.assertEqual(result.report.chart_columns, ["收入"])

    def test_unrecognized_inputs_are_rejected_before_launch(self):
        with patch("app.file_analysis.run_sandbox") as sandbox:
            for filename, content, question in (
                ("payload.py", b"content", "统计"), ("table.csv", b"", "统计"),
                ("big.csv", b"x" * (MAX_UPLOAD_BYTES + 1), "统计"),
                ("table.csv", b"content", "读取我的宿主文件"),
            ):
                with self.subTest(filename=filename, question=question), self.assertRaises(ValueError):
                    analyze_upload(filename, content, question)
            sandbox.assert_not_called()

    def test_remote_docker_endpoint_is_rejected(self):
        with patch.dict(os.environ, {"DOCKER_HOST": "ssh://remote.example", "DOCKER_CONTEXT": ""}, clear=True):
            with patch("app.file_sandbox.subprocess.run") as run, self.assertRaisesRegex(SandboxError, "本机"):
                local_docker()
            run.assert_not_called()

    def test_launch_uses_stdin_and_no_volume_flags(self):
        with patch("app.file_sandbox.local_docker", return_value=(["docker", "--host", "unix:///run/docker.sock"], {})):
            with patch("app.file_sandbox.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout=report().model_dump_json().encode())
                self.assertEqual(run_sandbox(AnalysisRequest(format="csv", action="outliers", chart=True), b"raw"), report())
        command = run.call_args.args[0]
        self.assertEqual(command[-1], IMAGE)
        for flag in ("--network=none", "--read-only", "--cap-drop=ALL", "--log-driver=none", "--pull=never"):
            self.assertIn(flag, command)
        self.assertFalse(any(flag.startswith(("--mount", "--volume")) or flag == "-v" for flag in command))
        header, body = run.call_args.kwargs["input"].split(b"\n", 1)
        self.assertEqual(json.loads(header), {"format": "csv", "action": "outliers", "chart": True})
        self.assertEqual(body, b"raw")

    def test_timeout_removes_only_the_launched_container(self):
        with patch("app.file_sandbox.local_docker", return_value=(["docker"], {})):
            with patch("app.file_sandbox.subprocess.run") as run:
                run.side_effect = [subprocess.TimeoutExpired("docker", 30), subprocess.CompletedProcess([], 0)]
                with self.assertRaisesRegex(SandboxError, "超时"):
                    run_sandbox(AnalysisRequest(format="csv", action="summary", chart=False), b"raw")
        command = run.call_args_list[0].args[0]
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "-f", command[command.index("--name") + 1]])


class FileSelectionProtocolTests(unittest.TestCase):
    def test_filter_protocol_bounds_and_types(self):
        from pydantic import ValidationError
        from sandbox.step7.protocol import Filter

        for operator, value in (
            ("eq", 2021), ("ne", "缺失"), ("in", [1, 2, 3]), ("between", [0, 2.5]),
            ("contains", "[.a]"), ("gt", -1.5), ("le", 100),
        ):
            self.assertEqual(Filter(column=0, operator=operator, value=value).value, value)
        for operator, value in (
            ("regex", ".*"), ("eq", True), ("gt", "2"), ("lt", 1e16),
            ("eq", float("inf")), ("between", [1, 0]), ("between", ["1", "2"]),
            ("between", [1]), ("in", 1), ("in", []), ("in", list(range(21))),
            ("contains", 1), ("contains", "  "), ("eq", "x" * 81),
        ):
            with self.subTest(operator=operator, value=value), self.assertRaises(ValidationError):
                Filter(column=0, operator=operator, value=value)
        with self.assertRaises(ValidationError):
            Filter(column=0, operator="eq", value=2021, expr="unsafe")

    def test_plan_limits_and_order_by(self):
        from pydantic import ValidationError
        from sandbox.step7.protocol import AggregatePlan, Filter, OrderBy

        fields = {"mode": "aggregate", "statistic": "sum", "columns": [1], "groups": [[0]]}
        self.assertTrue(AggregatePlan(**fields, limit=5).rank_problem())
        self.assertIsNone(AggregatePlan(**fields, order_by=OrderBy(column=1, direction="desc"), limit=100).rank_problem())
        for invalid in ({"limit": 0}, {"limit": 101}, {"limit": "2"},
                        {"filters": [Filter(column=0, operator="eq", value=1)] * 6}, {"code": "unsafe"}):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                AggregatePlan(**fields, **invalid)
