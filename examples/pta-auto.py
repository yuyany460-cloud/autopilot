# -*- coding: utf-8 -*-
"""PTA 作业自动化 —— 全程 0 token（不调用任何模型）。

为什么要有这个脚本：
    Agent（``apilot run``）每一步都要调一次大模型，30 步就是几十万 token。
    但"点哪里、填什么"本身**不需要智能**——需要智能的只是"答案是什么"，
    而那个只需要确定一次。把两者拆开后，执行部分就能完全脚本化。

用法（在项目根目录下执行）::

    python examples/pta-auto.py --dump          # 导出题目到 var/pta/
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

from autopilot.actions import _browser_exe, _cdp_ready          # noqa: E402

PROFILE = ROOT / "var" / "browser-profile"
OUT_DIR = ROOT / "var" / "pta"
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

DUMP_JS = r"""
() => {
  const txt = e => (e && (e.innerText || e.textContent) || '').trim();
  const numId = e => e && e.id && /^\d+$/.test(e.id) ? e.id : null;

  const optionText = r => {
    let t = '';
    if (r.id) {
      const l = document.querySelector(`label[for="${CSS.escape(r.id)}"]`);
      if (l) t = txt(l);
    }
    if (!t && r.closest('label')) t = txt(r.closest('label'));
    if (!t) t = txt(r.parentElement);
    return t.replace(/\s+/g, ' ').slice(0, 300);
  };

  const problems = [];

  // ---- 单选题：按 radio 分组 ----
  const radios = Array.from(document.querySelectorAll('input[type=radio]'));
  const groups = {};
  radios.forEach(r => { (groups[r.name] = groups[r.name] || []).push(r); });
  for (const name of Object.keys(groups)) {
    const rs = groups[name];
    let box = rs[0];
    while (box && !numId(box)) box = box.parentElement;
    const full = box ? txt(box) : '';
    problems.push({
      label: (full.match(/^(\d+-\d+)/) || [, ''])[1],
      id: name,
      kind: 'choice',
      head: full.slice(0, 260).replace(/\s+/g, ' '),
      options: rs.map((r, i) => ({index: i, text: optionText(r), id: r.id || ''})),
    });
  }

  // ---- 编程题：带数字 id 且挂了编辑器的容器 ----
  const editorSel = '.monaco-editor, .CodeMirror, textarea, [contenteditable=true]';
  const seen = new Set(problems.map(p => p.id));
  Array.from(document.querySelectorAll('div[id]')).forEach(box => {
    const id = numId(box);
    if (!id || seen.has(id)) return;
    if (!box.querySelector(editorSel)) return;
    const full = txt(box);
    seen.add(id);
    problems.push({
      label: (full.match(/^(\d+-\d+)/) || [, ''])[1],
      id,
      kind: 'program',
      head: full.slice(0, 260).replace(/\s+/g, ' '),
      editors: {
        monaco: box.querySelectorAll('.monaco-editor').length,
        codemirror: box.querySelectorAll('.CodeMirror').length,
        textarea: box.querySelectorAll('textarea').length,
        contenteditable: box.querySelectorAll('[contenteditable=true]').length,
      },
    });
  });

  // 按题号排序，读起来顺
  problems.sort((a, b) => a.label.localeCompare(b.label, 'zh', {numeric: true}));

  return {
    url: location.href,
    title: document.title,
    count: problems.length,
    problems,
    pageButtons: Array.from(document.querySelectorAll('button'))
                      .map(txt).filter(Boolean).slice(0, 40),
  };
}
"""

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


def goto_problem(page, href: str, settle: int = 2500) -> None:
    """按 href 直接导航到某道题。

    最早是模拟点击题号，但 SPA 的切换时序很不稳定（实测点了 1~9 都停在原题），
    而这些 ``<a>`` 本来就带 href，直接 goto 又准又快。
    """
    url = href if href.startswith("http") else "https://pintia.cn" + href
    page.goto(url, wait_until="domcontentloaded", timeout=90000)
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


def cmd_dump(page, args) -> int:
    # 编程题是单题视图，结构跟单选题页完全不同，先分流
    if is_program_page(page):
        return dump_program(page, args)

    print("抓取题目…")
    data = page.evaluate(DUMP_JS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "questions.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [f"# {data['title']}", f"# {data['url']}", f"# 共 {data['count']} 题", ""]
    for p in data["problems"]:
        lines.append(f"## {p['label']}   [{p['kind']}]  (id={p['id']})")
        lines.append(f"   {p['head'][:300]}")
        for o in p.get("options", []):
            lines.append(f"     [{o['index']}] {o['text']}")
        if p.get("editors"):
            lines.append(f"     编辑器: {p['editors']}")
        lines.append("")
    lines.append("页面级按钮: " + ", ".join(data["pageButtons"]))
    (OUT_DIR / "questions.txt").write_text("\n".join(lines), encoding="utf-8")

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
    print("  questions.txt   （人看的）")
    print("  questions.json  （机器读的）")
    print("  answers.json    ← 把答案填这里")
    print("\n题目概览：")
    for p in data["problems"]:
        n = len(p.get("options", []))
        print(f"  {p['label']:6} {p['kind']:8} {'选项 '+str(n) if n else '编辑器'}  "
              f"{p['head'][:64]}")
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
    """编程题页一次只显示一道题，按 href 逐题翻。"""
    navs = nav_items(page)
    if not navs:
        print("找不到题号导航，无法逐题抓取。")
        return 1
    print(f"检测到编程题页，共 {len(navs)} 题，逐题抓取中…")

    problems = []
    for item in navs:
        goto_problem(page, item["href"])
        info = page.evaluate(READ_JS)
        info["index"] = item["n"]
        info["kind"] = "program"
        info["href"] = item["href"]
        problems.append(info)
        print(f"  {info.get('label') or item['n']:8} "
              f"{info.get('name', '')[:42]}  样例 {len(info.get('samples') or [])} 组")

    data = {"url": page.url, "title": page.title(), "count": len(problems),
            "problems": problems, "pageButtons": []}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    write_json(OUT_DIR / "questions.json", data)

    lines = [f"# {data['title']}", f"# {data['url']}", f"# 共 {data['count']} 题", ""]
    for p in problems:
        lines.append(f"## {p.get('label')}  {p.get('name', '')}")
        lines.append(p.get("text", ""))
        for i, s in enumerate(p.get("samples") or [], 1):
            lines.append(f"   样例{i} 输入: {s['in']!r}")
            lines.append(f"   样例{i} 输出: {s['out']!r}")
        lines.append("")
    (OUT_DIR / "questions.txt").write_text("\n".join(lines), encoding="utf-8")

    ans_file = OUT_DIR / "answers.json"
    if not ans_file.exists() or args.force:
        write_json(ans_file, {
            "_说明": "program 题填 code；先跑 --solve 可以让模型生成",
            "answers": [{"label": p.get("label"), "type": "program", "code": ""}
                        for p in problems],
        })
    print(f"\n已导出到 {OUT_DIR}")
    print("  下一步：python examples/pta-auto.py --solve")
    return 0


PROGRAM_SOLVE_SYSTEM = """你是 C 语言程序设计助教。用户会给你若干道编程题，请为每道题写出完整的 C 语言程序。

严格只输出一个 JSON 对象，不要解释、不要 markdown 代码块，格式：
{"answers": [{"label": "7-1", "code": "#include <stdio.h>\\nint main(){...}"}]}

要求：
- 程序必须完整、可直接编译运行（含 #include <stdio.h> 和 int main）。
- 严格遵守题目的输入格式与输出格式，**输出格式一个空格都不能差**（很多题会因为这个判错）。
- 用 double 处理实数，按题目要求控制小数位数。
- 代码放在 code 字段里，用 \\n 表示换行。"""


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


def _same_code(a: str, b: str) -> bool:
    """比较编辑器内容与目标代码。

    不能直接比字符串：CodeMirror 的 ``insertText`` 会偶尔多插一个空行
    （实测 188 字符写进去变 189）。对编译毫无影响，所以忽略空行差异。
    """
    def norm(s: str) -> list[str]:
        return [ln.rstrip() for ln in (s or "").strip().splitlines() if ln.strip()]
    return norm(a) == norm(b)


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


def fill_program(page, answers: list[dict], dry: bool) -> int:
    """逐题翻页 + 写代码。不提交。"""
    by_label = {str(a.get("label")): a for a in answers}
    navs = nav_items(page)
    if not navs:
        print("找不到题号导航。")
        return 1

    print(f"{'演练（不写）' if dry else '写入'} {len(navs)} 道编程题…")
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
        res = page.evaluate(WRITE_JS, code_item["code"])
        if not res.get("ok"):
            print(f"  ✗ {label}: {res.get('why')}")
            failed.append(label)
            continue
        page.wait_for_timeout(500)
        back = page.evaluate(READ_CODE_JS) or ""
        ok = _same_code(back, code_item["code"])
        print(f"  {'✓' if ok else '✗'} {label:8} {info.get('name', '')[:26]:28}"
              f" {len(code_item['code']):>4} 字符{'  读回一致' if ok else '  读回不一致'}")
        wrote += 1 if ok else 0
        if not ok:
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
{"answers": [{"label": "题号", "index": 选项序号(从0开始), "reason": "一句话理由"}]}

注意 C 语言的常见陷阱：运算符优先级、赋值 vs 相等（= 与 ==）、
短路求值、if-else 配对、switch 穿透、自增自减的前后缀、整数除法。
如果题目里的代码有语法错误导致无法运行，如实选择对应选项。"""


def build_solve_prompt(data: dict) -> str:
    lines = [f"共 {data['count']} 道单选题，请逐题作答。", ""]
    for p in data["problems"]:
        if p.get("kind") != "choice" or not p.get("options"):
            continue
        lines.append(f"【{p['label']}】{p['head']}")
        for o in p["options"]:
            lines.append(f"  [{o['index']}] {o['text']}")
        lines.append("")
    return "\n".join(lines)


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

    from autopilot.llm import LLMClient, LLMConfig, resolve_api_key, resolve_base_url, resolve_model

    cfg = LLMConfig(
        model=args.model or resolve_model(ROOT),
        base_url=resolve_base_url(ROOT),
        api_key=resolve_api_key(ROOT),
        temperature=0.0,
    ).resolved()
    client = LLMClient(cfg, workspace=ROOT)

    answers: list[dict] = []
    total_in = total_out = 0

    # 一次调用解决所有题目；编程题代码量大，给更宽的 max_tokens
    if choice_todo:
        print(f"把 {len(choice_todo)} 道单选题发给 {cfg.model}（一次调用）…")
        resp = client.chat(
            [{"role": "system", "content": SOLVE_SYSTEM},
             {"role": "user", "content": build_solve_prompt(data)}],
            temperature=0.0, max_tokens=4096)
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
        resp = client.chat(
            [{"role": "system", "content": PROGRAM_SOLVE_SYSTEM},
             {"role": "user", "content": build_program_prompt(data)}],
            temperature=0.0, max_tokens=16000)
        total_in += (resp.usage or {}).get("prompt_tokens", 0)
        total_out += (resp.usage or {}).get("completion_tokens", 0)
        try:
            prog = parse_solve_output(resp.content)
        except Exception as exc:
            print(f"编程题解析失败：{exc}\n{resp.content[:600]}")
            return 1
        for a in prog:
            a["type"] = "program"
        answers += prog
        print(f"  {len(prog)} 题，输入 {(resp.usage or {}).get('prompt_tokens', 0)} "
              f"/ 输出 {(resp.usage or {}).get('completion_tokens', 0)} tokens")

    print(f"合计：输入 {total_in} / 输出 {total_out} tokens")

    by_label = {p["label"]: p for p in data["problems"]}
    out = []
    print("\n模型给出的答案（**请自己复核**）：")
    for a in answers:
        label = str(a.get("label", ""))
        src = by_label.get(label)
        if a.get("type") == "program":
            code = str(a.get("code", ""))
            first = code.strip().splitlines()[0] if code.strip() else "(空)"
            print(f"  {label:8} [代码 {len(code):>4} 字符] {first[:60]}")
            out.append({"label": label, "type": "program", "code": code})
            continue
        idx = a.get("index")
        picked = ""
        if src and isinstance(idx, int) and 0 <= idx < len(src.get("options", [])):
            picked = src["options"][idx]["text"]
        print(f"  {label:8} [{idx}] {picked[:70]}")
        print(f"           理由：{str(a.get('reason', ''))[:100]}")
        if src is not None:
            out.append({"label": label, "type": "choice", "index": idx})

    # 补全模型漏掉的题，保持文件结构完整
    got = {a["label"] for a in out}
    for p in data["problems"]:
        if p["label"] not in got:
            out.append({"label": p["label"], "type": p.get("kind", "choice"),
                        **({"code": ""} if p.get("kind") == "program" else {"index": None})})

    ans_file = Path(args.answers) if args.answers else OUT_DIR / "answers.json"
    if ans_file.is_file() and not args.force:
        backup = ans_file.with_suffix(".json.bak")
        backup.write_text(ans_file.read_text(encoding="utf-8-sig"), encoding="utf-8")
        print(f"\n已备份原答案文件到 {backup.name}")
    write_json(ans_file, {
        "_说明": "由 --solve 生成，请复核后再 --fill；想手改就直接改 index/text/code",
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
        return fill_program(page, answers, dry=bool(args.dry_run))

    choices = [a for a in answers
               if a.get("type") == "choice"
               and (a.get("index") is not None or a.get("text"))]
    programs = [a for a in answers if a.get("type") == "program" and a.get("code")]

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
