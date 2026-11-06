# -*- coding: utf-8 -*-
"""题目文本与答案的规范化工具。

**为什么单独一个模块**：读题→答题这条链路上，真正决定正确率的往往不是
"点得准不准"，而是三件很容易被忽略的小事——

1. **题干必须完整**。早期实现把题干截到 260 字符，长题（尤其带选项说明的
   程序阅读题）等于把关键条件切掉了，答对全靠运气。
2. **公式必须变成文本**。实测同一批题目图片：

   ==================  ==========================================
   Windows OCR          ``设 " ： ln@ + a2 + ） ， 则 迦 等 于 （``
   视觉模型             ``设y = ln(x + √a² + x²)，则dy/dx等于( ).``
   ==================  ==========================================

   OCR 还会把 ``sin²x`` 读成 ``sin2x``、把 ``dy/dx`` 读成 ``迦``——
   这种题不是"可能错"，而是必错。所以读图结果必须再规范化一遍：
   统一 LaTeX 与 Unicode 符号，去掉模型爱加的 ``$`` 和代码块。
3. **"第几个选项"要能被可靠解析**。模型可能给 ``0``/``1``/``A``/``（B）``/
   整段选项文字，PTA 的 index 是 0 起始，错一位就全错。

这些全是纯字符串处理，所以集中在这里、脱离浏览器和网络单独测。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------
# 数学文本规范化
# --------------------------------------------------------------------------

# LaTeX 命令 → 普通文本。**顺序有意义**：先长后短（\leq 先于 \le）。
_LATEX_SYMBOLS: list[tuple[str, str]] = [
    (r"\leqslant", "≤"), (r"\geqslant", "≥"), (r"\leq", "≤"), (r"\geq", "≥"),
    (r"\neq", "≠"), (r"\ne", "≠"), (r"\approx", "≈"), (r"\equiv", "≡"),
    (r"\cdot", "·"), (r"\times", "×"), (r"\div", "÷"), (r"\pm", "±"),
    (r"\mp", "∓"), (r"\infty", "∞"), (r"\partial", "∂"), (r"\nabla", "∇"),
    (r"\to", "→"), (r"\rightarrow", "→"), (r"\leftarrow", "←"),
    (r"\Rightarrow", "⇒"), (r"\Leftrightarrow", "⇔"),
    (r"\in", "∈"), (r"\notin", "∉"), (r"\subset", "⊂"), (r"\cup", "∪"),
    (r"\cap", "∩"), (r"\forall", "∀"), (r"\exists", "∃"),
    (r"\int", "∫"), (r"\sum", "∑"), (r"\prod", "∏"),
    (r"\pi", "π"), (r"\alpha", "α"), (r"\beta", "β"), (r"\gamma", "γ"),
    (r"\delta", "δ"), (r"\epsilon", "ε"), (r"\theta", "θ"), (r"\lambda", "λ"),
    (r"\mu", "μ"), (r"\rho", "ρ"), (r"\sigma", "σ"), (r"\tau", "τ"),
    (r"\phi", "φ"), (r"\omega", "ω"), (r"\Delta", "Δ"), (r"\Omega", "Ω"),
    (r"\sinh", "sinh"), (r"\cosh", "cosh"), (r"\tanh", "tanh"),
    (r"\sin", "sin"), (r"\cos", "cos"), (r"\tan", "tan"), (r"\cot", "cot"),
    (r"\sec", "sec"), (r"\csc", "csc"), (r"\arcsin", "arcsin"),
    (r"\arccos", "arccos"), (r"\arctan", "arctan"),
    (r"\ln", "ln"), (r"\log", "log"), (r"\lg", "lg"), (r"\exp", "exp"),
    (r"\lim", "lim"), (r"\max", "max"), (r"\min", "min"),
    (r"\left", ""), (r"\right", ""), (r"\displaystyle", ""),
    (r"\quad", " "), (r"\qquad", "  "), (r"\,", ""), (r"\;", ""),
    (r"\!", ""), (r"\ ", " "),
]

# Unicode 里长得像数学符号但会让模型"读歪"的字符，统一成 ASCII 或标准符号。
_CHAR_MAP = {
    "\u2212": "-",   # MINUS SIGN（OCR 常把减号吐成这个）
    "\u2013": "-", "\u2014": "-", "\u2015": "-",
    "\u00a0": " ", "\u3000": " ", "\u200b": "",
    "\uff0b": "+", "\uff0d": "-", "\uff0a": "*", "\uff0f": "/",
    "\uff1d": "=", "\uff1c": "<", "\uff1e": ">", "\uff5c": "|",
    "\uff08": "(", "\uff09": ")", "\uff3b": "[", "\uff3d": "]",
    "\uff5b": "{", "\uff5d": "}",
    "\uff10": "0", "\uff11": "1", "\uff12": "2", "\uff13": "3", "\uff14": "4",
    "\uff15": "5", "\uff16": "6", "\uff17": "7", "\uff18": "8", "\uff19": "9",
    "\u222b": "∫",
}

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
_CJK_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9+-]*\s*|\s*```\s*$")


def normalize_math(text: str) -> str:
    """把视觉模型/OCR 的输出规范成干净的一行行文本。

    做四件事：去掉代码块与 ``$``、LaTeX 命令转普通符号、统一 Unicode
    数学字符、压掉多余空白。**不做**任何"猜测性修正"（比如把 0 改成 9）——
    猜错比不猜更糟。
    """
    if not text:
        return ""
    out = str(text)
    out = _FENCE.sub("", out)
    out = out.replace("$", "").replace("\\(", "").replace("\\)", "")
    out = out.replace("\\[", "").replace("\\]", "")

    # 先处理 \frac / \sqrt 这类带参数的，再处理单符号
    out = _expand_latex(out)
    for src, dst in _LATEX_SYMBOLS:
        out = out.replace(src, dst)

    # ^{...} / _{...} → ^(...) / _(...)，单字符的去掉花括号
    out = re.sub(r"\^\{([^{}]{1,40})\}", r"^(\1)", out)
    out = re.sub(r"_\{([^{}]{1,40})\}", r"_(\1)", out)
    out = re.sub(r"\^\{([^{}])\}", r"^\1", out)
    out = re.sub(r"_\{([^{}])\}", r"_\1", out)
    # \text{…} / \mathrm{…} 这类只起排版作用，保留花括号里的内容
    out = re.sub(r"\\(?:text|textrm|mathrm|mathbf|mathit|mathsf|operatorname)\s*"
                 r"\{([^{}]*)\}", r"\1", out)
    # **不要**无差别删除花括号：代码题面里的 `int main(){}` 也是花括号，
    # 一律删掉会把题面改成语义不同的东西（实测 `if (a) { return 1; }` 会变成
    # `if (a) return 1;`）。只处理上面这些明确的 LaTeX 用法。

    for src, dst in _CHAR_MAP.items():
        out = out.replace(src, dst)

    out = _CJK_SPACE.sub("", out)
    out = _MULTI_SPACE.sub(" ", out)
    out = "\n".join(line.strip() for line in out.splitlines())
    return _BLANK_LINES.sub("\n\n", out).strip()


def _take_group(text: str, start: int) -> tuple[str | None, int]:
    """从 ``start`` 处的 ``{`` 开始取一整组（支持任意层嵌套），返回 ``(内容, 组后位置)``。"""
    if start >= len(text) or text[start] != "{":
        return None, start
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
    return None, start


def _expand_latex(text: str) -> str:
    """展开 ``\\frac`` / ``\\sqrt``，支持任意层嵌套。

    不用正则做这件事：``\\frac{-b\\pm\\sqrt{b^2-4ac}}{2a}`` 里花括号是嵌套的，
    ``[^{}]*`` 这类写法要么匹配不上（原文原样留一堆反斜杠），要么把结构切坏
    （实测会得到 ``x=\\frac-b±√(b^2-4ac)2a`` 这种比不转还糟的东西）。
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "\\":
            out.append(text[i])
            i += 1
            continue
        match = re.match(r"\\(dfrac|tfrac|frac|sqrt)\b\s*", text[i:])
        if not match:
            out.append(text[i])
            i += 1
            continue
        name = match.group(1)
        j = i + match.end()
        if name == "sqrt":
            root = ""
            if j < n and text[j] == "[":
                end = text.find("]", j)
                if end > 0:
                    root, j = text[j + 1:end], end + 1
            body, j2 = _take_group(text, j)
            if body is None:
                out.append(text[i])
                i += 1
                continue
            inner = _expand_latex(body).strip()
            out.append(f"{root}√({inner})" if root else f"√({inner})")
            i = j2
            continue

        numerator, j1 = _take_group(text, j)
        if numerator is None:
            out.append(text[i])
            i += 1
            continue
        denominator, j2 = _take_group(text, j1)
        if denominator is None:
            out.append(text[i])
            i += 1
            continue
        num = _expand_latex(numerator).strip()
        den = _expand_latex(denominator).strip()
        if len(num) > 1:
            num = f"({num})"
        if len(den) > 1:
            den = f"({den})"
        out.append(f"{num}/{den}")
        i = j2
    return "".join(out)


# --------------------------------------------------------------------------
# 题干合成
# --------------------------------------------------------------------------


def compose_text(blocks: Sequence[dict[str, Any]], image_marker: str = "〔图{i}: {text}〕") -> str:
    """把"文本块 / 公式块 / 预格式块 / 图片块"按原顺序合成题干。

    ``blocks`` 形如 ``[{"kind": "text", "text": ...}, {"kind": "math", "text": "\\frac{dy}{dx}"},
    {"kind": "img", "index": 1, "text": "（视觉转录结果）"}]``。

    * ``math`` 块（来自 KaTeX/MathJax 的 ``annotation``）规范化后**内联**进正文——
      它本来就是句子的一部分，独立成行会让模型读成两句话。
    * ``pre`` 块（代码）原样保留：绝不对代码做数学符号规范化，否则 ``{}`` 会被吃掉。
    * 图片没读到内容时留 ``〔图1: 未读出〕`` 占位——**必须留**，
      否则模型会以为那里什么都没有，直接按缺条件的题干作答。
    """
    parts: list[str] = []
    for block in blocks or []:
        kind = str(block.get("kind") or "text")
        text = str(block.get("text") or "").strip()
        if kind == "img":
            idx = block.get("index") or (len(parts) + 1)
            body = normalize_math(text) if text else "未读出"
            parts.append(image_marker.format(i=idx, text=body))
        elif kind == "math":
            tex = normalize_math(text)
            if not tex:
                continue
            if parts:
                parts[-1] = parts[-1] + f" {tex} "
            else:
                parts.append(f" {tex} ")
        else:
            if text:
                parts.append(text)
    joined = "\n".join(parts)
    return _BLANK_LINES.sub("\n\n", joined).strip()


def strip_options(stem: str, options: Iterable[tuple[int, str]]) -> str:
    """从题干里去掉与选项重复的文字。

    PTA 的题目容器把题干和选项放在同一个 div 里，直接取 ``innerText``
    会让每道题的选项在 prompt 里出现两遍——白烧 token，还容易让模型
    把"选项里出现的数字"当成题干条件。
    """
    out = str(stem or "")
    for _, text in options or []:
        clean = str(text or "").strip()
        if len(clean) < 2:
            continue
        idx = out.find(clean)
        if idx >= 0:
            out = out[:idx] + out[idx + len(clean):]
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def fit_budget(text: str, budget: int, keep_tail: float = 0.3) -> str:
    """按字符预算裁剪，但**保住开头和结尾**。

    题目条件通常写在开头，而"输出格式/数据范围"写在结尾——只截前面
    （早期实现就是 ``[:1600]``）会把输出格式丢掉，编程题因此大面积格式错。
    """
    text = str(text or "")
    budget = max(80, int(budget))
    if len(text) <= budget:
        return text
    tail = max(20, int(budget * max(0.0, min(1.0, keep_tail))))
    head_len = budget - tail - 8
    return f"{text[:head_len]}\n……（中间省略 {len(text) - head_len - tail} 字）……\n{text[-tail:]}"


# --------------------------------------------------------------------------
# 视觉转录结果解析
# --------------------------------------------------------------------------

_LABEL_LINE = re.compile(
    r"^\s*(?:[【\[]?)(?P<label>题干图?\d*|选项\s*[A-Ha-h]|[A-H](?=[：:])|[A-H])\s*(?:[】\]]?)\s*[：:]\s*(?P<body>.+)$")


def parse_labeled(text: str, labels: Sequence[str] | None = None) -> dict[str, str]:
    """解析"标签：内容"式的多图转录结果。

    视觉模型对"一次读多张图"的回答格式并不总是稳定，常见三种：每条一行
    ``选项A：…``、用空行分段、或者干脆只有内容。这里按标签匹配，
    匹配不到行内标签时按出现顺序回退。
    """
    out: dict[str, str] = {}
    if not text:
        return out
    current = ""
    buffer: dict[str, list[str]] = {}
    for raw_line in str(text).replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = _LABEL_LINE.match(line)
        if match:
            current = canon_label(match.group("label"))
            buffer.setdefault(current, []).append(match.group("body").strip())
            continue
        if current:
            buffer.setdefault(current, []).append(line)
    for key, lines in buffer.items():
        body = "\n".join(lines).strip()
        if body:
            out[key] = normalize_math(body)

    if out or not labels:
        return out

    # 完全没有标签：按顺序把非空段落分给 labels
    chunks = [c.strip() for c in re.split(r"\n\s*\n", str(text)) if c.strip()]
    for label, chunk in zip(labels, chunks):
        out[canon_label(label)] = normalize_math(chunk)
    return out


def canon_label(label: str) -> str:
    text = str(label or "").strip()
    text = re.sub(r"^[【\[]|[】\]]$", "", text)
    text = text.replace(" ", "")
    if text.startswith("选项"):
        return "选项" + text[2:3].upper()
    if re.fullmatch(r"[A-Ha-h]", text):
        return "选项" + text.upper()
    return text


# --------------------------------------------------------------------------
# 答案序号解析
# --------------------------------------------------------------------------


def normalize_index(value: Any, count: int) -> tuple[int | None, str]:
    """把模型给的"哪个选项"解析成 0 起始下标。

    返回 ``(index, 说明)``；解析不出或越界时 index 为 ``None``，说明里写清原因。
    支持：``0`` / ``1``（自动判断是 0 还是 1 起始）/ ``"A"`` / ``"（B）"`` /
    ``"选项C"`` / ``"C. 105"``。

    **1 起始的自动纠正只在不歧义时做**：``count=4`` 时 ``3`` 既可能是第 3 个
    （0 起始）也可能是第 3 个（1 起始）——这种一律按 0 起始，因为 PTA 的
    ``index`` 就是 0 起始，而模型在拿不准时给 1 起始的情况已被 prompt 排除。
    """
    if value is None or value == "":
        return None, "没有给出选项序号"
    if isinstance(value, bool):
        return None, f"布尔值不是选项序号：{value!r}"
    if isinstance(value, (int, float)):
        return _index_from_int(int(value), count)

    text = str(value).strip()
    if not text:
        return None, "选项序号是空字符串"
    if re.fullmatch(r"[+-]?\d+", text):
        return _index_from_int(int(text), count)

    # 字母写法：A / a / (B) / 【C】 / 选项D / D. 105
    match = re.match(r"^[\s（(【\[]*(?:选项|答案|answer)?\s*([A-Ha-h])[\s）)】\]]*"
                     r"(?:[.．、,，:：)].*)?$", text)
    if match:
        idx = ord(match.group(1).upper()) - ord("A")
        if idx < count:
            return idx, ""
        return None, f"选项 {match.group(1).upper()} 超出本题选项数（{count} 个）"

    return None, f"看不懂的选项序号：{text[:40]!r}"


def _index_from_int(index: int, count: int) -> tuple[int | None, str]:
    if 0 <= index < count:
        return index, ""
    if 1 <= index <= count and count >= 2:
        return index - 1, f"按 1 起始纠正为 {index - 1}"
    return None, f"选项序号 {index} 超出范围 0~{max(0, count - 1)}"


def match_option(options: Sequence[Any], hint: Any) -> tuple[int | None, str]:
    """按选项文字片段找下标：先用原样包含，再用去空白的包含。"""
    needle = str(hint or "").strip()
    if not needle:
        return None, "没有给出选项文字"
    texts = [_option_text(o) for o in options]
    for i, text in enumerate(texts):
        if text.strip() == needle:
            return i, ""
    hits = [i for i, text in enumerate(texts) if needle in text]
    if len(hits) == 1:
        return hits[0], ""
    if len(hits) > 1:
        return None, f"选项文字 {needle[:20]!r} 匹配到 {len(hits)} 个选项，请写得更具体"
    compact = re.sub(r"\s+", "", needle)
    hits = [i for i, text in enumerate(texts) if compact and compact in re.sub(r"\s+", "", text)]
    if len(hits) == 1:
        return hits[0], ""
    return None, f"选项里找不到 {needle[:20]!r}"


def _option_text(option: Any) -> str:
    if isinstance(option, dict):
        return str(option.get("text") or "")
    return str(option or "")


__all__ = [
    "canon_label", "compose_text", "fit_budget", "match_option", "normalize_index",
    "normalize_math", "parse_labeled", "strip_options",
]
