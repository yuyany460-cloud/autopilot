# AutoPilot —— Windows 全自动电脑操控框架

用自然语言让 AI 真的替你操作 Windows：看屏幕、点按钮、敲键盘、开程序、改文件、跑命令。

```powershell
apilot run "打开记事本，把今天的日期写进去，保存到桌面的 note.txt"
apilot run "把当前屏幕上所有窗口标题列出来，用通知发给我"
```

---

## 它和别的"电脑操控 Agent"有什么不同

GitHub 上已经有几个同类项目，最接近的是 [microsoft/UFO²](https://github.com/microsoft/UFO)。
但它们的共同前提是 **必须有一个视觉模型**（GPT-4o / Claude / Qwen-VL 级别）：

| 项目 | 路线 | 前提 |
| --- | --- | --- |
| microsoft/UFO² | UIA + Win32 + COM + 视觉，多智能体 | 需要 gpt-4o 等视觉模型 |
| OthersideAI/self-operating-computer | 纯截图 + 视觉模型 + pyautogui | 需要视觉模型 |
| simular-ai/Agent-S | 认知架构，视觉为主 | 需要强视觉模型 |
| bytedance/UI-TARS-desktop | 端到端 VLA 模型 | 依赖其模型 |
| **AutoPilot（本项目）** | **UIA 元素树 + 系统 OCR → 文本** | **纯文本模型即可** |

AutoPilot 把"看屏幕"变成"读结构"：

* **UI Automation** 读出每个按钮/输入框的名称、类型、坐标、可用操作 —— 信息密度远高于像素
* **Windows 自带 OCR** 覆盖 UIA 拿不到的自绘界面（Electron、游戏、图片里的字）
* 每个可交互元素分配短编号（`E1` `E2`…），模型只需说"点 E7"，**不做像素级推理**

于是 `deepseek-flash` 这种没有视觉能力的模型也能可靠操作电脑。实测：

```
🎯 第 1 步 → system_info {}
   ✅ 内存占用 54%、CPU 10.1%、开机 0天9小时29分
🎯 第 2 步 → notify {"title":"电脑状态提醒","message":"当前内存占用 54%，共打开 3 个窗口。"}
   ✅ 通知已发送
🎯 第 3 步 → finish {"summary":"...","success":true}
🏁 任务完成
```

其他特点：

* **核心零第三方依赖** —— 只用标准库 + `ctypes` + 系统自带的 PowerShell 5.1
* **安全护栏是一等公民** —— 命令黑名单、系统路径保护、风险分级、全局急停、审计日志
* **确定性流程** —— 固定套路写成 JSON 反复跑，不烧 token，还能录制生成
* **中文优先**，复用你机器上已有的 DeepSeek 密钥

**已知短板**：拿不到元素树的应用（Electron 应用未开无障碍、游戏、自绘界面）只能退到 OCR + 坐标；
没有 UFO² 的多智能体编排与经验学习；只支持 Windows。

---

## 安装

需要 **Windows 10/11** 和 **Python 3.10+**（含 `ctypes`、`sqlite3` 等标准模块）。

```powershell
cd D:\code\autopilot
python -m pip install -e .
```

装完后有 `apilot` 命令。如果 `apilot` 不在 PATH 里（或你的 Python 是"嵌入式发行版"），
用项目自带的免安装启动器，效果完全一样：

```powershell
python run.py selfcheck
```

### 更短的命令：`ap`

`python -m autopilot do screenshot path=a.png` 敲起来太长了。本机已经装好两个短命令：
**`ap`**（两个字母）和 **`apilot`**，等价于 `python -m autopilot`，**在任何目录下都能用**。

| 短写 | 等价于 | 说明 |
| --- | --- | --- |
| `ap doctor` / `ap dr` | `python -m autopilot doctor` | 环境体检 |
| `ap a` | `actions` | 列出全部动作 |
| `ap o` | `observe` | 打印屏幕快照 |
| `ap d screenshot path=a.png` | `do ...` | 直接执行一个动作 |
| `ap r "打开记事本写一句"` | `run "..."` | 让 Agent 自主完成 |
| `ap f list` / `ap f run --file x.json` | `flow ...` | 确定性流程 |
| `ap sc` | `selfcheck` | 子系统自检 |
| `ap s` | `serve` | Web 控制台 |
| `ap cfg` | `config` | 看当前配置 |

子命令都可以只写首字母，`--help` 在任意层级都能用（`ap r --help`）。

> `ap r` 后面**必须跟目标**（`ap r "..."` 或 `ap r @task.txt`）。只敲 `ap r` 会打印用法，
> 不会瞎跑；只敲 `ap` 会打印总帮助。

**`ap` 提示"不是内部或外部命令"？** PATH 是用户级的，改完之后**已经开着的 cmd 窗口读不到**，
开一个新的窗口就行；再不行用 `where ap` 确认能否找到 `%USERPROFILE%\bin\ap.cmd`。


短命令怎么来的：`%USERPROFILE%\bin\ap.cmd`（和 `apilot.cmd`）调用项目里的
`run.py`，后者把项目根目录塞进 `sys.path` 再 `chdir` 过去——所以不用 `pip install`，
`var/logs`、`flows/` 也永远指向项目里的那份。换机器或移动了项目目录时，把这两个
`.cmd` 里的 `APILOT_ROOT` 改成新路径即可：

```bat
@echo off
setlocal
set "APILOT_ROOT=D:\code\autopilot"
python -c "import sys" >nul 2>nul && (set "PY=python") || (set "PY=D:\code\python\python.exe")
"%PY%" "%APILOT_ROOT%\run.py" %*
```

（`%USERPROFILE%\bin` 已加入**用户级** PATH，新开的 cmd 窗口直接生效，不需要管理员权限。）


**可选增强**（不装也能用）：

```powershell
pip install pillow mss                  # 更快的图像缩放与截屏
pip install fastapi uvicorn             # Web 控制台
pip install playwright                  # 网页自动化（复用系统 Edge，无需下载浏览器）
```

### 出问题怎么查

不用靠"盯着 cmd 窗口"，三样东西都在 `var/logs/` 里，窗口滚没了也在：

| 文件 | 内容 | 什么时候看 |
| --- | --- | --- |
| `run-YYYYMMDD.log` | **控制台全文**（模型的思考、每一步动作、工具返回、报错），已去掉颜色码 | "它刚才到底干了什么 / 为什么停住了" |
| `audit-YYYYMMDD.jsonl` | 每一步动作的结构化记录：参数、结果、耗时、是否被拒 | 想按时间线统计、算间隔、看失败原因 |
| `var/STOP` | 出现这个文件就等于急停 | 想远程叫停，或怀疑被叫停 |

`run-*.log` 默认就写（每次命令追加，超过 5MB 自动滚一个 `.1.log`），
不想留痕加 `--no-log`。想实时看进度用 `ap s` 打开 Web 控制台。

> 常见的"它不干活"其实只有三种：模型自己 `finish` 结束了（一次性任务，不会待命）、
> 被 `Ctrl+Alt+Q` 急停、或者卡在某个工具的超时上（`audit` 里最后一条的时间戳会说清）。

### 配置模型

按优先级任选一种：

```powershell
# 1) 环境变量
$env:AUTOPILOT_API_KEY = "sk-xxxx"

# 2) 项目配置文件 var/config.json
'{"api_key":"sk-xxxx","model":"deepseek-flash","base_url":"https://api.deepseek.com"}' | Set-Content -Encoding UTF8 var/config.json

# 3) 项目根目录 .env
# AUTOPILOT_API_KEY=sk-xxxx
```

也会自动读取 `~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY`。
任何 OpenAI 兼容接口都能用（DeepSeek / OpenAI / 本地 vLLM / Ollama）。

### 验证环境

```powershell
apilot doctor      # 体检：Python、PowerShell、OCR 语言包、可选依赖、模型连通性
apilot selfcheck   # 逐项自检：截屏、DPI、UIA、OCR、鼠标回环、剪贴板、急停热键
```

输出示例：

```
AutoPilot 自检
  截屏：gdi 后端 [1920, 1200] 耗时 26.5ms
  DPI：per-monitor-v2 120dpi (缩放 1.25x)
  UIA：可用（30 个元素，506ms）
  OCR：可用（引擎 zh-Hans-CN，61 行，904ms）
  鼠标：误差 (0, 0) 正常
  剪贴板：正常
  急停热键：ctrl+alt+q 已注册
  动作数量：62
自检结论：全部正常 ✔
```

---

## 三种用法

### 1. 让 Agent 自主完成（需要 LLM）

```powershell
apilot run "打开计算器算一下 1234 乘 5678" --max-steps 15
apilot run "把 D:\tmp 里的 .txt 文件按修改时间重命名" --allow-dangerous
apilot run "..." --dry-run          # 只报告不执行
apilot run "..." --mode confirm     # 每个有副作用的操作都先问一次
```

### 2. 确定性流程（不需要 LLM）

```powershell
apilot flow list
apilot flow run --file notepad-demo.json
apilot flow run --file morning-routine.json --var 要打开的程序=calc.exe
apilot flow record --output flows/my.json     # 手动做一遍，录下来
```

流程文件就是 JSON，支持 `${变量}`、`${date}`、`${env:VAR}`、`${clipboard}` 占位符：

```json
{
  "name": "早安流程",
  "vars": { "音量档位": "8" },
  "steps": [
    { "action": "notify", "params": { "title": "AutoPilot", "message": "开始准备工作环境" } },
    { "action": "launch_app", "params": { "target": "notepad.exe" } },
    { "action": "screenshot", "params": { "path": "var/shots/morning-${date}.png" } }
  ]
}
```

### 3. 单步调用 / 集成到别的程序

```powershell
apilot actions                          # 列出全部 62 个动作
apilot actions --json                   # 导出 function-calling schema
apilot observe                          # 打印当前屏幕的文本快照
apilot do click element=E7
apilot do screenshot path=var/a.png
apilot do run_command command="ipconfig /all" shell=cmd
```

```python
from autopilot.context import make_context
from autopilot.actions import build_registry
from autopilot.executor import Executor

ctx = make_context(workspace=r"D:\code\autopilot")
ex = Executor(build_registry(ctx), ctx)
print(ex.run("screenshot", {}).to_text())
```

### 4. Web 控制台

```powershell
apilot serve          # 打开 http://127.0.0.1:8787
```

实时画面、Agent 任务下发与实时日志、动作面板（自动生成参数表单）、
流程一键运行、审计记录，右上角一个红色急停按钮。前端是单文件 HTML，不依赖任何 CDN。

---

## 网页自动化：登录态是关键

网页任务最容易翻车的地方不是"点不中按钮"，而是**登录态用错了浏览器**。
AutoPilot 有两种模式，差别就在登录：

| 动作 | 浏览器 | 登录态 | 用在哪 |
| --- | --- | --- | --- |
| `browser_debug` | 带调试端口的 Edge，被 Playwright 接管 | **保存在 `var/browser-profile`，登录一次长期有效** | **需要登录的网站**（考试系统、教务、后台、邮箱…） |
| `browser_open` | 全新临时配置 | 无 | 公开页面 |

需要登录的网站**必须**用 `browser_debug`：

```powershell
python -m autopilot do browser_debug url=https://pintia.cn/...
python -m autopilot do browser_status        # 确认连上了、当前在哪个页面
```

如果页面显示登录页，让 Agent 用 `ask_human` 请你在弹出的窗口里手动登录一次；
登录态写进 profile 后，以后每次都能直接自动操作。

> ⚠️ 网页元素**不在** UIA 元素树里（快照中看不到 `E1` 编号），OCR 坐标还会随滚动整体偏移。
> 所以批量读写网页请走 `browser_eval` 跑 JS，一次求值能顶几十次点击——
> 靠鼠标点浏览器窗口做批量操作几乎必然失败。

`browser_*` 报"受控浏览器已关闭"是正常的（窗口被手动关了或进程崩了）：
直接重新 `browser_debug` 即可，AutoPilot 会自动清掉失效句柄。

### 网页上的代码编辑器

Monaco / CodeMirror / contenteditable 这类编辑器的内容**不在普通输入框里**，
`browser_fill` 和 JS 的 `setValue()` 经常静默失效（表现为"写了但没写进去"）。
必须用真实键盘事件：

```powershell
python -m autopilot do browser_type target=".monaco-editor" text="int main(){}" clear_first=true
python -m autopilot do browser_eval script="monaco.editor.getModels()[0].getValue()"   # 读回确认
```

`method="insert"` 快，不生效就换 `method="type"`（逐键输入，兼容性更好）。

### 题目里的公式是图片怎么办

题面常用 `<img>` 渲染数学公式，DOM 里拿不到文本：

```powershell
python -m autopilot do browser_read_image index=0 zoom=4
```

它会放大图片后截图再做 OCR。**但别在公式上反复折腾**——
`样例输入/输出` 才是判断函数行为的可靠依据（`f(5.00)=2.24` 就说明是 `sqrt(x)`）。

### 想省 token？把"判断"和"执行"拆开

Agent（`apilot run`）每一步都要调一次模型，30 步就是几十万 token。
但**"点哪里、填什么"本身不需要智能**——需要智能的只是"答案是什么"，而那个只需确定一次。

`examples/pta-auto.py` 就是这个思路的完整实现。**只有"答题"那一步花 token，而且是纯文本、一次调用**：

```powershell
python examples/pta-auto.py --dump            # 导出题目到 var/pta/          → 0 token
python examples/pta-auto.py --solve           # 把题目发给模型答一次          → 1 次调用
python examples/pta-auto.py --fill --dry-run  # 演练：只解析要填哪个选项        → 0 token
python examples/pta-auto.py --fill            # 真的填入（不提交）            → 0 token
python examples/pta-auto.py --submit --yes    # 确认无误后再提交              → 0 token
```

`--solve` 之所以便宜，是因为它**不看屏幕、不用工具、不试错**——
题目已经在 `questions.json` 里了，就是个纯文本问答：

| 做法 | 调用次数 | 输入 token | 耗时 |
| --- | --- | --- | --- |
| `apilot run`（Agent 自己看屏幕做） | 30 步 | 未命中缓存约 3~5 万 | 146s |
| `pta-auto.py --solve` | **1 次** | **1619**（实测 10 道题） | 4.4s |

`--solve` 会打印每题的选择和一句话理由，**请你复核后**再 `--fill`；
已有的答案文件会自动备份成 `.json.bak`。

### 读图：公式是图片怎么办

题面里的数学公式基本都是图片，`innerText` 抓不到任何内容——这是"明明看懂了却答错"的
头号原因。现在 `--dump` 按**由便宜到贵**的三级顺序解决：

| 优先级 | 来源 | 耗时 | 说明 |
| --- | --- | --- | --- |
| ① | DOM 里的数学元数据 | **0 ms** | `<img alt>`、`<annotation encoding="…tex">`、MathML；很多站点直接写着 LaTeX |
| ② | 视觉模型，**一题一次** | ~1.2 s | 题干 + 若干选项图一起送，模型还能靠上下文把形近符号读对 |
| ③ | Windows 系统 OCR | ~0.6 s | 只在没有视觉能力时兜底 |

同一批真实题目图片（4 张公式）实测：

| 做法 | 耗时 | 与原文相似度 |
| --- | --- | --- |
| Windows OCR | 2.0 s | **0.45**（`sin²x`→`sin2x`、`dy/dx`→「迦」） |
| 视觉模型，一张图一次 | 3.3 s | 0.95 |
| 视觉模型，一题一次批量 | **1.2 s** | **0.96** |
| 同一批图再读（缓存命中） | **0.02 s** | 0.96（0 token） |

读回来的文本还会再规范化一遍：LaTeX 的 `\frac{dy}{dx}` → `(dy)/(dx)`、`\sqrt{x}` → `√(x)`，
统一 Unicode 减号与上标，并且**把图片内容插回它在题干里的原始位置**（`〔图1: …〕`），
而不是全堆在末尾。识别结果按图片内容哈希缓存在 `var/cache/vision/`，重跑不花钱。

```powershell
python examples/pta-auto.py --dump --jobs 6     # 6 题同时读图
```

### 复核：一次调用换一份保险

`--solve` 之后默认还会用**更强的模型独立重做一遍**，再和初答对比：

```powershell
python examples/pta-auto.py --solve              # 作答 + 复核（默认）
python examples/pta-auto.py --solve --no-check   # 省掉复核这一次调用
python examples/pta-auto.py --solve --trust first # 有分歧时保留初答
```

复核的提示词**刻意不让它先看答案**（先独立解题、再对比），否则它会顺着初答说"对"。
有分歧的题在控制台标 `⚠`，`answers.json` 里写 `conflict: true` 和 `checked`（复核答案），
`--fill` 默认采用复核答案并逐条打印分歧，方便你人工定夺。

还可以本地把编程题跑一遍再提交：

```powershell
python examples/pta-auto.py --verify            # gcc 编译 + 逐组样例比对
```

答案文件支持按序号或按选项文字模糊匹配：

```json
{"answers": [
  {"label": "2-1", "type": "choice", "index": 1},
  {"label": "2-2", "type": "choice", "text": "最近的且不带else"},
  {"label": "7-1", "type": "program", "code": "int main(){...}"}
]}
```

> 提交不可逆，所以脚本**必须**加 `--yes` 才点提交，且不代劳二次确认弹窗。

**关于登录**：PTA 用的是**会话级 cookie**（`PTASession` / `JSESSIONID`），
关掉浏览器就失效。所以脚本会把浏览器保持开着；检测到未登录时会提示你手动登一次。

### 0 token 的三条路

| 路径 | 说明 | token |
| --- | --- | --- |
| `flow run` | 确定性流程，动作序列写死在 JSON 里 | 0 |
| `do <动作>` | 直接执行单个动作 | 0 |
| 自己写脚本调 `autopilot` 的库（如上面的 `pta-auto.py`） | 最灵活 | 0 |
| `run "..."` | Agent 自主决策 | 按步数计费 |

---

### 页面禁粘贴怎么办

考试页常把 `paste` / `beforeinput` 拦掉，表现是**脚本不报错、编辑器却是空的**
（有的还会把非键盘输入回滚）。`--fill` 现在走一条**阶梯**，每一步都读回校验，
不通过才升级，并告诉你最后用的是哪一档：

| 顺序 | 通道 | 原理 | 能过"禁粘贴"吗 |
| --- | --- | --- | --- |
| 1 | `api` | Monaco `model.setValue` / CM6 `view.dispatch` / CM5 / textarea 原型 setter | 能，不产生输入事件 |
| 2 | `insert` | CDP `Input.insertText`（浏览器级文本插入） | 能，不是 paste 事件 |
| 3 | `exec` | `document.execCommand('insertText')` | 看站点 |
| 4 | `type` | 逐键输入，最像真人 | 能，连"检测输入来源"也能过 |

```powershell
python examples/pta-auto.py --fill                     # 自动逐级降级（推荐）
python examples/pta-auto.py --fill --write-method type --typing-delay 10
python examples/pta-auto.py --fill --write-method api   # 最快：直接改框架内部值
```

`apilot` 那条路同理：`browser_type` 默认用 `insert`（CDP 插入），**每次写完都读回校验**，
没写进去就依次升级到 **框架 API**（Monaco / CM6 dispatch）→ **逐键输入**；
三档都不行会直接报失败，并明确禁止模型改用"复制 + Ctrl+V"（这类页面正好拦的就是 paste）。

> 顺带修掉一个会让人误判的坑：contenteditable 会把连续空格变成 NBSP（`\u00a0`），
> 且多行常被折成一行，导致"明明写对了却报读回不一致"。现在读回会按 `<br>` 和
> 块级子元素还原换行、把 NBSP 归一化成空格；逐行比对失败时再退回"忽略空白"的
> 兜底比对，并在结果里注明"排版以编辑器显示为准"。

---

## 安全设计

能操控电脑的程序必须有刹车。AutoPilot 有四层：

| 层 | 机制 |
| --- | --- |
| **风险分级** | `safe` / `low` / `medium` / `high`；HIGH 级（删除、关机、杀进程）默认直接拒绝，要 `--allow-dangerous` |
| **命令黑名单** | 正则拦截格式化、删盘、改注册表、下载即执行、停用安全服务等破坏性命令 |
| **路径保护** | `C:\Windows`、`Program Files`、`ProgramData` 等系统目录禁止写入与删除 |
| **模式开关** | `readonly` 只允许只读动作；`dry_run` 只报告不执行；`confirm` 每步人工确认 |

外加两个随时能按下的刹车：

* 全局热键 **`Ctrl+Alt+Q`**（任何程序在前台都有效）
* 创建 `var/STOP` 文件

每一步动作都会写进 `var/logs/audit-YYYYMMDD.jsonl`，包含参数、结果、耗时、是否被拒绝。

> ⚠️ 自动化会在你的真实桌面上执行。建议先在虚拟机或临时目录里试，
> 不要让 Agent 操作生产系统、支付页面或含敏感数据的窗口。

---

## 工作原理

```
                    ┌──────────────── 感知 ────────────────┐
  屏幕 ──GDI截屏──▶ │ OCR(Windows.Media.Ocr) ──▶ 文字+坐标   │
                    │ UIAutomationClient ─────▶ 元素树 E1..En │
                    └───────────────────┬──────────────────┘
                                        ▼
                          文本快照（窗口 + 元素 + OCR）
                                        ▼
                        LLM（function calling）→ 选一个动作
                                        ▼
                    ┌──────────── 执行 ────────────┐
                    │ 安全护栏 → 急停检查 → 真执行   │
                    │ 鼠标/键盘/窗口/文件/命令/浏览器 │
                    └───────────────┬──────────────┘
                                    ▼
                          审计日志 + 自动重新感知
```

| 模块 | 职责 |
| --- | --- |
| `winapi.py` | DPI 感知、SendInput 鼠标键盘、剪贴板（纯 ctypes） |
| `screen.py` | GDI 截屏、手写 PNG 编码、缩放、画面稳定检测 |
| `windows.py` | 窗口枚举/查找/聚焦/移动，绕过前台窗口锁 |
| `psbridge.py` + `ps/*.ps1` | PowerShell 5.1 桥：UIA 元素树、系统 OCR、Toast |
| `perceive.py` | 组装快照、元素编号、渲染成 LLM 可读文本 |
| `registry.py` / `actions.py` | 62 个动作的声明与实现 |
| `guard.py` / `executor.py` | 安全判定、执行、审计、自动重感知 |
| `killswitch.py` | 全局急停热键与停止文件 |
| `llm.py` / `agent.py` | OpenAI 兼容客户端 + 观察-决策-执行循环 |
| `flows.py` | 确定性流程执行器 + 低级钩子录制器 |
| `server.py` + `static/` | Web 控制台 |

### 踩过的坑（都已修复，写在代码注释里）

* **DPI 缩放**：物理 1920×1200 / 逻辑 1536×960（125%），不设 DPI 感知则点击坐标全错。现在坐标零误差。
* **`argtypes` 必须声明**：64 位下 ctypes 默认按 `c_int` 传参，GDI 句柄会溢出、`CallNextHookEx` 的 `lParam` 会炸掉录制器。
* **`WM_CLOSE` 会静默丢数据**：Win11 记事本收到 `PostMessage(WM_CLOSE)` 会直接退出、不弹保存确认。改成默认走 Alt+F4 优雅关闭。
* **浏览器读不到元素树**：Edge/Chrome 默认不暴露无障碍树，加 `--force-renderer-accessibility` 后可读到 200+ 元素。
* **容器抢占 E1**：占满屏幕的 Pane 会排在按钮前面，现在纯布局容器会被沉到列表末尾。
* **`stream()` 忘了写 `yield`**：函数退化成返回 `None`，任务跑完了却在收尾时崩溃。已有回归测试守着。
* **`hotkey()` 漏发主键 keydown**：只发了 keyup，于是 `Ctrl+A` / `Ctrl+S` / `Ctrl+V` 全部**静默失效**——
  不报错、什么也不做，最难查的一类 bug。现在 `_hotkey_plan()` 是纯函数，有 5 条测试守着这个不变量。
* **FastAPI + `from __future__ import annotations`**：函数内定义的 Pydantic 模型注解变成字符串后解析不到，
  接口会莫名返回 422。`server.py` 因此不使用 future import，请求体直接手工解析 JSON。
* **参数声明与函数签名不一致**：会在任务跑到一半才炸，现在 `Registry.register` 就校验。
* **修饰键的虚拟键码是左右分开的**：低级键盘钩子报的是 `0xA0`（左 Shift）/`0xA2`（左 Ctrl），
  不是通用的 `0x10`/`0x11`。只认后者的话 Shift/Ctrl/Alt 会被整段丢掉，
  组合键还会被录成 `ctrl+vk_a2` 这种没法回放的垃圾。
* **未知键名必须能往返**：录制器输出 `vk_0x87`，而 `resolve_key` 原先按十进制解析（= `0x57`，完全是另一个键）。
  现在有专门的往返测试。
* **"句柄还在"≠"进程还活着"**：浏览器被外部关掉后，`ctx.browser["page"]` 仍然非空，
  于是每个 `browser_*` 都秒报 `TargetClosedError`，Agent 却以为"浏览器在运行"，
  把剩余步数全烧光。现在统一走 `_browser_alive` / `_browser_call`：识别失效句柄、
  清掉状态、给出"请重新 browser_debug"的可读指引。有真实复现该场景的测试。
* **登录态用错浏览器**：`browser_open` 开的是全新配置（未登录），
  拿它操作需要登录的网站等于白忙。为此新增 `browser_debug`——
  用独立 profile 起带调试端口的 Edge，Playwright 通过 CDP 接管，登录一次长期有效。
* **同一个 profile 只能被一个实例占用**：`browser_debug` 起的 Edge 占着配置目录时，
  再 `browser_open` 只会得到一句毫不相关的 `Target page, context or browser has been closed`。
  现在会先枚举进程命令行，提前给出准确原因和两个可选做法。
* **网页代码编辑器写不进去**：Monaco/CodeMirror 的内容不在隐藏 textarea 里，
  `browser_fill` / `setValue()` 静默失效。新增 `browser_type`（真实键盘事件），
  并强制要求"写完必须读回确认"。
* **公式是图片，DOM 读不到**：新增 `browser_read_image`（放大后截图 + OCR）。
  提示词里同时写明"别在图片上反复折腾，用样例输入输出反推"——
  这是实测中烧掉最多步数的坑。
* **题干只要 260 字符**：`--dump` 原来只存 `head`（前 260 字符），长题的
  "输出格式/数据范围"全被切掉，编程题大面积格式错、单选题按残缺条件硬答。
  现在存 `blocks`（文字/公式/代码/图片按 DOM 顺序）与全文题干。
* **OCR 把公式读成另一个东西**：`sin²x`→`sin2x`、`dy/dx`→「迦」，
  这不是"可能错"而是必错。改成 DOM 元数据 → 视觉模型（一题一次批量）→ OCR 兜底，
  实测相似度 0.45 → 0.96，速度还快 1.6 倍。
* **思考模式吃光 `max_tokens`**：给单题 1024 token 时，推理把额度用完、`content`
  返回空串，`--solve` 直接报"模型没有返回 JSON：（空）"。现在自动关掉思考重发一次。


---

## 测试

```powershell
python -m pytest tests/ -q                  # 249 项
python -m pytest tests/ -q -m "not hardware" # 跳过会短暂移动鼠标的硬件测试
```

覆盖：安全护栏（25 个破坏性命令用例）、注册表 schema、执行器（拦截/演练/审计/急停）、
感知层（元素编号/查询/渲染/OCR 清洗）、Agent 循环（含回归测试）、流程与录制器、
底层能力（DPI/坐标/截屏/窗口/剪贴板/UIA/OCR）、LLM 客户端。

其中录制器有完整闭环测试：**真机装钩子 → 录到真实按键 → 存盘 → 读回 → 演练回放**，
保证"手动做一遍就能重放"。测试用 F24 这类任何程序都没绑定的键，完全无副作用。

测试原则：**不干扰使用者**——只需要翻译逻辑的用例直接喂内部回调，
必须碰真实硬件的用例单独标记为 `hardware`，且在检测到有人正在用鼠标时跳过而不是误报失败。

---

## 常见问题

**Agent 点不中目标？**
先用 `apilot observe` 看快照。如果目标元素不在列表里，说明那个应用没暴露 UIA 树，
改用 OCR 文字定位或 `browser_*` 系列动作。

**浏览器里什么都读不到？**
用 `browser_debug`（带登录态，推荐）或 `browser_open`（全新无登录）；
单纯让 `launch_app` 打开浏览器会自动加 `--force-renderer-accessibility`，
但那条路只适合"人看着点"，不适合 Agent 批量操作。

**`python -m autopilot` 报"找不到模块"？**
你的 Python 可能是嵌入式发行版（目录里有 `python312._pth`，会开启隔离模式，
忽略当前目录和 `PYTHONPATH`）。用 `python run.py` 或先 `pip install -e .`。

**OCR 识别不了中文？**
系统需要装中文语言包：设置 → 时间和语言 → 语言和区域 → 添加中文。
`apilot doctor` 会告诉你当前装了哪些 OCR 语言。

**担心它乱来？**
默认就拦住了所有 HIGH 风险动作。再加 `--dry-run` 先看它打算做什么，
`--mode confirm` 让它每步都问你。

---

## 许可

MIT
