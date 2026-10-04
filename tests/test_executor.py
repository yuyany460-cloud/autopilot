# -*- coding: utf-8 -*-
"""执行器测试：护栏拦截、演练模式、审计落盘、自动重新感知。

这几个行为共同决定了"模型的输出如何安全地变成真实操作"，
所以每一个都要有测试兜底。
"""

from __future__ import annotations

import json

from autopilot.executor import AuditLog, Executor
from autopilot.registry import Action, Danger, Param, Registry


def _build(ctx, auto_observe: str = "none") -> Executor:
    reg = Registry()
    calls: list[dict] = []

    def ok_action(value: str = "v") -> str:
        calls.append({"value": value})
        return f"做了 {value}"

    def boom() -> str:
        raise RuntimeError("故意的失败")

    reg.register(Action("ok_action", "正常动作", [Param("value", default="v")],
                        ok_action, Danger.LOW))
    reg.register(Action("boom", "会失败", [], boom, Danger.LOW))
    reg.register(Action("dangerous", "危险动作", [], lambda: "不该执行", Danger.HIGH))
    reg.register(Action("readonly_thing", "只读", [], lambda: "读了", Danger.SAFE,
                        mutates=False))
    ex = Executor(reg, ctx, audit=AuditLog(ctx.workspace / "var" / "logs"),
                  auto_observe=auto_observe, log=lambda l, m: None)
    ex.calls = calls  # type: ignore[attr-defined]
    return ex


def test_unknown_action_returns_error(ctx) -> None:
    ex = _build(ctx)
    result = ex.run("根本没有这个动作", {})
    assert not result.ok
    assert "未知动作" in result.error


def test_successful_action(ctx) -> None:
    ex = _build(ctx)
    result = ex.run("ok_action", {"value": "hi"})
    assert result.ok and result.output == "做了 hi"
    assert ex.steps == 1


def test_action_exception_is_captured_not_raised(ctx) -> None:
    ex = _build(ctx)
    result = ex.run("boom", {})
    assert not result.ok
    assert "故意的失败" in result.error
    assert "boom" in result.to_text()


def test_high_danger_denied_by_default(ctx) -> None:
    ex = _build(ctx)
    result = ex.run("dangerous", {})
    assert result.denied and not result.ok
    assert "危险动作" in result.reason
    assert not ex.calls, "被拒绝的动作绝不能被执行"


def test_high_danger_runs_when_allowed(ctx) -> None:
    ctx.guard.policy.allow_danger = True
    ex = _build(ctx)
    assert ex.run("dangerous", {}).ok


def test_dry_run_blocks_execution(ctx) -> None:
    ctx.guard.policy.dry_run = True
    ex = _build(ctx)
    result = ex.run("ok_action", {"value": "x"})
    assert result.dry_run and result.ok
    assert not ex.calls, "演练模式不能真的执行"


def test_readonly_mode(ctx) -> None:
    ctx.guard.policy.mode = "readonly"
    ex = _build(ctx)
    assert not ex.run("ok_action", {}).ok
    assert ex.run("readonly_thing", {}).ok


def test_audit_log_written(ctx) -> None:
    ex = _build(ctx)
    ex.run("ok_action", {"value": "审计"})
    ex.run("dangerous", {})
    rows = ex.audit.tail(10)
    assert len(rows) == 2
    assert rows[0]["action"] == "ok_action" and rows[0]["ok"] is True
    assert rows[1]["denied"] is True
    # 必须是合法 JSONL
    for line in ex.audit.path.read_text(encoding="utf-8").splitlines():
        json.loads(line)


def test_kill_switch_blocks_execution(ctx) -> None:
    ctx.killswitch.trigger("测试")
    ex = _build(ctx)
    result = ex.run("ok_action", {})
    assert result.denied and "急停" in result.error
    assert not ex.calls


def test_confirm_callback_can_reject(ctx) -> None:
    ctx.guard.policy.mode = "confirm"
    ctx.confirm = lambda name, params: False
    ex = _build(ctx)
    result = ex.run("ok_action", {})
    assert result.denied and not ex.calls


def test_confirm_callback_can_approve(ctx) -> None:
    ctx.guard.policy.mode = "confirm"
    ctx.confirm = lambda name, params: True
    ex = _build(ctx)
    assert ex.run("ok_action", {}).ok
    assert ex.calls


def test_run_steps_stops_on_error(ctx) -> None:
    ex = _build(ctx)
    results = ex.run_steps([
        {"action": "ok_action", "params": {"value": "1"}},
        {"action": "boom"},
        {"action": "ok_action", "params": {"value": "3"}},
    ])
    assert len(results) == 2
    assert results[0].ok and not results[1].ok


def test_run_steps_keep_going(ctx) -> None:
    ex = _build(ctx)
    results = ex.run_steps([
        {"action": "boom"},
        {"action": "ok_action", "params": {"value": "2"}},
    ], stop_on_error=False)
    assert len(results) == 2 and results[1].ok


def test_step_shorthand_without_params_key(ctx) -> None:
    ex = _build(ctx)
    results = ex.run_steps([{"action": "ok_action", "value": "简写"}])
    assert results[0].ok and ex.calls == [{"value": "简写"}]


def test_result_to_text_is_readable(ctx) -> None:
    ex = _build(ctx)
    assert "✅" in ex.run("ok_action", {}).to_text()
    assert "❌" in ex.run("boom", {}).to_text()
    assert "被安全策略拒绝" in ex.run("dangerous", {}).to_text()
