"""Shared file preview and layout selection for both Streamlit entry points."""
import streamlit as st

from sandbox.step7.protocol import MAX_HEADER_DEPTH, MAX_HEADER_ROW


def choose_layout(filename: str, digest: str, fetch_preview, *, saved_layout: dict | None = None):
    cache = st.session_state.get("upload_preview")
    if cache is None or cache["digest"] != digest or cache["name"] != filename:
        cache = {"digest": digest, "name": filename, "previews": {}}
        st.session_state["upload_preview"] = cache

    def load(sheet=None, row=None, depth=None):
        key = (sheet, row, depth)
        if key not in cache["previews"]:
            params = {}
            if sheet is not None:
                params["sheet"] = sheet
            if row is not None:
                params["header_row"] = row
            if depth is not None:
                params["header_depth"] = depth
            reply = fetch_preview(params)
            if reply.get("kind") != "preview":
                st.error(reply.get("message", "文件预览暂不可用"))
                return None
            if len(cache["previews"]) >= 4:
                cache["previews"].pop(next(item for item in cache["previews"] if item[0] is not None))
            cache["previews"][key] = reply["preview"]
        return cache["previews"][key]

    workbook = load()
    if not workbook or not workbook["sheets"]:
        return None
    options = {item["index"]: item for item in workbook["sheets"]}
    initial = 0 if len(options) == 1 and not workbook["extra_sheets"] else None
    saved_layout = saved_layout or {}
    if saved_layout.get("sheet") in options:
        initial = list(options).index(saved_layout["sheet"])
    sheet = st.selectbox("工作表", list(options), index=initial, format_func=lambda i: options[i]["name"],
                         placeholder="选择要分析的工作表", key=f"sheet-{digest}")
    if workbook["extra_sheets"]:
        st.caption("当前可预览前 8 张工作表。")
    if sheet is None:
        st.info("文件包含多个工作表。请选择要分析的工作表。")
        return None
    default = options[sheet]
    selected = saved_layout if saved_layout.get("sheet") == sheet else {}
    left, right = st.columns(2)
    row = left.number_input("表头起始行", 1, MAX_HEADER_ROW, selected.get("header_row", default["header_row"]),
                            step=1, key=f"header-row-{digest}-{sheet}")
    depth = right.number_input("表头行数", 1, MAX_HEADER_DEPTH, selected.get("header_depth", default["header_depth"]), step=1,
                               key=f"header-depth-{digest}-{sheet}")
    if row == default["header_row"] and depth == default["header_depth"]:
        preview = default
        if not default["rows"] and default.get("sample_included") is not True:
            updated = load(sheet)
            if not updated:
                return None
            preview = updated["sheets"][0]
    else:
        updated = load(sheet, row, depth)
        if not updated:
            return None
        preview = updated["sheets"][0]
    for warning in preview.get("warnings", []):
        st.warning(warning)
    width = max(len(preview["columns"]), max(map(len, preview["rows"]), default=0))
    sample = [cells[:width] + [None] * max(0, width - len(cells)) for cells in preview["rows"]]
    if sample:
        st.caption("原表预览")
        table = {"行号": [preview["sample_start"] + i for i in range(len(sample))]}
        table.update({
            f"第 {column + 1} 列": [str(cells[column]) if cells[column] is not None else None for cells in sample]
            for column in range(width)
        })
        st.dataframe(table, hide_index=True)
    if preview["columns"]:
        st.text("解析出的表头　" + " ｜ ".join(preview["columns"]))
        st.caption(f"第 {row}–{row + depth - 1} 行作为表头，第 {row + depth} 行起为数据。")
    if preview["issue"]:
        st.warning(preview["issue"])
        if not preview.get("needs_confirmation") or not st.checkbox(
            "确认使用上述表头范围", key=f"confirm-header-{digest}-{sheet}-{row}-{depth}",
        ):
            return None
    return {"sheet": sheet, "header_row": row, "header_depth": depth}

