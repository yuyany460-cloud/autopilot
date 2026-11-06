# AutoPilot Bridge（浏览器扩展）

让 AutoPilot 直接接管你**已经打开**的那个浏览器：不用调试端口、不用新开窗口、  
不用复制配置、**不用重新登录**。

## 安装（一次性，约 1 分钟）

1. 地址栏输入 `edge://extensions/` 回车
2. 打开左下角的 **开发人员模式**
3. 点 **加载解压缩的扩展**，选中这个目录（`extensions\autopilot-bridge`）
4. 装好后工具栏会出现 AutoPilot Bridge 图标：
   - **灰色空心**= 没连上（AutoPilot 没在跑）
   - **绿色圆点**= 已连上

装完不用管它，AutoPilot 一起来它自己就连上了。

## 怎么用

```powershell
python -m autopilot run "帮我看看浏览器里开着的那个超星作业页面"
```

Agent 调 `browser_debug` 时会**自动优先走扩展**，直接操作你当前的标签页。  
`browser_status` 能列出所有标签页，`browser_tabs(action="switch", tab_id=...)`  
可以切到指定的那个。

## 它是怎么工作的

扩展用 [`chrome.debugger`](https://developer.chrome.com/docs/extensions/reference/api/debugger)  
拿到标签页的完整 CDP 能力——和 Playwright 是同一套接口，所以  
`browser_click` / `browser_type` 产生的都是**真实鼠标键盘事件**（`isTrusted=true`），  
Monaco / CodeMirror 这类会校验输入来源的编辑器照常能用。

和 AutoPilot 之间走本地 WebSocket（默认 `ws://127.0.0.1:8849`），  
JSON 一行一条命令。端口可以用环境变量 `AUTOPILOT_EXT_PORT` 改（两边要一致）。

## 两个已知限制

- **`edge://` / `chrome://` 内部页面碰不了**。CDP 按设计不允许附加到浏览器自己的页面上，  
  操作这类标签页会报"无法附加调试器"。普通 `http(s)` 网页不受影响。
- **那个标签页不能同时开着开发者工具（F12）**。CDP 一个标签页只能被一个调试器占用，  
  这时会报"无法附加调试器"，关掉 F12 再试即可。

关于"正在调试此浏览器"提示条：它是 `chrome.debugger` 的固定行为、关不掉，但扩展  
只在真正要操作时才挂载调试器，**空闲 8 秒自动卸载**，提示条跟着消失。

## 让扩展保持在线

MV3 的 service worker 会被浏览器在空闲 30 秒后回收，连接跟着断。扩展用两条官方  
支持的路子维持：

1. **内容脚本保活**：普通网页里会注入一个极小的 `keepalive.js`，它开一个长连的  
   port 顶住 service worker 的存活计时（不读页面内容、不发任何业务数据）。  
   所以**只要浏览器里开着一个普通网页，连接就一直在**。
2. **`chrome.alarms` 兜底**：万一没有任何网页，每 30 秒也会醒一次重连。

如果你刚装完/重载完扩展，之前已经开着的页面需要**刷新一次**才会注入保活脚本。  
AutoPilot 那边也做了兜底：命令超时会重置连接并重试，不会一直干等。

## 权限说明

| 权限           | 用来干什么                                    |
| ------------ | ---------------------------------------- |
| `debugger`   | 通过 CDP 操作页面（真实输入事件、跨域 iframe、后台标签页截图都靠它） |
| `tabs`       | 列出/切换/关闭标签页                              |
| `scripting`  | 页面里的 JS 求值                               |
| `<all_urls>` | 要能操作任意网站，否则只能操作被授权的那几个                   |

扩展**只连本机 127.0.0.1**，不往任何外部服务器发数据。
