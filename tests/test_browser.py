# -*- coding: utf-8 -*-
"""浏览器动作测试。

重点是那次真实踩坑：**句柄还在内存里，但浏览器进程已经死了。**
原先只判断 `ctx.browser.get("page")` 存不存在，于是浏览器被关掉之后，
每个 `browser_*` 都秒报 TargetClosedError，Agent 却以为"浏览器在运行"，
把剩下的步数全烧光。现在有 `_browser_alive` / `_browser_call` 统一兜底。

不碰真实屏幕：真实浏览器测试一律 headless；需要弹出可见窗口的 CDP 测试
标记为 ``hardware``，可用 ``-m "not hardware"`` 跳过。
"""

from __future__ import annotations

import importlib.util
import socket

import pytest

from autopilot.actions import (_browser_alive, _browser_call, _browser_kind,
                               _cdp_ready, _page, _profile_in_use, build_registry)

HAS_PLAYWRIGHT = importlib.util.find_spec("playwright") is not None
needs_playwright = pytest.mark.skipif(not HAS_PLAYWRIGHT,
                                      reason="未安装 playwright")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _editor_page(tmp_path, formula: str = "") -> str:
    """造一个本地页面：一个 textarea（模拟代码编辑器）+ 可选的一张公式图。

    用本地文件而不是 data URL，避免嵌套转义把测试自己搞晕。
    """
    import urllib.parse

    img = ""
    if formula:
        svg = ("<svg xmlns='http://www.w3.org/2000/svg' width='560' height='100'>"
               "<rect width='100%' height='100%' fill='white'/>"
               "<text x='12' y='70' font-size='60' font-family='Arial' fill='black'>"
               f"{formula}</text></svg>")
        img = ("<p><img id='formula' alt='公式图片' "
               f"src='data:image/svg+xml;utf8,{urllib.parse.quote(svg)}'></p>")
    html = ("<!doctype html><meta charset='utf-8'><body>"
            "<textarea id='ed' style='width:640px;height:130px'></textarea>"
            f"{img}</body>")
    path = tmp_path / "editor.html"
    path.write_text(html, encoding="utf-8")
    return path.as_uri()


# --------------------------------------------------------------------------
# 无浏览器时的行为
# --------------------------------------------------------------------------


def test_kind_and_alive_on_empty_context(ctx) -> None:
    assert _browser_kind(ctx) == ""
    assert _browser_alive(ctx) is False
    assert not ctx.browser


def test_page_error_points_at_browser_debug(ctx) -> None:
    """错误信息必须告诉 Agent 下一步该做什么，而不是只说"没启动"。"""
    with pytest.raises(RuntimeError) as info:
        _page(ctx)
    msg = str(info.value)
    assert "browser_debug" in msg
    assert "登录" in msg


def test_browser_status_without_browser(ctx) -> None:
    out = build_registry(ctx).get("browser_status").call()
    assert "没有受控浏览器" in out and "browser_debug" in out


def test_browser_close_without_browser(ctx) -> None:
    assert "未在运行" in build_registry(ctx).get("browser_close").call()


def test_cdp_ready_false_on_dead_port() -> None:
    assert _cdp_ready(f"http://127.0.0.1:{_free_port()}", timeout=0.5) is False


def test_profile_in_use_false_for_unused_dir(tmp_path) -> None:
    """没人用的配置目录不该被误判成占用。"""
    d = tmp_path / "some-profile"
    d.mkdir()
    assert _profile_in_use(d) is False
    assert _profile_in_use(tmp_path / "根本不存在") is False


def test_browser_open_refuses_busy_profile(ctx, tmp_path, monkeypatch) -> None:
    """配置目录被占用时，必须给出准确原因，而不是那句莫名其妙的
    "Target page, context or browser has been closed"。"""
    monkeypatch.setattr("autopilot.actions._profile_in_use", lambda p: True)
    reg = build_registry(ctx)
    with pytest.raises(RuntimeError) as info:
        reg.get("browser_open").call(url="about:blank", headless=True,
                                     profile=str(tmp_path / "p"))
    msg = str(info.value)
    assert "配置目录正在被另一个浏览器占用" in msg
    assert "browser_goto" in msg and "browser_debug" in msg


# --------------------------------------------------------------------------
# 失效句柄的识别与清理（核心回归）
# --------------------------------------------------------------------------


class _ClosedPage:
    def is_closed(self) -> bool:
        return True


class _DyingPage:
    """is_closed() 还说活着，但一操作就报 TargetClosedError。真实场景就是这样。"""

    def is_closed(self) -> bool:
        return False

    def goto(self, *args, **kwargs):
        raise RuntimeError("Page.goto: Target page, context or browser has been closed")


def test_closed_page_is_detected_and_state_cleared(ctx) -> None:
    ctx.browser.update({"page": _ClosedPage(), "mode": "cdp"})
    with pytest.raises(RuntimeError, match="已经关闭"):
        _page(ctx)
    assert not ctx.browser, "失效句柄必须被清掉，否则会一直撞同一面墙"


def test_target_closed_mid_call_is_translated(ctx) -> None:
    ctx.browser.update({"page": _DyingPage(), "mode": "cdp"})
    with pytest.raises(RuntimeError) as info:
        _browser_call(ctx, lambda page: page.goto("https://x"), "browser_goto")
    msg = str(info.value)
    assert "browser_goto" in msg
    assert "已清除失效状态" in msg
    assert "browser_debug" in msg
    assert not ctx.browser


def test_other_errors_are_not_swallowed(ctx) -> None:
    """普通业务错误要原样抛出，不能被当成"浏览器关了"糊掉。"""

    class _BadPage:
        def is_closed(self) -> bool:
            return False

        def click(self) -> None:
            raise RuntimeError("Timeout 15000ms exceeded")

    ctx.browser.update({"page": _BadPage(), "mode": "cdp"})
    with pytest.raises(RuntimeError, match="Timeout"):
        _browser_call(ctx, lambda page: page.click(), "browser_click")
    assert ctx.browser, "普通错误不该清空浏览器状态"


# --------------------------------------------------------------------------
# 真实浏览器（headless，不干扰使用者）
# --------------------------------------------------------------------------


@needs_playwright
def test_headless_browser_roundtrip(ctx) -> None:
    reg = build_registry(ctx)
    try:
        out = reg.get("browser_open").call(
            url="data:text/html,<h1>hello autopilot</h1>", headless=True)
        assert "已启动" in out
        assert _browser_kind(ctx) == "persistent"
        assert _browser_alive(ctx)

        assert "hello autopilot" in reg.get("browser_get_text").call()
        assert reg.get("browser_eval").call(
            script="document.querySelector('h1').innerText") == "hello autopilot"
        status = reg.get("browser_status").call()
        assert "独立持久化上下文" in status
        assert "打开标签页" in status
    finally:
        reg.get("browser_close").call(force=True)
    assert not ctx.browser


@needs_playwright
def test_real_browser_death_is_recovered(ctx) -> None:
    """真实复现 PTA 那次的失败：浏览器进程没了，句柄还在。

    期望：报可读错误 + 自动清理，而不是后面每一步都重复 TargetClosedError。
    """
    reg = build_registry(ctx)
    reg.get("browser_open").call(
        url="data:text/html,<h1>alive</h1>", headless=True)
    assert reg.get("browser_eval").call(script="1+1") == 2

    # 从底层把浏览器掐掉，但 ctx.browser 里的句柄原封不动
    ctx.browser["context"].close()

    with pytest.raises(RuntimeError) as info:
        reg.get("browser_eval").call(script="1+1")
    assert "已经关闭" in str(info.value)
    assert not ctx.browser, "必须自动清理，否则后续每一步都是同样的失败"

    # 清理之后应该给出"没有受控浏览器"的指引，而不是继续报 TargetClosed
    assert "没有受控浏览器" in reg.get("browser_status").call()

    # 而且能重新建立连接
    reg.get("browser_open").call(url="data:text/html,<h1>back</h1>", headless=True)
    assert reg.get("browser_eval").call(
        script="document.querySelector('h1').innerText") == "back"
    reg.get("browser_close").call(force=True)


@needs_playwright
def test_browser_type_writes_into_editor_and_reads_back(ctx, tmp_path) -> None:
    """往网页代码编辑器写内容。

    这是第二次真实踩坑：PTA 的代码编辑器不是普通输入框，
    `browser_fill` 和 JS 的 `setValue` 都静默失效，必须走真实键盘事件。
    """
    page_url = _editor_page(tmp_path)
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-type")
    code = 'int main(){int x;scanf("%d",&x);printf("%.2f",x>=0?sqrt(x):0);return 0;}'
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        reg.get("browser_type").call(target="#ed", text=code, clear_first=True)
        got = reg.get("browser_eval").call(
            script="document.querySelector('#ed').value")
        assert got == code, "编辑器内容与写入不一致"
    finally:
        reg.get("browser_close").call(force=True)


@needs_playwright
def test_browser_type_clear_first_replaces_old_text(ctx, tmp_path) -> None:
    page_url = _editor_page(tmp_path)
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-clear")
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        reg.get("browser_type").call(target="#ed", text="旧内容", clear_first=True)
        reg.get("browser_type").call(target="#ed", text="新内容", clear_first=True)
        assert reg.get("browser_eval").call(
            script="document.querySelector('#ed').value") == "新内容"
    finally:
        reg.get("browser_close").call(force=True)


@needs_playwright
def test_browser_read_image_ocrs_formula(ctx, tmp_path) -> None:
    """题目里的公式常是图片，DOM 拿不到文本，得靠放大截图 + OCR。"""
    page_url = _editor_page(tmp_path, formula="f(x)=sqrt(x)+2026")
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-img")
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        out = reg.get("browser_read_image").call(index=0, zoom=3)
        assert "OCR 结果" in out, out
        assert "sqrt" in out, f"没读出公式：{out}"

        # 边界情况要说人话
        assert "越界" in reg.get("browser_read_image").call(index=9)
        assert "没有匹配" in reg.get("browser_read_image").call(selector=".不存在")
    finally:
        reg.get("browser_close").call(force=True)


@needs_playwright
@pytest.mark.hardware
def test_browser_debug_cdp_roundtrip(ctx) -> None:
    """CDP 接管真实 Edge（会弹出可见窗口，所以标记为 hardware）。

    这是"带登录态操作需要登录的网站"的完整路径。
    """
    port = _free_port()
    reg = build_registry(ctx)
    try:
        out = reg.get("browser_debug").call(
            url="data:text/html,<h1>cdp ok</h1>", port=port)
        assert "已接管调试浏览器" in out
        assert _browser_kind(ctx) == "cdp"
        assert reg.get("browser_eval").call(
            script="document.querySelector('h1').innerText") == "cdp ok"
        assert "调试端口直连" in reg.get("browser_status").call()
    finally:
        reg.get("browser_close").call(force=True)
        import subprocess
        ps = ("Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" | "
              f"Where-Object {{ $_.CommandLine -like '*{port}*' }} | "
              "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)


# --------------------------------------------------------------------------
# 提示词
# --------------------------------------------------------------------------


def test_prompt_explains_login_choice() -> None:
    """提示词必须明确"要登录的网站用 browser_debug"，否则 Agent 还会走错路。"""
    from autopilot.prompts import SYSTEM_PROMPT

    assert "browser_debug" in SYSTEM_PROMPT
    assert "需要登录的网站" in SYSTEM_PROMPT
    assert "ask_human" in SYSTEM_PROMPT
    assert "browser_open" in SYSTEM_PROMPT
