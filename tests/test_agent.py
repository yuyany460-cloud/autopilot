# -*- coding: utf-8 -*-
"""Agent 主循环测试。

这里的第一个测试是回归测试：早期版本 ``Agent.stream()`` 忘了写 ``yield``，
函数退化成普通函数返回 ``None``，导致任务其实跑完了却在收尾时崩掉
（``TypeError: 'NoneType' object is not iterable``）。外面完全看不出来，
因为所有事件都是通过订阅者实时推送的。
"""

from __future__ import annotations

import time

import pytest

from autopilot import perceive, winapi
from autopilot.agent import Agent, AgentConfig, AgentRun
from autopilot.executor import ActionResult
from autopilot.llm import LLMResponse, ToolCall
from autopilot.perceive import Snapshot
from autopilot.registry import Action, Danger, Param


def make_snapshot() -> Snapshot:
    return Snapshot(ts=time.time(), screen_size=(1920, 1200), cursor=(100, 100),
                    scale=1.25, foreground=None, window_list=[], elements=[])


# --------------------------------------------------------------------------
# 测试替身
# --------------------------------------------------------------------------


class FakeKillSwitch:
    def __init__(self) -> None:
        self.triggered = False
        self.reason = ""

    def raise_if_triggered(self) -> None:
        pass


class FakeContext:
    """只实现 Agent 真正用到的那几个成员。"""

    def __init__(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        self.killswitch = FakeKillSwitch()
        self.events: list[dict] = []
        self.logs: list[tuple[str, str]] = []

    def take_snapshot(self, **kwargs) -> Snapshot:
        return self.snapshot

    def set_snapshot(self, snap: Snapshot) -> Snapshot:
        self.snapshot = snap
        return snap

    def emit(self, kind: str, **payload) -> None:
        self.events.append({"kind": kind, **payload})

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


class FakeRegistry:
    def __init__(self) -> None:
        self.actions = {
            "observe": Action("observe", "观察", [], lambda: None, Danger.SAFE),
            "notify": Action("notify", "通知", [Param("title", required=True)],
                             lambda title="": {"ok": True}, Danger.SAFE),
            "finish": Action("finish", "结束", [Param("summary", required=True)],
                             lambda summary="": {"finished": True, "success": True,
                                                 "summary": summary}, Danger.SAFE),
            "delete_path": Action("delete_path", "删除", [], lambda: None, Danger.HIGH),
        }

    def openai_tools(self, allow_danger: bool = False, only=None):
        return [{"type": "function", "function": {"name": n, "description": a.description,
                                                  "parameters": a.schema()}}
                for n, a in self.actions.items()
                if allow_danger or a.danger != Danger.HIGH]


class FakeExecutor:
    """按脚本返回预设动作，不碰真实系统。"""

    def __init__(self, script: list[tuple[str, dict]]) -> None:
        self.script = list(script)
        self.registry = FakeRegistry()
        self.auto_observe = "none"
        self.calls: list[tuple[str, dict]] = []
        # 每次 run 依次弹出一个快照，用完就一直复用最后一个（模拟"界面读不出元素"）
        self.snapshots: list[Snapshot] = []

    def run(self, name: str, params: dict) -> ActionResult:
        self.calls.append((name, params))
        result = ActionResult(action=name, params=params, ok=True)
        if name == "finish":
            result.output = {"finished": True, "success": True,
                             "summary": params.get("summary", "")}
            result.finished = True
            result.success = True
        else:
            result.output = f"{name} 执行完成"
            if self.snapshots:
                result.snapshot = (self.snapshots.pop(0) if len(self.snapshots) > 1
                                   else self.snapshots[0])
        return result


class FakeLLM:
    """按预设顺序返回 tool_calls / 纯文本。"""

    def __init__(self, script: list[LLMResponse]) -> None:
        self.script = list(script)
        self.messages_seen: list[list[dict]] = []

        class _Cfg:
            model = "fake-model"

        self.config = _Cfg()

    def chat(self, messages, tools=None, temperature=None, max_tokens=None) -> LLMResponse:
        self.messages_seen.append([dict(m) for m in messages])
        if self.script:
            return self.script.pop(0)
        return LLMResponse(content="（脚本耗尽）", tool_calls=[])


def _call(name: str, args: dict | None = None, cid: str = "c1") -> LLMResponse:
    return LLMResponse(tool_calls=[ToolCall(id=cid, name=name, arguments=args or {})],
                       usage={"prompt_tokens": 10, "completion_tokens": 5})


@pytest.fixture(autouse=True)
def _stub_context(monkeypatch):
    monkeypatch.setattr(perceive, "quick_context", lambda: "测试环境")


def _make_agent(llm: FakeLLM, executor: FakeExecutor, ctx: FakeContext,
                **cfg) -> Agent:
    cfg.setdefault("goal", "测试目标")
    cfg.setdefault("max_steps", 5)
    return Agent(llm, executor, ctx, AgentConfig(**cfg), log=ctx.log)


# --------------------------------------------------------------------------
# 核心回归
# --------------------------------------------------------------------------


def test_stream_is_a_real_generator() -> None:
    """stream() 必须真的产出事件，而不是返回 None。"""
    llm = FakeLLM([_call("finish", {"summary": "好了"})])
    agent = _make_agent(llm, FakeExecutor([]), FakeContext(make_snapshot()))
    gen = agent.stream()
    assert hasattr(gen, "__next__"), "stream() 必须是生成器"
    events = list(gen)
    assert events, "stream() 至少要产出事件"
    assert events[0]["kind"] == "start"
    assert events[-1]["kind"] == "done"


def test_run_returns_result_without_iterating_none() -> None:
    """run() 之前会因为迭代 None 而崩溃，这是回归点。"""
    llm = FakeLLM([_call("finish", {"summary": "完成啦"})])
    agent = _make_agent(llm, FakeExecutor([]), FakeContext(make_snapshot()))
    run = agent.run()
    assert isinstance(run, AgentRun)
    assert run.ok and run.success
    assert run.summary == "完成啦"
    assert run.stopped_reason == "任务完成"


def test_event_order_and_subscriber_notified_once() -> None:
    llm = FakeLLM([_call("notify", {"title": "hi"}), _call("finish", {"summary": "ok"})])
    ctx = FakeContext(make_snapshot())
    agent = _make_agent(llm, FakeExecutor([]), ctx)

    seen: list[str] = []
    agent.subscribe(lambda e: seen.append(e["kind"]))
    agent.run()

    assert seen[0] == "start"
    assert "action" in seen and "result" in seen
    assert seen[-1] == "done"
    # 订阅者每条事件只收到一次（run() 不应重复投递）
    assert seen.count("start") == 1
    assert seen.count("done") == 1


# --------------------------------------------------------------------------
# 循环行为
# --------------------------------------------------------------------------


def test_finish_stops_the_loop() -> None:
    executor = FakeExecutor([])
    llm = FakeLLM([_call("notify", {"title": "a"}),
                   _call("finish", {"summary": "结束"})])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=10)
    run = agent.run()
    assert [c[0] for c in executor.calls] == ["notify", "finish"]
    assert run.steps == 2


def test_max_steps_terminates() -> None:
    executor = FakeExecutor([])
    llm = FakeLLM([_call("notify", {"title": str(i)}) for i in range(20)])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=3)
    run = agent.run()
    assert run.steps == 3
    assert "最大步数" in run.stopped_reason
    assert not run.success


def test_text_only_reply_gets_nudged() -> None:
    """模型只说话不动作时，应被提示去调用动作，而不是直接结束。"""
    executor = FakeExecutor([])
    llm = FakeLLM([LLMResponse(content="我先想想"),
                   _call("finish", {"summary": "想好了"})])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=5)
    run = agent.run()
    assert run.success
    # 第二轮发给模型的消息里应包含催促
    second = llm.messages_seen[1]
    assert any("没有调用任何动作" in str(m.get("content")) for m in second)


def test_only_first_tool_call_is_executed() -> None:
    executor = FakeExecutor([])
    parallel = LLMResponse(tool_calls=[
        ToolCall(id="a", name="notify", arguments={"title": "1"}),
        ToolCall(id="b", name="observe", arguments={}),
    ])
    llm = FakeLLM([parallel, _call("finish", {"summary": "done"})])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=5)
    run = agent.run()
    assert [c[0] for c in executor.calls] == ["notify", "finish"]
    assert run.success


def test_kill_switch_stops_the_loop() -> None:
    ctx = FakeContext(make_snapshot())
    ctx.killswitch.triggered = True
    ctx.killswitch.reason = "测试急停"
    agent = _make_agent(FakeLLM([]), FakeExecutor([]), ctx)
    run = agent.run()
    assert "急停" in run.stopped_reason
    assert not run.success


def test_ask_human_stops_and_reports() -> None:
    executor = FakeExecutor([])

    def run(name, params):
        result = ActionResult(action=name, params=params, ok=True)
        result.output = "[需要人工介入] 请输入验证码"
        return result

    executor.run = run  # type: ignore[method-assign]
    llm = FakeLLM([_call("ask_human", {"question": "验证码是多少"})])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()))
    run_result = agent.run()
    assert run_result.needs_human
    assert "人工介入" in run_result.stopped_reason


def test_history_is_trimmed_but_keeps_recent() -> None:
    """超预算时丢掉最老的几轮，最近的一定要留着。

    注意这个测试以前断言的是"原地把老内容替换成占位符"——那正是把前缀缓存
    打掉一半的原因（见文件末尾的裁剪/缓存测试）。现在改成了整轮丢弃。
    """
    from autopilot.agent import CONTEXT_CHAR_BUDGET

    chunk = "观察内容" * 500                     # 2000 字符
    rounds = CONTEXT_CHAR_BUDGET // len(chunk) + 10   # 保证真的超预算

    messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "目标"}]
    for i in range(rounds):
        messages.append({"role": "user", "content": f"[第 {i} 步]"})
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"t{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"t{i}", "content": chunk})
    trimmed = Agent._trim_history(messages, keep_recent=3)

    assert len(trimmed) < len(messages), "超预算就该真的裁掉，而不是只改内容"
    tools = [m for m in trimmed if m["role"] == "tool"]
    assert len(tools) >= 3, "最近几轮必须留着"
    assert tools[-1]["content"] == chunk, "最新的观察一个字都不能动"
    assert tools[0]["content"] == chunk, "留下的观察也不许被改写"


def test_usage_is_accumulated() -> None:
    llm = FakeLLM([_call("notify", {"title": "a"}), _call("finish", {"summary": "x"})])
    agent = _make_agent(llm, FakeExecutor([]), FakeContext(make_snapshot()))
    run = agent.run()
    assert run.usage["prompt_tokens"] == 20
    assert run.usage["completion_tokens"] == 10


def test_run_has_duration_and_actions() -> None:
    llm = FakeLLM([_call("notify", {"title": "a"}), _call("finish", {"summary": "x"})])
    agent = _make_agent(llm, FakeExecutor([]), FakeContext(make_snapshot()))
    run = agent.run()
    assert run.finished_at >= run.started_at > 0
    assert len(run.actions) == 2
    assert run.as_dict()["steps"] == 2
    assert time.time() - run.started_at < 30


# --------------------------------------------------------------------------
# 重复动作熔断
#
# 真实教训：读一张公式图片反复折腾，把 30 步预算烧掉一大半。
# 提示词写了"不要重复调用同一个失败的动作"，但那是软约束——
# 模型完全可能照旧。这里用硬拦截兜底。
# --------------------------------------------------------------------------


def test_identical_action_is_circuit_broken() -> None:
    executor = FakeExecutor([])
    llm = FakeLLM([_call("observe", {}) for _ in range(8)])
    events: list[str] = []
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=6)
    agent.subscribe(lambda e: events.append(e["kind"]))
    run = agent.run()

    # 前 3 次真的执行，第 4 次起被拦下
    assert [c[0] for c in executor.calls] == ["observe"] * 3
    assert "circuit_break" in events
    blocked = [a for a in run.actions if a.get("reason") == "重复动作熔断"]
    assert blocked, "应该有被熔断的动作"


def test_circuit_break_message_tells_model_what_to_do() -> None:
    """熔断不能只是"拒绝"，必须告诉模型下一步该怎么走。"""
    executor = FakeExecutor([])
    llm = FakeLLM([_call("browser_read_image", {"index": 0}) for _ in range(6)])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=5)
    run = agent.run()
    msg = " ".join(str(a.get("error") or "") for a in run.actions)
    assert "熔断" in msg or "重复" in msg
    assert "换" in msg, "要提示换方法"
    assert "finish" in msg, "要给出兜底出路"


def test_different_params_reset_the_counter() -> None:
    """换参数就不算重复，不能误伤正常的连续调用。"""
    executor = FakeExecutor([])
    llm = FakeLLM([
        _call("click", {"element": "E1"}),
        _call("click", {"element": "E1"}),
        _call("click", {"element": "E1"}),          # 第 3 次仍然执行
        _call("click", {"element": "E2"}),          # 换目标 -> 计数器重置
        _call("finish", {"summary": "ok"}),
    ])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=8)
    agent.run()
    assert [c[0] for c in executor.calls] == ["click", "click", "click", "click", "finish"]


def test_interleaved_actions_are_not_treated_as_repeats() -> None:
    """A、B 交替出现不算"连续重复"，不该被熔断。"""
    executor = FakeExecutor([])
    llm = FakeLLM([
        _call("observe", {}), _call("list_windows", {}),
        _call("observe", {}), _call("list_windows", {}),
        _call("observe", {}), _call("list_windows", {}),
        _call("finish", {"summary": "ok"}),
    ])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=10)
    agent.run()
    assert [c[0] for c in executor.calls].count("observe") == 3
    assert [c[0] for c in executor.calls].count("list_windows") == 3
    assert [c[0] for c in executor.calls][-1] == "finish"


def test_same_action_with_reordered_params_is_still_a_repeat() -> None:
    """参数顺序不同但内容一样，仍应算重复（签名按 key 排序）。"""
    executor = FakeExecutor([])
    llm = FakeLLM([_call("browser_type", {"text": "x", "target": "#ed"}) for _ in range(6)])
    agent = _make_agent(llm, executor, FakeContext(make_snapshot()), max_steps=5)
    agent.run()
    assert len(executor.calls) == 3, "参数顺序不该影响重复判定"


def test_threshold_is_reasonable() -> None:
    from autopilot.agent import MAX_IDENTICAL_REPEAT

    assert 2 <= MAX_IDENTICAL_REPEAT <= 5, "太小会误伤重试，太大挡不住死循环"


# --------------------------------------------------------------------------
# 步数提示
#
# 也是真实教训：原来写的是"剩余步数不多，请直接推进到目标**或调用 finish
# 说明情况**"，结果模型在还剩 3 步时就主动认输（实测第 29/30 步就 finish）。
# 催促必须是"抓紧推进"，不能给放弃的台阶。
# --------------------------------------------------------------------------


def test_step_hint_reports_progress() -> None:
    from autopilot.prompts import build_step_hint

    hint = build_step_hint(5, 50, 45)
    assert "第 5/50 步" in hint
    assert "还剩 45 步" in hint


def test_step_hint_is_silent_until_the_end() -> None:
    """预算充裕时不该催收尾，否则模型会提前放弃。"""
    from autopilot.prompts import build_step_hint

    for remaining in (45, 20, 10, 3):
        assert "预算快用完" not in build_step_hint(1, 50, remaining)


def test_step_hint_pushes_forward_near_the_end() -> None:
    from autopilot.prompts import build_step_hint

    hint = build_step_hint(49, 50, 1)
    assert "预算快用完" in hint
    assert "全部" in hint, "要明确让它把剩余步数用在推进目标上"
    assert "只有确实做不完时才" in hint, "不能给'可以放弃了'的台阶"


def test_step_hint_includes_last_error() -> None:
    from autopilot.prompts import build_step_hint

    hint = build_step_hint(7, 50, 43, last_error="点击超时")
    assert "点击超时" in hint
    assert "不要重复" in hint


# --------------------------------------------------------------------------
# 上下文裁剪 vs 前缀缓存
#
# 踩过的大坑：原来会把"过期的工具结果"**原地改写成占位符**。上下文是变短了，
# 但改的位置在对话靠前处，前缀缓存从那里整段失效。10 步实测：
#   纯追加阶段  命中率 85→90%，每步未命中 ~1714 token
#   第 7 步开始改写   命中率掉到 53.9%，未命中 8322 token，单步 1.6s → 7.6s
# 现在只整条丢弃，且攒够了一次丢到位。
# --------------------------------------------------------------------------


def _long_conversation(rounds: int, chars: int = 4000) -> list[dict]:
    msgs = [{"role": "system", "content": "系统提示"},
            {"role": "user", "content": "目标"}]
    for i in range(rounds):
        msgs.append({"role": "user", "content": f"[第 {i} 步]"})
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"c{i}", "type": "function",
                                     "function": {"name": "observe", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "观" * chars})
    return msgs


def test_history_is_not_rewritten_in_place() -> None:
    """裁剪只能整条丢，绝不能改写已有消息——改写会毁掉前缀缓存。"""
    from autopilot.agent import Agent

    msgs = _long_conversation(30)
    before = [m.get("content") for m in msgs]
    out = Agent._trim_history(msgs, 5)

    kept = {id(m) for m in out}
    for msg, content in zip(msgs, before):
        if id(msg) in kept:
            assert msg.get("content") == content or msg.get("role") == "tool", \
                "留在上下文里的消息内容被改写了"
    # 长内容一个都不能被替换成占位符
    for msg in out:
        if msg.get("role") == "tool":
            assert "已省略" not in str(msg.get("content")), "又出现了原地改写"


def test_history_drops_a_whole_batch_not_one_at_a_time() -> None:
    """攒够了一次丢到位，否则每步都丢一点 = 每步都破坏缓存。"""
    from autopilot.agent import Agent, CONTEXT_CHAR_BUDGET

    msgs = _long_conversation(40)
    out = Agent._trim_history(msgs, 5)

    dropped = len(msgs) - len(out)
    assert dropped > 3, f"只丢了 {dropped} 条——应该一次丢够，留出后续纯追加的空间"
    # 丢完应该明显低于预算，这样接下来好几步都不用再丢
    from autopilot.agent import _history_chars

    assert _history_chars(out) < CONTEXT_CHAR_BUDGET * 0.8


def test_history_keeps_the_system_prompt_and_goal() -> None:
    from autopilot.agent import Agent

    msgs = _long_conversation(40)
    out = Agent._trim_history(msgs, 5)
    assert out[0] == msgs[0], "system 提示词永远不能丢"
    assert out[1] == msgs[1], "任务目标永远不能丢"


def test_short_conversation_is_left_alone() -> None:
    """没超预算就别动——任何改动都是缓存失效。"""
    from autopilot.agent import Agent

    msgs = _long_conversation(3, chars=200)
    assert Agent._trim_history(msgs, 5) is msgs


def test_trim_keeps_message_structure_valid() -> None:
    """丢掉几轮之后，第一条不能是 tool（tool 必须跟在它的 assistant 后面）。"""
    from autopilot.agent import Agent

    out = Agent._trim_history(_long_conversation(40), 5)
    assert out[2].get("role") == "user", "裁剪后第一轮必须以 user 提示开头"
    for i, msg in enumerate(out):
        if msg.get("role") == "tool":
            assert out[i - 1].get("role") in ("tool", "assistant"), \
                "tool 消息的前一条必须是 assistant 或另一个 tool"


def test_default_budget_is_generous_enough() -> None:
    """默认预算要够做完一个多步 GUI 任务（原来 30 步实测不够）。"""
    from autopilot.agent import AgentConfig

    assert AgentConfig().max_steps >= 40


# --------------------------------------------------------------------------
# 盲点熔断
#
# 真实教训：让 Agent 去"打开UU加速器并加速 steam"，UU 是自绘界面——没有 UIA
# 元素、OCR 读出来的也是乱码。模型看不到任何反馈，就一路按坐标瞎点，
# 37 步全部 ok、零进展，步数全烧光。软提示没用，必须硬拦。
# --------------------------------------------------------------------------


def _blind_agent(steps: int = 12, max_steps: int = 20):
    ctx = FakeContext(make_snapshot())          # make_snapshot() 的 elements 是空的
    executor = FakeExecutor([])
    executor.snapshots = [make_snapshot()]
    llm = FakeLLM([_call("click", {"x": 100 + i, "y": 200 + i}) for i in range(steps)]
                  + [_call("finish", {"summary": "放弃"})])
    agent = _make_agent(llm, executor, ctx, max_steps=max_steps)
    return agent, executor, ctx


def test_blind_clicking_is_blocked() -> None:
    from autopilot.agent import BLIND_BLOCK_STEPS

    agent, executor, ctx = _blind_agent()
    agent.run()

    clicks = [c for c in executor.calls if c[0] == "click"]
    assert len(clicks) < BLIND_BLOCK_STEPS + 3, \
        f"瞎点必须被拦住，实际执行了 {len(clicks)} 次"
    assert any(e["kind"] == "circuit_break" and e.get("reason") == "blind"
               for e in ctx.events), "要发出瞎点熔断事件"


def test_blind_block_tells_the_model_what_to_do_instead() -> None:
    agent, _, _ = _blind_agent()
    run = agent.run()
    denied = [a for a in run.actions if a.get("denied") and "读不出" in str(a.get("reason", ""))]
    assert denied, "被拦下来的动作要有记录"
    err = str(denied[0].get("error", ""))
    for kw in ("键盘", "ask_human", "finish", "screenshot"):
        assert kw in err, f"拦下来还得给出路，缺少 {kw}"


def test_blind_counter_resets_when_elements_come_back() -> None:
    """一旦读出元素，就不该再算"瞎点"。"""
    from autopilot.agent import BLIND_BLOCK_STEPS

    empty = make_snapshot()
    good = Snapshot(ts=time.time(), screen_size=(1920, 1200), cursor=(0, 0), scale=1.0,
                    foreground=None, window_list=[],
                    elements=[perceive.Element(index=1, name="开始加速", control_type="Button",
                                               class_name="Button", rect=winapi.Rect(10, 10, 80, 30))])
    ctx = FakeContext(empty)
    executor = FakeExecutor([])
    # 前 6 步读不到元素，第 7 步界面出来了——第 7 步的 blind 计数是 7，还没到 8
    executor.snapshots = [empty] * (BLIND_BLOCK_STEPS - 2) + [good]
    llm = FakeLLM(
        [_call("click", {"x": i, "y": i}) for i in range(BLIND_BLOCK_STEPS - 1)]
        + [_call("click", {"x": 9, "y": 9}) for _ in range(3)]
        + [_call("finish", {"summary": "ok"})])
    agent = _make_agent(llm, executor, ctx, max_steps=20)
    agent.run()
    assert not any(e["kind"] == "circuit_break" for e in ctx.events), \
        "读出元素后不该再熔断"
    assert [c[0] for c in executor.calls].count("click") == BLIND_BLOCK_STEPS + 2, \
        "计数清零后应该恢复正常点击"


def test_blind_step_hint_warns_but_does_not_give_up() -> None:
    from autopilot.prompts import build_step_hint

    hint = build_step_hint(9, 50, 41, blind_steps=5)
    assert "读不到任何界面元素" in hint
    assert "键盘" in hint and "ask_human" in hint
    assert "预算快用完" not in hint, "还早，不能泄气"
    assert build_step_hint(9, 50, 41, blind_steps=0).count("读不到") == 0


def test_prompt_has_one_step_guidance() -> None:
    """提示词必须明确"一件事一步做完"，否则模型还是会分步探索。"""
    from autopilot.prompts import SYSTEM_PROMPT

    assert "一步到位" in SYSTEM_PROMPT
    for kw in ("app_status", "wait_title", "find_app", "wmic"):
        assert kw in SYSTEM_PROMPT, f"提示词里缺少 {kw} 的指引"
    assert "递归扫盘" in SYSTEM_PROMPT
