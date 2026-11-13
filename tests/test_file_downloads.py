import csv
import io
import unittest
from importlib.util import find_spec

from app.file_downloads import csv_table, table_rows, xlsx_table
from sandbox.step7.protocol import ResultTable


class FileDownloadsTests(unittest.TestCase):
    def test_csv_keeps_rows_beyond_preview_in_order(self):
        rows = [[f"城市{index:03}", index] for index in range(200)]
        table = ResultTable.from_rows("按城市", ["城市", "人口"], rows)
        self.assertEqual(len(table.rows), 180)
        full = table_rows(table.model_dump())
        self.assertEqual(full, rows)
        downloaded = list(csv.reader(io.StringIO(csv_table(table.columns, full).decode("utf-8-sig"))))
        self.assertEqual(downloaded, [table.columns, *[[name, str(value)] for name, value in rows]])
        table.additional_rows = None
        self.assertIsNone(table_rows(table.model_dump()))

    def test_top_n_exports_only_requested_rows_and_csv_protects_text(self):
        table = ResultTable.from_rows("排名", ["=标题", "指标"], [["=1+1", 3], ["普通值", 2]], limit=1)
        self.assertEqual(table_rows(table.model_dump()), [["=1+1", 3]])
        downloaded = list(csv.reader(io.StringIO(csv_table(table.columns, table.rows).decode("utf-8-sig"))))
        self.assertEqual(downloaded, [["'=标题", "指标"], ["'=1+1", "3"]])

    @unittest.skipUnless(find_spec("openpyxl"), "requires requirements-step7.txt")
    def test_excel_preserves_strings_as_text_and_numbers_as_numbers(self):
        from openpyxl import load_workbook

        data = xlsx_table("地区/分组", ["=标题", "人口"], [["=1+1", 12], ["华南", 17.5]])
        book = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
        try:
            rows = list(book.active.iter_rows())
            self.assertEqual([(row[0].value, row[0].data_type) for row in rows],
                             [("=标题", "s"), ("=1+1", "s"), ("华南", "s")])
            self.assertEqual([row[1].value for row in rows], ["人口", 12, 17.5])
        finally:
            book.close()


if __name__ == "__main__":
    unittest.main()
