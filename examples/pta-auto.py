# -*- coding: utf-8 -*-
"""PTA 作业自动化 —— 全程 0 token（不调用任何模型）。

为什么要有这个脚本：
    Agent（``apilot run``）每一步都要调一次大模型，30 步就是几十万 token。
    但"点哪里、填什么"本身**不需要智能**——需要智能的只是"答案是什么"，
    而那个只需要确定一次。把两者拆开后，执行部分就能完全脚本化。

用法（在项目根目录下执行）::

    python examples/pta-auto.py --dump          # 导出题目到 var/pta/（含图片公式）
    #   → 打开 var/pta/questions.txt 看题，把答案填进 var/pta/answers.json
    python examples/pta-auto.py --fill          # 把答案填进页面（不提交）
    python examples/pta-auto.py --submit --yes  # 确认无误后再提交

答案文件格式::

    {"answers": [
      {"label": "2-1", "type": "choice", "index": 1},          # 选第 1 个选项（从 0 起）
      {"label": "2-2", "type": "choice", "text": "最近的if"},   # 或者按选项文字模糊匹配
      {"label": "7-1", "type": "program", "code": "int main(){}"}
    ]}

关于登录：PTA 用的是**会话级 cookie**，关掉浏览器就失效。
    脚本会把浏览器保持打开，并在检测到未登录时提示你手动登录一次。

本脚本只做三件事：读页面、填答案、按你的指令提交。
它不会自己判断答案，也不会在你没加 ``--yes`` 时提交任何东西。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from autopilot import quizkit, quizread
from autopilot.actions import _browser_exe, _cdp_ready          # noqa: E402

PROFILE = ROOT / "var" / "browser-profile"
OUT_DIR = ROOT / "var" / "pta"
IMG_DIR = OUT_DIR / "images"
CDP = "http://127.0.0.1:9222"
DEFAULT_URL = ("https://pintia.cn/problem-sets/2102237619822460928"
               "/exam/problems/type/2")

# --------------------------------------------------------------------------
# 页面结构说明（实测自 pintia.cn 2026 版考试页）
#
#   单选题：每题的 4 个 radio 共享同一个 name，**name 就是题目 ID**；
#           题目容器是 id 等于该 name 的 div；选项文字在关联的 <label> 里，
#           形如 "A. 105"。radio 自身的 value 恒为 "on"，没用。
#   编程题：容器同样是带数字 id 的 div，里面挂 Monaco / CodeMirror / textarea。
#
#   所以一切以 "按 radio 的 name 分组" 为锚点，比猜 CSS 类名可靠得多。
# --------------------------------------------------------------------------

FILL_JS = r"""
(payload) => {
  const dry = !!payload.dry;
  const items = payload.items || payload;
  const txt = e => (e && (e.innerText || e.textContent) || '').trim();
  const optionText = r => {
    let t = '';
    if (r.id) {
      const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      if (l) t = txt(l);
    }
    if (!t && r.closest('label')) t = txt(r.closest('label'));
    if (!t) t = txt(r.parentElement);
    return t.replace(/\s+/g, ' ');
  };

  const radios = Array.from(document.querySelectorAll('input[type=radio]'));
  const groups = {};
  radios.forEach(r => { (groups[r.name] = groups[r.name] || []).push(r); });
  // 同时支持按 题目ID 和 题号(2-1) 两种指定方式
  const byLabel = {};
  for (const name of Object.keys(groups)) {
    const box = (() => { let b = groups[name][0];
                         while (b && !(b.id && /^\d+$/.test(b.id))) b = b.parentElement;
                         return b; })();
    const m = box ? txt(box).match(/^(\d+-\d+)/) : null;
    if (m) byLabel[m[1]] = name;
  }

  const done = [], missed = [];
  for (const item of items) {
    const key = groups[item.label] ? item.label : byLabel[item.label];
    if (!key || !groups[key]) { missed.push({label: item.label, why: '找不到该题'}); continue; }
    const rs = groups[key];
    let target = null;
    if (typeof item.index === 'number') target = rs[item.index] || null;
    if (!target && item.text) {
      target = rs.find(r => optionText(r).includes(item.text)) || null;
    }
    if (!target) { missed.push({label: item.label, why: '找不到对应选项'}); continue; }
    if (!dry) {
      target.click();
      target.dispatchEvent(new Event('change', {bubbles: true}));
    }
    done.push({label: item.label, picked: optionText(target), dry});
  }
  return {done, missed, dry};
}
"""

FILL_PROGRAM_JS = r"""
(payload) => {
  const txt = e => (e && (e.innerText || e.textContent) || '').trim();
  const boxes = {};
  Array.from(document.querySelectorAll('div[id]')).forEach(box => {
    if (/^\d+$/.test(box.id) && box.querySelector('.monaco-editor, .CodeMirror, textarea, [contenteditable=true]')) {
      const m = txt(box).match(/^(\d+-\d+)/);
      if (m) boxes[m[1]] = box;
    }
  });

  const done = [], missed = [];
  for (const item of payload) {
    const box = boxes[item.label];
    if (!box) { missed.push({label: item.label, why: '找不到该编程题容器'}); continue; }

    // Monaco：优先按"哪個 model 属于这个容器"来定位
    if (window.monaco && window.monaco.editor) {
      try {
        const models = window.monaco.editor.getModels();
        let hit = null;
        box.querySelectorAll('.monaco-editor').forEach(el => {
          for (const m of models) {
            try {
              const nodes = el.querySelectorAll('*');
              if (m.uri && nodes.length) { /* 无法直接映射，退到顺序 */ }
            } catch (e) {}
          }
        });
        hit = models[0];
        if (models.length > 1) {
          const idx = Array.from(document.querySelectorAll('div[id]'))
              .filter(b => /^\d+$/.test(b.id) && b.querySelector('.monaco-editor'))
              .findIndex(b => b === box);
          if (idx >= 0 && models[idx]) hit = models[idx];
        }
        if (hit) { hit.setValue(item.code); done.push({label:item.label, via:'monaco'}); continue; }
      } catch (e) {}
    }
    const cmEl = box.querySelector('.CodeMirror');
    if (cmEl && cmEl.CodeMirror) {
      cmEl.CodeMirror.setValue(item.code);
      done.push({label:item.label, via:'codemirror'});
      continue;
    }
    const ce = box.querySelector('[contenteditable=true]');
    if (ce) {
      ce.focus();
      ce.innerHTML = '';
      document.execCommand('insertText', false, item.code);
      ce.dispatchEvent(new InputEvent('input', {bubbles: true}));
      done.push({label:item.label, via:'contenteditable'});
      continue;
    }
    const ta = box.querySelector('textarea');
    if (ta) {
      const setter = Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, item.code);
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      done.push({label:item.label, via:'textarea'});
      continue;
    }
    missed.push({label: item.label, why: '容器里没有可写的编辑器'});
  }
  return {done, missed};
}
"""

SUBMIT_JS = r"""
() => {
  const txt = e => (e && (e.innerText || e.textContent) || '').trim();
  const btns = Array.from(document.querySelectorAll('button, a[role=button]'));
  const hit = btns.find(b => /^(提交|交卷|提交答案|提交作业|提交本题作答)$/.test(txt(b)));
  if (!hit) return {ok: false, seen: btns.map(txt).filter(Boolean).slice(0, 40)};
  hit.click();
  return {ok: true, clicked: txt(hit)};
}
"""

# --------------------------------------------------------------------------
# 编程题（单题视图 + CodeMirror 6）
#
# PTA 的编程题页一次只显示一道题，靠左侧题号导航或「上一题/下一题」切换。
# 编辑器是 CodeMirror 6（contenteditable），没有 setValue 接口，
# 但 execCommand('insertText') 能可靠写进去（实测）。
# 页面上还有好几个只读的 .cm-editor 用来展示样例输入输出——
# 靠父元素类名里的 readOnly 区分，代码编辑器那个没有。
# --------------------------------------------------------------------------

NAV_JS = r"""
() => Array.from(document.querySelectorAll('a[href]'))
    .filter(e => /^\d{1,2}$/.test((e.innerText || '').trim()))
    .map(e => ({n: parseInt(e.innerText.trim()), href: e.getAttribute('href')}))
    .filter((v, i, a) => a.findIndex(x => x.n === v.n) === i)
    .sort((a, b) => a.n - b.n)
"""

READ_JS = r"""
() => {
  const txt = document.body.innerText;
  const out = {url: location.href, title: document.title};
  const m = document.title.match(/^(\d+-\d+)\s+(.+?)\s+-\s+/);
  out.label = m ? m[1] : '';
  out.name = m ? m[2] : '';
  let body = txt;
  const s = body.indexOf('题目描述');
  if (s >= 0) body = body.slice(s);
  const e = body.indexOf('代码长度限制');
  if (e > 0) body = body.slice(0, e);
  // 样例输入输出：别再拿正则去啃 body 文本了——那里混着行号、"复制内容/格式/全屏"
  // 按钮、折叠按钮的"收起"等等，实测会把样例抠坏（空输入、少第一行）。
  // 正确做法：样例本身就渲染在只读的 CodeMirror 块里，按 DOM 顺序配对即可。
  const roBlocks = Array.from(document.querySelectorAll('.cm-editor'))
      .filter(e => ((e.parentElement && e.parentElement.className) || '').includes('readOnly'));
  const blockText = e => {
    const c = e.querySelector('.cm-content');
    return c ? (c.innerText || '').replace(/\r\n/g, '\n').trim() : '';
  };
  const leaves = Array.from(document.querySelectorAll('*'))
      .filter(e => e.children.length === 0 && /^(输入|输出)样例/.test((e.innerText || '').trim()));
  const after = (a, list) => list
      .filter(b => a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)
      .sort((x, y) => (x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING) ? -1 : 1);

  const samples = [];
  const inLabels = leaves.filter(e => /^输入样例/.test((e.innerText || '').trim()));
  for (const lab of inLabels) {
    const inBlock = after(lab, roBlocks)[0];
    if (!inBlock) continue;
    const outLab = leaves.find(e => /^输出样例/.test((e.innerText || '').trim())
                                    && (inBlock.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_FOLLOWING));
    const outBlock = outLab ? after(outLab, roBlocks)[0] : null;
    samples.push({in: blockText(inBlock), out: outBlock ? blockText(outBlock) : ''});
  }
  out.text = body.trim().slice(0, 3000);
  out.samples = samples;
  return out;
}
"""

CODE_EDITOR_FIND = ("Array.from(document.querySelectorAll('.cm-editor'))"
                    ".find(e => !((e.parentElement && e.parentElement.className) || '')"
                    ".includes('readOnly'))")

WRITE_JS = """
(code) => {
  const ed = %s;
  if (!ed) return {ok: false, why: '找不到代码编辑器（没有非只读的 .cm-editor）'};
  const c = ed.querySelector('.cm-content');
  if (!c) return {ok: false, why: '编辑器里没有 .cm-content'};
  c.focus();
  document.execCommand('selectAll');
  document.execCommand('insertText', false, code);
  return {ok: true, got: (c.innerText || '').slice(0, 100)};
}
""" % CODE_EDITOR_FIND

READ_CODE_JS = """
() => {
  const ed = %s;
  return ed ? (ed.querySelector('.cm-content').innerText || '') : '';
}
""" % CODE_EDITOR_FIND


def nav_items(page) -> list[dict]:
    """左侧题号导航：``[{n, href}, ...]``。"""
    return page.evaluate(NAV_JS) or []


def is_program_page(page) -> bool:
    """单选页有 radio；编程题页没有 radio 但有可写的 CodeMirror。"""
    return page.evaluate(
        "() => document.querySelectorAll('input[type=radio]').length === 0"
        " && !!%s" % CODE_EDITOR_FIND)


def goto_problem(page, href: str, settle: int = 600, timeout: int = 8000) -> None:
    """按 href 直接导航到某道题。

    最早是模拟点击题号，但 SPA 的切换时序很不稳定（实测点了 1~9 都停在原题），
    而这些 ``<a>`` 本来就带 href，直接 goto 又准又快。

    翻页等待从"固定 sleep 2.5 秒"改成"等正文和编辑器出现"：条件通常
    0.4~0.8 秒就满足（10 道题省 15 秒以上），而真慢的时候它会一直等到
    ``timeout``，比死等 2.5 秒然后对着半张页面抓题更稳。
    """
    url = href if href.startswith("http") else "https://pintia.cn" + href
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    try:
        page.wait_for_function(
            """() => {
                const body = document.body ? (document.body.innerText || '') : '';
                if (body.length < 200) return false;
                return !!document.querySelector(
                    '.cm-content, .monaco-editor, .CodeMirror, textarea, [contenteditable=true]');
            }""",
            timeout=timeout)
    except Exception:
        pass          # 条件没满足也继续：宁可抓到半页，也别直接报错退出
    page.wait_for_timeout(settle)


# --------------------------------------------------------------------------
# 浏览器连接
# --------------------------------------------------------------------------


def same_problem_set(current: str, target: str) -> bool:
    """判断当前页是不是目标那个题目集。

    只比 'exam/problems' 是不够的——PTA 里每个作业的 URL 都长这样，
    换一个作业或换一个题型（type/2 单选 vs type/7 编程）就完全不是一回事了。
    """
    m = re.search(r"/problem-sets/(\d+)", target)
    if not m:
        return True
    if f"/problem-sets/{m.group(1)}" not in current:
        return False
    # 题型也要一致
    t = re.search(r"/problems/type/(\d+)", target)
    return not t or f"/problems/type/{t.group(1)}" in current


def goto_target(page, url: str) -> None:
    if same_problem_set(page.url, url):
        print(f"当前页面：{page.url}")
        return
    print(f"导航到目标页面…（当前在 {page.url[:80]}）")
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
    page.wait_for_timeout(4000)


def ensure_browser(url: str, headless: bool = False):
    if not _cdp_ready(CDP, timeout=0.6):
        exe = _browser_exe()
        if not exe:
            raise SystemExit("找不到 msedge.exe / chrome.exe")
        print("启动调试浏览器…")
        PROFILE.mkdir(parents=True, exist_ok=True)
        args = [exe, "--remote-debugging-port=9222", f"--user-data-dir={PROFILE}",
                "--no-first-run", "--no-default-browser-check",
                "--force-renderer-accessibility"]
        if not headless:
            args.append(url)
        subprocess.Popen(args, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if not _cdp_ready(CDP, timeout=30):
            raise SystemExit("调试端口一直没就绪：可能已有 Edge 在用这个配置目录")

    from playwright.sync_api import sync_playwright

    pw = sync_playwright().start()
    browser = pw.chromium.connect_over_cdp(CDP)
    context = browser.contexts[0] if browser.contexts else browser.new_context()
    pages = [p for p in context.pages if not p.is_closed()]
    page = pages[-1] if pages else context.new_page()
    return pw, page


def is_logged_out(page) -> bool:
    try:
        text = page.inner_text("body")[:2000]
    except Exception:
        return False
    if page.url.rstrip("/").endswith("pintia.cn/home") or "/auth/login" in page.url:
        return True
    return "登录" in text and "退出" not in text and "题目总览" not in text


def wait_for_login(page, url: str, timeout: float = 300.0) -> None:
    print("\n" + "=" * 62)
    print("检测到未登录。请在打开的浏览器窗口里登录 PTA（不用管这个终端）。")
    print("提示：PTA 是会话级登录，关掉浏览器就要重新登一次。")
    print("=" * 62)
    try:
        page.goto("https://pintia.cn/auth/login", wait_until="domcontentloaded",
                  timeout=60000)
    except Exception:
        pass
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(3)
        try:
            text = page.inner_text("body")
        except Exception:
            continue
        if "退出" in text or "题目总览" in text or "我的题目集" in text:
            print("\n已登录，继续。")
            page.goto(url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            return
        print("  等待登录中…（还剩 %d 秒）" % int(deadline - time.time()), end="\r")
    raise SystemExit("等待登录超时")


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------


def _render_questions_txt(data: dict) -> str:
    """人看的题目清单：**全文题干**（含公式识别结果），不截断。"""
    lines = [f"# {data.get('title')}", f"# {data.get('url')}",
             f"# 共 {data.get('count')} 题", ""]
    for p in data.get("problems") or []:
        lines.append(f"## {p.get('label')}  [{p.get('kind')}]  (id={p.get('id')})")
        body = quizread.compose_problem(p, budget=6000) if p.get("blocks") else (p.get("text") or "")
        lines.append(body)
        for i, sample in enumerate(p.get("samples") or [], 1):
            lines.append(f"   样例{i} 输入: {sample.get('in')!r}")
            lines.append(f"   样例{i} 输出: {sample.get('out')!r}")
        lines.append("")
    buttons = data.get("pageButtons") or []
    if buttons:
        lines.append("页面级按钮: " + ", ".join(buttons))
    return "\n".join(lines)


def cmd_dump(page, args) -> int:
    # 编程题是单题视图，结构跟单选题页完全不同，先分流
    if is_program_page(page):
        return dump_program(page, args)

    print("抓取题目（题干全文 + 图片公式）…")
    data = quizread.read_page(page, IMG_DIR, workspace=ROOT, jobs=args.jobs,
                              prompt=args.read_prompt)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "questions.json", data)
    (OUT_DIR / "questions.txt").write_text(_render_questions_txt(data), encoding="utf-8")

    ans_file = OUT_DIR / "answers.json"
    if not ans_file.exists():
        template = {
            "_说明": "choice 用 index（选项序号，从 0 开始）或 text（选项文字片段）；"
                     "program 用 code。填完跑 --fill",
            "answers": [
                {"label": p["label"],
                 "type": p["kind"],
                 **({"index": None} if p["kind"] == "choice" else {"code": ""})}
                for p in data["problems"]
            ],
        }
        ans_file.write_text(json.dumps(template, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"已生成答案模板：{ans_file}")
        print("  （用记事本编辑没问题，带 BOM 也读得进来）")

    print(f"\n共 {data['count']} 题，已导出到 {OUT_DIR}")
    print(f"  读题结果：{quizread.summarize(data)}")
    for failure in (data.get("stats") or {}).get("failures") or []:
        print(f"  ⚠ 图片读取失败：{failure}")
    print("  questions.txt   （人看的，题干是全文）")
    print("  questions.json  （机器读的，含图片识别结果）")
    print("  answers.json    ← 把答案填这里，或跑 --solve")
    print("\n题目概览：")
    for p in data["problems"]:
        n = len(p.get("options") or [])
        flags = []
        if p.get("images"):
            flags.append(f"图{p['images']}")
        unread = sum(1 for o in (p.get("options") or [])
                     for img in (o.get("imgs") or []) if not img.get("text"))
        if unread:
            flags.append(f"未读图{unread}")
        print(f"  {p['label']:6} {p['kind']:8} "
              f"{'选项 ' + str(n) if n else '编辑器'}  {' '.join(flags):10} "
              f"{(p.get('text') or '').replace(chr(10), ' ')[:56]}")
    return 0


def read_json(path: Path):
    """读 JSON，容忍 BOM。

    记事本另存 UTF-8 时会加 BOM，直接 ``encoding="utf-8"`` 会报
    "Unexpected UTF-8 BOM"。用户编辑 answers.json 几乎必然会踩到。
    """
    text = path.read_text(encoding="utf-8-sig")
    return json.loads(text)


def write_json(path: Path, data) -> None:
    """写 JSON，不带 BOM（PowerShell 的 Set-Content -Encoding UTF8 会带）。"""
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 编程题：逐题抓题面 / 逐题写代码
# --------------------------------------------------------------------------


def dump_program(page, args) -> int:
    """编程题页一次只显示一道题，按 href 逐题翻。

    每题做两件事：① 用 ``quizread`` 抽题干（含**公式图片**，程序题的数学
    表达式也常是图片）；② 用 ``READ_JS`` 抽样例输入输出（那部分在只读
    CodeMirror 里，DOM 结构比正文可靠）。

    原有的固定 ``sleep(2.5s)`` 换成了"等编辑器/正文出现"的条件等待：
    条件一般 0.4~0.8 秒就满足，10 道题能省 15 秒以上，且比死等更稳。
    """
    navs = nav_items(page)
    if not navs:
        print("找不到题号导航，无法逐题抓取。")
        return 1
    print(f"检测到编程题页，共 {len(navs)} 题，逐题抓取中…")

    problems = []
    for item in navs:
        goto_problem(page, item["href"])
        try:
            info = page.evaluate(READ_JS) or {}
        except Exception:
            info = {}
        extracted = quizread.extract(page)
        candidates = extracted.get("problems") or []
        if candidates:
            problem = candidates[0]
            for other in candidates[1:]:
                if other.get("label") == (info.get("label") or ""):
                    problem = other
                    break
        else:
            problem = {"kind": "program", "blocks": [{"kind": "text",
                                                      "text": info.get("text") or ""}],
                       "options": [], "images": 0}
        problem["label"] = info.get("label") or problem.get("label") or str(item["n"])
        problem["name"] = info.get("name") or ""
        problem["kind"] = "program"
        problem["samples"] = info.get("samples") or []
        problem["href"] = item["href"]
        problems.append(problem)
        print(f"  {problem.get('label') or item['n']:8} "
              f"{problem.get('name', '')[:40]}  样例 {len(problem['samples'])} 组"
              f"  图 {problem.get('images') or 0}")

    # 图片落地 + 视觉识别：按题批量提交、多题并行
    prepared = quizread.prepare_images(page, problems, IMG_DIR, workspace=ROOT)
    vision_stats = quizread.read_problem_images(problems, workspace=ROOT, jobs=args.jobs,
                                                prompt=args.read_prompt)
    data = {"url": page.url, "title": page.title(), "count": len(problems),
            "problems": problems, "pageButtons": [],
            "stats": {**prepared, **vision_stats}}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "questions.json", data)
    (OUT_DIR / "questions.txt").write_text(_render_questions_txt(data), encoding="utf-8")

    ans_file = OUT_DIR / "answers.json"
    if not ans_file.exists() or args.force:
        write_json(ans_file, {
            "_说明": "program 题填 code；先跑 --solve 可以让模型生成",
            "answers": [{"label": p.get("label"), "type": "program", "code": ""}
                        for p in problems],
        })
    print(f"\n已导出到 {OUT_DIR}")
    print(f"  读题结果：{quizread.summarize(data)}")
    for failure in (data.get("stats") or {}).get("failures") or []:
        print(f"  ⚠ {failure}")
    print("  下一步：python examples/pta-auto.py --solve")
    return 0


PROGRAM_SOLVE_SYSTEM = """你是 C 语言程序设计助教。用户会给你若干道编程题，请为每道题写出完整的 C 语言程序。

严格只输出一个 JSON 对象，不要解释、不要 markdown 代码块，格式：
{"answers": [{"label": "7-1", "code": "#include <stdio.h>\\nint main(){...}"}]}

要求：
- 程序必须完整、可直接编译运行（含 #include <stdio.h> 和 int main）。
- 严格遵守题目的输入格式与输出格式，**输出格式一个空格都不能差**（很多题会因为这个判错）。
- 用 double 处理实数，按题目要求控制小数位数。
- 代码放在 code 字段里，用 \\n 表示换行。

题面里的数学公式已经被转成普通文本（√ 表示根号、^ 表示乘方、/ 表示分数），
形状为〔图N: …〕的片段是题面中的图片被识别后的内容，属于题目的一部分。
样例输入/输出是**权威**：它们比题面的文字描述更能决定程序的真实行为。"""


def build_program_prompt(data: dict) -> str:
    lines = [f"共 {data['count']} 道编程题。", ""]
    for p in data["problems"]:
        lines.append(f"【{p.get('label')}】{p.get('name', '')}")
        lines.append(p.get("text", "")[:1600])
        for i, s in enumerate(p.get("samples") or [], 1):
            lines.append(f"样例{i} 输入：{s['in']}")
            lines.append(f"样例{i} 输出：{s['out']}")
        lines.append("")
    return "\n".join(lines)


WRITE_API_JS = r"""
(code) => {
  const isRO = e => ((e.parentElement && e.parentElement.className) || '').includes('readOnly');
  const visible = e => e && e.offsetParent !== null;

  // ① Monaco：直接改 model，不经过任何输入事件（粘贴拦截对它无效）
  try {
    if (window.monaco && window.monaco.editor) {
      const eds = Array.from(document.querySelectorAll('.monaco-editor')).filter(e => !isRO(e));
      const models = window.monaco.editor.getModels();
      if (eds.length && models.length) {
        const idx = eds.findIndex(visible);
        const model = models[Math.max(0, idx) % models.length] || models[0];
        model.setValue(code);
        return {ok: true, via: 'monaco.setValue'};
      }
    }
  } catch (e) {}

  // ② CodeMirror 6：走 EditorView.dispatch（内部 API，取不到就交给下一档）
  try {
    const cs = Array.from(document.querySelectorAll('.cm-content')).filter(e => !isRO(e));
    const c = cs.find(visible) || cs[0];
    if (c) {
      const cmv = c.cmView || (c.parentElement && c.parentElement.cmView);
      const view = cmv && (cmv.view || (cmv.rootView && cmv.rootView.view));
      if (view && view.dispatch && view.state && view.state.doc) {
        view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: code}});
        return {ok: true, via: 'cm6.dispatch'};
      }
    }
  } catch (e) {}

  // ③ CodeMirror 5
  try {
    const el = Array.from(document.querySelectorAll('.CodeMirror')).filter(e => !isRO(e))[0];
    if (el && el.CodeMirror) {
      el.CodeMirror.setValue(code);
      return {ok: true, via: 'cm5.setValue'};
    }
  } catch (e) {}

  // ④ 原生 textarea：用原型 setter + input 事件，绕过 React 受控组件
  try {
    const tas = Array.from(document.querySelectorAll('textarea')).filter(t => !isRO(t));
    const ta = tas.find(visible) || tas[0];
    if (ta) {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set;
      setter.call(ta, code);
      ta.dispatchEvent(new Event('input', {bubbles: true}));
      ta.dispatchEvent(new Event('change', {bubbles: true}));
      return {ok: true, via: 'textarea.setter'};
    }
  } catch (e) {}

  return {ok: false, via: '', why: '取不到编辑器 API（Monaco / CM6 / CM5 / textarea 都没有）'};
}
"""

# 读回校验用：不假设是哪一种编辑器，按优先级找"当前可见的那个"
EDITOR_READ_JS = r"""
() => {
  const isRO = e => ((e.parentElement && e.parentElement.className) || '').includes('readOnly');
  const visible = e => e && e.offsetParent !== null;

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
  if (!ce) return '';
  // 纯 contenteditable 里，<br> 和块级子元素（<div>/<p>）都代表换行。
  // 直接取 innerText 常把多行粘成一行（实测 "int main(){   return 0; }"），
  // 于是读回校验永远判"不一致"——用户看到的就是"明明写进去了却报失败"。
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
  return walkCE(ce);
}
"""


def _same_code(a: str, b: str) -> bool:
    """比较编辑器内容与目标代码（逐行，忽略空行与行尾空白）。

    不能直接比字符串：CodeMirror 的 ``insertText`` 会偶尔多插一个空行
    （实测 188 字符写进去变 189）。对编译毫无影响，所以忽略空行差异。

    另一个坑：contenteditable 会把**连续空格变成 NBSP**（``\u00a0``），
    于是缩进 4 空格读回来是 ``\xa0\xa0``——不归一化就会"明明写对了判失败"。
    """
    def norm(s: str) -> list[str]:
        text = (s or "").replace("\u00a0", " ").replace("\r\n", "\n")
        return [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    return norm(a) == norm(b)


def _same_code_loose(a: str, b: str) -> bool:
    """忽略**全部空白**比较：只判断"内容有没有丢"，不判断换行/缩进表示。

    只在严格比较失败后作为兜底：某些编辑器（纯 contenteditable）拿不到可靠的
    换行结构，但字符内容是对的。命中这一档会在结果里注明，让人知道"排版以
    编辑器为准"。
    """
    squeeze = lambda s: re.sub(r"\s+", "", (s or "").replace("\u00a0", " "))
    target = squeeze(b)
    return bool(target) and squeeze(a) == target


WRITE_METHODS = ("api", "insert", "exec", "type")

# 写入失败时的建议（真的遇到"禁止粘贴"的页面时，这段话比错误码有用）
WRITE_TIPS = """\
      这页很可能禁用了程序化写入（粘贴 / beforeinput 被拦，或者检测了事件来源）。
      可以按顺序试：
        1) --write-method type --typing-delay 10   逐键输入，最像真人（慢一点）
        2) apilot do browser_type text="..." method=type   走 agent 的键盘通道
        3) 仍然不行就手动粘贴/输入这一题，再跑 --submit --yes"""


def _editor_locator(page):
    return page.locator(".cm-content, .monaco-editor, .CodeMirror, textarea, "
                        "[contenteditable=true]").first


def _focus_editor(page) -> None:
    """点一下编辑器，确保后续键盘事件落到正确位置。"""
    try:
        _editor_locator(page).click(timeout=8000)
        page.wait_for_timeout(150)
    except Exception:
        pass


def _clear_editor(page) -> None:
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(120)


def _read_editor(page) -> str:
    try:
        return str(page.evaluate(EDITOR_READ_JS) or "")
    except Exception:
        return ""


def write_code(page, code: str, method: str = "auto", delay_ms: int = 0) -> dict:
    """把代码写进当前题目的编辑器，**每次写入都读回校验**，不通过就升级手段。

    为什么要阶梯：PTA 这类考试页会主动拦截粘贴（有的连 ``beforeinput`` 一起拦），
    单纯 execCommand / insertText 会出现"看起来写了、其实没写"。
    从"最不可能被拦"到"最像真人"依次尝试：

    ====== ============================================================
    api    Monaco ``model.setValue`` / CM6 ``view.dispatch`` / CM5 / textarea
           原型 setter —— **完全不产生输入事件**，粘贴拦截对它无效
    insert CDP ``Input.insertText`` —— 浏览器级文本插入，不是 paste 事件
    exec   ``document.execCommand('insertText')`` —— 历史路径，兼容性最好
    type   逐键输入 —— 最慢，但连"检测输入来源"的站点也拦不住
    ====== ============================================================

    返回 ``{"ok", "via", "tried": [(手段, 是否成功)], "detail"}``。
    """
    requested = str(method or "auto")
    order = WRITE_METHODS if requested == "auto" else (requested,)
    tried: list[tuple[str, bool]] = []

    if requested == "auto":
        _focus_editor(page)

    for name in order:
        detail = ""
        try:
            if name == "api":
                out = page.evaluate(WRITE_API_JS, code) or {}
                detail = str(out.get("why") or "")
                if not out.get("ok"):
                    tried.append((name, False))
                    continue
            elif name == "insert":
                _focus_editor(page)
                _clear_editor(page)
                page.keyboard.insert_text(code)
                page.wait_for_timeout(250)
            elif name == "exec":
                _focus_editor(page)
                page.evaluate(WRITE_JS, code)
                page.wait_for_timeout(250)
            elif name == "type":
                _focus_editor(page)
                _clear_editor(page)
                page.keyboard.type(code, delay=max(0, int(delay_ms)))
                page.wait_for_timeout(250)
            else:
                raise ValueError(f"未知的写入方式：{name}")
        except Exception as exc:      # 这一档炸了就换下一档，不中断整题
            tried.append((name, False))
            detail = f"{type(exc).__name__}: {exc}"[:120]
            continue

        # 读回校验：给编辑器两次机会（有的框架下一帧才同步）
        back = _read_editor(page)
        if not _same_code(back, code):
            page.wait_for_timeout(350)
            back = _read_editor(page)
        ok = _same_code(back, code)
        loose = False
        if not ok and _same_code_loose(back, code):
            ok, loose = True, True
        tried.append((name, ok))
        if ok:
            if loose:
                detail = "内容一致，但换行/缩进以编辑器显示为准（读回无法逐行比对）"
            return {"ok": True, "via": name, "tried": tried, "detail": detail}
        preview = (back or "").strip().replace("\n", " ")[:60]
        detail = f"读回不一致（编辑器里是 {preview!r}）"

    return {"ok": False, "via": "", "tried": tried,
            "detail": detail or "所有写入通道都没有生效"}


def _find_gcc() -> str:
    """找一个 C 编译器用来本地验证。"""
    import shutil as _shutil
    for name in ("gcc", "clang", "tcc"):
        found = _shutil.which(name)
        if found:
            return found
    for guess in (r"D:\code\tools\w64devkit\bin\gcc.exe",
                  r"C:\msys64\ucrt64\bin\gcc.exe",
                  r"C:\MinGW\bin\gcc.exe"):
        if Path(guess).is_file():
            return guess
    return ""


def _norm_out(text: str) -> str:
    """比较输出时忽略行尾空白和末尾空行（PTA 也是这么比的）。"""
    lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def cmd_verify(args) -> int:
    """本地编译 + 跑样例，把错误在提交前就抓出来。

    这一条是被真实教训逼出来的：7-2 本地样例两个都过，但 PTA 的
    "没有学生" 那个测试点要求**只输出一行**，模型按题面"输出格式"写了两行，
    提交后才判 部分正确 4/5。样例输出才是权威，必须逐字节对齐。
    """
    import subprocess as _sp
    import tempfile

    gcc = args.gcc or _find_gcc()
    if not gcc:
        print("找不到 C 编译器（gcc/clang/tcc）。用 --gcc 指定路径。")
        return 2

    data = read_json(OUT_DIR / "questions.json")
    ans_file = Path(args.answers) if args.answers else OUT_DIR / "answers.json"
    payload = read_json(ans_file)
    codes = {str(a.get("label")): a.get("code", "")
             for a in payload.get("answers", []) if a.get("type") == "program"}

    print(f"用 {gcc} 逐个编译并跑样例…\n")
    bad = []
    skipped = []
    for p in data["problems"]:
        label = p.get("label") or ""
        code = codes.get(label)
        samples = p.get("samples") or []
        if not code:
            skipped.append(label)
            continue
        if not samples:
            print(f"  ? {label:8} 没有样例，跳过")
            skipped.append(label)
            continue

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "m.c"
            exe = Path(td) / "m.exe"
            src.write_text(code, encoding="utf-8")
            cp = _sp.run([gcc, "-O0", "-o", str(exe), str(src)],
                         capture_output=True, text=True, errors="replace", timeout=60)
            if cp.returncode != 0:
                print(f"  ✗ {label:8} 编译失败")
                print("      " + (cp.stderr or "").strip().splitlines()[-1][:110])
                bad.append((label, "编译失败"))
                continue

            all_ok = True
            for i, s in enumerate(samples, 1):
                try:
                    rp = _sp.run([str(exe)], input=s.get("in", ""), capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=15)
                except _sp.TimeoutExpired:
                    print(f"  ✗ {label:8} 样例{i} 运行超时")
                    all_ok = False
                    break
                got, want = _norm_out(rp.stdout), _norm_out(s.get("out", ""))
                if got != want:
                    all_ok = False
                    print(f"  ✗ {label:8} 样例{i} 输出不符")
                    print(f"      输入: {s.get('in','')!r}")
                    print(f"      期望: {want!r}")
                    print(f"      实际: {got!r}")
                    break
            if all_ok:
                print(f"  ✓ {label:8} {p.get('name','')[:30]:32} {len(samples)} 组样例全过")

        if not all_ok:
            bad.append((label, "样例不过"))

    print()
    if skipped:
        print(f"跳过（没有样例或没有代码）：{skipped}")
    if bad:
        print(f"\n有问题的题：{[b[0] for b in bad]}")
        print("改好 var/pta/answers.json 里的 code 再跑一次 --verify。")
        return 1
    print("全部通过，可以 --fill 了。")
    return 0


def fill_program(page, answers: list[dict], args) -> int:
    """逐题翻页 + 写代码。不提交。"""
    dry = bool(getattr(args, "dry_run", False))
    method = str(getattr(args, "write_method", "auto") or "auto")
    delay_ms = int(getattr(args, "typing_delay", 0) or 0)
    by_label = {str(a.get("label")): a for a in answers}
    navs = nav_items(page)
    if not navs:
        print("找不到题号导航。")
        return 1

    print(f"{'演练（不写）' if dry else '写入'} {len(navs)} 道编程题…"
          f"{'' if dry else f'（写入方式：{method}）'}")
    wrote = 0
    failed = []
    for item in navs:
        goto_problem(page, item["href"])
        info = page.evaluate(READ_JS)
        label = info.get("label") or ""
        code_item = by_label.get(label)
        if not code_item or not code_item.get("code"):
            print(f"  · {label or item['n']:8} 没有对应代码，跳过")
            continue
        if dry:
            print(f"  · {label:8} 将写入 {len(code_item['code'])} 字符 "
                  f"（{info.get('name', '')[:30]}）")
            wrote += 1
            continue
        out = write_code(page, code_item["code"], method=method, delay_ms=delay_ms)
        if out["ok"]:
            chain = "/".join(f"{name}{'✓' if ok else '✗'}" for name, ok in out["tried"])
            print(f"  ✓ {label:8} {info.get('name', '')[:26]:28}"
                  f" {len(code_item['code']):>4} 字符  读回一致"
                  f"（{out['via']}；尝试 {chain or '-'}）")
            wrote += 1
            continue
        print(f"  ✗ {label:8} {info.get('name', '')[:26]:28} 写入失败：{out['detail']}")
        print(WRITE_TIPS)
        failed.append(label)

    print(f"\n完成 {wrote}/{len(navs)} 题。**没有提交**。")
    if failed:
        print(f"有问题的题：{failed}")
    print("提交要用页面上的「提交本题作答」按钮、逐题提交——脚本不代劳这一步。")
    return 0 if wrote == len(navs) else 1


# --------------------------------------------------------------------------
# --solve：只调一次模型做"判断"，不碰屏幕
#
# 这是整个脚本里唯一花 token 的地方，而且便宜得离谱：
#   Agent 方式：30 步 × 每步重发系统提示词+59个工具schema+屏幕快照 ≈ 30 万+ token
#   这里：      题目文本（几千 token）进、答案 JSON（几百 token）出，**一次调用**
# 因为"答题"本来就是纯文本任务，根本不需要看屏幕、不需要工具、不需要试错。
# --------------------------------------------------------------------------

SOLVE_SYSTEM = """你是 C 语言考试答题助手。用户会给你若干道单选题，你只需要给出答案。

严格只输出一个 JSON 对象，不要任何解释性文字、不要 markdown 代码块，格式：
{"answers": [{"label": "题号", "letter": "B", "index": 1, "reason": "一句话理由"}]}

**letter 和 index 必须指向同一个选项**：index 从 0 开始（A=0、B=1、C=2、D=3）。
两个都写出来，程序会交叉校验；只写一个也能跑，但写两个能少一类"错一位"的事故。

注意 C 语言的常见陷阱：运算符优先级、赋值 vs 相等（= 与 ==）、
短路求值、if-else 配对、switch 穿透、自增自减的前后缀、整数除法。
如果题目里的代码有语法错误导致无法运行，如实选择对应选项。

题干里的数学公式已经被转成普通文本（√ 表示根号、^ 表示乘方、/ 表示分数），
形状为〔图N: …〕的片段是题面里的图片被识别后的内容，请当作题面的一部分。
看不清的地方（写了 [?]）不要猜，按最合理的理解作答并在 reason 里说明。"""


def _problem_text(problem: dict, budget: int = 2000) -> str:
    """一题的完整文本：题干全文（含公式识别结果）+ 选项。

    早期实现只给 ``head``（前 260 字符）——长题的关键条件全被切掉，
    那才是答错的主要原因，比模型的推理能力影响大得多。
    """
    if problem.get("blocks"):
        return quizread.compose_problem(problem, budget=budget)
    return str(problem.get("head") or problem.get("text") or "")[:budget]


def build_solve_prompt(data: dict, budget: int = 2000) -> str:
    problems = [p for p in data["problems"]
                if p.get("kind") == "choice" and p.get("options")]
    lines = [f"共 {len(problems)} 道单选题，请逐题作答。", ""]
    for p in problems:
        lines.append(f"【{p['label']}】")
        lines.append(_problem_text(p, budget))
        lines.append("")
    return "\n".join(lines)


def build_program_prompt(data: dict, budget: int = 2600) -> str:
    lines = [f"共 {data['count']} 道编程题。", ""]
    for p in data["problems"]:
        lines.append(f"【{p.get('label')}】{p.get('name', '')}")
        lines.append(_problem_text(p, budget))
        for i, sample in enumerate(p.get("samples") or [], 1):
            lines.append(f"样例{i} 输入：{sample.get('in')}")
            lines.append(f"样例{i} 输出：{sample.get('out')}")
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 答案规范化：把模型的各种写法落成确定的选项下标
# --------------------------------------------------------------------------


def _pick_index(item: dict, count: int) -> tuple[int | None, str]:
    """从模型的一条答案里取选项下标：**字母优先**，再与 index 交叉校验。

    为什么字母优先：``A/B/C`` 没有"0 还是 1 起始"的歧义，而纯数字有。
    提示词里已经要求两个都写、并且 index 从 0 开始；这一层是防止模型不听话——
    两者指向不同选项时按字母取，并把分歧写进告警（而不是默默采信一个）。
    """
    letter = item.get("letter", item.get("answer_letter"))
    raw = item.get("index", item.get("answer", item.get("choice")))

    by_letter: int | None = None
    letter_note = ""
    if letter not in (None, ""):
        by_letter, letter_note = quizkit.normalize_index(letter, count)

    by_index: int | None = None
    index_note = ""
    if raw not in (None, ""):
        by_index, index_note = quizkit.normalize_index(raw, count)

    if by_letter is None:
        return by_index, index_note or letter_note
    if by_index is not None and by_index != by_letter:
        return by_letter, (f"letter={letter!r} 与 index={raw!r} 指向不同选项，"
                           f"按 letter 取 {by_letter}")
    return by_letter, ""


def normalize_answers(raw_answers: list[dict], problems: list[dict]
                      ) -> tuple[list[dict], list[str]]:
    """把模型返回的答案变成 ``answers.json`` 的结构，并收集需要人看的告警。

    模型对"第几个选项"的写法五花八门（``0``/``1``/``A``/``（B）``/整段文字），
    这里统一解析；解析不出来时**保留 None 并告警**，绝不瞎填一个下标——
    填错一位的代价是整题错，而留空至少能让人一眼看出来。
    """
    by_label = {str(p.get("label")): p for p in problems}
    out: list[dict] = []
    warnings: list[str] = []
    for item in raw_answers or []:
        label = str(item.get("label", "")).strip()
        problem = by_label.get(label)
        if problem is None:
            warnings.append(f"模型给了不存在的题号 {label!r}，已忽略")
            continue
        if problem.get("kind") == "program" or item.get("type") == "program":
            code = str(item.get("code") or item.get("answer") or "")
            out.append({"label": label, "type": "program", "code": code})
            continue
        options = problem.get("options") or []
        index, note = _pick_index(item, len(options))
        if index is None and item.get("text"):
            index, note = quizkit.match_option(options, item.get("text"))
        if index is None:
            warnings.append(f"{label}: {note or '答案无法解析'}")
        elif note:
            warnings.append(f"{label}: {note}")
        record = {"label": label, "type": "choice", "index": index,
                  "reason": str(item.get("reason") or "")[:200]}
        if isinstance(index, int):
            record["letter"] = chr(ord("A") + index)   # 方便人眼核对，--fill 不读它
        out.append(record)
    return out, warnings


def parse_solve_output(text: str) -> list[dict]:
    """从模型回复里抠出 JSON（容忍它包了 markdown 代码块）。"""
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1] if "```" in raw[3:] else raw[3:]
        raw = raw.removeprefix("json").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < 0:
        raise ValueError(f"模型没有返回 JSON：{text[:200]}")
    payload = json.loads(raw[start:end + 1])
    answers = payload.get("answers", payload if isinstance(payload, list) else [])
    if not answers:
        raise ValueError(f"模型返回的 JSON 里没有 answers：{text[:200]}")
    return answers


REVIEW_SYSTEM = """你是一位严谨的阅卷老师，正在**独立复核**一批 C 语言单选题。

必须严格按下面的顺序，不要跳步：
1. 先完全独立地把每道题解一遍（不要看"待复核答案"那一段的内容），
   把关键推理写在心里：运算符优先级、赋值 vs 相等、短路求值、
   if-else 配对、switch 穿透、前后缀自增、整数除法、字符串结尾 '\\0' 等。
2. 得出你自己认为正确的选项：写出选项字母（A/B/C/D）和下标（**从 0 开始**，A=0）。
3. 再和"待复核答案"逐题对比，标出是否一致。

严格只输出一个 JSON 对象，不要解释性文字、不要 markdown 代码块，格式：
{"answers": [{"label": "题号", "letter": "B", "index": 1, "agree": true/false, "note": "不一致时一句话说明对方错在哪"}]}"""


def _make_client(model: str = "", temperature: float = 0.0, max_tokens: int = 4096,
                 extra: dict | None = None):
    from autopilot.llm import (LLMClient, LLMConfig, resolve_api_key, resolve_base_url,
                               resolve_model)

    cfg = LLMConfig(
        model=model or resolve_model(ROOT),
        base_url=resolve_base_url(ROOT),
        api_key=resolve_api_key(ROOT),
        temperature=temperature,
        max_tokens=max_tokens,
        extra_body=dict(extra or {}),
    ).resolved()
    return cfg, LLMClient(cfg, workspace=ROOT)


def _chat_with_retry(cfg, messages: list[dict], max_tokens: int = 4096,
                     temperature: float = 0.0):
    """发一轮对话；**内容为空时关掉思考模式重试一次**。

    实测坑：deepseek 系列默认开思考，思维链按 completion token 计费。
    ``max_tokens`` 给小了（比如单题 1024）会出现"推理把额度吃光、``content``
    返回空字符串"，上层看到的就是「模型没有返回 JSON：（空）」——
    一次任务白跑。关掉思考重发几乎必定拿到答案。
    """
    import dataclasses

    from autopilot.llm import LLMClient

    response = LLMClient(cfg, workspace=ROOT).chat(
        messages, temperature=temperature, max_tokens=max_tokens)
    if (response.content or "").strip():
        return response
    retry_cfg = dataclasses.replace(
        cfg, extra_body={**(cfg.extra_body or {}), "thinking": {"type": "disabled"}})
    return LLMClient(retry_cfg, workspace=ROOT).chat(
        messages, temperature=temperature, max_tokens=max_tokens)


def review_choice_answers(data: dict, answers: list[dict], model: str = "") -> tuple[dict, int]:
    """让更强的模型**先独立重做**一遍，再和初答对比。一次调用解决整卷。

    为什么先独立做、再对比：反过来问"这个答案对吗"，它会顺着你说"对"，
    复核就白做了（``autopilot.llm.review`` 里踩过这个坑，这里是批量版）。

    返回 ``({label: index}, 未命中题数)``；调用失败时返回空字典，不阻塞流程。
    """
    from autopilot.llm import REVIEW_MODEL

    targets = [a for a in answers
               if a.get("type") == "choice" and isinstance(a.get("index"), int)]
    if not targets:
        return {}, 0
    mine = "\n".join(f"  {a['label']}: [{a['index']}]" for a in targets)
    prompt = (build_solve_prompt(data, budget=2000)
              + "\n\n# 待复核答案（第 3 步之前不要看它）\n" + mine)

    messages = [{"role": "system", "content": REVIEW_SYSTEM},
                {"role": "user", "content": prompt}]
    try:
        cfg, client = _make_client(model or REVIEW_MODEL, max_tokens=8000,
                                   extra={"reasoning_effort": "low"})
        try:
            resp = _chat_with_retry(cfg, messages, max_tokens=8000)
        except Exception as exc:
            # 拿不到更强的模型就退回默认模型：复核弱一点，总比不复核强
            print(f"  复核模型不可用（{str(exc)[:80]}），退回默认模型")
            cfg, client = _make_client(model, max_tokens=8000)
            resp = _chat_with_retry(cfg, messages, max_tokens=8000)
    except Exception as exc:
        print(f"  ⚠ 复核失败：{str(exc)[:120]}")
        return {}, len(targets)

    print(f"  复核用模型：{cfg.model}，输入 {(resp.usage or {}).get('prompt_tokens', 0)}"
          f" / 输出 {(resp.usage or {}).get('completion_tokens', 0)} tokens")
    try:
        raw = parse_solve_output(resp.content)
    except Exception as exc:
        print(f"  ⚠ 复核结果解析失败：{exc}")
        return {}, len(targets)

    reviewed = {}
    for item in raw:
        label = str(item.get("label", "")).strip()
        problem = next((p for p in data["problems"] if str(p.get("label")) == label), None)
        if problem is None:
            continue
        index, _ = _pick_index(item, len(problem.get("options") or []))
        if index is not None:
            reviewed[label] = index
    missing = len([a for a in targets if a["label"] not in reviewed])
    return reviewed, missing


def cmd_solve(args) -> int:
    qfile = OUT_DIR / "questions.json"
    if not qfile.is_file():
        print(f"找不到 {qfile}\n先跑一次 --dump。")
        return 2
    data = read_json(qfile)

    choice_todo = [p for p in data["problems"]
                   if p.get("kind") == "choice" and p.get("options")]
    program_todo = [p for p in data["problems"] if p.get("kind") == "program"]

    if not choice_todo and not program_todo:
        print("导出的题目里没有可作答的题。")
        return 2

    cfg, client = _make_client(args.model, temperature=0.0)
    if not cfg.api_key:
        print("没有配置 API 密钥，无法 --solve。见 README 的「配置模型」。")
        return 2

    answers: list[dict] = []
    total_in = total_out = 0

    # 一次调用解决所有题目；编程题代码量大，给更宽的 max_tokens
    if choice_todo:
        print(f"把 {len(choice_todo)} 道单选题发给 {cfg.model}（一次调用，题干全文）…")
        resp = _chat_with_retry(cfg, [
            {"role": "system", "content": SOLVE_SYSTEM},
            {"role": "user", "content": build_solve_prompt(data)}], max_tokens=4096)
        total_in += (resp.usage or {}).get("prompt_tokens", 0)
        total_out += (resp.usage or {}).get("completion_tokens", 0)
        try:
            answers += parse_solve_output(resp.content)
        except Exception as exc:
            print(f"单选题解析失败：{exc}\n{resp.content[:400]}")
            return 1
        print(f"  {len(answers)} 题，输入 {(resp.usage or {}).get('prompt_tokens', 0)} "
              f"/ 输出 {(resp.usage or {}).get('completion_tokens', 0)} tokens")

    if program_todo:
        print(f"把 {len(program_todo)} 道编程题发给 {cfg.model}（一次调用，代码量大）…")
        resp = _chat_with_retry(cfg, [
            {"role": "system", "content": PROGRAM_SOLVE_SYSTEM},
            {"role": "user", "content": build_program_prompt(data)}], max_tokens=16000)
        total_in += (resp.usage or {}).get("prompt_tokens", 0)
        total_out += (resp.usage or {}).get("completion_tokens", 0)
        try:
            prog = parse_solve_output(resp.content)
        except Exception as exc:
            print(f"编程题解析失败：{exc}\n{resp.content[:600]}")
            return 1
        for item in prog:
            item["type"] = "program"
        answers += prog
        print(f"  {len(prog)} 题，输入 {(resp.usage or {}).get('prompt_tokens', 0)} "
              f"/ 输出 {(resp.usage or {}).get('completion_tokens', 0)} tokens")

    print(f"合计：输入 {total_in} / 输出 {total_out} tokens")

    out, warnings = normalize_answers(answers, data["problems"])
    for warning in warnings:
        print(f"  ⚠ {warning}")

    # 批量复核：只对选择题做（编程题有 --verify 的编译校验，那个更靠得住）
    if getattr(args, "check", True) and any(a["type"] == "choice" for a in out):
        print("\n独立复核（用更强的模型重做一遍再对比，一次调用）…")
        reviewed, missing = review_choice_answers(data, out, args.check_model)
        if reviewed:
            conflicts = 0
            for answer in out:
                if answer["type"] != "choice":
                    continue
                checked = reviewed.get(answer["label"])
                if checked is None:
                    continue
                answer["checked"] = checked
                if checked != answer.get("index"):
                    answer["conflict"] = True
                    conflicts += 1
            print(f"  复核完成：{len(reviewed)} 题一致，⚠ {conflicts} 题有分歧"
                  f"（写上 checked 字段，--fill 默认采用复核答案）")
        if missing:
            print(f"  ⚠ 有 {missing} 题没能复核（模型漏答）")

    by_label = {str(p["label"]): p for p in data["problems"]}
    print("\n最终答案：")
    for answer in out:
        label = answer["label"]
        problem = by_label.get(label) or {}
        if answer["type"] == "program":
            code = str(answer.get("code", ""))
            first = code.strip().splitlines()[0] if code.strip() else "(空)"
            print(f"  {label:8} [代码 {len(code):>4} 字符] {first[:60]}")
            continue
        options = problem.get("options") or []
        index = answer.get("index")
        picked = options[index]["text"] if isinstance(index, int) and 0 <= index < len(options) else "(未确定)"
        mark = "⚠" if answer.get("conflict") else " "
        print(f" {mark}{label:8} [{index}] {str(picked)[:66]}")
        if answer.get("reason"):
            print(f"           理由：{answer['reason'][:100]}")
        if answer.get("conflict"):
            checked = answer.get("checked")
            other = options[checked]["text"] if isinstance(checked, int) and 0 <= checked < len(options) else "?"
            print(f"           ⚠ 复核认为应选 [{checked}] {str(other)[:60]}")

    # 补全模型漏掉的题，保持文件结构完整
    got = {a["label"] for a in out}
    for problem in data["problems"]:
        label = str(problem.get("label"))
        if label not in got:
            out.append({"label": label, "type": problem.get("kind", "choice"),
                        **({"code": ""} if problem.get("kind") == "program"
                           else {"index": None})})

    ans_file = Path(args.answers) if args.answers else OUT_DIR / "answers.json"
    if ans_file.is_file() and not args.force:
        backup = ans_file.with_suffix(".json.bak")
        backup.write_text(ans_file.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"\n已备份原答案文件到 {backup.name}")
    write_json(ans_file, {
        "_说明": "由 --solve 生成；conflict=true 表示复核有分歧，"
                 "checked 是复核答案。想手改就直接改 index/text/code",
        "answers": out,
    })
    print(f"\n已写入 {ans_file}")
    print("下一步：python examples/pta-auto.py --fill --dry-run  先演练一遍")
    return 0


def cmd_fill(page, args) -> int:
    ans_file = Path(args.answers) if args.answers else OUT_DIR / "answers.json"
    if not ans_file.is_file():
        print(f"找不到答案文件：{ans_file}\n先跑一次 --dump 生成模板。")
        return 2
    try:
        payload = read_json(ans_file)
    except json.JSONDecodeError as exc:
        print(f"答案文件不是合法 JSON：{ans_file}\n  {exc}")
        return 2
    answers = payload.get("answers", payload if isinstance(payload, list) else [])

    # 编程题是单题视图，逐题翻页 + 写代码，跟单选题完全不是一套流程
    if is_program_page(page):
        programs = [a for a in answers if a.get("type") == "program" and a.get("code")]
        if not programs:
            print(f"{ans_file} 里没有可写入的编程题代码（code 字段都是空的）。")
            return 2
        return fill_program(page, answers, args)

    choices = []
    conflicts = []
    undefined = []
    trust = str(getattr(args, "trust", "check") or "check")
    for answer in answers:
        if answer.get("type") != "choice":
            continue
        index = answer.get("index")
        checked = answer.get("checked")
        # 复核有分歧的题：默认采用**复核答案**（更强模型独立重做的那一份）
        if answer.get("conflict") and isinstance(checked, int):
            conflicts.append((answer.get("label"), index, checked))
            if trust == "check":
                index = checked
        if index is None and not answer.get("text"):
            undefined.append(answer.get("label"))
            continue
        choices.append({**answer, "index": index})
    programs = [a for a in answers if a.get("type") == "program" and a.get("code")]

    for label, first, checked in conflicts:
        print(f"  ⚠ {label}: 初答 [{first}] 与复核 [{checked}] 不一致，"
              f"按 --trust={trust} 采用 [{checked if trust == 'check' else first}]")
    if undefined:
        print(f"  ⚠ 这些题没有确定的答案，已跳过：{undefined}")

    if not choices and not programs:
        print(f"{ans_file} 里还没填任何答案（index / text / code 都是空的）。")
        return 2

    if choices:
        mode = "演练（不真的点）" if args.dry_run else "填入"
        print(f"{mode} {len(choices)} 道选择题…")
        res = page.evaluate(FILL_JS, {"dry": bool(args.dry_run), "items": choices})
        for d in res["done"]:
            print(f"  {'·' if args.dry_run else '✓'} {d['label']} -> {d['picked'][:60]}")
        for m in res["missed"]:
            print(f"  ✗ {m['label']}: {m['why']}")
        if res["missed"] and len(res["missed"]) == len(choices):
            print(f"\n一道都没匹配上。当前页面是：{page.url}")
            print("  多半是停在了别的题目集/题型上（比如编程题页 type/7 没有单选 radio）。")
            print(f"  目标页面是：{args.url}")
            print("  用 --url 指定正确地址，或先在浏览器里切到对应的题型标签页。")
            return 1

    if programs and not args.dry_run:
        print(f"填入 {len(programs)} 道编程题…")
        res = page.evaluate(FILL_PROGRAM_JS, programs)
        for d in res["done"]:
            print(f"  ✓ {d['label']} (via {d['via']})")
        for m in res["missed"]:
            print(f"  ✗ {m['label']}: {m['why']}")

    if args.dry_run:
        print("\n演练结束：只解析了目标，没有改动页面上任何东西。")
        return 0
    print("\n填完了，**没有提交**。请自己看一眼页面确认，再跑 --submit --yes。")
    return 0


def cmd_submit(page, args) -> int:
    if not args.yes:
        print("拒绝执行：提交不可逆，必须显式加 --yes。")
        print("  用法：python examples/pta-auto.py --submit --yes")
        return 2
    print("查找提交按钮…")
    res = page.evaluate(SUBMIT_JS)
    if not res.get("ok"):
        print("没找到提交按钮。页面上的按钮是：")
        for b in res.get("seen", []):
            print("   ", b)
        return 1
    print(f"已点击「{res['clicked']}」。")
    print("如果有二次确认弹窗，请你手动确认——脚本不代劳这一步。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="PTA 作业自动化（0 token，不调用模型）")
    ap.add_argument("--url", default=DEFAULT_URL, help="考试页面地址")
    ap.add_argument("--dump", action="store_true", help="导出题目结构")
    ap.add_argument("--fill", action="store_true", help="按 answers.json 填入答案")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --fill：只解析要填哪个选项，不真的改动页面")
    ap.add_argument("--submit", action="store_true", help="提交（需再加 --yes）")
    ap.add_argument("--yes", action="store_true", help="确认执行提交")
    ap.add_argument("--answers", default="", help="答案文件路径")
    ap.add_argument("--headless", action="store_true", help="无头模式")
    ap.add_argument("--login-timeout", type=float, default=300.0,
                    help="等待手动登录的秒数")
    ap.add_argument("--solve", action="store_true",
                    help="把导出的题目发给模型答一次（唯一花 token 的步骤）")
    ap.add_argument("--verify", action="store_true",
                    help="本地编译并跑样例，提前发现错误（需要 gcc）")
    ap.add_argument("--gcc", default="", help="指定 C 编译器路径")
    ap.add_argument("--model", default="", help="配合 --solve 指定模型")
    ap.add_argument("--jobs", type=int, default=4,
                    help="读图并发数（一题一次视觉调用，默认 4）")
    ap.add_argument("--write-method", choices=["auto", *WRITE_METHODS], default="auto",
                    help="代码写入方式：auto 逐级降级（默认）/ api 框架API / "
                         "insert CDP插入 / exec execCommand / type 逐键")
    ap.add_argument("--typing-delay", type=int, default=0,
                    help="配合 --write-method type：每个字符之间的毫秒数（被判定输入过快时调大）")
    ap.add_argument("--read-prompt", default="",
                    help="覆盖读图时给视觉模型的额外要求")
    ap.add_argument("--check", dest="check", action="store_true", default=True,
                    help="--solve 后额外做一次独立复核（默认开启，一次调用）")
    ap.add_argument("--no-check", dest="check", action="store_false",
                    help="关闭独立复核，省一次调用")
    ap.add_argument("--check-model", default="",
                    help="复核用的模型（默认更強的 REVIEW_MODEL）")
    ap.add_argument("--trust", choices=["check", "first"], default="check",
                    help="复核有分歧时以哪份答案为准（默认 check）")
    ap.add_argument("--force", action="store_true", help="覆盖已有答案文件时不备份")
    args = ap.parse_args()

    if not any([args.dump, args.fill, args.submit, args.solve, args.verify]):
        ap.print_help()
        return 2

    # --solve / --verify 都是纯本地任务，不需要浏览器
    if args.solve:
        return cmd_solve(args)
    if args.verify:
        return cmd_verify(args)

    pw, page = ensure_browser(args.url, headless=args.headless)
    try:
        goto_target(page, args.url)
        if is_logged_out(page):
            wait_for_login(page, args.url, timeout=args.login_timeout)

        if args.dump:
            return cmd_dump(page, args)
        if args.fill:
            return cmd_fill(page, args)
        if args.submit:
            return cmd_submit(page, args)
        return 0
    finally:
        pw.stop()      # 只断开连接，不关浏览器（登录态是会话级的）


if __name__ == "__main__":
    raise SystemExit(main())
