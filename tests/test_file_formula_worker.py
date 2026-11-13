"""Check audit arithmetic and display precision in the worker runtime."""
import io
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path

import pandas as pd

_worker = Path(__file__).resolve().parents[1] / "sandbox" / "step7"
if _worker.is_dir():
    sys.path.insert(0, str(_worker))
try:
    from audit import audit_frame, workbook_tolerances
    from protocol import AuditPlan
finally:
    if _worker.is_dir():
        sys.path.pop(0)


def density_plan(tolerance=None):
    return AuditPlan.model_validate({
        "mode": "audit", "labels": [0], "tolerance": tolerance,
        "checks": [{
            "target": 3, "unit": "人/平方公里", "basis": "explicit",
            "input_units": [{"column": 1, "unit": "万人"}, {"column": 2, "unit": "平方公里"}],
            "formula": {
                "kind": "divide", "right": {"kind": "column", "column": 2},
                "left": {
                    "kind": "multiply", "left": {"kind": "column", "column": 1},
                    "right": {"kind": "number", "value": 10000.0},
                },
            },
        }],
    })


def summary(result):
    table = result.tables[0]
    return dict(zip(table.columns, table.rows[0], strict=True))


class FormulaWorkerTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame([
            ("甲", 1, 333, "30.0"), ("乙", 1, 141, "70.9"), ("丙", 1, 305, "32.8"),
            ("丁", 1, 500, "20.0"), ("测试偏差", 1, 500, "10000.0"),
        ], columns=["城市", "人口(万人)", "面积(平方公里)", "人口密度(人/平方公里)"])

    def test_default_tolerance_distinguishes_rounding_and_substantive_error(self):
        result = audit_frame(self.frame, density_plan())
        info = summary(result)
        self.assertEqual(info["可核对行数"], 5)
        self.assertEqual(info["实质超差行数"], 1)
        self.assertEqual(info["舍入级差异行数"], 3)
        self.assertEqual(info["最大绝对偏差"], 9980)
        self.assertEqual(result.tables[1].rows[0][:2], [6, "测试偏差"])
        self.assertEqual(result.tables[1].rows[0][-1], 0.05)

        strict = audit_frame(self.frame, density_plan(tolerance=0.01))
        info = summary(strict)
        self.assertEqual(info["超出容差行数"], 4)
        self.assertEqual(info["实质超差行数"], 1)
        self.assertEqual(info["舍入级差异行数"], 3)
        self.assertIn("3 行舍入级差异超出指定容差", strict.message)
        self.assertEqual(len(strict.tables[1].rows), 4)

    @unittest.skipUnless(find_spec("openpyxl"), "requires sandbox/step7 Excel parser")
    def test_xlsx_precision_uses_selected_sheet_cell_formats_and_original_rows(self):
        from openpyxl import Workbook

        workbook = Workbook()
        workbook.active.append(["其他表"])
        sheet = workbook.create_sheet("密度")
        sheet.append(["人口密度表"])
        sheet.append(list(self.frame.columns))
        sheet.append(["一位小数", 1, 333, 30])
        sheet.append([])
        sheet.append(["两位小数", 1, 333, 30])
        sheet["D3"].number_format = "0.0"
        sheet["D5"].number_format = "0.00"
        raw = io.BytesIO()
        workbook.save(raw)
        tolerances = workbook_tolerances(raw.getvalue(), "xlsx", 1, {3}, data_offset=2, rows=3)
        self.assertEqual(tolerances, {3: {0: 0.05, 2: 0.005}})
        frame = pd.DataFrame([
            ("一位小数", 1, 333, 30), ("两位小数", 1, 333, 30),
        ], columns=self.frame.columns, index=[0, 2])
        result = audit_frame(frame, density_plan(), data_offset=2, display_tolerances=tolerances)
        self.assertEqual(summary(result)["舍入级差异行数"], 1)
        self.assertEqual(summary(result)["实质超差行数"], 1)
        self.assertEqual(result.tables[1].rows[0][:2], [5, "两位小数"])


if __name__ == "__main__":
    unittest.main()
