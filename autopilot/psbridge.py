# -*- coding: utf-8 -*-
"""PowerShell 5.1 桥接层：为 Python 提供 UI Automation 与系统 OCR 能力。

为什么走 PowerShell 而不是 pip 包？

* **Windows.Media.Ocr**：Windows 10/11 自带，中文识别开箱可用，
  且不需要下载模型、不需要 tesseract 二进制。
* **UIAutomationClient**：.NET 自带完整 UI Automation 客户端，
  比 ``comtypes`` 手撸 COM 接口稳得多。

坑与对策：

1. 必须调用 **Windows PowerShell 5.1**（``powershell.exe``），
   不能是 PowerShell 7（``pwsh``）——后者对 WinRT 的投影支持不完整。
2. PS 5.1 把无 BOM 的 ``.ps1`` 当 ANSI 读，所以 ``ps/*.ps1`` 全部保持 ASCII，
   中文数据一律走 UTF-8 JSON 文件传递。
3. 结果写文件而不是读 stdout，彻底绕开控制台代码页问题。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from . import winapi

PS_DIR = Path(__file__).resolve().parent / "ps"


class BridgeError(RuntimeError):
    """PowerShell 桥接执行失败。"""


def find_powershell() -> str:
    """定位 Windows PowerShell 5.1。"""
    candidates = [
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                     "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        shutil.which("powershell.exe") or "",
        shutil.which("powershell") or "",
    ]
    for path in candidates:
        if path and Path(path).is_file():
            return path
    raise BridgeError("找不到 powershell.exe（Windows PowerShell 5.1），"
                      "UI Automation / OCR 功能不可用")


class PSBridge:
    """执行 ``ps/*.ps1`` 脚本并返回解析后的 JSON。

    线程安全：每次调用使用独立的临时文件，实例可被多个线程共享。
    """

    def __init__(self, timeout: float = 60.0, keep_temp: bool = False,
                 exe: str | None = None) -> None:
        self.timeout = float(timeout)
        self.keep_temp = keep_temp
        self._exe = exe
        self._lock = threading.Lock()
        self._ocr_langs: list[str] | None = None
        self.failures = 0

    # -- 基础设施 --------------------------------------------------------
    @property
    def exe(self) -> str:
        if not self._exe:
            self._exe = find_powershell()
        return self._exe

    def _script_path(self, name: str) -> Path:
        p = PS_DIR / f"{name}.ps1"
        if not p.is_file():
            raise BridgeError(f"缺少脚本：{p}")
        return p

    def _run(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        winapi.enable_dpi_awareness()
        script = self._script_path(name)
        tmpdir = Path(tempfile.gettempdir()) / "autopilot-ps"
        tmpdir.mkdir(parents=True, exist_ok=True)
        token = f"{name}-{os.getpid()}-{uuid.uuid4().hex[:10]}"
        job = tmpdir / f"{token}.json"
        result = tmpdir / f"{token}.json.out.json"
        job.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        cmd = [self.exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script), "-Job", str(job)]
        started = time.perf_counter()
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=False, timeout=self.timeout,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise BridgeError(f"{name} 执行超时（>{self.timeout}s）") from exc
        elapsed = (time.perf_counter() - started) * 1000

        if not result.is_file():
            stderr = (proc.stderr or b"").decode("utf-8", "replace")[:800]
            stdout = (proc.stdout or b"").decode("utf-8", "replace")[:400]
            with self._lock:
                self.failures += 1
            raise BridgeError(
                f"{name} 未产出结果文件（exit={proc.returncode}）\n"
                f"stderr: {stderr}\nstdout: {stdout}")

        raw = json.loads(result.read_text(encoding="utf-8-sig"))
        raw["_elapsed_ms"] = round(elapsed, 1)

        if not self.keep_temp:
            for f in (job, result):
                try:
                    f.unlink()
                except OSError:
                    pass
        if not raw.get("ok", False):
            with self._lock:
                self.failures += 1
        return raw

    # -- UI Automation ---------------------------------------------------
    def uia_tree(self, hwnd: int = 0, max_depth: int = 10, max_nodes: int = 400) -> dict[str, Any]:
        """导出窗口（或整个桌面）的 UI Automation 元素树。"""
        return self._run("uia", {
            "mode": "tree", "hwnd": int(hwnd or 0),
            "maxDepth": int(max_depth), "maxNodes": int(max_nodes),
        })

    def uia_focused(self) -> dict[str, Any]:
        """查询当前拥有键盘焦点的元素（判断输入会落到哪里）。"""
        return self._run("uia", {"mode": "focused"})

    # -- OCR -------------------------------------------------------------
    def ocr(self, image_path: str | Path, lang: str | None = None) -> dict[str, Any]:
        """识别图片文字，返回 ``{"text": ..., "lines": [{"text", "rect"}]}``。"""
        return self._run("ocr", {"path": str(Path(image_path).resolve()),
                                 "lang": lang or ""})

    def ocr_languages(self) -> list[str]:
        """列出系统已安装的 OCR 语言包（结果会缓存）。"""
        if self._ocr_langs is not None:
            return self._ocr_langs
        langs: list[str] = []
        try:
            exe = self.exe
            cmd = [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                   "-Command",
                   "Add-Type -AssemblyName System.Runtime.WindowsRuntime;"
                   "[Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]|Out-Null;"
                   "[Windows.Media.Ocr.OcrEngine]::AvailableRecognizerLanguages|"
                   "ForEach-Object{$_.LanguageTag}"]
            proc = subprocess.run(cmd, capture_output=True, timeout=30,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            langs = [ln.strip() for ln in
                     (proc.stdout or b"").decode("utf-8", "replace").splitlines() if ln.strip()]
        except Exception:
            langs = []
        self._ocr_langs = langs
        return langs

    # -- 通知 ------------------------------------------------------------
    def toast(self, title: str, message: str = "", app_id: str = "") -> dict[str, Any]:
        return self._run("toast", {"title": title, "message": message, "appId": app_id})


_BRIDGE: PSBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def bridge() -> PSBridge:
    """进程级共享的桥接实例。"""
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = PSBridge()
        return _BRIDGE


def self_check() -> dict[str, Any]:
    """桥接子系统自检：UIA + OCR 是否真正可用。"""
    out: dict[str, Any] = {"powershell": None, "uia": None, "ocr": None, "errors": []}
    try:
        b = bridge()
        out["powershell"] = b.exe
    except BridgeError as exc:
        out["errors"].append(str(exc))
        return out

    try:
        from . import screen
        shot = screen.capture()
        small = shot.scaled(1400, 900)
        tmp = Path(tempfile.gettempdir()) / "autopilot-ps" / "selfcheck.png"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        small.save(tmp)
        res = b.ocr(tmp)
        out["ocr"] = {
            "ok": bool(res.get("ok")),
            "engine": res.get("engine"),
            "languages": b.ocr_languages(),
            "lines": len(res.get("lines") or []),
            "ms": res.get("_elapsed_ms"),
            "sample": (res.get("text") or "")[:120],
        }
    except Exception as exc:
        out["errors"].append(f"ocr: {exc}")

    try:
        fg = winapi.get_foreground_window()
        res = b.uia_tree(fg, max_depth=6, max_nodes=120)
        out["uia"] = {
            "ok": bool(res.get("ok")),
            "elements": res.get("count"),
            "ms": res.get("_elapsed_ms"),
            "root": (res.get("root") or {}).get("name"),
        }
    except Exception as exc:
        out["errors"].append(f"uia: {exc}")

    return out
