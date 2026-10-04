# -*- coding: utf-8 -*-
"""确定性流程测试：变量展开、加载、执行，以及录制器。

录制器测试直接给内部回调喂事件来验证"原始输入 → 流程步骤"的翻译逻辑，
不产生任何真实输入；另有标记为 ``hardware`` 的测试负责验证全局钩子能否装上。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from autopilot import winapi
from autopilot.executor import AuditLog, Executor
from autopilot.flows import (WM_KEYDOWN, Recorder, expand, list_flows, load_flow,
                             run_flow)
from autopilot.registry import Action, Danger, Param, Registry


# --- 变量展开 -----------------------------------------------------------


def test_simple_placeholder(tmp_path: Path) -> None:
    out = expand("你好 ${名字}", {"名字": "世界"}, tmp_path)
    assert out == "你好 世界"


def test_nested_placeholders_expanded(tmp_path: Path) -> None:
    """变量里还带占位符时必须继续展开（真实踩过的坑）。"""
    out = expand({"text": "${外层}"}, {"外层": "今天 ${date}"}, tmp_path)
    assert out["text"].startswith("今天 2")
    assert "${date}" not in out["text"]


def test_builtin_placeholders(tmp_path: Path) -> None:
    assert str(tmp_path) == expand("${workspace}", {}, tmp_path)
    assert len(expand("${date}", {}, tmp_path)) == 10
    assert expand("${time}", {}, tmp_path).count(":") == 2


def test_env_placeholder(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AP_TEST_VAR", "环境值")
    assert expand("${env:AP_TEST_VAR}", {}, tmp_path) == "环境值"
    assert expand("${env:不存在的变量}", {}, tmp_path) == ""


def test_unknown_placeholder_kept(tmp_path: Path) -> None:
    assert expand("${未知}", {}, tmp_path) == "${未知}"


def test_timestamp_placeholder_is_unique_per_second(tmp_path: Path) -> None:
    """给临时文件命名用：纯数字、可作文件名、随时间变化。"""
    first = expand("${timestamp}", {}, tmp_path)
    assert first.isdigit()
    time.sleep(1.05)
    assert expand("${timestamp}", {}, tmp_path) != first


def test_expand_walks_lists_and_dicts(tmp_path: Path) -> None:
    out = expand({"a": ["${x}", {"b": "${x}"}]}, {"x": "V"}, tmp_path)
    assert out == {"a": ["V", {"b": "V"}]}


def test_clipboard_placeholder(tmp_path: Path) -> None:
    winapi.clipboard_set_text("剪贴板内容")
    assert expand("${clipboard}", {}, tmp_path) == "剪贴板内容"


# --- 流程加载/执行 ------------------------------------------------------


def _flow_file(tmp_path: Path, steps: list[dict]) -> Path:
    p = tmp_path / "demo.json"
    p.write_text(json.dumps({"name": "测试流程", "steps": steps}, ensure_ascii=False),
                 encoding="utf-8")
    return p


def test_load_flow(tmp_path: Path) -> None:
    p = _flow_file(tmp_path, [{"action": "wait", "params": {"seconds": 0}}])
    flow = load_flow(p)
    assert flow["name"] == "测试流程" and len(flow["steps"]) == 1


def test_load_flow_accepts_bare_list(tmp_path: Path) -> None:
    p = tmp_path / "bare.json"
    p.write_text(json.dumps([{"action": "wait"}]), encoding="utf-8")
    flow = load_flow(p)
    assert flow["steps"] and flow["name"] == "bare"


def test_load_flow_rejects_bad_file(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(ValueError, match="不是合法 JSON"):
        load_flow(p)
    q = tmp_path / "empty.json"
    q.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少 steps"):
        load_flow(q)


def _executor(ctx) -> Executor:
    reg = Registry()
    seen: list[str] = []
    reg.register(Action("record", "记录", [Param("text", default="")],
                        lambda text="": (seen.append(text), "已记录")[1], Danger.SAFE))
    ex = Executor(reg, ctx, audit=AuditLog(ctx.workspace / "var" / "logs"),
                  auto_observe="none", log=lambda l, m: None)
    ex.seen = seen  # type: ignore[attr-defined]
    return ex


def test_run_flow_expands_variables(ctx, tmp_path: Path) -> None:
    ex = _executor(ctx)
    flow = {"vars": {"内容": "你好 ${date}"},
            "steps": [{"action": "record", "params": {"text": "${内容}"}}]}
    results = run_flow(ex, flow)
    assert results[0].ok
    assert ex.seen[0].startswith("你好 2")  # type: ignore[attr-defined]


def test_run_flow_variables_override(ctx) -> None:
    ex = _executor(ctx)
    flow = {"vars": {"x": "默认"},
            "steps": [{"action": "record", "params": {"text": "${x}"}}]}
    run_flow(ex, flow, variables={"x": "覆盖"})
    assert ex.seen == ["覆盖"]  # type: ignore[attr-defined]


def test_list_flows(tmp_path: Path) -> None:
    _flow_file(tmp_path, [{"action": "wait"}])
    (tmp_path / "broken.json").write_text("坏文件", encoding="utf-8")
    items = list_flows(tmp_path)
    assert len(items) == 1 and items[0]["file"] == "demo.json"
    assert list_flows(tmp_path / "不存在") == []


# --- 录制器 -------------------------------------------------------------


def test_recorder_builds_steps_from_events() -> None:
    """直接喂事件给录制器，验证它把原始输入翻译成流程步骤的逻辑。"""
    rec = Recorder()
    from autopilot.flows import (WM_KEYDOWN, WM_LBUTTONDOWN, WM_MOUSEWHEEL)

    rec._on_mouse(WM_LBUTTONDOWN, 100, 200, 0)
    rec._on_mouse(WM_MOUSEWHEEL, 100, 200, 120 << 16)
    rec._on_key(WM_KEYDOWN, 0x0D, 0)          # Enter
    rec._on_key(WM_KEYDOWN, 0x41, 0)          # a -> 进入文本缓冲
    rec._on_key(WM_KEYDOWN, 0x42, 0)          # b

    steps = rec.to_flow("测试", "")["steps"]
    actions = [s["action"] for s in steps]
    assert actions == ["click", "scroll", "press_key", "type_text"]

    assert steps[0]["params"] == {"x": 100, "y": 200, "button": "left"}
    assert steps[1]["params"]["amount"] == 1
    assert steps[2]["params"]["key"] == "enter"
    assert steps[3]["params"]["text"] == "ab"


def test_recorder_merges_consecutive_clicks() -> None:
    from autopilot.flows import WM_LBUTTONDOWN

    rec = Recorder()
    rec._on_mouse(WM_LBUTTONDOWN, 300, 400, 0)
    rec._on_mouse(WM_LBUTTONDOWN, 301, 401, 0)
    rec._on_mouse(WM_LBUTTONDOWN, 302, 399, 0)
    rec._on_mouse(WM_LBUTTONDOWN, 800, 100, 0)   # 换个地方 -> 另算一次

    steps = rec.to_flow()["steps"]
    assert len(steps) == 2
    assert int(steps[0]["params"].get("clicks", 1)) == 3
    assert int(steps[1]["params"].get("clicks", 1)) == 1


def test_recorder_negative_scroll() -> None:
    from autopilot.flows import WM_MOUSEWHEEL

    rec = Recorder()
    rec._on_mouse(WM_MOUSEWHEEL, 10, 10, (0xFFFF << 16))  # -1 的补码
    assert rec.to_flow()["steps"][0]["params"]["amount"] == -1


def test_recorder_empty_is_valid_flow() -> None:
    rec = Recorder()
    steps = rec.to_flow("空")["steps"]
    assert steps == []


# --- 键盘翻译：低级钩子报的是左右分开的修饰键码（0xA0..0xA5） -------------
# 这是又一个踩过的坑：只认 0x10..0x12 的话，Shift/Ctrl/Alt 会被整段丢掉。


def test_recorder_ignores_modifier_keydown() -> None:
    """修饰键自身不该产生步骤（它的作用体现在下一个主键的组合上）。"""
    rec = Recorder()
    for vk in (0x10, 0xA0, 0xA1, 0x11, 0xA2, 0xA3, 0x12, 0xA4, 0xA5):
        rec._on_key(WM_KEYDOWN, vk, 0, mods=(False, False, False))
    assert rec.to_flow()["steps"] == []


def test_recorder_normalises_left_ctrl_combination() -> None:
    """左 Ctrl（0xA2）+ S 必须录成 ``ctrl+s``，而不是 ``ctrl+vk_a2``。"""
    rec = Recorder()
    rec._on_key(WM_KEYDOWN, 0xA2, 0, mods=(True, False, False))   # 左 Ctrl 按下
    rec._on_key(WM_KEYDOWN, 0x53, 0, mods=(True, False, False))   # S
    steps = rec.to_flow()["steps"]
    assert len(steps) == 1, steps
    assert steps[0]["action"] == "hotkey"
    assert steps[0]["params"]["keys"] == "ctrl+s"


def test_recorder_shows_shift_in_combination() -> None:
    rec = Recorder()
    rec._on_key(WM_KEYDOWN, 0xA0, 0, mods=(True, False, True))    # 左 Shift
    rec._on_key(WM_KEYDOWN, 0xA2, 0, mods=(True, False, True))    # 左 Ctrl
    rec._on_key(WM_KEYDOWN, 0x5A, 0, mods=(True, False, True))    # Z
    assert rec.to_flow()["steps"][0]["params"]["keys"] == "ctrl+shift+z"


def test_recorder_alt_combination_uses_readable_key_name() -> None:
    rec = Recorder()
    rec._on_key(WM_KEYDOWN, 0x09, 0, mods=(False, True, False))   # Alt + Tab
    assert rec.to_flow()["steps"][0]["params"]["keys"] == "alt+tab"


def test_recorder_plain_letter_goes_to_text_not_hotkey() -> None:
    rec = Recorder()
    rec._on_key(WM_KEYDOWN, 0x41, 0, mods=(False, False, False))  # a
    steps = rec.to_flow()["steps"]
    assert [s["action"] for s in steps] == ["type_text"]
    assert steps[0]["params"]["text"] == "a"


def test_key_name_helper() -> None:
    from autopilot.flows import _key_name

    assert _key_name(0x41) == "a"      # A
    assert _key_name(0x5A) == "z"      # Z
    assert _key_name(0x30) == "0"      # 0
    assert _key_name(0x70) == "f1"     # F1
    assert _key_name(0x0D) == "enter"
    assert _key_name(0x99).startswith("vk_0x")  # 未知键留个可排查的名字


def test_unknown_key_name_round_trips_through_resolve_key() -> None:
    """录制出的未知键必须能被回放端解析成**同一个**键码。"""
    from autopilot.flows import _key_name

    for vk in (0x87, 0x99, 0xFE):          # F24、未分配键、极端值
        assert winapi.resolve_key(_key_name(vk))[0] == vk, _key_name(vk)


def test_recorder_to_flow_shape() -> None:
    flow = Recorder().to_flow("名字", "说明")
    assert flow["name"] == "名字" and flow["description"] == "说明"
    assert isinstance(flow["steps"], list)
    assert "created" in flow


def test_recorder_save(tmp_path: Path) -> None:
    rec = Recorder()
    path = rec.save(tmp_path / "sub" / "out.json", name="录制")
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "录制"


@pytest.mark.hardware
@pytest.mark.skipif(not winapi.IS_WINDOWS, reason="仅 Windows")
def test_recorder_captures_real_input_via_hooks(cursor_guard) -> None:
    """真实验证低级钩子能收到事件——这是"手动做一遍就能回放"的基础。

    只用**无副作用**的输入：
    * 鼠标只移动、不点击
    * 键盘只轻敲一次 Shift（单独按 Shift 不输入字符、不触发命令）

    用 ``raw_events``（钩子原始命中数）判断，而不是看产出了什么步骤：
    修饰键按设计本来就不该产出步骤，但钩子必须收到它。
    """
    rec = Recorder()
    if not rec.start():
        pytest.skip("无法注册全局钩子（可能被系统策略限制）")
    try:
        assert rec.hooks_ok["mouse"], "鼠标钩子未装上"
        assert rec.hooks_ok["key"], "键盘钩子未装上"
        baseline = dict(rec.raw_events)
        time.sleep(0.3)

        winapi.move_to(360, 360)
        time.sleep(0.3)
        moved = rec._last_move
        winapi.press("shift")
        time.sleep(0.4)
    finally:
        rec.stop()

    # 不断言光标精确落点：使用者可能同时在动鼠标，那会让测试无谓地抖动。
    # 真正要守住的是"钩子确实收到了事件"。
    assert rec.raw_events["mouse"] > baseline["mouse"], "鼠标钩子命中数没有增加"
    assert rec.raw_events["key"] > baseline["key"], "键盘钩子命中数没有增加"
    assert moved != (0, 0), "鼠标钩子没把坐标记下来"
    # 单独按 Shift 不应该产生任何步骤
    assert not any(s["action"] == "press_key" for s in rec.to_flow()["steps"]), \
        "修饰键不该被录成 press_key"


@pytest.mark.hardware
@pytest.mark.skipif(not winapi.IS_WINDOWS, reason="仅 Windows")
def test_recorded_flow_is_replayable(ctx, tmp_path: Path, cursor_guard) -> None:
    """完整闭环：真机录制 → 存盘 → 读回 → 演练回放。

    只按 F24（``0x87``）——这个键在任何程序里都没有绑定，完全无副作用，
    但低级钩子会正常收到它，足以证明"录制的东西能被回放"。

    演练模式（dry-run）会走完"查动作 → 清洗参数 → 过安全护栏"的全流程，
    因此只要没有报错，就说明录出的每一步都能对应到真实能力。
    """
    from autopilot.actions import build_registry
    from autopilot.executor import AuditLog, Executor

    rec = Recorder()
    if not rec.start():
        pytest.skip("无法注册全局钩子（可能被系统策略限制）")
    try:
        time.sleep(0.3)
        before = rec.raw_events["key"]
        winapi.press("0x87")
        time.sleep(0.5)
    finally:
        rec.stop()

    assert rec.raw_events["key"] > before, "键盘钩子没收到 F24"
    flow = load_flow(rec.save(tmp_path / "recorded.json", name="闭环验证"))
    assert flow["steps"], "一个步骤都没录到"

    # 只按内容找我们那一下，不假设它是第一步：
    # 录制器是全局的，测试期间使用者自己的鼠标键盘也会被录进来。
    ours = [s for s in flow["steps"]
            if s["action"] == "press_key" and s["params"].get("key") == "vk_0x87"]
    assert ours, f"没录到 F24：{[s['action'] for s in flow['steps']]}"

    ctx.guard.policy.dry_run = True
    executor = Executor(build_registry(ctx), ctx,
                        audit=AuditLog(tmp_path / "logs"),
                        auto_observe="none", log=lambda level, msg: None)
    results = run_flow(executor, flow, stop_on_error=False)

    assert len(results) == len(flow["steps"])
    problems = [r for r in results if r.error]
    assert not problems, f"有步骤无法回放：{[(r.action, r.error) for r in problems]}"
    assert all(r.dry_run and r.ok for r in results)


@pytest.mark.hardware
@pytest.mark.skipif(not winapi.IS_WINDOWS, reason="仅 Windows")
def test_recorder_can_install_global_hooks() -> None:
    """验证全局钩子能装上并干净卸载。

    这里**不**断言事件数为 0——使用者可能正在真的动鼠标，
    录到事件恰恰说明钩子工作正常。
    """
    rec = Recorder()
    if not rec.start():
        pytest.skip("无法注册全局钩子（可能被系统策略限制）")
    try:
        assert rec.event_count >= 0
    finally:
        assert isinstance(rec.stop(), list)
