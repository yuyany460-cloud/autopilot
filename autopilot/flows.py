# -*- coding: utf-8 -*-
"""确定性任务流程：不依赖 LLM 的录制与回放。

两种用法：

1. **回放**（``flows/*.json``）：把一串动作写成文件，反复执行。
   适合"每天开机要做的那几件事"这类固定套路——不烧 token、结果稳定。
2. **录制**：用低级钩子监听真实的鼠标键盘操作，自动转成流程文件。
   适合先手动做一遍，再让程序重复。

流程文件格式::

    {
      "name": "早安流程",
      "description": "打开记事本写待办",
      "vars": {"名字": "世界"},
      "steps": [
        {"action": "launch_app", "params": {"target": "notepad.exe", "wait": 2}},
        {"action": "type_text", "params": {"text": "你好，${名字}！${date}"}}
      ]
    }

支持 ``${变量名}``、``${env:环境变量}``、``${date}``、``${time}``、
``${datetime}``、``${workspace}``、``${clipboard}`` 占位符。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from . import winapi, windows
from .executor import ActionResult, Executor

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


# --------------------------------------------------------------------------
# 变量替换
# --------------------------------------------------------------------------


def expand(value: Any, variables: dict[str, str], workspace: Path,
           max_passes: int = 5) -> Any:
    """递归展开 ``${...}`` 占位符。

    变量本身可能又含占位符（例如 ``"内容": "生成于 ${datetime}"``），
    所以要做多轮替换直到结果不再变化。
    """

    def one_pass(node: Any) -> Any:
        if isinstance(node, dict):
            return {k: one_pass(v) for k, v in node.items()}
        if isinstance(node, list):
            return [one_pass(v) for v in node]
        if not isinstance(node, str):
            return node

        def sub(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            if token.startswith("env:"):
                import os
                return os.environ.get(token[4:], "")
            if token == "date":
                return datetime.now().strftime("%Y-%m-%d")
            if token == "time":
                return datetime.now().strftime("%H:%M:%S")
            if token == "datetime":
                return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if token == "timestamp":
                # 每次运行都不同的整数：给临时文件命名用，
                # 避免被"会话恢复"之类的机制干扰成上次的残留状态
                return str(int(time.time()))
            if token == "workspace":
                return str(workspace)
            if token == "clipboard":
                try:
                    return winapi.clipboard_get_text()
                except Exception:
                    return ""
            if token in variables:
                return str(variables[token])
            return match.group(0)  # 未知占位符原样保留，便于排查

        return _PLACEHOLDER.sub(sub, node)

    result = value
    for _ in range(max_passes):
        updated = one_pass(result)
        if updated == result:
            break
        result = updated
    return result


# --------------------------------------------------------------------------
# 流程加载与执行
# --------------------------------------------------------------------------


def load_flow(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"流程文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"流程文件不是合法 JSON：{p}\n{exc}") from exc
    if isinstance(data, list):
        data = {"steps": data}
    if not isinstance(data, dict) or not data.get("steps"):
        raise ValueError(f"流程文件缺少 steps 字段：{p}")
    data.setdefault("name", p.stem)
    return data


def run_flow(executor: Executor, flow: dict[str, Any],
             variables: dict[str, str] | None = None,
             stop_on_error: bool = True,
             on_step: Callable[[int, ActionResult], None] | None = None) -> list[ActionResult]:
    """执行一个流程（同步）。"""
    workspace = executor.ctx.workspace
    variables = {**(flow.get("vars") or {}), **(variables or {})}
    steps = expand(flow.get("steps") or [], variables, workspace)
    return executor.run_steps(steps, stop_on_error=stop_on_error, on_step=on_step)


def iter_flow(executor: Executor, flow: dict[str, Any],
              variables: dict[str, str] | None = None) -> Iterator[ActionResult]:
    """逐步执行流程并 yield 结果（给 Web 控制台做实时输出）。"""
    workspace = executor.ctx.workspace
    variables = {**(flow.get("vars") or {}), **(variables or {})}
    steps = expand(flow.get("steps") or [], variables, workspace)
    for i, step in enumerate(steps, 1):
        executor.ctx.killswitch.raise_if_triggered()
        name = step.get("action") or step.get("do") or ""
        params = step.get("params") or {k: v for k, v in step.items()
                                        if k not in ("action", "do", "params")}
        yield executor.run(str(name), params)


def list_flows(directory: str | Path) -> list[dict[str, Any]]:
    base = Path(directory)
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.glob("*.json")):
        try:
            data = load_flow(p)
        except Exception:
            continue
        out.append({"file": p.name, "name": data.get("name") or p.stem,
                    "description": data.get("description") or "",
                    "steps": len(data.get("steps") or [])})
    return out


# --------------------------------------------------------------------------
# 录制器
# --------------------------------------------------------------------------

WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_QUIT = 0x0012

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

_VK_NAMES = {
    0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc", 0x20: "space",
    0x21: "pageup", 0x22: "pagedown", 0x23: "end", 0x24: "home",
    0x25: "left", 0x26: "up", 0x27: "right", 0x28: "down", 0x2E: "delete",
    0x5B: "win", 0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x14: "capslock",
}
for _i in range(0x70, 0x7C):
    _VK_NAMES[_i] = f"f{_i - 0x6F}"

# 低级键盘钩子报告的是**左右分开**的修饰键码（0xA0..0xA5），
# 而不是通用的 VK_SHIFT/VK_CONTROL/VK_MENU（0x10..0x12）。
# 不做归一化的话，Shift/Ctrl/Alt 会被当成未知键丢掉，
# 组合键还会被录成 "ctrl+vk_a2" 这种没法回放的垃圾。
_MODIFIER_ALIASES = {
    0xA0: 0x10, 0xA1: 0x10,   # 左/右 Shift
    0xA2: 0x11, 0xA3: 0x11,   # 左/右 Ctrl
    0xA4: 0x12, 0xA5: 0x12,   # 左/右 Alt
}
_GENERIC_MODIFIERS = {0x10, 0x11, 0x12, 0x5B, 0x5C}

# vk -> 可读键名（用于组合键里的主键）
_VK_REVERSE: dict[int, str] = {}
for _name, _vk in winapi.VK.items():
    if _vk not in _VK_REVERSE or len(_name) < len(_VK_REVERSE[_vk]):
        _VK_REVERSE[_vk] = _name


def _key_name(vk: int) -> str:
    """把虚拟键码转成可读名字。

    未知键输出 ``vk_0x87`` 这种十六进制写法——它必须能被
    ``winapi.resolve_key`` 原样解析回来，否则录制的流程回放时会按错键。
    """
    if 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    return _VK_NAMES.get(vk) or _VK_REVERSE.get(vk) or f"vk_0x{vk:02x}"


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", winapi.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ctypes.c_void_p)]


class Recorder:
    """低级钩子录制器：记录鼠标点击、滚轮和键盘输入，产出流程 JSON。

    实现说明：用 ``WH_MOUSE_LL`` / ``WH_KEYBOARD_LL`` 全局钩子，
    不需要 DLL 注入，也不需要管理员权限。钩子回调必须尽快返回，
    所以这里只做坐标和按键名记录，元素信息在停录后统一补齐。
    """

    def __init__(self, resolve_element: bool = True, move_threshold: int = 12) -> None:
        self.events: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self._mouse_proc = None
        self._key_proc = None
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._running = False
        self._resolve = resolve_element
        self._threshold = int(move_threshold)
        self._last_move: tuple[int, int] = (0, 0)
        self._pending_text: list[str] = []
        self._typed = 0
        self._input = _InputTranslator()
        # 钩子原始命中次数：录到 0 个操作时，用它区分"钩子没装上"和"没人操作"
        self.raw_events: dict[str, int] = {"mouse": 0, "key": 0}
        self.hooks_ok: dict[str, bool] = {"mouse": False, "key": False}

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> bool:
        if self._running:
            return True
        self._running = True
        self._thread = threading.Thread(target=self._pump, daemon=True, name="autopilot-recorder")
        self._thread.start()
        time.sleep(0.4)
        return bool(self._mouse_proc)

    def stop(self) -> list[dict[str, Any]]:
        self._flush_text()
        self._running = False
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=2.0)
        self._thread = None
        return self.events

    # -- 钩子线程 --------------------------------------------------------
    def _pump(self) -> None:
        user32 = ctypes.windll.user32
        self._thread_id = int(ctypes.windll.kernel32.GetCurrentThreadId())

        HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                                      ctypes.c_size_t, ctypes.c_ssize_t)

        def mouse_cb(code, wparam, lparam):
            if code >= 0 and self._running:
                self.raw_events["mouse"] += 1
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
                    self._on_mouse(int(wparam), info.pt.x, info.pt.y, int(info.mouseData))
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def key_cb(code, wparam, lparam):
            if code >= 0 and self._running:
                self.raw_events["key"] += 1
                try:
                    info = ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    self._on_key(int(wparam), int(info.vkCode), int(info.scanCode))
                except Exception:
                    pass
            return user32.CallNextHookEx(None, code, wparam, lparam)

        self._mouse_proc = HOOKPROC(mouse_cb)
        self._key_proc = HOOKPROC(key_cb)
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, ctypes.c_void_p, w.DWORD)
        user32.UnhookWindowsHookEx.argtypes = (ctypes.c_void_p,)
        # argtypes 必须声明：lParam 是 LONG_PTR，64 位下按默认的 c_int 传会溢出，
        # 让钩子回调每次都抛异常（曾导致录制器静默失效）。
        user32.CallNextHookEx.argtypes = (ctypes.c_void_p, ctypes.c_int,
                                          ctypes.c_size_t, ctypes.c_ssize_t)
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        h_mouse = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mouse_proc, None, 0)
        h_key = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._key_proc, None, 0)
        self.hooks_ok = {"mouse": bool(h_mouse), "key": bool(h_key)}
        if not h_mouse:
            self._running = False
            return

        MSG = _msg_struct()
        m = MSG()
        while self._running:
            ret = user32.GetMessageW(ctypes.byref(m), None, 0, 0)
            if ret in (0, -1):
                break
        try:
            user32.UnhookWindowsHookEx(ctypes.c_void_p(h_mouse))
            if h_key:
                user32.UnhookWindowsHookEx(ctypes.c_void_p(h_key))
        except Exception:
            pass

    # -- 事件处理 --------------------------------------------------------
    def _on_mouse(self, msg: int, x: int, y: int, data: int) -> None:
        if msg == WM_MOUSEMOVE:
            self._last_move = (x, y)
            return
        if msg == WM_MOUSEWHEEL:
            delta = ctypes.c_short((data >> 16) & 0xFFFF).value
            self._flush_text()
            self._append({"action": "scroll",
                          "params": {"amount": 1 if delta > 0 else -1,
                                     "x": x, "y": y}})
            return
        if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN):
            self._flush_text()
            button = {WM_LBUTTONDOWN: "left", WM_RBUTTONDOWN: "right",
                      WM_MBUTTONDOWN: "middle"}[msg]
            self._append({"action": "click",
                          "params": {"x": x, "y": y, "button": button}})

    def _read_modifiers(self) -> tuple[bool, bool, bool]:
        """当前修饰键状态 ``(ctrl, alt, shift)``。"""
        user32 = ctypes.windll.user32
        return (bool(user32.GetAsyncKeyState(0x11) & 0x8000),
                bool(user32.GetAsyncKeyState(0x12) & 0x8000),
                bool(user32.GetAsyncKeyState(0x10) & 0x8000))

    def _on_key(self, msg: int, vk: int, scan: int,
                mods: tuple[bool, bool, bool] | None = None) -> None:
        """处理一个键盘事件。

        ``mods`` 允许注入修饰键状态，这样整条键盘路径可以脱离真实键盘单测。
        """
        if msg not in (WM_KEYDOWN, WM_SYSKEYDOWN):
            return

        vk = _MODIFIER_ALIASES.get(vk, vk)
        if vk in _GENERIC_MODIFIERS:
            # 修饰键自身不产生步骤：它的作用体现在下一个主键的组合上
            return

        ctrl, alt, shift = mods if mods is not None else self._read_modifiers()

        if not (ctrl or alt):
            named = _VK_NAMES.get(vk)
            if named:
                self._flush_text()
                self._append({"action": "press_key", "params": {"key": named}})
                return
            ch = self._input.translate(vk, scan, shift)
            if ch:
                self._pending_text.append(ch)
                return
            # 既不产生字符、也不在已知键名表里（例如 F13~F24）。
            # 仍然记下来——丢掉的话回放就会少一步。
            self._flush_text()
            self._append({"action": "press_key", "params": {"key": _key_name(vk)}})
            return

        combo = "+".join((["ctrl"] if ctrl else []) + (["alt"] if alt else []) +
                         (["shift"] if shift else []) + [_key_name(vk)])
        self._flush_text()
        self._append({"action": "hotkey", "params": {"keys": combo}})

    def _append(self, step: dict[str, Any]) -> None:
        with self._lock:
            if self._resolve:
                self._enrich(step)
            self.events.append(step)

    def _enrich(self, step: dict[str, Any]) -> None:
        """给点击补上它落在哪个窗口/哪个元素上，回放时更稳。"""
        params = step.get("params") or {}
        x, y = params.get("x"), params.get("y")
        if step["action"] == "click" and x is not None:
            try:
                info = windows.window_at(int(x), int(y))
                if info:
                    step["window"] = {"title": info.title, "process": info.process}
            except Exception:
                pass

    def _flush_text(self) -> None:
        if not self._pending_text:
            return
        text = "".join(self._pending_text)
        self._pending_text.clear()
        if text:
            with self._lock:
                self.events.append({"action": "type_text", "params": {"text": text}})
            self._typed += len(text)

    # -- 导出 ------------------------------------------------------------
    def to_flow(self, name: str = "录制流程", description: str = "") -> dict[str, Any]:
        self._flush_text()
        with self._lock:
            steps = list(self.events)
        # 合并连续的同类型点击为双击
        merged: list[dict[str, Any]] = []
        for step in steps:
            prev = merged[-1] if merged else None
            if (prev and step["action"] == "click" and prev["action"] == "click"
                    and abs(prev["params"].get("x", 0) - step["params"].get("x", 0)) <= 2
                    and abs(prev["params"].get("y", 0) - step["params"].get("y", 0)) <= 2):
                prev["params"]["clicks"] = int(prev["params"].get("clicks", 1)) + 1
                continue
            merged.append(step)
        return {
            "name": name,
            "description": description or f"由录制生成，共 {len(merged)} 步",
            "created": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "steps": merged,
        }

    def save(self, path: str | Path, name: str = "录制流程",
             description: str = "") -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        flow = self.to_flow(name, description)
        p.write_text(json.dumps(flow, ensure_ascii=False, indent=2), encoding="utf-8")
        return p

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self.events) + (1 if self._pending_text else 0)


class _InputTranslator:
    """把虚拟键码翻译成字符（正确处理 Shift 和大写）。"""

    def __init__(self) -> None:
        self._user32 = ctypes.windll.user32
        try:
            self._user32.ToUnicode.argtypes = (
                w.UINT, w.UINT, ctypes.POINTER(ctypes.c_ubyte),
                ctypes.POINTER(w.WCHAR), ctypes.c_int, w.UINT)
        except Exception:
            pass

    def translate(self, vk: int, scan: int, shift: bool) -> str:
        state = (ctypes.c_ubyte * 256)()
        if shift:
            state[0x10] = 0x80
        if self._user32.GetKeyState(0x14) & 1:
            state[0x14] = 1
        buf = ctypes.create_unicode_buffer(8)
        try:
            got = self._user32.ToUnicode(int(vk), int(scan), state, buf, 8, 0)
        except Exception:
            return ""
        if got <= 0:
            return ""
        text = buf.value
        return text if text and text.isprintable() else ""


def _msg_struct() -> Any:
    class MSG(ctypes.Structure):
        _fields_ = [("hwnd", ctypes.c_void_p), ("message", w.UINT),
                    ("wParam", ctypes.c_size_t), ("lParam", ctypes.c_ssize_t),
                    ("time", w.DWORD), ("pt_x", w.LONG), ("pt_y", w.LONG)]

    return MSG
