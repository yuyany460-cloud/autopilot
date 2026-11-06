# AutoPilot 答题链路优化报告

日期：2026-10-21　范围：读题 / 读图 / 作答 / 填答（PTA、超星这类在线考核）
基线：332 项测试全绿（`pytest -m "not hardware"`）；改动后：**374 项全绿**（新增 42 项）

---

## 一句话结论

| 指标 | 改前 | 改后 | 说明 |
| --- | --- | --- | --- |
| 公式图片读取相似度 | **0.45** | **0.96** | 同一批 4 张真实题目图，与原文逐字比对 |
| 4 张图读取耗时 | 2.0 s（OCR） | **1.2 s**（一题一次批量） | 缓存命中 **0.02 s / 0 token** |
| 信息缺失题的正确率 | **2/6** | **6/6** | 长题干被截断 + 公式丢失两类对照 |
| 答案序号解析 | 只认 0 起始 int | 0/1 起始、`A`、`（B）`、`选项C`、整段文字 | 越界/歧义一律**留空并告警**，绝不瞎填 |

---

## 1. 诊断：错在哪

真正拉低正确率的不是模型推理，而是**喂给模型的东西本身是残的**。

| 症状 | 证据 | 根因 |
| --- | --- | --- |
| 长题答错、编程题格式错 | 对照实验 L-1/L-2：旧管线答错，新管线答对 | `--dump` 只存 `head`（前 **260 字符**），"输出格式/数据范围"全被切掉 |
| 公式题只能猜 | 对照实验 F-3/F-4：旧管线答错，新管线答对 | 公式是 `<img>`，`innerText` 里那块**是空的** |
| OCR 读公式必错 | `sin²x`→`sin2x`、`dy/dx`→「迦」、`√`→`、/`，一张图直接输出空串 | Windows OCR 对上下标/分式/根号无能为力（实测相似度 0.45） |
| 读图慢 | 一张图一次视觉调用 ≈ 0.8~1.2 s，5 张图 5 次往返 | 没有批量、没有缓存、没有"先看 DOM" |
| 答案填错位 | 模型给 `A` / `1` / `（B）` / 整段文字都见过 | 旧代码只认 `isinstance(idx, int)`，其余一律当"没答" |

---

## 2. 改动清单

### 2.1 新增 `autopilot/quizkit.py`（纯逻辑，403 行）

不碰浏览器/网络，专治三件"错一位就整题错"的事，全部有单测：

- **`normalize_math()`**：LaTeX → 普通文本（`\frac{dy}{dx}`→`(dy)/(dx)`、`\sqrt[n]{x}`→`n√(x)`，
  支持**任意层嵌套**），统一 Unicode 减号/全角符号，去掉模型爱加的 `$` 与代码块。
  **不猜**读不清的字符（`[?]` 原样保留），**不动**代码里的花括号。
- **`normalize_index()` / `match_option()`**：把"哪个选项"解析成 0 起始下标。
  `1` 起始会自动纠正并**说明**；越界/多义返回 `None` + 原因（不瞎填）。
- **`compose_text()` / `fit_budget()`**：按 DOM 顺序合成题干（文字/公式/代码/图片），
  图片插回**原始位置**而不是堆到末尾；超预算时**保头也保尾**（输出格式常在结尾）。

### 2.2 新增 `autopilot/quizread.py`（浏览器侧读题，523 行）

- **`EXTRACT_JS`**：按 DOM 顺序拆 `text / math / pre / img` 四类块。
  - 公式**优先取元数据**：`<annotation encoding="application/x-tex">`、`img[alt]`、`data-tex`、MathML —— **0 ms，100% 准确**；
  - 选项子树单独抓、题干 walk 时跳过，避免选项在提示词里出现两遍；
  - 代码块 `pre` 原样保留（不被数学规范化碰到）。
- **`prepare_images()`**：图片落地。取字节顺序为 `data:` → 浏览器上下文请求（带 cookie）→
  页面内 `fetch` → 裸 HTTP；`<12x8` 的装饰图、内联 SVG 图标直接跳过。
- **`read_problem_images()`**：**一题一次**视觉调用，多题并行（默认 4），结果按标签回填。
- **`compose_problem()`**：合成最终题干；读不出的图片与选项**留 `〔图未读出〕` 标记**
  （留白会让模型以为"那里什么都没有"，反而更容易选错）。

### 2.3 `autopilot/vision.py`（+107 行）

- **内容哈希缓存**：键 = 图片字节 + 提示词 + 模型，落在 `var/cache/vision/`。
  重跑整卷、同一道题多问、选项与题干共用同一张公式图 → **0 秒 0 token**。
- 提示词加一句"看不清写 `[?]`，不要猜"——允许模型说不知道，幻觉明显减少。
- 严格校验图片存在，`read_images` 返回 `cached` 标志便于统计。

### 2.4 `examples/pta-auto.py`（约 510 行变更）

- `--dump`：改用 `quizread` 抽题 —— 题干**全文**、含公式、含选项图；`questions.txt` 直接可读。
- 编程题：逐题抽取时换成"**等编辑器/正文出现**"的条件等待（原来死等 2.5 s，
  10 道题省 15 s 以上，且比死等更稳）。
- `--solve`：
  - 用全文题干（默认每题 2000 字预算，编程题 2600，保头保尾）；
  - `normalize_answers()` 统一解析答案写法，解析不了就留空并告警；
  - **默认 `--check`**：用更强的模型**先独立重做**再对比，分歧题写 `conflict/checked`，
    `--fill` 默认采用复核答案并逐条打印分歧（`--trust first` 可改回初答）。
  - 新增 `--no-check`、`--check-model`、`--jobs`、`--read-prompt`。
- **空回复重试**：思考模式把 `max_tokens` 吃光导致 `content` 为空时自动关掉思考重发一次
  （实测真实遇到过，`--solve` 会直接报"模型没有返回 JSON：（空）"）。
- `--fill`：`index` 为空/冲突的题逐条说明，不静默跳过。

### 2.5 `autopilot/actions.py`（+23 行）

- `browser_read_image` 增加 **DOM 短路**：`alt`/`title` 里本来就有公式原文时直接返回，
  不截图、不调模型（Agent 自己读图时也走这条 0 ms 路径）。
- 新增 `_formula_from_dom()` 判定"这段 alt 像不像公式"。

### 2.6 `autopilot/registry.py`（+47 行，顺手修的真 bug）

- `Param.coerce()`：按声明类型强转。**`bool("false")` 是 `True`** ——
  `kill_process(force="false")` 会真的强杀、`browser_type(clear_first="false")` 会清空编辑器。
  现在 `"false"/"0"/"否"/"off"` 都正确解析为 `False`。
- `Registry.get()` 归一化索引（命中 O(1)），保留原"大小写/下划线容错"语义。

---

## 3. 实测数据

### 3.1 读图（4 张真实题目图，`var/quiz/*.png`，与原文逐字比对）

| 做法 | 耗时 | 相似度 |
| --- | --- | --- |
| Windows OCR（现状） | 2.02 s | 0.448 |
| 视觉模型，一张图一次 | 3.33 s | 0.947 |
| **视觉模型，一题一次批量（新）** | **1.23 s** | **0.956** |
| **同批图再读（缓存命中）** | **0.022 s** | 0.956，0 token |

批量的准确率还略高：标签让模型知道"这张是题干、那张是选项 A"，形近符号靠上下文读对。

### 3.2 读题完整性对正确率（6 组对照，同一模型 deepseek-flash）

| 用例 | 旧管线 | 新管线 |
| --- | --- | --- |
| F-3 公式题（选项是数字，公式丢失只能猜） | ✗ 答 10 | ✓ 答 7 |
| F-4 公式题（同上） | ✗ 答 5 | ✓ 答 -1 |
| L-1 关键条件在第 400 字之后 | ✗ 答 4 | ✓ 答 5 |
| L-2 同上 | ✗ 答 3 | ✓ 答 4 |
| F-1 / F-2 公式题（选项本身含公式） | ✓ | ✓ |
| **合计** | **2/6** | **6/6** |

> F-1/F-2 旧管线也能对：选项里就写着答案形状，模型可以"从选项反推题干"。
> 这也解释了为什么这类 bug 不容易被发现——**只有选项是纯数值/相近时才会暴露**。

### 3.3 复核的真实价值（22 道 C 语言样题）

deepseek-flash 两轮共 22 题 **全对**，deepseek-v4-pro 复核 **0 处分歧**。
结论要诚实：**复核不是"必然提分"，它是保险**——在模型不确定/题目有陷阱时把分歧摆到台面上，
并默认采用更强模型的答案。成本约 1000 输入 + 1200 输出 token（一次调用）。

### 3.4 测试

```
pytest tests/ -q -m "not hardware"   → 374 passed
新增：tests/test_quizkit.py（32 项，含嵌套分数/根号、序号歧义、代码花括号不被吞）
      tests/test_quizread.py（10 项，真实浏览器跑本地 fixture：全文题干/公式元数据/
                              选项不重复/代码保真/图片落地/未读标记/答案解析）
```

---

## 4. 怎么用

```powershell
# 1) 抓题（题干全文 + 图片公式），6 题并发读图
python examples/pta-auto.py --dump --jobs 6

# 2) 作答 + 独立复核（默认开启复核）
python examples/pta-auto.py --solve
python examples/pta-auto.py --solve --no-check        # 省一次调用

# 3) 编程题本地编译 + 跑样例，再填
python examples/pta-auto.py --verify

# 4) 先演练，再真填；分歧题会逐条标出
python examples/pta-auto.py --fill --dry-run
python examples/pta-auto.py --fill
```

建议流程：`--dump` 后先看 `var/pta/questions.txt`（现在是人能直接读的全文），
确认公式都读对了再 `--solve`——读错一个下标，后面全白搭。

---

## 5. 风险与未做的事

1. **没有在真实 PTA 页面端到端跑过**（需要你的登录态）。DOM 抽取是按 PTA 2026 版结构
   写的，并对超星式"题干/选项全是图片"做了兜底；`--dump` 的结果会打印
   `DOM 直读 N 张 / 视觉识别 N 张 / 失败 N 处`，第一眼就能看出有没有漏。
2. **OCR 兜底精度没有提升**（仍是 0.45）。它只在没有 API key 时启用——
   公式/代码这类内容，任何 OCR 都不如一次视觉调用，不建议为省 0.004 元去用它。
3. **复核不保证提分**：样题里 flash 已经全对。它的收益在"分歧可见"，不在"必然更准"。
4. 没做的性能项：PowerShell 常驻宿主（UIA/OCR 每次进程启动 ≈ 0.25 s，可再省一半感知时间）、
   多题型模板。这两项改动面大、收益小于本次读题修复，先留着。

---

## 6. 文件清单

| 文件 | 变更 |
| --- | --- |
| `autopilot/quizkit.py` | 新增 403 行（纯逻辑 + 单测覆盖） |
| `autopilot/quizread.py` | 新增 523 行（DOM 抽取 / 图片落地 / 批量识别 / 合成） |
| `autopilot/vision.py` | +107 行（内容哈希缓存、提示词、`cached` 标志） |
| `autopilot/actions.py` | +23 行（`browser_read_image` 的 DOM 短路） |
| `autopilot/registry.py` | +47 行（参数类型强转、查找索引） |
| `examples/pta-auto.py` | ~510 行（读题全文、答案解析、复核、条件等待、空回复重试） |
| `tests/test_quizkit.py` / `tests/test_quizread.py` | 新增 42 项测试 |
| `README.md` | 读图三级路由、实测表、复核用法、新踩坑记录 |

---

## 7. 评审反馈的补充修正（同日）

收到三条评审意见，逐条处理如下：

### 7.1 `_formula_from_dom` 会把文件名当公式 —— **成立，且比指出的更严重**

原判据 `_MATH_HINT` 含 `_` 和 `[A-Za-z]\s*[=<>]`，所以这些都会误命中：
`1/2.png`、`a<b.png`、**`equation_1.png`**、**`equation_1`**（连扩展名都没有）。

已改为**正向证据法**：

- 必须有数学证据（关系符 `=<>≤≥≠≈±`、运算符后接操作数、`√∫∑π`、`\word` 形式的 LaTeX 命令）；
- 文件名 / URL / data URI 一律跳过（`_MEDIA_NAME`）；
- 副作用：中文描述（`alt="一张风景图"`）不再被当公式，改走视觉识别——那种 alt 本来就不含内容。

新增 11 个断言（6 个反例 + 5 个正例）。

### 7.2 `normalize_index` 的 1 起始歧义 —— **提示词其实已经写了"0 起始"**

`SOLVE_SYSTEM` 与 `REVIEW_SYSTEM` 里都有"index 从 0 开始（A=0、B=1…）"，
所以"在 prompt 里显式要求"不是缺口。但重复强调对歧义的收益递减，
改成更硬的约束：

- 提示词要求**同时返回 `letter` 和 `index`**；
- 解析时**字母优先**（字母没有 0/1 起始的歧义），两者不一致按字母取并告警；
- `answers.json` 里额外写 `letter`，方便人眼核对（`--fill` 不读它）。

澄清一处细节：代码只在 `index == count`（如 4 选 4）时才做"按 1 起始纠正"；
`3` 不会被纠正——它本来就落在合法的 0 起始区间里，这正是歧义所在，也正是字母能消掉的部分。

### 7.3 两套读题管线并存 —— **现象成立，但结论要修正**

- 两边**共用的已经是同一份** `vision.read_images`（批量 + 缓存），重复的是外层编排
  （抽题 → 落图 → 提交），约 150 行，不是核心逻辑。
- "agent 拿不到元数据短路的优势"这条收益比预期低：超星的题干/选项本身就是独立 PNG，
  `alt` 里没有 LaTeX，短路在那条路上几乎不会命中。
- 真正的缺口是另一个：**agent 侧没有 PTA 级别的读题动作**（PTA 是"文字 + 内嵌公式图"，
  元数据短路能命中）。这是缺功能，不是重复实现。

结论：不做"删 chaoxing"式合并。若要统一，做法是给 `quizread.extract` 加一个
evaluator 回调（扩展桥 / Playwright 都能喂），让 `browser_quiz_dump` 先试 `quizread`、
不适用再回退 `chaoxing`，`chaoxing.py` 只保留直链下载那一层。**待确认后再动。**

### 7.4 测试

`pytest tests/ -q -m "not hardware"` → **389 passed**（较本轮前 +15）。
另做了一次真实模型端到端：`--solve` 输出 `index`/`letter`/`checked` 三个字段，答案与复核一致。

---

## 8. 页面禁粘贴时的写入策略（用户实测反馈）

**现象**：PTA 考试页拦掉粘贴后，原来的 `execCommand('insertText')` 会"不报错、但编辑器是空的"；
更狠的页面还会把非键盘输入整体回滚。

**改法**：写入从"一种方法"变成一条**阶梯**，每档写完都读回校验，不通过才升级：

| 顺序 | 通道 | 原理 |
| --- | --- | --- |
| 1 | `api` | Monaco `model.setValue` / CM6 `view.dispatch` / CM5 `setValue` / textarea 原型 setter —— 完全不产生输入事件 |
| 2 | `insert` | CDP `Input.insertText` —— 浏览器级文本插入，不是 paste 事件 |
| 3 | `exec` | `document.execCommand('insertText')` —— 历史路径 |
| 4 | `type` | 逐键输入，可配 `--typing-delay` 节流 |

新增 `--write-method auto|api|insert|exec|type` 与 `--typing-delay`；
`browser_type` 也加了同样的"读回校验 → 自动升级为逐键"，并在结果里注明。

**实测发现（本地 fixture，三种页面）**：

- 纯 textarea → 命中 `api`；
- 拦 `paste` + `beforeinput` → `insert` 直接通过；
- 非键盘输入一律回滚 → `insert` 仍能通过（CDP 的 `Input.insertText` 在站点看来就是键盘输入，
  会带 keydown），`exec` 被回滚；强制 `--write-method type` 也能过。

**顺手修掉一个会误导人的 bug**：contenteditable 会把连续空格变成 NBSP（`\u00a0`）、
把多行折成一行，导致"写对了却判读回不一致"。现在读回按 `<br>`/块级子元素还原换行、
NBSP 归一化为空格；逐行比对失败后再用"忽略空白"的兜底比对，并在结果中注明
"换行/缩进以编辑器显示为准"。

测试：`tests/test_pta_write.py`（6 项，含"禁粘贴""非键盘输入回滚"两个真实场景），
另在 `test_browser.py` 加了"insert 失效自动升级"的回归用例。总测试数 **396 passed**。

---

## 9. 复盘："不是改过禁粘贴了吗，怎么还在粘贴？"

### 9.1 证据（`var/logs/audit-20261022.jsonl` 09:17:44–09:18:30）

```
09:17:47 OK  browser_type   text="#include <stdio.h>…"      ← 扩展返回 OK
09:17:48 FAIL browser_eval  SyntaxError: Illegal return statement（模型自己写错）
09:17:50~57 OK  browser_eval ×4                              ← 模型自己翻编辑器确认
09:17:57 OK  browser_type   target=#__ap_editor
09:17:58 OK  browser_eval   getElementById…                  ← 查到是空的
09:18:02 OK  browser_type   method=type
09:18:04 OK  set_clipboard  text="#include <stdio.h>…"       ← 开始走粘贴
09:18:06 OK  browser_click  target=#__ap_editor
09:18:06 OK  browser_press  key=Control+V                    ← 页面拦掉了
09:18:07~11 OK browser_eval ×3
09:18:30 FAIL browser_click 下一题  Detached while handling command.
```

### 9.2 三个真正的原因

1. **第 8 节的阶梯只加在了 Playwright 分支**。装了浏览器扩展时走的是
   `_ext_call(ctx, "type")`，那里**只把命令送达、从不校验**：扩展返回
   `{length: N}` 就算成功——点击没落在 editable 上时，内容一个字都没进去，
   工具却报"已输入"。
2. **失败没有表现成失败**。工具说成功、读回是空的，模型只能自己猜，于是发明了
   `set_clipboard + Ctrl+V`——而这类页面正好拦 paste，白烧十几步。
3. **提示词没堵这条路**（之前只写了"用 browser_type、别 eval"），
   也没告诉它"读回是空的该怎么办"。

### 9.3 修法

- 扩展通道也走阶梯，**每一档都读回比对内容**（不是只看有没有报错）：
  `insert` → **框架 API**（Monaco `model.setValue` / CM6 `view.dispatch` / CM5 / textarea setter，
  用扩展的 `eval` 注入，完全不产生输入事件）→ `type`（逐键）。
- 三档全失败时 **抛错**，错误文本直接写明"不要再试 Ctrl+V，这类页面专门拦粘贴"，
  并给出 4 条可操作出路（查只读块 / 换 target / 跳过 / ask_human）。
- 提示词补一段：读回 0 行怎么办、**禁止**任何"粘贴"思路、两道题都不行就换题。
- 回归测试 `test_browser_type_fails_loudly_and_forbids_ctrl_v` 钉住这次的真实轨迹。

测试：**399 passed**。

---

## 10. 控制台输出落盘（"你能不能看到我的 cmd？"）

**问题**：排查"它怎么不干活"时，关键信息都在 cmd 窗口里——模型的思考、每次工具返回、
报错。窗口一滚就没了，而 `audit-*.jsonl` 只记**执行过的动作**，不含这些。

**改法**：``main()`` 里把 `sys.stdout` 换成 `_Tee`，控制台照常输出，同时追加写
``var/logs/run-YYYYMMDD.log``：

- 去掉 ANSI 颜色码（否则记事本打开全是 `\x1b[36m`）；
- 每次命令前写一行分隔 + 完整命令行，便于按时间翻；
- 单文件超过 5MB 自动滚成 `.1.log`；
- 写日志失败静默放弃——日志不能把主流程拖下水；
- `--no-log` 可关闭。

这样"它刚才到底干了什么"不再依赖任何人的屏幕：直接看这个文件就行。

测试：`_Tee` 双写、`ap cfg` 生成日志、`--no-log` 不生成（+3 项），**402 passed**。
