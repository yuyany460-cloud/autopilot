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

from autopilot import perceive
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

    def takemake_snapshotshot(self, **kwargs) -> Snapshot:
        return self.snapshot

    def setmake_snapshotshot(self, snap: Snapshot) -> Snapshot:
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
    messages = [{"role": "system", "content": "s"}]
    for i in range(10):
        messages.append({"role": "assistant", "content": None,
                         "tool_calls": [{"id": f"t{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"t{i}",
                         "content": "观察内容" * 100})
    trimmed = Agent._trim_history(messages, keep_recent=3)
    assert len(trimmed) == len(messages), "消息数量不能变，否则 tool 配对会断"
    tools = [m for m in trimmed if m["role"] == "tool"]
    assert "省略" in tools[0]["content"]
    assert "观察内容" in tools[-1]["content"]


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
