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


def test_eval_without_return_value_does_not_fake_missing_browser(ctx,
                                                                monkeypatch) -> None:
    """回归：脚本没有返回值时，扩展也回 None，别把它当成"扩展不在线"。

    实测（2026-10-16 的 PTA 运行）：agent 反复 ``const el = ...``，第二次同名
    声明在同一个页面上下文里是 SyntaxError，扩展退化成 ``(() => { ... })()``
    求值、返回 undefined，于是连续三步报"当前没有受控浏览器"——牛头不对马嘴，
    还白扔三步。现在改成先看扩展在不在线，再看结果。
    """
    from autopilot import actions as A

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            return None                                  # 扩展答了 null

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    out = build_registry(ctx).get("browser_eval").call(script="const a = 1;")
    assert "没有返回值" in str(out), f"应给出可操作的提示，实际是：{out!r}"


def test_browser_type_auto_finds_editor_and_reads_back(ctx, monkeypatch) -> None:
    """target 留空时自动定位答题编辑器，写完读回**并且内容要真的对得上**。

    实测（2026-10-16 的 PTA 运行）：80 步里有 25 步是 ``browser_eval``，
    大半是模型自己在翻 ``.cm-content`` 找编辑器、写完再求值确认。
    """
    from autopilot import actions as A

    calls = []
    state = {"text": ""}

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            params = params or {}
            calls.append((op, params))
            script = params.get("script", "")
            if op == "type":
                state["text"] = str(params.get("text") or "")
                return {"method": params.get("method"), "length": len(state["text"])}
            if op == "eval":
                if "usable.length" in script:
                    return {"found": True, "tag": "DIV"}       # 找编辑器
                if "walk(" in script:
                    return state["text"]                       # 读回真实内容
                if "monaco" in script:
                    return ""                                  # 没有框架 API 可用
                return {"found": True}
            return {}

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    code = "#include <stdio.h>\nint main(){return 0;}"
    out = build_registry(ctx).get("browser_type").call(text=code)
    assert "读回确认：2 行" in out, f"应自带读回确认，实际：{out!r}"
    typed = [p for op, p in calls if op == "type"]
    assert typed and typed[0]["target"] == "#__ap_editor", "应自动定位到答题编辑器"


def test_browser_type_fails_loudly_and_forbids_ctrl_v(ctx, monkeypatch) -> None:
    """回归（2026-10-22 实测）：扩展返回 OK 但内容是空的，Agent 转头去 Ctrl+V。

    审计日志里的真实轨迹：browser_type OK → eval 发现是空的 → 又试一次 →
    set_clipboard → browser_press("Control+V") → 页面禁粘贴 → 白烧十几步。
    现在"写不进去"必须是**明确的失败**，而且要把 Ctrl+V 这条路直接堵死。
    """
    from autopilot import actions as A

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            params = params or {}
            if op == "type":
                return {"length": len(str(params.get("text") or ""))}   # 送达 ≠ 写进去
            if op == "eval":
                script = params.get("script", "")
                if "usable.length" in script:
                    return {"found": True, "tag": "DIV"}
                if "walk(" in script:
                    return ""                                            # 读回是空的
                if "monaco" in script:
                    return ""                                            # 框架 API 也没有
            return {}

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    with pytest.raises(Exception) as info:
        build_registry(ctx).get("browser_type").call(text="int main(){return 0;}")
    msg = str(info.value)
    assert "写入失败" in msg
    assert "Ctrl+V" in msg and "不要再试" in msg


def test_browser_debug_does_not_hijack_users_current_page(ctx, monkeypatch) -> None:
    """回归：接管浏览器时**不许**把用户正在看的那一页顶掉。

    实测（2026-10-16）：用户说"把这个页面的作业做了"，agent 第一步就是
    ``browser_debug(url="https://pintia.cn/")``——同站的作业页被导航走，
    于是"突然返回到上个界面"；第二次让他做第二个作业时，又从题目集列表里
    挑回了第一个作业。
    """
    from autopilot import actions as A

    calls = []

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            calls.append((op, params or {}))
            if op == "tabs":
                return {"count": 1, "tabs": [{
                    "id": 7, "active": True, "title": "7-2 小于m的最大的10个素数",
                    "url": "https://pintia.cn/problem-sets/1/exam/problems/type/7?x=2"}]}
            return {}

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    reg = build_registry(ctx)

    out = reg.get("browser_debug").call(url="https://pintia.cn/")
    assert "没有跳转" in out, f"同站应明确说明没跳转，实际：{out!r}"
    assert not [1 for op, _ in calls if op == "goto"], "同站不该跳转"

    calls.clear()
    out = reg.get("browser_debug").call(url="https://example.com/other")
    assert "没有动" in out, f"不同站也应说明没动用户的页面，实际：{out!r}"
    assert not [1 for op, _ in calls if op == "goto"], "不同站也不许顶掉用户的页面"


def test_read_image_works_under_extension(ctx, monkeypatch) -> None:
    """回归：扩展接管时 ``browser_read_image`` 不许再报"当前没有受控浏览器"。

    实测（2026-10-16 19:58）：这个老动作只有 Playwright 一条路，而扩展模式下
    根本没有 Playwright 页面，于是每次调用都**瞬间失败**，agent 只能换个选择器
    再撞一次——用户看到的就是"遇到图片就卡在那里"。
    """
    from autopilot import actions as A

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            if op == "eval":
                return '{"found": false}'                # 交给 read_element 去查
            return {}

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    out = build_registry(ctx).get("browser_read_image").call(selector="img", index=0)
    assert "没有找到匹配" in str(out), f"应该走扩展通道，实际：{out!r}"
    assert "没有受控浏览器" not in str(out)


def test_commands_carry_pinned_tab_id(ctx, monkeypatch) -> None:
    """回归：接管之后每条命令都要带**钉住的工作标签页** tabId。

    不给 tabId 时，扩展那边是 ``pickTab()``：用"当前前台窗口的活动标签页"，
    也就是**用户此刻正在看的那一页**。他随手切个标签、开个新网页，agent 的
    eval/type/click/截图就全打偏了（截图还会因为那页被遮挡而挂死）。
    """
    from autopilot import actions as A

    calls = []

    class _FakeBridge:
        port = 1
        connected = True

        def call(self, op, params=None, timeout=None):  # noqa: ANN001
            calls.append((op, params or {}))
            if op == "tabs":
                return {"count": 2, "tabs": [
                    {"id": 11, "active": True, "title": "作业页",
                     "url": "https://pintia.cn/a"},
                    {"id": 22, "active": False, "title": "用户开的新网页",
                     "url": "https://example.com/b"}]}
            if op == "eval":
                return "1"
            return {}

    monkeypatch.setattr(A, "_ext", lambda ctx, wait=5.0: _FakeBridge())
    reg = build_registry(ctx)
    reg.get("browser_debug").call()
    assert ctx.browser.get("ext_tab") == 11, "接管时应钉住活动标签页"

    reg.get("browser_eval").call(script="1+1")
    evals = [p for op, p in calls if op == "eval"]
    assert evals and evals[0].get("tabId") == 11, f"命令必须带 tabId，实际：{evals}"

    # 用户切到别的标签页也不该改变 agent 的工作标签页
    reg.get("browser_status").call()
    assert ctx.browser.get("ext_tab") == 11


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
def test_browser_type_escalates_when_insert_is_blocked(ctx, tmp_path, monkeypatch) -> None:
    """禁粘贴的页面：insertText 静默失效时要自动升级为逐键输入。

    真实场景：某些考试页把 paste / insertText 一并拦掉，动作**不报错**、
    编辑器却还是空的，Agent 会以为"写好了"，然后在提交时才发现没内容。
    """
    from autopilot import actions as A

    page_url = _editor_page(tmp_path)
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-escalate")
    real_reader = A._editor_text
    state = {"calls": 0}

    def fake_reader(page):
        state["calls"] += 1
        return "" if state["calls"] == 1 else real_reader(page)   # 第一次假装没写进去

    monkeypatch.setattr(A, "_editor_text", fake_reader)
    code = "int main(){return 0;}"
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        out = reg.get("browser_type").call(target="#ed", text=code, clear_first=True)
        assert "自动升级" in out, f"应说明已升级通道，实际：{out!r}"
        got = reg.get("browser_eval").call(script="document.querySelector('#ed').value")
        squeeze = lambda text: text.replace(" ", "").replace("\n", "")
        assert squeeze(got) == squeeze(code), got
    finally:
        reg.get("browser_close").call(force=True)


@needs_playwright
def test_browser_read_image_ocrs_formula(ctx, tmp_path) -> None:
    """题目里的公式常是图片，DOM 拿不到文本——OCR 兜底路径要能读出公式。"""
    page_url = _editor_page(tmp_path, formula="f(x)=sqrt(x)+2026")
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-img")
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        out = reg.get("browser_read_image").call(index=0, method="ocr", zoom=3)
        assert "OCR 结果" in out, out
        assert "sqrt" in out, f"没读出公式：{out}"

        # 边界情况要说人话
        assert "越界" in reg.get("browser_read_image").call(index=9)
        assert "没有匹配" in reg.get("browser_read_image").call(selector=".不存在")
    finally:
        reg.get("browser_close").call(force=True)


@needs_playwright
def test_browser_read_image_defaults_to_vision(ctx, tmp_path, monkeypatch) -> None:
    """不指定 method 时走视觉模型，返回里要标明识别方式。

    这里把模型换成假的：真实调用要联网、要 API key，不适合放进测试套件。
    真实效果是手工实测的——同一张"代码 + 公式"图，OCR 行命中 1/12、视觉 11/12。
    """
    from autopilot import actions

    calls: list = []

    def fake_read_image(path, prompt="", workspace=None, **kw):
        calls.append({"path": str(path), "prompt": prompt})
        return {"text": "f(x)=sqrt(x)+2026", "ms": 7.0, "usage": {}}

    monkeypatch.setattr(actions.vision, "read_image", fake_read_image)

    page_url = _editor_page(tmp_path, formula="f(x)=sqrt(x)+2026")
    reg = build_registry(ctx)
    profile = str(tmp_path / "prof-img-vision")
    try:
        reg.get("browser_open").call(url=page_url, headless=True, profile=profile)
        out = reg.get("browser_read_image").call(index=0)
        assert "视觉模型" in out, out
        assert "sqrt" in out
        assert calls, "应该把截图交给视觉模型"
        assert calls[0]["path"].endswith(".png"), "PNG 没有 JPEG 块效应，文字更清楚"
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
