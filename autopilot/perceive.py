# -*- coding: utf-8 -*-
"""感知层：把"电脑当前的状态"压缩成一段 LLM 和人都能读的文本。

核心思想是 **文本优先、视觉可选**：

* 大多数桌面应用（记事本、Office、资源管理器、设置、各种 Win32/WinUI 程序）
  通过 UI Automation 会暴露一棵带名称、类型、坐标和可用操作的**元素树**。
  相比喂截图给模型，这信息密度高得多，而且点击目标明确、可验证。
* 截图交给 Windows 自带 OCR 转成文字，覆盖 UIA 拿不到的自绘界面
  （游戏、Electron 应用、图片里的字）。
* 每个可交互元素分配一个短 ID（``E1`` ``E2`` …），
  模型只需要说"点 E7"，不用做像素级推理——这是整套系统可靠性的关键。

如果模型有视觉能力，也可以把 ``Snapshot.image`` 一起发过去，
两路信息互为补充。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import psbridge, screen, winapi, windows

# 这些 UIA 模式意味着元素"能被操作"，值得暴露给模型
ACTIONABLE_PATTERNS = {
    "Invoke", "Value", "Toggle", "SelectionItem",
    "ExpandCollapse", "Scroll", "RangeValue",
}

# 纯装饰性/结构性控件，出现在元素列表里只会干扰模型
NOISE_TYPES = {"Image", "Separator", "Thumb", "Group", "Custom", "TitleBar"}

_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff\uac00-\ud7af"
_CJK_SPACE = re.compile(rf"(?<=[{_CJK}])\s+(?=[{_CJK}])")


def normalize_ocr_text(text: str) -> str:
    """Windows OCR 会在中文字之间插空格，去掉它们可读性更好。"""
    if not text:
        return ""
    out = _CJK_SPACE.sub("", text)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out.strip()


@dataclass
class Element:
    """UI Automation 里的一个元素。"""

    index: int
    name: str
    control_type: str
    class_name: str
    rect: winapi.Rect
    patterns: list[str] = field(default_factory=list)
    enabled: bool = True
    value: str | None = None
    depth: int = 0
    automation_id: str = ""

    @property
    def id(self) -> str:
        return f"E{self.index}"

    @property
    def center(self) -> tuple[int, int]:
        return self.rect.center

    @property
    def actionable(self) -> bool:
        return bool(ACTIONABLE_PATTERNS & set(self.patterns))

    @property
    def label(self) -> str:
        """给模型看的一行描述。"""
        name = (self.name or "").replace("\n", " ").strip()
        if len(name) > 60:
            name = name[:57] + "..."
        bits = [self.id, f"{self.control_type}"]
        if name:
            bits.append(f'"{name}"')
        else:
            bits.append("(无名称)")
        bits.append(f"@({self.rect.left},{self.rect.top}) {self.rect.width}x{self.rect.height}")
        if self.patterns:
            bits.append("[" + ",".join(self.patterns) + "]")
        if not self.enabled:
            bits.append("(已禁用)")
        if self.value:
            val = str(self.value).replace("\n", " ")[:60]
            bits.append(f'值="{val}"')
        return " ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "type": self.control_type,
            "rect": self.rect.as_dict(), "patterns": list(self.patterns),
            "enabled": self.enabled, "value": self.value,
        }


@dataclass
class OcrLine:
    text: str
    rect: winapi.Rect

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "rect": self.rect.as_dict()}


@dataclass
class Snapshot:
    """某一时刻的完整"电脑状态"。"""

    ts: float
    screen_size: tuple[int, int]
    cursor: tuple[int, int]
    scale: float
    foreground: windows.WindowInfo | None
    window_list: list[windows.WindowInfo]
    elements: list[Element]
    focused: dict[str, Any] | None = None
    ocr_text: str = ""
    ocr_lines: list[OcrLine] = field(default_factory=list)
    image: screen.Grab | None = None
    image_path: Path | None = None
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0

    # -- 查询 ------------------------------------------------------------
    def element(self, ref: str | int) -> Element | None:
        """按 ``E7`` / ``7`` / 名称 查找元素。"""
        text = str(ref).strip()
        if text.upper().startswith("E") and text[1:].isdigit():
            idx = int(text[1:])
        elif text.isdigit():
            idx = int(text)
        else:
            matches = self.find(text)
            return matches[0] if matches else None
        for el in self.elements:
            if el.index == idx:
                return el
        return None

    def find(self, text: str, control_type: str = "", actionable_only: bool = False
             ) -> list[Element]:
        """按名称/值模糊匹配元素。"""
        needle = str(text).strip().lower()
        out: list[Element] = []
        for el in self.elements:
            if actionable_only and not el.actionable:
                continue
            if control_type and control_type.lower() != el.control_type.lower():
                continue
            haystack = f"{el.name}\n{el.value or ''}".lower()
            if needle in haystack:
                out.append(el)
        return out

    def at(self, x: int, y: int) -> list[Element]:
        """哪些元素覆盖了这个点（最深的排最前）。"""
        hits = [el for el in self.elements
                if el.rect.left <= x < el.rect.right and el.rect.top <= y < el.rect.bottom]
        hits.sort(key=lambda e: e.rect.width * e.rect.height)
        return hits

    # -- 渲染 ------------------------------------------------------------
    def render(self, max_windows: int = 12, max_elements: int = 80,
               max_ocr_lines: int = 60, detail: str = "normal") -> str:
        """生成给 LLM 的文本描述。"""
        lines: list[str] = []
        stamp = datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S")
        w, h = self.screen_size
        lines.append(f"# 屏幕快照 {stamp}")
        lines.append(f"分辨率 {w}x{h}（缩放 {self.scale:g}x）| 光标 ({self.cursor[0]},{self.cursor[1]})")

        if self.foreground:
            fg = self.foreground
            lines.append(f"前台窗口：{fg.title or '(无标题)'} | 进程 {fg.process or '?'} "
                         f"| 位置 ({fg.rect.left},{fg.rect.top}) 尺寸 {fg.rect.width}x{fg.rect.height}")

        if self.focused:
            f = self.focused
            r = f.get("rect") or [0, 0, 0, 0]
            val = f" 值={f.get('value')!r}" if f.get("value") else ""
            lines.append(f"键盘焦点：{f.get('type')} {f.get('name') or '(无名称)'} "
                         f"@({r[0]},{r[1]}){val}")

        if self.window_list:
            lines.append(f"\n## 打开的窗口（{len(self.window_list)}）")
            for i, win in enumerate(self.window_list[:max_windows], 1):
                lines.append(f"  W{i} {win.one_line()}")
            if len(self.window_list) > max_windows:
                lines.append(f"  … 其余 {len(self.window_list) - max_windows} 个已省略")

        if self.elements:
            lines.append(f"\n## 可交互元素（共 {len(self.elements)}，"
                         f"显示 {min(len(self.elements), max_elements)}）")
            lines.append("  用法：click E7 / type E7 \"文本\" / 需要坐标时用方括号里的 @(x,y)")
            for el in self.elements[:max_elements]:
                lines.append(f"  {el.label}")
            if len(self.elements) > max_elements:
                lines.append(f"  … 其余 {len(self.elements) - max_elements} 个已省略，"
                             f"可用 find_element 精确查找")

        if self.ocr_text and detail != "minimal":
            lines.append("\n## 屏幕文字（OCR）")
            shown = self.ocr_lines[:max_ocr_lines] if self.ocr_lines else []
            if shown:
                for ln in shown:
                    txt = ln.text.replace("\n", " ")[:110]
                    lines.append(f"  ({ln.rect.left},{ln.rect.top}) {txt}")
                if len(self.ocr_lines) > max_ocr_lines:
                    lines.append(f"  … 其余 {len(self.ocr_lines) - max_ocr_lines} 行已省略")
            else:
                lines.append("  " + self.ocr_text[:1500])

        if self.notes:
            lines.append("\n## 备注")
            for note in self.notes:
                lines.append(f"  - {note}")
        return "\n".join(lines)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "time": datetime.fromtimestamp(self.ts).strftime("%Y-%m-%d %H:%M:%S"),
            "screen": {"width": self.screen_size[0], "height": self.screen_size[1],
                       "scale": self.scale},
            "cursor": list(self.cursor),
            "foreground": self.foreground.as_dict() if self.foreground else None,
            "windows": [w.as_dict() for w in self.window_list],
            "focused": self.focused,
            "elements": [e.as_dict() for e in self.elements],
            "ocr_text": self.ocr_text,
            "ocr_lines": [l.as_dict() for l in self.ocr_lines],
            "image_path": str(self.image_path) if self.image_path else None,
            "truncated": self.truncated,
            "notes": self.notes,
            "elapsed_ms": self.elapsed_ms,
        }


# --------------------------------------------------------------------------
# 采集
# --------------------------------------------------------------------------


def _build_elements(raw: Sequence[dict[str, Any]]) -> tuple[list[Element], bool]:
    """把 UIA 原始结果整理成按阅读顺序编号的元素列表。"""
    parsed: list[Element] = []
    for item in raw:
        rect_raw = item.get("rect") or [0, 0, 0, 0]
        rect = winapi.Rect(int(rect_raw[0]), int(rect_raw[1]),
                           int(rect_raw[0]) + int(rect_raw[2]),
                           int(rect_raw[1]) + int(rect_raw[3]))
        if rect.width <= 0 or rect.height <= 0:
            continue
        patterns = [p for p in (item.get("patterns") or [])]
        ctype = str(item.get("type") or "")
        name = str(item.get("name") or "").strip()
        value = item.get("value")
        value = str(value) if value not in (None, "") else None

        informative = bool(name) or bool(ACTIONABLE_PATTERNS & set(patterns))
        if not informative:
            continue
        if ctype in NOISE_TYPES and not (ACTIONABLE_PATTERNS & set(patterns)):
            continue
        # 面积过大又没有名字的容器，多半是背景板
        if not name and not value and rect.width * rect.height > 0.85 * 1920 * 1200:
            continue

        parsed.append(Element(
            index=0, name=name, control_type=ctype,
            class_name=str(item.get("class") or ""), rect=rect,
            patterns=patterns, enabled=bool(item.get("enabled", True)),
            value=value, depth=int(item.get("depth") or 0),
            automation_id=str(item.get("aid") or ""),
        ))

    # 去重：同名同位置只留一个
    seen: set[tuple[str, int, int, int, int]] = set()
    unique: list[Element] = []
    for el in parsed:
        key = (el.name, el.rect.left, el.rect.top, el.rect.width, el.rect.height)
        if key in seen:
            continue
        seen.add(key)
        unique.append(el)

    # 阅读顺序：从上到下、从左到右。但"纯布局容器"要沉到最后，
    # 否则一个占满屏幕的 Pane 会占据 E1，把真正能点的按钮挤到后面。
    screen_area = max(1, 1920 * 1200)
    try:
        screen_area = max(1, winapi.primary_screen_size()[0] *
                          winapi.primary_screen_size()[1])
    except Exception:
        pass

    def sort_key(e: Element) -> tuple:
        background = (not e.actionable and not e.value
                      and e.rect.width * e.rect.height >= 0.25 * screen_area)
        return (background, e.rect.top // 12, e.rect.left, not e.actionable,
                -e.rect.width * e.rect.height)

    unique.sort(key=sort_key)
    for i, el in enumerate(unique, 1):
        el.index = i
    return unique, len(parsed) > len(unique)


def observe(include_uia: bool = True, include_ocr: bool = True,
            save_image: bool = True, shot_dir: str | Path | None = None,
            ocr_region: str = "window", max_elements: int = 400,
            max_depth: int = 12, ocr_lang: str | None = None,
            image_format: str = "png") -> Snapshot:
    """采集一次完整的电脑状态。

    参数：
        include_uia:   是否读取 UI Automation 元素树
        include_ocr:   是否对画面做 OCR
        save_image:    是否把截图落盘（Agent 每步都存，便于事后复盘）
        ocr_region:    ``"window"`` 只识别前台窗口，``"screen"`` 识别整屏
        max_elements:  UIA 最多收集多少元素
    """
    t0 = time.perf_counter()
    winapi.enable_dpi_awareness()
    notes: list[str] = []

    fg = None
    try:
        fg = windows.get_foreground()
    except Exception as exc:
        notes.append(f"获取前台窗口失败：{exc}")

    try:
        win_list = windows.list_windows()
    except Exception as exc:
        win_list = []
        notes.append(f"枚举窗口失败：{exc}")

    elements: list[Element] = []
    focused: dict[str, Any] | None = None
    truncated = False
    if include_uia:
        try:
            res = psbridge.bridge().uia_tree(
                fg.hwnd if fg else 0, max_depth=max_depth, max_nodes=max_elements)
            if res.get("ok"):
                elements, _ = _build_elements(res.get("elements") or [])
                focused = res.get("focused")
                truncated = bool(res.get("truncated"))
                if truncated:
                    notes.append("元素过多已截断，可用 find_element 精确定位")
            else:
                notes.append(f"UIA 失败：{res.get('error')}")
        except Exception as exc:
            notes.append(f"UIA 异常：{exc}")

    image = None
    image_path: Path | None = None
    ocr_text = ""
    ocr_lines: list[OcrLine] = []
    if include_ocr or save_image:
        try:
            image = screen.capture()
        except Exception as exc:
            notes.append(f"截屏失败：{exc}")

    if image is not None and save_image:
        try:
            base = Path(shot_dir) if shot_dir else Path("var/shots")
            base.mkdir(parents=True, exist_ok=True)
            image_path = base / f"shot-{int(time.time() * 1000)}.{image_format}"
            image.save(image_path, quality=75)
        except Exception as exc:
            notes.append(f"保存截图失败：{exc}")

    if image is not None and include_ocr:
        try:
            if ocr_region == "window" and fg and fg.rect.width > 40 and fg.rect.height > 40:
                win_rect = winapi.Rect(max(0, fg.rect.left), max(0, fg.rect.top),
                                       min(image.width, fg.rect.right),
                                       min(image.height, fg.rect.bottom))
                if win_rect.width > 40 and win_rect.height > 40:
                    sub = screen.capture(win_rect)
                else:
                    sub = image
            else:
                sub = image
            # 缩一下再 OCR：更快，且系统 OCR 对过大图反而更差
            sub = sub.scaled(2200, 1400)
            import tempfile
            tmp = Path(tempfile.gettempdir()) / "autopilot-ps" / "ocr-input.png"
            tmp.parent.mkdir(parents=True, exist_ok=True)
            sub.save(tmp)
            res = psbridge.bridge().ocr(tmp, lang=ocr_lang)
            if res.get("ok"):
                ocr_text = normalize_ocr_text(res.get("text") or "")
                for item in (res.get("lines") or []):
                    r = item.get("rect") or [0, 0, 0, 0]
                    ocr_lines.append(OcrLine(
                        normalize_ocr_text(item.get("text") or ""),
                        winapi.Rect(int(r[0]), int(r[1]), int(r[0]) + int(r[2]),
                                    int(r[1]) + int(r[3])),
                    ))
            else:
                notes.append(f"OCR 失败：{res.get('error')}")
        except Exception as exc:
            notes.append(f"OCR 异常：{exc}")

    return Snapshot(
        ts=time.time(),
        screen_size=winapi.primary_screen_size(),
        cursor=winapi.get_cursor_pos(),
        scale=winapi.get_scale(),
        foreground=fg,
        window_list=win_list,
        elements=elements,
        focused=focused,
        ocr_text=ocr_text,
        ocr_lines=ocr_lines,
        image=image,
        image_path=image_path,
        truncated=truncated,
        notes=notes,
        elapsed_ms=round((time.perf_counter() - t0) * 1000, 1),
    )


def quick_context() -> str:
    """一行式环境摘要，放在每次对话的开头。"""
    winapi.enable_dpi_awareness()
    w, h = winapi.primary_screen_size()
    fg = windows.get_foreground()
    fg_txt = f"{fg.title or '(无标题)'} ({fg.process})" if fg else "未知"
    return (f"Windows 桌面 {w}x{h}，缩放 {winapi.get_scale():g}x，"
            f"光标 ({winapi.get_cursor_pos()[0]},{winapi.get_cursor_pos()[1]})，"
            f"当前前台：{fg_txt}")
