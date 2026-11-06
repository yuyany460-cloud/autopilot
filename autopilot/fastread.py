# -*- coding: utf-8 -*-
"""快速读图：常驻 OCR 工作进程 + 视觉模型兜底。

为什么要它——三条路线的实测耗时（1920x1200 的网页截图，本机 2026-11-05）：

    走 psbridge 的 ocr.ps1    0.76~0.87s   （其中引擎只占 0.33s，其余是新起
                                            powershell.exe + WinRT 绑定的固定开销）
    常驻 OCR 进程（本模块）   约 0.10~0.20s（只剩引擎时间）
    视觉模型（vision.read_image）1.0~5.0s   （取决于输出长度，不是图片大小）

但**快不等于对**：Windows OCR 认代码、公式、挤在一起的表格会出错，早期基准里
代码 + 公式图的行命中只有 8%，视觉模型 92%。它有个坑：**每个汉字之间都会插一个
空格**（``使 用 函 数 求 素 数 和``），如果直接按"单字 token 多"判乱码，正常中文
也会被误判成乱码、一路升级到视觉模型。所以打分前先 ``join_cjk`` 把汉字之间的空格
去掉，再看剩下的东西是不是真乱。

所以本模块的策略是「先快后准」而不是「用 OCR 换掉视觉模型」：

    1. 常驻 OCR 先读（~0.15s）
    2. ``join_cjk`` 去汉字间空格，再用 ``garble_score`` 打分：孤立单字多、标点全角化、
       行长短不齐 → 判为乱码
    3. 判为乱码、看着像代码、或用户明确要公式/代码 → 升级到视觉模型
    4. 中文正文干净 → 直接返回，省下 1~5 秒

命令行：

    python -m autopilot.fastread 图片.png              # 自动（先 OCR，必要时升级）
    python -m autopilot.fastread 图片.png --mode ocr   # 只用 OCR（最快，可能错）
    python -m autopilot.fastread 图片.png --mode vision
    python -m autopilot.fastread --watch 目录          # 后台盯着目录，来图就出 .txt
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from . import vision
from .psbridge import PS_DIR, find_powershell

# 出现这些符号就说明有公式/数学，OCR 基本读不对，直接给视觉模型
_MATH_HINTS = set("√∑∫≠≤≥∞±×÷²³√πθΔ∂≈∝∈∉⇒⇔")

# 汉字 + 中文标点：这两个集合里的字符之间被 OCR 插的空格要吃掉
_CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_CJK_PUNCT = "，。；：、！？（）【】「」『』《》〈〉…—·“”‘’"
_JOIN_CJK = re.compile(rf"(?<=[{_CJK}{_CJK_PUNCT}])\s+(?=[{_CJK}{_CJK_PUNCT}])")

# 代码味儿：OCR 对标识符基本是靠猜（long long → tong ton、PrimeSum → prxmesum）
_CODE_HINTS = ("#include", "printf", "scanf", "int main", "console.log", "def ", "->", "==")
_CODE_SYMBOLS = set("{}()[]<>;=_&*$#@!")

DEFAULT_LANG = "zh-Hans-CN"


def join_cjk(text: str) -> str:
    """吃掉 OCR 在汉字之间插的空格：``使 用 函 数`` → ``使用函数``。"""
    return _JOIN_CJK.sub("", text)


def looks_like_code(text: str) -> bool:
    """像代码/公式就不该信 OCR：标识符和符号它读不准。"""
    if any(hint in text for hint in _CODE_HINTS):
        return True
    symbols = sum(1 for ch in text if ch in _CODE_SYMBOLS)
    return symbols >= 6


def cjk_ratio(text: str) -> float:
    """汉字占"汉字 + 字母数字"的比例；纯中文正文接近 1，代码/英文表格很低。"""
    han = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    alnum = sum(1 for ch in text if ch.isascii() and ch.isalnum())
    total = han + alnum
    return han / total if total else 0.0


def garble_score(text: str) -> float:
    """0 = 干净，1 = 全乱码。

    Windows OCR 认错字形时会把一个词拆成一串单字（``跸 哩 跸 哩``），
    所以"单字 token 占比"是最灵敏的信号；再加上全角标点和超短行做加权。

    注意先 ``join_cjk``（它会给每个汉字插空格），并且纯标点 token（``|``、``-``、``:``）
    不算"单字词"——表格里的竖线不是乱码。
    """
    raw = text.replace("\n", " ").split(" ")
    tokens = [t for t in raw
              if any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in t)]
    if not tokens:
        return 1.0
    singles = sum(1 for t in tokens if len(t) == 1 and not t.isdigit())
    ratio = singles / len(tokens)

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    short = sum(1 for ln in lines if len(ln) <= 3)
    short_ratio = short / len(lines) if lines else 1.0

    weird = sum(text.count(c) for c in "．，；：（）〈〉｜") / max(1, len(text))

    return min(1.0, 0.62 * ratio + 0.28 * short_ratio + 2.4 * weird)


class OCRDaemon:
    """一个常驻的 powershell.exe，里面跑 Windows.Media.Ocr。

    生命期与进程同寿，第一次 ``read`` 时启动并预热引擎；失败就重启一次，
    再失败就抛 ``RuntimeError``（调用方可以退回 psbridge 的老路径）。
    """

    def __init__(self, lang: str = DEFAULT_LANG, timeout: float = 12.0,
                 exe: str | None = None) -> None:
        self.lang = lang
        self.timeout = float(timeout)
        self._exe = exe
        self._proc: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    # -- 生命周期 --------------------------------------------------------
    @property
    def exe(self) -> str:
        if not self._exe:
            self._exe = find_powershell()
        return self._exe

    def _start(self) -> subprocess.Popen[str]:
        script = PS_DIR / "ocr_daemon.ps1"
        if not script.is_file():
            raise RuntimeError(f"缺少脚本：{script}")
        cmd = [self.exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-File", str(script)]
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        ready = self._readline(proc)
        if not (ready and ready.get("ready")):
            try:
                proc.kill()
            except OSError:
                pass
            raise RuntimeError(f"OCR 常驻进程没有就绪：{ready}")
        return proc

    @staticmethod
    def _readline(proc: subprocess.Popen[str]) -> dict[str, Any] | None:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line:
            return None
        try:
            return json.loads(line)
        except ValueError:
            return None

    def _ensure(self) -> subprocess.Popen[str]:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self._proc = self._start()
        return self._proc

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass

    # -- 读图 ------------------------------------------------------------
    def read(self, path: str | Path, lang: str | None = None) -> dict[str, Any]:
        image = str(Path(path).resolve())
        payload = json.dumps({"path": image, "lang": lang or self.lang},
                             ensure_ascii=False)
        started = time.perf_counter()
        with self._lock:
            for attempt in (1, 2):
                try:
                    proc = self._ensure()
                    if proc.stdin is None:
                        raise RuntimeError("stdin 不可用")
                    proc.stdin.write(payload + "\n")
                    proc.stdin.flush()
                    answer = self._readline(proc)
                    if answer is None:
                        raise RuntimeError("OCR 进程没有响应（可能已退出）")
                    answer["wall_ms"] = round((time.perf_counter() - started) * 1000, 1)
                    return answer
                except (OSError, RuntimeError):
                    self.close()
                    if attempt == 2:
                        raise
        raise RuntimeError("unreachable")            # pragma: no cover


def _wants_precision(prompt: str) -> bool:
    """用户（或提示词）是不是在要公式/代码/表格这类 OCR 读不好的东西。"""
    if not prompt:
        return False
    keys = ("公式", "代码", "数学", "matrix", "矩阵", "表格", "根号", "分数", "下标")
    return any(k in prompt for k in keys) or any(ch in _MATH_HINTS for ch in prompt)


class FastReader:
    """先快后准的读图入口。"""

    def __init__(self, workspace: str | Path | None = None,
                 lang: str = DEFAULT_LANG) -> None:
        self.workspace = workspace
        self.daemon = OCRDaemon(lang=lang)
        self.stats = {"ocr": 0, "vision": 0, "escalated": 0}

    def close(self) -> None:
        self.daemon.close()

    def read(self, path: str | Path, prompt: str = "", mode: str = "auto",
             max_tokens: int = 700, threshold: float = 0.18,
             min_cjk: float = 0.45) -> dict[str, Any]:
        """读一张图，返回 ``{text, source, ms, cached?, note?}``。

        ``source`` 是 ``ocr`` 或 ``vision``；``mode="auto"`` 才会升级。

        阈值定在 0.18 而不是更松：OCR 读中文正文几乎不出错（实测 score 0.1），
        但一旦混进代码、公式、被表格切碎的单元格，它就会改字（``too low`` →
        ``too tow``、``6-4`` → ``6 一 4``），而这种错误靠分数抓不住。宁可多升
        一次视觉模型，也别把改过的题面喂给模型。
        """
        image = Path(path)
        if not image.is_file():
            raise vision.VisionUnavailable(f"图片不存在：{image}")

        ocr: dict[str, Any] = {}
        if mode in ("auto", "ocr"):
            try:
                ocr = self.daemon.read(image)
            except (RuntimeError, OSError) as exc:
                if mode == "ocr":
                    raise
                ocr = {"ok": False, "error": f"常驻 OCR 不可用（{exc}）"}

            text = (ocr.get("text") or "").strip() if ocr.get("ok") else ""
            text = join_cjk(text)
            score = garble_score(text) if text else 1.0
            code = looks_like_code(text)
            zh = cjk_ratio(text)
            good = bool(text) and score < threshold and zh >= min_cjk and not code \
                and not _wants_precision(prompt)
            if mode == "ocr" or good:
                self.stats["ocr"] += 1
                if good or mode == "ocr":
                    note = ""
                elif code:
                    note = "（看着像代码，OCR 的标识符不可信）"
                elif zh < min_cjk:
                    note = f"（汉字只占 {zh:.0%}，多半是代码/英文表格）"
                else:
                    note = f"（OCR 质量差 score={score:.2f}）"
                return {"text": text, "source": "ocr", "ms": ocr.get("wall_ms", 0.0),
                        "engine": ocr.get("engine", ""), "score": round(score, 3),
                        "note": note}

        res = vision.read_image(image, prompt=prompt, workspace=self.workspace,
                               max_tokens=max_tokens)
        self.stats["vision"] += 1
        if ocr.get("ok"):
            self.stats["escalated"] += 1
        reason = ""
        if ocr and not ocr.get("ok"):
            reason = f"（OCR 失败：{ocr.get('error')}）"
        elif ocr:
            score = garble_score(join_cjk(ocr.get("text") or ""))
            why = "像代码" if looks_like_code(join_cjk(ocr.get("text") or "")) else "判为乱码"
            reason = f"（OCR {why} score={score:.2f}）"

        return {"text": res["text"], "source": "vision", "ms": res["ms"],
                "cached": res.get("cached", False), "note": reason}


# ----------------------------------------------------------------------
# 命令行
# ----------------------------------------------------------------------

def _watch(reader: FastReader, folder: Path, out_dir: Path) -> None:
    """盯着一个目录：出现新图就读出来，写成同名 .txt。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    print(f"盯着 {folder} → 结果写到 {out_dir}（Ctrl+C 退出）", flush=True)
    while True:
        try:
            for path in sorted(folder.glob("*")):
                if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp"):
                    continue
                key = f"{path.name}:{path.stat().st_mtime_ns}"
                if key in seen:
                    continue
                seen.add(key)
                try:
                    res = reader.read(path)
                except Exception as exc:                 # noqa: BLE001 - 后台循环不能死
                    print(f"[{path.name}] 失败：{exc}", flush=True)
                    continue
                target = out_dir / f"{path.stem}.txt"
                target.write_text(res["text"], encoding="utf-8")
                print(f"[{path.name}] {res['source']} {res['ms']:.0f}ms → {target.name}",
                      flush=True)
            time.sleep(0.4)
        except KeyboardInterrupt:
            print("退出", flush=True)
            return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopilot.fastread", description="快速读图")
    parser.add_argument("image", nargs="?", help="图片路径")
    parser.add_argument("--mode", choices=["auto", "ocr", "vision"], default="auto")
    parser.add_argument("--prompt", default="")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--watch", metavar="DIR", help="后台盯目录")
    parser.add_argument("--out", metavar="DIR", default="", help="--watch 的结果目录")
    args = parser.parse_args(argv)

    reader = FastReader()
    try:
        if args.watch:
            folder = Path(args.watch)
            out = Path(args.out) if args.out else folder / "ocr_out"
            _watch(reader, folder, out)
            return 0
        if not args.image:
            parser.error("需要图片路径或 --watch 目录")
        res = reader.read(args.image, prompt=args.prompt, mode=args.mode)
        if args.json:
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"{res['source']} {res['ms']:.0f}ms {res.get('note','')}".strip())
            print(res["text"])
        return 0
    finally:
        reader.close()


if __name__ == "__main__":                              # pragma: no cover
    sys.exit(main())
