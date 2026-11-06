# -*- coding: utf-8 -*-
"""窗口管理：枚举、查找、聚焦、移动、最大化、关闭。

聚焦是最容易踩坑的一环——Windows 的前台窗口锁会让
``SetForegroundWindow`` 静默失败。这里实现了 AttachThreadInput
回退方案，并在返回前校验是否真的切过去了。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
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


# 程序内部的消息窗口：它们也是"有标题的可见窗口"，但对用户毫无意义。
# 例：uu.exe 会登记 CAsyncSocketEx Helper Window ×3、GDI+ Window (uu.exe)。
# 不排掉的话，app_status("uu") 会报"在运行，共 8 个窗口"，模型就以为界面已经开了。
_HELPER_TITLE_MARKERS = (
    "helper window", "default ime", "gdi+ window", "olemainthreadwnd",
    "tooltips_class32", "program manager", "msctfime", "ime window",
    "windows shell experience", "nvidia share", "hidden window", "message window",
)


def is_helper_window(win: WindowInfo) -> bool:
    """判断"这不是一个用户能看见的界面"。

    app_status 用它把后台辅助窗口滤掉；否则模型会把它们当成已经打开的界面。
    """
    title = (win.title or "").strip().lower()
    if not title:
        return True
    if any(m in title for m in _HELPER_TITLE_MARKERS):
        return True
    if win.rect.width <= 1 or win.rect.height <= 1:
        return True
    return False


# --------------------------------------------------------------------------
# 识别"我们自己的终端窗口"
#
# 踩过的坑：Agent 是从命令行启动的，那个 cmd/WindowsTerminal 窗口的**标题
# 就是整条命令行**，里面当然包含任务目标里的字。于是：
#   - app_status("uu") 会把它当成"UU加速器正在运行"
#   - launch_app 等窗口时也会先等到它，然后报"已切到前台"（其实是切到自己终端）
# 结果模型完全搞不清自己在操作哪个窗口。
#
# 这些窗口对任何任务都不是目标，直接排除。
# --------------------------------------------------------------------------

CONSOLE_PROCESSES = {
    "cmd.exe", "conhost.exe", "openconsole.exe", "windowsterminal.exe", "wt.exe",
    "powershell.exe", "pwsh.exe", "bash.exe", "wsl.exe", "mintty.exe",
}

_own_console_cache: set[int] | None = None


def own_console_markers() -> list[str]:
    """从自己的命令行里取特征串（长参数就是最好的指纹，比如整段任务目标）。"""
    import sys

    markers: list[str] = []
    try:
        markers.extend(a.lower() for a in sys.argv if len(a) >= 10)
    except Exception:
        pass
    markers.append("autopilot")          # 我们的调用方式总带这个词
    return markers


def own_console_windows() -> set[int]:
    """返回属于本次运行的终端窗口句柄集合（结果会缓存）。

    判据：**控制台类进程 + 标题里带我们的命令行特征串**。

    为什么不用进程祖先链：Win11 下 ``GetConsoleWindow()`` 返回的是隐藏的
    PseudoConsoleWindow，真正可见的窗口属于 WindowsTerminal.exe；而中间那层
    shell 往往已经退出，血缘链是断的（实测）。标题才是一直在的指纹——
    cmd/终端会把正在跑的命令行写进标题，所以"autopilot"必然出现在里面。

    注意：这里**不能**调 ``list_windows``——那个函数要用本函数的结果，
    会造成无限递归。所以自己枚举一遍。
    """
    global _own_console_cache
    if _own_console_cache is not None:
        return _own_console_cache

    handles: set[int] = set()
    try:                                   # 经典控制台直接问系统
        hwnd = int(ctypes.windll.kernel32.GetConsoleWindow() or 0)
        if hwnd:
            handles.add(hwnd)
    except Exception:
        pass

    markers = own_console_markers()

    def _cb(hwnd, _lp):
        hwnd = int(hwnd)
        try:
            pid = winapi.get_window_pid(hwnd)
            if winapi.process_name(pid).lower() not in CONSOLE_PROCESSES:
                return True
            title = winapi.get_window_text(hwnd).lower()
            if title and any(m in title for m in markers):
                handles.add(hwnd)
        except Exception:
            pass
        return True

    if winapi.IS_WINDOWS:
        try:
            _user32.EnumWindows(ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, w.LPARAM)(_cb), 0)
        except Exception:
            pass

    _own_console_cache = handles
    return handles


def reset_console_cache() -> None:
    """命令行标题会随任务变化，长任务里重新识别一次。"""
    global _own_console_cache
    _own_console_cache = None



def list_windows(include_hidden: bool = False, with_process: bool = True,
                 include_own_console: bool = False) -> list[WindowInfo]:
    """枚举所有顶层窗口，按"最可能被用户看见"的程度排序。

    ``include_own_console=False``（默认）会剔除启动本 Agent 的那个终端窗口——
    它的标题就是整条命令行、含任务原文，留着会污染一切按标题的匹配。
    """
    winapi.enable_dpi_awareness()
    result: list[WindowInfo] = []
    fg = winapi.get_foreground_window()
    _pid_cache: dict[int, str] = {}
    own = set() if include_own_console else own_console_windows()

    WNDENUMPROC = ctypes.WINFUNCTYPE(w.BOOL, ctypes.c_void_p, w.LPARAM)

    def _cb(hwnd, _lparam):
        hwnd = int(hwnd)
        if hwnd in own:
            return True
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
                 exact: bool = False, include_own_console: bool = False) -> list[WindowInfo]:
    """按标题 / 进程名 / 类名模糊查找窗口（不区分大小写）。"""
    pat = pattern.lower()
    proc = process.lower()
    cls = class_name.lower()
    out = []
    for win in list_windows(include_hidden=bool(proc or cls),
                            include_own_console=include_own_console):
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


def find_window(pattern: str = "", process: str = "", class_name: str = "",
                include_own_console: bool = False) -> WindowInfo | None:
    """找最合适的一个窗口：优先前台且标题精确匹配的。"""
    wins = find_windows(pattern, process, class_name,
                        include_own_console=include_own_console)
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

# --------------------------------------------------------------------------
# 按名字找程序
#
# 为什么需要它：Agent 想启动一个"知道名字、不知道路径"的程序时，
# 会退化成用 run_command 递归扫盘、翻注册表——实测一次任务在这上面
# 烧掉了 9 步（还踩了新版 Windows 已移除 wmic 的坑）。
# 这里把 Windows 自己那套查找顺序封装成一步：PATH → App Paths → 开始菜单。
# --------------------------------------------------------------------------

_APP_PATHS_SUBKEYS = (
    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths",
    r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths",
)

_START_MENU_DIRS = (
    r"%ProgramData%\Microsoft\Windows\Start Menu\Programs",
    r"%APPDATA%\Microsoft\Windows\Start Menu\Programs",
)

_app_cache: dict[str, str] | None = None
_menu_cache: dict[str, str] | None = None


def registry_app_paths() -> dict[str, str]:
    """``App Paths`` 注册表：Windows 记录"程序名 -> 完整路径"的官方位置。"""
    global _app_cache
    if _app_cache is not None:
        return _app_cache
    import winreg

    found: dict[str, str] = {}
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in _APP_PATHS_SUBKEYS:
            try:
                with winreg.OpenKey(root, sub) as key:
                    for i in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            name = winreg.EnumKey(key, i)
                            with winreg.OpenKey(key, name) as item:
                                path, _ = winreg.QueryValueEx(item, "")
                            if path:
                                found.setdefault(name.lower(), str(path).strip('"'))
                        except OSError:
                            continue
            except OSError:
                continue
    _app_cache = found
    return found


def start_menu_shortcuts() -> dict[str, str]:
    """开始菜单里的快捷方式——装在 Program Files 的程序基本都能在这找到。"""
    global _menu_cache
    if _menu_cache is not None:
        return _menu_cache
    found: dict[str, str] = {}
    for raw in _START_MENU_DIRS:
        base = Path(os.path.expandvars(raw))
        if not base.is_dir():
            continue
        try:
            for lnk in base.rglob("*.lnk"):
                found.setdefault(lnk.stem.lower(), str(lnk))
        except OSError:
            continue
    _menu_cache = found
    return found


def find_app(name: str, limit: int = 8) -> list[dict[str, str]]:
    """按名字找可启动项，返回 ``[{"name","path","via"}]``。

    查找顺序和 Windows 自己一致：PATH → App Paths → 开始菜单，
    最后再按子串模糊匹配兜底。
    """
    needle = str(name).strip().strip('"')
    if not needle:
        return []
    low = needle.lower()
    stem = low[:-4] if low.endswith(".exe") else low

    out: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(n: str, p: str, via: str) -> None:
        key = str(p).lower()
        if key in seen:
            return
        seen.add(key)
        out.append({"name": n, "path": str(p), "via": via})

    direct = shutil.which(needle) or shutil.which(stem) or shutil.which(stem + ".exe")
    if direct:
        add(needle, direct, "PATH")
    else:
        for cand in (needle, stem, stem + ".exe"):
            p = Path(cand)
            if p.is_file():
                add(needle, str(p.resolve()), "路径")
                break

    apps = registry_app_paths()
    for key in (low, stem + ".exe", stem):
        if key in apps:
            add(key, apps[key], "App Paths")

    menu = start_menu_shortcuts()
    for key in (low, stem):
        if key in menu:
            add(key, menu[key], "开始菜单")

    if len(out) < limit:                      # 子串模糊兜底
        for pool, via in ((apps, "App Paths"), (menu, "开始菜单")):
            for k, v in sorted(pool.items()):
                if stem and stem in k:
                    add(k, v, via)
                    if len(out) >= limit:
                        break
            if len(out) >= limit:
                break

    # 名字最贴近的排前面
    out.sort(key=lambda x: (stem not in x["name"].lower(), len(x["name"])))
    return out[:limit]


def _is_url_or_shell(target: str) -> bool:
    return target.lower().startswith(("http://", "https://", "shell:", "ms-", "mailto:"))


def launch(target: str, args: list[str] | None = None, wait: float = 1.0,
           cwd: str | None = None) -> dict[str, Any]:
    """启动程序 / 打开文件 / 打开 URL。

    对 ``.lnk``、文档、URL 走 ``ShellExecute``（和双击效果一致）；
    对可执行文件走 ``CreateProcess``。

    ``target`` 可以只给**程序名**（如 ``"uu"``）——会自动去 PATH、
    App Paths 注册表和开始菜单里找，省掉一长串"找它在哪"的探索。
    """
    args = args or []
    resolved_from = ""
    resolved_via = ""

    if (not args and not _is_url_or_shell(target)
            and not _looks_like_document(target)
            and not os.path.sep in str(target) and "/" not in str(target)):
        hits = find_app(str(target))
        if hits:
            resolved_from, resolved_via = target, hits[0]["via"]
            target = hits[0]["path"]

    if not args and (target.lower().endswith((".lnk", ".url"))
                     or _is_url_or_shell(target)
                     or _looks_like_document(target)):
        try:
            os.startfile(target)  # type: ignore[attr-defined]
            time.sleep(wait)
            return {"launched": target, "via": "shell", "pid": None,
                    "resolved_from": resolved_from, "resolved_via": resolved_via}
        except OSError as exc:
            raise RuntimeError(f"无法打开 {target!r}：{exc}") from exc

    cmd = subprocess.list2cmdline([target, *args])
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, shell=False,
                                creationflags=CREATE_NEW_CONSOLE)
    except FileNotFoundError as exc:
        # 别把原始堆栈丢给模型——它看不懂，而且会退化成"用 run_command 满盘找"。
        raise RuntimeError(
            f"找不到可执行文件 {target!r}。"
            f"如果它是已安装的程序，直接给程序名就行（如 \"uu\"、\"steam\"、\"微信\"），"
            f"系统会自动去 PATH / App Paths / 开始菜单里找；"
            f"也可以先用 find_app 动作查询它在哪。"
            f"**不要**用 run_command 递归扫描磁盘去找程序。") from exc
    time.sleep(wait)
    return {"launched": target, "via": "exec", "pid": proc.pid, "args": args,
            "resolved_from": resolved_from, "resolved_via": resolved_via}


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
                    interval: float = 0.4, include_own_console: bool = False) -> WindowInfo | None:
    """等到某个窗口出现，返回它；超时返回 ``None``。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = find_window(pattern, process, include_own_console=include_own_console)
        if win:
            return win
        time.sleep(interval)
    return None
