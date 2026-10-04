# -*- coding: utf-8 -*-
"""内置动作库——模型可以直接调用的全部能力。

动作分成九类：感知、鼠标、键盘、窗口、文件、系统、浏览器、剪贴板、控制。
每个动作都声明了参数、风险等级和是否需要重新感知，注册表据此生成
function-calling schema 并交给安全护栏审查。

约定：
* 涉及坐标的动作同时接受 ``element``（如 ``"E7"``）和 ``x``/``y``，
  优先使用 ``element``——它来自刚采集的 UIA 快照，比模型猜坐标可靠得多。
* 相对路径一律相对于工作目录解析。
"""

from __future__ import annotations

import csv
import ctypes
import io
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import perceive, psbridge, screen, winapi, windows
from .context import ActionContext
from .perceive import Snapshot
from .registry import Param, Registry, Danger

P = Param


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------


def _resolve_point(ctx: ActionContext, element: Any = None,
                   x: Any = None, y: Any = None) -> tuple[int, int]:
    """把 ``element`` 或 ``x,y`` 统一解析成屏幕物理坐标。"""
    if element not in (None, ""):
        snap = ctx.require_snapshot()
        found = snap.element(str(element))
        if found is None:
            raise ValueError(
                f"在最近一次快照里找不到元素 {element!r}。"
                f"请先调用 observe，或用 find_element 按文字查找。")
        return found.center
    if x in (None, "") or y in (None, ""):
        raise ValueError("必须提供 element 或者 x/y 坐标")
    return int(float(x)), int(float(y))


def _path(ctx: ActionContext, raw: Any) -> Path:
    """解析路径：支持 ~、环境变量；相对路径基于工作目录。"""
    text = str(raw).strip().strip('"')
    text = os.path.expandvars(os.path.expanduser(text))
    p = Path(text)
    if not p.is_absolute():
        p = (ctx.workspace / p).resolve()
    return p


def _decode(data: bytes) -> str:
    """控制台输出解码：中文 Windows 常见 GBK，先试 UTF-8。"""
    if not data:
        return ""
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = data.decode(enc)
            if "\ufffd" not in text:
                return text
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


def _pick_window(ctx: ActionContext, title: Any = None, process: Any = None,
                 class_name: Any = None, hwnd: Any = None) -> windows.WindowInfo:
    """按参数找窗口；找不到抛出可读错误。"""
    if hwnd not in (None, "", 0, "0"):
        handle = int(str(hwnd), 16) if str(hwnd).lower().startswith("0x") else int(hwnd)
        if not winapi.is_window(handle):
            raise ValueError(f"窗口句柄无效：{hwnd}")
        pid = winapi.get_window_pid(handle)
        return windows.WindowInfo(
            hwnd=handle, title=winapi.get_window_text(handle),
            class_name=winapi.get_class_name(handle), rect=winapi.get_window_rect(handle),
            pid=pid, process=winapi.process_name(pid))

    if not any([title, process, class_name]):
        fg = windows.get_foreground()
        if fg is None:
            raise ValueError("当前没有前台窗口，请指定 title/process/hwnd")
        return fg

    win = windows.find_window(str(title or ""), str(process or ""), str(class_name or ""))
    if win is None:
        raise ValueError(f"找不到匹配的窗口（title={title!r} process={process!r} "
                         f"class={class_name!r}）")
    return win


# --------------------------------------------------------------------------
# 系统信息采集（无第三方依赖）
# --------------------------------------------------------------------------


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.wintypes.DWORD), ("dwMemoryLoad", ctypes.wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _memory_info() -> dict[str, Any]:
    try:
        stat = MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        gb = 1024 ** 3
        return {
            "total_gb": round(stat.ullTotalPhys / gb, 1),
            "available_gb": round(stat.ullAvailPhys / gb, 1),
            "used_percent": int(stat.dwMemoryLoad),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _cpu_percent(sample: float = 0.25) -> float:
    """用 GetSystemTimes 前后两次采样算 CPU 占用。"""

    class FILETIME(ctypes.Structure):
        _fields_ = [("dwLowDateTime", ctypes.wintypes.DWORD),
                    ("dwHighDateTime", ctypes.wintypes.DWORD)]

    def snap() -> tuple[int, int, int]:
        idle, kern, user = FILETIME(), FILETIME(), FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                              ctypes.byref(user))
        to_int = lambda f: (f.dwHighDateTime << 32) | f.dwLowDateTime
        return to_int(idle), to_int(kern), to_int(user)

    a = snap()
    time.sleep(sample)
    b = snap()
    idle_d = b[0] - a[0]
    total_d = (b[1] - a[1]) + (b[2] - a[2])
    if total_d <= 0:
        return 0.0
    return round(max(0.0, min(100.0, (1 - idle_d / total_d) * 100)), 1)


def _uptime() -> str:
    try:
        ms = ctypes.windll.kernel32.GetTickCount64()
    except Exception:
        return "未知"
    seconds = int(ms // 1000)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    return f"{days}天{hours}小时{minutes}分"


# --------------------------------------------------------------------------
# 构建注册表
# --------------------------------------------------------------------------


def build_registry(ctx: ActionContext) -> Registry:  # noqa: C901 - 动作很多，集中注册更直观
    reg = Registry()

    # ======================================================================
    # 感知
    # ======================================================================
    @reg.action(
        "observe",
        "观察当前电脑状态：前台窗口、所有打开的窗口、可交互元素列表（带 E 编号）、"
        "屏幕 OCR 文字。这是每一步决策前都应该调用的动作。",
        [P("detail", "string", "详细程度", enum=["minimal", "normal", "full"], default="normal"),
         P("include_ocr", "boolean", "是否做屏幕文字识别", default=True),
         P("include_uia", "boolean", "是否读取 UI Automation 元素树", default=True),
         P("max_elements", "integer", "最多列出多少个元素", default=80),
         P("save_image", "boolean", "是否保存截图到磁盘", default=False)],
        danger=Danger.SAFE, category="感知", mutates=False,
        returns="当前屏幕的文本化描述")
    def _observe(detail: str = "normal", include_ocr: bool = True, include_uia: bool = True,
                 max_elements: int = 80, save_image: bool = False) -> Snapshot:
        snap = perceive.observe(include_uia=include_uia, include_ocr=include_ocr,
                               save_image=save_image, ocr_region="window")
        snap.notes.append(f"detail={detail}")
        return ctx.set_snapshot(snap)

    @reg.action(
        "screenshot",
        "截屏并保存到文件，返回文件路径。用于留证或需要看图时。",
        [P("path", "string", "保存路径，默认自动生成", default=""),
         P("region", "string", "区域，格式 x,y,宽,高；留空表示全屏", default=""),
         P("format", "string", "格式", enum=["png", "jpg"], default="png")],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _screenshot(path: str = "", region: str = "", format: str = "png") -> dict[str, Any]:
        if region:
            parts = [int(v.strip()) for v in str(region).split(",")]
            if len(parts) != 4:
                raise ValueError("region 需要 4 个数字：x,y,宽,高")
            grab = screen.capture((parts[0], parts[1], parts[2], parts[3]))
        else:
            grab = screen.capture()
        if path:
            target = _path(ctx, path)
        else:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = ctx.workspace / "var" / "shots" / f"manual-{stamp}.{format}"
        saved = grab.save(target, quality=80)
        return {"path": str(saved), "size": list(grab.size), "bytes": saved.stat().st_size}

    @reg.action(
        "get_screen_text",
        "只做 OCR，返回屏幕上识别到的纯文本。适合读取无法用 UIA 获取的内容。",
        [P("region", "string", "window 或 screen", enum=["window", "screen"], default="window"),
         P("max_chars", "integer", "最多返回多少字符", default=4000)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _get_screen_text(region: str = "window", max_chars: int = 4000) -> str:
        import tempfile
        grab = screen.capture()
        target = "整屏"
        if region == "window":
            fg = windows.get_foreground()
            if fg and fg.rect.width > 40 and fg.rect.height > 40:
                grab = screen.capture(winapi.Rect(
                    max(0, fg.rect.left), max(0, fg.rect.top),
                    min(grab.width, fg.rect.right), min(grab.height, fg.rect.bottom)))
                target = f"前台窗口「{fg.title or fg.process}」"
        tmp = Path(tempfile.gettempdir()) / "autopilot-ps" / "ocr-action.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        grab.scaled(2200, 1400).save(tmp)
        res = psbridge.bridge().ocr(tmp)
        if not res.get("ok"):
            return f"OCR 失败：{res.get('error')}"
        text = perceive.normalize_ocr_text(res.get("text") or "")[: int(max_chars)]
        return f"# OCR 区域：{target}\n{text}"

    @reg.action(
        "get_focused_value",
        "查看当前键盘焦点在哪个控件、里面的内容是什么。"
        "输入文字后用它确认内容真的写进去了（比 OCR 可靠，因为它读的是控件真实值）。",
        [], danger=Danger.SAFE, category="感知", mutates=False)
    def _get_focused_value() -> str:
        res = psbridge.bridge().uia_focused()
        info = res.get("focused")
        if not info:
            return "当前没有可读取的焦点控件（可能焦点在自绘界面上）"
        rect = info.get("rect") or [0, 0, 0, 0]
        lines = [
            f"控件类型：{info.get('type')}",
            f"名称：{info.get('name') or '(无)'}",
            f"位置：({rect[0]},{rect[1]}) {rect[2]}x{rect[3]}",
            f"可用操作：{', '.join(info.get('patterns') or []) or '无'}",
        ]
        value = info.get("value")
        if value not in (None, ""):
            text = str(value)
            preview = text if len(text) <= 1500 else text[:1500] + f"…（共 {len(text)} 字符）"
            lines.append(f"内容：\n{preview}")
        else:
            lines.append("内容：（该控件没有暴露文本值）")
        return "\n".join(lines)

    @reg.action(
        "read_window_ui_text",
        "把某个窗口里所有控件的文本值汇总出来（读取编辑器内容、表单内容等）。",
        [P("title", "string", "窗口标题关键词，留空表示前台窗口", default=""),
         P("process", "string", "进程名", default=""),
         P("max_chars", "integer", "最多返回字符数", default=6000)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _read_window_ui_text(title: str = "", process: str = "",
                             max_chars: int = 6000) -> str:
        win = _pick_window(ctx, title, process, None, None)
        res = psbridge.bridge().uia_tree(win.hwnd, max_depth=14, max_nodes=600)
        if not res.get("ok"):
            return f"读取 UI 失败：{res.get('error')}"
        chunks: list[str] = []
        for item in (res.get("elements") or []):
            value = item.get("value")
            if value:
                chunks.append(f"[{item.get('type')}] {value}")
        if not chunks:
            names = [str(i.get("name")) for i in (res.get("elements") or []) if i.get("name")]
            return ("该窗口没有暴露文本值。可见的控件名称：\n  " +
                    "\n  ".join(names[:40])) if names else "该窗口没有暴露任何文本。"
        text = "\n".join(chunks)
        return text[: int(max_chars)]

    @reg.action(
        "find_element",
        "按名称/文本在当前快照里查找元素，返回带 E 编号的候选列表。"
        "元素太多被截断时用它精确定位。",
        [P("text", "string", "要查找的文字（不区分大小写，可部分匹配）", required=True),
         P("control_type", "string", "限定控件类型，如 Button/Edit/Document", default=""),
         P("actionable_only", "boolean", "只返回可点击/可输入的控件", default=True)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _find_element(text: str, control_type: str = "",
                      actionable_only: bool = True) -> str:
        snap = ctx.require_snapshot()
        hits = snap.find(text, control_type, actionable_only)
        if not hits:
            return f"没有找到包含 {text!r} 的元素。可尝试 observe 后按坐标点击。"
        lines = [f"找到 {len(hits)} 个匹配："]
        lines += [f"  {el.label}" for el in hits[:25]]
        return "\n".join(lines)

    @reg.action(
        "inspect_point",
        "查看某个屏幕坐标上都有什么元素（判断该点能不能点、点的是什么）。",
        [P("x", "integer", "屏幕 x", required=True),
         P("y", "integer", "屏幕 y", required=True)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _inspect_point(x: int, y: int) -> str:
        snap = ctx.require_snapshot()
        info = windows.window_at(int(x), int(y))
        lines = [f"坐标 ({x},{y})"]
        if info:
            lines.append(f"  窗口：{info.one_line()}")
        hits = snap.at(int(x), int(y))
        if not hits:
            lines.append("  该点下没有已登记的元素（可能是自绘界面，可按坐标直接点击）")
        for el in hits[:8]:
            lines.append(f"  {el.label}")
        return "\n".join(lines)

    @reg.action(
        "wait_for_element",
        "等待某个文字的元素出现（比如等对话框弹出、等页面加载完）。",
        [P("text", "string", "要等待的文字", required=True),
         P("timeout", "number", "最长等待秒数", default=15.0),
         P("interval", "number", "轮询间隔秒", default=0.8)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _wait_for_element(text: str, timeout: float = 15.0, interval: float = 0.8) -> str:
        deadline = time.time() + float(timeout)
        while time.time() < deadline:
            ctx.killswitch.raise_if_triggered()
            snap = ctx.take_snapshot(include_uia=True, include_ocr=False, save_image=False)
            hits = snap.find(text)
            if hits:
                return "已出现：\n" + "\n".join(f"  {e.label}" for e in hits[:6])
            time.sleep(float(interval))
        return f"等待 {timeout}s 后仍未找到 {text!r}"

    @reg.action(
        "wait",
        "等待一段时间，或等待画面稳定下来。",
        [P("seconds", "number", "等待秒数", default=1.0),
         P("until_stable", "boolean", "改为等待画面稳定（按需更长）", default=False)],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _wait(seconds: float = 1.0, until_stable: bool = False) -> str:
        if until_stable:
            screen.wait_stable(timeout=max(5.0, float(seconds) * 3))
            return "画面已稳定"
        end = time.time() + float(seconds)
        while time.time() < end:
            ctx.killswitch.raise_if_triggered()
            time.sleep(min(0.2, end - time.time()))
        return f"已等待 {seconds}s"

    @reg.action(
        "list_windows",
        "列出当前所有打开的窗口（含进程名和位置尺寸）。",
        [P("filter", "string", "只看标题或进程名包含该文字的窗口", default="")],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _list_windows(filter: str = "") -> str:
        wins = windows.list_windows()
        if filter:
            needle = filter.lower()
            wins = [w for w in wins
                    if needle in w.title.lower() or needle in w.process.lower()]
        if not wins:
            return "没有匹配的窗口"
        return "\n".join(f"W{i} {w.one_line()}" for i, w in enumerate(wins, 1))

    # ======================================================================
    # 鼠标
    # ======================================================================
    def _do_click(element: Any, x: Any, y: Any, button: str, clicks: int,
                  ensure_front: bool = True) -> str:
        px, py = _resolve_point(ctx, element, x, y)
        if ensure_front:
            windows.ensure_clickable(px, py)
            px, py = _resolve_point(ctx, element, x, y)
        ctx.killswitch.raise_if_triggered()
        winapi.click(px, py, button=button, clicks=int(clicks))
        target = f"元素 {element}" if element else f"坐标 ({px},{py})"
        return f"已在 {target} 执行 {clicks} 次 {button} 点击（落点 {px},{py}）"

    @reg.action(
        "click",
        "单击鼠标。强烈建议用 element 指定目标（如 E7），比猜坐标可靠得多。",
        [P("element", "string", "元素编号，如 E7", default=""),
         P("x", "integer", "屏幕 x 坐标（未用 element 时必填）"),
         P("y", "integer", "屏幕 y 坐标"),
         P("button", "string", "按键", enum=["left", "right", "middle"], default="left"),
         P("clicks", "integer", "点击次数", default=1)],
        danger=Danger.LOW, category="鼠标", aliases=["click_element", "tap"])
    def _click(element: str = "", x: Any = None, y: Any = None,
               button: str = "left", clicks: int = 1) -> str:
        return _do_click(element, x, y, button, clicks)

    @reg.action(
        "double_click",
        "双击鼠标（常用于打开文件/列表项）。",
        [P("element", "string", "元素编号，如 E7", default=""),
         P("x", "integer", "屏幕 x"),
         P("y", "integer", "屏幕 y")],
        danger=Danger.LOW, category="鼠标")
    def _double_click(element: str = "", x: Any = None, y: Any = None) -> str:
        return _do_click(element, x, y, "left", 2)

    @reg.action(
        "right_click",
        "右键单击（打开上下文菜单）。",
        [P("element", "string", "元素编号", default=""),
         P("x", "integer", "屏幕 x"),
         P("y", "integer", "屏幕 y")],
        danger=Danger.LOW, category="鼠标")
    def _right_click(element: str = "", x: Any = None, y: Any = None) -> str:
        return _do_click(element, x, y, "right", 1)

    @reg.action(
        "move_mouse",
        "把鼠标移动到指定位置（不点击）。",
        [P("x", "integer", "屏幕 x", required=True),
         P("y", "integer", "屏幕 y", required=True),
         P("duration", "number", "移动耗时秒，0 为瞬移", default=0.0)],
        danger=Danger.LOW, category="鼠标")
    def _move_mouse(x: int, y: int, duration: float = 0.0) -> str:
        winapi.move_to(int(x), int(y), duration=float(duration))
        return f"鼠标已移动到 ({x},{y})"

    @reg.action(
        "drag",
        "按住鼠标从一点拖到另一点（拖拽文件、框选、拖动滚动条）。",
        [P("x1", "integer", "起点 x", required=True), P("y1", "integer", "起点 y", required=True),
         P("x2", "integer", "终点 x", required=True), P("y2", "integer", "终点 y", required=True),
         P("button", "string", "按键", enum=["left", "right", "middle"], default="left"),
         P("duration", "number", "拖动耗时秒", default=0.4)],
        danger=Danger.LOW, category="鼠标")
    def _drag(x1: int, y1: int, x2: int, y2: int, button: str = "left",
              duration: float = 0.4) -> str:
        winapi.drag(int(x1), int(y1), int(x2), int(y2), button=button, duration=float(duration))
        return f"已从 ({x1},{y1}) 拖到 ({x2},{y2})"

    @reg.action(
        "scroll",
        "滚动滚轮。正数向上或向右，负数向下或向左，单位为「格」。",
        [P("amount", "integer", "滚动格数，如 3 或 -5", required=True),
         P("x", "integer", "在该位置滚动（可选）"),
         P("y", "integer", "在该位置滚动（可选）"),
         P("horizontal", "boolean", "改为横向滚动", default=False)],
        danger=Danger.LOW, category="鼠标")
    def _scroll(amount: int, x: Any = None, y: Any = None, horizontal: bool = False) -> str:
        winapi.scroll(int(amount), x=int(x) if x is not None else None,
                      y=int(y) if y is not None else None, horizontal=bool(horizontal))
        return f"已滚动 {amount} 格"

    # ======================================================================
    # 键盘
    # ======================================================================
    @reg.action(
        "type_text",
        "输入文本（支持中文）。可选先点击某个元素聚焦，可选先清空原有内容。",
        [P("text", "string", "要输入的文字", required=True),
         P("element", "string", "先点击该元素以取得焦点", default=""),
         P("clear_first", "boolean", "先全选删除原有内容", default=False),
         P("submit", "boolean", "输入后按回车", default=False),
         P("method", "string", "输入方式", enum=["auto", "unicode", "clipboard"], default="auto")],
        danger=Danger.LOW, category="键盘", aliases=["type", "input_text"])
    def _type_text(text: str, element: str = "", clear_first: bool = False,
                   submit: bool = False, method: str = "auto") -> str:
        if element:
            _do_click(element, None, None, "left", 1)
            time.sleep(0.15)
        if clear_first:
            winapi.hotkey("ctrl", "a")
            time.sleep(0.05)
            winapi.press("delete")
            time.sleep(0.08)
        # 记录输入落点：焦点被别的窗口抢走时，这一步能立刻暴露问题
        fg = windows.get_foreground()
        where = f"「{fg.title or fg.process}」" if fg else "未知窗口"
        payload = str(text)
        if method == "auto":
            method = "clipboard" if (len(payload) > 40 or not payload.isascii()) else "unicode"
        if method == "clipboard":
            winapi.paste_text(payload)
        else:
            winapi.type_text(payload)
        if submit:
            time.sleep(0.1)
            winapi.press("enter")
        preview = payload if len(payload) <= 60 else payload[:57] + "…"
        return (f"已通过 {method} 向 {where} 输入 {len(payload)} 个字符：{preview!r}\n"
                f"提示：可用 get_focused_value 确认内容是否真的写进去了。")

    @reg.action(
        "press_key",
        "按一次按键，如 enter / esc / tab / f5 / backspace / delete / up。",
        [P("key", "string", "键名", required=True),
         P("presses", "integer", "按几次", default=1)],
        danger=Danger.LOW, category="键盘", aliases=["press"])
    def _press_key(key: str, presses: int = 1) -> str:
        winapi.press(str(key), presses=int(presses))
        return f"已按 {key} ×{presses}"

    @reg.action(
        "hotkey",
        "按组合键，如 ctrl+s、ctrl+shift+esc、alt+f4、win+d。",
        [P("keys", "string", "组合键，用 + 连接", required=True)],
        danger=Danger.LOW, category="键盘", aliases=["key_combo"])
    def _hotkey(keys: str) -> str:
        winapi.hotkey(str(keys))
        return f"已按下组合键 {keys}"

    @reg.action(
        "key_down", "按住某个键不放（配合 key_up 使用）。",
        [P("key", "string", "键名", required=True)],
        danger=Danger.LOW, category="键盘")
    def _key_down(key: str) -> str:
        winapi.key_down(str(key))
        return f"{key} 已按下"

    @reg.action(
        "key_up", "松开某个键。",
        [P("key", "string", "键名", required=True)],
        danger=Danger.LOW, category="键盘")
    def _key_up(key: str) -> str:
        winapi.key_up(str(key))
        return f"{key} 已松开"

    # ======================================================================
    # 窗口
    # ======================================================================
    @reg.action(
        "activate_window",
        "把某个窗口切到前台并聚焦（可先最小化还原）。",
        [P("title", "string", "标题关键词（部分匹配）", default=""),
         P("process", "string", "进程名，如 notepad.exe", default=""),
         P("class_name", "string", "窗口类名", default=""),
         P("hwnd", "string", "窗口句柄", default="")],
        danger=Danger.LOW, category="窗口", aliases=["focus_window", "switch_window"])
    def _activate_window(title: str = "", process: str = "", class_name: str = "",
                         hwnd: str = "") -> str:
        win = _pick_window(ctx, title, process, class_name, hwnd)
        ok = windows.focus_window(win.hwnd)
        if not ok:
            raise RuntimeError(f"无法把窗口切到前台：{win.one_line()}")
        time.sleep(0.25)
        return f"已切换到：{win.title or win.process}"

    @reg.action(
        "launch_app",
        "启动程序、打开文件或用默认浏览器打开网址。对浏览器会自动加上"
        "无障碍参数，这样它的界面元素才能被读取。",
        [P("target", "string", "程序路径 / 文件路径 / URL", required=True),
         P("args", "string", "命令行参数（空格分隔）", default=""),
         P("wait", "number", "启动后等待秒数", default=1.5),
         P("focus", "boolean", "启动后自动把它切到前台", default=True)],
        danger=Danger.MEDIUM, category="窗口", aliases=["open_app", "start"])
    def _launch_app(target: str, args: str = "", wait: float = 1.5,
                    focus: bool = True) -> str:
        arg_list = str(args).split() if args else []
        low = str(target).lower()
        if not arg_list and ("http://" in low or "https://" in low) and _browser_exe():
            arg_list = ["--force-renderer-accessibility"]
        res = windows.launch(str(target), arg_list, wait=float(wait))
        note = ""
        if focus:
            # 新启动的程序不一定是前台（存在"前台窗口锁"），显式等窗口出现再切
            probe = Path(str(target)).stem or str(target)
            win = (windows.wait_for_window(process=f"{probe}.exe", timeout=6.0)
                   or windows.wait_for_window(probe, timeout=1.0))
            if win and windows.focus_window(win.hwnd):
                note = f"，已切到前台「{win.title or win.process}」"
            elif not win:
                note = "，暂未找到它的窗口（可能还在启动）"
        return f"已启动 {target}（pid={res.get('pid')}）{note}"

    @reg.action(
        "close_window",
        "关闭窗口。默认走「优雅关闭」（等同 Alt+F4），"
        "这样应用自己的「是否保存」确认流程能正常弹出；"
        "force=true 会强制关闭，可能丢弃未保存内容。",
        [P("title", "string", "标题关键词", default=""),
         P("process", "string", "进程名", default=""),
         P("hwnd", "string", "窗口句柄", default=""),
         P("force", "boolean", "强制关闭（跳过确认，可能丢数据）", default=False)],
        danger=Danger.MEDIUM, category="窗口", aliases=["close"])
    def _close_window(title: str = "", process: str = "", hwnd: str = "",
                      force: bool = False) -> str:
        win = _pick_window(ctx, title, process, None, hwnd)
        label = win.title or win.process

        if force:
            windows.close_window(win.hwnd, force=True)
            time.sleep(0.4)
            gone = not winapi.is_window(win.hwnd)
            return (f"已强制关闭「{label}」"
                    + ("" if gone else "（窗口仍在，可能需要更长等待）")
                    + "。注意：强制关闭会丢弃该程序未保存的内容。")

        # 优雅关闭：先切到前台再 Alt+F4。
        # 直接 PostMessage(WM_CLOSE) 会让某些现代应用（如 Win11 记事本）
        # 绕过自己的保存确认逻辑，静默丢弃未保存的修改，所以不能默认用它。
        windows.focus_window(win.hwnd)
        time.sleep(0.25)
        winapi.hotkey("alt", "f4")
        time.sleep(0.8)

        if winapi.is_window(win.hwnd):
            fg = windows.get_foreground()
            extra = ""
            if fg and fg.hwnd != win.hwnd and fg.process == win.process:
                extra = "（可能是窗口内的确认框）"
            return (f"已向「{label}」发送关闭请求，但窗口仍然存在{extra}——"
                    f"多半是弹出了「是否保存」之类的确认。\n"
                    f"建议：用 observe 查看当前元素，再用 click 选择「保存 / 不保存 / 取消」；"
                    f"若确认要放弃未保存内容，可用 close_window(force=true)。")
        return f"已关闭「{label}」"

    @reg.action(
        "window_state",
        "改变窗口状态：最小化 / 最大化 / 还原。",
        [P("action", "string", "目标状态", enum=["minimize", "maximize", "restore"],
           required=True),
         P("title", "string", "标题关键词", default=""),
         P("process", "string", "进程名", default=""),
         P("hwnd", "string", "窗口句柄", default="")],
        danger=Danger.LOW, category="窗口", aliases=["minimize", "maximize"])
    def _window_state(action: str, title: str = "", process: str = "",
                      hwnd: str = "") -> str:
        win = _pick_window(ctx, title, process, None, hwnd)
        {"minimize": windows.minimize, "maximize": windows.maximize,
         "restore": windows.restore}[str(action)](win.hwnd)
        return f"已{action}：{win.title or win.process}"

    @reg.action(
        "move_window",
        "移动 / 缩放窗口（宽高留空表示不变）。",
        [P("x", "integer", "左上角 x", required=True),
         P("y", "integer", "左上角 y", required=True),
         P("width", "integer", "宽度"),
         P("height", "integer", "高度"),
         P("title", "string", "标题关键词", default=""),
         P("process", "string", "进程名", default="")],
        danger=Danger.LOW, category="窗口")
    def _move_window(x: int, y: int, width: Any = None, height: Any = None,
                     title: str = "", process: str = "") -> str:
        win = _pick_window(ctx, title, process, None, None)
        windows.move_window(win.hwnd, int(x), int(y),
                            int(width) if width else None,
                            int(height) if height else None)
        return f"已移动窗口到 ({x},{y})"

    @reg.action(
        "always_on_top", "把窗口设为/取消置顶。",
        [P("on_top", "boolean", "是否置顶", default=True),
         P("title", "string", "标题关键词", default=""),
         P("process", "string", "进程名", default="")],
        danger=Danger.LOW, category="窗口")
    def _always_on_top(on_top: bool = True, title: str = "", process: str = "") -> str:
        win = _pick_window(ctx, title, process, None, None)
        windows.set_always_on_top(win.hwnd, bool(on_top))
        return f"窗口已{'置顶' if on_top else '取消置顶'}"

    # ======================================================================
    # 文件
    # ======================================================================
    @reg.action(
        "read_file", "读取文本文件内容。",
        [P("path", "string", "文件路径", required=True),
         P("max_chars", "integer", "最多读取字符数", default=20000),
         P("encoding", "string", "编码", default="utf-8")],
        danger=Danger.SAFE, category="文件", mutates=False)
    def _read_file(path: str, max_chars: int = 20000, encoding: str = "utf-8") -> str:
        p = _path(ctx, path)
        if not p.is_file():
            raise FileNotFoundError(f"文件不存在：{p}")
        try:
            text = p.read_text(encoding=encoding, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = _decode(p.read_bytes())
        if len(text) > int(max_chars):
            return text[: int(max_chars)] + f"\n…（已截断，共 {len(text)} 字符）"
        return text

    @reg.action(
        "write_file", "写入文本文件（可覆盖或追加），自动创建父目录。",
        [P("path", "string", "文件路径", required=True),
         P("content", "string", "要写入的内容", required=True),
         P("mode", "string", "写入方式", enum=["overwrite", "append"], default="overwrite"),
         P("encoding", "string", "编码", default="utf-8")],
        danger=Danger.MEDIUM, category="文件")
    def _write_file(path: str, content: str, mode: str = "overwrite",
                    encoding: str = "utf-8") -> str:
        p = _path(ctx, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a" if mode == "append" else "w", encoding=encoding, newline="") as fh:
            fh.write(str(content))
        return f"已{'追加' if mode == 'append' else '写入'} {p}（{p.stat().st_size} 字节）"

    @reg.action(
        "list_dir", "列出目录内容。",
        [P("path", "string", "目录路径", required=True),
         P("pattern", "string", "通配符过滤，如 *.txt", default="*"),
         P("limit", "integer", "最多列出多少项", default=200)],
        danger=Danger.SAFE, category="文件", mutates=False)
    def _list_dir(path: str, pattern: str = "*", limit: int = 200) -> str:
        p = _path(ctx, path)
        if not p.is_dir():
            raise NotADirectoryError(f"不是目录：{p}")
        items = sorted(p.glob(str(pattern) or "*"))
        lines = [f"{p} 共 {len(items)} 项"]
        for item in items[: int(limit)]:
            if item.is_dir():
                lines.append(f"  [目录] {item.name}/")
            else:
                try:
                    size = item.stat().st_size
                except OSError:
                    size = -1
                lines.append(f"  [文件] {item.name}  {size} 字节")
        if len(items) > int(limit):
            lines.append(f"  … 其余 {len(items) - int(limit)} 项已省略")
        return "\n".join(lines)

    @reg.action(
        "make_dir", "创建目录（含多级）。",
        [P("path", "string", "目录路径", required=True)],
        danger=Danger.MEDIUM, category="文件")
    def _make_dir(path: str) -> str:
        p = _path(ctx, path)
        p.mkdir(parents=True, exist_ok=True)
        return f"目录已就绪：{p}"

    @reg.action(
        "copy_path", "复制文件或目录。",
        [P("src", "string", "源路径", required=True),
         P("dst", "string", "目标路径", required=True),
         P("overwrite", "boolean", "覆盖已存在的目标", default=False)],
        danger=Danger.MEDIUM, category="文件")
    def _copy_path(src: str, dst: str, overwrite: bool = False) -> str:
        s, d = _path(ctx, src), _path(ctx, dst)
        if not s.exists():
            raise FileNotFoundError(f"源路径不存在：{s}")
        if d.exists() and not overwrite:
            raise FileExistsError(f"目标已存在（overwrite=false）：{d}")
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=bool(overwrite))
        else:
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(s, d)
        return f"已复制 {s} -> {d}"

    @reg.action(
        "move_path", "移动 / 重命名文件或目录。",
        [P("src", "string", "源路径", required=True),
         P("dst", "string", "目标路径", required=True),
         P("overwrite", "boolean", "覆盖已存在的目标", default=False)],
        danger=Danger.MEDIUM, category="文件")
    def _move_path(src: str, dst: str, overwrite: bool = False) -> str:
        s, d = _path(ctx, src), _path(ctx, dst)
        if not s.exists():
            raise FileNotFoundError(f"源路径不存在：{s}")
        if d.exists():
            if not overwrite:
                raise FileExistsError(f"目标已存在（overwrite=false）：{d}")
            if d.is_dir():
                shutil.rmtree(d)
            else:
                d.unlink()
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return f"已移动 {s} -> {d}"

    @reg.action(
        "delete_path",
        "删除文件或目录。默认移动到回收站，recursive=true 时永久删除目录。",
        [P("path", "string", "要删除的路径", required=True),
         P("recursive", "boolean", "递归删除目录", default=False),
         P("permanent", "boolean", "永久删除（不进回收站）", default=False)],
        danger=Danger.HIGH, category="文件", aliases=["delete"])
    def _delete_path(path: str, recursive: bool = False, permanent: bool = False) -> str:
        p = _path(ctx, path)
        if not p.exists():
            return f"路径不存在，无需删除：{p}"
        if p.is_dir():
            if not recursive:
                raise IsADirectoryError(f"{p} 是目录，需要 recursive=true")
            shutil.rmtree(p)
            return f"已永久删除目录 {p}"
        if permanent:
            p.unlink()
            return f"已永久删除文件 {p}"
        _recycle(p)
        return f"已把 {p} 移入回收站"

    @reg.action(
        "path_info", "查看路径是否存在、大小、修改时间。",
        [P("path", "string", "路径", required=True)],
        danger=Danger.SAFE, category="文件", mutates=False)
    def _path_info(path: str) -> dict[str, Any]:
        p = _path(ctx, path)
        if not p.exists():
            return {"path": str(p), "exists": False}
        st = p.stat()
        return {
            "path": str(p), "exists": True, "is_dir": p.is_dir(),
            "size": st.st_size if p.is_file() else None,
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
        }

    @reg.action(
        "open_path", "用系统默认程序打开文件或目录。",
        [P("path", "string", "路径", required=True),
         P("wait", "number", "等待秒数", default=1.0)],
        danger=Danger.LOW, category="文件")
    def _open_path(path: str, wait: float = 1.0) -> str:
        p = _path(ctx, path)
        if not p.exists():
            raise FileNotFoundError(f"路径不存在：{p}")
        os.startfile(str(p))  # type: ignore[attr-defined]
        time.sleep(float(wait))
        return f"已打开 {p}"

    # ======================================================================
    # 系统
    # ======================================================================
    @reg.action(
        "run_command",
        "执行命令行命令并返回输出。破坏性命令会被安全策略拦截。",
        [P("command", "string", "要执行的命令", required=True),
         P("shell", "string", "用哪个 shell", enum=["cmd", "powershell"], default="cmd"),
         P("cwd", "string", "工作目录", default=""),
         P("timeout", "number", "超时秒数", default=60.0)],
        danger=Danger.MEDIUM, category="系统", aliases=["shell", "exec"])
    def _run_command(command: str, shell: str = "cmd", cwd: str = "",
                    timeout: float = 60.0) -> str:
        if shell == "powershell":
            argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-Command", str(command)]
        else:
            argv = [os.environ.get("COMSPEC", "cmd.exe"), "/c", str(command)]
        workdir = str(_path(ctx, cwd)) if cwd else str(ctx.workspace)
        ctx.killswitch.raise_if_triggered()
        try:
            proc = subprocess.run(argv, cwd=workdir, capture_output=True,
                                  timeout=float(timeout),
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired:
            return f"命令超时（>{timeout}s）：{command}"
        out = _decode(proc.stdout).strip()
        err = _decode(proc.stderr).strip()
        parts = [f"退出码 {proc.returncode}"]
        if out:
            parts.append(f"标准输出：\n{out[:4000]}")
        if err:
            parts.append(f"标准错误：\n{err[:2000]}")
        if not out and not err:
            parts.append("（无输出）")
        return "\n".join(parts)

    @reg.action(
        "list_processes", "列出正在运行的进程（按内存占用排序）。",
        [P("filter", "string", "只看进程名包含该文字的", default=""),
         P("top", "integer", "最多列出多少个", default=30)],
        danger=Danger.SAFE, category="系统", mutates=False)
    def _list_processes(filter: str = "", top: int = 30) -> str:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = list(csv.reader(io.StringIO(_decode(proc.stdout))))
        entries = []
        needle = str(filter).lower()
        for row in rows:
            if len(row) < 5:
                continue
            name, pid, mem = row[0], row[1], row[4]
            if needle and needle not in name.lower():
                continue
            try:
                mem_mb = int(re.sub(r"[^0-9]", "", mem) or 0) / 1024
            except ValueError:
                mem_mb = 0.0
            entries.append((mem_mb, name, pid))
        entries.sort(reverse=True)
        if not entries:
            return "没有匹配的进程"
        lines = [f"共 {len(entries)} 个进程，按内存降序："]
        for mem_mb, name, pid in entries[: int(top)]:
            lines.append(f"  {name}  pid={pid}  {mem_mb:.1f} MB")
        return "\n".join(lines)

    @reg.action(
        "kill_process", "结束进程（可用进程名或 PID）。",
        [P("target", "string", "进程名（如 notepad.exe）或 PID", required=True),
         P("force", "boolean", "强制结束", default=True)],
        danger=Danger.HIGH, category="系统")
    def _kill_process(target: str, force: bool = True) -> str:
        text = str(target).strip()
        if text.isdigit():
            ok = windows.kill_process(int(text), force=force)
            return f"已结束 PID {text}" if ok else f"结束 PID {text} 失败"
        pid = _find_pid(text)
        if not pid:
            raise ValueError(f"没有找到进程：{text}")
        ok = windows.kill_process(pid, force=force)
        return f"已结束 {text}（pid={pid}）" if ok else f"结束 {text} 失败"

    @reg.action(
        "system_info", "查看系统状态：内存、CPU 占用、开机时长、屏幕信息。",
        [], danger=Danger.SAFE, category="系统", mutates=False)
    def _system_info() -> dict[str, Any]:
        info: dict[str, Any] = {
            "时间": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "屏幕": winapi.screen_summary(),
            "内存": _memory_info(),
            "CPU 占用%": _cpu_percent(),
            "开机时长": _uptime(),
            "工作目录": str(ctx.workspace),
        }
        try:
            usage = shutil.disk_usage(ctx.workspace.anchor or "C:\\")
            info["系统盘"] = {
                "total_gb": round(usage.total / 1024 ** 3, 1),
                "free_gb": round(usage.free / 1024 ** 3, 1),
            }
        except Exception:
            pass
        return info

    @reg.action(
        "volume", "调节系统音量或静音（通过媒体键，每次约 2%）。",
        [P("action", "string", "操作", enum=["up", "down", "mute"], required=True),
         P("steps", "integer", "up/down 时的次数", default=5)],
        danger=Danger.LOW, category="系统")
    def _volume(action: str, steps: int = 5) -> str:
        key = {"up": "volume_up", "down": "volume_down", "mute": "volume_mute"}[str(action)]
        times = 1 if key == "volume_mute" else max(1, int(steps))
        for _ in range(times):
            winapi.press(key)
            time.sleep(0.04)
        return f"已执行音量操作：{action} ×{times}"

    @reg.action(
        "lock_screen", "锁定当前用户会话。",
        [], danger=Danger.MEDIUM, category="系统")
    def _lock_screen() -> str:
        ctypes.windll.user32.LockWorkStation()
        return "已锁定屏幕"

    @reg.action(
        "notify", "弹出 Windows 通知（不阻塞）。",
        [P("title", "string", "标题", required=True),
         P("message", "string", "正文", default="")],
        danger=Danger.SAFE, category="系统", mutates=False)
    def _notify(title: str, message: str = "") -> str:
        res = psbridge.bridge().toast(str(title), str(message))
        return "通知已发送" if res.get("ok") else f"通知失败：{res.get('error')}"

    @reg.action(
        "power",
        "电源操作：睡眠 / 关机 / 重启 / 注销。危险动作，默认被安全策略拦截。",
        [P("action", "string", "操作",
           enum=["sleep", "shutdown", "restart", "logoff", "cancel"], required=True),
         P("delay", "integer", "延迟秒数", default=30)],
        danger=Danger.HIGH, category="系统")
    def _power(action: str, delay: int = 30) -> str:
        mode = str(action)
        if mode == "cancel":
            subprocess.run(["shutdown", "/a"], capture_output=True)
            return "已取消计划中的关机"
        if mode == "sleep":
            subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                           capture_output=True)
            return "已进入睡眠"
        flag = {"shutdown": "/s", "restart": "/r", "logoff": "/l"}[mode]
        if mode == "logoff":
            subprocess.run(["shutdown", "/l"], capture_output=True)
            return "已注销"
        subprocess.run(["shutdown", flag, "/t", str(int(delay))], capture_output=True)
        return f"{'关机' if mode == 'shutdown' else '重启'}已计划，{delay} 秒后执行（可用 cancel 取消）"

    # ======================================================================
    # 剪贴板
    # ======================================================================
    @reg.action("get_clipboard", "读取剪贴板文本。", [],
                danger=Danger.SAFE, category="剪贴板", mutates=False)
    def _get_clipboard() -> str:
        text = winapi.clipboard_get_text()
        return text if text else "（剪贴板为空或不含文本）"

    @reg.action(
        "set_clipboard", "写入剪贴板文本。",
        [P("text", "string", "要写入的文字", required=True)],
        danger=Danger.LOW, category="剪贴板")
    def _set_clipboard(text: str) -> str:
        return "已写入剪贴板" if winapi.clipboard_set_text(str(text)) else "写入剪贴板失败"

    # ======================================================================
    # 浏览器（Playwright，可选依赖）
    #
    # 两种工作模式，关键差别在"登录态"：
    #   browser_debug  用独立 profile 启动带调试端口的 Edge，登录一次就长期保留，
    #                  Playwright 通过 CDP 连上去 —— **需要登录的网站用这个**
    #   browser_open   全新临时 profile，干净但没登录态，适合公开页面
    # ======================================================================
    @reg.action(
        "browser_debug",
        "【网页任务首选】启动一个带调试端口的 Edge 并用 Playwright 接管，登录态保存在"
        "项目自己的 profile 里，登录一次以后长期有效。需要登录的网站（PTA、教务系统、"
        "后台管理等）必须用它，否则新开的浏览器是未登录状态。",
        [P("url", "string", "启动后打开的网址", default=""),
         P("port", "integer", "调试端口", default=9222),
         P("profile", "string", "浏览器配置目录，默认 var/browser-profile（登录态存这里）",
           default="")],
        danger=Danger.LOW, category="浏览器", long_running=True,
        aliases=["browser_connect"])
    def _browser_debug(url: str = "", port: int = 9222, profile: str = "") -> str:
        port = int(port)
        # 已经连着就不重复启动
        if _browser_kind(ctx) == "cdp" and _browser_alive(ctx):
            page = _page(ctx)
            if url:
                page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
            return (f"已连接到调试浏览器（端口 {port}），当前页面：{page.url}（标题：{page.title()}）\n"
                    f"下一步可以用 browser_get_text 看页面内容、browser_eval 执行脚本。")
        _browser_reset(ctx, "准备重新建立连接")

        exe = _browser_exe()
        if not exe:
            raise RuntimeError("找不到 msedge.exe / chrome.exe，无法启动调试浏览器")

        profile_dir = _path(ctx, profile) if profile else (ctx.workspace / "var" / "browser-profile")
        profile_dir.mkdir(parents=True, exist_ok=True)
        cdp = f"http://127.0.0.1:{port}"

        # 端口已在监听 -> 直接连（用户可能自己起过了）
        if _cdp_ready(cdp, timeout=0.6):
            return _browser_attach(ctx, cdp)

        args = [exe,
                f"--remote-debugging-port={port}",
                # 关键：必须用独立 user-data-dir。Chrome/Edge 136+ 禁止用默认配置开调试端口。
                f"--user-data-dir={profile_dir}",
                "--no-first-run", "--no-default-browser-check",
                "--force-renderer-accessibility"]
        if url:
            args.append(str(url))
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

        if not _cdp_ready(cdp, timeout=20.0):
            raise RuntimeError(
                f"启动了 Edge 但调试端口 {port} 一直没就绪。可能原因："
                f"该配置目录已被另一个 Edge 实例占用（先关掉用这个目录的窗口再试）。")

        result = _browser_attach(ctx, cdp)
        return (result + f"\n配置目录：{profile_dir}\n"
                f"提示：如果页面显示未登录，请调用 ask_human 让用户手动登录一次，"
                f"登录态会保存在这个目录里，之后就能直接自动操作。")

    @reg.action(
        "browser_status",
        "查看受控浏览器的连接状态、当前网址和标题。操作网页前先确认它是否可用。",
        [], danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_status() -> str:
        kind = _browser_kind(ctx)
        if not kind:
            return ("当前没有受控浏览器。网页任务建议先用 browser_debug（带登录态）"
                    "或 browser_open（全新无登录）建立连接。")
        if not _browser_alive(ctx):
            _browser_reset(ctx, "查询状态时发现浏览器已关闭")
            return "受控浏览器已经关闭（进程退出了），需要重新 browser_debug / browser_open。"
        page = _page(ctx)
        pages = ctx.browser["context"].pages
        lines = [f"模式：{'调试端口直连（带登录态）' if kind == 'cdp' else '独立持久化上下文'}",
                 f"当前页面：{page.url}",
                 f"标题：{page.title()}",
                 f"打开标签页：{len(pages)} 个"]
        return "\n".join(lines)

    @reg.action(
        "browser_open",
        "用一个**全新**的浏览器配置启动受控浏览器。注意：新配置没有登录态，"
        "需要登录的网站请改用 browser_debug。",
        [P("url", "string", "初始网址", default="about:blank"),
         P("headless", "boolean", "无头模式", default=False),
         P("channel", "string", "浏览器通道", enum=["msedge", "chrome"], default="msedge"),
         P("profile", "string", "浏览器配置目录，默认 var/browser-profile",
           default="")],
        danger=Danger.LOW, category="浏览器", long_running=True)
    def _browser_open(url: str = "about:blank", headless: bool = False,
                      channel: str = "msedge", profile: str = "") -> str:
        if _browser_alive(ctx):
            return "浏览器已在运行，请直接用 browser_goto / browser_get_text"
        _browser_reset(ctx, "准备重新启动")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError("需要先安装 Playwright：pip install playwright") from exc

        profile_dir = _path(ctx, profile) if profile else (
            ctx.workspace / "var" / "browser-profile")
        profile_dir.mkdir(parents=True, exist_ok=True)

        # 同一个配置目录只能被一个浏览器实例占用。
        # browser_debug 起的 Edge 也在用这个目录，直接再开一个只会得到
        # 一句莫名其妙的 "Target page, context or browser has been closed"。
        if _profile_in_use(profile_dir):
            raise RuntimeError(
                f"配置目录正在被另一个浏览器占用：{profile_dir}\n"
                f"通常是你已经用 browser_debug 起过浏览器。请二选一：\n"
                f"  1) 直接 browser_goto 用现成的那个（推荐，它带登录态）；\n"
                f"  2) 先关掉那些 Edge 窗口，或给 browser_open 传一个别的 profile。")

        pw = sync_playwright().start()
        try:
            context = pw.chromium.launch_persistent_context(
                str(profile_dir), channel=str(channel), headless=bool(headless),
                viewport={"width": 1440, "height": 900},
                args=["--force-renderer-accessibility"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            if url and url != "about:blank":
                page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            try:
                pw.stop()
            except Exception:
                pass
            raise RuntimeError(
                f"启动浏览器失败：{exc}\n"
                f"提示：如果提示配置目录被占用，说明已有 Edge 在用 {profile_dir}，"
                f"先关掉那些窗口，或改用 browser_debug 连接。") from exc
        ctx.browser.update({"pw": pw, "context": context, "page": page,
                            "mode": "persistent"})
        return f"浏览器已启动（{channel}），当前页面：{page.url}"

    @reg.action(
        "browser_goto", "在受控浏览器里打开网址。",
        [P("url", "string", "网址", required=True)],
        danger=Danger.LOW, category="浏览器", long_running=True)
    def _browser_goto(url: str) -> str:
        def _do(page: Any) -> str:
            page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
            return f"已打开 {page.url}（标题：{page.title()}）"

        return _browser_call(ctx, _do, "browser_goto")

    @reg.action(
        "browser_get_text",
        "取当前网页的可见文本（已去掉脚本样式）。",
        [P("max_chars", "integer", "最多返回字符数", default=6000),
         P("selector", "string", "只取某个元素内的文本", default="body")],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_get_text(max_chars: int = 6000, selector: str = "body") -> str:
        def _do(page: Any) -> str:
            try:
                node = page.query_selector(str(selector))
                text = node.inner_text() if node else ""
            except Exception:
                text = page.inner_text("body")
            text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
            return text[: int(max_chars)] if text else "（页面没有可见文本）"

        return _browser_call(ctx, _do, "browser_get_text")

    @reg.action(
        "browser_click",
        "点击网页元素：可以给 CSS 选择器，也可以给可见文字。",
        [P("target", "string", "CSS 选择器或可见文字", required=True),
         P("by", "string", "定位方式", enum=["auto", "selector", "text"], default="auto")],
        danger=Danger.LOW, category="浏览器")
    def _browser_click(target: str, by: str = "auto") -> str:
        def _do(page: Any) -> str:
            _locator(page, target, by).first.click(timeout=15000)
            page.wait_for_timeout(300)
            return f"已点击 {target}"

        return _browser_call(ctx, _do, "browser_click")

    @reg.action(
        "browser_fill", "在输入框里填内容。",
        [P("target", "string", "CSS 选择器或占位文字", required=True),
         P("text", "string", "要填入的文字", required=True),
         P("submit", "boolean", "填完按回车", default=False)],
        danger=Danger.LOW, category="浏览器")
    def _browser_fill(target: str, text: str, submit: bool = False) -> str:
        def _do(page: Any) -> str:
            loc = _locator(page, target, "auto").first
            loc.fill(str(text), timeout=15000)
            if submit:
                loc.press("Enter")
                page.wait_for_timeout(500)
            return f"已在 {target} 填入内容"

        return _browser_call(ctx, _do, "browser_fill")

    @reg.action(
        "browser_press", "在页面上按键。",
        [P("key", "string", "键名，如 Enter / Control+A", required=True)],
        danger=Danger.LOW, category="浏览器")
    def _browser_press(key: str) -> str:
        def _do(page: Any) -> str:
            page.keyboard.press(str(key))
            return f"已按键 {key}"

        return _browser_call(ctx, _do, "browser_press")

    @reg.action(
        "browser_eval",
        "在页面里执行 JavaScript 并返回结果。批量填表/读数据时最高效——"
        "一次求值就能省掉几十次点击。",
        [P("script", "string", "JS 代码", required=True)],
        danger=Danger.MEDIUM, category="浏览器")
    def _browser_eval(script: str) -> Any:
        return _browser_call(ctx, lambda page: page.evaluate(str(script)), "browser_eval")

    @reg.action(
        "browser_type",
        "往网页控件里**模拟真实键盘输入**。写代码编辑器（Monaco / CodeMirror / "
        "contenteditable）必须用它——这类编辑器的内容不在隐藏 textarea 里，"
        "browser_fill 和 JS 的 setValue 往往不生效。",
        [P("text", "string", "要输入的内容", required=True),
         P("target", "string", "先点击这个元素以取得焦点（CSS 选择器或可见文字）；"
                               "留空表示对当前焦点直接输入", default=""),
         P("clear_first", "boolean", "先 Ctrl+A + Delete 清空原有内容", default=True),
         P("submit", "boolean", "输入后按回车", default=False),
         P("method", "string", "输入方式：insert 快、type 逐键更兼容",
           enum=["insert", "type"], default="insert")],
        danger=Danger.LOW, category="浏览器")
    def _browser_type(text: str, target: str = "", clear_first: bool = True,
                      submit: bool = False, method: str = "insert") -> str:
        def _do(page: Any) -> str:
            if target:
                _locator(page, target, "auto").first.click(timeout=15000)
                page.wait_for_timeout(250)
            if clear_first:
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                page.wait_for_timeout(120)
            payload = str(text)
            if method == "type":
                page.keyboard.type(payload)
            else:
                page.keyboard.insert_text(payload)
            page.wait_for_timeout(300)
            if submit:
                page.keyboard.press("Enter")
                page.wait_for_timeout(400)
            preview = payload if len(payload) <= 80 else payload[:77] + "…"
            return (f"已用 {method} 方式输入 {len(payload)} 个字符：{preview!r}\n"
                    f"提示：用 browser_eval 读回编辑器内容确认是否真的写进去了"
                    f"（Monaco 可以试 monaco.editor.getModels()[0].getValue()）。")

        return _browser_call(ctx, _do, "browser_type")

    @reg.action(
        "browser_read_image",
        "读取网页上图片里的文字/公式。题目、图表常把数学公式渲染成 <img>，"
        "DOM 里拿不到内容，就用这个：把图放大后截图再做 OCR。",
        [P("index", "integer", "第几张图（从 0 开始）", default=0),
         P("selector", "string", "图片来源选择器", default="img"),
         P("zoom", "number", "放大倍数，字体小就调大", default=3.0),
         P("max_chars", "integer", "最多返回多少字符", default=1200)],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_read_image(index: int = 0, selector: str = "img", zoom: float = 3.0,
                            max_chars: int = 1200) -> str:
        import tempfile

        def _do(page: Any) -> str:
            loc = page.locator(str(selector))
            total = loc.count()
            if total == 0:
                return f"页面上没有匹配 {selector!r} 的图片"

            idx = int(index)
            if idx < 0 or idx >= total:
                return f"共 {total} 张图，index={idx} 越界（有效范围 0~{total - 1}）"

            node = loc.nth(idx)
            info = node.evaluate(
                "el => ({src: el.currentSrc || el.src || '', alt: el.alt || '',"
                " title: el.title || '', w: el.naturalWidth || el.width,"
                " h: el.naturalHeight || el.height})")

            # 先放大再截图：小字号公式 OCR 基本读不出来
            node.evaluate(
                "el => { el.dataset.__apW = el.style.width; el.dataset.__apH = el.style.height;"
                f" el.style.width = (el.getBoundingClientRect().width * {float(zoom)}) + 'px';"
                " el.style.height = 'auto'; }")
            page.wait_for_timeout(250)
            tmp = Path(tempfile.gettempdir()) / "autopilot-ps" / f"web-img-{idx}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            try:
                node.screenshot(path=str(tmp))
            finally:
                node.evaluate(
                    "el => { el.style.width = el.dataset.__apW || '';"
                    " el.style.height = el.dataset.__apH || ''; }")

            res = psbridge.bridge().ocr(tmp)
            text = perceive.normalize_ocr_text(res.get("text") or "")
            head = (f"第 {idx}/{total - 1} 张图（{info['w']}x{info['h']}）\n"
                    f"src: {str(info['src'])[:160]}\n"
                    f"alt: {info['alt'][:120] or '(无)'}")
            if not text:
                return (head + "\nOCR 没识别出文字。"
                        "可以调大 zoom 再试，或直接用样例输入输出去反推规律——"
                        "在图片上反复折腾通常是浪费时间。")
            return head + f"\nOCR 结果：\n{text[: int(max_chars)]}"

        return _browser_call(ctx, _do, "browser_read_image")

    @reg.action(
        "browser_screenshot", "网页截图。",
        [P("path", "string", "保存路径", default=""),
         P("full_page", "boolean", "整页截图", default=True)],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_screenshot(path: str = "", full_page: bool = True) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = _path(ctx, path) if path else (
            ctx.workspace / "var" / "shots" / f"web-{stamp}.png")
        target.parent.mkdir(parents=True, exist_ok=True)
        _browser_call(ctx, lambda page: page.screenshot(path=str(target),
                                                        full_page=bool(full_page)),
                      "browser_screenshot")
        return f"已保存网页截图：{target}"

    @reg.action(
        "browser_close",
        "断开与受控浏览器的连接。默认不会关掉用调试端口启动的浏览器窗口"
        "（那可能是用户正在用的），只解除绑定。",
        [P("force", "boolean", "真的把浏览器进程也关掉", default=False)],
        danger=Danger.LOW, category="浏览器")
    def _browser_close(force: bool = False) -> str:
        kind = _browser_kind(ctx)
        if not kind:
            return "浏览器未在运行"
        browser = ctx.browser.get("browser")
        pw = ctx.browser.get("pw")
        if force and browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        ctx.browser.clear()
        if pw is not None:
            try:
                pw.stop()
            except Exception:
                pass
        if kind == "cdp" and not force:
            return "已断开连接（浏览器窗口保持打开，登录态仍在）"
        return "浏览器已关闭"

    # ======================================================================
    # 控制
    # ======================================================================
    @reg.action(
        "finish",
        "任务完成时调用。给出最终结论，然后停止。",
        [P("summary", "string", "完成情况总结", required=True),
         P("success", "boolean", "是否成功完成", default=True)],
        danger=Danger.SAFE, category="控制", mutates=False)
    def _finish(summary: str, success: bool = True) -> dict[str, Any]:
        ctx.emit("finished", summary=str(summary), success=bool(success))
        return {"finished": True, "success": bool(success), "summary": str(summary)}

    @reg.action(
        "ask_human",
        "遇到必须由人决定的事情（验证码、密码、二选一）时调用，等待人回答。",
        [P("question", "string", "要问的问题", required=True)],
        danger=Danger.SAFE, category="控制", mutates=False)
    def _ask_human(question: str) -> str:
        return f"[需要人工介入] {question}"

    @reg.action(
        "note", "记录一条进度备注（不影响操作，便于复盘）。",
        [P("message", "string", "备注内容", required=True)],
        danger=Danger.SAFE, category="控制", mutates=False)
    def _note(message: str) -> str:
        ctx.info(str(message))
        return "已记录"

    return reg


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------


def _browser_exe() -> str:
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(path).is_file():
            return path
    return ""


def _find_pid(name: str) -> int:
    proc = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
                          capture_output=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for row in csv.reader(io.StringIO(_decode(proc.stdout))):
        if len(row) >= 2 and row[0].lower() == name.lower():
            try:
                return int(row[1])
            except ValueError:
                continue
    return 0


def _recycle(path: Path) -> None:
    """把文件移到回收站（走 Shell API，失败则退回直接删除）。"""
    try:
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT),
                ("pFrom", wintypes.LPCWSTR), ("pTo", wintypes.LPCWSTR),
                ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR),
            ]

        FO_DELETE = 3
        FOF_ALLOWUNDO, FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 0x40, 0x10, 0x4, 0x400
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = str(path.resolve()) + "\0\0"
        op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI
        rc = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        if rc != 0:
            raise OSError(f"SHFileOperation 返回 {rc}")
    except Exception:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


# --------------------------------------------------------------------------
# 浏览器状态管理
#
# 这里的核心教训：**句柄还在内存里，不代表浏览器进程还活着。**
# 之前只判断 `ctx.browser.get("page")` 是否存在，于是浏览器被关掉之后，
# 每个 browser_* 动作都秒报 TargetClosedError，Agent 却以为"浏览器在运行"，
# 白白烧掉剩下的步数。现在统一走 _browser_alive / _browser_call。
# --------------------------------------------------------------------------


def _browser_kind(ctx: ActionContext) -> str:
    """返回 ``""`` / ``"cdp"`` / ``"persistent"``。"""
    return str(ctx.browser.get("mode") or "")


def _browser_alive(ctx: ActionContext) -> bool:
    """受控浏览器是否真的还能用。"""
    page = ctx.browser.get("page")
    if page is None:
        return False
    try:
        return not page.is_closed()
    except Exception:
        return False


def _browser_reset(ctx: ActionContext, reason: str = "") -> None:
    """丢弃失效的浏览器句柄（并把 Playwright driver 收掉）。"""
    if not ctx.browser:
        return
    pw = ctx.browser.get("pw")
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
    ctx.browser.clear()
    if reason:
        ctx.info(f"受控浏览器状态已重置：{reason}")


def _page(ctx: ActionContext) -> Any:
    page = ctx.browser.get("page")
    if page is None:
        raise RuntimeError(
            "当前没有受控浏览器。网页任务建议先调用 browser_debug"
            "（带持久登录态，需要登录的网站必须用它）；公开页面可以用 browser_open。")
    try:
        closed = page.is_closed()
    except Exception:
        closed = True
    if closed:
        _browser_reset(ctx, "底层浏览器进程已退出")
        raise RuntimeError(
            "受控浏览器已经关闭（进程没了，之前的句柄是失效的）。"
            "请重新调用 browser_debug 或 browser_open 建立连接。")
    return page


def _browser_call(ctx: ActionContext, fn: Any, what: str) -> Any:
    """执行一次浏览器操作，并把"中途浏览器被关掉"翻译成可读错误。

    顺便把失效句柄清掉，避免后续每一步都重复撞同一个墙。
    """
    page = _page(ctx)
    try:
        return fn(page)
    except Exception as exc:
        name = type(exc).__name__
        text = str(exc)
        if "TargetClosed" in name or "has been closed" in text:
            _browser_reset(ctx, f"{what} 时检测到浏览器已关闭")
            raise RuntimeError(
                f"{what} 失败：受控浏览器已关闭（可能被手动关闭或进程崩溃）。"
                f"已清除失效状态，请重新 browser_debug。") from exc
        raise


def _profile_in_use(profile_dir: Path) -> bool:
    """判断浏览器配置目录是否正被某个浏览器实例占用。

    同一个 user-data-dir 只能被一个 Chromium 实例打开；再开一个会失败，
    但报出来的是一句毫不相关的 "Target page, context or browser has been closed"。
    这里通过枚举进程命令行来提前给出准确原因。

    （不用 SingletonLock 那套文件：Windows 上的 Chromium 不创建它们。）
    """
    target = str(profile_dir).lower().rstrip("\\/")
    if not target:
        return False
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter "
             "\"Name='msedge.exe' or Name='chrome.exe'\" | "
             "Select-Object -ExpandProperty CommandLine"],
            capture_output=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        return False       # 查不出来就别拦着，让真正的启动错误自己暴露
    text = (proc.stdout or b"").decode("utf-8", "replace").lower()
    return target in text


def _cdp_ready(cdp_url: str, timeout: float = 1.0) -> bool:
    """轮询 CDP 的 /json/version，确认调试端口真的可用了。"""
    import urllib.error
    import urllib.request

    deadline = time.time() + max(0.1, timeout)
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{cdp_url}/json/version", timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.4)
    return False


def _browser_attach(ctx: ActionContext, cdp_url: str) -> str:
    """通过 CDP 接管一个已经用调试端口启动的浏览器（带登录态）。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("需要先安装 Playwright：pip install playwright") from exc

    pw = sync_playwright().start()
    try:
        browser = pw.chromium.connect_over_cdp(cdp_url)
    except Exception as exc:
        try:
            pw.stop()
        except Exception:
            pass
        raise RuntimeError(
            f"连接调试浏览器失败（{cdp_url}）：{exc}\n"
            f"请确认 Edge 是用 --remote-debugging-port 启动的。") from exc

    contexts = browser.contexts
    if not contexts:
        context = browser.new_context()
    else:
        context = contexts[0]          # CDP 模式下第一个就是用户的默认上下文
    pages = [p for p in context.pages if not p.is_closed()]
    page = pages[-1] if pages else context.new_page()

    ctx.browser.update({"pw": pw, "browser": browser, "context": context,
                        "page": page, "mode": "cdp"})
    return (f"已接管调试浏览器（{cdp_url}），当前页面：{page.url}"
            f"（标题：{_safe_title(page)}）")


def _safe_title(page: Any) -> str:
    try:
        return str(page.title())
    except Exception:
        return "未知"


def _locator(page: Any, target: str, by: str = "auto") -> Any:
    """按选择器或可见文字定位元素。"""
    text = str(target)
    if by == "text":
        return page.get_by_text(text, exact=False)
    if by == "selector":
        return page.locator(text)
    looks_like_selector = bool(re.match(r"^[.#\[]|^[a-z]+[>\[.:#\s]", text)) and " " not in text.strip()
    if looks_like_selector:
        try:
            if page.locator(text).count() > 0:
                return page.locator(text)
        except Exception:
            pass
    try:
        loc = page.get_by_text(text, exact=False)
        if loc.count() > 0:
            return loc
    except Exception:
        pass
    try:
        loc = page.get_by_role("button", name=text)
        if loc.count() > 0:
            return loc
    except Exception:
        pass
    return page.locator(text)
