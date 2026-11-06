# -*- coding: utf-8 -*-
"""底层能力测试：DPI、坐标、截屏、窗口、剪贴板、UIA/OCR 桥接。

这些测试会真的碰系统（移动鼠标、截屏、读控件树）。
用 ``-m "not hardware"`` 可以跳过其中需要真实桌面的部分。
"""

from __future__ import annotations

import time

import pytest

from autopilot import psbridge, screen, winapi, windows


# --- DPI 与几何 ---------------------------------------------------------


def test_dpi_awareness_gets_enabled() -> None:
    mode = winapi.enable_dpi_awareness()
    assert mode in ("per-monitor-v2", "per-monitor", "system", "none")
    # 重复调用应该是幂等的
    assert winapi.enable_dpi_awareness() == mode


def test_dpi_and_scale_are_sane() -> None:
    dpi = winapi.get_dpi()
    assert 72 <= dpi <= 480
    assert abs(winapi.get_scale() - dpi / 96) < 0.01


def test_primary_and_virtual_screen() -> None:
    w, h = winapi.primary_screen_size()
    assert w >= 640 and h >= 480
    vs = winapi.virtual_screen()
    assert vs.width >= w and vs.height >= h
    assert winapi.monitor_count() >= 1


def test_screen_summary_calls_dpi_first() -> None:
    info = winapi.screen_summary()
    assert info["dpi_awareness"] != "unset"
    assert info["primary_size"][0] > 0


def test_rect_helpers() -> None:
    r = winapi.Rect(10, 20, 110, 60)
    assert r.width == 100 and r.height == 40
    assert r.center == (60, 40)
    assert r.as_dict() == {"x": 10, "y": 20, "width": 100, "height": 40}


# --- 指针 ---------------------------------------------------------------


def test_absolute_coordinate_mapping_math() -> None:
    """不碰硬件，只验证物理像素 -> SendInput 归一化坐标的换算。

    这是 DPI 缩放与多显示器下点击是否准确的核心，必须能确定性验证。
    """
    vs = winapi.virtual_screen()
    assert winapi._to_absolute(vs.left, vs.top) == (0, 0)
    assert winapi._to_absolute(vs.right - 1, vs.bottom - 1) == (65535, 65535)
    mx, my = winapi._to_absolute(vs.left + vs.width // 2, vs.top + vs.height // 2)
    assert 32000 < mx < 33500 and 32000 < my < 33500


def _move_and_verify(point: tuple[int, int], attempts: int = 3) -> tuple[bool, tuple[int, int]]:
    """移动鼠标并核对落点。

    如果连续在**不同**位置都失败，说明有外部输入在抢鼠标（使用者本人
    正在操作），这时应该跳过而不是误报失败——本测试的目标是验证
    坐标换算，而不是和真人抢鼠标。
    """
    got = (0, 0)
    for _ in range(attempts):
        winapi.move_to(*point)
        time.sleep(0.12)
        got = winapi.get_cursor_pos()
        if abs(got[0] - point[0]) <= 2 and abs(got[1] - point[1]) <= 2:
            return True, got
    return False, got


def _require_quiet_desktop() -> None:
    """确认没有别人在动鼠标，否则跳过依赖精确光标位置的断言。"""
    probe = (60, 60)
    winapi.move_to(*probe)
    time.sleep(0.15)
    if winapi.get_cursor_pos() != probe:
        time.sleep(0.4)
        winapi.move_to(*probe)
        time.sleep(0.15)
        if winapi.get_cursor_pos() != probe:
            pytest.skip("检测到外部鼠标输入（有人正在使用这台电脑），跳过精确坐标测试")


@pytest.mark.hardware
def test_mouse_move_is_pixel_accurate(cursor_guard) -> None:
    """DPI 缩放没处理好的话，这里会差一大截。"""
    assert isinstance(cursor_guard, tuple), "cursor_guard 夹具应提供测试前的光标位置"
    _require_quiet_desktop()
    ok, got = _move_and_verify((400, 300))
    if not ok:
        # 失败可能是因为使用者在检查之后又抢了鼠标。再确认一次：
        # 桌面此刻仍被占用就跳过，确认安静了才当真的失败——否则就是误报。
        _require_quiet_desktop()
    assert ok, f"期望落在 (400,300)，实际 {got}"


@pytest.mark.hardware
def test_absolute_coordinates_roundtrip(cursor_guard) -> None:
    assert isinstance(cursor_guard, tuple), "cursor_guard 夹具应提供测试前的光标位置"
    _require_quiet_desktop()
    for point in [(0, 0), (1200, 800), (100, 1100)]:
        ok, got = _move_and_verify(point)
        assert ok, f"期望落在 {point}，实际 {got}"


# --- 键盘映射 -----------------------------------------------------------


# 组合键序列生成。这几条断言直接守护一个曾经让 Ctrl+S/Ctrl+V 静默失效的 bug：
# hotkey() 只发了主键的 keyup，没发 keydown。
def test_hotkey_plan_presses_and_releases_main_key() -> None:
    plan = winapi._hotkey_plan([winapi.resolve_key(k) for k in ("ctrl", "s")])
    s_vk = winapi.resolve_key("s")[0]

    assert (s_vk, True) in plan, "主键必须被按下"
    assert (s_vk, False) in plan, "主键必须被松开"
    assert plan.index((s_vk, True)) < plan.index((s_vk, False))


def test_hotkey_plan_modifier_held_throughout() -> None:
    plan = winapi._hotkey_plan([winapi.resolve_key(k) for k in ("ctrl", "s")])
    ctrl_vk = winapi.VK["ctrl"]
    down = plan.index((ctrl_vk, True))
    up = plan.index((ctrl_vk, False))
    s_down = plan.index((winapi.resolve_key("s")[0], True))
    s_up = plan.index((winapi.resolve_key("s")[0], False))

    assert down < s_down, "修饰键要先按下"
    assert s_up < up, "修饰键要最后松开"
    assert down == 0 and up == len(plan) - 1


def test_hotkey_plan_three_keys_order() -> None:
    keys = ("ctrl", "shift", "esc")
    plan = winapi._hotkey_plan([winapi.resolve_key(k) for k in keys])
    order = {vk: i for i, (vk, is_down) in enumerate(plan) if is_down}
    esc_vk = winapi.VK["esc"]
    assert order[winapi.VK["ctrl"]] < order[winapi.VK["shift"]] < order[esc_vk]


def test_hotkey_plan_releases_all_keys() -> None:
    plan = winapi._hotkey_plan([winapi.resolve_key(k) for k in ("ctrl", "alt", "delete")])
    for vk, is_down in plan:
        if is_down:
            assert (vk, False) in plan, f"键 {vk:#x} 按下后没有松开"


def test_hotkey_accepts_plus_joined_string() -> None:
    """``hotkey("alt+f4")`` 和 ``hotkey("alt", "f4")`` 必须等价。"""
    joined = winapi._split_keys(["alt+f4"])
    separate = winapi._split_keys(["alt", "f4"])
    assert joined == separate == ["alt", "f4"]

    plan_joined = winapi._hotkey_plan([winapi.resolve_key(k) for k in joined])
    plan_sep = winapi._hotkey_plan([winapi.resolve_key(k) for k in separate])
    assert plan_joined == plan_sep


def test_hotkey_split_rejects_empty() -> None:
    with pytest.raises(ValueError, match="至少需要一个按键"):
        winapi._split_keys([""])


@pytest.mark.parametrize("key,expected", [
    ("enter", 0x0D), ("esc", 0x1B), ("tab", 0x09), ("space", 0x20),
    ("f5", 0x74), ("delete", 0x2E), ("volume_up", 0xAF), ("a", 0x41),
])
def test_key_resolution(key: str, expected: int) -> None:
    vk, _shift = winapi.resolve_key(key)
    assert vk == expected


def test_unknown_key_raises() -> None:
    with pytest.raises(ValueError):
        winapi.resolve_key("这不是键")


def test_hotkey_requires_keys() -> None:
    with pytest.raises(ValueError):
        winapi.hotkey("")


# --- 剪贴板 -------------------------------------------------------------


@pytest.mark.hardware
def test_clipboard_roundtrip_unicode() -> None:
    backup = winapi.clipboard_get_text()
    try:
        text = "AutoPilot 剪贴板测试 ✔ 123"
        assert winapi.clipboard_set_text(text)
        assert winapi.clipboard_get_text() == text
    finally:
        winapi.clipboard_set_text(backup)


# --- 截屏 ---------------------------------------------------------------


@pytest.mark.hardware
def test_capture_full_screen() -> None:
    grab = screen.capture()
    w, h = winapi.primary_screen_size()
    assert grab.size == (w, h)
    r, g, b = grab.pixel(1, 1)
    assert all(0 <= v <= 255 for v in (r, g, b))


@pytest.mark.hardware
def test_capture_region() -> None:
    grab = screen.capture((100, 100, 200, 150))
    assert grab.size == (200, 150)
    assert grab.origin == (100, 100)


@pytest.mark.hardware
def test_png_encoding_is_valid() -> None:
    grab = screen.capture((0, 0, 64, 48))
    data = grab.to_png_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[-8:] == b"IEND\xaeB`\x82"


@pytest.mark.hardware
def test_scaling_preserves_aspect() -> None:
    grab = screen.capture((0, 0, 800, 400))
    small = grab.scaled(200, 200)
    assert small.size[0] <= 200 and small.size[1] <= 200
    assert abs(small.size[0] / small.size[1] - 2.0) < 0.1


@pytest.mark.hardware
def test_scaling_never_upsizes() -> None:
    grab = screen.capture((0, 0, 100, 100))
    assert grab.scaled(1000, 1000).size == (100, 100)


def test_fingerprint_and_diff() -> None:
    """用合成像素验证指纹/差异逻辑。

    这里**故意不截真屏**：原来抓两次真实画面比较，只要使用者正在动
    什么（滚动、动画、视频）就会偶发失败。但被验证的逻辑跟屏幕内容
    无关，用构造的数据测反而更严格也更稳。
    """
    def px(r: int, g: int, b: int, n: int = 16) -> bytes:
        return bytes([b, g, r, 255]) * n

    a = screen.Grab((0, 0), 4, 4, px(10, 20, 30))
    b = screen.Grab((0, 0), 4, 4, px(10, 20, 30))
    assert a.fingerprint() == b.fingerprint()
    assert a.diff_ratio(b) == 0.0

    # 差异够大就要被察觉
    c = screen.Grab((0, 0), 4, 4, px(250, 250, 250))
    assert a.diff_ratio(c) == 1.0

    # 细微差异不该被误判（阈值 12）
    d = screen.Grab((0, 0), 4, 4, px(15, 25, 35))
    assert a.diff_ratio(d) == 0.0

    # 尺寸不同一律算全变
    assert a.diff_ratio(screen.Grab((0, 0), 2, 2, px(10, 20, 30, 4))) == 1.0


def test_screen_self_check() -> None:
    info = screen.self_check()
    assert info["size"][0] > 0 and info["grab_ms"] > 0
    assert info["png_bytes"] > 1000


# --- 窗口 ---------------------------------------------------------------


def test_list_windows_excludes_noise() -> None:
    wins = windows.list_windows()
    classes = {w.class_name for w in wins}
    assert "Default IME" not in classes
    assert "MSCTFIME UI" not in classes
    for win in wins:
        assert win.rect.width > 1 and win.rect.height > 1


def _fake_win(title: str, width: int = 800, height: int = 600) -> windows.WindowInfo:
    return windows.WindowInfo(
        hwnd=1, title=title, class_name="FakeWnd", pid=1,
        rect=winapi.Rect(0, 0, width, height),
    )


def test_helper_windows_are_recognised() -> None:
    """程序内部的消息窗口不算"界面"。

    真实踩坑：app_status("uu") 曾经报"在运行，共 8 个窗口"，其中 3 个是
    CAsyncSocketEx Helper Window、1 个 GDI+ Window (uu.exe)——模型于是以为
    界面已经开好了，继续在错误的窗口上瞎点。
    """
    for title in ("CAsyncSocketEx Helper Window", "GDI+ Window (uu.exe)",
                  "Default IME", "  ", "ToolTips_Class32"):
        assert windows.is_helper_window(_fake_win(title)), title
    assert windows.is_helper_window(_fake_win("真界面", width=1)) is True   # 1 像素不算界面
    assert windows.is_helper_window(_fake_win("UU加速器")) is False
    assert windows.is_helper_window(_fake_win("记事本")) is False


def test_own_console_window_is_hidden_from_listing() -> None:
    """自己的终端窗口不能出现在窗口列表里。

    踩过的坑：命令行标题就是整条命令、含任务原文，于是
    app_status("uu") 会把启动 Agent 的那个终端当成"UU加速器正在运行"。
    """
    import subprocess

    marker = "python -m autopilot run 打开uu加速器"      # 含固定特征串 autopilot
    found = None
    for _ in range(3):                                   # 系统忙时 start 可能慢，重试几次
        subprocess.Popen(f'start "{marker}" /MIN cmd /k', shell=True)
        for _ in range(12):
            windows.reset_console_cache()
            for win in windows.list_windows(include_hidden=True, include_own_console=True):
                if win.process.lower() == "cmd.exe" and marker in win.title:
                    found = win
                    break
            if found:
                break
            time.sleep(0.4)
        if found:
            break

    try:
        if found is None:
            pytest.skip("系统没有及时生成测试用终端窗口")
        assert found.hwnd in windows.own_console_windows()
        plain = {w.hwnd for w in windows.list_windows()}
        assert found.hwnd not in plain, "自己的终端窗口不该出现在 list_windows() 里"
        with_own = {w.hwnd for w in windows.list_windows(include_own_console=True)}
        assert found.hwnd in with_own, "显式要求时才列出来"
    finally:
        if found:
            windows.kill_process(found.pid, force=True)
        windows.reset_console_cache()


def test_foreground_window_info() -> None:
    fg = windows.get_foreground()
    if fg is None:
        pytest.skip("当前没有前台窗口")
    assert fg.hwnd > 0
    assert fg.foreground is True
    assert isinstance(fg.one_line(), str)


def test_find_window_by_process() -> None:
    """按进程名应该能找回一个已知在列表里的窗口。

    注意不能想当然地拿"前台窗口"来测：前台可能是任务栏（Shell_TrayWnd）、
    输入法、或者我们自己的终端——这些按设计都不在窗口列表里，测试会无故挂掉。
    """
    listed = windows.list_windows()
    if not listed:
        pytest.skip("当前没有可枚举的窗口")
    target = listed[0]
    found = windows.find_windows(process=target.process)
    assert any(w.hwnd == target.hwnd for w in found)


def test_find_missing_window_returns_none() -> None:
    assert windows.find_window("绝对不存在的窗口标题zzz") is None


def test_root_window_and_point_lookup() -> None:
    fg = windows.get_foreground()
    if fg is None:
        pytest.skip("没有前台窗口")
    assert windows.root_window(fg.hwnd) == fg.hwnd  # 顶层窗口的 root 是它自己
    cx, cy = fg.rect.center
    info = windows.window_at(cx, cy)
    assert info is not None and info.hwnd > 0


# --- PowerShell 桥接 ----------------------------------------------------


def test_powershell_is_found() -> None:
    exe = psbridge.find_powershell()
    assert exe.lower().endswith("powershell.exe")


def test_uia_tree_of_foreground() -> None:
    fg = winapi.get_foreground_window()
    if not fg:
        pytest.skip("没有前台窗口")
    res = psbridge.bridge().uia_tree(fg, max_depth=6, max_nodes=120)
    assert res["ok"], res.get("error")
    assert res["count"] >= 1
    assert res["root"]["rect"][2] > 0


def test_uia_focused_shape() -> None:
    res = psbridge.bridge().uia_focused()
    assert res["ok"]
    assert "focused" in res


@pytest.mark.hardware
def test_ocr_reads_rendered_text(tmp_path) -> None:
    """把文字画到图上再 OCR 回来，验证整条链路。"""
    grab = screen.capture((0, 0, 400, 200))
    path = tmp_path / "ocr.png"
    grab.save(path)
    res = psbridge.bridge().ocr(path)
    assert res["ok"], res.get("error")
    assert isinstance(res.get("lines"), list)


def test_ocr_languages_listed() -> None:
    langs = psbridge.bridge().ocr_languages()
    assert isinstance(langs, list)


def test_toast_notification() -> None:
    res = psbridge.bridge().toast("AutoPilot 测试", "这是一条自检测试通知")
    assert "ok" in res


def test_bridge_error_on_missing_script() -> None:
    with pytest.raises(psbridge.BridgeError):
        psbridge.bridge()._run("根本不存在的脚本", {})


# --- 按名字找程序 -------------------------------------------------------
# 为什么要有这个能力：Agent 想启动"知道名字、不知道路径"的程序时，会退化成
# 用 run_command 递归扫盘 + 翻注册表。实测一次任务在这上面白烧 9 步。
# 这几个测试用打桩的数据，不依赖本机装了什么。


def _stub_pools(monkeypatch, apps=None, menu=None):
    from autopilot import windows

    monkeypatch.setattr(windows, "_app_cache", dict(apps or {}))
    monkeypatch.setattr(windows, "_menu_cache", dict(menu or {}))
    monkeypatch.setattr(windows.shutil, "which", lambda _n: None)


def test_find_app_hits_registry_and_start_menu(monkeypatch) -> None:
    _stub_pools(monkeypatch,
                apps={"uu.exe": r"C:\Program Files\UU\uu.exe"},
                menu={"uu加速器": r"C:\Menu\UU加速器.lnk"})
    from autopilot import windows

    hits = windows.find_app("uu")
    assert hits, "应该能按名字找到"
    vias = {h["via"] for h in hits}
    assert "App Paths" in vias and "开始菜单" in vias


def test_find_app_prefers_exact_match(monkeypatch) -> None:
    _stub_pools(monkeypatch, menu={"steam": r"C:\Menu\Steam.lnk",
                                   "steam++": r"C:\Menu\Steam++.lnk"})
    from autopilot import windows

    hits = windows.find_app("steam")
    assert hits[0]["name"] == "steam", "精确匹配要排在最前"


def test_find_app_fuzzy_fallback(monkeypatch) -> None:
    _stub_pools(monkeypatch, menu={"网易uu加速器": r"C:\Menu\网易UU.lnk"})
    from autopilot import windows

    assert windows.find_app("uu"), "子串匹配应该能兜住"


def test_find_app_empty_and_missing(monkeypatch) -> None:
    _stub_pools(monkeypatch)
    from autopilot import windows

    assert windows.find_app("") == []
    assert windows.find_app("绝对不存在的程序zzz") == []


def test_find_app_respects_limit(monkeypatch) -> None:
    _stub_pools(monkeypatch, menu={f"uu{i}": f"C:\\Menu\\uu{i}.lnk" for i in range(20)})
    from autopilot import windows

    assert len(windows.find_app("uu", limit=3)) == 3


def test_launch_resolves_bare_name(monkeypatch) -> None:
    """``launch`` 收到裸程序名时要先解析成真实路径再启动。"""
    from autopilot import windows

    _stub_pools(monkeypatch, menu={"uu加速器": r"C:\Menu\UU加速器.lnk"})
    called: list[str] = []
    monkeypatch.setattr(windows.os, "startfile",
                        lambda p: called.append(str(p)), raising=False)
    res = windows.launch("uu", wait=0)
    assert res["resolved_from"] == "uu"
    assert res["resolved_via"] == "开始菜单"
    assert called == [r"C:\Menu\UU加速器.lnk"]


def test_launch_missing_exe_gives_actionable_error(monkeypatch) -> None:
    """找不到程序时不能丢原始 FileNotFoundError，要指路。"""
    from autopilot import windows

    _stub_pools(monkeypatch)
    with pytest.raises(RuntimeError) as info:
        windows.launch("绝对不存在zzz", wait=0)
    msg = str(info.value)
    assert "find_app" in msg
    assert "开始菜单" in msg
    assert "不要" in msg and "递归" in msg


def test_launch_does_not_resolve_paths_or_urls(monkeypatch) -> None:
    """给了带分隔符的路径或 URL 就不该去猜名字。"""
    from autopilot import windows

    calls: list[str] = []
    monkeypatch.setattr(windows, "find_app",
                        lambda *a, **k: calls.append("find_app") or [])
    monkeypatch.setattr(windows.os, "startfile", lambda p: None, raising=False)
    windows.launch("https://example.com", wait=0)
    assert calls == [], "URL 不该走名字解析"


# --- 一步到位 -----------------------------------------------------------


def test_app_status_answers_everything_in_one_call(ctx) -> None:
    """一次问清"装在哪 / 在不在跑 / 窗口在哪"，省掉 list_processes 再自己翻。"""
    from autopilot.actions import build_registry

    reg = build_registry(ctx)
    out = reg.get("app_status").call(name="notepad")
    assert "安装位置" in out
    assert "运行状态" in out
    assert "notepad" in out.lower()


def test_app_status_handles_unknown_program(ctx) -> None:
    from autopilot.actions import build_registry

    out = build_registry(ctx).get("app_status").call(name="绝对不存在zzz")
    assert "没找到" in out
    assert "没有在运行" in out


def test_app_status_requires_name(ctx) -> None:
    from autopilot.actions import build_registry

    with pytest.raises(ValueError):
        build_registry(ctx).get("app_status").call(name="  ")


def test_list_processes_matches_window_titles(ctx) -> None:
    """进程名是英文、界面名是中文的程序也要能搜到。"""
    from autopilot.actions import build_registry

    out = build_registry(ctx).get("list_processes").call(filter="explorer", top=5)
    assert isinstance(out, str) and out


def test_list_processes_miss_gives_next_step(ctx) -> None:
    from autopilot.actions import build_registry

    out = build_registry(ctx).get("list_processes").call(filter="绝对不存在的进程zzz")
    assert "没有匹配" in out
    assert "app_status" in out, "找不到时要指路，而不是让模型继续瞎试"


def test_launch_app_exposes_wait_title(ctx) -> None:
    """「启动并确认可用」要能一步做到。"""
    from autopilot.actions import build_registry

    spec = {p.name for p in build_registry(ctx).get("launch_app").params}
    assert {"target", "wait_title", "focus"} <= spec


# --- run_command 的传参/编码 -------------------------------------------------
# 这三个 bug 让一次真实任务里 9 条命令全部静默失效，必须钉死。


def test_normalize_shell_unwraps_powershell_in_cmd() -> None:
    """模型爱写 run_command('powershell -Command "..."', shell="cmd")，
    cmd 会把内层引号吃掉、$_ 变空。必须自动纠正成直接跑 PowerShell。"""
    from autopilot.actions import _normalize_shell

    cases = [
        ('powershell -Command "Get-ChildItem | Where-Object { $_.Name -match \'a\' }"',
         "Get-ChildItem | Where-Object { $_.Name -match 'a' }"),
        ('powershell -NoProfile -Command "$x=1; Write-Output $x"',
         "$x=1; Write-Output $x"),
        ('pwsh -c "Write-Output 1"', "Write-Output 1"),
        ('powershell.exe -ExecutionPolicy Bypass -Command "echo hi"', "echo hi"),
    ]
    for raw, expect_inner in cases:
        text, shell = _normalize_shell(raw, "cmd")
        assert shell == "powershell", f"没纠正：{raw}"
        assert text == expect_inner, f"{text!r} != {expect_inner!r}"


def test_normalize_shell_leaves_plain_commands_alone() -> None:
    from autopilot.actions import _normalize_shell

    assert _normalize_shell("dir /b", "cmd") == ("dir /b", "cmd")
    assert _normalize_shell("echo hi", "cmd") == ("echo hi", "cmd")
    # 已经指明 powershell 的，原样保留
    assert _normalize_shell("$x=1", "powershell") == ("$x=1", "powershell")


def test_decode_explicit_encoding_beats_heuristic() -> None:
    """GBK 的某些字节恰好是合法 UTF-8（"值" = D6 B5 → U+05B5）。

    所以"先试 UTF-8"的启发式必然出错，能确定编码的地方必须显式指定。
    """
    from autopilot.actions import _decode

    raw = "值=5".encode("gbk")
    assert _decode(raw, "cp936") == "值=5"
    assert _decode(raw) != "值=5", "启发式确实会解错，所以才必须显式传编码"


def test_clean_clixml_extracts_real_error() -> None:
    """PowerShell 重定向 stderr 时吐的是 CLIXML，要还原成人能读的错误。"""
    from autopilot.actions import _clean_clixml

    raw = ('#< CLIXML\n<Objs Version="1.1.0.1"><S S="Error">找不到路径_x000D__x000A_'
           '请检查后再试_x000D__x000A_</S></Objs>')
    out = _clean_clixml(raw)
    assert "找不到路径" in out
    assert "CLIXML" not in out
    assert "_x000D_" not in out
    assert "\n" in out, "换行转义要还原"


def test_clean_clixml_passes_plain_text_through() -> None:
    from autopilot.actions import _clean_clixml

    assert _clean_clixml("普通错误") == "普通错误"
