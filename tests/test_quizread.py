# -*- coding: utf-8 -*-
"""读题链路测试：DOM 抽取（含公式元数据）、图片落地、题目合成。

用真实浏览器（Playwright + 系统 Edge/Chrome）跑本地 fixture 页面，
不联网、不碰用户桌面。没有 Playwright 或启动不了浏览器时整组跳过。
"""

from __future__ import annotations

import base64
import importlib.util
from pathlib import Path

import pytest

from autopilot import quizread

# 浏览器相关的用例才需要 Playwright（在 fixture 里 importorskip），
# 纯逻辑用例（答案解析、公式短路）在任何环境下都跑。

# 1x1 PNG：用于验证"图片落地"这条路径
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+2m4AAAAASUVORK5CYII=")
TINY_URI = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()
# 24x14 的图：足够大，不会被"装饰图"过滤掉（<12x8 才算装饰）
OPTION_URI = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABgAAAAOCAIAAAC6mkspAAABLElEQVR4nNVTIauDUBi9E9uSYWFhXVixiz7WBJvFviAKK/4QqwhTEQTzsoJJDbYFGQOxrF24IqhR74Pn2Hvgsy28d9J3v+9wvnP4uCuMMXgHiLeogH8u5Loux3EMw4Rh+MsYLyOKoqIophpCyPP8MAy3242m6Tn56UiSpMvlAgBQFMX3/akZBIFt21ONEDqdTgRB7HY7hNCio8fjwTBMnueCIEydLMssy9J1vWman5s9zzsej3NH39EMw9hut1VVTU9VVeu6TpLENM0XpyzL/X4PIVyMBgDouo4kya7rAABt2/Z9T1EUy7JxHL8Isiw7jrPZbBaj3e93juOu1yvP8+M4zheO4yhJUhAES5d5Ch0OhzRNMcaapp3P5znPdd31ev3xBVEU54TVn/trn3BrYCjiAmcxAAAAAElFTkSuQmCC"

LONG_STEM = "2-1 设 " + "条件说明，" * 80 + "则 dy/dx 等于（　　）"

FIXTURE = f"""<!doctype html><html><head><meta charset="utf-8">
<title>2-1 导数 - 考试</title></head><body>
<div id="1001">
  <div class="stem">{LONG_STEM}
    <img src="{TINY_URI}" alt="y=\\ln x">
    <span class="katex"><annotation encoding="application/x-tex">\\frac{{dy}}{{dx}}</annotation></span>
  </div>
  <label><input type="radio" name="1001"> A. 1/x</label>
  <label><input type="radio" name="1001"> B. 2/x</label>
  <label><input type="radio" name="1001"> C. <img src="{OPTION_URI}"></label>
</div>
<div id="1002"><div>2-2 程序输出是（　　）</div>
  <pre>int a = 1;{{ if (a) {{ return 1; }} }}</pre>
  <label><input type="radio" name="1002"> A. 1</label>
  <label><input type="radio" name="1002"> B. 2</label>
</div>
</body></html>"""


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    playwright = pytest.importorskip("playwright.sync_api")
    path = tmp_path_factory.mktemp("fixture") / "quiz.html"
    path.write_text(FIXTURE, encoding="utf-8")
    with playwright.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:      # pragma: no cover - 环境缺失时跳过
            pytest.skip(f"启动 Edge 失败：{exc}")
        pg = browser.new_page()
        pg.goto(path.as_uri())
        yield pg
        browser.close()


def test_extract_keeps_full_stem(page) -> None:
    data = quizread.extract(page)
    assert data["count"] == 2
    first = data["problems"][0]
    assert first["label"] == "2-1"
    assert first["kind"] == "choice"
    # 早期实现把题干截到 260 字符，长题等于把条件砍掉
    assert len(first["text"]) > 300


def test_extract_uses_dom_math_metadata(page) -> None:
    """KaTeX 的 annotation 里就有 LaTeX，不该再花一次视觉调用。"""
    data = quizread.extract(page)
    kinds = [b["kind"] for b in data["problems"][0]["blocks"]]
    assert "math" in kinds
    math_block = next(b for b in data["problems"][0]["blocks"] if b["kind"] == "math")
    assert "frac" in math_block["text"]

    images = [b for b in data["problems"][0]["blocks"] if b["kind"] == "img"]
    assert images and images[0]["alt"].startswith("y="), "img 的 alt 应被当成公式文本"


def test_option_images_are_not_duplicated_in_stem(page) -> None:
    """选项里的图片只属于选项，不该同时出现在题干里（否则提示词里读两遍）。"""
    data = quizread.extract(page)
    first = data["problems"][0]
    stem_imgs = [b for b in first["blocks"] if b["kind"] == "img"]
    assert len(stem_imgs) == 1, stem_imgs
    assert first["options"][2]["imgs"], "选项 C 的图片应被单独抓到"


def test_pre_block_preserved(page) -> None:
    data = quizread.extract(page)
    second = data["problems"][1]
    pre = [b for b in second["blocks"] if b["kind"] == "pre"]
    assert pre and "return 1" in pre[0]["text"]
    assert "{" in pre[0]["text"], "代码里的花括号必须原样保留"


def test_compose_problem_has_options_and_math(page) -> None:
    data = quizread.extract(page)
    text = quizread.compose_problem(data["problems"][0])
    assert "(dy)/(dx)" in text
    assert "[0] A. 1/x" in text
    assert "〔图1: y=ln x〕" in text


def test_prepare_images_saves_data_uri(page, tmp_path) -> None:
    data = quizread.extract(page)
    # 该图有 alt，属于"DOM 已解决"，不该再下载
    stats = quizread.prepare_images(page, data["problems"], tmp_path)
    assert stats["dom_tex"] >= 1
    # 选项 C 的图片没有 alt，应该被落盘
    saved = [img for opt in data["problems"][0]["options"] for img in opt["imgs"]
             if img.get("path")]
    assert saved and Path(saved[0]["path"]).is_file()


def test_prepare_images_then_compose_marks_unread(page, tmp_path) -> None:
    data = quizread.extract(page)
    quizread.prepare_images(page, data["problems"], tmp_path)
    text = quizread.compose_problem(data["problems"][0])
    # 选项 C 的图还没交给视觉模型，合成时要明说"未读出"，而不是当它不存在
    assert "未读出" in text


# --- pta-auto 的答案解析 -------------------------------------------------


def _load_pta_module():
    path = Path(__file__).resolve().parent.parent / "examples" / "pta-auto.py"
    spec = importlib.util.spec_from_file_location("pta_auto", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


PROBLEMS = [
    {"label": "2-1", "kind": "choice", "options": [{"text": "A. 1"}, {"text": "B. 2"}]},
    {"label": "2-2", "kind": "choice", "options": [{"text": "A. 最近的 if"}, {"text": "B. 最近的 else"}]},
    {"label": "7-1", "kind": "program", "options": []},
]


def test_normalize_answers_accepts_letter_and_text() -> None:
    pta = _load_pta_module()
    raw = [
        {"label": "2-1", "index": "B"},
        {"label": "2-2", "text": "最近的 else"},
        {"label": "7-1", "code": "int main(){return 0;}"},
    ]
    answers, warnings = pta.normalize_answers(raw, PROBLEMS)
    by_label = {a["label"]: a for a in answers}
    assert by_label["2-1"]["index"] == 1
    assert by_label["2-2"]["index"] == 1
    assert by_label["7-1"]["type"] == "program"
    assert warnings == []


def test_normalize_answers_flags_unparsable() -> None:
    pta = _load_pta_module()
    answers, warnings = pta.normalize_answers(
        [{"label": "2-1", "index": 9}, {"label": "9-9", "index": 0}], PROBLEMS)
    assert answers[0]["index"] is None, "越界必须留空，不能瞎填"
    assert any("超出范围" in w for w in warnings)
    assert any("不存在的题号" in w for w in warnings)


def test_prompt_uses_full_stem_not_head() -> None:
    pta = _load_pta_module()
    data = {"count": 1, "problems": [{
        "label": "2-1", "kind": "choice", "options": [{"index": 0, "text": "A. 1"}],
        "blocks": [{"kind": "text", "text": "题干" + "很长" * 400}],
    }]}
    prompt = pta.build_solve_prompt(data, budget=3000)
    assert "很长" * 100 in prompt


# --- 选项下标：字母优先 + 交叉校验 -------------------------------------


def test_pick_index_prefers_letter() -> None:
    pta = _load_pta_module()
    item = {"label": "1-1", "letter": "C", "index": 3}      # 数字是 1 起始写错了
    index, note = pta._pick_index(item, 4)
    assert index == 2, "字母 C 没有歧义，应以字母为准"
    assert "不同选项" in note, "分歧必须报告，不能默默采信一个"


def test_pick_index_agrees_without_warning() -> None:
    pta = _load_pta_module()
    index, note = pta._pick_index({"letter": "C", "index": 2}, 4)
    assert index == 2 and note == ""


def test_pick_index_falls_back_to_index() -> None:
    pta = _load_pta_module()
    index, _ = pta._pick_index({"index": 3}, 4)
    assert index == 3
    index, note = pta._pick_index({"index": 4}, 4)
    assert index == 3 and "1 起始" in note


def test_normalize_answers_uses_letter_cross_check() -> None:
    pta = _load_pta_module()
    problems = [{"label": "1-1", "kind": "choice",
                 "options": [{"text": "A. 1"}, {"text": "B. 2"}, {"text": "C. 3"}]}]
    answers, warnings = pta.normalize_answers([{"label": "1-1", "letter": "B", "index": 0}],
                                              problems)
    assert answers[0]["index"] == 1
    assert warnings and "letter" in warnings[0]


# --- DOM 公式短路：别把文件名当公式 ------------------------------------


@pytest.mark.parametrize("text", ["1/2.png", "a<b.png", "equation_1.png",
                                  "equation_1", "http://x.com/a=1", "一张风景图"])
def test_formula_from_dom_rejects_filenames(text: str) -> None:
    from autopilot.actions import _formula_from_dom

    assert _formula_from_dom(text) == "", f"{text!r} 是文件名/地址或描述文字，不该当公式"


@pytest.mark.parametrize("text", ["1/2", "3/4", "y=\\ln x", "x^2", "f(x)=x+1"])
def test_formula_from_dom_keeps_formulas(text: str) -> None:
    from autopilot.actions import _formula_from_dom

    assert _formula_from_dom(text), f"{text!r} 是公式，应该直接采用"
