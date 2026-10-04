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


@dataclass
class AgentConfig:
    """Agent 运行参数。"""

    goal: str = ""
    max_steps: int = 30
    allow_danger: bool = False
    temperature: float = 0.0
    observe_every_step: bool = True
    include_screenshot: bool = False   # 需要视觉模型才有效
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

        for step in range(1, cfg.max_steps + 1):
            if self.ctx.killswitch.triggered:
                stopped = f"急停：{self.ctx.killswitch.reason}"
                yield self._emit("stopped", reason=stopped)
                break

            remaining = cfg.max_steps - step
            messages = self._trim_history(messages, cfg.keep_recent_observations)
            messages.append({"role": "user",
                             "content": prompts.build_step_hint(
                                 step, cfg.max_steps, remaining, last_error)})
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
        """把截图作为图像内容附在最后一条消息上（需要视觉模型）。"""
        import base64

        shot = snapshot.image.scaled(1280, 800)
        data = base64.b64encode(shot.to_bytes("jpg", quality=72)).decode("ascii")
        msgs = [dict(m) for m in messages]
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].get("role") == "user":
                original = msgs[i].get("content")
                if isinstance(original, str):
                    msgs[i]["content"] = [
                        {"type": "text", "text": original},
                        {"type": "image_url",
                         "image_url": {"url": f"data:image/jpeg;base64,{data}"}},
                    ]
                break
        return self.llm.chat(msgs, tools=tools, temperature=self.config.temperature)

    @staticmethod
    def _trim_history(messages: list[dict[str, Any]], keep_recent: int) -> list[dict[str, Any]]:
        """把较早的工具结果压缩掉，控制上下文长度（保持消息结构合法）。"""
        tool_indexes = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indexes) <= keep_recent:
            return messages
        stale = set(tool_indexes[:-keep_recent])
        out = []
        for i, msg in enumerate(messages):
            if i in stale and len(str(msg.get("content") or "")) > 200:
                out.append({**msg, "content": "（早期观察已省略，需要时重新 observe）"})
            else:
                out.append(msg)
        return out

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
