# -*- coding: utf-8 -*-
"""自主 Agent 循环：观察 → 决策 → 执行 → 再观察。

循环的每一步：

1. 取一份屏幕快照（文本形式：窗口 + 元素树 + OCR）
2. 把快照和历史发给 LLM，附带全部可用动作的 function-calling schema
3. LLM 选择**一个**动作并给出参数
4. 执行器做安全审查后真正执行，并自动采集新快照
5. 把结果和新快照回灌给 LLM，进入下一轮

直到模型调用 ``finish``、步数用尽、或急停被触发。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from . import prompts
from .context import ActionContext
from .executor import ActionResult, Executor
from .llm import LLMClient, LLMError, LLMResponse
from .perceive import Snapshot

# 同一个动作 + 同样的参数，连续重复到这个次数就直接熔断不执行。
# 取 3 是权衡：给正常的"重试一次"留余地，又不至于让它把步数烧光。
MAX_IDENTICAL_REPEAT = 3

# 连续多少步"界面读不出任何元素"就警告 / 直接禁止点击。
# 真实教训：UU加速器这类自绘界面没有 UIA 元素、OCR 也读不出字，
# 模型就按坐标瞎点，37 步全部 ok 但零进展。软提示没用，必须硬拦。
BLIND_WARN_STEPS = 4
BLIND_BLOCK_STEPS = 8

# 这些动作是"按坐标/元素点"，在瞎点状态下最该被拦住
CLICK_ACTIONS = {"click", "double_click", "right_click", "drag", "scroll",
                 "move_mouse", "browser_click"}

# 对话历史的大小上限（按字符粗略折算，中英混排约 1.9 字符 = 1 token）。
#
# 为什么值得这么较真：DeepSeek 的输入计费是
#     缓存命中 0.04 元/百万 token   vs   未命中 2 元/百万 token   （高峰时段）
# 差 **50 倍**。也就是说 1 个未命中 token 顶 50 个命中 token。
# 一次裁剪会瞬间让整段历史变成未命中，所以裁剪必须"攒够了再一次丢到位"。
#
# 取值依据（按上面单价做的优化）：设每次裁剪后上下文为 C0、每步新增 r，
# 一个周期 K 步的总成本 ≈ 0.04·K·(C0 + Kr/2) + 2·(C0 - 9.5k)，
# 对 K 求极小得 K = √(100·(C0-9.5k)/r)。C0 越小越省，但太小会丢掉任务上下文，
# 所以取"预算的 30%"作为落点，实际大约每 30~40 步裁一次。
#
# 顺带一提：模型上下文有 1M，这里卡住不是为了不超限，纯粹是为了省钱和提速。
CONTEXT_CHAR_BUDGET = 90_000
CONTEXT_TRIM_TARGET = 0.30


def _history_chars(messages: list[dict[str, Any]]) -> int:
    """粗略估算一段对话的字符数，用来判断要不要裁剪。"""
    total = 0
    for msg in messages:
        content = msg.get("content")
        total += len(content) if isinstance(content, str) else 0
        calls = msg.get("tool_calls")
        if calls:
            total += len(json.dumps(calls, ensure_ascii=False))
    return total


def _flatten(rounds: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [msg for group in rounds for msg in group]


@dataclass
class AgentConfig:
    """Agent 运行参数。"""

    goal: str = ""
    max_steps: int = 100
    allow_danger: bool = False
    temperature: float = 0.0
    observe_every_step: bool = True
    include_screenshot: bool = False   # 每步附上截图（deepseek-flash 支持看图）
    extra_rules: str = ""
    max_observation_chars: int = 5000
    keep_recent_observations: int = 5
    save_initial_screenshot: bool = True
    step_pause: float = 0.0            # 每步之间的强制停顿，便于人观察
    system_prompt: str = prompts.SYSTEM_PROMPT


@dataclass
class AgentRun:
    """一次任务的完整结果。"""

    goal: str
    ok: bool = False
    success: bool = False
    summary: str = ""
    steps: int = 0
    stopped_reason: str = ""
    needs_human: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    transcript: list[dict[str, Any]] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal, "ok": self.ok, "success": self.success,
            "summary": self.summary, "steps": self.steps,
            "stopped_reason": self.stopped_reason, "needs_human": self.needs_human,
            "duration_s": round(self.finished_at - self.started_at, 1),
            "actions": self.actions, "usage": self.usage,
        }


class Agent:
    """把 LLM、执行器和感知层串起来的主循环。"""

    def __init__(self, llm: LLMClient, executor: Executor, ctx: ActionContext,
                 config: AgentConfig,
                 log: Callable[[str, str], None] | None = None) -> None:
        self.llm = llm
        self.executor = executor
        self.ctx = ctx
        self.config = config
        self._log = log or ctx.log

    # -- 事件流 ----------------------------------------------------------
    def stream(self) -> Iterator[dict[str, Any]]:
        """执行任务并逐步 ``yield`` 事件（CLI 和 Web 都用它做实时输出）。

        这是一个真正的生成器：每产生一个事件就 ``yield`` 一次，
        调用方逐步消费即可实时看到 Agent 的思考和动作。
        订阅者（``subscribe``）仍然会在事件产生的**当下**收到通知，
        不受消费速度影响。
        """
        cfg = self.config
        run = AgentRun(goal=cfg.goal, started_at=time.time())
        yield self._emit("start", goal=cfg.goal, max_steps=cfg.max_steps,
                         model=self.llm.config.model)

        # ---- 初始观察 ----
        try:
            snapshot = self.ctx.take_snapshot(
                include_uia=True, include_ocr=True,
                save_image=cfg.save_initial_screenshot)
        except Exception as exc:
            self._log("warn", f"初始感知失败：{exc}")
            snapshot = None

        from .perceive import quick_context
        try:
            context = quick_context()
        except Exception:
            context = ""

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": cfg.system_prompt},
            {"role": "user", "content": prompts.build_goal_message(
                cfg.goal, snapshot, context, cfg.extra_rules)},
        ]
        if snapshot is not None:
            yield self._emit("observation", text=snapshot.render(), step=0,
                             snapshot=snapshot.as_dict())

        tools = self.executor.registry.openai_tools(allow_danger=cfg.allow_danger)
        yield self._emit("tools", count=len(tools),
                         names=[t["function"]["name"] for t in tools])

        last_error = ""
        finish_info: dict[str, Any] | None = None
        stopped = ""
        # 重复动作熔断用的状态（每次 run 重置）
        self._last_signature: tuple[str, str] | None = None
        self._repeat_run = 0
        # "瞎点"计数：连续多少步界面读不出任何元素
        self._blind_run = 0
        if snapshot is not None and not snapshot.elements:
            self._blind_run = 1

        for step in range(1, cfg.max_steps + 1):
            if self.ctx.killswitch.triggered:
                stopped = f"急停：{self.ctx.killswitch.reason}"
                yield self._emit("stopped", reason=stopped)
                break

            remaining = cfg.max_steps - step
            blind = self._blind_run
            if blind >= BLIND_WARN_STEPS:
                self._log("warn", f"👁 连续 {blind} 步读不到界面元素，已提醒模型换打法")
                yield self._emit("blind", step=step, count=blind)
            messages = self._trim_history(messages, cfg.keep_recent_observations)
            messages.append({"role": "user",
                             "content": prompts.build_step_hint(
                                 step, cfg.max_steps, remaining, last_error, blind)})
            last_error = ""

            # ---- 问模型 ----
            yield self._emit("thinking", step=step)
            try:
                response = self._ask(messages, tools, snapshot)
            except LLMError as exc:
                stopped = f"调用模型失败：{exc}"
                yield self._emit("error", message=str(exc))
                break

            # 累计 token 用量。前缀缓存命中与否直接决定实际花费，
            # 所以把 命中/未命中 也分开记下来——不然只看 prompt_tokens
            # 会严重高估成本（系统提示词 + 工具 schema 每次都一样，是最容易被缓存的）。
            for key in ("prompt_tokens", "completion_tokens",
                        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"):
                value = int(response.usage.get(key) or 0)
                if value:
                    run.usage[key] = run.usage.get(key, 0) + value
            yield self._emit("llm", step=step, content=response.content,
                             reasoning=response.reasoning[:600],
                             tool_calls=[{"name": c.name, "arguments": c.arguments}
                                         for c in response.tool_calls],
                             ms=response.elapsed_ms)

            # ---- 模型没给动作 ----
            if not response.tool_calls:
                text = (response.content or "").strip()
                if step >= cfg.max_steps or remaining <= 0:
                    run.summary = text or "达到最大步数，任务未确认完成"
                    stopped = "达到最大步数"
                    break
                messages.append({"role": "assistant", "content": text or "(空)"})
                last_error = ("你上一轮没有调用任何动作。请调用一个动作来推进任务，"
                              "完成后调用 finish。")
                yield self._emit("narration", text=text)
                continue

            # ---- 执行动作（只执行第一个，保证状态推演可控）----
            messages.append({
                "role": "assistant",
                "content": response.content or None,
                "tool_calls": [c.as_message_part() for c in response.tool_calls],
            })

            primary = response.tool_calls[0]
            yield self._emit("action", step=step, name=primary.name,
                             params=primary.arguments)

            # ---- 重复动作熔断 ----
            # 提示词里已经写了"不要重复调用同一个失败的动作"，但那是软约束。
            # 真实教训：读一张公式图片反复折腾，把 30 步预算烧掉一大半。
            # 这里做硬拦截：同一个动作 + 同样的参数连续第 4 次就直接不执行。
            signature = (primary.name,
                         json.dumps(primary.arguments, sort_keys=True, ensure_ascii=False))
            if signature == self._last_signature:
                self._repeat_run += 1
            else:
                self._last_signature, self._repeat_run = signature, 1

            if self._repeat_run > MAX_IDENTICAL_REPEAT:
                result = ActionResult(
                    action=primary.name, params=primary.arguments, ok=False,
                    denied=True, reason="重复动作熔断",
                    error=(f"动作 {primary.name} 用完全相同的参数已经连续执行了 "
                           f"{MAX_IDENTICAL_REPEAT} 次仍未推进任务，已阻止继续重复。"
                           f"必须换一个动作、换参数、或改用别的方法；"
                           f"实在做不下去就调用 finish 说明卡在哪里。"))
                self._log("warn", f"⛔ 熔断：{primary.name} 连续重复 {self._repeat_run} 次")
                yield self._emit("circuit_break", step=step, name=primary.name,
                                 count=self._repeat_run)
            elif blind >= BLIND_BLOCK_STEPS and primary.name in CLICK_ACTIONS:
                # 界面连续多步读不出任何元素，还在一门心思按坐标点——直接不执行。
                # 真实教训：UU加速器自绘界面，37 步全 ok、零进展，步数全烧在瞎点上。
                result = ActionResult(
                    action=primary.name, params=primary.arguments, ok=False,
                    denied=True, reason="界面读不出元素，禁止继续盲点",
                    error=(f"已经连续 {blind} 步读不到任何界面元素（这个窗口多半是自绘界面），"
                           f"再按坐标点下去只是浪费步数，已阻止 {primary.name}。\n"
                           f"必须换一种打法，任选其一：\n"
                           f"  1) 用键盘：press_key / hotkey（如 Tab 切焦点、Enter 确认、"
                           f"Ctrl+F 搜索），比坐标可靠；\n"
                           f"  2) 用程序的命令行/配置文件等确定性入口，绕开界面；\n"
                           f"  3) 换目标窗口，或先 screenshot 看一眼真实布局再决定；\n"
                           f"  4) 确实需要人看一眼时调用 ask_human 问清楚；\n"
                           f"  5) 判断这任务做不了就调用 finish 说明卡在哪。"))
                self._log("warn", f"🚫 已阻止 {primary.name}：连续 {blind} 步读不到界面元素")
                yield self._emit("circuit_break", step=step, name=primary.name,
                                 count=blind, reason="blind")
            else:
                result = self.executor.run(primary.name, primary.arguments)

            run.steps = step
            run.actions.append(result.as_dict())
            yield self._emit("result", step=step, result=result.as_dict())

            messages.append({
                "role": "tool", "tool_call_id": primary.id,
                "content": result.to_text()[: cfg.max_observation_chars],
            })
            for extra in response.tool_calls[1:]:
                messages.append({
                    "role": "tool", "tool_call_id": extra.id,
                    "content": "（本系统每轮只执行一个动作，此调用被忽略，请下一轮重新决定）",
                })
                yield self._emit("skipped", name=extra.name)

            if result.snapshot is not None:
                snapshot = result.snapshot
                # 界面读不出元素就累计"瞎点"步数；一旦读出元素立刻清零
                self._blind_run = 0 if snapshot.elements else self._blind_run + 1
                yield self._emit("observation", step=step, text=snapshot.render(),
                                 snapshot=snapshot.as_dict())

            if result.action == "ask_human":
                run.needs_human = str(result.output)
                stopped = "需要人工介入"
                run.summary = str(result.output)
                break

            if result.finished:
                finish_info = result.output if isinstance(result.output, dict) else {}
                run.summary = str(finish_info.get("summary") or result.observation)[:4000]
                run.success = bool(finish_info.get("success", True))
                run.ok = True
                stopped = "任务完成"
                yield self._emit("finished", summary=run.summary, success=run.success)
                break

            if not result.ok:
                last_error = result.error or result.reason or "未知错误"
                if result.denied:
                    last_error += "（该动作被安全策略拒绝，请换一个合法做法）"

            if cfg.step_pause > 0:
                time.sleep(cfg.step_pause)
        else:
            stopped = "达到最大步数"

        run.stopped_reason = stopped or "结束"
        run.finished_at = time.time()
        if not run.summary:
            run.summary = f"任务未完成（{run.stopped_reason}）"
        self._last_run = run
        yield self._emit("done", run=run.as_dict())

    # -- 内部 ------------------------------------------------------------
    def _ask(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]],
             snapshot: Snapshot | None) -> LLMResponse:
        if self.config.include_screenshot and snapshot and snapshot.image is not None:
            return self._ask_with_image(messages, tools, snapshot)
        return self.llm.chat(messages, tools=tools,
                             temperature=self.config.temperature)

    def _ask_with_image(self, messages: list[dict[str, Any]],
                        tools: list[dict[str, Any]],
                        snapshot: Snapshot) -> LLMResponse:
        """把截图作为图像内容附在最后一条 user 消息上。

        注意用 **PNG** 而不是 JPEG：JPEG 的块效应会把小字号代码和公式糊掉，
        而模型这边是按"最多 1024 token/图"计费的，压不压缩几乎不影响成本。
        ``detail=original`` 保留原图，否则会被缩到 512x512 直接读不清。
        """
        import base64

        shot = snapshot.image.scaled(1600, 1000)
        data = base64.b64encode(shot.to_bytes("png")).decode("ascii")
        msgs = [dict(m) for m in messages]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                original = msgs[i].get("content")
                if isinstance(original, str):
                    msgs[i]["content"] = [
                        {"type": "text", "text": original},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/png;base64,{data}",
                                       "detail": "original"}},
                    ]
                break
        return self.llm.chat(msgs, tools=tools, temperature=self.config.temperature)

    @staticmethod
    def _trim_history(messages: list[dict[str, Any]], keep_recent: int) -> list[dict[str, Any]]:
        """上下文超预算时，**从最前面整批丢掉最老的几轮对话**。

        踩过的坑（实测）：早期版本会把"过期的工具结果"**原地改写成占位符**。
        看着很合理——上下文确实变短了——但改的位置在对话靠前处，前缀缓存
        从被改的那一条起整段失效。10 步模拟：

            步 1~6   纯追加      命中率 85→90%，每步未命中 ~1714 token
            步 7     开始改写    命中率掉到 53.9%，未命中 8322 token
            步 8                单步耗时从 1.6s 涨到 7.6s

        未命中 token 是要真花钱的，也是要真等的。所以现在：
          · 只**整条丢弃**，绝不改写任何已有消息的内容；
          · 攒到超预算才丢，而且一次丢够（丢到预算的 70%），
            这样两次丢弃之间全是纯追加，前缀缓存全中。

        ``keep_recent`` 现在的作用是"至少保留多少轮"，防止一次丢太狠。
        """
        if _history_chars(messages) <= CONTEXT_CHAR_BUDGET:
            return messages

        head, rest = messages[:2], messages[2:]      # 保住 system + 目标
        rounds: list[list[dict[str, Any]]] = []
        for msg in rest:
            if msg.get("role") == "user" or not rounds:
                rounds.append([msg])
            else:
                rounds[-1].append(msg)

        floor = max(1, int(keep_recent))
        target = int(CONTEXT_CHAR_BUDGET * CONTEXT_TRIM_TARGET)
        while len(rounds) > floor and _history_chars(head + _flatten(rounds)) > target:
            rounds.pop(0)
        return head + _flatten(rounds)

    def _emit(self, kind: str, **payload: Any) -> dict[str, Any]:
        """广播一个事件，并返回它（便于 ``yield self._emit(...)``）。"""
        event = {"kind": kind, "ts": time.time(), **payload}
        self.ctx.emit(kind, **payload)
        self._events = getattr(self, "_events", [])
        self._events.append(event)
        self._on_event(event)
        return event

    def _on_event(self, event: dict[str, Any]) -> None:
        listeners = getattr(self, "_listeners", None)
        if listeners:
            for fn in list(listeners):
                try:
                    fn(event)
                except Exception:
                    pass

    def subscribe(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners = getattr(self, "_listeners", [])
        self._listeners.append(fn)

    # -- 同步接口 --------------------------------------------------------
    def run(self, on_event: Callable[[dict[str, Any]], None] | None = None) -> AgentRun:
        """跑到结束，返回结果。

        ``on_event`` 会以订阅者身份收到每一个事件（``stream()`` 已经负责产出，
        这里不再重复推送）。
        """
        if on_event:
            self.subscribe(on_event)
        for _ in self.stream():
            pass
        return getattr(self, "_last_run", AgentRun(goal=self.config.goal))
