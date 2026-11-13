"""Read the same bounded layout for previews, schema inspection and analysis."""
from __future__ import annotations

import csv
import io
import re
from contextlib import nullcontext
from dataclasses import dataclass
from itertools import islice
from typing import Literal
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

import pandas as pd
from defusedxml.ElementTree import iterparse
from defusedxml.common import DefusedXmlException
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.exceptions import InvalidFileException
from xlrd import XLRDError

from protocol import (
    MAX_COLUMNS, MAX_HEADER_DEPTH, MAX_HEADER_ROW, MAX_PREVIEW_ROWS, MAX_ROWS, MAX_SHEETS,
    FileFormat, FileLayout, FilePreview, FileSchema, SheetPreview, SheetSchema,
)
from tasks import clean_label, numeric_values

_MAX_EXCEL_BYTES = 64 * 1024 * 1024
_SAMPLE_DATA_ROWS = 32
_PARSE_ERRORS = (BadZipFile, XLRDError, InvalidFileException, ParseError, DefusedXmlException, pd.errors.ParserError,
                 pd.errors.EmptyDataError, UnicodeError, OSError, KeyError, csv.Error)
_UNIT_NOTE = re.compile(r"^(?:计量)?单位\s*[:：=＝]")
_TITLE = re.compile(r"(?:统计表|汇总表|报表|一览表|明细表|统计报告|统计年鉴)$")
_VALUE = re.compile(r"[+-]?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:[eE][+-]?\d+)?%?|\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?")
_YEAR_LABEL = re.compile(r"(?:19|20)\d{2}")
_LAYOUT_CONFIRMATION = "无法确定多级表头的结束行。请在预览中确认表头起始行和表头行数。"
MergeRange = tuple[int, int, int, int]


@dataclass(frozen=True)
class Header:
    row: int
    depth: int
    issue: str | None = None
    warnings: tuple[str, ...] = ()
    needs_confirmation: bool = False


class HeaderLayoutError(ValueError):
    """The requested layout cannot safely separate headers from data."""


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
    if file_format == "xls":
        return pd.ExcelFile(io.BytesIO(data), engine="xlrd", engine_kwargs={"formatting_info": True})
    return pd.ExcelFile(io.BytesIO(data), engine="openpyxl")


def _excel_rows(book: pd.ExcelFile, index: int, count: int) -> list[list]:
    frame = book.parse(index, header=None, nrows=count, keep_default_na=False)
    if len(frame.columns) > MAX_COLUMNS:
        raise ValueError(f"单张工作表最多 {MAX_COLUMNS} 列")
    return frame.values.tolist()


def _label(value, limit: int = 120) -> str:
    return "" if value is None or pd.isna(value) else clean_label(value, limit)


def _xlsx_merges(archive: ZipFile, book: pd.ExcelFile, index: int) -> tuple[list[MergeRange], int]:
    # openpyxl's read-only worksheet omits merge ranges. Its resolved XML path
    # follows workbook relationships, including workbooks with custom sheet paths.
    worksheet = book.book.worksheets[index]
    path = getattr(worksheet, "_worksheet_path", None)
    if path is None:
        return [], 0
    ranges = []
    last_merged_row = 0
    with archive.open(path.lstrip("/")) as xml:
        stack = []
        for event, node in iterparse(xml, events=("start", "end")):
            if event == "start":
                stack.append(node)
                continue
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "mergeCell":
                left, first, right, last = range_boundaries(node.get("ref", ""))
                if None not in (first, last, left, right) and left <= MAX_COLUMNS and (first != last or left != right):
                    last_merged_row = max(last_merged_row, last)
                    if first <= MAX_HEADER_ROW + MAX_HEADER_DEPTH:
                        ranges.append((first, last, left, right))
                        if len(ranges) > (MAX_HEADER_ROW + MAX_HEADER_DEPTH) * MAX_COLUMNS:
                            raise ValueError("表头附近的合并区域过多，请整理工作表")
            if tag == "mergeCells":
                break
            stack.pop()
            if stack:
                stack[-1].remove(node)
    return ranges, last_merged_row


def _sheet_merges(archive: ZipFile | None, book: pd.ExcelFile, index: int) -> tuple[list[MergeRange], int]:
    if archive is not None:
        return _xlsx_merges(archive, book, index)
    ranges = []
    last_merged_row = 0
    for start, stop, left, right in book.book.sheet_by_index(index).merged_cells:
        if left < MAX_COLUMNS and (stop - start > 1 or right - left > 1):
            last_merged_row = max(last_merged_row, stop)
            if start < MAX_HEADER_ROW + MAX_HEADER_DEPTH:
                ranges.append((start + 1, stop, left + 1, right))
    return ranges, last_merged_row


def _is_value(label: str) -> bool:
    return bool(_VALUE.fullmatch(label))


def _looks_like_title(labels: list[str]) -> bool:
    populated = [label for label in labels if label]
    return bool(populated) and ((len(populated) <= 2 and any(_UNIT_NOTE.match(label) for label in populated))
                               or (len(populated) == 1 and _TITLE.search(populated[0])))


def _child_level(parent: list[str], child: list[str], row: int, merges: list[MergeRange]) -> Literal["merge", "possible"] | None:
    children = {i for i, label in enumerate(child) if label}
    if len(children) < 2 or any(_UNIT_NOTE.match(child[i]) for i in children):
        return None
    merged = any(first == row and right > left and any(left <= i < right for i in children)
                 for first, _, left, right in merges)
    values = [child[i] for i in children if _is_value(child[i])]
    if values:
        if merged and len(set(values)) >= 2 and all(_YEAR_LABEL.fullmatch(value) for value in values):
            return "possible"
        return None
    if merged:
        return "merge"
    first_label = next((i for i, label in enumerate(parent) if label), len(parent))
    gaps = any(first_label < i < len(parent) and not parent[i] for i in children)
    repeated = any(a and a == b for a, b in zip(parent, parent[1:]))
    return "possible" if gaps or repeated else None


def _header_leaf(labels: list[list[str]], start: int, merges: list[MergeRange]) -> tuple[int, bool]:
    leaf = start
    exact = True
    for row in range(start, min(start + MAX_HEADER_DEPTH - 1, len(labels))):
        if not (evidence := _child_level(labels[row - 1], labels[row], row, merges)):
            break
        leaf += 1
        exact &= evidence == "merge"
    return leaf, exact


def _select_header(rows: list[list], layout: FileLayout, merges: list[MergeRange],
                   last_merged_row: int) -> Header:
    labels = [[_label(value) for value in row] for row in
              rows[:(layout.header_row or MAX_PREVIEW_ROWS) + MAX_HEADER_DEPTH + _SAMPLE_DATA_ROWS]]
    candidates = [(i, row) for i, row in enumerate(labels[:MAX_PREVIEW_ROWS], 1) if any(row)]
    title_rows = set()
    for i, row in candidates[:-1]:
        if not _looks_like_title(row):
            break
        title_rows.add(i)
    first = next((i for i, _ in candidates if i not in title_rows), 1)
    if candidates and sum(bool(label) for label in labels[first - 1]) == 1:
        following = next(((i, row) for i, row in candidates if i > first), None)
        if following:
            next_index, next_row = following
            child = next_index == first + 1 and _child_level(labels[first - 1], next_row, first, merges)
            populated = [label for label in next_row if label]
            if len(populated) >= 2 and not any(map(_is_value, populated)) and not child:
                first = next_index
    row = layout.header_row or first
    # Check earlier rows even when an explicit layout starts in the middle of
    # the inferred hierarchy. Beginning at its leaf is a valid manual choice.
    leaf, exact = _header_leaf(labels, first, merges)
    if not first <= row <= leaf:
        leaf, exact = _header_leaf(labels, row, merges)
    suggested = max(1, leaf - row + 1)
    selected = layout.header_depth or suggested
    multi_level = suggested > 1
    numeric_body = any(_is_value(label) for item in labels[leaf:leaf + 5] for label in item if label)
    needs_confirmation = multi_level and not (exact and numeric_body)
    explicit = layout.header_row is not None and layout.header_depth is not None
    issue = None
    warnings = []
    header_end = row + selected - 1
    merge_notes = []
    if any(row <= first <= header_end for first, _, _, _ in merges):
        merge_notes.append("检测到表头合并单元格，合并区沿用首格标签。")
    if last_merged_row > header_end:
        merge_notes.append("检测到数据区合并单元格，产生的空白按缺失处理。")
    if merge_notes:
        warnings.append("".join(merge_notes))
    if title_rows and row > max(title_rows):
        warnings.append("已跳过标题或单位说明行。")
    if multi_level:
        evidence = "可能有" if needs_confirmation else "检测到"
        warnings.append(f"{evidence}多级表头，建议使用第 {row}–{leaf} 行。")
    if row in title_rows:
        issue = f"第 {row} 行是标题或单位说明。请选择第 {first} 行或实际列名所在行作为表头。"
    elif selected < suggested and not needs_confirmation:
        issue = f"检测到多级表头，第 {leaf} 行仍是列名。请将表头设为从第 {row} 行起共 {suggested} 行，或从第 {leaf} 行开始。"
    elif needs_confirmation and not explicit:
        return Header(row, selected, _LAYOUT_CONFIRMATION, tuple(warnings), needs_confirmation=True)
    return Header(row, selected, issue, tuple(warnings))


def _columns(rows: list[list], start: int, depth: int,
             merges: list[MergeRange]) -> tuple[list[str], list[str]]:
    headers = rows[start - 1:start - 1 + depth]
    width = max(map(len, rows), default=0)
    if len(headers) != depth or not any(_label(value) for row in headers for value in row):
        raise ValueError("所选表头为空或超出工作表，请重新选择表头行")
    levels = [[_label(value) for value in row] + [""] * (width - len(row)) for row in headers]
    if merges:
        for first, last, left, right in merges:
            if start <= first < start + depth and left <= width:
                label = levels[first - start][left - 1]
                for row in range(first, min(last + 1, start + depth)):
                    for col in range(left - 1, min(right, width)):
                        if not levels[row - start][col]:
                            levels[row - start][col] = label
    else:
        # CSV exports can leave parent labels blank after the first child.
        for level, parent in enumerate(levels[:-1]):
            previous = ""
            for col, label in enumerate(parent):
                if label:
                    previous = label
                elif any(child[col] for child in levels[level + 1:]):
                    parent[col] = previous
    names = []
    used = set()
    missing = duplicated = False
    for col in range(width):
        pieces = list(dict.fromkeys(level[col] for level in levels if level[col]))
        name = clean_label(" / ".join(pieces), 105) or f"列 {col + 1}"
        missing |= not pieces
        candidate = name
        suffix = 1
        while candidate in used:
            candidate = f"{name}.{suffix}"
            suffix += 1
        duplicated |= candidate != name
        names.append(candidate)
        used.add(candidate)
    warnings = []
    if missing:
        warnings.append("有空列名，已使用列号代替。")
    if duplicated:
        warnings.append("有重复列名，已添加数字后缀以便区分。")
    return names, warnings


def _frame(rows: list[list], header: int, depth: int,
           merges: list[MergeRange]) -> tuple[pd.DataFrame, list[str]]:
    columns, warnings = _columns(rows, header, depth, merges)
    values = [row + [None] * (len(columns) - len(row)) for row in rows[header - 1 + depth:]]
    return pd.DataFrame(values, columns=columns).replace(r"^\s*$", None, regex=True), warnings


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
            yield 0, "CSV", False, _csv_rows(data, count), None, [], 0
            return
        archive = ZipFile(io.BytesIO(data)) if file_format == "xlsx" else nullcontext()
        with archive as zip_metadata, _workbook(data, file_format) as book:
            if sheet is not None and sheet >= len(book.sheet_names):
                raise ValueError("所选工作表不存在")
            indices = [sheet] if sheet is not None else range(min(MAX_SHEETS, len(book.sheet_names)))
            for index in indices:
                name = clean_label(book.sheet_names[index])
                extra = len(book.sheet_names) > MAX_SHEETS
                try:
                    rows = _excel_rows(book, index, count)
                    merges, last_merged_row = _sheet_merges(zip_metadata, book, index)
                except ValueError as exc:
                    yield index, name, extra, [], str(exc), [], 0
                else:
                    yield index, name, extra, rows, None, merges, last_merged_row
    except _PARSE_ERRORS as exc:
        raise ValueError("无法读取文件，请检查文件格式、列数和表头") from exc


def inspect_layout(data: bytes, file_format: FileFormat, layout: FileLayout, *, preview: bool) -> FilePreview | FileSchema:
    previews, schemas = [], []
    extra = False
    sheet_count = 0
    count = (layout.header_row or MAX_PREVIEW_ROWS) + MAX_HEADER_DEPTH + _SAMPLE_DATA_ROWS
    for index, name, extra, rows, read_issue, merges, last_merged_row in _read(data, file_format, layout.sheet, count):
        sheet_count += 1
        header = _select_header(rows, layout, merges, last_merged_row)
        issue = read_issue or header.issue
        columns = []
        warnings = list(header.warnings)
        if not read_issue:
            try:
                frame, column_warnings = _frame(rows, header.row, header.depth, merges)
            except ValueError as exc:
                issue = str(exc)
            else:
                columns = list(frame.columns)
                warnings.extend(column_warnings)
                schemas.append(SheetSchema(
                    index=index, name=name, columns=columns,
                    numeric_columns=[
                        i for i in range(len(columns))
                        if not pd.api.types.is_bool_dtype(frame.iloc[:, i].dtype)
                        and numeric_values(frame.iloc[:, i]).notna().any()
                    ],
                    header_row=header.row, header_depth=header.depth, issue=issue,
                ))
        if preview:
            start = max(1, header.row - 2) if layout.header_row else 1
            sample_included = layout.sheet is not None or index == 0
            sample = rows[start - 1:start - 1 + MAX_PREVIEW_ROWS] if sample_included else []
            previews.append(SheetPreview(
                index=index, name=name, header_row=header.row, header_depth=header.depth, columns=columns,
                sample_start=start, sample_included=sample_included,
                rows=[[_preview_cell(value) for value in row] for row in sample],
                issue=issue, warnings=warnings, needs_confirmation=header.needs_confirmation,
            ))
        elif not columns and issue and layout.sheet is not None:
            raise ValueError(issue)
    if preview:
        return FilePreview(sheets=previews, extra_sheets=extra)
    if not schemas:
        raise ValueError("没有找到可分析的工作表，请在预览中确认表头")
    return FileSchema(sheets=schemas, extra_sheets=extra, sheet_count=sheet_count)


def load_frame(data: bytes, file_format: FileFormat, layout: FileLayout) -> tuple[pd.DataFrame, Header]:
    count = MAX_ROWS + MAX_HEADER_ROW + MAX_HEADER_DEPTH + 1
    source = _read(data, file_format, layout.sheet or 0, count)
    try:
        _, _, _, rows, issue, merges, last_merged_row = next(source)
    finally:
        source.close()
    if issue:
        raise ValueError(issue)
    header = _select_header(rows, layout, merges, last_merged_row)
    if header.issue:
        raise HeaderLayoutError(header.issue)
    frame, _ = _frame(rows, header.row, header.depth, merges)
    return frame, header
