"""Conservative shape checks for already authorized SQLite queries.

Passing these checks leaves correctness unverified. Only a clearly requested
result shape contradicted by the query or its complete result blocks an answer.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal


_SQL_TOKEN = re.compile(
    r"--[^\r\n]*|/\*[\s\S]*?\*/|'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|[^\W\d]\w*|>=|<=|!=|<>|==|[^\s]"
)
_AGGREGATES = {"AVG", "COUNT", "GROUP_CONCAT", "SUM", "TOTAL", "JSON_GROUP_ARRAY", "JSON_GROUP_OBJECT"}
_RANK = re.compile(r"\btop\s+\d+\b|\b(?:first|last)\s+\d+\b|前\s*[一二三四五六七八九十百\d]+(?:名|条|个)", re.I)
_GROUP = re.compile(
    r"\b(?:breakdown by|broken down by|group(?:ed)? by|(?:what (?:is|are)|give|find|list|show|return|calculate|report)\b[^.;?]{0,100}for each)\b"
    r"|\bhow many\b[^.;?]{0,80}\beach\b|按[^，。？;]{1,24}(?:分组|分别统计|分别计算)|(?:统计|列出|分别计算)(?:每个|各个)", re.I,
)
_GROUP_AMBIGUITY = re.compile(
    r"\b(?:on average|overall|across all|combined|average number|single total|highest|lowest|minimum|maximum|largest|smallest|top)\b"
    r"|\b(?:which|how many)\b[^.;?]{0,70}\b(?:has|have|having)\b[^.;?]{0,25}\b(?:more|fewer|at least|at most)\b"
    r"|总体|总共|总计|最多|最少|最高|最低|平均每", re.I,
)
_COUNT_QUESTION = re.compile(r"^\s*(?:how many\b|what (?:is|was) (?:the )?(?:total )?(?:number|count) of\b)", re.I)
_COUNT_CN = re.compile(r"(?:多少|几)(?:个|笔|名|条|所|位|件|家|次)(?:[^。？！?!]+)?[。？！?!]?$")
_COUNT_AMBIGUITY = re.compile(
    r"\b(?:and|or|each|per|by|list|which|names?|compare|percentage|ratio|average|distinct groups)\b|按|分别|各个|每个|哪些|列出|占比|比率|平均|对比", re.I,
)
_DATE_COLUMN = re.compile(r"(?:date|(?:.+_)?date|.+_(?:at|on)|birthdate|birthday)", re.I)
_NON_ISO_DATE = re.compile(r"(?:\d{4}/\d{1,2}(?:/\d{1,2})?|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{8})")


@dataclass(frozen=True)
class _Signal:
    code: str
    phase: Literal["pre_execution", "post_execution"]
    message: str
    suggestion: str | None = None

    @property
    def blocking(self) -> bool:
        return self.suggestion is not None


@dataclass(frozen=True)
class SemanticCheck:
    signals: tuple[_Signal, ...]

    @property
    def blocked(self) -> bool:
        return any(signal.blocking for signal in self.signals)

    @property
    def message(self) -> str | None:
        return next((signal.message for signal in self.signals if signal.blocking), None)

    def as_dict(self) -> dict:
        return {
            "confidence": "low" if self.signals else "unverified",
            "signals": [
                {"code": signal.code, "phase": signal.phase, "blocking": signal.blocking, "message": signal.message}
                for signal in self.signals
            ],
        }

    def abstention(self, sql: str, *, executed: bool) -> dict:
        issue = next((signal for signal in self.signals if signal.blocking), None)
        if issue is None:
            raise ValueError("An unblocked query has no abstention")
        return {
            "kind": "abstained",
            "message": issue.message,
            "suggestion": issue.suggestion,
            "stop_reason": "semantic_check_failed",
            "sql": sql,
            "executed": executed,
            "semantics": self.as_dict(),
        }


def _tokens(sql: str) -> list[tuple[str, int]]:
    """Tokenize only the constructs used by the checks, keeping nesting depth."""
    tokens = []
    depth = 0
    for match in _SQL_TOKEN.finditer(sql):
        raw = match[0]
        if raw.startswith(("--", "/*")):
            continue
        if raw == ")":
            depth -= 1
        tokens.append((raw, depth))
        if raw == "(":
            depth += 1
    return tokens


def _pre_execution(question: str, sql: str) -> list[_Signal]:
    tokens = _tokens(sql)
    top = [token.upper() for token, depth in tokens if depth == 0]
    if not top or top[0] != "SELECT" or "FROM" not in top:
        return []
    # A nested or compound SELECT can express grouping indirectly. Windowed
    # aggregates preserve detail rows even when no GROUP BY is present.
    words = [token.upper() for token, _ in tokens]
    simple = words.count("SELECT") == 1 and not {"UNION", "INTERSECT", "EXCEPT", "OVER"}.intersection(words)
    if not simple:
        return []
    group_by = ("GROUP", "BY") in zip(top, top[1:])
    first_from = next(i for i, (token, depth) in enumerate(tokens) if depth == 0 and token.upper() == "FROM")
    projection = words[1:first_from]
    aggregate = any(name in _AGGREGATES and following == "(" for name, following in zip(projection, projection[1:]))
    signals = []
    if aggregate and not group_by and _GROUP.search(question) and not _GROUP_AMBIGUITY.search(question):
        signals.append(_Signal(
            "missing_group_by", "pre_execution", "问题要求按类别分别返回结果，生成的查询只计算了一个整体汇总。",
            "请明确要分组的字段后重试。",
        ))
    order_by = ("ORDER", "BY") in zip(top, top[1:])
    if _RANK.search(question) and order_by and "LIMIT" in top and projection == ["*"]:
        signals.append(_Signal("ranking_full_rows", "pre_execution", "排名查询返回整行，可核对是否需要所有列。"))

    for left, operator, right in zip(words, words[1:], words[2:]):
        # Quoted SQL literals are kept intact so comments and projection labels
        # cannot masquerade as comparisons or grouping clauses.
        name = left.strip('"`[]').casefold()
        if not right.startswith("'"):
            continue
        if (name == "id" or name.endswith("_id")) and operator in {"LIKE", "GLOB"}:
            signals.append(_Signal("id_text_match", "pre_execution", "查询按文本模式匹配 ID，请核对这是编号还是名称。"))
            break
    for left, operator, right in zip(words, words[1:], words[2:]):
        name = left.strip('"`[]')
        if operator in {"=", "==", ">", ">=", "<", "<=", "BETWEEN"} and right.startswith("'") and (
            _DATE_COLUMN.fullmatch(name) and _NON_ISO_DATE.fullmatch(right[1:-1])
        ):
            signals.append(_Signal("date_encoding_check", "pre_execution", "日期条件使用文本比较，请核对数据列实际采用的日期编码。"))
            break
    return signals


def _post_execution(question: str, rows: Sequence[Sequence[object]], total_rows: int) -> _Signal | None:
    if not (_COUNT_QUESTION.search(question) or _COUNT_CN.search(question)) or _COUNT_AMBIGUITY.search(question):
        return None
    if total_rows == 0:
        return _Signal(
            "missing_count_result", "post_execution", "问题要求返回数量，但执行结果没有给出计数。",
            "请确认筛选范围，或改问该范围内共有多少条记录。",
        )
    if total_rows == len(rows) == 1 and len(rows[0]) == 1:
        value = rows[0][0]
        if value is None or isinstance(value, (int, float)) and value < 0:
            return _Signal(
                "invalid_count_result", "post_execution", "查询未返回有效的非负计数。",
                "请明确要统计的对象和筛选条件后重试。",
            )
    return None


def check_query(
    question: str, sql: str, *, rows: Sequence[Sequence[object]] | None = None, total_rows: int | None = None,
) -> SemanticCheck:
    signals = _pre_execution(question, sql)
    if rows is not None and total_rows is not None and (signal := _post_execution(question, rows, total_rows)):
        signals.append(signal)
    return SemanticCheck(tuple(signals))
