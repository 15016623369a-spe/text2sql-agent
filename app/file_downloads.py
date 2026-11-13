"""Export the selected result rows without re-running the analysis."""
import io
import re

from app.query_export import csv_result


def table_rows(table: dict) -> list[list] | None:
    rows = [*table["rows"], *(table.get("additional_rows") or [])]
    total = table["total_groups"]
    expected = min(total, table["limit"]) if table.get("limit") is not None else total
    return rows if len(rows) == expected else None


def csv_table(columns: list[str], rows: list[list]) -> bytes:
    data = csv_result(columns, rows)
    if data is None:
        raise ValueError("CSV 超过导出上限")
    return data


def xlsx_table(title: str, columns: list[str], rows: list[list]) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet(re.sub(r"[\\/:?*\[\]]", " ", title).strip()[:31] or "结果")
    for row in (columns, *rows):
        cells = []
        for value in row:
            cell = WriteOnlyCell(sheet, value=value)
            if isinstance(value, str):
                cell.data_type = "s"
            cells.append(cell)
        sheet.append(cells)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()
