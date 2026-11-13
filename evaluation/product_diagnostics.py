"""Triage result mismatches from local product benchmark logs for review."""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

_LITERAL = re.compile(r"'(?:''|[^'])*'")
_SELECT = re.compile(r"\bSELECT\b(.*?)\bFROM\b", re.I | re.S)
_ID = re.compile(r"\b(?:\w+_)?(?:id|code|cds(?:code)?)\b", re.I)
_NAME = re.compile(r"\b(?:\w+_)?(?:name|title|school|district|first|last)\b", re.I)
_DATE = re.compile(r"\b(?:strftime|date|datetime|year|month|substr|julianday)\b|\b\d{4}[-/]\d{2}", re.I)
_TOP = re.compile(r"\b(?:ORDER\s+BY|LIMIT|DENSE_RANK|ROW_NUMBER)\b", re.I)
_GRAIN = re.compile(r"\b(?:JOIN|DISTINCT|GROUP\s+BY)\b", re.I)
_FILTER = re.compile(r"\b(?:WHERE|HAVING|IS\s+NOT\s+NULL|BETWEEN|EXISTS|IN\s*\()", re.I)


def _terms(pattern: re.Pattern, sql: str) -> Counter:
    return Counter(" ".join(match[0].upper().split()) for match in pattern.finditer(_LITERAL.sub("''", sql)))


def mismatch_signals(record: dict) -> list[str]:
    """Return evidence for triage. Signals may overlap and are not correctness rules."""
    sql, gold = record.get("sql") or "", record.get("reference_sql") or ""
    if not sql or not gold:
        return []
    projection = _SELECT.search(sql)
    reference_projection = _SELECT.search(gold)
    left = projection[1] if projection else ""
    right = reference_projection[1] if reference_projection else ""
    signals = []
    if len(record.get("prediction_columns") or []) != len(record.get("reference_columns") or []):
        signals.append("projection_width")
    if (_ID.search(left) and _NAME.search(right)) or (_ID.search(right) and _NAME.search(left)):
        signals.append("id_or_label")
    if (_DATE.search(sql) or _DATE.search(gold)) and _terms(_DATE, sql) != _terms(_DATE, gold):
        signals.append("date_or_encoding")
    if _terms(_TOP, sql) != _terms(_TOP, gold) and _TOP.search(gold):
        signals.append("topn_or_order")
    if _terms(_GRAIN, sql) != _terms(_GRAIN, gold):
        signals.append("join_or_grain")
    if _terms(_FILTER, gold) - _terms(_FILTER, sql) or set(_LITERAL.findall(gold)) - set(_LITERAL.findall(sql)):
        signals.append("predicate_or_value")
    return signals or ["manual_review"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    run = json.loads((args.run_dir / "run.json").read_text(encoding="utf-8"))
    if run.get("split") != "dev":
        parser.error("Triage uses the development split")
    with (args.run_dir / "questions.jsonl").open(encoding="utf-8") as log:
        records = [json.loads(line) for line in log if line.strip()]
    failures = [{
        "question_id": record["question_id"], "db_id": record["db_id"], "status": record["status"],
        "signals": mismatch_signals(record) if record["status"] == "mismatch" else [record["status"]],
    } for record in records if not record["ex"]]
    report = {"signal_counts": dict(Counter(signal for row in failures for signal in row["signals"])), "failures": failures}
    (args.run_dir / "diagnostics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["signal_counts"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
