# -*- coding: utf-8 -*-
"""Win32 底层封装（纯 ``ctypes``，不依赖 pyautogui / pywin32）。

本模块是整个项目的"手和脚"，提供：

* DPI 感知开关（**必须在任何 GUI 调用之前设置**，否则 125% 缩放下点击坐标全错）
* 虚拟桌面尺寸与坐标换算
* 鼠标移动 / 点击 / 拖拽 / 滚轮
* 键盘按键、组合键、Unicode 文本输入（支持中文）
* 剪贴板读写
* 光标位置、屏幕尺寸、物理/逻辑像素换算

设计要点：
    1. 所有返回句柄的 API 都显式声明 ``restype = c_void_p``。
       64 位下 ctypes 默认把返回值当 ``c_int``，句柄会被截断成负数或 0。
    2. 鼠标绝对定位统一走 ``SendInput`` + ``MOUSEEVENTF_ABSOLUTE|VIRTUALDESK``，
       这样多显示器负坐标区域也能正确命中。
    3. 文本输入优先走 ``KEYEVENTF_UNICODE``，绕开键盘布局，
       中文/emoji 都能直接送进去。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import sys
import threading
import time
from typing import NamedTuple

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
else:  # pragma: no cover - 仅用于让 lint / 文档生成在非 Windows 上不炸
    _user32 = None
    _kernel32 = None


def _require_windows() -> None:
    if not IS_WINDOWS:
        raise RuntimeError("autopilot 仅支持 Windows（依赖 user32/kernel32）")


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------

_DEFAULT_DPI = 96
_dpi_state: dict[str, object] = {"mode": None, "dpi": None}


def enable_dpi_awareness() -> str:
    """把当前进程设为 DPI 感知，返回生效的模式名。

    顺序：Per-Monitor-V2 -> Per-Monitor -> System -> 未设置。
    重复调用是安全的（后续调用会失败并被忽略）。
    """
    _require_windows()
    if _dpi_state["mode"]:
        return str(_dpi_state["mode"])

    mode = "none"
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            mode = "per-monitor-v2"
    except Exception:
        pass

    if mode == "none":
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            shcore = ctypes.WinDLL("shcore", use_last_error=True)
            if shcore.SetProcessDpiAwareness(2) == 0:
                mode = "per-monitor"
        except Exception:
            pass

    if mode == "none":
        try:
            if _user32.SetProcessDPIAware():
                mode = "system"
        except Exception:
            pass

    _dpi_state["mode"] = mode
    return mode


def get_dpi_awareness() -> str:
    return str(_dpi_state["mode"] or "unset")


def get_dpi() -> int:
    """返回主显示器 DPI（96 = 100%，120 = 125%，144 = 150%）。"""
    _require_windows()
    if _dpi_state["dpi"]:
        return int(_dpi_state["dpi"])  # type: ignore[arg-type]
    try:
        dpi = int(_user32.GetDpiForSystem())
    except Exception:
        hdc = _user32.GetDC(None)
        try:
            dpi = int(ctypes.WinDLL("gdi32").GetDeviceCaps(ctypes.c_void_p(hdc), 88))
        finally:
            _user32.ReleaseDC(None, ctypes.c_void_p(hdc))
    if dpi <= 0:
        dpi = _DEFAULT_DPI
    _dpi_state["dpi"] = dpi
    return dpi


def get_scale() -> float:
    """DPI 缩放比例，例如 1.25 表示 125%。"""
    return round(get_dpi() / _DEFAULT_DPI, 4)


# --------------------------------------------------------------------------
# 屏幕几何
# --------------------------------------------------------------------------

SM_CXSCREEN, SM_CYSCREEN = 0, 1
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79
SM_CMONITORS = 80


class Rect(NamedTuple):
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def as_dict(self) -> dict[str, int]:
        return {
            "x": self.left,
            "y": self.top,
            "width": self.width,
            "height": self.height,
        }


def primary_screen_size() -> tuple[int, int]:
    _require_windows()
    return int(_user32.GetSystemMetrics(SM_CXSCREEN)), int(_user32.GetSystemMetrics(SM_CYSCREEN))


def virtual_screen() -> Rect:
    """整个虚拟桌面的边界（多显示器时为负坐标区域也算进去）。"""
    _require_windows()
    return Rect(
        int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN)),
        int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN)),
        int(_user32.GetSystemMetrics(SM_XVIRTUALSCREEN))
        + int(_user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)),
        int(_user32.GetSystemMetrics(SM_YVIRTUALSCREEN))
        + int(_user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)),
    )


def monitor_count() -> int:
    _require_windows()
    return int(_user32.GetSystemMetrics(SM_CMONITORS))


# --------------------------------------------------------------------------
# 光标
# --------------------------------------------------------------------------


class POINT(ctypes.Structure):
    _fields_ = [("x", w.LONG), ("y", w.LONG)]


def get_cursor_pos() -> tuple[int, int]:
    _require_windows()
    p = POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return int(p.x), int(p.y)


# --------------------------------------------------------------------------
# SendInput 结构体
# --------------------------------------------------------------------------

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
INPUT_HARDWARE = 2

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x1000
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008

WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", w.DWORD),
        ("wParamL", w.WORD),
        ("wParamH", w.WORD),
    ]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


if IS_WINDOWS:
    _user32.SendInput.argtypes = (w.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    _user32.SendInput.restype = w.UINT
    _user32.WindowFromPoint.restype = ctypes.c_void_p
    _user32.GetForegroundWindow.restype = ctypes.c_void_p
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.GetDC.argtypes = (ctypes.c_void_p,)

_INPUT_LOCK = threading.RLock()


def _send(*inputs: INPUT) -> int:
    _require_windows()
    if not inputs:
        return 0
    arr = (INPUT * len(inputs))(*inputs)
    with _INPUT_LOCK:
        sent = _user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
    if sent != len(inputs):
        err = ctypes.get_last_error()
        raise OSError(f"SendInput 失败：发送 {sent}/{len(inputs)}，GetLastError={err}")
    return int(sent)


def _mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(dx, dy, data & 0xFFFFFFFF, flags, 0, None))


def _key_input(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, None))


# --------------------------------------------------------------------------
# 鼠标
# --------------------------------------------------------------------------


def _to_absolute(x: int, y: int) -> tuple[int, int]:
    """物理像素坐标 -> SendInput 归一化绝对坐标（0..65535）。"""
    vs = virtual_screen()
    width = max(1, vs.width - 1)
    height = max(1, vs.height - 1)
    nx = int(round((int(x) - vs.left) * 65535 / width))
    ny = int(round((int(y) - vs.top) * 65535 / height))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def move_to(x: int, y: int, duration: float = 0.0, steps: int | None = None) -> None:
    """把鼠标移动到物理坐标 ``(x, y)``。

    ``duration > 0`` 时按步进插值移动，模拟人手轨迹——某些应用
    （游戏、画布类控件）对瞬移不响应，需要这个。
    """
    _require_windows()
    if duration <= 0:
        _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                           *_to_absolute(x, y)))
        return

    sx, sy = get_cursor_pos()
    n = steps or max(2, int(duration / 0.012))
    for i in range(1, n + 1):
        t = i / n
        # ease-in-out，让轨迹看起来更自然
        e = 2 * t * t if t < 0.5 else 1 - ((-2 * t + 2) ** 2) / 2
        cx = int(round(sx + (x - sx) * e))
        cy = int(round(sy + (y - sy) * e))
        _send(_mouse_input(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                           *_to_absolute(cx, cy)))
        time.sleep(duration / n)


def _click_at(button: str, x: int, y: int, clicks: int, interval: float,
              duration: float) -> None:
    down, up = {
        "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
        "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
        "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
    }[button]

    move_to(x, y, duration=duration)
    time.sleep(0.02)
    for i in range(clicks):
        _send(_mouse_input(down), _mouse_input(up))
        if i < clicks - 1:
            time.sleep(interval)


def click(x: int | None = None, y: int | None = None, button: str = "left",
          clicks: int = 1, interval: float = 0.08, duration: float = 0.0) -> tuple[int, int]:
    """在 ``(x, y)`` 点击；坐标省略时点当前位置。返回实际落点。"""
    if x is None or y is None:
        x, y = get_cursor_pos()
    _click_at(button, int(x), int(y), max(1, int(clicks)), interval, duration)
    return int(x), int(y)


def mouse_down(button: str = "left") -> None:
    flags = {"left": MOUSEEVENTF_LEFTDOWN, "right": MOUSEEVENTF_RIGHTDOWN,
             "middle": MOUSEEVENTF_MIDDLEDOWN}[button]
    _send(_mouse_input(flags))


def mouse_up(button: str = "left") -> None:
    flags = {"left": MOUSEEVENTF_LEFTUP, "right": MOUSEEVENTF_RIGHTUP,
             "middle": MOUSEEVENTF_MIDDLEUP}[button]
    _send(_mouse_input(flags))


def drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
         duration: float = 0.35) -> None:
    """从 ``(x1,y1)`` 按住拖到 ``(x2,y2)`` 后松开。"""
    move_to(x1, y1, duration=0.08)
    time.sleep(0.05)
    mouse_down(button)
    time.sleep(0.05)
    move_to(x2, y2, duration=duration)
    time.sleep(0.05)
    mouse_up(button)


def scroll(amount: int, x: int | None = None, y: int | None = None,
           horizontal: bool = False) -> None:
    """滚轮。``amount`` 为正向上/向右，单位为"格"（每格 120）。"""
    if x is not None and y is not None:
        move_to(x, y, duration=0.05)
        time.sleep(0.02)
    flag = MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL
    remaining = int(amount)
    while remaining != 0:
        chunk = max(-3, min(3, remaining))
        _send(_mouse_input(flag, data=chunk * WHEEL_DELTA))
        remaining -= chunk
        if remaining != 0:
            time.sleep(0.012)


# --------------------------------------------------------------------------
# 键盘
# --------------------------------------------------------------------------

VK: dict[str, int] = {
    "backspace": 0x08, "back": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D,
    "return": 0x0D, "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12,
    "menu": 0x12, "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B,
    "space": 0x20, "pageup": 0x21, "prior": 0x21, "pagedown": 0x22, "next": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "select": 0x29, "print": 0x2A, "execute": 0x2B, "printscreen": 0x2C, "snapshot": 0x2C,
    "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E, "help": 0x2F,
    "win": 0x5B, "lwin": 0x5B, "cmd": 0x5B, "super": 0x5B, "rwin": 0x5C,
    "apps": 0x5D, "sleep": 0x5F, "num0": 0x60, "num1": 0x61, "num2": 0x62,
    "num3": 0x63, "num4": 0x64, "num5": 0x65, "num6": 0x66, "num7": 0x67,
    "num8": 0x68, "num9": 0x69, "multiply": 0x6A, "add": 0x6B, "separator": 0x6C,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "numlock": 0x90, "scrolllock": 0x91,
    "volume_mute": 0xAD, "volume_down": 0xAE, "volume_up": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stopmedia": 0xB2, "playpause": 0xB3,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}

_EXTENDED = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
    0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xAD, 0xAE, 0xAF,
    0xB0, 0xB1, 0xB2, 0xB3, 0xA3, 0xA5,
}


def resolve_key(key: str) -> tuple[int, bool]:
    """把键名解析成 ``(虚拟键码, 是否需要 Shift)``。"""
    k = str(key).strip()
    if not k:
        raise ValueError("按键名不能为空")

    lower = k.lower()
    if lower in VK:
        return VK[lower], False
    if len(k) == 1:
        vk_scan = _user32.VkKeyScanW(ctypes.c_wchar(k))
        if vk_scan == -1:
            raise ValueError(f"无法映射字符 {k!r} 到虚拟键")
        vk = vk_scan & 0xFF
        shift = bool((vk_scan >> 8) & 0x01)
        return vk, shift
    if lower.startswith("vk_"):
        # 兼收 "vk_87"（十进制）和 "vk_0x87"（十六进制）两种写法；
        # 录制器产出的是后者，必须能原样回放。
        return int(lower[3:], 0), False
    if lower.startswith("0x"):
        return int(lower, 16), False
    raise ValueError(f"未知按键：{key!r}")


def _vk_down(vk: int) -> INPUT:
    return _key_input(vk=vk, flags=KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)


def _vk_up(vk: int) -> INPUT:
    flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
    return _key_input(vk=vk, flags=flags)


def press(key: str, presses: int = 1, interval: float = 0.05,
          _shift: bool = False) -> None:
    """按一次（或多次）某个键。支持 ``"enter"``、``"f5"``、``"a"`` 等。"""
    vk, need_shift = resolve_key(key)
    need_shift = need_shift or _shift
    shift_vk = VK["shift"]
    for i in range(max(1, int(presses))):
        seq = []
        if need_shift:
            seq.append(_vk_down(shift_vk))
        seq.append(_vk_down(vk))
        seq.append(_vk_up(vk))
        if need_shift:
            seq.append(_vk_up(shift_vk))
        _send(*seq)
        if i < presses - 1:
            time.sleep(interval)


def _split_keys(keys: tuple[str, ...] | list[str]) -> list[str]:
    """把 ``("ctrl", "shift+esc")`` 这类写法拍平成一串键名。"""
    flat: list[str] = []
    for item in keys:
        flat.extend(part for part in str(item).replace(" ", "").split("+") if part)
    if not flat:
        raise ValueError("hotkey 至少需要一个按键")
    return flat


def _hotkey_plan(resolved: list[tuple[int, bool]]) -> list[tuple[int, bool]]:
    """把组合键展开成 ``(虚拟键码, 是否按下)`` 序列。

    顺序很重要：修饰键先按下 → 主键按下 → 主键松开 → 逆序松开修饰键。

    抽成纯函数是为了能单测。这里曾经漏发主键的 **keydown**（只发了 keyup），
    结果是 Ctrl+A / Ctrl+S / Ctrl+V 全部静默失效——不报错，什么也不做，
    极难排查。现在有测试守着这个不变量。
    """
    plan: list[tuple[int, bool]] = []
    for vk, need_shift in resolved[:-1]:
        if need_shift:
            plan.append((VK["shift"], True))
        plan.append((vk, True))

    last_vk, last_shift = resolved[-1]
    if last_shift:
        plan.append((VK["shift"], True))
    plan.append((last_vk, True))    # 主键按下——绝对不能少
    plan.append((last_vk, False))
    if last_shift:
        plan.append((VK["shift"], False))

    for vk, need_shift in reversed(resolved[:-1]):
        plan.append((vk, False))
        if need_shift:
            plan.append((VK["shift"], False))
    return plan


def hotkey(*keys: str, interval: float = 0.03) -> None:
    """按组合键，例如 ``hotkey("ctrl", "shift", "esc")`` 或 ``hotkey("alt+f4")``。"""
    plan = _hotkey_plan([resolve_key(k) for k in _split_keys(keys)])
    _send(*[_vk_down(vk) if down else _vk_up(vk) for vk, down in plan])
    time.sleep(interval)


def key_down(key: str) -> None:
    vk, need_shift = resolve_key(key)
    if need_shift:
        _send(_vk_down(VK["shift"]))
    _send(_vk_down(vk))


def key_up(key: str) -> None:
    vk, need_shift = resolve_key(key)
    _send(_vk_up(vk))
    if need_shift:
        _send(_vk_up(VK["shift"]))


def _unicode_units(ch: str) -> list[int]:
    """把字符编码成 UTF-16 码元（emoji 是代理对，需要两个）。"""
    raw = ch.encode("utf-16-le")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw), 2)]


def _type_unicode(ch: str) -> None:
    for unit in _unicode_units(ch):
        _send(_key_input(scan=unit, flags=KEYEVENTF_UNICODE),
              _key_input(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))


_SPECIAL_CHARS = {
    "\n": "enter",
    "\r": "enter",
    "\t": "tab",
}


def type_text(text: str, interval: float = 0.012) -> int:
    """用 Unicode 注入方式输入文本，中文/emoji 均可。返回输入的字符数。"""
    _require_windows()
    if not text:
        return 0
    count = 0
    for ch in text:
        special = _SPECIAL_CHARS.get(ch)
        if special:
            press(special)
        else:
            _type_unicode(ch)
        count += 1
        if interval > 0:
            time.sleep(interval)
    return count


# --------------------------------------------------------------------------
# 剪贴板
# --------------------------------------------------------------------------

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

if IS_WINDOWS:
    _kernel32.GlobalAlloc.argtypes = (w.UINT, ctypes.c_size_t)
    _kernel32.GlobalAlloc.restype = ctypes.c_void_p
    _kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    _kernel32.GlobalLock.restype = ctypes.c_void_p
    _kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    _kernel32.GlobalFree.argtypes = (ctypes.c_void_p,)
    _user32.GetClipboardData.argtypes = (w.UINT,)
    _user32.GetClipboardData.restype = ctypes.c_void_p
    _user32.SetClipboardData.argtypes = (w.UINT, ctypes.c_void_p)
    _user32.SetClipboardData.restype = ctypes.c_void_p
    _user32.OpenClipboard.argtypes = (ctypes.c_void_p,)


def clipboard_get_text() -> str:
    _require_windows()
    if not _user32.OpenClipboard(None):
        return ""
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def clipboard_set_text(text: str) -> bool:
    _require_windows()
    data = str(text).encode("utf-16-le") + b"\x00\x00"
    handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = _kernel32.GlobalLock(handle)
    if not ptr:
        _kernel32.GlobalFree(handle)
        return False
    ctypes.memmove(ptr, data, len(data))
    _kernel32.GlobalUnlock(handle)

    for _ in range(20):
        if _user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        _kernel32.GlobalFree(handle)
        return False
    try:
        _user32.EmptyClipboard()
        # 成功后所有权移交系统，不能再 GlobalFree
        return bool(_user32.SetClipboardData(CF_UNICODETEXT, handle))
    finally:
        _user32.CloseClipboard()


def paste_text(text: str, restore_clipboard: bool = True) -> None:
    """用剪贴板 + Ctrl+V 输入长文本，比逐字 SendInput 快得多。"""
    backup = clipboard_get_text() if restore_clipboard else ""
    if not clipboard_set_text(text):
        raise RuntimeError("写入剪贴板失败")
    time.sleep(0.05)
    hotkey("ctrl", "v")
    if restore_clipboard:
        time.sleep(0.15)
        clipboard_set_text(backup)


# --------------------------------------------------------------------------
# 前台窗口辅助
# --------------------------------------------------------------------------


def window_from_point(x: int, y: int) -> int:
    _require_windows()
    return int(_user32.WindowFromPoint(POINT(int(x), int(y))) or 0)


def get_foreground_window() -> int:
    _require_windows()
    return int(_user32.GetForegroundWindow() or 0)


def get_window_text(hwnd: int) -> str:
    _require_windows()
    length = int(_user32.GetWindowTextLengthW(ctypes.c_void_p(int(hwnd))))
    buf = ctypes.create_unicode_buffer(length + 1)
    _user32.GetWindowTextW(ctypes.c_void_p(int(hwnd)), buf, length + 1)
    return buf.value


def get_class_name(hwnd: int) -> str:
    _require_windows()
    buf = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256)
    return buf.value


def get_window_rect(hwnd: int) -> Rect:
    _require_windows()

    class RECT(ctypes.Structure):
        _fields_ = [("left", w.LONG), ("top", w.LONG),
                    ("right", w.LONG), ("bottom", w.LONG)]

    r = RECT()
    if not _user32.GetWindowRect(ctypes.c_void_p(int(hwnd)), ctypes.byref(r)):
        return Rect(0, 0, 0, 0)
    return Rect(int(r.left), int(r.top), int(r.right), int(r.bottom))


def get_window_pid(hwnd: int) -> int:
    _require_windows()
    pid = w.DWORD(0)
    _user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
    return int(pid.value)


def is_window(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsWindow(ctypes.c_void_p(int(hwnd))))


def is_iconic(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsIconic(ctypes.c_void_p(int(hwnd))))


def is_visible(hwnd: int) -> bool:
    _require_windows()
    return bool(_user32.IsWindowVisible(ctypes.c_void_p(int(hwnd))))


def process_name(pid: int) -> str:
    """由 PID 取可执行文件名（如 ``explorer.exe``）。"""
    _require_windows()
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = w.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if _kernel32.QueryFullProcessImageNameW(ctypes.c_void_p(handle), 0, buf,
                                                ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))


def is_elevated() -> bool:
    """当前进程是否以管理员身份运行。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def screen_summary() -> dict[str, object]:
    """给自检和 Agent 用的环境摘要。"""
    _require_windows()
    enable_dpi_awareness()   # 必须早于任何几何查询，否则拿到的是被缩放后的逻辑尺寸
    vs = virtual_screen()
    pw, ph = primary_screen_size()
    return {
        "dpi_awareness": get_dpi_awareness(),
        "dpi": get_dpi(),
        "scale": get_scale(),
        "primary_size": [pw, ph],
        "virtual_desktop": vs.as_dict(),
        "monitors": monitor_count(),
        "cursor": list(get_cursor_pos()),
        "elevated": is_elevated(),
    }
