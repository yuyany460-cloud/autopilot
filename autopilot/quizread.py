# -*- coding: utf-8 -*-
"""读题：把网页上的题目**完整**地取出来，包括渲染成图片的公式。

为什么需要这一层——原来的做法有两个致命缺口，都直接影响正确率：

1. **题干被截断**。``questions.json`` 里只存 ``head``（前 260 字符），
   长题（程序阅读题、带说明的多选题）等于把条件砍掉了。
2. **公式是图片，DOM 里一个字都没有**。PTA/超星这类站点的数学公式都是
   ``<img>`` 或自绘字形，``innerText`` 只会得到一个空位；实测 Windows OCR
   把 ``dy/dx`` 读成「迦」、``sin²x`` 读成 ``sin2x``——这种题不是"可能错"，
   而是必错。

所以读题的正确顺序是**由便宜到贵**：

======  ==================================  ======  ==========
优先级  来源                                耗时    准确率
======  ==================================  ======  ==========
①       DOM 里的数学元数据                  0ms     ~100%
        （``<annotation encoding="…tex">``、
        ``alt`` / ``data-tex`` / MathML）
②       视觉模型一次读一题的全部图片          ~1.2s   ~92~98%
③       Windows 系统 OCR                     0.6s    ~8%（公式）
======  ==================================  ======  ==========

第 ① 步常常直接解决问题：很多站点在 ``<img>`` 的 ``alt`` 或相邻的
``<annotation>`` 里就写着 LaTeX，根本不用调模型。只有 ① 拿不到时才走 ②，
而 ② 是**一题一次调用**（题干 + 若干选项图一起送），不是一张图一次。
"""

from __future__ import annotations

import base64
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha1
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import unquote_to_bytes

from . import quizkit, vision

# --------------------------------------------------------------------------
# 抽取：题干按 DOM 顺序拆成「文字 / 图片 / 预格式块」
#
# 关键点：**不要**直接 `innerText`。那样图片位置的信息全丢了，既没法把
# 识别结果插回原位置，也分不清「哪个公式属于哪句话」。
# --------------------------------------------------------------------------

EXTRACT_JS = r"""
() => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const numId = e => e && e.id && /^\d+$/.test(e.id) ? e.id : null;

  // ---- 数学元数据：优先拿现成的 LaTeX，拿不到才算图片 ----
  const texOf = el => {
    const attrs = ['data-tex', 'data-latex', 'data-formula', 'alt', 'title', 'aria-label'];
    for (const a of attrs) {
      let v = '';
      try { v = el.getAttribute(a) || ''; } catch (e) { v = ''; }
      v = norm(v);
      if (v && /[\\^_√∫∑π≤≥]|[A-Za-z]\s*[=<>]|\d\s*\/\s*\d/.test(v) && v.length <= 400) return v;
    }
    let host = null;
    try { host = el.closest('.katex, .MathJax, mjx-container, math, [data-tex]'); } catch (e) {}
    if (host) {
      const ann = host.querySelector('annotation[encoding="application/x-tex"]');
      if (ann && norm(ann.textContent)) return norm(ann.textContent);
      const t = host.getAttribute && host.getAttribute('data-tex');
      if (t) return norm(t);
    }
    return '';
  };

  const pushText = (blocks, text) => {
    const t = text || '';
    if (!t.trim()) {
      if (t.includes('\n')) return;
      return;
    }
    const last = blocks[blocks.length - 1];
    if (last && last.kind === 'text') last.text += t;
    else blocks.push({kind: 'text', text: t});
  };

  const pushImg = (blocks, img) => {
    const src = img.currentSrc || img.src || '';
    let w = img.naturalWidth || img.width || 0;
    let h = img.naturalHeight || img.height || 0;
    blocks.push({kind: 'img', index: blocks.filter(b => b.kind === 'img').length + 1,
                 src: src, alt: texOf(img), w: w, h: h, text: ''});
  };

  const walk = (node, blocks, depth, skip) => {
    if (!node || (depth || 0) > 40) return;
    (node.childNodes || []).forEach(n => {
      if (n.nodeType === 3) { pushText(blocks, n.nodeValue); return; }
      if (n.nodeType !== 1) return;
      if (skip && skip.has(n)) return;          // 选项子树单独抓，不重复进题干
      const tag = (n.tagName || '').toUpperCase();
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'NOSCRIPT' ||
          tag === 'BUTTON' || tag === 'SVG' || tag === 'INPUT') return;
      if (tag === 'IMG') { pushImg(blocks, n); return; }
      if (tag === 'BR') { pushText(blocks, '\n'); return; }
      const cls = n.classList || {contains: () => false};
      if (tag === 'MATH' || cls.contains('katex') || cls.contains('MathJax') ||
          tag === 'MJX-CONTAINER') {
        const tex = texOf(n);
        if (tex) { blocks.push({kind: 'math', text: tex}); return; }
      }
      if (tag === 'PRE' || tag === 'CODE') {
        const raw = (n.innerText || n.textContent || '').replace(/\r\n/g, '\n').trim();
        if (raw) blocks.push({kind: 'pre', text: raw});
        return;
      }
      walk(n, blocks, (depth || 0) + 1, skip);
    });
  };

  const blocksOf = (el, skip) => {
    const blocks = [];
    walk(el, blocks, 0, skip);
    // 合并相邻 text、压掉空白
    const merged = [];
    for (const b of blocks) {
      if (b.kind === 'text') {
        b.text = b.text.replace(/[ \t]{2,}/g, ' ');
        if (!b.text.trim()) continue;
        const last = merged[merged.length - 1];
        if (last && last.kind === 'text') { last.text += b.text; continue; }
      }
      merged.push(b);
    }
    return merged;
  };

  const blocksText = blocks => blocks.map(b =>
      b.kind === 'img' ? '' : (b.text || '')).join('')
    .replace(/[ \t]{2,}/g, ' ')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  // ---- 选项文字（完整，不截断）----
  const optionOf = r => {
    let t = '';
    if (r.id) {
      const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      if (l) t = l.innerText || l.textContent || '';
    }
    if (!t && r.closest('label')) t = r.closest('label').innerText || '';
    if (!t) t = (r.parentElement && (r.parentElement.innerText || r.parentElement.textContent)) || '';
    return norm(t);
  };

  const imgListOf = el => {
    const out = [];
    el.querySelectorAll('img').forEach(img => {
      out.push({src: img.currentSrc || img.src || '', alt: texOf(img),
                w: img.naturalWidth || img.width || 0, h: img.naturalHeight || img.height || 0});
    });
    return out;
  };

  const problems = [];
  const seen = new Set();

  // ---- 选择题：按 radio 的 name 分组 ----
  const radios = Array.from(document.querySelectorAll('input[type=radio]'));
  const groups = {};
  radios.forEach(r => { (groups[r.name] = groups[r.name] || []).push(r); });
  for (const name of Object.keys(groups)) {
    const rs = groups[name];
    let box = rs[0];
    while (box && !numId(box)) box = box.parentElement;
    // 选项各自的宿主元素单独抓（下面 options 里），题干 walk 时跳过，避免重复
    const optHosts = new Set();
    rs.forEach(r => {
      const label = r.closest('label');
      if (label) optHosts.add(label);
      else if (r.parentElement && r.parentElement !== box) optHosts.add(r.parentElement);
    });
    const blocks = box ? blocksOf(box, optHosts) : [];
    const full = box ? ((box.innerText || '')) : '';
    problems.push({
      label: (full.match(/^(\d+-\d+)/) || [, ''])[1],
      id: name,
      kind: 'choice',
      blocks: blocks,
      text: blocksText(blocks),
      options: rs.map((r, i) => ({index: i, text: optionOf(r), imgs: imgListOf(r.closest('label') || r.parentElement || r)})),
      images: blocks.filter(b => b.kind === 'img').length,
    });
    if (box) seen.add(box);
  }

  // ---- 编程题：带数字 id 且挂了编辑器的容器 ----
  const editorSel = '.monaco-editor, .CodeMirror, .cm-editor, textarea, [contenteditable=true]';
  Array.from(document.querySelectorAll('div[id]')).forEach(box => {
    const id = numId(box);
    if (!id || seen.has(box)) return;
    if (!box.querySelector(editorSel)) return;
    const blocks = blocksOf(box);
    const full = box.innerText || '';
    problems.push({
      label: (full.match(/^(\d+-\d+)/) || [, ''])[1],
      id: id,
      kind: 'program',
      blocks: blocks,
      text: blocksText(blocks),
      options: [],
      images: blocks.filter(b => b.kind === 'img').length,
      editors: {
        monaco: box.querySelectorAll('.monaco-editor').length,
        codemirror: box.querySelectorAll('.CodeMirror, .cm-editor').length,
        textarea: box.querySelectorAll('textarea').length,
        contenteditable: box.querySelectorAll('[contenteditable=true]').length,
      },
    });
    seen.add(box);
  });

  problems.sort((a, b) => String(a.label).localeCompare(String(b.label), 'zh', {numeric: true}));
  return {
    url: location.href,
    title: document.title,
    count: problems.length,
    problems: problems,
    pageButtons: Array.from(document.querySelectorAll('button')).map(b => norm(b.innerText)).filter(Boolean).slice(0, 40),
  };
}
"""


def extract(page: Any) -> dict[str, Any]:
    """在页面上跑 ``EXTRACT_JS``，返回整页题目（含完整的题干块与图片清单）。"""
    data = page.evaluate(EXTRACT_JS) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"题目抽取结果异常：{type(data).__name__}")
    data.setdefault("problems", [])
    data["count"] = len(data["problems"])
    return data


# --------------------------------------------------------------------------
# 图片落地与识别
# --------------------------------------------------------------------------


def _suffix(src: str, default: str = ".png") -> str:
    tail = src.split("?")[0].rsplit(".", 1)[-1].lower()
    return "." + tail if tail in {"png", "jpg", "jpeg", "gif", "webp", "bmp"} else default


def fetch_bytes(page: Any, src: str, timeout: float = 15.0) -> bytes:
    """取图片字节：data URI → 浏览器上下文请求 → 页面内 fetch → 裸 HTTP。"""
    if not src:
        raise ValueError("图片地址为空")
    if src.startswith("data:"):
        head, _, body = src.partition(",")
        if "base64" in head:
            return base64.b64decode(body)
        return unquote_to_bytes(body)

    # ① 用浏览器上下文发请求：自动带上页面的 cookie / 登录态
    try:
        response = page.request.get(src, timeout=int(timeout * 1000))
        if response.ok:
            body = response.body()
            if body:
                return body
    except Exception:
        pass

    # ② 页面内 fetch（有些站点要求站内 Referer / 自定义头）
    try:
        encoded = page.evaluate(
            """async (url) => {
                const resp = await fetch(url, {credentials: 'include'});
                if (!resp.ok) return '';
                const buf = await resp.arrayBuffer();
                const bytes = new Uint8Array(buf);
                let binary = '';
                for (let i = 0; i < bytes.length; i += 0x8000) {
                    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
                }
                return btoa(binary);
            }""", src)
        if encoded:
            return base64.b64decode(encoded)
    except Exception:
        pass

    # ③ 公开直链
    request = urllib.request.Request(src, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"),
        "Referer": src.split("/", 3)[0] + "//" + src.split("/", 3)[2] + "/",
    })
    with urllib.request.urlopen(request, timeout=timeout) as handle:
        return handle.read()


def _problem_images(problem: dict[str, Any]) -> list[dict[str, Any]]:
    """这题里所有"需要读"的图片：题干里的 + 选项里的。"""
    out: list[dict[str, Any]] = []
    for block in problem.get("blocks") or []:
        if block.get("kind") == "img":
            out.append(block)
    for option in problem.get("options") or []:
        for img in option.get("imgs") or []:
            out.append(img)
    return out


def _needs_reading(img: dict[str, Any]) -> bool:
    """已经有 DOM 里的 LaTeX/alt 的图片不用再花一次视觉调用。"""
    if img.get("text"):
        return False
    src = str(img.get("src") or "")
    if not src or src.startswith("data:image/svg"):     # 内联 SVG 图标基本是装饰
        return False
    if int(img.get("w") or 0) < 12 or int(img.get("h") or 0) < 8:   # 占位图/分隔线
        return False
    return True


def prepare_images(page: Any, problems: Sequence[dict[str, Any]],
                   dest: str | Path, workspace: str | Path | None = None) -> dict[str, Any]:
    """把题目里的图片先落地到本地，并把 DOM 里已有的 LaTeX 直接填进 block。

    返回 ``{"problems": 需要视觉读取的题目数, "images": 需要读的图片数,
    "dom_tex": 直接由 DOM 解决的数量, "failures": [...]}``。
    """
    base = Path(dest)
    base.mkdir(parents=True, exist_ok=True)
    stats = {"images": 0, "dom_tex": 0, "failures": []}

    for problem in problems:
        for img in _problem_images(problem):
            alt = str(img.get("alt") or "").strip()
            if alt:
                img["text"] = alt
                img["source"] = "dom"
                stats["dom_tex"] += 1
                continue
            if not _needs_reading(img):
                continue
            src = str(img.get("src") or "")
            try:
                body = fetch_bytes(page, src)
            except Exception as exc:
                stats["failures"].append(f"{problem.get('label')}: {str(exc)[:80]}")
                continue
            if not body:
                stats["failures"].append(f"{problem.get('label')}: 图片是空的")
                continue
            path = base / f"{sha1(body).hexdigest()[:16]}{_suffix(src)}"
            if not path.is_file():
                path.write_bytes(body)
            img["path"] = str(path)
            stats["images"] += 1
    return stats


def _prompt_for(labels: Sequence[str]) -> str:
    """一题一次调用的转录指令：按实际存在的标签生成格式，避免模型编造选项。"""
    body = "\n".join(f"{label}：<逐字转录>" for label in labels)
    return (
        "下面是同一道题的各个部分（题干和选项）的图片，按【标签】顺序给出。\n"
        "把每一张图**逐字**转录，严格按下面的格式输出（有几行就写几行，不要多写别的）：\n"
        f"{body}\n"
        "规则：\n"
        "- 数学公式用一行普通文本：根号 √、平方 ²、分数 a/b、不等号 ≤ ≥ ≠、π、∫、∑。\n"
        "- 不要化简、不要计算、不要翻译、不要补全你没看见的内容。\n"
        "- 看不清的字符写 [?]，不要猜。"
    )


def read_problem_images(problems: Sequence[dict[str, Any]],
                        workspace: str | Path | None = None,
                        jobs: int = 4, prompt: str = "",
                        cache_dir: str | Path | None = None) -> dict[str, Any]:
    """把已落地的图片交给视觉模型，**一题一次调用**，多题并行。

    识别结果按标签回填到 ``block["text"]`` / ``option["text_image"]``，
    同时整题结果也存进 ``problem["vision_text"]`` 便于排查。

    为什么一题一次：题目常有多张图（题干 + 公式 + 选项），分开调用的代价是
    N 次模型往返（每次 0.9~1.2s）且互相看不到上下文；合成一次只有一次往返，
    模型还能利用"这道题在问什么"把形近符号读对。
    """
    stats = {"problems": 0, "images": 0, "cached": 0, "failures": []}

    def work(problem: dict[str, Any]) -> None:
        pending = [img for img in _problem_images(problem)
                   if _needs_reading(img) and img.get("path")]
        if not pending:
            return
        pending_ids = {id(img) for img in pending}

        # 标签按 DOM 顺序排：题干里的图依次叫"题干/题干图2…"，选项里的叫"选项A/B…"
        todo: list[tuple[str, dict[str, Any]]] = []
        stem_seen = 0
        for block in problem.get("blocks") or []:
            if block.get("kind") == "img" and id(block) in pending_ids:
                stem_seen += 1
                todo.append(("题干" if stem_seen == 1 else f"题干图{stem_seen}", block))
        for i, option in enumerate(problem.get("options") or []):
            letter = chr(ord("A") + i)
            seen = 0
            for img in option.get("imgs") or []:
                if id(img) not in pending_ids:
                    continue
                seen += 1
                todo.append((f"选项{letter}" if seen == 1 else f"选项{letter}图{seen}", img))
        if not todo:
            return
        labels = [label for label, _ in todo]
        pairs = [(label, str(img["path"])) for label, img in todo]
        try:
            result = vision.read_images(
                pairs, prompt=prompt or _prompt_for(labels),
                workspace=workspace, max_tokens=1200,
                cache_dir=cache_dir)
        except Exception as exc:
            stats["failures"].append(f"{problem.get('label')}: {str(exc)[:100]}")
            return

        problem["vision_text"] = result.get("text") or ""
        if result.get("cached"):
            stats["cached"] += 1
        parsed = quizkit.parse_labeled(problem["vision_text"], labels)
        for label, img in todo:
            text = parsed.get(quizkit.canon_label(label)) or parsed.get(label) or ""
            if text:
                img["text"] = text
                img["source"] = "cached" if result.get("cached") else "vision"
        stats["problems"] += 1
        stats["images"] += len(todo)

    workers = max(1, min(int(jobs or 1), len(problems) or 1))
    if workers == 1:
        for problem in problems:
            work(problem)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(work, problems))
    return stats


def compose_problem(problem: dict[str, Any], budget: int = 4000) -> str:
    """把一题合成给模型看的完整文本：题干（含公式内容）+ 选项。

    **不修改传入的 problem**：这个函数会被问句生成、人看的清单、调试各调一次，
    一旦就地改 ``option["text"]``，图片内容会被反复追加成
    ``C. 3eˣ C. 3eˣ C. 3eˣ``（实测很容易踩到）。
    """
    blocks = []
    for block in problem.get("blocks") or []:
        kind = str(block.get("kind") or "text")
        if kind == "img":
            # text 是视觉转录结果；还没读到时先用 DOM 里的 alt（常常就是 LaTeX），
            # 这样"只抽题不读图"的场景也能拿到公式
            blocks.append({"kind": "img", "index": block.get("index"),
                           "text": block.get("text") or block.get("alt") or ""})
        else:
            blocks.append({"kind": kind, "text": block.get("text") or ""})

    raw_options = problem.get("options") or []
    base_options = [(int(o.get("index") or i), str(o.get("text") or ""))
                    for i, o in enumerate(raw_options)]
    stem = quizkit.strip_options(quizkit.compose_text(blocks), base_options)

    # 选项里的图片：读到的内容并进选项文字；没读到的**必须留标记**，
    # 否则模型会把"选项 C：<空>"当成一个空选项，甚至反过来选它。
    display: list[tuple[int, str]] = []
    for i, option in enumerate(raw_options):
        text = str(option.get("text") or "").strip()
        pieces = []
        for img in option.get("imgs") or []:
            body = str(img.get("text") or img.get("alt") or "").strip()
            pieces.append(quizkit.normalize_math(body) if body else "〔图未读出〕")
        if pieces:
            text = (text + " " + " ".join(pieces)).strip()
        display.append((int(option.get("index") or i), text))

    lines = [quizkit.fit_budget(stem, budget)]
    if display:
        lines.append("")
        for index, text in display:
            lines.append(f"  [{index}] {text}")
    return "\n".join(lines).strip()


def read_page(page: Any, dest: str | Path, workspace: str | Path | None = None,
              jobs: int = 4, prompt: str = "") -> dict[str, Any]:
    """一步到位：抽题 → 落地图片 → 视觉转录（可选）→ 返回可用的题目数据。"""
    data = extract(page)
    stats = prepare_images(page, data["problems"], dest, workspace)
    stats.update(read_problem_images(data["problems"], workspace=workspace, jobs=jobs,
                                     prompt=prompt))
    data["stats"] = stats
    return data


def summarize(data: dict[str, Any]) -> str:
    """一句话说明这一页读到了什么、图片是怎么解决的（给人看）。"""
    stats = data.get("stats") or {}
    problems = data.get("problems") or []
    with_images = [p for p in problems if p.get("images")]
    bits = [f"{data.get('count', len(problems))} 题"]
    if with_images:
        bits.append(f"{len(with_images)} 题含图片")
    if stats:
        bits.append(f"DOM 直读 {stats.get('dom_tex', 0)} 张")
        bits.append(f"视觉识别 {stats.get('images', 0)} 张"
                    f"（命中缓存 {stats.get('cached', 0)} 题）")
        if stats.get("failures"):
            bits.append(f"失败 {len(stats['failures'])} 处")
    return "，".join(bits)


__all__ = ["EXTRACT_JS", "compose_problem", "extract", "fetch_bytes", "prepare_images",
           "read_page", "read_problem_images", "summarize"]
