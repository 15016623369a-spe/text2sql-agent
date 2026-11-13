"""Ground audit formulas in declared units or documented header-based conversions."""
from __future__ import annotations

import ast
import math
import re
import unicodedata

from sandbox.step7.protocol import (
    AuditCheck, AuditInputUnit, AuditPlan, FileSchema, Formula, FormulaBinary, FormulaColumn, FormulaNumber,
)

_PEOPLE = {"人": 1, "千人": 1_000, "万人": 10_000, "亿人": 100_000_000}
_MONEY = {"元": 1, "千元": 1_000, "万元": 10_000, "亿元": 100_000_000}
_COUNTS = {"户": 1, "万户": 10_000, "亿户": 100_000_000, "个": 1, "万个": 10_000}
_POPULATION = {"人口", "常住人口", "年末常住人口", "年末人口"}
_AREA_COLUMNS = {"面积", "总面积", "土地面积", "行政区划面积", "行政区域面积", "行政面积", "地区面积"}
_AREA = {"平方米": 1, "平方公里": 1_000_000, "公顷": 10_000, "亩": 2000 / 3,
         "平方厘米": 0.0001, "平方毫米": 0.000001}
_UNIT_ALIASES = {
    "平方千米": "平方公里", "km2": "平方公里", "km^2": "平方公里",
    "m2": "平方米", "m^2": "平方米", "ha": "公顷",
    "cm2": "平方厘米", "cm^2": "平方厘米", "mm2": "平方毫米", "mm^2": "平方毫米",
}
_QUOTED = re.compile(r'["“「]([^"”」]+)["”」]')
_OPERATIONS = {ast.Add: "add", ast.Sub: "subtract", ast.Mult: "multiply", ast.Div: "divide"}

_UNIT_PART = (
    r"(?:[千百十万亿]*(?:元|人|户|个|件|套|单|笔|次|台|辆|吨|克|米|公里|平方米|立方米|升|年|月|日|小时|分钟|秒|度)"
    r"|平方(?:公里|千米|厘米|毫米)|公顷|亩|ha|(?:km|cm|mm|m)\^?[23]"
    r"|%|‰|1|℃|usd|eur|cny|kg|g|mg|km|m|cm|mm|s|ms|h|l|ml|kwh|wh|w|kw|mw|pa|kpa|mpa)"
)
_HEADER_UNIT = re.compile(rf"{_UNIT_PART}(?:[/·]{_UNIT_PART}){{0,3}}")
_TOLERANCE = re.compile(
    r"(?:^|[,;。])(?:指定|使用|允许|按)?(?:绝对)?(?:误差)?容差[:=]?(?:为)?([^,;。!?]*)(?=$|[,;。!?])"
)
_DEVIATION_DETAILS = re.compile(
    r"[,;。](?:并|再|然后)?(?:列出|找出|展示|显示|给出|返回)(?:偏差|误差)(?:最大|最高)(?:的)?(?:记录|数据行|行)$"
)
_AUDIT_PREFIX = re.compile(
    r"^(?:请)?(?:逐行)?(?:核对|校验|验证|重算|复算|检查|复核)(?:一下)?"
    r"(?:(?:[^=;:]{0,80}的)?(?:计算|换算|重算)?公式:?)?"
)
_TARGETS_BEFORE_FORMULA = re.compile(
    r"(?:核对|校验|验证|检查|复核)(?:一下)?(?P<targets>[^,;:。!?]+?)"
    r"(?:列(?:的)?|的)?(?:(?:计算|换算|重算)?公式|是否正确|算得对不对|的计算偏差)"
)


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


def _canonical_unit(unit: str) -> str:
    return "".join(_UNIT_ALIASES.get(part, part) for part in re.split(r"([/·])", unit))


def _label(text: str) -> tuple[str, str]:
    value = normalize(text.rsplit(" / ", 1)[-1])
    match = re.fullmatch(r"(.+?)[(]([^()]*)[)]", value)
    if match:
        unit = match[2]
        if unit.startswith("单位:"):
            return match[1], _canonical_unit(unit.removeprefix("单位:"))
        return match[1], _canonical_unit(unit) if _HEADER_UNIT.fullmatch(unit) else ""
    return value, ""


def _check(target: int, formula: Formula, units: dict[int, str], inputs: set[int], *, inferred: bool) -> AuditCheck:
    return AuditCheck(
        target=target, formula=formula, unit=units[target],
        input_units=[AuditInputUnit(column=index, unit=units[index]) for index in sorted(inputs)],
        basis="inferred" if inferred else "explicit",
    )


def ratio_hints(schema: FileSchema, question: str) -> dict[int, list[AuditCheck]]:
    """Infer only known ratios with a unique numerator, denominator and compatible units."""
    text = normalize(question)
    hints = {}
    for sheet in schema.sheets:
        names = [_label(column) for column in sheet.columns]
        population = [(i, _PEOPLE[unit]) for i, (name, unit) in enumerate(names) if name in _POPULATION and unit in _PEOPLE]
        if len(population) != 1:
            continue
        population_index, population_factor = population[0]
        areas = [(i, _AREA[unit]) for i, (name, unit) in enumerate(names) if name in _AREA_COLUMNS and unit in _AREA]
        checks = []
        for target, (name, unit) in enumerate(names):
            if not name or name not in text:
                continue
            denominator = population_index
            if name in {"人口密度", "常住人口密度"} and unit.startswith("人/") and unit[2:] in _AREA:
                if len(areas) != 1:
                    continue
                numerator = population_index
                denominator, area_factor = areas[0]
                multiplier = population_factor * _AREA[unit[2:]] / area_factor
            elif name.startswith("人均") and unit.endswith("/人") and unit.removesuffix("/人") in _MONEY:
                base = name.removeprefix("人均")
                bases = [i for i, (label, units) in enumerate(names) if label == base and units in _MONEY]
                if len(bases) != 1:
                    continue
                numerator = bases[0]
                multiplier = _MONEY[names[numerator][1]] / population_factor / _MONEY[unit.removesuffix("/人")]
            elif name.startswith("每百人"):
                base = name.removeprefix("每百人").removesuffix("数")
                bases = [i for i, (label, units) in enumerate(names)
                         if label.removesuffix("数") == base and units in _COUNTS]
                if len(bases) != 1:
                    continue
                numerator = bases[0]
                source_unit = names[numerator][1]
                count_unit = "户" if source_unit.endswith("户") else "个"
                if unit and unit not in {count_unit, count_unit + "/百人"}:
                    continue
                unit = count_unit + "/百人"
                multiplier = 100 * _COUNTS[source_unit] / population_factor
            else:
                continue
            formula = FormulaBinary(
                kind="multiply",
                left=FormulaBinary(kind="divide", left=FormulaColumn(kind="column", column=numerator),
                                   right=FormulaColumn(kind="column", column=denominator)),
                right=FormulaNumber(kind="number", value=multiplier),
            )
            units = {target: unit, numerator: names[numerator][1], denominator: names[denominator][1]}
            checks.append(_check(target, formula, units, {numerator, denominator}, inferred=True))
        if checks:
            hints[sheet.index] = checks
    return hints


def _aliases(columns: list[str]) -> dict[str, int]:
    aliases: dict[str, set[int]] = {}
    for index, column in enumerate(columns):
        for name in {normalize(column), _label(column)[0]}:
            if name:
                aliases.setdefault(name, set()).add(index)
    return {name: next(iter(indices)) for name, indices in aliases.items() if len(indices) == 1}


def _unquote(text: str) -> str:
    return text.strip('"“”「」')


def _arithmetic(node, depth: int = 0) -> tuple[Formula, set[int]]:
    if depth > 4:
        raise ValueError("公式过于复杂")
    if isinstance(node, ast.Name) and re.fullmatch(r"_col\d+", node.id):
        index = int(node.id[4:])
        return FormulaColumn(kind="column", column=index), {index}
    if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
        return FormulaNumber(kind="number", value=node.value), set()
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)) and isinstance(node.operand, ast.Constant):
        sign = -1 if isinstance(node.op, ast.USub) else 1
        if type(node.operand.value) in {int, float}:
            return FormulaNumber(kind="number", value=sign * node.operand.value), set()
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATIONS:
        left, left_cols = _arithmetic(node.left, depth + 1)
        right, right_cols = _arithmetic(node.right, depth + 1)
        return FormulaBinary(kind=_OPERATIONS[type(node.op)], left=left, right=right), left_cols | right_cols
    raise ValueError("公式仅支持列名、数值和四则运算")


def _explicit_checks(columns: list[str], question: str) -> list[AuditCheck]:
    text = _AUDIT_PREFIX.sub("", question)
    formula_text, _, declarations = re.split(r"[,;。]?(单位(?:说明)?):", text, maxsplit=1) if re.search(
        r"单位(?:说明)?:", text
    ) else (text, "", "")
    aliases = _aliases(columns)
    units = {index: unit for index, column in enumerate(columns) if (unit := _label(column)[1])}
    if declarations:
        for declaration in re.split(r"[,;、]", declarations):
            name, sep, unit = declaration.partition("=")
            index = aliases.get(_unquote(name))
            if not sep or index is None or not re.fullmatch(r"[\w%°℃/·^²³-]{1,24}", unit):
                raise ValueError("请按「单位：列名=单位」声明各列单位")
            unit = _canonical_unit(unit)
            if index in units and units[index] != unit:
                raise ValueError(f"「{columns[index]}」的声明单位与表头冲突，请确认实际单位")
            units[index] = unit

    if not aliases:
        raise ValueError("没有可唯一识别的公式列")
    names = "|".join(re.escape(name) for name in sorted(aliases, key=len, reverse=True) if not name.isdecimal())
    checks = []
    for expression in re.split(r"[;；]", formula_text):
        left, sep, right = expression.strip("，,").replace("等于", "=", 1).partition("=")
        target = aliases.get(_unquote(left))
        if not sep or target is None or not right or "_col" in right:
            raise ValueError("请用「目标列 = 原始列 × 原始列」明确公式，列名需要与表头一致")
        # Exact column names can be quoted. The replacement uses only known column indexes.
        right = _QUOTED.sub(lambda m: f"_col{aliases[_unquote(m[1])]}" if _unquote(m[1]) in aliases else m[0], right)
        if names:
            right = re.sub(rf"(?<![a-z0-9_])(?:{names})(?![a-z0-9_])",
                           lambda m: f"_col{aliases[m[0]]}", right)
        right = right.translate(str.maketrans({"×": "*", "÷": "/", "−": "-"}))
        try:
            formula, inputs = _arithmetic(ast.parse(right, mode="eval").body)
        except (SyntaxError, ValueError, OverflowError) as exc:
            raise ValueError("请用表头列名和数值写出可识别的四则运算公式") from exc
        if not inputs or target in inputs or len(inputs) > 8:
            raise ValueError("公式需要引用独立的原始列")
        missing = [columns[index] for index in sorted({target, *inputs}) if index not in units]
        if missing:
            raise ValueError(f"请补充「{'、'.join(missing)}」的单位，再核对公式")
        checks.append(_check(target, formula, units, inputs, inferred=False))
    return checks


def _audit_options(question: str) -> tuple[str, float | None]:
    text = normalize(question).rstrip("。?!")
    matches = list(_TOLERANCE.finditer(text))
    if not matches:
        return _DEVIATION_DETAILS.sub("", text), None
    try:
        if len(matches) != 1:
            raise ValueError
        tolerance = float(matches[0][1])
        if not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError
    except ValueError as exc:
        raise ValueError("请用「容差=0.05」指定一个非负数，单位与目标列相同") from exc
    return _DEVIATION_DETAILS.sub("", _TOLERANCE.sub("", text)), tolerance


def audit_plan(schema: FileSchema, question: str) -> AuditPlan:
    sheet = schema.sheets[0]
    text, tolerance = _audit_options(question)
    formula_text = re.split(r"[,;。]?单位(?:说明)?:", text, maxsplit=1)[0]
    if re.search(r"=|等于", formula_text):
        checks = _explicit_checks(sheet.columns, text)
    else:
        checks = ratio_hints(schema, text).get(sheet.index, [])
        aliases = _aliases(sheet.columns)
        requested = {aliases.get(normalize(name)) for name in _QUOTED.findall(question)}
        if targets := _TARGETS_BEFORE_FORMULA.search(text):
            names = re.split(r"[、,]|和|与|以及", targets["targets"])
            requested |= {aliases[name] for item in names if (name := _unquote(item.removesuffix("列"))) in aliases}
        count = re.search(r"([1-9一二两三四五])\s*个(?:派生|衍生)", question)
        expected_count = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5}.get(count[1], count[1]) if count else 0
        if not checks or not requested <= {check.target for check in checks} or len(checks) < int(expected_count):
            raise ValueError(
                "无法确认全部目标列的公式和单位。请按「目标列 = 输入列 × 输入列」补充重算公式。"
                "表头未标单位时，请同时注明目标列及输入列的单位"
            )
    if not 1 <= len(checks) <= 3 or len({check.target for check in checks}) != len(checks):
        raise ValueError("每次请核对 1 到 3 个不重复的派生列")
    labels = [index for index, column in enumerate(sheet.columns)
              if _label(column)[0] in {"时间", "年份", "城市", "省份", "地区", "日期", "year", "city"}][:3]
    return AuditPlan(mode="audit", checks=checks, labels=labels, tolerance=tolerance)
