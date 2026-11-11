"""Read the same bounded layout for previews, schema inspection and analysis."""
from __future__ import annotations

import csv
import io
from itertools import islice
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import pandas as pd
from openpyxl.utils.exceptions import InvalidFileException
from xlrd import XLRDError

from protocol import (
    MAX_COLUMNS, MAX_HEADER_ROW, MAX_PREVIEW_ROWS, MAX_ROWS, MAX_SHEETS,
    FileFormat, FileLayout, FilePreview, FileSchema, SheetPreview, SheetSchema,
)
from tasks import clean_label, numeric_values

_MAX_EXCEL_BYTES = 64 * 1024 * 1024
_SAMPLE_DATA_ROWS = 32
_PARSE_ERRORS = (BadZipFile, XLRDError, InvalidFileException, ParseError, pd.errors.ParserError,
                 pd.errors.EmptyDataError, UnicodeError, OSError, KeyError, csv.Error)


def _csv_rows(data: bytes, count: int) -> list[list]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("gb18030")
    rows = []
    for row in islice(csv.reader(io.StringIO(text, newline=""), strict=True), count):
        if len(row) > MAX_COLUMNS:
            raise ValueError(f"单张工作表最多 {MAX_COLUMNS} 列")
        rows.append([cell if cell.strip() else None for cell in row])
    return rows


def _workbook(data: bytes, file_format: FileFormat) -> pd.ExcelFile:
    if file_format == "xlsx":
        with ZipFile(io.BytesIO(data)) as workbook:
            entries = workbook.infolist()
            if len(entries) > 2000 or sum(item.file_size for item in entries) > _MAX_EXCEL_BYTES:
                raise ValueError("Excel 解压后的内容超过分析上限")
    return pd.ExcelFile(io.BytesIO(data), engine="openpyxl" if file_format == "xlsx" else "xlrd")


def _excel_rows(book: pd.ExcelFile, index: int, count: int) -> list[list]:
    frame = book.parse(index, header=None, nrows=count, keep_default_na=False)
    if len(frame.columns) > MAX_COLUMNS:
        raise ValueError(f"单张工作表最多 {MAX_COLUMNS} 列")
    return frame.values.tolist()


def _label(value, limit: int = 120) -> str:
    return "" if value is None or pd.isna(value) else clean_label(value, limit)


def _guess_header(rows: list[list]) -> int:
    for index, row in enumerate(rows[:MAX_PREVIEW_ROWS], 1):
        if sum(bool(_label(value)) for value in row) >= 2:
            return index
    return next((index for index, row in enumerate(rows[:MAX_PREVIEW_ROWS], 1) if any(_label(value) for value in row)), 1)


def _columns(rows: list[list], start: int, depth: int) -> list[str]:
    headers = rows[start - 1:start - 1 + depth]
    width = max(map(len, rows), default=0)
    if len(headers) != depth or not any(_label(value) for row in headers for value in row):
        raise ValueError("所选表头为空或超出工作表，请重新选择表头行")
    levels = [[_label(value) for value in row] + [""] * (width - len(row)) for row in headers]
    # A blank parent above a named child can belong to a horizontal merge.
    # Carry labels only within the chosen header block. Body cells stay untouched.
    for level, parent in enumerate(levels[:-1]):
        previous = ""
        for col, label in enumerate(parent):
            if label:
                previous = label
            elif any(child[col] for child in levels[level + 1:]):
                parent[col] = previous
    names = []
    used = set()
    for col in range(width):
        pieces = list(dict.fromkeys(level[col] for level in levels if level[col]))
        name = clean_label(" / ".join(pieces), 105) or f"列 {col + 1}"
        candidate = name
        suffix = 1
        while candidate in used:
            candidate = f"{name}.{suffix}"
            suffix += 1
        names.append(candidate)
        used.add(candidate)
    return names


def _frame(rows: list[list], header: int, depth: int) -> pd.DataFrame:
    columns = _columns(rows, header, depth)
    values = [row + [None] * (len(columns) - len(row)) for row in rows[header - 1 + depth:]]
    return pd.DataFrame(values, columns=columns).replace(r"^\s*$", None, regex=True)


def _preview_cell(value):
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value if float("-inf") < value < float("inf") else str(value)
    return _label(value, 80) or None


def _read(data: bytes, file_format: FileFormat, sheet: int | None, count: int):
    try:
        if file_format == "csv":
            if sheet not in {0, None}:
                raise ValueError("CSV 只有一个工作表")
            yield 0, "CSV", False, _csv_rows(data, count), None
            return
        with _workbook(data, file_format) as book:
            if sheet is not None and sheet >= len(book.sheet_names):
                raise ValueError("所选工作表不存在")
            indices = [sheet] if sheet is not None else range(min(MAX_SHEETS, len(book.sheet_names)))
            for index in indices:
                name = clean_label(book.sheet_names[index])
                extra = len(book.sheet_names) > MAX_SHEETS
                try:
                    rows = _excel_rows(book, index, count)
                except ValueError as exc:
                    yield index, name, extra, [], str(exc)
                else:
                    yield index, name, extra, rows, None
    except _PARSE_ERRORS as exc:
        raise ValueError("无法读取文件，请检查文件格式、列数和表头") from exc


def inspect_layout(data: bytes, file_format: FileFormat, layout: FileLayout, *, preview: bool) -> FilePreview | FileSchema:
    previews, schemas = [], []
    extra = False
    sheet_count = 0
    count = (layout.header_row or MAX_PREVIEW_ROWS) + layout.header_depth + _SAMPLE_DATA_ROWS
    for index, name, extra, rows, issue in _read(data, file_format, layout.sheet, count):
        sheet_count += 1
        header = layout.header_row or _guess_header(rows)
        columns = []
        if not issue:
            try:
                frame = _frame(rows, header, layout.header_depth)
            except ValueError as exc:
                issue = str(exc)
            else:
                columns = list(frame.columns)
                schemas.append(SheetSchema(
                    index=index, name=name, columns=columns,
                    numeric_columns=[
                        i for i in range(len(columns))
                        if not pd.api.types.is_bool_dtype(frame.iloc[:, i].dtype)
                        and numeric_values(frame.iloc[:, i]).notna().any()
                    ],
                    header_row=header, header_depth=layout.header_depth,
                ))
        if preview:
            start = max(1, header - 2) if layout.header_row else 1
            previews.append(SheetPreview(
                index=index, name=name, header_row=header, header_depth=layout.header_depth, columns=columns,
                sample_start=start,
                rows=[[_preview_cell(value) for value in row] for row in (rows[start - 1:start - 1 + MAX_PREVIEW_ROWS]
                             if layout.sheet is not None or index == 0 else [])],
                issue=issue,
            ))
        elif issue and layout.sheet is not None:
            raise ValueError(issue)
    if preview:
        return FilePreview(sheets=previews, extra_sheets=extra)
    if not schemas:
        raise ValueError("没有找到可分析的工作表，请在预览中确认表头")
    return FileSchema(sheets=schemas, extra_sheets=extra, sheet_count=sheet_count)

def load_frame(data: bytes, file_format: FileFormat, layout: FileLayout) -> tuple[pd.DataFrame, int]:
    count = MAX_ROWS + MAX_HEADER_ROW + layout.header_depth + 1
    source = _read(data, file_format, layout.sheet or 0, count)
    try:
        _, _, _, rows, issue = next(source)
    finally:
        source.close()
    if issue:
        raise ValueError(issue)
    header = layout.header_row or _guess_header(rows)
    return _frame(rows, header, layout.header_depth), header - 1 + layout.header_depth
