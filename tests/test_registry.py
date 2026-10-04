# -*- coding: utf-8 -*-
"""动作注册表测试：schema 生成与参数清洗（模型给的参数不能直接透传进 handler）。"""

from __future__ import annotations

import pytest

from autopilot.actions import build_registry
from autopilot.registry import Action, Danger, Param, Registry


def test_all_actions_have_valid_schema(ctx) -> None:
    reg = build_registry(ctx)
    assert len(reg.names()) >= 50
    for act in reg.all():
        schema = act.schema()
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for name, prop in schema["properties"].items():
            assert prop["type"] in ("string", "integer", "number", "boolean",
                                    "object", "array"), (act.name, name)
        for required in schema.get("required", []):
            assert required in schema["properties"], (act.name, required)


def test_openai_tools_hides_high_danger_by_default(ctx) -> None:
    reg = build_registry(ctx)
    safe = {t["function"]["name"] for t in reg.openai_tools(allow_danger=False)}
    allnames = {t["function"]["name"] for t in reg.openai_tools(allow_danger=True)}
    assert "delete_path" in allnames
    assert "delete_path" not in safe
    assert "power" not in safe
    assert "click" in safe


def test_alias_resolution(ctx) -> None:
    reg = build_registry(ctx)
    assert reg.get("click_element").name == "click"
    assert reg.get("type").name == "type_text"
    assert reg.get("focus_window").name == "activate_window"
    assert reg.get("nope") is None


def test_case_insensitive_lookup(ctx) -> None:
    reg = build_registry(ctx)
    assert reg.get("CLICK") is not None
    assert reg.get("take_screenshot".replace("take_", "")).name == "screenshot"


# --- 参数清洗 -----------------------------------------------------------


def _registry_with_probe() -> tuple[Registry, list]:
    reg = Registry()
    seen: list[dict] = []

    def handler(must: str, text: str = "默认", times: int = 1) -> str:
        seen.append({"must": must, "text": text, "times": times})
        return "ok"

    reg.register(Action("probe", "探测", [
        Param("text", "string", default="默认"),
        Param("times", "integer", default=1),
        Param("must", "string", required=True),
    ], handler, Danger.SAFE))
    return reg, seen


def test_unknown_params_are_dropped() -> None:
    reg, seen = _registry_with_probe()
    reg.get("probe").call(text="hi", must="x", 不要的参数="危险", another=1)
    assert seen == [{"must": "x", "text": "hi", "times": 1}]


def test_defaults_are_filled() -> None:
    reg, seen = _registry_with_probe()
    reg.get("probe").call(must="x")
    assert seen == [{"must": "x", "text": "默认", "times": 1}]


def test_missing_required_raises() -> None:
    reg, _ = _registry_with_probe()
    with pytest.raises(ValueError, match="缺少必填参数"):
        reg.get("probe").call(text="hi")


def test_mismatched_handler_signature_rejected_at_registration() -> None:
    """声明了 handler 不接受的参数，应该在注册时就报错，而不是任务跑一半才炸。"""
    reg = Registry()
    with pytest.raises(ValueError, match="handler 不接受的参数"):
        reg.register(Action("bad", "坏动作", [Param("不存在")],
                            lambda: "x", Danger.SAFE))


def test_duplicate_registration_rejected() -> None:
    reg, _ = _registry_with_probe()
    with pytest.raises(ValueError, match="重名"):
        reg.register(Action("probe", "重复", [], lambda: None))


def test_categories_cover_all_actions(ctx) -> None:
    reg = build_registry(ctx)
    listed = {a.name for acts in reg.categories().values() for a in acts}
    assert listed == set(reg.names())
    for expected in ("感知", "鼠标", "键盘", "窗口", "文件", "系统", "浏览器", "控制"):
        assert expected in reg.categories()


def test_danger_labels() -> None:
    assert Danger.SAFE.rank < Danger.LOW.rank < Danger.MEDIUM.rank < Danger.HIGH.rank
    assert Danger.HIGH.label == "危险"
