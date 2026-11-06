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

import base64
import csv
import ctypes
import io
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import (chaoxing, extbridge, llm, perceive, psbridge, quizkit, screen, vision,
               winapi, windows)
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


def _oem_encoding() -> str:
    """当前控制台的 OEM 代码页，如中文 Windows 的 cp936。

    cmd 重定向输出时用的是它，所以必须按它解码，不能靠猜。
    """
    try:
        cp = int(ctypes.windll.kernel32.GetOEMCP())
    except Exception:
        cp = 0
    return f"cp{cp}" if cp else "gbk"


def _clean_clixml(text: str) -> str:
    """把 PowerShell 重定向 stderr 时吐的 CLIXML 包装剥掉。

    ``-EncodedCommand`` 下报错会写成 ``#< CLIXML <Objs …><S S="Error">真正的错误</S>…``，
    而且换行被转义成 ``_x000D__x000A_``。模型看到一堆 XML 只会更糊涂。
    """
    if "CLIXML" not in text:
        return text.strip()
    import html

    pieces = re.findall(r'<S S="(?:Error|Warning)">(.*?)</S>', text, re.DOTALL)
    if pieces:
        text = "\n".join(p for p in pieces if p.strip())
    else:
        text = re.sub(r"^#<\s*CLIXML\s*", "", text)
        text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # CLIXML 用 _xHHHH_ 转义控制字符，换行是 _x000D__x000A_
    text = re.sub(r"_x([0-9A-Fa-f]{4})_", lambda m: chr(int(m.group(1), 16)), text)
    return text.strip()


def _decode(data: bytes, encoding: str | None = None) -> str:
    """控制台输出解码。

    指定 ``encoding`` 时按它解；否则回退到启发式。

    **不要依赖启发式**：中文 Windows 的 GBK 字节经常"恰好"是合法 UTF-8
    （例如"值"= ``D6 B5`` 会被当成 U+05B5 解出来变乱码），
    所以能确定编码的地方（PowerShell 我们强制 UTF-8、cmd 用 OEM 代码页）
    一定要显式传进来。
    """
    if not data:
        return ""
    if encoding:
        try:
            return data.decode(encoding, "replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = data.decode(enc)
            if "\ufffd" not in text:
                return text
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")


# 识别「自己包了一层 powershell」的写法
_PS_LAUNCH = re.compile(r"^\s*(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b", re.IGNORECASE)


def _normalize_shell(command: str, shell: str) -> tuple[str, str]:
    """纠正「在 cmd 里自己包一层 powershell」的写法，返回 ``(命令, shell)``。

    模型经常写::

        run_command(command='powershell -NoProfile -Command "…$_…"', shell="cmd")

    这层 cmd 会把内层引号吃掉：实测 ``$_`` 变空、``$keys=`` 变 ``=``、
    ``findstr /i "A B"`` 的引号丢失导致把关键词当成文件名。
    一次失败任务里 9 条命令全因为这个静默作废。

    这里把外层包装剥掉，直接交给 PowerShell 用 ``-EncodedCommand`` 跑。
    """
    text = str(command).strip()
    if shell == "powershell":
        return text, "powershell"

    m = _PS_LAUNCH.match(text)
    if not m:
        return text, "cmd"

    rest = text[m.end():].strip()
    while rest.startswith("-"):
        head, _, tail = rest.partition(" ")
        token = head.lower()
        if token in ("-command", "-c"):
            rest = tail.strip()
            break
        if token in ("-executionpolicy", "-windowstyle", "-inputformat"):
            _, _, rest = tail.strip().partition(" ")   # 这类开关带一个值
        else:
            rest = tail
        rest = rest.strip()

    if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "\"'":
        rest = rest[1:-1].strip()
    return (rest, "powershell") if rest else (text, "cmd")


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



# 一条文本"像不像公式原文"：必须有**数学证据**（关系符、运算符、根号/积分、LaTeX 命令）。
# 不要用"下划线/尖括号"这种弱特征——`equation_1`、`a<b.png` 都会误命中。
_MATH_EVIDENCE = re.compile(r"[=<>≤≥≠≈±]|[+\-*/^]\s*[\w(]|[√∫∑π]|\\[A-Za-z]{2,}")
# 文件名 / URL / data URI：这些是图片地址，不是公式原文
_MEDIA_NAME = re.compile(r"(?i)\.(png|jpe?g|gif|webp|bmp|svg|ico|avif)\s*$|^https?://|^data:")


def _formula_from_dom(*candidates: str) -> str:
    """从 ``alt`` / ``title`` 里认出"这条其实就是公式原文"。

    很多站点把公式原文塞在 ``<img alt="...">``，或者用 KaTeX 渲染时把
    LaTeX 放在 ``<annotation>`` 里。这种时候再截图 + 调视觉模型纯属浪费
    （1 秒 + token），而且比直接取原文更容易读错。

    反例也要挡住：``alt="1/2.png"`` / ``title="a<b.png"`` 是**图片文件名**，
    误当成公式会让 Agent 拿到一个文件名去解题——比读图失败更糟。
    """
    for text in candidates:
        value = str(text or "").strip()
        if not value or len(value) > 400:
            continue
        if _MEDIA_NAME.search(value):
            continue
        if not _MATH_EVIDENCE.search(value):
            continue
        return quizkit.normalize_math(value)
    return ""

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
        "屏幕 OCR 文字。这是每一步决策前都应该调用的动作。"
        "（UIA 已经能读出元素时，为省时间会自动跳过 OCR；"
        "要强制识别屏幕上的字，用 get_screen_text。）",
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
        "double_check",
        "让一个**更强的模型独立重做一遍**，再和你给的答案对比。"
        "提交不可逆、或者自己拿不准的时候用它——比「再想一遍」有用得多，"
        "因为它是真的从头算，不是顺着你的答案找理由。",
        [P("question", "string", "题目原文，选择题要把选项一起写进来", required=True),
         P("my_answer", "string", "你目前的答案", default=""),
         P("context", "string", "补充信息：相关公式、你的判断依据、题目图片读出来的内容",
           default="")],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _double_check(question: str, my_answer: str = "", context: str = "") -> str:
        # 复核失败**不能**挡住提交：它是质量保险，不是必经关卡。
        # 实测 2026-10-19 22:10 复核模型把 8000 输出额度全用在思维链上、content 为空，
        # 连挂两次、白等 3 分 40 秒，任务就卡在"提交前必须复核"再也不提交了。
        try:
            res = llm.review(str(question), str(my_answer), str(context),
                             workspace=ctx.workspace)
        except Exception as exc:                       # noqa: BLE001
            return ("复核不可用：" + str(exc)[:200] + "\n"
                    "**不要因此停下**：自己把答案逐条对一遍（每题依据能不能说清、"
                    "选项文字和判断是否一致），确认后按原计划提交，不要反复重试复核。")
        body = (res.content or "").strip()
        verdict = "未给出结论"
        for line in reversed(body.splitlines()):
            if "结论" in line:
                verdict = line.strip()
                break
        agree = "不一致" not in verdict
        head = "✅ 复核一致" if agree else "⚠️ **复核结果不一致，以复核为准**"
        return f"{head}\n复核结论：{verdict}（{res.elapsed_ms:.0f}ms）\n\n{body}"

    @reg.action(
        "read_screen",
        "**用视觉模型看屏幕**（截图 → 模型）。自绘界面（游戏、UU加速器等）用 UIA 和 OCR "
        "都读不出东西时用这个：可以问\"这个界面上有哪些按钮、都在什么位置\"，"
        "模型会给出带坐标的描述，之后就能按坐标点。",
        [P("region", "string", "截取范围", enum=["window", "screen"], default="window"),
         P("prompt", "string", "想问什么；留空表示把界面上的文字和可点区域都描述出来",
           default="")],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _read_screen(region: str = "window", prompt: str = "") -> str:
        grab = screen.capture()
        where = "整屏"
        origin = (0, 0)
        if region == "window":
            fg = windows.get_foreground()
            if fg and fg.rect.width > 40 and fg.rect.height > 40:
                origin = (max(0, fg.rect.left), max(0, fg.rect.top))
                grab = screen.capture(winapi.Rect(
                    origin[0], origin[1],
                    min(grab.width, fg.rect.right), min(grab.height, fg.rect.bottom)))
                where = f"前台窗口「{fg.title or fg.process}」"
        question = str(prompt).strip() or (
            f"这张界面截图的实际像素尺寸是 {grab.width}x{grab.height}（左上角为 0,0）。"
            "把界面内容描述出来：列出可见的文字、按钮和可点击区域，"
            "需要点击的位置给出**图内像素坐标**（不要假设成 1920x1080）。"
            "如果是代码或数学公式，一字不差地转录，保留缩进和符号。")
        res = vision.read_bytes(grab.to_bytes("png"), prompt=question,
                                workspace=ctx.workspace, max_tokens=1500)
        # 模型给的是图内坐标，模型要点击还得换算回屏幕坐标，这里直接告诉它怎么加
        hint = (f"（图内坐标 + 窗口左上角 ({origin[0]},{origin[1]}) = 屏幕坐标）"
                if origin != (0, 0) else "（图内坐标即屏幕坐标）")
        return (f"{where} {grab.width}x{grab.height} {hint}"
                f"（视觉模型 {res['ms']:.0f}ms）\n{res['text']}")

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
        "启动程序、打开文件或用默认浏览器打开网址。"
        "**target 可以直接给程序名**（如 \"uu\"、\"steam\"、\"微信\"）——"
        "系统会自动去 PATH / App Paths 注册表 / 开始菜单里找，不用你先找它在哪。"
        "想一次做到「启动并确认可用」，就传 wait_title（等标题匹配的窗口出现）。"
        "对浏览器会自动加上无障碍参数，这样它的界面元素才能被读取。",
        [P("target", "string", "程序名 / 程序路径 / 文件路径 / URL", required=True),
         P("args", "string", "命令行参数（空格分隔）", default=""),
         P("wait", "number", "启动后等待秒数", default=1.5),
         P("focus", "boolean", "启动后自动把它切到前台", default=True),
         P("wait_title", "string", "等标题/进程名匹配这个关键词的窗口出现（最长 20 秒）",
           default="")],
        danger=Danger.MEDIUM, category="窗口", aliases=["open_app", "start"])
    def _launch_app(target: str, args: str = "", wait: float = 1.5,
                    focus: bool = True, wait_title: str = "") -> str:
        arg_list = str(args).split() if args else []
        low = str(target).lower()
        if not arg_list and ("http://" in low or "https://" in low) and _browser_exe():
            arg_list = ["--force-renderer-accessibility"]
        res = windows.launch(str(target), arg_list, wait=float(wait))

        where = ""
        if res.get("resolved_from"):
            where = f"（按名字找到：{res['resolved_via']} → {res['launched']}）"
        elif not arg_list and not Path(str(target)).is_file() and "://" not in low:
            # 没解析到，也明确告诉模型"别自己去翻磁盘"
            where = ("（注：没能按名字定位到这个程序。想找它在哪可以用 find_app 动作，"
                     "**不要**用 run_command 递归扫描磁盘，那会把步数耗光）")

        # 等窗口：指定了 wait_title 就按它等，否则按程序名猜
        probe = str(wait_title or "").strip()
        if probe:
            win = (windows.wait_for_window(probe, timeout=20.0)
                   or windows.wait_for_window(process=probe, timeout=1.0))
            if win is None:
                return (f"已启动 {target}（pid={res.get('pid')}）{where}，"
                        f"但等了 20 秒没出现标题含 {probe!r} 的窗口。"
                        f"它可能启动失败、还在加载、或窗口标题和预期不同——"
                        f"用 list_windows 或 app_status 看一眼实际情况。")
            focused = windows.focus_window(win.hwnd) if focus else False
            return (f"已启动并就绪 {target}（pid={res.get('pid')}）{where}，"
                    f"窗口「{win.title or win.process}」"
                    f"{'已切到前台' if focused else '已出现'}")

        note = ""
        if focus:
            # 新启动的程序不一定是前台（存在"前台窗口锁"），显式等窗口出现再切
            stem = Path(str(res.get("launched") or target)).stem or str(target)
            win = (windows.wait_for_window(process=f"{stem}.exe", timeout=6.0)
                   or windows.wait_for_window(stem, timeout=1.0))
            if win and windows.focus_window(win.hwnd):
                note = f"，已切到前台「{win.title or win.process}」"
            elif not win:
                note = ("，暂未找到它的窗口。可以用 wait_title 参数等特定窗口，"
                        "或用 window_state/app_status 查一下")
        return f"已启动 {target}（pid={res.get('pid')}）{where}{note}"

    @reg.action(
        "find_app",
        "按名字查找程序装在哪（PATH → App Paths 注册表 → 开始菜单快捷方式）。"
        "想知道某个程序叫什么/在哪时用它，**不要用 run_command 递归扫盘**。",
        [P("name", "string", "程序名或关键词，如 uu / steam / 微信", required=True),
         P("limit", "integer", "最多返回几个候选", default=8)],
        danger=Danger.SAFE, category="窗口", mutates=False)
    def _find_app(name: str, limit: int = 8) -> str:
        hits = windows.find_app(str(name), limit=int(limit))
        if not hits:
            return (f"没找到匹配 {name!r} 的程序。可以试试更短的英文名，"
                    f"或直接用 launch_app 打开它的完整路径。")
        lines = [f"找到 {len(hits)} 个候选（可直接传给 launch_app 的 target）："]
        for h in hits:
            lines.append(f"  {h['name']}  [{h['via']}]  {h['path']}")
        return "\n".join(lines)

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
        "执行命令行命令并返回输出。破坏性命令会被安全策略拦截。"
        "需要跑 PowerShell 时**直接把 PowerShell 命令写进来**（shell 留默认即可），"
        "不要自己写 \"powershell -Command ...\" 包一层——那层引号会被吃掉。",
        [P("command", "string", "要执行的命令", required=True),
         P("shell", "string", "用哪个 shell", enum=["cmd", "powershell"], default="cmd"),
         P("cwd", "string", "工作目录", default=""),
         P("timeout", "number", "超时秒数", default=60.0)],
        danger=Danger.MEDIUM, category="系统", aliases=["shell", "exec"])
    def _run_command(command: str, shell: str = "cmd", cwd: str = "",
                    timeout: float = 60.0) -> str:
        cmd_text, shell = _normalize_shell(str(command), str(shell))
        workdir = str(_path(ctx, cwd)) if cwd else str(ctx.workspace)
        ctx.killswitch.raise_if_triggered()
        try:
            if shell == "powershell":
                # -EncodedCommand 用 base64(UTF-16LE) 传整段脚本，彻底绕开引号转义。
                # 实测：走 cmd 包一层 powershell 时，$_ / $keys 会被吃掉，命令直接失效。
                #
                # 前缀两件事：
                #   OutputEncoding=UTF8 —— 否则中文输出是 GBK，而不巧 GBK 字节常常
                #   恰好是合法 UTF-8（"值"→U+05B5），解码必然出错
                #   ProgressPreference —— 关掉进度条，不然 stderr 全是 CLIXML 噪音
                prelude = ("$ProgressPreference='SilentlyContinue';"
                           "[Console]::OutputEncoding=[Text.Encoding]::UTF8;")
                encoded = base64.b64encode(
                    (prelude + cmd_text).encode("utf-16-le")).decode("ascii")
                argv = ["powershell.exe", "-NoProfile", "-NonInteractive",
                        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
                proc = subprocess.run(argv, cwd=workdir, capture_output=True,
                                      timeout=float(timeout),
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                out = _decode(proc.stdout, "utf-8-sig")
                err = _clean_clixml(_decode(proc.stderr, "utf-8-sig"))
            else:
                # 必须用字符串 + shell=True。用 [cmd.exe, "/c", cmd] 的列表形式时，
                # Python 的 list2cmdline 会把内层双引号转义成 \"，
                # 而 cmd.exe 不认识这种转义 —— 引号丢失、管道/变量全废。
                proc = subprocess.run(cmd_text, shell=True, cwd=workdir, capture_output=True,
                                      timeout=float(timeout),
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                # cmd 重定向输出用的是 OEM 代码页，按它解，别猜
                out = _decode(proc.stdout, _oem_encoding())
                err = _decode(proc.stderr, _oem_encoding())
        except subprocess.TimeoutExpired:
            return f"命令超时（>{timeout}s）：{cmd_text}"
        out = out.strip()
        err = err.strip()
        parts = [f"退出码 {proc.returncode}"]
        if out:
            parts.append(f"标准输出：\n{out[:4000]}")
        if err:
            parts.append(f"标准错误：\n{err[:2000]}")
        if not out and not err:
            parts.append("（无输出）")
        return "\n".join(parts)

    @reg.action(
        "list_processes",
        "列出正在运行的进程。filter 同时匹配**进程名和窗口标题**，"
        "所以用中文名（如 \"加速器\"、\"steam\"）也能找到。",
        [P("filter", "string", "关键词：匹配进程名或窗口标题", default=""),
         P("top", "integer", "最多列出多少个", default=30)],
        danger=Danger.SAFE, category="系统", mutates=False)
    def _list_processes(filter: str = "", top: int = 30) -> str:
        needle = str(filter).lower()
        # 窗口标题也算关键词：很多程序进程名是英文，用户只记得中文界面名
        title_hits: dict[str, str] = {}
        try:
            for win in windows.list_windows():
                if win.pid:
                    title_hits.setdefault(str(win.pid), win.title)
        except Exception:
            pass

        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"], capture_output=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        rows = list(csv.reader(io.StringIO(_decode(proc.stdout))))
        entries = []
        for row in rows:
            if len(row) < 5:
                continue
            name, pid, mem = row[0], row[1], row[4]
            title = title_hits.get(str(pid), "")
            if needle and needle not in name.lower() and needle not in title.lower():
                continue
            try:
                mem_mb = int(re.sub(r"[^0-9]", "", mem) or 0) / 1024
            except ValueError:
                mem_mb = 0.0
            entries.append((mem_mb, name, pid, title))
        entries.sort(reverse=True)
        if not entries:
            hint = (f"没有匹配 {filter!r} 的进程。"
                    f"可以换更短的关键词，或用 app_status 一次问清"
                    f"（装在哪 / 在不在跑 / 窗口在哪）。") if filter else "没有进程"
            return hint
        lines = [f"共 {len(entries)} 个进程，按内存降序："]
        for mem_mb, name, pid, title in entries[: int(top)]:
            extra = f"  窗口：{title[:50]}" if title else ""
            lines.append(f"  {name}  pid={pid}  {mem_mb:.1f} MB{extra}")
        return "\n".join(lines)

    @reg.action(
        "app_status",
        "**一次问清一个程序的全部情况**：装在哪、在不在运行、窗口在哪。"
        "比 list_processes 再自己找快得多——想问某个程序就先用它。",
        [P("name", "string", "程序名或关键词，如 uu / steam / 微信", required=True)],
        danger=Danger.SAFE, category="系统", mutates=False)
    def _app_status(name: str) -> str:
        needle = str(name).strip().lower()
        if not needle:
            raise ValueError("请给出程序名")

        lines = [f"【{name}】"]

        installed = windows.find_app(name, limit=4)
        if installed:
            lines.append("安装位置：")
            for h in installed:
                lines.append(f"  [{h['via']}] {h['path']}")
        else:
            lines.append("安装位置：没找到（可以试更短的关键词，或直接给完整路径）")

        hits: list[str] = []
        seen: set[tuple[str, str]] = set()
        for win in windows.list_windows(include_hidden=True):
            hay = f"{win.process}\n{win.title}".lower()
            if needle not in hay:
                continue
            if windows.is_helper_window(win):
                continue                       # 内部消息窗口不算"界面"
            dedup = (win.process.lower(), win.title.strip().lower())
            if dedup in seen:
                continue
            seen.add(dedup)
            state = "最小化" if win.minimized else ("前台" if win.foreground else "可见")
            hits.append(f"  {win.process}  pid={win.pid}  [{state}]  "
                        f"{win.title[:50]}  位置={win.rect.left},{win.rect.top} "
                        f"{win.rect.width}x{win.rect.height}")

        if hits:
            lines.append(f"运行状态：在运行，有 {len(hits)} 个可用窗口")
            lines.extend(hits)
        else:
            running = _pid_of(name)
            if running:
                lines.append(f"运行状态：进程在跑（pid={running}），但**没有一个可用窗口**——"
                             "多半在托盘里。下一步点托盘图标或用快捷键唤出界面，"
                             "别再去它进程上找窗口了。")
            else:
                lines.append("运行状态：没有在运行")
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
    # 浏览器
    #
    # 有三条工作模式，优先顺序是从上到下：
    #   ① 浏览器扩展   你**已经开着**的那个浏览器，装一次扩展就能直接操作它的标签页。
    #                  不用重启、不用复制配置、不用重新登录，登录态就是你自己那份。
    #                  装了扩展时所有 browser_* 动作自动走这条路。
    #   ② browser_debug 用独立 profile 启动带调试端口的 Edge，登录一次长期保留。
    #                  Playwright 通过 CDP 连上去 —— 没装扩展时，需要登录的网站用它。
    #   ③ browser_open  全新临时 profile，干净但没有登录态，适合公开页面。
    # ======================================================================
    @reg.action(
        "browser_debug",
        "【网页任务首选】建立浏览器连接。如果浏览器里装了 AutoPilot Bridge 扩展，"
        "会**直接接管你正在用的那个浏览器**（保留你所有的登录态，不新开窗口）；"
        "没装扩展时才会另起一个带调试端口的 Edge（登录态存在项目 profile 里）。",
        [P("url", "string", "**只在确实要新开一个页面时才给**。留空（推荐）就老老实实"
                            "用当前标签页；给了网址也不会顶掉用户正在看的页面", default=""),
         P("port", "integer", "调试端口（仅没装扩展时用得上）", default=9222),
         P("profile", "string", "浏览器配置目录，默认 var/browser-profile（登录态存这里）",
           default="")],
        danger=Danger.LOW, category="浏览器", long_running=True,
        aliases=["browser_connect"], observe="none")
    def _browser_debug(url: str = "", port: int = 9222, profile: str = "") -> str:
        # 这里给扩展**多等一会儿**：它的 service worker 空闲会被浏览器回收，
        # 内容脚本每 4 秒会把它戳醒。等几秒几乎总能等到，
        # 而等不到的代价是"另开一个浏览器"——用户明明开着 Edge，却多出一个窗口，
        # 这是最让人困惑的一种失败。实测就是因为只等 1 秒而误判。
        ext = _ext(ctx, wait=8.0)
        if ext is not None:
            data = ext.call("tabs", {})
            tabs = data.get("tabs") or []
            active = next((t for t in tabs if t.get("active")), tabs[0] if tabs else {})
            current = str(active.get("url") or "")
            # 钉住"工作标签页"：之后每条命令都带这个 tabId，
            # 用户随手切标签页、开别的网页都不会再让动作打偏。
            if active.get("id"):
                ctx.browser["ext_tab"] = int(active["id"])

            # 用户说"把**这个**页面的作业做了"时，他指的就是他现在看着的这一页。
            # 以前这里会拿 url 直接 goto，等于把他正在做的页面顶掉——
            # 实测就是这么"突然返回到上个界面"，还得从头把作业重新找一遍。
            note = ""
            if url and _same_site(current, url):
                note = (f"\n注意：{url} 和当前标签页同站，**我没有跳转**；"
                        f"用户正在看的这一页就是工作区：{_clip(current)}")
            elif url and _blank_page(current):
                ext.call("goto", {"url": str(url)}, timeout=60)
                note = f"\n已在当前（空白）标签页打开：{url}"
            elif url:
                note = (f"\n注意：{url} 和当前标签页不同站，**我没有动用户正在看的页面**"
                        f"（{_clip(current)}）。先 browser_status 看清当前页；"
                        f"要的就是它就别跳，确实需要那个网址再自己 browser_goto。")

            return "\n".join(filter(None, [
                f"已通过浏览器扩展接管你**正在用的**浏览器（端口 {ext.port}），"
                f"共 {data.get('count', 0)} 个标签页。",
                "你的登录态、书签、扩展都在，不用重新登录。",
                "后续 browser_* 动作默认作用在**已钉住的工作标签页**上，你切标签页也不影响。",
                note.strip(),
                "用 browser_status 看完整标签页清单和当前页地址。"]))

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
                f"登录态会保存在这个目录里，之后就能直接自动操作。\n"
                f"（说明：这次没能通过 AutoPilot Bridge 扩展接管用户已开着的浏览器，"
                f"所以另外起了一个。如果用户装了扩展，让他随便开着一个普通网页，"
                f"扩展就会被保活，下次就不会再另开窗口了。）")

    @reg.action(
        "browser_status",
        "查看受控浏览器的连接状态、当前网址和标题。操作网页前先确认它是否可用。",
        [], danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_status() -> str:
        ext = _ext(ctx)
        if ext is not None:
            data = ext.call("tabs", {})
            tabs = data.get("tabs") or []
            active = next((t for t in tabs if t.get("active")), tabs[0] if tabs else {})
            pinned = int(ctx.browser.get("ext_tab") or 0)
            work = next((t for t in tabs if int(t.get("id") or 0) == pinned), None)
            if work is None:                      # 没钉过（或标签页被关了）就现取当前活动页
                work = active
                if work.get("id"):
                    ctx.browser["ext_tab"] = int(work["id"])
            lines = [f"模式：**浏览器扩展**（接管你正在用的这个浏览器，"
                     f"端口 {ext.port}，共 {data.get('count', 0)} 个标签页）",
                     f"工作标签页：[{work.get('id')}] {work.get('title', '')}"
                     f"（已钉住，用户切标签页也不影响）",
                     f"地址：{work.get('url', '')}"]
            if work.get("id") != active.get("id"):
                lines.append(f"注意：你现在看的是 [{active.get('id')}] "
                             f"{active.get('title', '')} —— 那不是 agent 的工作标签页。")
            lines += ["", "标签页清单："]
            for t in tabs[:20]:
                mark = "▶" if t.get("id") == work.get("id") else (
                    "*" if t.get("active") else " ")
                lines.append(f"  {mark} [{t.get('id')}] {str(t.get('title'))[:40]}"
                             f"  —  {str(t.get('url'))[:70]}")
            lines.append("（▶ = agent 的工作标签页，* = 你正在看的）")
            return "\n".join(lines)

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
        danger=Danger.LOW, category="浏览器", long_running=True, observe="none")
    def _browser_goto(url: str) -> str:
        out = _ext_call(ctx, "goto", {"url": str(url)},
                        lambda d: f"已打开 {d.get('url')}（标题：{d.get('title')}）",
                        timeout=60)
        if out is not None:
            return out

        def _do(page: Any) -> str:
            page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
            return f"已打开 {page.url}（标题：{page.title()}）"

        return _browser_call(ctx, _do, "browser_goto")

    @reg.action(
        "browser_get_text",
        "取当前网页的可见文本（已去掉脚本样式）。**会穿透同源 iframe 逐层收集**，"
        "并在每层正文前标出 frame 的 URL —— 正文在 iframe 里的站点（学习通、教务系统等）"
        "一次就能拿到题目，不需要再用 browser_eval 猜结构。",
        [P("max_chars", "integer", "最多返回字符数", default=6000),
         P("selector", "string", "只取某个元素内的文本", default="body"),
         P("depth", "integer", "iframe 递归层数上限", default=4)],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_get_text(max_chars: int = 6000, selector: str = "body",
                          depth: int = 4) -> str:
        # 走通用的 eval 通道下发"穿透同源 iframe"的收集脚本，而不是扩展的 text 通道。
        # 原因：MV3 扩展**不会热重载**，磁盘上改了 background.js 也要用户去
        # edge://extensions 点一次「重新加载」才生效；而 eval 通道对现役扩展立即可用。
        out = _ext_call(ctx, "eval", {"script": _frame_text_script(str(selector), int(depth))},
                        lambda d: (re.sub(r"\n{3,}", "\n\n", (d or "").strip())[: int(max_chars)]
                                   or "（页面没有可见文本）"))
        if out is not None:
            return out

        def _do(page: Any) -> str:
            # 和扩展侧同样的理由：正文常在同源 iframe 里，只读主框架等于什么都没读到。
            # Playwright 能跨域访问 frame，所以这里比扩展侧更彻底，不需要 try 跨域。
            chunks: list[str] = []
            for idx, frame in enumerate(page.frames):
                try:
                    node = frame.query_selector(str(selector)) or frame.query_selector("body")
                    text = node.inner_text() if node else ""
                except Exception:
                    continue
                text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
                if not text:
                    continue
                if idx == 0:
                    chunks.append(text)
                else:
                    try:
                        url = frame.url
                    except Exception:
                        url = ""
                    chunks.append(f"--- frame#{idx} {url} ---\n{text}")
            joined = "\n\n".join(chunks)
            return joined[: int(max_chars)] if joined else "（页面没有可见文本）"

        return _browser_call(ctx, _do, "browser_get_text", state=False)

    @reg.action(
        "browser_click",
        "点击网页元素：可以给 CSS 选择器，也可以给可见文字。",
        [P("target", "string", "CSS 选择器或可见文字", required=True),
         P("by", "string", "定位方式", enum=["auto", "selector", "text"], default="auto")],
        danger=Danger.LOW, category="浏览器", observe="none")
    def _browser_click(target: str, by: str = "auto") -> str:
        out = _ext_call(ctx, "click", {"target": str(target), "by": str(by)},
                        lambda d: f"已点击 {target}（命中 {d.get('tag')}「{d.get('text')}」）"
                                  + _ext_page_state(d))
        if out is not None:
            return out

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
        danger=Danger.LOW, category="浏览器", observe="none")
    def _browser_fill(target: str, text: str, submit: bool = False) -> str:
        out = _ext_call(ctx, "fill", {"target": str(target), "text": str(text),
                                      "submit": bool(submit)},
                        lambda d: f"已在 {target} 填入 {d.get('length')} 个字符")
        if out is not None:
            return out

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
        danger=Danger.LOW, category="浏览器", observe="none")
    def _browser_press(key: str) -> str:
        out = _ext_call(ctx, "press", {"key": str(key)}, lambda d: f"已按键 {key}")
        if out is not None:
            return out

        def _do(page: Any) -> str:
            page.keyboard.press(str(key))
            return f"已按键 {key}"

        return _browser_call(ctx, _do, "browser_press")

    @reg.action(
        "browser_eval",
        "在页面里执行 JavaScript 并返回结果。批量填表/读数据时最高效——"
        "一次求值就能省掉几十次点击。",
        [P("script", "string", "JS 代码", required=True)],
        danger=Danger.MEDIUM, category="浏览器", observe="none")
    def _browser_eval(script: str) -> Any:
        # state=False：这个动作用来**取数据**，往返回值后面追一行页面状态会污染结果
        #
        # 这里必须**先问扩展在不在线**，不能拿"结果是不是 None"当判据：
        # 脚本没有返回值时扩展也回 None（例如最后一句是语句、或者脚本被包进
        # ``(() => { ... })()`` 而里面没写 return），此时若退回 Playwright，
        # 就会报出"当前没有受控浏览器"这种牛头不对马嘴的错，还白扔一步。
        # 实测（2026-10-16 的 PTA 运行）：agent 反复 ``const el = ...``，
        # 第二次声明在同名执行上下文里是 SyntaxError，扩展退化成 IIFE 求值，
        # 于是连续三次返回 None、连着报三次"没有受控浏览器"。
        if _ext(ctx) is not None:
            out = _ext_call(ctx, "eval", {"script": str(script)})
            if out is None:
                return ("脚本执行完了，但没有返回值。要拿数据就让它最后一句是表达式"
                        "（比如 JSON.stringify(...)），或者自己包一层"
                        "「(function(){ ...; return 值; })()」——"
                        "另外别重复 const/let 同名变量，同一个页面上下文里会 SyntaxError。")
            return out
        return _browser_call(ctx, lambda page: page.evaluate(str(script)),
                             "browser_eval", state=False)

    @reg.action(
        "browser_type",
        "往网页控件里**模拟真实键盘输入**。写代码编辑器（Monaco / CodeMirror / "
        "contenteditable）必须用它——这类编辑器的内容不在隐藏 textarea 里，"
        "browser_fill 和 JS 的 setValue 往往不生效。"
        "**target 留空就会自动找到页面上的答题编辑器**（OJ 的编程题就是这么用），"
        "写完会直接读回行数和首行，不用再另外求值确认。"
        "**禁粘贴的页面也能用**：insert 走浏览器级文本插入（不是 paste 事件），"
        "万一被拦会自动升级为逐键输入。",
        [P("text", "string", "要输入的内容", required=True),
         P("target", "string", "先点击这个元素以取得焦点（CSS 选择器或可见文字）；"
                               "**留空自动找答题编辑器**；都不合适才手动指定", default=""),
         P("clear_first", "boolean", "先 Ctrl+A + Delete 清空原有内容", default=True),
         P("submit", "boolean", "输入后按回车", default=False),
         P("method", "string", "输入方式：insert 快（默认，禁粘贴页面也可用）、"
                               "type 逐键更兼容（慢）",
           enum=["insert", "type"], default="insert")],
        danger=Danger.LOW, category="浏览器", observe="none")
    def _browser_type(text: str, target: str = "", clear_first: bool = True,
                      submit: bool = False, method: str = "insert") -> str:
        payload = str(text)
        note = ""
        if method == "type" and ("\n" in payload or "\t" in payload):
            # 逐键通道对换行不可靠（没重载过的扩展会把整段代码挤成一行），多行一律走 insertText
            method = "insert"
            note = "\n（多行内容已自动改用 insertText 通道，避免换行丢失）"

        selector = str(target).strip()
        if selector in ("", "auto", "#auto"):
            selector = _find_editor_selector(ctx)
            note += ("\n（target 留空：已自动找到答题编辑器，不用再去翻 DOM）"
                     if selector else
                     "\n（target 留空但没找到编辑器，请手动给 target）")

        params: dict[str, Any] = {
            "text": payload, "target": selector, "clear_first": bool(clear_first),
            "submit": bool(submit), "method": str(method)}
        try:
            out = _ext_call(ctx, "type", params)
        except extbridge.BridgeError:
            # 自动定位时 id 是打在 React 管的节点上的，重渲染可能把它抹掉——
            # 重新找一次就行，别让这一步白费（agent 会以为是选择器写错了）。
            if not selector.startswith("#__ap"):
                raise
            selector = _find_editor_selector(ctx)
            if not selector:
                raise
            params["target"] = selector
            out = _ext_call(ctx, "type", params)
        if out is not None:
            # ---- 扩展通道也要**读回校验**，不能拿"命令送达"当"写进去了" ----
            # 真实教训（2026-10-22 09:17 的审计日志）：扩展返回 OK，编辑器却是空的，
            # Agent 于是自己发明了 set_clipboard + Ctrl+V —— 而这类页面正好禁粘贴，
            # 一路白烧十几步。这里把"没写进去"变成一个**明确的失败**。
            used = "insert"
            back = _ext_read_text(ctx, selector)
            if not _text_written(back, payload):
                via = _ext_write_via_api(ctx, selector, payload)      # ① 框架 API
                back = _ext_read_text(ctx, selector)
                if _text_written(back, payload):
                    used = f"框架API({via})" if via else "框架API"
                else:
                    _ext_call(ctx, "type", {**params, "method": "type"})   # ② 逐键
                    back = _ext_read_text(ctx, selector)
                    used = "逐键type"
            if not _text_written(back, payload):
                raise RuntimeError(WRITE_FAILED_HINT)
            lines = len(back.splitlines()) or (1 if back else 0)
            head = (back.splitlines()[0] if back else "")[:60]
            return (f"已写入 {len(payload)} 个字符（{used}）{note}"
                    f"\n读回确认：{lines} 行 / {len(back)} 字符，首行 {head!r}")

        def _do(page: Any) -> str:
            if selector:
                _locator(page, selector, "auto").first.click(timeout=15000)
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
            # 读回校验 + 自动升级：有些考试页拦掉了 insertText（禁粘贴/禁程序化输入），
            # 表现是"没报错但编辑器还是空的"。这时立刻改用逐键输入，别让 Agent 白烧一步。
            escalated = False
            if method != "type" and not _text_written(_editor_text(page), payload):
                page.keyboard.press("Control+A")
                page.keyboard.press("Delete")
                page.wait_for_timeout(120)
                page.keyboard.type(payload)
                page.wait_for_timeout(300)
                escalated = True
            if submit:
                page.keyboard.press("Enter")
                page.wait_for_timeout(400)
            preview = payload if len(payload) <= 80 else payload[:77] + "…"
            before = "" if not selector.startswith("#__ap") else (
                "\n（自动定位的编辑器只在扩展通道可用）")
            tail = ""
            if escalated:
                tail = ("\n（insertText 没生效——这页多半禁了粘贴/程序化输入，"
                        "已自动升级为**逐键输入**，会慢一些）")
            return (f"已用 {method} 方式输入 {len(payload)} 个字符：{preview!r}{note}"
                    f"{before}{tail}\n"
                    f"提示：用 browser_eval 读回编辑器内容确认是否真的写进去了"
                    f"（CodeMirror 6 可以试 "
                    f"Array.from(document.querySelectorAll('.cm-content')).map(e=>e.innerText)）。")

        return _browser_call(ctx, _do, "browser_type")

    @reg.action(
        "browser_read_image",
        "读取网页图片里的文字 / 代码 / 数学公式。题目和图表常把公式渲染成 <img>，"
        "DOM 里拿不到内容，就用这个。默认交给视觉模型识别（代码和公式准确率远高于 OCR），"
        "没有视觉能力时自动退回系统 OCR。",
        [P("index", "integer", "第几张图（从 0 开始）", default=0),
         P("selector", "string", "图片来源选择器", default="img"),
         P("method", "string",
           "识别方式：auto 自动（先用常驻 OCR，判为乱码再升级视觉模型，默认，通常 0.01~0.3 秒）/ "
           "vision 只用视觉模型（最准，1~5 秒）/ ocr 只用系统 OCR（最快，公式和代码基本读不对）",
           enum=["auto", "vision", "ocr"], default="auto"),
         P("prompt", "string", "额外要求，比如\"只输出公式本身\"", default=""),
         P("zoom", "number", "放大倍数（仅 ocr 方式需要，字体小就调大）", default=3.0),
         P("max_chars", "integer", "最多返回多少字符", default=1200)],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_read_image(index: int = 0, selector: str = "img", method: str = "vision",
                            prompt: str = "", zoom: float = 3.0,
                            max_chars: int = 1200) -> str:
        import tempfile

        # 扩展接管时**没有 Playwright 页面**，而下面那条路全是 page.locator 写的，
        # 于是每次调用都瞬间报"当前没有受控浏览器"——agent 只会换个选择器再撞一次，
        # 用户看到的就是"遇到图片就卡在那里"（实测 2026-10-16 19:58 连撞两次）。
        # <img> 本来就是个元素，直接借 browser_read_element 那条扩展通道：
        # 视口截图 → 按坐标裁剪 → 视觉模型识别，一次调用拿到内容。
        if _ext(ctx) is not None:
            # 先挑一张**真的能看**的图（跳过隐藏占位图），否则会拿到空白图、
            # 让模型误以为读图坏了，转而去写脚本下载图片（实测白烧十几步）。
            picked, pick_note = _pick_image_selector(ctx, selector, index)
            return pick_note + str(reg.get("browser_read_element").call(
                selector=picked, index=(0 if pick_note else int(index)),
                method=str(method),
                zoom=(float(zoom) if str(method) == "ocr" else 1.0),
                prompt=str(prompt) or "读出这张图片里的文字/公式，逐字保持原样",
                max_chars=int(max_chars), path=""))

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

            head = (f"第 {idx}/{total - 1} 张图（{info['w']}x{info['h']}）\n"
                    f"src: {str(info['src'])[:160]}\n"
                    f"alt: {info['alt'][:120] or '(无)'}")

            # 0ms 路径：很多站点把公式原文放在 alt/title 里（或者干脆是 KaTeX 的
            # annotation）。有就直接用——不用截图、不用调模型，而且 100% 准确。
            dom_text = _formula_from_dom(info.get("alt") or "", info.get("title") or "")
            if str(method) in ("vision", "auto") and dom_text:
                return head + f"\n识别结果（直接取自 DOM，未调用模型）：\n{dom_text[: int(max_chars)]}"

            tmp = Path(tempfile.gettempdir()) / "autopilot-vision" / f"web-img-{idx}.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)

            if str(method) in ("vision", "auto"):
                # 视觉模型会自己把过小的图放大，不需要在页面上动它的样式
                node.screenshot(path=str(tmp), timeout=10000)
                try:
                    res = vision.read_image(tmp, prompt=prompt, workspace=ctx.workspace)
                    return head + f"\n识别结果（视觉模型）：\n{res['text'][: int(max_chars)]}"
                except Exception as exc:
                    ctx.info(f"视觉识别失败（{exc}），退回系统 OCR")
                    head += f"\n（视觉模型不可用：{exc}，已退回 OCR）"

            # OCR 兜底：小字号公式基本读不出来，所以放大再截
            node.evaluate(
                "el => { el.dataset.__apW = el.style.width; el.dataset.__apH = el.style.height;"
                f" el.style.width = (el.getBoundingClientRect().width * {float(zoom)}) + 'px';"
                " el.style.height = 'auto'; }")
            page.wait_for_timeout(250)
            try:
                node.screenshot(path=str(tmp), timeout=10000)
            finally:
                node.evaluate(
                    "el => { el.style.width = el.dataset.__apW || '';"
                    " el.style.height = el.dataset.__apH || ''; }")

            res = psbridge.bridge().ocr(tmp)
            text = perceive.normalize_ocr_text(res.get("text") or "")
            if not text:
                return (head + "\nOCR 没识别出文字。"
                        "可以调大 zoom 再试，或直接用样例输入输出去反推规律——"
                        "在图片上反复折腾通常是浪费时间。")
            return head + f"\nOCR 结果：\n{text[: int(max_chars)]}"

        return _browser_call(ctx, _do, "browser_read_image", state=False)

    @reg.action(
        "browser_quiz_dump",
        "**一次抓出超星/学习通整卷题目清单**（几题、题干有没有文字、每题几张图）。"
        "超星的题干和每个选项都是 PNG 图片（p.ananas.chaoxing.com 直链），DOM 里只剩空的 A/B/C/D，"
        "所以读题**别用 browser_get_text、也别截图**：先 dump 看有几题，再用 browser_quiz_read 读正文。",
        [], danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_quiz_dump() -> str:
        ext = _ext(ctx)
        if ext is None:
            raise RuntimeError("browser_quiz_dump 需要扩展接管：先 browser_debug() 再试")
        items = chaoxing.extract(lambda op, params=None, **kw: _ext_call(ctx, op, params, **kw))
        ctx.browser["quiz"] = items
        if not items:
            return ("这一页没找到题目容器（超星判定页是 .mark_item，作答页是 [id^=question]）。"
                    "确认当前标签页停在作业页面上：browser_status 看一眼地址。")
        return (chaoxing.summary(items) +
                "\n\n读正文：browser_quiz_read(index=1) 读第 1 题；all=true 依次读完整卷。")

    @reg.action(
        "browser_quiz_read",
        "**读超星题目正文**（题干 + 四个选项）。做法是把题干图和选项图**直接下载成原图**，"
        "一次交给视觉模型转录：不截图、不抢焦点、不依赖 CDP 截图，比截图识别又快又准。",
        [P("index", "integer", "第几题（从 1 开始，跟 browser_quiz_dump 列表一致）", default=1),
         P("all", "boolean", "true = 依次读完整卷", default=False),
         P("prompt", "string", "额外要求", default="")],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_quiz_read(index: int = 1, all: bool = False,
                           prompt: str = "") -> str:            # noqa: A002
        ext = _ext(ctx)
        if ext is None:
            raise RuntimeError("browser_quiz_read 需要扩展接管：先 browser_debug() 再试")
        items = ctx.browser.get("quiz") or chaoxing.extract(
            lambda op, params=None, **kw: _ext_call(ctx, op, params, **kw))
        ctx.browser["quiz"] = items
        if not items:
            return "没找到题目：先 browser_quiz_dump 看看这一页有没有题目容器。"
        dest = ctx.workspace / "var" / "quiz"

        if all:
            lines = [f"整卷 {len(items)} 题："]
            for i, item in enumerate(items, 1):
                try:
                    got = chaoxing.read_item(item, dest, workspace=ctx.workspace, prompt=prompt)
                except Exception as exc:                      # noqa: BLE001
                    lines.append(f"\n## 第 {i} 题 读取失败：{str(exc)[:120]}")
                    continue
                lines.append(f"\n## 第 {i} 题（{got['images']} 张图 / {got['ms']:.0f}ms）\n"
                             f"{got['text']}")
            return "\n".join(lines)

        pos = max(1, min(int(index), len(items))) - 1
        got = chaoxing.read_item(items[pos], dest, workspace=ctx.workspace, prompt=prompt)
        stem = items[pos].get("stemText") or ""
        head = f"第 {pos + 1} 题（{got['images']} 张原图 / 视觉 {got['ms']:.0f}ms）"
        if stem:
            head += f"，页面上可见的题头文字：{stem}"
        return head + "\n\n" + (got["text"] or "（视觉模型没返回内容）")


    @reg.action(
        "browser_read_element",
        "截图网页上某个元素并识别里面的文字。**专治 DOM 文字被自定义字体混淆的站点**："
        "超星/学习通的题目是 class=font-cxsecret，DOM 里取到的是假字（如「垼周」），"
        "只有浏览器渲染出来的字形是真的 —— 这时读 DOM 永远读不对，必须走截图识别。"
        "会自动在同源 iframe 里递归查找元素，裁剪后放大再识别，一次调用拿到真文字。",
        [P("selector", "string", "CSS 选择器（会在同源 iframe 里递归查找）", required=True),
         P("index", "integer", "第几个匹配的元素（从 0 开始）", default=0),
         P("method", "string",
           "识别方式：auto 自动（常驻 OCR 先读，乱码才升级视觉模型，默认，最快）/ "
           "ocr 只用系统 OCR / vision 只用视觉模型（最准但慢 1~5 秒）",
           enum=["auto", "ocr", "vision"], default="auto"),
         P("zoom", "number", "裁剪后放大倍数（字小就调大）", default=2.0),
         P("prompt", "string", "visual 方式的额外要求", default=""),
         P("max_chars", "integer", "最多返回多少字符", default=2000),
         P("path", "string", "裁剪图保存路径（留空自动命名）", default="")],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_read_element(selector: str = "", index: int = 0, method: str = "ocr",
                              zoom: float = 2.0, prompt: str = "", max_chars: int = 2000,
                              path: str = "") -> str:
        ext = _ext(ctx)
        if ext is None:
            raise RuntimeError("browser_read_element 需要浏览器扩展桥在线，"
                               "先 browser_debug 接管浏览器再调用")

        # 1) 在同源 iframe 里递归找到目标元素，算出它在**顶层视口**里的坐标。
        #    getBoundingClientRect 给的是元素自己所在视口的坐标，所以要逐层把
        #    frameElement 的偏移加回去，否则 iframe 里的元素会算到错误的位置。
        script = (
            "(() => {"
            f"const sel = {json.dumps(str(selector), ensure_ascii=False)};"
            f"const want = {int(index)};"
            "const hits = [];"
            "const walk = (doc, d) => {"
            "  try { doc.querySelectorAll(sel).forEach(e => hits.push(e)); } catch (_) {}"
            "  if (d >= 4) return;"
            "  let fs = []; try { fs = Array.from(doc.querySelectorAll('iframe, frame')); }"
            "  catch (_) { fs = []; }"
            "  fs.forEach(f => { let fd = null; try { fd = f.contentDocument; } catch (_) {}"
            "    if (fd) walk(fd, d + 1); });"
            "};"
            "walk(document, 0);"
            "if (!hits.length) return JSON.stringify({found: false});"
            "const el = hits[Math.min(want, hits.length - 1)];"
            # 先滚到可见处再量坐标：图片大多在题目下半部分，直接量会在视口外，
            # 于是白白多花一轮"先滚动再读"（agent 还得自己反应过来）。
            "try { el.scrollIntoView({block: 'center', inline: 'center'}); } catch (_) {}"
            "const r = el.getBoundingClientRect();"
            "let x = r.left, y = r.top;"
            "let w = el.ownerDocument.defaultView, guard = 0;"
            "while (w && w !== window && guard++ < 10) {"
            "  const fe = w.frameElement; if (!fe) break;"
            "  const fr = fe.getBoundingClientRect();"
            "  x += fr.left; y += fr.top; w = w.parent;"
            "}"
            "return JSON.stringify({found: true, count: hits.length,"
            "  x, y, width: r.width, height: r.height,"
            "  dpr: window.devicePixelRatio || 1,"
            "  preview: (el.innerText || '').replace(/\\s+/g, ' ').slice(0, 80)});"
            "})()"
        )
        raw = _ext_call(ctx, "eval", {"script": script})
        try:
            info = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
        except (ValueError, TypeError):
            info = {}
        if not info.get("found"):
            return f"没有找到匹配 {selector!r} 的元素（已在同源 iframe 里找过）"
        time.sleep(0.12)          # 等刚触发的滚动/重排落定，否则截到的还是旧位置

        # 2) 截视口图，按坐标裁出来。截图是设备像素，rect 是 CSS 像素，要乘 dpr。
        # 重试一次是给"service worker 刚好被回收、命令石沉大海"留的余地；
        # 但**别重试三次**：日志实测 18:18 和 20:08 各白等了 28 秒和 40 秒。
        import base64 as _b64

        data = None
        last_exc: Exception | None = None
        if not _shot_dead():                 # 断路器：刚挂过就别再白等
            # 只探一次、且只给 3 秒：健康的 CDP 截图是 70~140 毫秒，
            # 超时就说明这条管线挂了，屏幕截图立刻顶上，别重试着空等。
            try:
                data = ext.call("screenshot", {"full_page": False}, timeout=3)
                _mark_shot(True)
            except Exception as exc:
                last_exc = exc
                _mark_shot(False)
                extbridge.wait_ready(4)

        # CDP 截图拿不到就直接换屏幕截图：CDP 的截图管线一旦在这个浏览器会话里卡住，
        # 所有标签页（连 about:blank）都会一直卡，而 eval 是好的——
        # 所以坐标拿得到，屏幕截图也截得到（实测 2026-10-16 20:07 起全灭）。
        via_screen = data is None
        if via_screen:
            target = _screen_shot(ctx, info)
            if target is None:
                raise RuntimeError(f"{_SCREENSHOT_HINT}（原始错误：{last_exc}）")
            size_note, cut = "屏幕截图", False
        else:
            try:
                from PIL import Image
            except ImportError as exc:                  # pragma: no cover
                raise RuntimeError("browser_read_element 需要 Pillow：pip install pillow") from exc

            img = Image.open(io.BytesIO(_b64.b64decode(data.get("base64") or ""))).convert("RGB")
            dpr = float(info.get("dpr") or 1.0)
            left = max(0, int(float(info["x"]) * dpr))
            top = max(0, int(float(info["y"]) * dpr))
            right = min(img.width, int((float(info["x"]) + float(info["width"])) * dpr))
            bottom = min(img.height, int((float(info["y"]) + float(info["height"])) * dpr))
            if right - left < 4 or bottom - top < 4:
                return (f"元素在视口外或尺寸为 0，先滚动到可见处再读"
                        f"（rect={info.get('x')},{info.get('y')} "
                        f"{info.get('width')}x{info.get('height')}）")

            crop = img.crop((left, top, right, bottom))
            # 截图只能拿到视口：元素比视口高/宽时会被**悄悄截掉一截**，
            # 读出来的公式就成了残的。宁可在结果里说清楚，也别让模型以为看到了全部。
            cut = (int((float(info["y"]) + float(info["height"])) * dpr) > img.height
                   or int((float(info["x"]) + float(info["width"])) * dpr) > img.width)
            factor = float(zoom or 1.0)
            if factor > 0 and factor != 1.0:
                crop = crop.resize((max(1, int(crop.width * factor)),
                                    max(1, int(crop.height * factor))), Image.LANCZOS)

            target = _path(ctx, path) if path else (
                ctx.workspace / "var" / "shots"
                / f"elem-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            crop.save(target)
            size_note = f"{crop.width}x{crop.height}"

        head = (f"已裁剪 {target.name}（{size_note}，"
                f"命中 {info.get('count')} 个取第 {index} 个）"
                f"{'（⚠ 元素超出视口，只截到可见部分）' if cut else ''}"
                f"{'（⚠ CDP 截图挂了，已改用**屏幕截图**裁剪；需要浏览器窗口可见）' if via_screen else ''}\n"
                f"DOM 原文（可能是混淆后的假字）：{info.get('preview') or '(空)'}")

        if str(method) == "auto":
            # 先让常驻 OCR 读（小图 6~8ms、整屏 0.3s），它自己判断像不像乱码：
            # 文字干净就直接返回，含公式/代码再升级到视觉模型——省下的 1~5 秒是白得的。
            try:
                label, text = _fast_read(ctx, target, prompt=prompt, mode="auto",
                                         max_chars=int(max_chars))
                if text:
                    return head + f"\n识别结果（{label}）：\n{text}"
            except Exception as exc:                 # noqa: BLE001 - 退回视觉
                ctx.info(f"快速识别失败（{exc}），直接走视觉模型")
                head += f"\n（快速识别不可用：{exc}）"

        if str(method) == "vision":
            try:
                res = vision.read_image(target, prompt=prompt, workspace=ctx.workspace)
                return head + f"\n识别结果（视觉模型 {res['ms']:.0f}ms）：\n{res['text'][: int(max_chars)]}"
            except Exception as exc:
                ctx.info(f"视觉识别失败（{exc}），退回系统 OCR")
                head += f"\n（视觉模型不可用：{exc}，已退回 OCR）"

        res = psbridge.bridge().ocr(target)
        text = perceive.normalize_ocr_text(res.get("text") or "")
        if not text:
            return head + "\nOCR 没识别出文字，调大 zoom 再试。"
        return head + f"\nOCR 结果：\n{text[: int(max_chars)]}"

    @reg.action(
        "read_image",
        "用视觉模型读一张**本地图片**里的文字 / 代码 / 公式（截图、下载的题目图等）。"
        "代码和数学公式的准确率远高于 OCR。",
        [P("path", "string", "图片路径（png/jpg/gif/webp）", required=True),
         P("prompt", "string", "额外要求，比如\"只输出公式\"、\"转成可运行的 Python\"",
           default="")],
        danger=Danger.SAFE, category="感知", mutates=False)
    def _read_image(path: str = "", prompt: str = "") -> str:
        target = _path(ctx, path)
        if not target.is_file():
            raise ValueError(f"图片不存在：{target}")
        res = vision.read_image(target, prompt=prompt, workspace=ctx.workspace)
        size_kb = target.stat().st_size / 1024
        return (f"{target.name}（{size_kb:.0f} KB，视觉模型耗时 {res['ms']:.0f}ms）\n"
                f"{res['text']}")


    @reg.action(
        "browser_tabs",
        "查看 / 切换浏览器里已经打开的标签页。装上 AutoPilot Bridge 扩展后，"
        "可以用它接管你**手动打开**的那些页面：先 list 看有哪些，再 switch 切过去操作。",
        [P("action", "string", "操作", enum=["list", "switch", "close"], default="list"),
         P("tab_id", "integer", "switch/close 时的标签页 id（list 里能看到）", default=0)],
        danger=Danger.LOW, category="浏览器", mutates=False, observe="none")
    def _browser_tabs(action: str = "list", tab_id: int = 0) -> str:
        ext = _ext(ctx)
        if ext is None:
            return ("浏览器扩展没连接，列不了你手动打开的标签页。"
                    "没装扩展的话用 browser_status 看内置浏览器里的页面。")

        act = str(action)
        if act == "list":
            data = ext.call("tabs", {})
            tabs = data.get("tabs") or []
            lines = [f"共 {data.get('count', 0)} 个标签页："]
            for t in tabs:
                mark = "▶" if t.get("active") else " "
                lines.append(f"  {mark} [{t.get('id')}] {str(t.get('title'))[:44]}"
                             f"  —  {str(t.get('url'))[:70]}")
            return "\n".join(lines)

        tid = int(tab_id or 0)
        if not tid:
            raise ValueError("需要给 tab_id（先用 action=list 看有哪些）")
        if act == "switch":
            # 激活标签页：扩展那边用 tabs.update；同时把工作标签页挪过去，
            # 否则后面的动作还是打在原来那个（钉住的）标签上。
            out = str(ext.call("activate", {"tabId": tid}))
            ctx.browser["ext_tab"] = tid
            return out + f"\n（工作标签页已切到 [{tid}]，后续动作都作用在它上面）"
        if act == "close":
            out = str(ext.call("close_tab", {"tabId": tid}))
            if ctx.browser.get("ext_tab") == tid:
                ctx.browser.pop("ext_tab", None)
                out += "\n（关掉的正是工作标签页，已解除钉住）"
            return out
        raise ValueError(f"不认识的操作：{action}")

    @reg.action(
        "browser_screenshot", "网页截图。整页截图在超长页面上可能卡住（扩展会在 12 秒后"
                              "自动退回可视区域截图），只要一小块就用 shot(region=...) 更快。",
        [P("path", "string", "保存路径", default=""),
         P("full_page", "boolean", "整页截图。**默认 False**：整页截图（CDP 的 "
                                  "captureBeyondViewport）在长页面/固定定位页面上会挂死，"
                                  "实测卡满桥超时；要整页就显式给 true，并准备好吃超时",
           default=False)],
        danger=Danger.SAFE, category="浏览器", mutates=False)
    def _browser_screenshot(path: str = "", full_page: bool = False) -> str:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = _path(ctx, path) if path else (
            ctx.workspace / "var" / "shots" / f"web-{stamp}.png")
        target.parent.mkdir(parents=True, exist_ok=True)

        ext = _ext(ctx)
        if ext is not None:
            import base64 as _b64

            def _cdp(full: bool) -> dict[str, Any] | None:
                """走 CDP 截图；挂了返回 None（确认超时就记断路器），不抛异常。"""
                if _shot_dead():
                    return None
                try:
                    data = ext.call("screenshot", {"full_page": bool(full)}, timeout=3)
                    _mark_shot(True)
                    return data if isinstance(data, dict) else None
                except Exception as exc:
                    if "超时" in str(exc) or "timeout" in str(exc).lower():
                        _mark_shot(False)       # 确认挂死，5 分钟内别再白等
                    return None

            def _screen_fallback() -> str | None:
                shot = _screen_shot(ctx)
                return (f"已保存网页截图：{shot}"
                        f"（⚠ CDP 截图挂了，改用**屏幕截图**截浏览器窗口）") if shot else None

            data, note = _cdp(bool(full_page)), ""
            if data is None and full_page:
                data = _cdp(False)              # 整页不行就退可视区
                note = "（⚠ 要的是整页，实际只截到可视区域）" if data else ""
            if data is None:
                fallback = _screen_fallback()
                if fallback:
                    return fallback
                raise RuntimeError(_SCREENSHOT_HINT)
            target.write_bytes(_b64.b64decode(data.get("base64") or ""))
            return f"已保存网页截图：{target}{note}"

        _browser_call(ctx, lambda page: page.screenshot(path=str(target),
                                                        full_page=bool(full_page),
                                                        timeout=15000),
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


# 读回编辑器内容（不区分 Monaco / CodeMirror / textarea / contenteditable）
_READ_EDITOR_JS = r"""
() => {
  const isRO = e => ((e.parentElement && e.parentElement.className) || '').includes('readOnly');
  const visible = e => e && e.offsetParent !== null;
  const walkCE = node => {
    let out = '';
    (node.childNodes || []).forEach(n => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeName === 'BR') { out += '\n'; return; }
      if (n.nodeType !== 1) return;
      const block = /^(DIV|P|PRE|LI|TR)$/.test(n.tagName);
      if (block && out && !out.endsWith('\n')) out += '\n';
      out += walkCE(n);
      if (block) out += '\n';
    });
    return out;
  };
  const cs = Array.from(document.querySelectorAll('.cm-content')).filter(e => !isRO(e));
  const c = cs.find(visible) || cs[0];
  if (c) return c.innerText || '';
  try {
    if (window.monaco && window.monaco.editor) {
      const models = window.monaco.editor.getModels();
      if (models.length) return models[0].getValue();
    }
  } catch (e) {}
  const tas = Array.from(document.querySelectorAll('textarea')).filter(t => !isRO(t));
  const ta = tas.find(visible) || tas[0];
  if (ta) return ta.value || '';
  const ces = Array.from(document.querySelectorAll('[contenteditable=true]'));
  const ce = ces.find(visible) || ces[0];
  return ce ? walkCE(ce) : '';
}
"""


def _editor_text(page: Any) -> str:
    """读回编辑器内容；读不到就返回空串（不抛异常，避免打断升级判断）。"""
    try:
        return str(page.evaluate(_READ_EDITOR_JS) or "")
    except Exception:
        return ""


def _text_written(back: str, payload: str) -> bool:
    """内容是不是真的写进去了（忽略空白，防止缩进/换行表示差异误判）。"""
    squeeze = lambda s: re.sub(r"\s+", "", (s or "").replace("\u00a0", " "))
    target = squeeze(payload)
    return bool(target) and squeeze(back) == target


# --------------------------------------------------------------------------
# 扩展通道的"写不进去"兜底
#
# 扩展那边的 type 只负责把命令**送达**：点元素 → Ctrl+A/Delete → Input.insertText。
# 如果点击没落在真正的 editable 上（编辑器重渲染、焦点被抢），命令会成功返回、
# 内容却一个字都没进去。**不能把"送达"当成"写进去了"**——实测 Agent 因此
# 自己发明了"复制到剪贴板再 Ctrl+V"，而这类页面正好禁粘贴，白烧十几步。
# --------------------------------------------------------------------------

WRITE_FAILED_HINT = (
    "写入失败：insert / 框架API / 逐键 三条通道都试过，编辑器里仍然是空的。\n"
    "**不要再试 set_clipboard + Ctrl+V（或 browser_press(\"Control+V\")）**——"
    "这类页面专门拦粘贴，实测只会白烧步数。可以：\n"
    "  1) browser_eval 确认目标是不是只读的样例块（class 里有 readOnly）；\n"
    "  2) 换一个 target 指向真正的答题编辑器再试一次；\n"
    "  3) 先跳过这一题，做下一题；\n"
    "  4) ask_human 请人手动把这段代码粘进去。")

# 读回编辑器的真实内容（扩展通道：只支持 eval，所以用 IIFE 返回字符串）
_EXT_READ_JS = r"""
(() => {
  const sel = __SEL__;
  let el = null;
  try { el = sel ? document.querySelector(sel) : null; } catch (e) { el = null; }
  if (!el) {
    const RO = "[class*='readOnly'], [class*='readonly'], [aria-readonly='true'], [readonly]";
    const cand = [...document.querySelectorAll(
        '.cm-content, .monaco-editor textarea, textarea, [contenteditable=true]')]
      .filter(e => {
        const r = e.getBoundingClientRect();
        return r.width > 60 && r.height > 24 && !e.closest(RO);
      });
    el = cand[0] || null;
  }
  if (!el) return '';
  if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') return el.value || '';
  const walk = node => {
    let out = '';
    (node.childNodes || []).forEach(n => {
      if (n.nodeType === 3) { out += n.nodeValue; return; }
      if (n.nodeName === 'BR') { out += '\n'; return; }
      if (n.nodeType !== 1) return;
      const block = /^(DIV|P|PRE|LI|TR)$/.test(n.tagName);
      if (block && out && !out.endsWith('\n')) out += '\n';
      out += walk(n);
      if (block) out += '\n';
    });
    return out;
  };
  return walk(el);
})()
"""

# 直接改框架内部值：Monaco model / CM6 dispatch / CM5 / textarea 原型 setter。
# 这条路**不产生任何输入事件**，所以禁粘贴、禁 beforeinput 的页面也拦不住它。
_EXT_WRITE_API_JS = r"""
(() => {
  const code = __CODE__;
  try {
    if (window.monaco && window.monaco.editor) {
      const models = window.monaco.editor.getModels();
      if (models.length) { models[0].setValue(code); return 'monaco.setValue'; }
    }
  } catch (e) {}
  try {
    let el = null;
    try { el = document.querySelector(__SEL__); } catch (e) { el = null; }
    if (!el) el = document.querySelector('.cm-content');
    const cmv = el && (el.cmView || (el.parentElement && el.parentElement.cmView));
    const view = cmv && (cmv.view || (cmv.rootView && cmv.rootView.view));
    if (view && view.dispatch && view.state && view.state.doc) {
      view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: code}});
      return 'cm6.dispatch';
    }
  } catch (e) {}
  try {
    const c5 = document.querySelector('.CodeMirror');
    if (c5 && c5.CodeMirror) { c5.CodeMirror.setValue(code); return 'cm5.setValue'; }
  } catch (e) {}
  try {
    let ta = null;
    try { ta = document.querySelector(__SEL__); } catch (e) { ta = null; }
    if (!ta) ta = document.querySelector('textarea');
    if (ta && ta.tagName === 'TEXTAREA') {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, code);
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
      return 'textarea.setter';
    }
  } catch (e) {}
  return '';
})()
"""


def _ext_read_text(ctx: ActionContext, selector: str) -> str:
    """读回扩展所在标签页里编辑器的内容；失败返回空串。"""
    js = _EXT_READ_JS.replace("__SEL__", json.dumps(selector or "", ensure_ascii=False))
    try:
        return str(_ext_call(ctx, "eval", {"script": js}) or "")
    except Exception:
        return ""


def _ext_write_via_api(ctx: ActionContext, selector: str, text: str) -> str:
    """用框架内部 API 直接写（返回用到的通道名，失败返回空串）。"""
    js = (_EXT_WRITE_API_JS
          .replace("__CODE__", json.dumps(str(text), ensure_ascii=False))
          .replace("__SEL__", json.dumps(selector or "", ensure_ascii=False)))
    try:
        return str(_ext_call(ctx, "eval", {"script": js}) or "")
    except Exception:
        return ""


def _browser_exe() -> str:
    for path in (r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                 r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
        if Path(path).is_file():
            return path
    return ""


def _pid_of(keyword: str) -> int:
    """按关键词模糊找进程 PID（进程名或窗口标题命中都算）。找不到返回 0。"""
    needle = str(keyword).strip().lower()
    if not needle:
        return 0
    try:
        proc = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                              timeout=30,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for row in csv.reader(io.StringIO(_decode(proc.stdout))):
            if len(row) >= 2 and needle in row[0].lower():
                try:
                    return int(row[1])
                except ValueError:
                    continue
    except Exception:
        pass
    try:                                       # 进程名对不上就靠窗口标题找
        for win in windows.list_windows():
            if needle in win.title.lower() and win.pid:
                return int(win.pid)
    except Exception:
        pass
    return 0


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


def _page_state(page: Any) -> str:
    """动作后附一行页面状态。

    这是"跳过全屏感知"的补偿：浏览器动作不再跑 UIA+OCR（省 0.45~1.28 秒），
    但模型仍需要知道自己现在在哪一页——两次 CDP 调用只要几毫秒。
    """
    try:
        return f"\n当前页面：{page.url}（{page.title()}）"
    except Exception:
        return ""


def _ext(ctx: ActionContext, wait: float = 5.0):
    """浏览器扩展在线就返回桥，否则 None（调用方退回内置浏览器）。

    扩展跑在用户**自己开着的**浏览器里，能直接操作已经打开的标签页——
    不用重启、不用复制配置、不用重新登录。没装扩展时一切照旧走 Playwright。

    三条规矩：

    1. **已经用 Playwright 连上浏览器了（browser_debug / browser_open），就以它为准。**
       否则会出现"用 browser_open 开了 A，后面的动作却打到扩展所在的 B"这种错乱——
       两个浏览器各干一半，怎么都调不通。
    2. 没连上时**等一等**再判定。扩展的 service worker 空闲会被浏览器回收，
       内容脚本每 4 秒戳一下把它叫醒，所以"正在重连"是常态——
       这一两秒不该被当成"没装扩展"，否则会莫名其妙又弹一个新浏览器。
       实测（2026-10-16 的 PTA 运行）``browser_status`` 就因为只等 1 秒而放弃过一次
       （日志里 1001 ms 后报错）。扩展在线时这个等待是立即返回的，给到 5 秒几乎没成本。
    3. 真正会另开浏览器的地方（browser_debug）把 ``wait`` 放大到 8 秒，
       多给扩展几秒，比多开一整个浏览器划算得多。
    """
    if ctx.browser.get("page") is not None:
        return None
    try:
        b = extbridge.bridge()
    except Exception:
        return None
    if b.connected:
        return b
    return b if extbridge.wait_ready(wait) else None


def _frame_text_script(selector: str, depth: int) -> str:
    """生成「穿透同源 iframe 收集可见文本」的页面脚本。

    抽成公开的一小段是因为要保证扩展侧（background.js 的 text 分支）和 Python 侧
    语义一致：超星/学习通、教务系统这类站点把正文放在二级 iframe 里，
    只读顶层文档拿到的永远是外层导航栏（"章节详情…1视频 2章节测验"），
    模型只能靠 browser_eval 一次次猜结构 —— 实测这样能白烧 20+ 步。

    每层 frame 的正文前会标出该 frame 的 URL，调用方一次就能看清正文在哪。
    """
    sel = json.dumps(selector, ensure_ascii=False)
    return (
        "(() => {"
        f"const sel = {sel}; const maxDepth = {int(depth)};"
        "const clean = (s) => (s || '').replace(/\\n{3,}/g, '\\n\\n').trim();"
        "const chunks = [];"
        "const walk = (doc, label, d) => {"
        "  let text = '';"
        "  try { const el = doc.querySelector(sel) || doc.body;"
        "        text = clean(el ? (el.innerText || '') : ''); } catch (_) { text = ''; }"
        "  if (text) chunks.push(label ? (label + '\\n' + text) : text);"
        "  if (d >= maxDepth) return;"
        "  let frames = [];"
        "  try { frames = Array.from(doc.querySelectorAll('iframe, frame')); }"
        "  catch (_) { frames = []; }"
        "  frames.forEach((f, i) => {"
        "    let fd = null;"
        "    try { fd = f.contentDocument; } catch (_) { fd = null; }"
        "    if (!fd) return;"                       # 跨域拿不到就跳过，不报错
        "    let url = '';"
        "    try { url = (f.contentWindow && f.contentWindow.location.href) || f.src || ''; }"
        "    catch (_) { url = f.src || ''; }"
        "    walk(fd, '--- frame#' + i + ' ' + url + ' ---', d + 1);"
        "  });"
        "};"
        "walk(document, '', 0);"
        "return chunks.join('\\n\\n');"
        "})()"
    )


# 这些命令天生不吃 tabId：tabs 列清单，detach_all 拆所有，activate/close_tab 自己带。
_NO_TAB_OPS = frozenset({"tabs", "detach_all"})


def _ext_call(ctx: ActionContext, op: str, params: dict[str, Any] | None = None,
              render: Any = None, timeout: float | None = None) -> Any:
    """优先走扩展执行；扩展没在线返回 ``None``，让调用方走内置浏览器。

    注意区分两种情况：**没连上**就静默退回 Playwright；**连上了但执行失败**
    直接把错误抛出去。否则一次"元素没找到"会退化成又开一个浏览器，越弄越乱。

    **每条命令都带上钉住的工作标签页**（``ctx.browser["ext_tab"]``）。
    不给的话，扩展那边是 ``pickTab()``：没有 tabId 就用"当前前台窗口的活动标签页"——
    也就是**用户此刻正在看的那一页**。用户随手切个标签、开个新网页，
    agent 的 eval/type/click/截图就全打到他的页面上去了（还会答非所问地失败）。
    """
    b = _ext(ctx)
    if b is None:
        return None
    payload = dict(params or {})
    pinned = ctx.browser.get("ext_tab")
    if pinned and "tabId" not in payload and op not in _NO_TAB_OPS:
        payload["tabId"] = pinned
    data = b.call(op, payload, timeout=timeout or extbridge.DEFAULT_TIMEOUT)
    return render(data) if render else data


# 在 OJ 页面上**自己**找到"答题用的那个代码编辑器"。
# PTA 编程题页面上有三个 CodeMirror：前两个是只读的"输入样例/输出样例"，
# 第三个才是答题框，而且 class 带哈希（codeEditor_WDyZc），没法写死选择器。
# 让模型自己用 browser_eval 一层层翻 DOM 找，实测一道题要白花 3~5 步。
_FIND_EDITOR_JS = r"""
(() => {
  const RO = "[class*='readOnly'], [class*='readonly'], [aria-readonly='true'], [readonly]";
  const cand = [...document.querySelectorAll('.cm-content, .monaco-editor textarea, textarea')];
  const usable = cand.filter(el => {
    const r = el.getBoundingClientRect();
    if (r.width < 60 || r.height < 24) return false;
    if (el.closest(RO)) return false;                 // 只读样例块，写进去没反应
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  });
  if (!usable.length) return {found: false};
  const score = el => {
    const r = el.getBoundingClientRect();
    const box = el.closest("[class*='AnswerForm'], [class*='answerInput'], [class*='CodingProblem']");
    return (box ? 1e7 : 0) + r.width * r.height;      // 答题区优先，其次挑最大的
  };
  usable.sort((a, b) => score(b) - score(a));
  const el = usable[0];
  el.id = '__ap_editor';
  const r = el.getBoundingClientRect();
  return {found: true, tag: el.tagName, cls: String(el.className).slice(0, 40),
          w: Math.round(r.width), h: Math.round(r.height)};
})()
"""

# 写完立刻读回编辑器。这一步替掉模型自己那次"再求值确认"，省一整步。
_READBACK_JS = r"""
(() => {
  const el = document.querySelector(__SEL__);
  if (!el) return {found: false};
  const t = (el.innerText || el.value || '');
  const lines = t.split('\n');
  return {found: true, lines: lines.length, chars: t.length,
          head: (lines[0] || '').slice(0, 60)};
})()
"""


def _find_editor_selector(ctx: ActionContext) -> str:
    """返回页面上答题编辑器的选择器；找不到返回 ``""``。"""
    try:
        out = _ext_call(ctx, "eval", {"script": _FIND_EDITOR_JS})
    except Exception:
        return ""
    return "#__ap_editor" if isinstance(out, dict) and out.get("found") else ""


def _editor_readback(ctx: ActionContext, selector: str) -> str:
    """写完读回确认（行数/字符数/首行），失败就返回空串不添乱。"""
    if not selector or not selector[0] in "#.[":
        return ""
    js = _READBACK_JS.replace("__SEL__", json.dumps(selector))
    try:
        out = _ext_call(ctx, "eval", {"script": js})
    except Exception:
        return ""
    if not isinstance(out, dict) or not out.get("found"):
        return ""
    return (f"\n读回确认：{out.get('lines')} 行 / {out.get('chars')} 字符，"
            f"首行 {str(out.get('head'))[:60]!r}")


def _clip(url: str, n: int = 90) -> str:
    text = str(url or "")
    return text if len(text) <= n else text[:n] + "…"


def _same_site(a: str, b: str) -> bool:
    """两个 URL 是不是同一个站点（只比主机名，端口/协议不同也算同站）。"""
    from urllib.parse import urlsplit

    try:
        ha = (urlsplit(str(a)).hostname or "").lower()
        hb = (urlsplit(str(b)).hostname or "").lower()
    except Exception:
        return False
    return bool(ha) and ha == hb


# CDP 截图（Page.captureScreenshot）在**浏览器窗口不在前台、标签页被遮挡/最小化**时
# 会一直挂着不返回——Chromium 不给被遮挡的窗口合成帧。日志实测：同一个调用要么
# 0.07 秒返回，要么挂死到超时，没有中间值（2026-10-16 12:40/13:24/20:07/20:08 全中）。
# 所以别在这里干等：超时就直说，并把"换条路"写清楚。
# CDP 截图（Page.captureScreenshot）会整条管线卡死：实测 2026-10-16 20:07 之后，
# 这个浏览器里**所有标签页、连 about:blank 都截不出来**，而 Runtime.evaluate 一直
# 是好的（2~3 毫秒）。当时怀疑的"窗口不在前台"已被实验否定（提到前台照样挂）。
_SCREENSHOT_HINT = (
    "CDP 截图拿不到（这条管线可能已经卡死——实测连 about:blank 都截不出来）。"
    "读内容改用 browser_get_text / browser_eval（几十毫秒）；"
    "要看画面就靠浏览器窗口截图（本动作会自动降级到它），或用 shot(region=...)。"
    "别反复重试截图，那只会一次次等超时。")


def _blank_page(url: str) -> bool:
    """空白页/新标签页——这种页面顶掉了也不心疼。"""
    text = str(url or "").strip().lower()
    return (not text or text.startswith("about:") or text.startswith("edge://newtab")
            or text.startswith("chrome://newtab") or text.startswith("edge://startpage"))


# 断路器：CDP 截图确认挂了就 5 分钟内不再白等它；哪次成功立刻恢复。
_shot_dead_until = 0.0


def _shot_dead() -> bool:
    return time.time() < _shot_dead_until


def _mark_shot(ok: bool, cooldown: float = 300.0) -> None:
    global _shot_dead_until
    _shot_dead_until = 0.0 if ok else time.time() + cooldown


def _screen_shot(ctx: ActionContext, info: dict[str, Any] | None = None) -> Path | None:
    """不碰 CDP 的截图：截浏览器窗口，``info`` 给元素矩形时按它裁出来。

    元素矩形是**网页视口坐标**，加上窗口客户区原点就是屏幕坐标（再乘 dpr 换成物理像素）。
    这正是 CDP 截图管线卡死时的备胎——实测能正确读出超星那些"公式是图片"的题目。
    代价：要求浏览器窗口可见，必要时会把它切到前台。
    """
    win = next((c for c in windows.list_windows()
                if "msedge" in (c.process or "").lower()
                or "chrome" in (c.process or "").lower()), None)
    if win is None:
        return None
    if not win.foreground:
        windows.focus_window(win.hwnd)          # 否则截到的是挡在前面的窗口
        time.sleep(0.35)

    winapi.enable_dpi_awareness()
    rect = winapi.get_client_rect(win.hwnd)
    if info is not None:
        dpr = float(info.get("dpr") or 1.0)
        x, y = float(info.get("x") or 0.0), float(info.get("y") or 0.0)
        wd, ht = float(info.get("width") or 0.0), float(info.get("height") or 0.0)
        if wd < 4 or ht < 4:
            return None
        ox, oy = winapi.client_origin(win.hwnd)
        left, top = max(rect.left, int(ox + x * dpr)), max(rect.top, int(oy + y * dpr))
        right = min(rect.right, left + max(4, int(wd * dpr)))
        bottom = min(rect.bottom, top + max(4, int(ht * dpr)))
        if right - left < 4 or bottom - top < 4:
            return None
        rect = winapi.Rect(left, top, right, bottom)
    if rect.width < 8 or rect.height < 8:
        return None

    target = (ctx.workspace / "var" / "shots"
              / f"screen-{datetime.now().strftime('%Y%m%d-%H%M%S')}.png")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(screen.capture(rect).to_bytes("png"))
    return target


_FAST_READER: Any = None
_FAST_READER_WS: str = ""


def _fast_reader(workspace: Any) -> Any:
    """进程内复用一个 FastReader：它的全部价值就是那把**常驻的 OCR 进程**。

    每次新建等于每次都冷启动（实测 0.8 秒），比不用它还慢；所以按 workspace
    缓存一个实例，让第二次以后的读图只剩引擎时间（小图 6~8 毫秒）。
    """
    global _FAST_READER, _FAST_READER_WS
    from . import fastread

    key = str(workspace)
    if _FAST_READER is None or _FAST_READER_WS != key:
        if _FAST_READER is not None:
            try:
                _FAST_READER.close()
            except Exception:                        # noqa: BLE001 - 关不掉就算了
                pass
        _FAST_READER = fastread.FastReader(workspace=workspace)
        _FAST_READER_WS = key
    return _FAST_READER


def _fast_read(ctx: ActionContext, target: Path, prompt: str = "",
               mode: str = "auto", max_chars: int = 2000) -> tuple[str, str]:
    """返回 ``(给模型看的标签行, 文本)``。OCR 判为乱码时内部会自动升级到视觉模型。"""
    res = _fast_reader(ctx.workspace).read(target, prompt=prompt, mode=mode)
    label = ("系统 OCR（常驻进程，快）" if res["source"] == "ocr"
             else "视觉模型（准）")
    note = res.get("note") or ""
    return (f"{label} {float(res.get('ms') or 0):.0f}ms{note}",
            str(res.get("text") or "")[: int(max_chars)])


def _pick_image_selector(ctx: ActionContext, selector: str, index: int) -> tuple[str, str]:
    """挑出第 ``index`` 张**可用**的图，返回 (选择器, 提示)。

    坑：`#question… img` 里常常夹着隐藏的占位图（0×0 或透明），
    按 index 硬取会拿到它，视觉模型只能说"纯白空白图"——然后模型就以为读图坏了，
    跑去写脚本下载图片。实测 2026-10-16 20:39 那轮为这件事白烧了十几步。
    """
    js = r"""
    (() => {
      const sel = __SEL__, want = __IDX__;
      const usable = [];
      let skipped = 0;
      for (const el of document.querySelectorAll(sel)) {
        const r = el.getBoundingClientRect();
        const w = el.naturalWidth || 0, h = el.naturalHeight || 0;
        const st = getComputedStyle(el);
        if (w < 2 || h < 2 || r.width < 4 || r.height < 4
            || st.visibility === 'hidden' || st.display === 'none' || st.opacity === '0') {
          skipped++;
          continue;
        }
        usable.push(el);
      }
      if (!usable.length) return {found: false, total: skipped};
      const el = usable[Math.min(want, usable.length - 1)];
      el.id = '__ap_img';
      return {found: true, total: usable.length, skipped: skipped,
              at: Math.min(want, usable.length - 1)};
    })()
    """.replace("__SEL__", json.dumps(str(selector))).replace("__IDX__", str(int(index)))
    try:
        out = _ext_call(ctx, "eval", {"script": js})
    except Exception:
        return str(selector), ""
    if not isinstance(out, dict) or not out.get("found"):
        return str(selector), ""
    note = ""
    if out.get("skipped"):
        note = f"\n（已跳过 {out['skipped']} 张隐藏占位图，命中第 {out['at']} 张可见图）"
    return "#__ap_img", note


def _ext_page_state(data: dict[str, Any]) -> str:
    return f"\n当前页面：{data.get('url', '')}（{data.get('title', '')}）"


def _browser_call(ctx: ActionContext, fn: Any, what: str, state: bool = True) -> Any:
    """执行一次浏览器操作，并把"中途浏览器被关掉"翻译成可读错误。

    顺便把失效句柄清掉，避免后续每一步都重复撞同一个墙。
    """
    page = _page(ctx)
    try:
        out = fn(page)
        if state and isinstance(out, str):
            out += _page_state(page)
        return out
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
    looks_like_selector = bool(
        re.match(r"^[.#\[]|^[a-z]+[>\[.:#\s]", text)
        # 带空格的后代选择器也算：div[class*="codeEditor"] .cm-content
        or (text.strip() and re.match(r"^[A-Za-z0-9_\-.#\[\]=\"'>*+~(),:\s]+$", text)))
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
