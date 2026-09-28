"""受限排版与条件短语比较（评价设计 A1–A5、B 系列、C1、U1/U2 边界）。

比较按域区分：数值/单位/条件各自适用不同规则，不把任意英文差异套
用到数值比较，也不把数值规则套到条件短语。

- 数值域（``compare_number``）：只做无尺度转换的排版整理（Unicode
  负号、上下标、``\\mathrm{}``/``\\,`` 包裹、``\\times``、时分秒标记、
  受限空白）；保留原报精度、系数尾零、指数符号和单位尺度（C1、B1、
  B2 由此保持不同）。未知宏或不可确定语义的表达一律 ``undecidable``
  （U1），绝不删除宏后比较，也不因包含合法数字而放行。
- 单位域（``compare_unit``）：符号不等即不同（不做尺度换算）；含未
  支持语法的单位表达无法确定语义时 ``undecidable``。
- 条件域（``compare_condition``）：精确匹配、A5 白名单、明确 M1/M2
  模型差异、字面否定为确定判定；白名单未命中不能证明不同，其余未支
  持差异一律 ``undecidable``（U2 缺失模型限定同样未决）。

不使用 Embedding，不做 eval/sympy/单位换算。
"""

from __future__ import annotations

import re
from typing import Literal

Verdict = Literal["equivalent", "different", "undecidable"]

# 已审 LaTeX 宏；出现其他宏（如 \mystery）即 undecidable。
_KNOWN_MACROS = frozenset({"times", "mathrm", ",", ";", "!", " ", "quad", "qquad"})

_SUPERSCRIPTS = {
    "⁰": "0",
    "¹": "1",
    "²": "2",
    "³": "3",
    "⁴": "4",
    "⁵": "5",
    "⁶": "6",
    "⁷": "7",
    "⁸": "8",
    "⁹": "9",
    "⁻": "-",
    "⁺": "+",
}

_MACRO_RE = re.compile(r"\\([A-Za-z]+|[^A-Za-z])")
_SUP_RUN_RE = re.compile("[" + "".join(_SUPERSCRIPTS) + "]+")
_TIMES_SPACE_RE = re.compile(r"\s*\*\s*")
_WS_RE = re.compile(r"\s+")

# 时分秒上下标标记（A4）：仅折叠这三个单字母标记，不转换成角度。
_SEXAGESIMAL_MARKS = ("h", "m", "s")

# 单位域的已支持字符集：字母、上下标记号与连字符/点。
_UNIT_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789^{}-. "
)

# 完整受限数值语法（对整理后的串全匹配）：带符号小数、可选 e/E 指数
# 或已审 ``*10^{±d}`` 乘幂、以及按时→分→秒顺序的时分秒链。字符在白
# 名单内不代表语法有效：未完成指数（``8.2e+``）、断裂乘幂、非配对括
# 号等一律不判确定差异。
_NUM_ATOM = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)"
_NUM_EXP = r"(?:[eE][+-]?\d+)"
_NUM_POW = r"(?:\*10\^\{[+-]?\d+\})"
_NUM_SEXAGESIMAL = (
    rf"(?:{_NUM_ATOM}h\s*{_NUM_ATOM}m\s*{_NUM_ATOM}s"
    rf"|{_NUM_ATOM}h\s*{_NUM_ATOM}m"
    rf"|{_NUM_ATOM}m\s*{_NUM_ATOM}s"
    rf"|{_NUM_ATOM}h"
    rf"|{_NUM_ATOM}m"
    rf"|{_NUM_ATOM}s)"
)
_NUMERIC_FORM = re.compile(
    rf"(?:{_NUM_ATOM}(?:{_NUM_EXP}|{_NUM_POW})?|{_NUM_SEXAGESIMAL})"
)

# A5 精确白名单：模型条件短语的排版变体；U2：缺失模型限定的模糊短语。
_MODEL_PHRASE_MODEL = {
    "under Galactic potential M1": "M1",
    "assuming potential M1": "M1",
    "under Galactic potential M2": "M2",
    "assuming potential M2": "M2",
}
_VAGUE_MODEL_PHRASES = frozenset(
    {
        "adopting the Galactic potential model",
        "assuming the Galactic potential model",
    }
)


def _normalize_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def normalize_printed(text: str) -> str | None:
    """受限排版整理；含未知宏或不可整理表达时返回 ``None``（undecidable）。

    整理只产生比较用的临时解释，不改写任何记录；原报精度、系数尾零、
    指数符号和单位尺度全部保留（C1、B1、B2 由此保持不同）。
    """
    for match in _MACRO_RE.finditer(text):
        if match.group(1) not in _KNOWN_MACROS:
            return None
    work = text.replace("−", "-")
    work = _SUP_RUN_RE.sub(
        lambda m: "^{" + "".join(_SUPERSCRIPTS[ch] for ch in m.group(0)) + "}",
        work,
    )
    work = _unwrap_mathrm(work)
    for spacing in ("\\,", "\\;", "\\ ", "~"):
        work = work.replace(spacing, " ")
    work = work.replace("\\!", "")
    work = work.replace("\\times", "*").replace("×", "*")
    work = _TIMES_SPACE_RE.sub("*", work)
    for mark in _SEXAGESIMAL_MARKS:
        work = work.replace("^{" + mark + "}", mark)
    return _normalize_whitespace(work)


def _unwrap_mathrm(text: str) -> str:
    while True:
        start = text.find("\\mathrm{")
        if start < 0:
            return text
        depth = 1
        index = start + len("\\mathrm{")
        while index < len(text) and depth:
            if text[index] == "{":
                depth += 1
            elif text[index] == "}":
                depth -= 1
            index += 1
        if depth:
            return text
        inner = text[start + len("\\mathrm{") : index - 1]
        text = text[:start] + inner + text[index:]


def is_supported_number_syntax(normalized: str) -> bool:
    """整理后的串是否为完整受限数值语法（全匹配，非字符白名单）。"""
    return _NUMERIC_FORM.fullmatch(normalized) is not None


def _is_supported_number(normalized: str) -> bool:
    return is_supported_number_syntax(normalized)


def _is_supported_unit(normalized: str) -> bool:
    return bool(normalized) and all(ch in _UNIT_ALLOWED for ch in normalized)


def _compare_domain(
    gold: str, prediction: str, is_supported
) -> tuple[str | None, str | None, bool]:
    left = normalize_printed(gold)
    right = normalize_printed(prediction)
    if left is None or right is None:
        return None, None, False
    if left == right:
        return left, right, True
    return left, right, is_supported(left) and is_supported(right)


def compare_number(gold: str, prediction: str) -> Verdict:
    """数值域比较：受限语法内排版等价或明确不同，其余不可确定。

    两串完全相同且为完整受限语法才判等价；任一侧语法未支持（未完成
    指数、断裂乘幂、非配对括号、数值域外文本等）时不从字符合法推导
    科学差异，一律 ``undecidable``。
    """
    left = normalize_printed(gold)
    right = normalize_printed(prediction)
    if left is None or right is None:
        return "undecidable"
    left_supported = _is_supported_number(left)
    right_supported = _is_supported_number(right)
    if left == right and left_supported:
        return "equivalent"
    if not (left_supported and right_supported):
        return "undecidable"
    return "different" if left != right else "equivalent"


def compare_unit(gold: str, prediction: str) -> Verdict:
    """单位域比较：不做尺度换算，符号不等即不同；未支持语法未决。"""
    left, right, determined = _compare_domain(gold, prediction, _is_supported_unit)
    if left is None or right is None:
        return "undecidable"
    if left == right:
        return "equivalent"
    return "different" if determined else "undecidable"


def compare_printed(gold: str, prediction: str) -> Verdict:
    """两段原报打印串的比较（数值域语义；单位请用 ``compare_unit``）。"""
    return compare_number(gold, prediction)


def compare_condition(gold: str, prediction: str) -> Verdict:
    """条件域比较：精确匹配、A5 白名单、M1/M2 差异、字面否定为确定判定。

    白名单未命中不能证明不同：未支持的条件差异一律 ``undecidable``，
    交由既定未知出口或合规 Agent 判定，不得默认对/错。
    """
    left = _normalize_whitespace(gold)
    right = _normalize_whitespace(prediction)
    if left == right:
        return "equivalent"
    if left == "not " + right or right == "not " + left:
        return "different"
    left_base = left.removeprefix("not ")
    right_base = right.removeprefix("not ")
    left_negated = left_base != left
    right_negated = right_base != right
    left_model = _MODEL_PHRASE_MODEL.get(left_base)
    right_model = _MODEL_PHRASE_MODEL.get(right_base)
    if left_model is not None and right_model is not None:
        if left_negated or right_negated:
            # 同一模型被字面否定是确定差异；否定 M1 不等于选定 M2
            return "different" if left_model == right_model else "undecidable"
        return "equivalent" if left_model == right_model else "different"
    if (left_model is not None and right_base in _VAGUE_MODEL_PHRASES) or (
        right_model is not None and left_base in _VAGUE_MODEL_PHRASES
    ):
        return "undecidable"
    return "undecidable"
