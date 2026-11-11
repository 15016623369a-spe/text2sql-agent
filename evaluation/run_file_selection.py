"""Check question-driven file filters and rankings against independent pandas calculations."""
from __future__ import annotations

import argparse
import io
import json
import math
import os
import subprocess
import unicodedata
from datetime import datetime
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

import pandas as pd

from app.config import Settings
from app.file_analysis import analyze_upload
from app.file_sandbox import local_docker
from app.telemetry import Trace
from sandbox.step7.protocol import FileLayout, TaskResult

PROJECT = Path(__file__).resolve().parent.parent
DATASET = PROJECT / "evaluation/splits/file_selection.json"
METRIC = "收入（元）"
SAMPLE = """年份,城市,省份,收入（元）
2020,Outside,North,70000
2021,One,West,"1,200"
2021,Two,West,9
2021,Three,North,25
2021,Literal[.]Name,South,7
2021,SimilarxName,South,7
2021,Missing,North,
2021,Low,North,2
2022,Later,West,9000
"""
SETTINGS = Settings("deepseek", "https://api.deepseek.com", "deepseek-v4-pro", "evaluation-mock", 0)
REFERENCE_WORKBOOK = r"""
import io,json,sys
import pandas as pd

with pd.ExcelFile(io.BytesIO(sys.stdin.buffer.read())) as xls:
    filled = xls.parse("填充数据", header=0)
    original = xls.parse("原始数据", header=0)
year = filled.loc[filled["时间"].eq(2021)]
population, revenue = filled.columns[3:5]
by_province = year.groupby("省份", sort=True)[revenue].sum(min_count=1).nlargest(10)
by_city = year.nlargest(5, population)
raw = original.loc[original["时间"].eq(2021), ["城市", revenue]]
assert len(year) == 322 and year["城市"].nunique() == 322 and len(filled) == 3642
assert not raw["城市"].duplicated().any()
both = year.merge(raw, on="城市", how="left", suffixes=("", "_原始"), validate="one_to_one")
quality = []
for region, province in both.loc[both["省份"].isin(["陕西省", "广东省"])].groupby("省份"):
    flagged = province[revenue + "_是否填充"].eq(True)
    from_raw = province[revenue + "_原始"]
    quality.append({
        "province": region, "rows": len(province),
        "workbook_total": float(province[revenue].sum()), "marked_imputed": int(flagged.sum()),
        "external_completion_flag": int(province[revenue + "_需外部补录"].eq(True).sum()),
        "original_valid": int(from_raw.notna().sum()), "original_total": float(from_raw.sum(min_count=1)) if from_raw.notna().any() else None,
        "imputed_total": float(province.loc[flagged, revenue].sum()),
    })
print(json.dumps({
    "total_rows": len(filled), "matched_rows": len(year), "annual_revenue": float(year[revenue].sum()),
    "province_rows": [[k, v] for k,v in by_province.items()],
    "city_rows": [[int(i)+1, row["城市"], row[population]] for i,row in by_city.iterrows()],
    "quality": quality,
}, ensure_ascii=False))
"""


def workbook_reference(raw: bytes, image: str) -> dict:
    engine, env = local_docker()
    result = subprocess.run(
        [*engine, "run", "--rm", "-i", "--network=none", "--read-only", "--cap-drop=ALL", "--pull=never",
         "--memory=512m", "--cpus=1", "--entrypoint", "python", image, "-c", REFERENCE_WORKBOOK],
        input=raw, capture_output=True, env=env, check=True, timeout=30,
    )
    return json.loads(result.stdout)


def plan_reply(case: dict) -> dict:
    plan = {
        "mode": "aggregate", "statistic": case.get("statistic", "sum"), "columns": [3],
        "groups": case.get("groups", []), "labels": case.get("labels", []),
        "filters": case["filters"], "chart": case.get("chart", False),
    }
    if "limit" in case:
        plan["limit"] = case["limit"]
        if not case.get("omit_order"):
            plan["order_by"] = {"column": 3, "direction": case.get("direction", "desc")}
    decision = {"kind": "run", "sheet": 0, "plan": plan}
    return {"choices": [{"message": {"content": json.dumps(decision, ensure_ascii=False)}}]}


def pandas_filter(frame: pd.DataFrame, conditions: list[dict]) -> pd.DataFrame:
    # Reference deliberately uses a separate per-cell implementation.
    def text(cell):
        return unicodedata.normalize("NFKC", str(cell)).casefold().strip()

    def match(cell, operator, value):
        if pd.isna(cell):
            return False
        if operator == "contains":
            return text(value) in text(cell)
        if operator == "in":
            return text(cell) in {text(item) for item in value}
        if isinstance(value, str):
            return (text(cell) == text(value)) == (operator == "eq")
        if operator == "between":
            return value[0] <= cell <= value[1]
        return {"eq": lambda: cell == value, "gt": lambda: cell > value, "ge": lambda: cell >= value,
                "lt": lambda: cell < value, "le": lambda: cell <= value}[operator]()

    keep = pd.Series(True, index=frame.index)
    for condition in conditions:
        column = frame.iloc[:, condition["column"]]
        keep &= column.map(lambda value: match(value, condition["operator"], condition["value"]))
    return frame.loc[keep]


def close_rows(actual: list[list], expected: list[list]):
    assert len(actual) == len(expected), (actual, expected)
    for left, right in zip(actual, expected, strict=True):
        assert left[:-1] == right[:-1], (left, right)
        assert math.isclose(left[-1], right[-1], rel_tol=1e-10, abs_tol=1e-8), (left, right)


def assert_chart(report: TaskResult):
    table = report.tables[0]
    figure = report.figure
    trace = figure["data"][0]
    assert len(trace["x"]) == len(table.rows)
    assert trace["y"] == [row[-1] for row in table.rows]
    assert figure["layout"]["xaxis"]["ticktext"] == [" / ".join(map(str, row[:-1])) for row in table.rows]
    assert "条" in figure["layout"]["title"]["text"]


def measure(filename, raw, question, settings=None, *, layout=None):
    trace = Trace("file_analysis_evaluation", lambda _: None)
    start = perf_counter()
    with trace.active():
        result = analyze_upload(filename, raw, question, settings, layout=layout)
    latency = (perf_counter() - start) * 1000
    observed = trace.finish("ok", None)
    return result, {
        "latency_ms": round(latency, 2), "model_calls": observed["model_calls"],
        "usage": observed["usage"],
    }


def check_workbook(workbook: Path, image: str, cases: list[dict]):
    raw = workbook.read_bytes()
    reference = workbook_reference(raw, image)
    results = []
    for case, key in zip(cases, ("province_rows", "city_rows"), strict=True):
        result, measurement = measure(workbook.name, raw, case["question"], layout=FileLayout(sheet=0))
        report = result.report
        assert result.kind == "file" and isinstance(report, TaskResult), result
        assert report.scope.matched_rows == reference["matched_rows"] == case["expected_rows"]
        assert report.scope.input_rows == reference["total_rows"]
        assert report.tables[0].limit == len(report.tables[0].rows) == case["limit"]
        close_rows(report.tables[0].rows, reference[key])
        if key == "province_rows":
            assert math.isclose(report.tables[1].rows[0][0], reference["annual_revenue"], abs_tol=1e-8)
        assert_chart(report)
        results.append({"id": case["id"], "passed": True, **measurement, "report": report.model_dump(mode="json", exclude={"figure"})})
    return results, reference


def check_synthetic(case: dict):
    csv = SAMPLE
    if case.get("extended"):
        csv += "".join(f"2021,City-{i},West,{i}\n" for i in range(40))
    with patch("app.file_planner.chat_completion", return_value=(200, plan_reply(case))) as model:
        result, measurement = measure("sample.csv", csv.encode(), case["question"], SETTINGS)
    assert model.call_count == 1, case["id"]
    if expected := case.get("clarification"):
        assert result.kind == "clarification" and expected in result.narrative, (case["id"], result)
        if case["id"] == "no_matches":
            frame = pd.read_csv(io.StringIO(csv), header=0)
            assert pandas_filter(frame, case["filters"]).empty
        return {"id": case["id"], "passed": True, "planner": "mock", **measurement, "message": result.narrative}
    frame = pd.read_csv(io.StringIO(csv), header=0)
    frame[METRIC] = pd.to_numeric(frame[METRIC].astype("string").str.replace(",", ""), errors="coerce")
    filtered = pandas_filter(frame, case["filters"])
    report = result.report
    assert result.kind == "file" and isinstance(report, TaskResult), (case["id"], result)
    assert report.scope.matched_rows == len(filtered)
    assert report.scope.input_rows == len(frame)
    if case.get("statistic") == "records":
        sorted_rows = filtered.dropna(subset=[METRIC]).sort_values(
            METRIC, ascending=case.get("direction") == "asc", kind="stable",
        )
        expected = [[int(i)+1, row["城市"], row[METRIC]] for i, row in sorted_rows.head(case["limit"]).iterrows()]
    elif case.get("groups"):
        grouped = filtered.groupby("省份")[METRIC].sum(min_count=1).nlargest(case["limit"])
        expected = [[key, value] for key, value in grouped.items()]
        assert math.isclose(report.tables[1].rows[0][0], filtered[METRIC].sum(), abs_tol=1e-8)
    else:
        expected = [[filtered[METRIC].sum()]]
    close_rows(report.tables[0].rows, expected)
    if case.get("chart"):
        assert_chart(report)
    return {"id": case["id"], "passed": True, "planner": "mock", **measurement,
            "report": report.model_dump(mode="json", exclude={"figure"})}


def check_coverage():
    csv = (
        "年份,城市,总量\n2013,A,1\n2014,A,10\n2014,B,20\n2015,A,12\n2015,C,5\n2016,A,90\n"
    ).encode()
    result, measurement = measure("sample.csv", csv, "只比较 2014 到 2015 年的城市覆盖变化")
    report = result.report
    assert result.kind == "file" and isinstance(report, TaskResult)
    frame = pd.read_csv(io.BytesIO(csv), header=0)
    selected = frame.loc[frame["年份"].between(2014, 2015)]
    assert report.scope.matched_rows == len(selected)
    assert {row[0] for row in report.tables[0].rows} == {2014, 2015}
    assert report.tables[2].rows[0][4:6] == selected.groupby("年份")["总量"].sum().tolist()
    return {"id": "filtered_coverage", "passed": True, **measurement,
            "report": report.model_dump(mode="json", exclude={"figure"})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workbook", type=Path, help="Original optional Excel regression file, processed locally")
    parser.add_argument("--image", default="rag-step7-sandbox:1")
    parser.add_argument("--baseline", type=Path, help="Optional pre-change regression results")
    parser.add_argument("--output", type=Path, default=PROJECT / "evaluation/results")
    args = parser.parse_args()
    cases = json.loads(DATASET.read_text())
    results, reference = [], None
    with patch.dict(os.environ, {"RAG_SANDBOX_IMAGE": args.image, "RAG_SANDBOX_SOCKET": ""}):
        for case in cases["synthetic"]:
            results.append(check_synthetic(case))
        results.append(check_coverage())
        if args.workbook:
            workbook_results, reference = check_workbook(args.workbook, args.image, cases["original"])
            results.extend(workbook_results)
    out = args.output / datetime.now().strftime("file-selection-%Y%m%d-%H%M%S-%f")
    out.mkdir(parents=True)
    run = {
        "sandbox_image": args.image, "passed": len(results), "results": results, "workbook_reference": reference,
        "baseline": json.loads(args.baseline.read_text()) if args.baseline else None,
    }
    (out / "results.json").write_text(json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(f"{len(results)}/{len(results)} passed · {out / 'results.json'}")


if __name__ == "__main__":
    main()
