# -*- coding: utf-8 -*-
"""窗口管理：枚举、查找、聚焦、移动、最大化、关闭。

聚焦是最容易踩坑的一环——Windows 的前台窗口锁会让
``SetForegroundWindow`` 静默失败。这里实现了 AttachThreadInput
回退方案，并在返回前校验是否真的切过去了。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import subprocess
import time
from dataclasses import dataclass
from typing import Any

from . import winapi

if winapi.IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
    _user32.EnumWindows.argtypes = (ctypes.c_void_p, w.LPARAM)
    _user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
    _user32.SetWindowPos.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)

SW_HIDE, SW_SHOWNORMAL, SW_NORMAL = 0, 1, 1
SW_MINIMIZE, SW_SHOWMINIMIZED, SW_SHOWMAXIMIZED = 6, 2, 3
SW_MAXIMIZE, SW_RESTORE, SW_SHOW = 3, 9, 5

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_APPWINDOW = 0x00040000
WS_EX_NOACTIVATE = 0x08000000

DWMWA_CLOAKED = 14

HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_SHOWWINDOW = 0x0040


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    rect: winapi.Rect
    pid: int
    process: str = ""
    visible: bool = True
    minimized: bool = False
    foreground: bool = False
    cloaked: bool = False

    @property
    def key(self) -> str:
        return f"0x{self.hwnd:X}"

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "hwnd": self.key,
            "title": self.title,
            "process": self.process,
            "class": self.class_name,
            "pid": self.pid,
            "rect": self.rect.as_dict(),
        }
        if self.minimized:
            d["minimized"] = True
        if self.foreground:
            d["foreground"] = True
        return d

    def one_line(self) -> str:
        flags = []
        if self.foreground:
            flags.append("前台")
        if self.minimized:
            flags.append("最小化")
        suffix = f" [{'/'.join(flags)}]" if flags else ""
        return (f"{self.title or '(无标题)'} | {self.process or '?'} | "
                f"{self.rect.width}x{self.rect.height}@{self.rect.left},{self.rect.top}{suffix}")


def _is_cloaked(hwnd: int) -> bool:
    """UWP 应用关闭后会留下"隐身"窗口，需要靠 DWM 属性排除。"""
    try:
        val = w.DWORD(0)
        hr = _dwmapi.DwmGetWindowAttribute(ctypes.c_void_p(int(hwnd)), DWMWA_CLOAKED,
                                           ctypes.byref(val), ctypes.sizeof(val))
        return hr == 0 and bool(val.value)
    except Exception:
        return False


def _ex_style(hwnd: int) -> int:
    try:
        return int(_user32.GetWindowLongPtrW(ctypes.c_void_p(int(hwnd)), GWL_EXSTYLE))
    except Exception:
        return 0


# 这些类名是输入法/系统内部的辅助窗口，用户看不到，列出来只会干扰判断
NOISE_CLASSES = {
    "MSCTFIME UI", "Default IME", "IME", "GDI+ Window (Notepad.exe)",
    "ToolTip", "SysShadow", "TaskListThumbnailWnd", "Shell_TrayWnd",
    "Windows.UI.Core.CoreWindow", "ForegroundStaging", "Xaml_WindowedPopupClass",
    "ApplicationFrameWindow_Cloaked", "Chrome_SystemMessageWindow",
    "EdgeUiInputTopWndClass", "Chrome_ExtensionWindow",
}


def _is_noise(class_name: str) -> bool:
    if class_name in NOISE_CLASSES:
        return True
    if class_name.startswith(("GDI+ Window", "MSCTFIME", "Chrome_Message", "Intermediate D3D")):
        return True
    return False


def list_windows(include_hidden: bool = False, with_process: bool = True) -> list[WindowInfo]:
    """枚举所有顶层窗口，按"最可能被用户看见"的程度排序。"""
    winapi.enable_dpi_awareness()
    result: list[WindowInfo] = []
    fg = winapi.get_foreground_window()
    _pid_cache: dict[int, str] = {}

    WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, w.LPARAM)

    def _cb(hwnd, _lparam):
        hwnd = int(hwnd)
        visible = winapi.is_visible(hwnd)
        if not visible and not include_hidden:
            return True
        ex = _ex_style(hwnd)
        if ex & WS_EX_TOOLWINDOW and not (ex & WS_EX_APPWINDOW):
            return True
        if _is_noise(winapi.get_class_name(hwnd)):
            return True
        if _is_cloaked(hwnd):
            return True

        title = winapi.get_window_text(hwnd)
        class_name = winapi.get_class_name(hwnd)
        rect = winapi.get_window_rect(hwnd)
        if not include_hidden and rect.width <= 1 and rect.height <= 1:
            return True
        if not include_hidden and not title and class_name in ("Program Manager", "WorkerW"):
            return True

        pid = winapi.get_window_pid(hwnd)
        proc = ""
        if with_process:
            if pid not in _pid_cache:
                _pid_cache[pid] = winapi.process_name(pid)
            proc = _pid_cache[pid]

        result.append(WindowInfo(
            hwnd=hwnd, title=title, class_name=class_name, rect=rect, pid=pid,
            process=proc, visible=visible, minimized=winapi.is_iconic(hwnd),
            foreground=(hwnd == fg and fg != 0), cloaked=False,
        ))
        return True

    _user32.EnumWindows(WNDENUMPROC(_cb), 0)
    # 前台窗口排最前，然后最小化的排后面，再按面积降序
    result.sort(key=lambda x: (not x.foreground, x.minimized, -x.rect.width * x.rect.height))
    return result


def get_foreground() -> WindowInfo | None:
    hwnd = winapi.get_foreground_window()
    if not hwnd:
        return None
    pid = winapi.get_window_pid(hwnd)
    return WindowInfo(
        hwnd=hwnd, title=winapi.get_window_text(hwnd),
        class_name=winapi.get_class_name(hwnd), rect=winapi.get_window_rect(hwnd),
        pid=pid, process=winapi.process_name(pid), visible=True,
        minimized=winapi.is_iconic(hwnd), foreground=True,
    )


def find_windows(pattern: str = "", process: str = "", class_name: str = "",
                 exact: bool = False) -> list[WindowInfo]:
    """按标题 / 进程名 / 类名模糊查找窗口（不区分大小写）。"""
    pat = pattern.lower()
    proc = process.lower()
    cls = class_name.lower()
    out = []
    for win in list_windows(include_hidden=bool(proc or cls)):
        if pat:
            title = win.title.lower()
            if (title != pat) if exact else (pat not in title):
                continue
        if proc and proc not in win.process.lower():
            continue
        if cls and cls not in win.class_name.lower():
            continue
        out.append(win)
    return out


def find_window(pattern: str = "", process: str = "", class_name: str = "") -> WindowInfo | None:
    """找最合适的一个窗口：优先前台且标题精确匹配的。"""
    wins = find_windows(pattern, process, class_name)
    if not wins:
        return None
    pat = pattern.lower()
    wins.sort(key=lambda x: (x.title.lower() != pat, not x.foreground, x.minimized))
    return wins[0]


def _force_foreground(hwnd: int) -> bool:
    """绕过前台窗口锁，尽最大努力把 ``hwnd`` 切到前台。"""
    if winapi.get_foreground_window() == hwnd:
        return True

    # 常规尝试
    _user32.SetForegroundWindow(ctypes.c_void_p(int(hwnd)))
    time.sleep(0.05)
    if winapi.get_foreground_window() == hwnd:
        return True

    # 回退：把自己挂到目标线程的输入队列上再切
    fg = winapi.get_foreground_window()
    target_thread = _user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), None)
    cur_thread = _kernel32.GetCurrentThreadId()
    fg_thread = _user32.GetWindowThreadProcessId(ctypes.c_void_p(int(fg)), None) if fg else 0
    attached = []
    try:
        for t in {target_thread, fg_thread}:
            if t and t != cur_thread and _user32.AttachThreadInput(cur_thread, t, True):
                attached.append(t)
        _user32.BringWindowToTop(ctypes.c_void_p(int(hwnd)))
        _user32.SetForegroundWindow(ctypes.c_void_p(int(hwnd)))
        time.sleep(0.05)
    finally:
        for t in attached:
            _user32.AttachThreadInput(cur_thread, t, False)

    if winapi.get_foreground_window() == hwnd:
        return True

    # 最后手段：模拟一次 ALT 轻敲，解除系统的前台锁定
    try:
        winapi.press("alt")
        time.sleep(0.03)
        _user32.SetForegroundWindow(ctypes.c_void_p(int(hwnd)))
        time.sleep(0.05)
    except Exception:
        pass
    return winapi.get_foreground_window() == hwnd


def focus_window(hwnd: int, retries: int = 3) -> bool:
    """把窗口切到前台；最小化时先还原。"""
    if not winapi.is_window(hwnd):
        raise RuntimeError(f"窗口句柄无效：0x{int(hwnd):X}")
    for _ in range(max(1, retries)):
        if winapi.is_iconic(hwnd):
            _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), SW_RESTORE)
            time.sleep(0.25)
        if _force_foreground(int(hwnd)):
            return True
        time.sleep(0.2)
    return False


def focus(pattern: str = "", process: str = "", class_name: str = "") -> WindowInfo | None:
    """按条件查找并聚焦窗口。"""
    win = find_window(pattern, process, class_name)
    if not win:
        return None
    return win if focus_window(win.hwnd) else None


def minimize(hwnd: int) -> None:
    _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), SW_MINIMIZE)


def maximize(hwnd: int) -> None:
    _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), SW_MAXIMIZE)


def restore(hwnd: int) -> None:
    _user32.ShowWindow(ctypes.c_void_p(int(hwnd)), SW_RESTORE)


def move_window(hwnd: int, x: int, y: int, width: int | None = None,
                height: int | None = None) -> None:
    """移动（并可选缩放）窗口。"""
    cur = winapi.get_window_rect(hwnd)
    width = cur.width if width is None else int(width)
    height = cur.height if height is None else int(height)
    _user32.SetWindowPos(ctypes.c_void_p(int(hwnd)), None, int(x), int(y),
                         int(width), int(height), SWP_NOZORDER | SWP_SHOWWINDOW)


def set_always_on_top(hwnd: int, on_top: bool = True) -> None:
    _user32.SetWindowPos(ctypes.c_void_p(int(hwnd)), HWND_TOPMOST if on_top else HWND_NOTOPMOST,
                         0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)


WM_CLOSE = 0x0010


def close_window(hwnd: int, force: bool = False) -> bool:
    """关闭窗口。``force=True`` 时直接 TerminateProcess 该窗口所属进程。"""
    if force:
        pid = winapi.get_window_pid(hwnd)
        if pid:
            return kill_process(pid)
        return False
    _user32.PostMessageW(ctypes.c_void_p(int(hwnd)), WM_CLOSE, 0, 0)
    return True


# --------------------------------------------------------------------------
# 进程与启动
# --------------------------------------------------------------------------

CREATE_NEW_CONSOLE = 0x00000010
SW_SHOWNORMAL_ = 1


def launch(target: str, args: list[str] | None = None, wait: float = 1.0,
           cwd: str | None = None) -> dict[str, Any]:
    """启动程序 / 打开文件 / 打开 URL。

    对 ``.lnk``、文档、URL 走 ``ShellExecute``（和双击效果一致）；
    对可执行文件走 ``CreateProcess``。
    """
    args = args or []
    if not args and (target.lower().endswith((".lnk", ".url"))
                     or target.lower().startswith(("http://", "https://", "shell:", "ms-"))
                     or _looks_like_document(target)):
        try:
            import os
            os.startfile(target)  # type: ignore[attr-defined]
            time.sleep(wait)
            return {"launched": target, "via": "shell", "pid": None}
        except OSError as exc:
            raise RuntimeError(f"无法打开 {target!r}：{exc}") from exc

    cmd = subprocess.list2cmdline([target, *args])
    proc = subprocess.Popen(cmd, cwd=cwd, shell=False,
                            creationflags=CREATE_NEW_CONSOLE)
    time.sleep(wait)
    return {"launched": target, "via": "exec", "pid": proc.pid, "args": args}


def _looks_like_document(target: str) -> bool:
    return any(target.lower().endswith(ext) for ext in (
        ".txt", ".md", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
        ".png", ".jpg", ".jpeg", ".gif", ".mp4", ".mp3", ".wav", ".csv", ".json",
        ".zip", ".rar", ".7z",
    ))


def kill_process(pid: int, force: bool = True) -> bool:
    PROCESS_TERMINATE = 0x0001
    handle = _kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if not handle:
        return False
    try:
        return bool(_kernel32.TerminateProcess(ctypes.c_void_p(handle), 1))
    finally:
        _kernel32.CloseHandle(ctypes.c_void_p(handle))


GA_PARENT, GA_ROOT, GA_ROOTOWNER = 1, 2, 3


def root_window(hwnd: int) -> int:
    """取窗口所属的顶层窗口（点击前判断"这点属于哪个窗口"很有用）。"""
    if not winapi.IS_WINDOWS:
        return int(hwnd)
    try:
        got = _user32.GetAncestor(ctypes.c_void_p(int(hwnd)), GA_ROOT)
        return int(got) if got else int(hwnd)
    except Exception:
        return int(hwnd)


def window_at(x: int, y: int) -> WindowInfo | None:
    """屏幕物理坐标 ``(x, y)`` 处最上层的顶层窗口。"""
    hwnd = winapi.window_from_point(int(x), int(y))
    if not hwnd:
        return None
    root = root_window(hwnd)
    if not root:
        return None
    pid = winapi.get_window_pid(root)
    return WindowInfo(
        hwnd=root, title=winapi.get_window_text(root),
        class_name=winapi.get_class_name(root), rect=winapi.get_window_rect(root),
        pid=pid, process=winapi.process_name(pid), visible=winapi.is_visible(root),
        minimized=winapi.is_iconic(root),
        foreground=(root == winapi.get_foreground_window()),
    )


def ensure_clickable(x: int, y: int, settle: float = 0.25) -> WindowInfo | None:
    """确保 ``(x, y)`` 处的窗口位于最前，返回该窗口。

    避免"目标元素属于后台窗口，但点击被前台窗口挡住"这类静默失败。
    """
    info = window_at(x, y)
    if info is None:
        return None
    fg = winapi.get_foreground_window()
    if info.hwnd != fg and not info.minimized:
        focus_window(info.hwnd)
        time.sleep(settle)
        info = window_at(x, y) or info
    return info


def wait_for_window(pattern: str = "", process: str = "", timeout: float = 15.0,
                    interval: float = 0.4) -> WindowInfo | None:
    """等到某个窗口出现，返回它；超时返回 ``None``。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = find_window(pattern, process)
        if win:
            return win
        time.sleep(interval)
    return None
