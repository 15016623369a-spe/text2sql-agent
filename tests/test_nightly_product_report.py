import importlib.util
import io
import json
import shutil
import tempfile
import unittest
from collections import Counter
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location("nightly_report", _ROOT / ".github" / "scripts" / "nightly_report.py")
nightly_report = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nightly_report)


class NightlyProductReportTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.ids = json.loads((_ROOT / "evaluation" / "splits" / "dev_ids.json").read_text())
        self.run_number = 0
        self.run_metadata = {
            "version": "product", "split": "dev", "full_split": True, "question_ids": self.ids,
            "dataset_sha256": "data", "split_sha256": "dev", "model": "model", "provider": "provider",
            "provider_host": "example.invalid", "temperature": 0, "execution_metric": "set equality",
            "uses_official_evidence": True, "sql_timeout_seconds": 5, "model_timeout_seconds": 30,
            "max_output_tokens": 1024, "max_result_rows": 100000, "observation_max_bytes": 33554432, "workers": 3,
            "schema_linking": {"version": "linker", "top_k": 4, "dense": None, "secret": "sensitive schema"},
        }
        self.output = self.root / "artifact"
        self.output.mkdir()

    def write_run(self, correct, p95=2.0, **metadata):
        self.run_number += 1
        target = self.root / f"run-{self.run_number}"
        target.mkdir()
        (target / "run.json").write_text(json.dumps({
            **self.run_metadata, "run_id": str(self.run_number), **metadata, "private": "sensitive metadata",
        }))
        records = [{
            "question_id": key, "ex": int(index < correct), "status": "correct" if index < correct else "mismatch",
            "latency_seconds": p95 if index else p95 * self.run_number / (self.run_number + 1),
            "sql": "sensitive SQL", "reference_rows": ["sensitive rows"],
        } for index, key in enumerate(self.ids)]
        (target / "summary.json").write_text(json.dumps({
            "benchmark_valid": True, "questions": 50, "correct": correct, "p95_latency_seconds": p95,
            "statuses": dict(Counter(row["status"] for row in records)),
            "token_totals": {"total_tokens": 10, "secret": "sensitive tokens"},
            "private": "sensitive summary", "by_database": {"fixture": {"correct": 1, "secret": "sensitive group"}},
        }))
        (target / "questions.jsonl").write_text("\n".join(json.dumps(row) for row in records))
        return target

    def write_batch(self, scores=(25, 25, 25), latencies=(2.0, 2.0, 2.0), **metadata):
        return [self.write_run(score, p95, **metadata) for score, p95 in zip(scores, latencies, strict=True)]

    def invoke(self, batch, *arguments):
        paths = [argument for path in batch for argument in ("--bird-dir", str(path))]
        with patch("sys.argv", ["nightly_report", *paths, *arguments]), redirect_stdout(io.StringIO()):
            return nightly_report.main()

    def freeze(self, name, batch):
        reference = self.root / name
        self.assertEqual(self.invoke(batch, "--write-baseline", str(reference)), 0)
        return reference

    def report(self, batch, *, previous=(), baseline=None, golden=30, turn_latency_ms=100):
        (self.output / "conversations.json").write_text(json.dumps({
            "transport": "live", "scenarios": 30, "passed": golden, "model_calls": 1,
            "cases": [{"passed": True, "turns": [{"latency_ms": turn_latency_ms}]} for _ in range(30)],
        }))
        args = ["--output-dir", str(self.output)]
        for candidate in previous:
            args.extend(("--previous", str(candidate)))
        if baseline:
            args.extend(("--baseline", str(baseline)))
        status = self.invoke(batch, *args)
        return status, json.loads((self.output / "bird.json").read_text())

    def test_comparison_allows_prompt_change_and_rejects_different_model(self):
        previous = self.freeze("prior.json", self.write_batch((23, 25, 27), system_prompt_sha256="old"))
        current = self.write_batch((24, 32, 23), system_prompt_sha256="new")
        status, report = self.report(current, baseline=previous)
        self.assertEqual(status, 0)
        self.assertEqual(report["summary"]["correct_median"], 24)
        self.assertEqual(report["summary"]["correct_range"], [23, 32])
        self.assertEqual(report["baseline_comparison"]["correct_change"], -1)
        self.assertEqual(report["baseline_comparison"]["lost"], [self.ids[24]])
        self.assertEqual(self.report(self.write_batch(model="other"), baseline=previous)[0], 1)
        self.assertEqual(self.report(self.write_batch(workers=5), baseline=previous)[0], 1)

    def test_frozen_baseline_and_previous_each_fail_at_five_lost_questions(self):
        low = self.freeze("low.json", self.write_batch((24, 25, 26)))
        high = self.freeze("high.json", self.write_batch((28, 29, 30)))
        at_limit = self.write_batch((14, 25, 33))
        self.assertEqual(self.report(at_limit, previous=(high,), baseline=low)[0], 0)
        over_limit = self.write_batch((13, 24, 34))
        for previous, baseline in ((low, high), (high, low)):
            with self.subTest(previous=previous, baseline=baseline):
                self.assertEqual(self.report(over_limit, previous=(previous,), baseline=baseline)[0], 1)

    def test_previous_uses_first_available_compatible_report(self):
        unavailable = self.root / "missing.json"
        incompatible = self.freeze("incompatible.json", self.write_batch(model="old-model"))
        recent = self.freeze("recent.json", self.write_batch((29, 30, 31)))
        older = self.freeze("older.json", self.write_batch((23, 24, 25)))
        current = self.write_batch((24, 25, 26))
        status, report = self.report(current, previous=(unavailable, incompatible, recent, older))
        self.assertEqual(status, 1)
        self.assertEqual(report["comparison"]["correct_change"], -5)
        status, report = self.report(current, previous=(unavailable, incompatible, older))
        self.assertEqual(status, 0)
        self.assertEqual(report["comparison"]["correct_change"], 1)

    def test_bird_p95_uses_median_and_relative_limits(self):
        reference = self.freeze("prior.json", self.write_batch(latencies=(3, 3, 3)))
        at_limit = self.write_batch(latencies=(3.7, 3.75, 10))
        status, report = self.report(at_limit, baseline=reference)
        self.assertEqual(status, 0)
        self.assertEqual(report["summary"]["p95_latency_seconds_median"], 3.75)
        self.assertEqual(report["summary"]["p95_latency_seconds_range"], [3.7, 10])
        over_relative = self.write_batch(latencies=(2, 3.76, 4))
        self.assertEqual(self.report(over_relative, previous=(reference,))[0], 1)
        self.assertEqual(self.report(over_relative, baseline=reference)[0], 1)
        fast_reference = self.freeze("fast.json", self.write_batch(latencies=(0.5, 0.5, 0.5)))
        self.assertEqual(self.report(self.write_batch(latencies=(0.9, 0.95, 1.2)), baseline=fast_reference)[0], 0)

    def test_conversation_p95_applies_absolute_limit_and_requires_timings(self):
        batch = self.write_batch()
        self.assertEqual(self.report(batch, turn_latency_ms=6000)[0], 0)
        status, report = self.report(batch, turn_latency_ms=6010)
        self.assertEqual(status, 1)
        self.assertEqual(report["conversation_p95_latency_seconds"], 6.01)
        self.assertTrue(any("6 s target" in failure for failure in report["gate_failures"]))
        self.assertEqual(self.report(batch, turn_latency_ms=None)[0], 1)

    def test_legacy_single_run_is_unavailable_as_prior_and_invalid_as_baseline(self):
        old = self.root / "single.json"
        old.write_text(json.dumps({"valid": True, **nightly_report.bird_summary(self.write_run(30))}))
        current = self.write_batch()
        status, report = self.report(current, previous=(old,))
        self.assertEqual(status, 0)
        self.assertIsNone(report["comparison"])
        self.assertEqual(self.report(current, baseline=old)[0], 1)
        old.write_text("{}")
        self.assertEqual(self.report(current, baseline=old)[0], 1)

    def test_missing_duplicate_and_incompatible_evidence_fails(self):
        one, two, three = self.write_batch()
        duplicate = self.root / "copy"
        shutil.copytree(one, duplicate)
        other = self.write_run(25, model="other")
        ablation = self.write_run(25, profile="no_semantics")
        for batch in ([one, two], [one, two, self.root / "missing"], [one, two, two], [one, two, duplicate],
                      [one, two, other], [one, two, ablation]):
            with self.subTest(batch=batch):
                status, report = self.report(batch)
                self.assertEqual(status, 1)
                self.assertFalse(report["valid"])
        self.assertEqual(self.report([one, two, three])[0], 0)

    def test_forged_latency_summary_and_infrastructure_failures_are_invalid(self):
        batch = self.write_batch()
        path = batch[0] / "summary.json"
        valid = json.loads(path.read_text())
        for value in (float("nan"), -1, 0.1):
            with self.subTest(value=value):
                path.write_text(json.dumps({**valid, "p95_latency_seconds": value}))
                self.assertEqual(self.report(batch)[0], 1)
        path.write_text(json.dumps(valid))
        questions = batch[0] / "questions.jsonl"
        rows = [json.loads(row) for row in questions.read_text().splitlines()]
        rows[-1]["status"] = "model_request_error"
        questions.write_text("\n".join(json.dumps(row) for row in rows))
        self.assertEqual(self.report(batch)[0], 1)
        self.assertIn("model_request_error", (self.output / "summary.md").read_text())

    def test_golden_gate_and_sanitized_public_artifact(self):
        current = self.write_batch()
        reference = self.freeze("prior.json", current)
        self.assertEqual(self.report(current, baseline=reference, golden=29)[0], 1)
        self.assertEqual(self.report(current, baseline=reference)[0], 0)
        for path in (reference, self.output / "bird.json"):
            self.assertNotIn("sensitive", path.read_text())
