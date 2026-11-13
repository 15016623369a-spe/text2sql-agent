"""Exercise workbook layout with the actual file worker dependencies."""
import io
import sys
import unittest
from importlib.util import find_spec
from pathlib import Path

_AVAILABLE = all(find_spec(module) for module in ("pandas", "openpyxl", "xlrd", "defusedxml"))
if _AVAILABLE:
    worker = Path(__file__).resolve().parents[1] / "sandbox" / "step7"
    if worker.is_dir():
        sys.path.insert(0, str(worker))
    try:
        from pandas import isna
        from openpyxl import Workbook
        from protocol import FileLayout
        from tabular import HeaderLayoutError, inspect_layout, load_frame
    finally:
        if worker.is_dir():
            sys.path.pop(0)


def _xlsx(rows, *, merges=(), other_sheets=()):
    book = Workbook()
    sheet = book.active
    sheet.title = "主表"
    for row in rows:
        sheet.append(row)
    for address in merges:
        sheet.merge_cells(address)
    for name, data in other_sheets:
        other = book.create_sheet(name)
        for row in data:
            other.append(row)
    with io.BytesIO() as out:
        book.save(out)
        return out.getvalue()


@unittest.skipUnless(_AVAILABLE, "requires sandbox/step7 parser dependencies")
class TabularLayoutTests(unittest.TestCase):
    def test_title_with_unit_metadata_is_not_a_column_name(self):
        data = "2021年城市人口统计表,单位：万人\n城市,人口\n杭州,1220\n宁波,954\n".encode()
        preview = inspect_layout(data, "csv", FileLayout(), preview=True).sheets[0]
        self.assertEqual((preview.header_row, preview.header_depth), (2, 1))
        self.assertEqual(preview.columns, ["城市", "人口"])
        self.assertEqual(preview.rows[0], ["2021年城市人口统计表", "单位：万人"])
        frame, header = load_frame(data, "csv", FileLayout())
        self.assertEqual(header.row, 2)
        self.assertEqual(frame["人口"].tolist(), ["1220", "954"])

    def test_two_level_excel_header_never_turns_column_labels_into_data(self):
        book = Workbook()
        sheet = book.active
        sheet.append(["地区信息", None, "指标", None])
        sheet.merge_cells("A1:B1")
        sheet.merge_cells("C1:D1")
        sheet.append(["省份", "城市", "人口", "面积"])
        sheet.append(["浙江", "杭州", 1220, 16850])
        sheet.append([None, "宁波", 954, 9816])
        sheet.merge_cells("A3:A4")
        sheet.append(["江苏", "南京", 949, 6587])
        raw = io.BytesIO()
        book.save(raw)

        data = raw.getvalue()
        preview = inspect_layout(data, "xlsx", FileLayout(), preview=True).sheets[0]
        self.assertEqual((preview.header_row, preview.header_depth), (1, 2))
        self.assertEqual(preview.columns, ["地区信息 / 省份", "地区信息 / 城市", "指标 / 人口", "指标 / 面积"])
        self.assertTrue(any("表头合并单元格" in warning for warning in preview.warnings))
        self.assertTrue(any("数据区合并单元格" in warning for warning in preview.warnings))
        self.assertIsNone(preview.issue)
        frame, header = load_frame(data, "xlsx", FileLayout())
        self.assertEqual(header.depth, 2)
        self.assertEqual(frame["指标 / 人口"].tolist(), [1220, 954, 949])
        self.assertTrue(frame["地区信息 / 省份"].isna().iloc[1])

        one_level = FileLayout(header_row=1, header_depth=1)
        schema = inspect_layout(data, "xlsx", one_level, preview=False).sheets[0]
        self.assertIn("第 2 行仍是列名", schema.issue)
        with self.assertRaisesRegex(HeaderLayoutError, "第 2 行仍是列名"):
            load_frame(data, "xlsx", one_level)
        manual, _ = load_frame(data, "xlsx", FileLayout(header_row=2, header_depth=1))
        self.assertEqual(manual["人口"].tolist(), [1220, 954, 949])

    def test_sheet_preview_distinguishes_unrequested_sample_from_empty_sheet(self):
        book = Workbook()
        summary = book.active
        summary.title = "概览"
        summary.append(["地区", None])
        summary.merge_cells("A1:B1")
        summary.append(["省份", "人口"])
        summary.append(["浙江", 1220])
        details = book.create_sheet("详情")
        details.append(["省份", "人口"])
        details.append(["江苏", 949])
        details[100][0].value = "浙江"
        details.merge_cells("A100:A101")
        book.create_sheet("空表")
        raw = io.BytesIO()
        book.save(raw)

        data = raw.getvalue()
        first, second, empty = inspect_layout(data, "xlsx", FileLayout(), preview=True).sheets
        self.assertTrue(first.sample_included)
        self.assertTrue(first.rows)
        self.assertTrue(any("表头合并单元格" in warning for warning in first.warnings))
        self.assertFalse(any("数据区合并单元格" in warning for warning in first.warnings))
        self.assertEqual(second.columns, ["省份", "人口"])
        self.assertFalse(second.sample_included)
        self.assertEqual(second.rows, [])
        self.assertFalse(empty.sample_included)

        selected = inspect_layout(data, "xlsx", FileLayout(sheet=1), preview=True).sheets[0]
        self.assertTrue(selected.sample_included)
        self.assertEqual(selected.rows[1], ["江苏", 949])
        self.assertTrue(any("数据区合并单元格" in warning for warning in selected.warnings))
        self.assertFalse(any("表头合并单元格" in warning for warning in selected.warnings))
        selected_empty = inspect_layout(data, "xlsx", FileLayout(sheet=2), preview=True).sheets[0]
        self.assertTrue(selected_empty.sample_included)
        self.assertEqual(selected_empty.rows, [])
        self.assertIsNotNone(selected_empty.issue)

    def test_ambiguous_csv_header_requires_explicit_layout(self):
        data = "地区信息,,指标,\n省份,城市,人口,面积\n浙江,杭州,1220,16850\n".encode()
        preview = inspect_layout(data, "csv", FileLayout(), preview=True).sheets[0]
        self.assertEqual(preview.header_depth, 2)
        self.assertTrue(preview.needs_confirmation)
        self.assertIn("确认表头", preview.issue)
        with self.assertRaises(HeaderLayoutError):
            load_frame(data, "csv", FileLayout())
        frame, _ = load_frame(data, "csv", FileLayout(header_row=1, header_depth=2))
        self.assertEqual(frame["地区信息 / 城市"].tolist(), ["杭州"])

    def test_twelve_named_layout_fixtures_load_the_expected_columns_and_rows(self):
        simple = [["城市", "人口"], ["杭州", 1220], ["宁波", 954]]
        grouped = [["地区", None, "指标", None], ["省份", "城市", "人口", "面积"], ["浙江", "杭州", 1220, 16850]]
        headers = ["地区 / 省份", "地区 / 城市", "指标 / 人口", "指标 / 面积"]
        cases = (
            ("csv-title-and-units", "csv", "人口统计表,单位：万人\n城市,人口\n杭州,1220\n".encode(),
             FileLayout(), (2, 1), ["城市", "人口"], [("杭州", "1220")]),
            ("csv-leading-blank-lines", "csv", b"\n\nCity,Population\nHangzhou,1220\n",
             FileLayout(), (3, 1), ["City", "Population"], [("Hangzhou", "1220")]),
            ("csv-gb18030", "csv", "城市,人口\n宁波,954\n".encode("gb18030"),
             FileLayout(), (1, 1), ["城市", "人口"], [("宁波", "954")]),
            ("csv-quoted-value-with-comma", "csv", b'Office,Amount\n"North, Hub",120\n',
             FileLayout(), (1, 1), ["Office", "Amount"], [("North, Hub", "120")]),
            ("csv-empty-and-duplicate-column-labels", "csv", b"City,Revenue,Revenue,\nNorth,12,15,18\n",
             FileLayout(), (1, 1), ["City", "Revenue", "Revenue.1", "列 4"], [("North", "12", "15", "18")]),
            ("csv-repeated-parents-explicit", "csv", "地区,地区,指标,指标\n省份,城市,人口,面积\n浙江,杭州,1220,16850\n".encode(),
             FileLayout(header_row=1, header_depth=2), (1, 2), headers, [("浙江", "杭州", "1220", "16850")]),
            ("xlsx-merged-title-and-two-level-header", "xlsx", _xlsx(
                [["人口统计表", None, None, None], *grouped], merges=("A1:D1", "A2:B2", "C2:D2")),
             FileLayout(), (2, 2), headers, [("浙江", "杭州", 1220, 16850)]),
            ("xlsx-vertical-and-horizontal-header-merges", "xlsx", _xlsx(
                [["城市", "指标", None], [None, "人口", "面积"], ["杭州", 1220, 16850]], merges=("A1:A2", "B1:C1")),
             FileLayout(), (1, 2), ["城市", "指标 / 人口", "指标 / 面积"], [("杭州", 1220, 16850)]),
            ("xlsx-three-header-levels", "xlsx", _xlsx(
                [["统计", None, None, None], ["地区", None, "指标", None], *grouped[1:]],
                merges=("A1:D1", "A2:B2", "C2:D2")),
             FileLayout(), (1, 3), [f"统计 / {column}" for column in headers], [("浙江", "杭州", 1220, 16850)]),
            ("xlsx-data-region-merge-keeps-missing", "xlsx", _xlsx(
                [["省份", "城市", "人口"], ["浙江", "杭州", 1220], [None, "宁波", 954]], merges=("A2:A3",)),
             FileLayout(), (1, 1), ["省份", "城市", "人口"], [("浙江", "杭州", 1220), (None, "宁波", 954)]),
            ("xlsx-explicit-nonfirst-sheet", "xlsx", _xlsx(simple, other_sheets=(
                ("明细", [["地区", "营收"], ["华南", 175]]),)),
             FileLayout(sheet=1), (1, 1), ["地区", "营收"], [("华南", 175)]),
            ("xlsx-manual-header-beyond-preview", "xlsx", _xlsx([[None]] * 14 + simple),
             FileLayout(header_row=15, header_depth=1), (15, 1), ["城市", "人口"], [("杭州", 1220), ("宁波", 954)]),
        )
        for name, file_format, data, layout, header, columns, rows in cases:
            with self.subTest(fixture=name):
                preview = inspect_layout(data, file_format, layout, preview=True).sheets[0]
                schema = inspect_layout(data, file_format, layout, preview=False).sheets[0]
                for inspected in (preview, schema):
                    self.assertIsNone(inspected.issue)
                    self.assertEqual(inspected.index, layout.sheet or 0)
                    self.assertEqual((inspected.header_row, inspected.header_depth), header)
                    self.assertEqual(inspected.columns, columns)
                frame, selected = load_frame(data, file_format, layout)
                self.assertEqual((selected.row, selected.depth), header)
                self.assertEqual(list(frame.columns), columns)
                observed = [tuple(None if isna(value) else value for value in row)
                            for row in frame.itertuples(index=False, name=None)]
                self.assertEqual(observed, rows)

    def test_eight_named_layout_fixtures_reject_ambiguous_or_invalid_input(self):
        simple = _xlsx([["城市", "人口"], ["杭州", 1220]], other_sheets=(("空表", []),))
        merged = _xlsx([["地区", None], ["城市", "省份"], ["杭州", "浙江"], ["南京", "江苏"]],
                       merges=("A1:B1",))
        cases = (
            ("csv-ambiguous-parent-labels", "csv", "地区,,指标,\n省份,城市,人口,面积\n浙江,杭州,1220,16850\n".encode(),
             FileLayout(), "确认表头", True),
            ("xlsx-ambiguous-year-labels", "xlsx", _xlsx(
                [["城市", "人口", None], [None, 2020, 2021], ["杭州", 1190, 1220]], merges=("B1:C1",)),
             FileLayout(), "确认表头", True),
            ("csv-explicit-title-as-header", "csv", "人口统计表,单位：万人\n城市,人口\n杭州,1220\n".encode(),
             FileLayout(header_row=1, header_depth=1), "标题或单位说明", True),
            ("xlsx-depth-would-include-column-labels-as-data", "xlsx", _xlsx(
                [["地区", None], ["城市", "人口"], ["杭州", 1220]], merges=("A1:B1",)),
             FileLayout(header_row=1, header_depth=1), "第 2 行仍是列名", True),
            ("xlsx-selected-empty-sheet", "xlsx", simple, FileLayout(sheet=1), "表头为空或超出", True),
            ("xlsx-explicit-header-past-used-range", "xlsx", merged,
             FileLayout(sheet=0, header_row=9, header_depth=1), "表头为空或超出", True),
            ("xlsx-sheet-index-out-of-range", "xlsx", simple, FileLayout(sheet=2), "工作表不存在", False),
            ("csv-sheet-index-out-of-range", "csv", b"City,Population\nHangzhou,1220\n",
             FileLayout(sheet=1), "CSV 只有一个工作表", False),
        )
        for name, file_format, data, layout, expected, preview_available in cases:
            with self.subTest(fixture=name):
                if preview_available:
                    preview = inspect_layout(data, file_format, layout, preview=True).sheets[0]
                    self.assertIn(expected, preview.issue or "")
                else:
                    with self.assertRaisesRegex(ValueError, expected):
                        inspect_layout(data, file_format, layout, preview=True)
                with self.assertRaisesRegex(ValueError, expected):
                    load_frame(data, file_format, layout)


if __name__ == "__main__":
    unittest.main()
