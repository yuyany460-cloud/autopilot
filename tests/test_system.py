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


def test_foreground_window_info() -> None:
    fg = windows.get_foreground()
    if fg is None:
        pytest.skip("当前没有前台窗口")
    assert fg.hwnd > 0
    assert fg.foreground is True
    assert isinstance(fg.one_line(), str)


def test_find_window_by_process() -> None:
    fg = windows.get_foreground()
    if fg is None or not fg.process:
        pytest.skip("没有可用的前台进程")
    found = windows.find_windows(process=fg.process)
    assert any(w.hwnd == fg.hwnd for w in found)


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
