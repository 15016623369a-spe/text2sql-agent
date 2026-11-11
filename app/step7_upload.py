"""Minimal Streamlit entry point for the file-analysis milestone."""
import hashlib

import streamlit as st

from app.file_analysis import analyze_upload, preview_upload
from app.file_preview_ui import choose_layout
from app.file_sandbox import SandboxError
from sandbox.step7.protocol import FileLayout


def request_preview(uploaded, params):
    try:
        result = preview_upload(uploaded.name, uploaded.getvalue(), FileLayout(**params))
    except (SandboxError, ValueError) as exc:
        return {"kind": "error", "message": str(exc)}
    return {"kind": "preview", "preview": result.model_dump(mode="json")}


st.set_page_config(page_title="文件分析 · Chat with Data", layout="wide")
st.title("文件分析")
st.caption("上传 CSV 或 Excel，在本机 Docker 沙箱中做清洗、统计和异常值分析。")
uploaded = st.file_uploader("上传数据文件", type=["csv", "xlsx", "xls"], max_upload_size=10)
layout = None
if uploaded is not None:
    digest = hashlib.sha256(uploaded.getbuffer()).hexdigest()
    layout = choose_layout(uploaded.name, digest, lambda params: request_preview(uploaded, params))
question = st.text_input("你想分析什么？", value="有没有异常值并画图", max_chars=500)
if st.button("开始分析", type="primary", disabled=layout is None):
    try:
        with st.spinner("正在分析…"):
            result = analyze_upload(uploaded.name, uploaded.getvalue(), question, layout=FileLayout(**layout))
    except (SandboxError, ValueError) as exc:
        st.error(str(exc))
    else:
        st.subheader("结果解读")
        st.text(result.narrative)
        if result.report is not None and result.report.figure is not None:
            st.subheader("分布图")
            st.plotly_chart(result.report.figure, width="stretch", config={"displaylogo": False})
