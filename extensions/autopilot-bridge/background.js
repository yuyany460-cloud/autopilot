// AutoPilot Bridge —— 让 AutoPilot 直接接管这个浏览器里已经打开的标签页。
//
// 为什么需要扩展：CDP 调试端口只能在浏览器启动那一刻打开，Edge 136+ 还禁止对
// 默认配置目录开这个端口。扩展跑在浏览器内部，用 chrome.debugger 就能对**正在
// 运行的**标签页拿到完整 CDP，不用重启、不用复制配置、不用重新登录。
//
// 两个设计要点：
//   1) 命令走 WebSocket。MV3 的 service worker 会被 30 秒空闲回收，只有 WebSocket
//      的收发能把存活计时器顶回去（Chrome 116+），所以我们每 20 秒发一次心跳，
//      顺便也用来确认 Python 那边还活着。
//   2) 调试器**按需挂载、空闲自动卸载**。挂载期间标签页顶上会有一条"正在调试"
//      提示条，自动 detach 之后它就消失了——只有自动化真正在跑的那几秒才看得见。

const PORT = 8849;
const WS_URL = `ws://127.0.0.1:${PORT}/`;
const HTTP_URL = `http://127.0.0.1:${PORT}/`;
const RECONNECT_MS = 1500;
const KEEPALIVE_MS = 20000;
const ATTACH_IDLE_MS = 8000;     // 空闲这么久就 detach，让提示条消失
const CDP_VERSION = "1.3";

let ws = null;
let keepaliveTimer = null;
const attachTimers = new Map();  // tabId -> 自动 detach 的定时器
const warmedTabs = new Set();    // 已经"焐热"过 CDP 通道的 tabId

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* ------------------------------------------------------ 让 SW 活下来 */

// MV3 的 service worker 空闲 30 秒就会被回收，而回收之后 WebSocket 和重连
// 定时器一起没了——表现就是"AutoPilot 起了，扩展却永远不连"。
// 两条官方支持的路子都用上：
//   1) 页面里的 content script 开一个长连 port（有 port 在，SW 就不会被回收）
//   2) chrome.alarms 兜底，万一没有任何页面也能每 30 秒醒一次重连
//
// 注意：下面每一处 chrome.* 注册都包了 try——只要有一个 API 因为权限没生效
// 而抛异常，整个 service worker 就会加载失败，而且**连日志都留不下来**，
// 表现就是"卡片显示已启用，但服务工作进程永远是不活动"。
const safe = (label, fn) => {
  try {
    fn();
  } catch (err) {
    reportNote(`注册 ${label} 失败：${err && err.message}`);
  }
};

safe("onConnect", () => chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "keepalive") return;
  port.onMessage.addListener(() => { /* 收到消息即刷新 SW 的存活计时 */ });
}));

// 内容脚本每 4 秒戳一下。这一戳有两个作用：
//   1) sendMessage 本身是唤醒事件——SW 被回收了也能被叫起来
//   2) 起来之后顺手检查连接，断了就立刻重连
// 少了这一步，AutoPilot 刚启动时扩展经常还睡着，browser_debug 就会退回
// "另开一个浏览器"，用户明明开着 Edge，却莫名其妙又多出一个窗口。
safe("onMessage", () => chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.op === "__ensure" && (!ws || ws.readyState !== WebSocket.OPEN)) {
    connect();
  }
  return false;
}));

function ensureAlarm() {
  try {
    chrome.alarms.create("autopilot-reconnect", { periodInMinutes: 0.5 });
  } catch (_) {}
}

safe("alarms.onAlarm", () => chrome.alarms.onAlarm.addListener(() => {
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
}));
safe("tabs.onUpdated", () => chrome.tabs.onUpdated.addListener(() => {
  if (!ws || ws.readyState === WebSocket.CLOSED) connect();
}));
safe("action.onClicked", () => chrome.action.onClicked.addListener(() => {
  if (!ws || ws.readyState !== WebSocket.OPEN) connect();
}));

/**
 * 连不上时把原因写到桥的 HTTP 诊断口上——否则扩展这边的报错在浏览器里
 * 根本看不见，只能靠猜。
 */
function reportNote(note) {
  try {
    fetch(`${HTTP_URL}?note=${encodeURIComponent(note)}`, { cache: "no-store" })
      .catch(() => {});
  } catch (_) {}
}

/* ------------------------------------------------------------------ 连接 */

function setBadge(on) {
  try {
    chrome.action.setBadgeText({ text: on ? "●" : "" });
    chrome.action.setBadgeBackgroundColor({ color: on ? "#1a7f37" : "#8b949e" });
    chrome.action.setTitle({
      title: on ? "AutoPilot Bridge：已连接" : "AutoPilot Bridge：未连接（等 AutoPilot 启动）",
    });
  } catch (_) {}
}

function connect() {
  ensureAlarm();
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  let sock;
  try {
    sock = new WebSocket(WS_URL);
  } catch (err) {
    reportNote(`构造 WebSocket 失败：${err}`);
    setTimeout(connect, RECONNECT_MS);
    return;
  }
  ws = sock;

  sock.onopen = () => {
    setBadge(true);
    reportNote("已连接");
    wsSend({ op: "__hello", params: {
      version: chrome.runtime.getManifest().version,
      browser: navigator.userAgent,
    }});
    clearInterval(keepaliveTimer);
    keepaliveTimer = setInterval(() => wsSend({ op: "__pong" }), KEEPALIVE_MS);
  };

  sock.onmessage = async (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch (_) {
      return;
    }
    // 判据必须是「有没有 id」，不能是「有没有 op」——
    // 桥下发的命令长这样：{id: 1, op: "tabs", params: {...}}，也是带 op 的。
    // 早先写成 `if (msg.op) return;` 的本意是忽略服务端通知，
    // 结果把**每一条命令**都当通知丢了：连得上、但永远不回话。
    if (msg.id === undefined) return;
    const reply = { id: msg.id };
    try {
      reply.ok = true;
      reply.result = await handle(msg.op, msg.params || {});
    } catch (err) {
      reply.ok = false;
      reply.error = String((err && err.message) || err);
    }
    wsSend(reply);
  };

  sock.onclose = (ev) => {
    setBadge(false);
    clearInterval(keepaliveTimer);
    if (ws === sock) ws = null;
    reportNote(`连接关闭 code=${ev && ev.code} reason=${(ev && ev.reason) || ""}`);
    setTimeout(connect, RECONNECT_MS);
  };

  sock.onerror = () => {
    // onerror 不带细节，只能记一笔；onclose 会跟着触发
    reportNote("WebSocket 报错");
  };
}

// 往 WebSocket 发一条消息。
//
// 函数名**不能**叫 send：下面还有一个发 CDP 命令的 send(tabId, ...)。
// manifest 里声明了 "type": "module"，而 ES module 是严格模式，
// 重复声明同名函数会直接语法错误，整个 service worker 都加载不起来——
// 而且浏览器只会淡淡显示一句"服务工作进程（不活动）"，一个字的原因都不给。
function wsSend(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify(obj));
  }
}

// 模块一开始就往桥上报一笔：这样只要 service worker 真的跑起来了，
// 我这边就一定看得到——否则"连不上"到底是没启动还是连不上，根本没法区分。
reportNote("service worker 启动");

connect();
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

/* ------------------------------------------------------- 标签页 / 调试器 */

async function pickTab(tabId) {
  if (tabId) return await chrome.tabs.get(Number(tabId));
  const [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (tab) return tab;
  const all = await chrome.tabs.query({});
  if (!all.length) throw new Error("浏览器里没有任何标签页");
  return all[0];
}

async function ensureAttached(tabId) {
  const target = { tabId };
  const key = String(tabId);
  let attached = false;
  try {
    const targets = await chrome.debugger.getTargets();
    attached = targets.some((t) => t.tabId === tabId && t.attached);
  } catch (_) {}
  if (!attached) {
    try {
      await chrome.debugger.attach(target, CDP_VERSION);
    } catch (err) {
      throw new Error(
        `无法附加调试器：${(err && err.message) || err}。` +
        `如果这个标签页正开着开发者工具（F12），请先关掉再试。`);
    }
  }
  // 每次使用都把自动卸载时间往后推
  clearTimeout(attachTimers.get(key));
  attachTimers.set(key, setTimeout(() => {
    attachTimers.delete(key);
    warmedTabs.delete(key);
    chrome.debugger.detach(target).catch(() => {});
  }, ATTACH_IDLE_MS));
  return target;
}

async function send(tabId, method, params) {
  const target = await ensureAttached(tabId);
  const key = String(tabId);
  if (!warmedTabs.has(key)) {
    // **刚 attach 上时，第一条命令常常石沉大海**——`Page.captureScreenshot` 尤其明显：
    // 它会把超时耗满，第二条才成功。日志实测 `browser_read_element` 每次稳定 9.6~11.4 秒，
    // 正好是"8 秒空等 + 0.2 秒成功 + 1.5 秒识别"——就是每次都吃了一次这个闷亏。
    // （`Runtime.evaluate` 不吃这一套，所以只看见截图出问题。）
    // 先用 Page.enable 走一个来回把通路焐热，再发真正的命令。
    try {
      await chrome.debugger.sendCommand(target, "Page.enable", {});
    } catch (_) { /* 焐热失败也照发，别把命令挡在这里 */ }
    await sleep(120);
    warmedTabs.add(key);
  }
  return await chrome.debugger.sendCommand(target, method, params || {});
}

// 有的 CDP 调用会一直不返回（整页截图遇到超长/固定定位页面最常见），
// 桥那边的调用超时是几十秒——不设上限，用户看到的就是界面整整卡一分钟。
function withTimeout(promise, ms, label) {
  let timer;
  const guard = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(
      `${label || "CDP 调用"}超过 ${Math.round(ms / 1000)} 秒没返回`)), ms);
  });
  return Promise.race([promise, guard]).finally(() => clearTimeout(timer));
}

async function evaluate(tabId, expression, awaitPromise = true) {
  const res = await send(tabId, "Runtime.evaluate", {
    expression, returnByValue: true, awaitPromise,
  });
  if (res && res.exceptionDetails) {
    const d = res.exceptionDetails;
    throw new Error((d.exception && d.exception.description) || d.text || "页面脚本报错");
  }
  return res && res.result ? res.result.value : undefined;
}

/* ------------------------------------------------------------ 元素定位 */

// 把「CSS 选择器 或 可见文字」解析成一个真实的 DOM 元素。
// 和 Python 侧 _locator 的策略保持一致：先当选择器试，再按文字找。
function locatorSource(target, by) {
  return `
    (() => {
      const target = ${JSON.stringify(target)};
      const by = ${JSON.stringify(by || "auto")};
      const looksSelector = /^[.#\\[]|^[a-z]+[>\\[.:#\\s]/i.test(target);
      // 带空格的后代选择器（div[class*="codeEditor"] .cm-content）靠字符猜不出来，
      // 猜错就当文字去搜、静默失灵。所以只要整串像选择器就直接 querySelector 试一次。
      const plausible = /^[A-Za-z0-9_\\-.#\\[\\]="'>*+~(),:\\s]+$/.test(target) && !!target.trim();
      const visible = (el) => {
        if (!el) return false;
        const r = el.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) return false;
        const st = getComputedStyle(el);
        return st.visibility !== "hidden" && st.display !== "none" && st.opacity !== "0";
      };
      const norm = (s) => (s || "").replace(/\\s+/g, " ").trim();
      const want = norm(target);

      if (by === "selector" || (by === "auto" && (looksSelector || plausible))) {
        try {
          const el = document.querySelector(target);
          if (el) return el;
        } catch (_) {}
        if (by === "selector") return null;
      }
      // 文字完全相等优先，其次是包含
      const clickable = [...document.querySelectorAll(
        'a,button,input,textarea,select,summary,label,[role="button"],[role="link"],' +
        '[role="tab"],[role="checkbox"],[role="radio"],[role="menuitem"],[onclick],[tabindex]')];
      for (const el of clickable) {
        const t = norm(el.innerText || el.value || el.getAttribute("aria-label") || "");
        if (t && t === want && visible(el)) return el;
      }
      for (const el of clickable) {
        const t = norm(el.innerText || el.value || el.getAttribute("aria-label") || "");
        if (t && t.includes(want) && visible(el)) return el;
      }
      // 最后再退回到"任何可见元素里文字匹配"
      for (const el of document.querySelectorAll("body *")) {
        const t = norm(el.textContent || "");
        if (t === want && visible(el)) return el;
      }
      return null;
    })()`;
}

async function locate(tabId, target, by) {
  const info = await evaluate(tabId, `
    (() => {
      const el = ${locatorSource(target, by)};
      if (!el) return null;
      el.scrollIntoView({ block: "center", inline: "center" });
      const r = el.getBoundingClientRect();
      return {
        x: Math.round(r.left + r.width / 2),
        y: Math.round(r.top + r.height / 2),
        w: Math.round(r.width), h: Math.round(r.height),
        tag: el.tagName,
        text: (el.innerText || el.value || el.getAttribute("aria-label") || "").slice(0, 60),
        // 只读展示块（比如 OJ 页面上"输入样例"的编辑器）看着一模一样，写进去却毫无反应
        readonly: !!(el.closest && el.closest(
          "[class*='readOnly'], [class*='readonly'], [aria-readonly='true'], [readonly]")),
      };
    })()`);
  if (!info) throw new Error(`页面上找不到「${target}」`);
  return info;
}

/* ---------------------------------------------------------- 键名映射 */

// CDP 的按键事件要自己拼 key / code / windowsVirtualKeyCode，这里覆盖常用键
const KEYS = {
  Enter:      { key: "Enter", code: "Enter", vk: 13, text: "\r" },
  Tab:        { key: "Tab", code: "Tab", vk: 9 },
  Escape:     { key: "Escape", code: "Escape", vk: 27 },
  Backspace:  { key: "Backspace", code: "Backspace", vk: 8 },
  Delete:     { key: "Delete", code: "Delete", vk: 46 },
  " ":        { key: " ", code: "Space", vk: 32, text: " " },
  Space:      { key: " ", code: "Space", vk: 32, text: " " },
  ArrowUp:    { key: "ArrowUp", code: "ArrowUp", vk: 38 },
  ArrowDown:  { key: "ArrowDown", code: "ArrowDown", vk: 40 },
  ArrowLeft:  { key: "ArrowLeft", code: "ArrowLeft", vk: 37 },
  ArrowRight: { key: "ArrowRight", code: "ArrowRight", vk: 39 },
  Home:       { key: "Home", code: "Home", vk: 36 },
  End:        { key: "End", code: "End", vk: 35 },
  PageUp:     { key: "PageUp", code: "PageUp", vk: 33 },
  PageDown:   { key: "PageDown", code: "PageDown", vk: 34 },
  Control:    { key: "Control", code: "ControlLeft", vk: 17, modifier: 2 },
  Shift:      { key: "Shift", code: "ShiftLeft", vk: 16, modifier: 8 },
  Alt:        { key: "Alt", code: "AltLeft", vk: 18, modifier: 1 },
  Meta:       { key: "Meta", code: "MetaLeft", vk: 91, modifier: 4 },
};
for (let i = 1; i <= 12; i++) {
  KEYS[`F${i}`] = { key: `F${i}`, code: `F${i}`, vk: 111 + i };
}

function keyPlan(combo) {
  const parts = String(combo).split("+").map((s) => s.trim()).filter(Boolean);
  const main = parts.pop() || "";
  const mods = parts.map((m) => {
    const k = m.length === 1 ? m.toUpperCase() + "" : m;
    return KEYS[m] || KEYS[k] || { key: m, code: m, vk: 0, modifier: 0 };
  });
  let def = KEYS[main];
  if (!def && main.length === 1) {
    const upper = main.toUpperCase();
    const code = /[a-zA-Z]/.test(main) ? `Key${upper}`
      : /[0-9]/.test(main) ? `Digit${main}` : "";
    def = { key: main, code, vk: upper.charCodeAt(0), text: main };
  }
  if (!def) def = { key: main, code: main, vk: 0 };
  let modifiers = 0;
  for (const m of mods) modifiers |= (m.modifier || 0);
  return { main: def, mods, modifiers };
}

async function pressKey(tabId, combo) {
  const { main, mods, modifiers } = keyPlan(combo);
  for (const m of mods) {
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "keyDown", key: m.key, code: m.code,
      windowsVirtualKeyCode: m.vk, modifiers,
    });
  }
  const base = {
    key: main.key, code: main.code,
    windowsVirtualKeyCode: main.vk, modifiers,
  };
  // 有可见字符的键要把 text 带上，否则页面收不到输入
  if (main.text && !(modifiers & ~8)) {
    await send(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyDown", text: main.text });
    await send(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
  } else {
    await send(tabId, "Input.dispatchKeyEvent", { ...base, type: "rawKeyDown" });
    await send(tabId, "Input.dispatchKeyEvent", { ...base, type: "keyUp" });
  }
  for (const m of mods.slice().reverse()) {
    await send(tabId, "Input.dispatchKeyEvent", {
      type: "keyUp", key: m.key, code: m.code,
      windowsVirtualKeyCode: m.vk, modifiers,
    });
  }
}

/* ------------------------------------------------------------ 命令实现 */

async function handle(op, p) {
  if (op === "tabs") {
    const list = await chrome.tabs.query({});
    return {
      count: list.length,
      tabs: list.map((t) => ({
        id: t.id, title: t.title || "", url: t.url || "",
        active: !!t.active, windowId: t.windowId,
      })),
    };
  }

  if (op === "state") {
    const tab = await pickTab(p.tabId);
    return { id: tab.id, url: tab.url || "", title: tab.title || "",
             ready: await evaluate(tab.id, "document.readyState", false) };
  }

  if (op === "activate") {
    const t = await chrome.tabs.update(Number(p.tabId), { active: true });
    if (t && t.windowId != null) {
      try { await chrome.windows.update(t.windowId, { focused: true }); } catch (_) {}
    }
    return `已切到标签页 [${t.id}] ${t.title || ""} — ${t.url || ""}`;
  }

  if (op === "close_tab") {
    const id = Number(p.tabId);
    let title = "";
    try { title = (await chrome.tabs.get(id)).title || ""; } catch (_) {}
    await chrome.tabs.remove(id);
    return `已关闭标签页 [${id}] ${title}`;
  }

  const tab = await pickTab(p.tabId);
  const tabId = tab.id;

  switch (op) {
    case "goto": {
      await send(tabId, "Page.navigate", { url: String(p.url) });
      const deadline = Date.now() + (p.timeoutMs || 25000);
      while (Date.now() < deadline) {
        await sleep(200);
        let ready = "";
        try {
          ready = await evaluate(tabId, "document.readyState", false);
        } catch (_) { continue; }
        if (ready === "complete") break;
      }
      const now = await chrome.tabs.get(tabId);
      return { url: now.url || "", title: now.title || "" };
    }

    case "text": {
      // 必须穿透同源 iframe：超星/学习通、教务系统这类站点把正文放在二级 iframe 里，
      // 只读顶层文档的话拿到的永远是外层导航栏（"章节详情…1视频 2章节测验"），
      // 模型只能靠 browser_eval 一次次猜测 iframe 结构——实测这样能白烧 20+ 步。
      // 这里递归收集，并把每层 frame 的 URL 一起带上，让调用方一次就看清楚正文在哪。
      const maxDepth = Number.isFinite(p.depth) ? Number(p.depth) : 4;
      return await evaluate(tabId,
        `(() => {
           const sel = ${JSON.stringify(p.selector || "body")};
           const maxDepth = ${maxDepth};
           const clean = (s) => (s || "").replace(/\\n{3,}/g, "\\n\\n").trim();
           const chunks = [];
           const walk = (doc, label, depth) => {
             let text = "";
             try {
               const el = doc.querySelector(sel) || doc.body;
               text = clean(el ? (el.innerText || "") : "");
             } catch (_) { text = ""; }
             if (text) chunks.push(label ? (label + "\\n" + text) : text);
             if (depth >= maxDepth) return;
             let frames = [];
             try { frames = Array.from(doc.querySelectorAll("iframe, frame")); } catch (_) { frames = []; }
             frames.forEach((f, i) => {
               let fd = null;
               try { fd = f.contentDocument; } catch (_) { fd = null; }
               if (!fd) return;                       // 跨域拿不到，跳过而不是报错
               let url = "";
               try { url = f.contentWindow.location.href || f.src || ""; }
               catch (_) { url = f.src || ""; }
               walk(fd, "--- frame#" + i + " " + url + " ---", depth + 1);
             });
           };
           walk(document, "", 0);
           return chunks.join("\\n\\n");
         })()`);
    }

    case "eval": {
      const script = String(p.script || "");
      try {
        return await evaluate(tabId, script);
      } catch (err) {
        // 允许传"一段语句"而不是表达式，跟 Playwright 一样两头都兜住。
        // 关键：用 eval 而不是直接塞进箭头函数体——箭头函数体没有 completion value，
        // 最后一句话是表达式也拿不到返回值（agent 会以为脚本没执行），
        // 而且 `const el = ...` 在函数体里连写两次同样会撞车。
        // eval 既保留"最后一句表达式的值"，又让 let/const 落在这个临时作用域里。
        if (!/SyntaxError|Unexpected token|Invalid or unexpected/i.test(String(err.message))) throw err;
        return await evaluate(tabId, `(() => eval(${JSON.stringify(script)}))()`);
      }
    }

    case "click": {
      const info = await locate(tabId, p.target, p.by);
      const button = p.button || "left";
      const clicks = Math.max(1, Number(p.clicks || 1));
      await send(tabId, "Input.dispatchMouseEvent",
        { type: "mouseMoved", x: info.x, y: info.y, button: "none" });
      for (let i = 1; i <= clicks; i++) {
        await send(tabId, "Input.dispatchMouseEvent",
          { type: "mousePressed", x: info.x, y: info.y, button, clickCount: i });
        await send(tabId, "Input.dispatchMouseEvent",
          { type: "mouseReleased", x: info.x, y: info.y, button, clickCount: i });
      }
      const now = await chrome.tabs.get(tabId);
      return { target: p.target, tag: info.tag, text: info.text,
               x: info.x, y: info.y, url: now.url || "", title: now.title || "" };
    }

    case "fill": {
      const info = await locate(tabId, p.target, p.by || "auto");
      const ok = await evaluate(tabId, `
        (() => {
          const el = ${locatorSource(p.target, p.by || "auto")};
          if (!el) return "找不到元素";
          el.focus();
          const proto = el instanceof HTMLTextAreaElement
            ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          const desc = Object.getOwnPropertyDescriptor(proto, "value");
          // 用原生 setter 绕开 React 对 value 的劫持，否则框架不认这次修改
          if (desc && desc.set) desc.set.call(el, ${JSON.stringify(String(p.text))});
          else el.value = ${JSON.stringify(String(p.text))};
          el.dispatchEvent(new Event("input", { bubbles: true }));
          el.dispatchEvent(new Event("change", { bubbles: true }));
          return "ok";
        })()`);
      if (ok !== "ok") throw new Error(`${ok}：${p.target}`);
      if (p.submit) await pressKey(tabId, "Enter");
      return { target: p.target, tag: info.tag, length: String(p.text).length };
    }

    case "type": {
      let info = null;
      if (p.target) {
        info = await locate(tabId, p.target, p.by || "auto");
        if (info.readonly) {
          throw new Error(
            `「${p.target}」命中的是只读展示块（样例输入/输出），往里写不会有任何反应。` +
            `先用 browser_eval 列出所有 .cm-editor 的 class，再指定答题编辑器的选择器。`);
        }
        await handle("click", { tabId, target: p.target, by: p.by || "auto" });
      }
      const text = String(p.text || "").replace(/\r\n?/g, "\n");
      if (p.clear_first !== false) {
        await pressKey(tabId, "Control+A");
        await pressKey(tabId, "Delete");
      }
      if ((p.method || "insert") === "type") {
        for (const ch of text) {
          // 换行/制表符不能当普通字符发：text:"\n" 页面收不到，
          // 整段代码会挤成一行（编辑器里就是"一直在同一行上面"）。
          if (ch === "\n") { await pressKey(tabId, "Enter"); continue; }
          if (ch === "\t") { await pressKey(tabId, "Tab"); continue; }
          await send(tabId, "Input.dispatchKeyEvent",
            { type: "keyDown", text: ch, unmodifiedText: ch, key: ch });
          await send(tabId, "Input.dispatchKeyEvent", { type: "keyUp", key: ch });
        }
      } else {
        // insertText 走的是"插入文本"通道，Monaco / CodeMirror 认这个
        await send(tabId, "Input.insertText", { text });
      }
      if (p.submit) await pressKey(tabId, "Enter");
      return { length: text.length, method: p.method || "insert", target: p.target || "" };
    }

    case "press": {
      await pressKey(tabId, String(p.key));
      return { key: String(p.key) };
    }

    case "screenshot": {
      const shot = (full) => {
        const opts = { format: "png" };
        if (full) opts.captureBeyondViewport = true;
        return withTimeout(send(tabId, "Page.captureScreenshot", opts), 8000, "截图");
      };

      // 后台/最小化时 Chromium 不合成帧，Page.captureScreenshot 会一直挂着。
      // 用 Emulation.setDeviceMetricsOverride 伪造一个视口，Chromium 就会走离屏
      // 渲染，被遮挡也能出图——而且一次能拍整页（不用滚动拼接）。
      // 拍完必须 clear，否则页面布局被一直改着。
      const offscreen = async (full) => {
        const expr = "JSON.stringify({w: Math.max(innerWidth, document.documentElement.scrollWidth),"
          + " h: " + (full ? "Math.max(innerHeight, document.documentElement.scrollHeight)" : "innerHeight")
          + ", d: devicePixelRatio || 1})";
        const { result } = await send(tabId, "Runtime.evaluate", {
          expression: expr, returnByValue: true,
        });
        const v = JSON.parse(result.value);
        await send(tabId, "Emulation.setDeviceMetricsOverride", {
          width: Math.max(320, Math.ceil(v.w)),
          height: Math.max(240, Math.ceil(v.h)),
          deviceScaleFactor: v.d || 1,
          mobile: false,
        });
        try {
          await sleep(60);
          return await shot(full);
        } finally {
          try { await send(tabId, "Emulation.clearDeviceMetricsOverride"); } catch (_) {}
        }
      };

      // 先走离屏（前台后台都能用，且更快），失败再退回前台合成
      let res;
      let mode = "offscreen";
      try {
        res = await offscreen(!!p.full_page);
      } catch (err) {
        try {
          res = await shot(!!p.full_page);
          mode = "direct";
        } catch (err2) {
          try {
            await chrome.tabs.update(tabId, { active: true });
            const tab = await chrome.tabs.get(tabId);
            if (tab && tab.windowId !== undefined) {
              await chrome.windows.update(tab.windowId, { focused: true });
            }
          } catch (_) {}
          await sleep(250);
          res = await shot(false);
          mode = "foreground";
        }
      }
      return { base64: res.data, mode,
               full_page: !!p.full_page && mode !== "foreground" };
    }

    case "reload": {
      await send(tabId, "Page.reload", {});
      await sleep(800);
      const now = await chrome.tabs.get(tabId);
      return { url: now.url || "", title: now.title || "" };
    }

    case "detach_all": {
      for (const key of [...attachTimers.keys()]) {
        clearTimeout(attachTimers.get(key));
        attachTimers.delete(key);
        try { await chrome.debugger.detach({ tabId: Number(key) }); } catch (_) {}
      }
      return { detached: true };
    }

    default:
      throw new Error(`不认识的命令：${op}`);
  }
}
