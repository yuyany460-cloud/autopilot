# -*- coding: utf-8 -*-
"""复核机制测试：动作接线、提示词规则、以及"空返回"这个坑。

背景（真实踩坑）：超星作业 5 道单选题错了 1 道。
题目是 `x = 0 是 y = e^(-1/x²) 的`，正确答案是**可去间断点**
（该点无定义、但极限存在且有限），Agent 答了「连续点」。
而同一份作业里结构完全相同的 `x·sin(1/x)` 它却答对了「可去间断点」——
两道同型题给出互相矛盾的答案，正是缺一次独立复算。
"""

from __future__ import annotations

import pytest

from autopilot.actions import build_registry
from autopilot.llm import LLMError, LLMResponse, review


# --- 复核动作接线 -------------------------------------------------------


def test_double_check_action_exists_and_is_read_only(ctx) -> None:
    act = build_registry(ctx).get("double_check")
    assert act is not None
    assert act.mutates is False, "复核不该触发重新感知"
    names = [p.name for p in act.params]
    assert "question" in names and "my_answer" in names


def test_double_check_verdict_is_parsed(ctx, monkeypatch) -> None:
    """返回里必须把「一致/不一致」提到最前面，方便模型一眼看到。"""

    def fake_review(question, my_answer="", context="", workspace=None, **kw):
        return LLMResponse(content="推理过程……\n结论：不一致（正确答案是 D）",
                           usage={}, elapsed_ms=1234.0)

    monkeypatch.setattr("autopilot.actions.llm.review", fake_review)
    out = build_registry(ctx).get("double_check").call(
        question="x=0 是 y=e^{-1/x^2} 的", my_answer="连续点")
    assert "不一致" in out
    assert out.index("复核结果不一致") < 40, "结论要放最前面"
    assert "正确答案是 D" in out


def test_double_check_reports_agreement(ctx, monkeypatch) -> None:
    monkeypatch.setattr(
        "autopilot.actions.llm.review",
        lambda *a, **k: LLMResponse(content="结论：一致", usage={}, elapsed_ms=800.0))
    out = build_registry(ctx).get("double_check").call(question="1+1=?", my_answer="2")
    assert "复核一致" in out


# --- review() 本身 ------------------------------------------------------


def test_review_asks_for_independent_solving_first() -> None:
    """必须要求"先独立解、最后才对比"。

    反过来问「这个答案对吗」，模型会顺着你的答案说对——那就白复核了。
    """
    import inspect

    src = inspect.getsource(review)
    assert "先完全独立地解题" in src
    assert src.index("先完全独立地解题") < src.index("待复核答案")


def test_review_raises_when_only_reasoning_came_back(monkeypatch, tmp_path) -> None:
    """只回思维链、没有答案时要报错，不能返回空字符串。

    真实踩过的坑：pro 默认开思考模式，思维链按 completion token 计费。
    max_tokens 给小了（1600），推理就把额度吃光，content 直接是空的
    （usage 里 completion=1600 而 reasoning=1600）。
    """
    from autopilot import llm

    monkeypatch.setattr(llm, "resolve_api_key", lambda ws=None: "sk-test")

    class _Client:
        def __init__(self, *a, **k):
            pass

        def chat(self, *a, **k):
            return LLMResponse(content="", usage={
                "completion_tokens": 1600,
                "completion_tokens_details": {"reasoning_tokens": 1600}}, elapsed_ms=1.0)

    monkeypatch.setattr(llm, "LLMClient", _Client)
    # 现在会先关掉思考重试一次；仍然空才报错（诚实说明拿不到答案）
    with pytest.raises(LLMError, match="仍未给出答案"):
        review("题目", "答案", workspace=tmp_path)


def test_review_uses_low_effort() -> None:
    """复核要有思考，但不需要 high——实测同一题 high 23s / low 12s，结论一样。"""
    import inspect

    assert '"reasoning_effort": "low"' in inspect.getsource(review)


# --- 提示词规则 ---------------------------------------------------------


def test_prompt_forbids_answering_by_letter() -> None:
    """必须明确禁止"凭字母记答案"。

    超星会打乱选项顺序，DOM 的 data 属性是原始序号，和屏幕上的 A/B/C/D 不一致
    （实测见到 `A (data=D)`）。
    """
    from autopilot.prompts import SYSTEM_PROMPT

    assert "按选项文字点击" in SYSTEM_PROMPT
    assert "data" in SYSTEM_PROMPT, "要点明 data 属性和显示字母不是一回事"
    assert "打乱选项顺序" in SYSTEM_PROMPT



def test_prompt_warns_against_narrow_image_prompts() -> None:
    """读题面图片时用「只输出公式」会把题干文字丢掉。

    实测：Agent 用 `prompt="只输出图中的数学公式本身"` 读 Q2，结果
    「设 f(x)=x·sin(1/x)，则 x=0 是 f(x) 的」被读成了
    `f(x)=x sin(1/x), x=0, f(x)`——「设…则…是…的」全丢了。
    """
    from autopilot.prompts import SYSTEM_PROMPT

    assert "只输出公式" in SYSTEM_PROMPT
    assert "selector=" in SYSTEM_PROMPT, "要告诉它可以给整道题的容器选择器"


def test_prompt_mentions_the_real_mistake() -> None:
    """把真实踩过的坑写进提示词，比抽象说教有用。"""
    from autopilot.prompts import SYSTEM_PROMPT

    assert "有没有定义" in SYSTEM_PROMPT
    assert "可去间断点" in SYSTEM_PROMPT
