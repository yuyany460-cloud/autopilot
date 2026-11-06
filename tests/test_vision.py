# -*- coding: utf-8 -*-
"""视觉层测试：图片内容块、可用性判断、以及"读图优先用视觉"的接线。

背景（实测）：同一张"带缩进的 C 代码 + 一行数学公式"的图，
Windows OCR 行命中 1/12（8%，空格被拆散、0 读成 9），
视觉模型 11/12（92%，缩进和符号全对）。
所以读图和读公式默认走视觉模型，OCR 只做兜底。
"""

from __future__ import annotations

import base64
import struct
import zlib
from pathlib import Path

import pytest

from autopilot import vision


def _png(path: Path, width: int = 4, height: int = 4) -> Path:
    """写一个真的最小 PNG 出来（不依赖 PIL）。"""
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return path


# --- 图片内容块 ---------------------------------------------------------


def test_image_block_is_openai_compatible(tmp_path) -> None:
    p = _png(tmp_path / "a.png")
    block = vision.image_block(p)
    assert block["type"] == "image_url"
    url = block["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == p.read_bytes()


def test_image_block_keeps_original_detail(tmp_path) -> None:
    """代码和公式必须用 original——缩到 512x512 就读不清了。"""
    block = vision.image_block(_png(tmp_path / "a.png"))
    assert block["image_url"]["detail"] == "original"


def test_image_block_guesses_mime_from_suffix(tmp_path) -> None:
    p = _png(tmp_path / "a.jpg")           # 内容还是 PNG，但后缀决定 MIME
    assert vision.image_block(p)["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_image_block_rejects_missing_file(tmp_path) -> None:
    with pytest.raises(vision.VisionUnavailable):
        vision.image_block(tmp_path / "nope.png")


def test_default_prompt_protects_code_and_formula() -> None:
    """默认指令必须点明"保留缩进"和"不要化简公式"，否则模型爱自作主张。"""
    assert "缩进" in vision.DEFAULT_PROMPT
    assert "不要化简" in vision.DEFAULT_PROMPT
    assert "√" in vision.DEFAULT_PROMPT


# --- 可用性 -------------------------------------------------------------


def test_is_available_reflects_api_key(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(vision, "resolve_api_key", lambda ws=None: "sk-test")
    assert vision.is_available(tmp_path) is True
    monkeypatch.setattr(vision, "resolve_api_key", lambda ws=None: "")
    assert vision.is_available(tmp_path) is False


def test_read_image_without_key_raises_clear_error(monkeypatch, tmp_path) -> None:
    """到处都没有 key 时要报一句能看懂的错，而不是把空 key 发出去。

    注意两处都要打：``LLMConfig.resolved()`` 会用**无参**的 ``resolve_api_key()``
    再兜一次底，只打 vision 这边的话真正的 key 还是会被捡回来。
    """
    monkeypatch.setattr(vision, "resolve_api_key", lambda ws=None: "")
    monkeypatch.setattr("autopilot.llm.resolve_api_key", lambda *a, **k: "")
    p = _png(tmp_path / "a.png")
    with pytest.raises(vision.VisionUnavailable, match="API"):
        vision.read_image(p, workspace=tmp_path)


# --- 动作接线 -----------------------------------------------------------


def test_browser_read_image_uses_auto_by_default(ctx) -> None:
    """默认 auto：先让常驻 OCR 秒出，判为乱码（或要公式/代码）才升级视觉模型。

    换成 vision 也行，但小图 OCR 只要 6~8 毫秒、视觉要 1~5 秒，
    而清晰中文题干正是 OCR 的强项——所以默认走 auto。
    """
    from autopilot.actions import build_registry

    act = build_registry(ctx).get("browser_read_image")
    methods = [p for p in act.params if p.name == "method"][0]
    assert methods.default == "auto"
    assert "vision" in methods.enum and "ocr" in methods.enum


def test_browser_read_image_falls_back_to_ocr(ctx, monkeypatch) -> None:
    """没有视觉能力时必须自动退回 OCR，而不是直接失败。

    直接跑动作需要真浏览器，太重；这里验证的是"视觉抛错被接住并转成提示"
    这段逻辑在源码里确实存在且会退回 OCR 分支。
    """
    import inspect

    from autopilot import actions

    src = inspect.getsource(actions.build_registry)
    assert "已退回 OCR" in src
    assert "psbridge.bridge().ocr" in src


def test_vision_actions_are_read_only(ctx) -> None:
    from autopilot.actions import build_registry

    reg = build_registry(ctx)
    for name in ("browser_read_image", "read_image", "read_screen"):
        act = reg.get(name)
        assert act is not None, f"缺少动作 {name}"
        assert act.mutates is False, f"{name} 是纯读，不该触发重新感知"


class _FakeGrab:
    width, height = 1499, 770

    def to_bytes(self, fmt: str = "png") -> bytes:
        return b"\x89PNG-fake"


def _patch_screen(monkeypatch, seen: dict) -> None:
    from autopilot import actions, winapi, windows

    class _Win:
        title, process = "记事本", "notepad.exe"
        rect = winapi.Rect(100, 50, 1599, 820)

    monkeypatch.setattr(actions.screen, "capture", lambda *a, **k: _FakeGrab())
    monkeypatch.setattr(windows, "get_foreground", lambda: _Win())

    def fake_read_bytes(data, suffix=".png", prompt="", workspace=None, max_tokens=0):
        seen["prompt"] = prompt
        seen["data"] = data
        return {"text": "看到「开始加速」按钮", "ms": 12.0, "usage": {}}

    monkeypatch.setattr(actions.vision, "read_bytes", fake_read_bytes)


def test_read_screen_sends_real_size_and_origin_hint(ctx, monkeypatch) -> None:
    """提示词里必须给出**真实像素尺寸**，返回里必须给出窗口偏移。

    踩过的坑：默认指令没写尺寸，模型自己"假设窗口是 1920x1080"，
    给出的坐标全错（实际窗口 1499x770）。
    """
    from autopilot.actions import build_registry

    seen: dict = {}
    _patch_screen(monkeypatch, seen)
    out = build_registry(ctx).get("read_screen").call(region="window")

    assert "1499x770" in seen["prompt"], "要把真实尺寸告诉模型，否则它自己瞎假设"
    assert "1920x1080" in seen["prompt"], "要明确警告它不要假设成 1920x1080"
    assert "缩进" in seen["prompt"], "代码/公式要强调保留缩进"
    assert "(100,50)" in out, "要告诉模型图内坐标怎么换算回屏幕坐标"
    assert "看到「开始加速」按钮" in out


def test_read_image_reports_missing_file(ctx) -> None:
    from autopilot.actions import build_registry

    with pytest.raises(ValueError, match="不存在"):
        build_registry(ctx).get("read_image").call(path="var/绝对没有这张图.png")
