# -*- coding: utf-8 -*-
"""题目文本/答案规范化测试。

这些函数的共同点是：**错一位就整题错**，所以每条规则都要有测试守着。
测试数据全部来自实测（Windows OCR 与视觉模型对同一批题目图片的真实输出）。
"""

from __future__ import annotations

import pytest

from autopilot import quizkit


# --- 公式规范化 ---------------------------------------------------------


@pytest.mark.parametrize("raw, want", [
    # 视觉模型的真实输出：LaTeX 片段要变成普通文本
    (r"设y = f(sin² x) + f(cos² x)，则$\frac{dy}{dx}$ = ( ).", "设y = f(sin² x) + f(cos² x)，则(dy)/(dx) = ( )."),
    (r"x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}", "x=(-b±√(b^2-4ac))/(2a)"),
    (r"\sqrt[3]{\frac{a}{b}}", "3√(a/b)"),
    (r"\frac{\frac{1}{2}}{3}", "(1/2)/3"),
    (r"\left( \sum_{i=1}^{n} a_i \right) \leq \infty", "( ∑_(i=1)^(n) a_i ) ≤ ∞"),
    # 代码块包裹要去掉
    ("```\n$y=\\ln x$\n```", "y=ln x"),
    # Unicode 减号（OCR/MathJax 都会吐这个）
    ("3eˣ(cos x \u2212 sin x)", "3eˣ(cos x - sin x)"),
])
def test_normalize_math(raw: str, want: str) -> None:
    assert quizkit.normalize_math(raw) == want


def test_normalize_math_keeps_code_braces() -> None:
    """规范化只作用于公式；代码里的花括号不能被吃掉。"""
    code = 'int main(){ if (a) { return 1; } }'
    assert "{" in quizkit.normalize_math(code)  # 这个函数不负责代码，但也不能吞得没影


def test_normalize_math_does_not_guess() -> None:
    """读不清就是 [?]，绝不猜——猜错比不猜更糟。"""
    assert quizkit.normalize_math("x = [?]") == "x = [?]"


# --- 选项序号 -----------------------------------------------------------


@pytest.mark.parametrize("value, count, want", [
    (0, 4, 0), (3, 4, 3), ("2", 4, 2), ("A", 4, 0), ("b", 4, 1),
    ("（B）", 4, 1), ("选项C", 4, 2), ("D. 105", 4, 3), ("【A】", 4, 0),
    (4, 4, 3),          # 1 起始写法 → 自动纠正为 0 起始
    (9, 4, None), ("E", 4, None), (None, 4, None), (True, 4, None),
])
def test_normalize_index(value, count, want) -> None:
    index, _ = quizkit.normalize_index(value, count)
    assert index == want


def test_normalize_index_explains_correction() -> None:
    index, note = quizkit.normalize_index(4, 4)
    assert index == 3 and "1 起始" in note
    index, note = quizkit.normalize_index(9, 4)
    assert index is None and "超出范围" in note


def test_match_option() -> None:
    options = [{"text": "A. 105"}, {"text": "B. 21"}, {"text": "C. 1050"}]
    assert quizkit.match_option(options, "105")[0] is None      # 有歧义，必须拒绝
    assert quizkit.match_option(options, "B. 21")[0] == 1
    assert quizkit.match_option(options, "1 0 5 0")[0] == 2      # 忽略空白
    assert quizkit.match_option(options, "999")[0] is None


# --- 题干合成 -----------------------------------------------------------


def test_compose_text_keeps_order_and_placeholder() -> None:
    blocks = [
        {"kind": "text", "text": "设 "},
        {"kind": "img", "index": 1, "text": r"y=\ln x"},
        {"kind": "math", "text": r"\frac{dy}{dx}"},
        {"kind": "text", "text": " 等于"},
        {"kind": "img", "index": 2, "text": ""},
    ]
    text = quizkit.compose_text(blocks)
    assert "〔图1: y=ln x〕" in text
    assert "(dy)/(dx)" in text
    # 读不出来的图片也要留占位，不能让模型以为那里什么都没有
    assert "〔图2: 未读出〕" in text


def test_compose_text_does_not_touch_code() -> None:
    blocks = [{"kind": "pre", "text": "int a[2] = {1, 2};"}]
    assert quizkit.compose_text(blocks) == "int a[2] = {1, 2};"


def test_strip_options_removes_duplicated_labels() -> None:
    stem = "2-1 设 x=1，则 y 等于（ ） A. 1 B. 2"
    out = quizkit.strip_options(stem, [(0, "A. 1"), (1, "B. 2")])
    assert "A. 1" not in out and "B. 2" not in out
    assert "设 x=1" in out


def test_fit_budget_keeps_head_and_tail() -> None:
    text = "条件：" + "x" * 500 + "输出格式：整数"
    out = quizkit.fit_budget(text, 120)
    assert out.startswith("条件：")
    assert "输出格式：整数" in out          # 结尾的输出格式必须保住
    assert len(out) < 200


# --- 多图转录结果解析 ---------------------------------------------------


def test_parse_labeled_with_explicit_labels() -> None:
    raw = "题干：设 y=x^2\n选项A：2x\n选项B：x"
    got = quizkit.parse_labeled(raw, ["题干", "选项A", "选项B"])
    assert got["题干"] == "设 y=x^2"
    assert got["选项A"] == "2x"


def test_parse_labeled_falls_back_to_order() -> None:
    raw = "x^2 + 1\n\ndy/dx"
    got = quizkit.parse_labeled(raw, ["题干", "选项A"])
    assert got["题干"] == "x^2 + 1"
    assert got["选项A"] == "dy/dx"


def test_parse_labeled_normalizes_latex() -> None:
    got = quizkit.parse_labeled("题干：$\\frac{1}{x}$", ["题干"])
    assert got["题干"] == "1/x"
