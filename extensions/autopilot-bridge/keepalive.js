// 内容脚本：让 service worker 一直在线。
//
// 为什么需要它：MV3 的 service worker 空闲约 30 秒就被浏览器回收，而 WebSocket
// 是**跑在 service worker 里的**——SW 一没，连接就断。
// 表现就是：AutoPilot 刚启动时扩展还没连上，于是它退回"另开一个浏览器"，
// 用户明明已经开着一个了，却看到又弹出一个新窗口，完全莫名其妙。
//
// 内容脚本活在网页里，不受 SW 那套回收规则管。所以由它每几秒戳一下 SW：
//   · sendMessage 本身就是唤醒事件
//   · 顺便让 SW 检查连接，断了就立刻重连
//   · 有来有往也算活动，SW 不会被回收
//
// 它不读页面内容、不发任何业务数据。

const BEAT_MS = 4000;

function beat() {
  try {
    chrome.runtime.sendMessage({ op: "__ensure" }, () => {
      // 读一下 lastError，否则"扩展上下文失效"的告警会刷满控制台
      void chrome.runtime.lastError;
    });
  } catch (_) {
    // 扩展被重载/禁用时会抛，忽略
  }
}

beat();
setInterval(beat, BEAT_MS);
