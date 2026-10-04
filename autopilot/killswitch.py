# -*- coding: utf-8 -*-
"""全局急停（kill switch）。

自动化程序必须有随时能按下的刹车。这里提供两个互相独立的刹车：

* **全局热键 ``Ctrl+Alt+Q``**：无论焦点在哪个程序，按下即中止当前任务。
  用 ``RegisterHotKey(NULL, ...)`` 把热键挂到后台线程的消息队列上，
  不创建窗口，也不需要额外依赖。
* **停止文件 ``var/STOP``**：外部脚本或用户手动创建一个文件就能叫停，
  方便远程 / 无人值守场景。

两者都通过一个 ``threading.Event`` 对外暴露，执行器在每一步动作前检查。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import threading
import time
from pathlib import Path

from . import winapi


class EmergencyStop(RuntimeError):
    """任务被急停中断。"""


MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

if winapi.IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("message", w.UINT),
        ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
        ("time", w.DWORD), ("pt_x", w.LONG), ("pt_y", w.LONG),
    ]


class KillSwitch:
    """急停控制器。"""

    def __init__(self, stop_file: str | Path | None = None,
                 hotkey: str = "ctrl+alt+q", enabled: bool = True) -> None:
        self._event = threading.Event()
        self._reason: str = ""
        self._lock = threading.Lock()
        self.stop_file = Path(stop_file) if stop_file else None
        self.hotkey = hotkey
        self._thread: threading.Thread | None = None
        self._thread_id: int = 0
        self._registered = False
        self._enabled = enabled
        self.register_error: int = 0   # RegisterHotKey 失败时的 GetLastError

    # -- 状态 ------------------------------------------------------------
    @property
    def triggered(self) -> bool:
        if self._event.is_set():
            return True
        if self.stop_file and self.stop_file.exists():
            self.trigger("检测到停止文件")
            return True
        return False

    @property
    def reason(self) -> str:
        return self._reason

    def trigger(self, reason: str = "手动触发") -> None:
        with self._lock:
            if not self._event.is_set():
                self._reason = reason
                self._event.set()

    def reset(self) -> None:
        with self._lock:
            self._event.clear()
            self._reason = ""
        if self.stop_file and self.stop_file.exists():
            try:
                self.stop_file.unlink()
            except OSError:
                pass

    def raise_if_triggered(self) -> None:
        if self.triggered:
            raise EmergencyStop(f"任务已被急停：{self._reason or '未知原因'}")

    def wait(self, timeout: float | None = None) -> bool:
        """阻塞直到被触发（``wait()`` 不带超时会一直等）。"""
        if self.stop_file:
            deadline = None if timeout is None else time.time() + timeout
            while True:
                if self._event.wait(0.25):
                    return True
                if self.stop_file.exists():
                    self.trigger("检测到停止文件")
                    return True
                if deadline is not None and time.time() >= deadline:
                    return False
        return self._event.wait(timeout)

    # -- 全局热键 --------------------------------------------------------
    def _parse_hotkey(self) -> tuple[int, int]:
        mods = 0
        vk = 0
        for raw in str(self.hotkey).lower().replace(" ", "").split("+"):
            if raw in ("ctrl", "control"):
                mods |= MOD_CONTROL
            elif raw == "alt":
                mods |= MOD_ALT
            elif raw == "shift":
                mods |= MOD_SHIFT
            elif raw in ("win", "super", "cmd"):
                mods |= MOD_WIN
            else:
                vk, _ = winapi.resolve_key(raw)
        if not vk:
            vk = 0x51  # Q
        return mods | MOD_NOREPEAT, vk

    def start(self) -> bool:
        """注册全局热键并启动监听线程；成功返回 ``True``。"""
        if not self._enabled or not winapi.IS_WINDOWS:
            return False
        if self._thread and self._thread.is_alive():
            return self._registered

        mods, vk = self._parse_hotkey()
        ready = threading.Event()
        self._thread = threading.Thread(target=self._listen, args=(mods, vk, ready),
                                        name="autopilot-killswitch", daemon=True)
        self._thread.start()
        ready.wait(3.0)
        return self._registered

    def _listen(self, mods: int, vk: int, ready: threading.Event) -> None:
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())
        try:
            ctypes.set_last_error(0)
            self._registered = bool(_user32.RegisterHotKey(None, 1, mods, vk))
            if not self._registered:
                self.register_error = int(ctypes.get_last_error())
        except Exception:
            self._registered = False
        ready.set()
        if not self._registered:
            return

        msg = MSG()
        try:
            while True:
                ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret in (0, -1):
                    break
                if msg.message == WM_HOTKEY:
                    self.trigger(f"按下急停热键 {self.hotkey}")
        finally:
            try:
                _user32.UnregisterHotKey(None, 1)
            except Exception:
                pass
            self._registered = False

    def stop(self) -> None:
        """注销热键并结束监听线程。"""
        if self._thread_id:
            try:
                _user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._registered = False

    @property
    def hotkey_active(self) -> bool:
        return self._registered

    def __enter__(self) -> "KillSwitch":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
