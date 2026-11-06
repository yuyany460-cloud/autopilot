# -*- coding: utf-8 -*-
"""超星/学习通作业的整卷读取。

为什么单独一个模块：超星把**题干和每个选项**都渲染成独立的 PNG
（``http://p.ananas.chaoxing.com/star3/origin/<hash>.png``），DOM 里只剩
``A.`` ``B.`` 这样的标签，``innerText`` 抓不到任何题目内容——这就是为什么
`browser_get_text` 只拿到一堆空的 A/B/C/D。

以前的读法是"截图 + 视觉识别"，但它有三个硬伤：
1. 依赖 CDP 截图，而那条管线在真机上会整条卡死（连 about:blank 都截不出来）；
2. 屏幕截图会抢焦点、还受 DPI/缩放影响；
3. 每次只能读一块，五张图要来回截五次。

实测这些 PNG 是**公开直链**：不需要 cookie，HTTP 200 返回合法 PNG（6 KB 左右）。
所以正确做法是——页面里只做一件事：**把图片 URL 抓出来**，然后 Python 直接下载原图，
一次把"题干 + 四个选项"五张图交给视觉模型。又快（每题 1~2 秒）、又准（原图而不是屏幕像素）、
而且完全不受截图故障影响。
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from . import vision

#: 超星对 UA 没要求，但带上更稳；Referer 让 CDN 认为是站内引用。
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"),
    "Referer": "https://mooc1-api.chaoxing.com/",
}

#: 判定页是 ``div.mark_item``，作答页是 ``[id^=question]``——两种结构都扫。
#: 返回 JSON 字符串（跨扩展桥比直接回对象稳）。
EXTRACT_JS = r"""(() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const url = img => img.getAttribute('data-original') || img.src || '';
  const imgs = el => [...el.querySelectorAll('img')].map(url).filter(Boolean);
  const out = [];
  const seen = new Set();

  document.querySelectorAll('.mark_item').forEach(item => {
    const box = item.querySelector('.aiAreaContent') || item;
    const stem = box.querySelector('h3.mark_name, .mark_name, .qtContent') || box;
    const opts = [...box.querySelectorAll('ul.mark_letter li, li.workTextWrap')].map(li => ({
      label: norm(li.innerText).slice(0, 20),
      imgs: imgs(li),
    }));
    const id = norm(stem.innerText).slice(0, 12) || ('item' + out.length);
    if (seen.has(id)) return;
    seen.add(id);
    out.push({ page: 'view', id: id, stemText: norm(stem.innerText).slice(0, 120),
               stemImgs: imgs(stem), opts: opts });
  });

  document.querySelectorAll('[id^=question]').forEach(q => {
    const stem = q.querySelector('.Zy_TItle, .qtContent, h3') || q;
    const opts = [...q.querySelectorAll('.answerBg, li')]
      .filter(li => /^[A-D]/.test(norm(li.innerText || '')))
      .slice(0, 8)
      .map(li => ({ label: norm(li.innerText).slice(0, 20), imgs: imgs(li) }));
    out.push({ page: 'dowork', id: q.id, stemText: norm(stem.innerText).slice(0, 120),
               stemImgs: imgs(stem), opts: opts });
  });

  return JSON.stringify({ count: out.length, items: out });
})()"""


def extract(call: Callable[..., Any]) -> list[dict[str, Any]]:
    """跑 ``EXTRACT_JS``，返回题目列表。``call`` 是 ``_ext_call`` 那类函数。"""
    raw = call("eval", {"script": EXTRACT_JS})
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    try:
        data = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"超星页面结构解析失败（拿到 {str(raw)[:80]!r}）") from exc
    items = data.get("items") if isinstance(data, dict) else None
    items = list(items or [])
    # 判定页(work/view)和作答页(work/dowork)的内容在同一个文档里各有一份，
    # 不过滤的话一页 10 题会变成 20 条。按题号去重，优先保留作答页那份。
    best: dict[str, dict[str, Any]] = {}
    for item in items:
        num = _question_number(item)
        key = num or hashlib.sha1(
            ("|".join(item.get("stemImgs") or []) or str(item.get("id"))).encode()).hexdigest()[:10]
        keep = best.get(key)
        if keep is None or (item.get("page") == "dowork" and keep.get("page") != "dowork"):
            best[key] = item
    # 按题号排回来：判定页和作答页混在一起时，顺序会是 1、10、2、3…
    return sorted(best.values(),
                  key=lambda it: (int(_question_number(it) or 10**6), str(it.get("id"))))


def _question_number(item: dict[str, Any]) -> str:
    """从题头文字里取题号（"1. (单选题)" → "1"）。取不到返回空串。"""
    match = re.match(r"\s*(\d+)", str(item.get("stemText") or ""))
    return match.group(1) if match else ""


def has_content(item: dict[str, Any]) -> bool:
    """这题有没有可读的东西（题干图或选项图）。"""
    return bool(item.get("stemImgs") or any(o.get("imgs") for o in item.get("opts") or []))


def as_blocks(item: dict[str, Any]) -> list[tuple[str, str]]:
    """把一题拆成 ``[(标签, 图片URL), ...]``：题干 + 选项A/B/C/D。"""
    blocks: list[tuple[str, str]] = []
    for i, url in enumerate(item.get("stemImgs") or []):
        blocks.append(("题干" if not i else f"题干图{i + 1}", url))
    letters = "ABCDEFGH"
    for i, opt in enumerate(item.get("opts") or []):
        label = "".join(ch for ch in str(opt.get("label") or "") if ch.isalpha())[:1]
        label = label or letters[i] if i < len(letters) else str(i + 1)
        for j, url in enumerate(opt.get("imgs") or []):
            blocks.append((f"选项{label}" if not j else f"选项{label}图{j + 1}", url))
    return blocks


def _suffix(url: str) -> str:
    tail = url.split("?")[0].rsplit(".", 1)[-1].lower()
    return "." + tail if tail in {"png", "jpg", "jpeg", "gif", "webp"} else ".png"


def download(urls: Iterable[str], dest: Path, timeout: float = 12.0) -> dict[str, Path]:
    """下载图片到 ``dest``，返回 ``{url: 本地路径}``；同一张图只下一次。

    直链是 http，但页面是 https——统一升级成 https，避免被当混合内容拦。
    """
    dest.mkdir(parents=True, exist_ok=True)
    saved: dict[str, Path] = {}
    for url in urls:
        if not url or url in saved:
            continue
        https_url = url.replace("http://", "https://", 1)
        target = dest / f"{hashlib.sha1(https_url.encode()).hexdigest()[:16]}{_suffix(https_url)}"
        if not target.is_file():
            req = urllib.request.Request(https_url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            if not data:
                raise RuntimeError(f"图片是空的：{https_url[-40:]}")
            target.write_bytes(data)
        saved[url] = target
    return saved


def _prompt_for(labels: list[str]) -> str:
    """按**实际存在**的标签生成转录格式：填空题没有选项，就不该让它吐 A~D。"""
    body = "\n".join(f"{lab}：<原样>" for lab in labels) or "题干：<原样>"
    return (
        "下面是同一道题的各部分图片，按【标签】顺序给出。\n"
        "请把每一张图里的内容**逐字**转录，严格用下面的格式（有几行就写几行）：\n"
        f"{body}\n"
        "要求：数学公式用一行普通文本（根号 √、平方 ²、分数 a/b、不等号 ≤ ≥），"
        "不要化简、不要计算、不要补全你没看见的内容；看不清就写 [?]。")


def read_item(item: dict[str, Any], dest: Path, workspace: str | Path | None = None,
              prompt: str = "") -> dict[str, Any]:
    """下载一题的图并**一次性**交给视觉模型，返回 ``{"index", "text", "ms", "images"}``。"""
    blocks = as_blocks(item)
    if not blocks:
        return {"text": "", "ms": 0.0, "images": 0}
    urls = [u for _, u in blocks]
    saved = download(urls, dest)
    pairs = [(label, saved[u]) for label, u in blocks if u in saved]
    out = vision.read_images(pairs, prompt=prompt or _prompt_for([lab for lab, _ in pairs]),
                             workspace=workspace, max_tokens=2000)
    return {"text": out.get("text") or "", "ms": out.get("ms") or 0.0,
            "images": len(pairs)}


def summary(items: list[dict[str, Any]]) -> str:
    """给模型看的整卷概览：几题、题干是否图片、每题的源页面。"""
    if not items:
        return "没在这一页找到题目（超星的题干容器不在页面上？）"
    lines = [f"整卷 {len(items)} 题："]
    for i, item in enumerate(items, 1):
        opts = item.get("opts") or []
        text_bit = f"题干文字『{item['stemText']}』" if item.get("stemText") else "题干无文字"
        lines.append(f"  #{i} [{item.get('page')}] {text_bit}，"
                     f"题干图 {len(item.get('stemImgs') or [])} 张，选项 {len(opts)} 个")
    return "\n".join(lines)
