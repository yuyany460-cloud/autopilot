# -*- coding: utf-8 -*-
"""代码写入阶梯测试：**专门模拟"禁止粘贴"的页面**。

PTA 这类考试页会主动拦截粘贴（有的连 beforeinput 一起拦），
所以写入不是"一种方法"，而是一条阶梯：框架 API → CDP 插入 → execCommand → 逐键。
这里用本地 fixture 复现三种页面，验证阶梯真的能逐级降级并在读回校验后收手。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_pta():
    spec = importlib.util.spec_from_file_location("pta_auto", ROOT / "examples" / "pta-auto.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


CODE = "int main(){\n    return 0;\n}"

# 1) 干净的 textarea：应该走 api（原型 setter）
PLAIN = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<textarea id="ed"></textarea></body></html>"""

# 2) 只拦粘贴：execCommand / insert 都能过
PASTE_BLOCKED = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="ed" contenteditable="true"></div>
<script>
const ed = document.getElementById('ed');
ed.addEventListener('paste', e => e.preventDefault());
ed.addEventListener('beforeinput', e => {
  if ((e.inputType || '').toLowerCase().includes('paste')) e.preventDefault();
});
</script></body></html>"""

# 3) 最狠的一种：非键盘输入一律回滚（模拟"检测输入来源"的站点）
KEYBOARD_ONLY = """<!doctype html><html><head><meta charset="utf-8"></head><body>
<div id="ed" contenteditable="true"></div>
<script>
const ed = document.getElementById('ed');
let good = '', keyOk = false;
ed.addEventListener('keydown', () => { keyOk = true; });
ed.addEventListener('paste', e => e.preventDefault());
ed.addEventListener('beforeinput', e => {
  if ((e.inputType || '').toLowerCase().includes('paste')) e.preventDefault();
});
ed.addEventListener('input', () => {
  if (!keyOk) { ed.textContent = good; return; }   // 回滚：不是键盘来的就撤销
  keyOk = false; good = ed.textContent;
});
</script></body></html>"""


@pytest.fixture()
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:      # pragma: no cover
            pytest.skip(f"启动 Edge 失败：{exc}")
        yield b
        b.close()


def _open(browser, tmp_path, html: str):
    path = tmp_path / "page.html"
    path.write_text(html, encoding="utf-8")
    page = browser.new_page()
    page.goto(path.as_uri())
    return page


def test_plain_textarea_uses_framework_api(browser, tmp_path) -> None:
    pta = _load_pta()
    page = _open(browser, tmp_path, PLAIN)
    out = pta.write_code(page, CODE)
    assert out["ok"], out
    assert out["via"] == "api", out["tried"]
    page.close()


def test_paste_blocked_page_still_writable(browser, tmp_path) -> None:
    pta = _load_pta()
    page = _open(browser, tmp_path, PASTE_BLOCKED)
    out = pta.write_code(page, CODE)
    assert out["ok"], f"拦粘贴的页面也应该能写进去：{out}"
    assert out["via"] in {"insert", "exec", "type"}, out
    page.close()


def test_keyboard_only_page_finds_a_working_channel(browser, tmp_path) -> None:
    """非键盘输入会被回滚——阶梯必须自己找到还能用的那条路。

    实测结论：CDP 的 ``Input.insertText`` 在站点看来**就是键盘输入**
    （会带 keydown），所以它连"检测输入来源"的页面都能过；
    ``execCommand`` 与直接改 DOM 则会被回滚。这也说明为什么 insert 排在 exec 前面。
    """
    pta = _load_pta()
    page = _open(browser, tmp_path, KEYBOARD_ONLY)
    out = pta.write_code(page, CODE)
    assert out["ok"], f"应能找到可用通道：{out}"
    assert out["via"] in {"insert", "type"}, f"应落在真实输入通道：{out['tried']}"
    assert any(not ok for _, ok in out["tried"]), "至少应有一档被识别为失败"
    page.close()


def test_forced_typing_works_on_hostile_page(browser, tmp_path) -> None:
    """被判定"输入过快"或 insert 也被拦时，逐键通道必须可用。"""
    pta = _load_pta()
    page = _open(browser, tmp_path, KEYBOARD_ONLY)
    out = pta.write_code(page, CODE, method="type", delay_ms=10)
    assert out["ok"] and out["via"] == "type", out
    page.close()


def test_forced_method_only_runs_that_method(browser, tmp_path) -> None:
    pta = _load_pta()
    page = _open(browser, tmp_path, PLAIN)
    out = pta.write_code(page, CODE, method="insert")
    assert out["ok"] and out["via"] == "insert", out
    assert len(out["tried"]) == 1
    page.close()


def test_failure_reports_actionable_detail(browser, tmp_path) -> None:
    """全都写不进去时要给出可操作的说明，而不是一句报错。"""
    pta = _load_pta()
    page = _open(browser, tmp_path, KEYBOARD_ONLY)
    out = pta.write_code(page, CODE, method="exec")      # 强行用会被回滚的通道
    assert not out["ok"]
    assert "读回不一致" in out["detail"] or "没有生效" in out["detail"], out["detail"]
    assert "逐键" in pta.WRITE_TIPS
    page.close()
