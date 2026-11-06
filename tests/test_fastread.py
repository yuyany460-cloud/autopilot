# -*- coding: utf-8 -*-
"""fastread：先快（常驻 OCR）后准（视觉模型）的路由逻辑。"""

from __future__ import annotations

import pytest

from autopilot import fastread


def _png(tmp_path):
    path = tmp_path / "shot.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    return path


CLEAN = "简答题：请说明操作系统的主要功能，并举例说明进程与线程的区别。"

GARBLED = "跸 哩 跸 哩 （ 。 一 。 ） 0 口 干 杯 一 一 b 山 b 山 ×"


# -- 质量打分 -------------------------------------------------------------

def test_garble_score_separates_clean_from_noise():
    assert fastread.garble_score(CLEAN) < 0.34
    assert fastread.garble_score(GARBLED) > 0.34


def test_empty_text_counts_as_garbage():
    assert fastread.garble_score("") == 1.0
    assert fastread.garble_score("   \n  ") == 1.0


def test_garble_score_is_bounded():
    for text in (CLEAN, GARBLED, "a", "．，；：（）" * 20):
        assert 0.0 <= fastread.garble_score(text) <= 1.0


# -- 路由 ----------------------------------------------------------------

def _fake_ocr(monkeypatch, reader, text, ok=True):
    monkeypatch.setattr(reader.daemon, "read", lambda path, lang=None: {
        "ok": ok, "engine": "zh-Hans-CN", "text": text,
        "ms": 7, "wall_ms": 7.5})


def _spy_vision(monkeypatch, text="视觉结果"):
    calls = []

    def fake(path, prompt="", workspace=None, max_tokens=700):
        calls.append(path)
        return {"text": text, "ms": 900.0, "cached": False}

    monkeypatch.setattr(fastread.vision, "read_image", fake)
    return calls


def test_auto_keeps_clean_ocr_result_and_skips_vision(tmp_path, monkeypatch):
    reader = fastread.FastReader()
    _fake_ocr(monkeypatch, reader, CLEAN)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path))

    assert res["source"] == "ocr"
    assert res["text"] == CLEAN
    assert res["ms"] == 7.5
    assert calls == []
    assert reader.stats["vision"] == 0


def test_auto_escalates_when_ocr_looks_garbled(tmp_path, monkeypatch):
    reader = fastread.FastReader()
    _fake_ocr(monkeypatch, reader, GARBLED)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path))

    assert res["source"] == "vision"
    assert res["text"] == "视觉结果"
    assert len(calls) == 1
    assert reader.stats == {"ocr": 0, "vision": 1, "escalated": 1}


def test_auto_escalates_when_ocr_fails(tmp_path, monkeypatch):
    reader = fastread.FastReader()

    def boom(path, lang=None):
        raise RuntimeError("进程没了")

    monkeypatch.setattr(reader.daemon, "read", boom)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path))

    assert res["source"] == "vision"
    assert "OCR" in res["note"]
    assert len(calls) == 1


def test_math_prompt_forces_vision_even_if_ocr_looks_fine(tmp_path, monkeypatch):
    reader = fastread.FastReader()
    _fake_ocr(monkeypatch, reader, CLEAN)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path), prompt="只输出公式本身")

    assert res["source"] == "vision"
    assert len(calls) == 1


def test_mode_ocr_never_escalates(tmp_path, monkeypatch):
    reader = fastread.FastReader()
    _fake_ocr(monkeypatch, reader, GARBLED)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path), mode="ocr")

    assert res["source"] == "ocr"
    assert calls == []


def test_mode_vision_skips_ocr_entirely(tmp_path, monkeypatch):
    reader = fastread.FastReader()

    def never(path, lang=None):
        raise AssertionError("mode=vision 不该跑 OCR")

    monkeypatch.setattr(reader.daemon, "read", never)
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path), mode="vision")

    assert res["source"] == "vision"
    assert len(calls) == 1


def test_missing_file_raises(tmp_path):
    reader = fastread.FastReader()
    with pytest.raises(Exception):
        reader.read(tmp_path / "nope.png")


def test_wants_precision_detection():
    assert fastread._wants_precision("只输出公式本身")
    assert fastread._wants_precision("把这段代码转成 Python")
    assert fastread._wants_precision("读出 √x 的值")
    assert not fastread._wants_precision("")
    assert not fastread._wants_precision("读出这张图片里的文字")

# -- 汉字间空格与代码识别 -------------------------------------------------

def test_join_cjk_removes_spaces_between_hanzi():
    """Windows OCR 给每个汉字之间插一个空格，必须先并回来再判质量。"""
    assert fastread.join_cjk("使 用 函 数 求 素 数 和") == "使用函数求素数和"
    assert fastread.join_cjk("答 案 正 确 ， 通 过 。") == "答案正确，通过。"
    # 英文/数字之间的空格不能动
    assert fastread.join_cjk("print i ++") == "print i ++"


def test_spaced_chinese_is_not_garbage():
    """字间空格 ≠ 乱码：并完以后分数要低。"""
    spaced = "请 说 明 进 程 与 线 程 的 区 别"
    assert fastread.garble_score(fastread.join_cjk(spaced)) < 0.18
    # 不并的话会被误判成一脸乱码
    assert fastread.garble_score(spaced) > 0.34


def test_code_like_text_is_flagged():
    assert fastread.looks_like_code("#include <stdio.h>")
    assert fastread.looks_like_code("int main(void) { return 0; }")
    assert not fastread.looks_like_code("请说明进程与线程的区别")


def test_cjk_ratio_separates_chinese_from_code():
    assert fastread.cjk_ratio("请说明进程与线程的区别") > 0.9
    assert fastread.cjk_ratio("if (x <= 0) { return -1; }") < 0.1


def test_auto_escalates_when_text_looks_like_code(tmp_path, monkeypatch):
    """干净但像代码的 OCR 结果也要升级：标识符它读不准。"""
    reader = fastread.FastReader()
    _fake_ocr(monkeypatch, reader, "int main(void) { return 0; }")
    calls = _spy_vision(monkeypatch)

    res = reader.read(_png(tmp_path))

    assert res["source"] == "vision"
    assert len(calls) == 1
