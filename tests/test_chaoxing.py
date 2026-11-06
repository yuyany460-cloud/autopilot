# -*- coding: utf-8 -*-
"""超星整卷读取：解析、去重、标签、下载、单次视觉调用。

这里的用例全部来自真机（2026-10-19 抓的作业2-2 结构）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autopilot import chaoxing


def _item(page: str, num: str, stem_imgs: list[str], opts: list[tuple[str, list[str]]]) -> dict:
    return {"page": page, "id": f"{page}{num}", "stemText": f"{num}. (单选题)",
            "stemImgs": stem_imgs, "opts": [{"label": f"{lab}.", "imgs": imgs} for lab, imgs in opts]}


def _caller(payload: dict):
    return lambda op, params=None, **kw: json.dumps(payload, ensure_ascii=False)


def test_extract_dedupes_view_and_dowork_and_sorts() -> None:
    """判定页和作答页各有一份，去重后要 10 题且按题号排序。"""
    items = []
    for num in ("1", "10", "2"):
        items.append(_item("view", num, [f"http://cdn/{num}a.png"], [("A", ["http://cdn/x.png"])]))
    for num in ("1", "10", "2"):
        items.append(_item("dowork", num, [f"http://cdn/{num}b.png"], [("A", ["http://cdn/y.png"])]))
    got = chaoxing.extract(_caller({"count": len(items), "items": items}))
    assert [chaoxing._question_number(it) for it in got] == ["1", "2", "10"]
    assert all(it["page"] == "dowork" for it in got), "重复时应保留作答页那份"


def test_extract_rejects_garbage() -> None:
    with pytest.raises(RuntimeError, match="解析失败"):
        chaoxing.extract(lambda *a, **k: "not json")


def test_as_blocks_labels_stem_and_options() -> None:
    item = _item("dowork", "3", ["http://cdn/stem.png"],
                 [("A", ["http://cdn/a.png"]), ("B", ["http://cdn/b.png"])])
    blocks = chaoxing.as_blocks(item)
    assert [lab for lab, _ in blocks] == ["题干", "选项A", "选项B"]
    assert blocks[0][1] == "http://cdn/stem.png"


def test_prompt_only_asks_for_existing_parts() -> None:
    """填空题没有选项，就不该让它吐 A~D 那四行。"""
    only_stem = chaoxing._prompt_for(["题干"])
    assert "题干：" in only_stem
    assert "选项A" not in only_stem and "\nA：" not in only_stem


def test_download_upgrades_to_https_and_caches(tmp_path: Path, monkeypatch) -> None:
    """页面是 https、图片是 http —— 必须升级成 https，否则被当混合内容拦。"""
    seen: list[str] = []

    class _Resp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=0):
        seen.append(req.full_url)
        return _Resp(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

    monkeypatch.setattr(chaoxing.urllib.request, "urlopen", fake_urlopen)
    urls = ["http://cdn/a.png", "http://cdn/a.png", "http://cdn/b.png"]
    saved = chaoxing.download(urls, tmp_path)
    assert len(seen) == 2, "同一张图只该下载一次"
    assert all(u.startswith("https://") for u in seen)
    assert len(saved) == 2 and all(p.is_file() for p in saved.values())

    # 第二次调用全部命中缓存，不再发请求
    seen.clear()
    chaoxing.download(urls, tmp_path)
    assert seen == []


def test_read_item_sends_all_parts_in_one_call(tmp_path: Path, monkeypatch) -> None:
    item = _item("dowork", "1", ["http://cdn/stem.png"],
                 [("A", ["http://cdn/a.png"]), ("B", ["http://cdn/b.png"])])
    calls: list[list[tuple[str, Path]]] = []

    monkeypatch.setattr(chaoxing, "download",
                        lambda urls, dest, timeout=12.0: {u: tmp_path / f"{i}.png"
                                                          for i, u in enumerate(urls)})
    monkeypatch.setattr(chaoxing.vision, "read_images",
                        lambda pairs, prompt="", workspace=None, max_tokens=0: (
                            calls.append(list(pairs)) or {"text": "题干：x", "ms": 12.0}))

    got = chaoxing.read_item(item, tmp_path)
    assert got["images"] == 3 and got["text"] == "题干：x"
    assert len(calls) == 1, "五张图要一次送进去，不是分五次"
    assert [lab for lab, _ in calls[0]] == ["题干", "选项A", "选项B"]


def test_summary_mentions_counts() -> None:
    items = [_item("dowork", "1", ["http://cdn/s.png"], [("A", ["http://cdn/a.png"])])]
    text = chaoxing.summary(items)
    assert "整卷 1 题" in text and "题干图 1 张" in text


def test_vision_read_images_labels_each_picture(tmp_path: Path, monkeypatch) -> None:
    """一次多图必须带标签，否则模型分不清哪张是题干、哪张是选项。"""
    from autopilot import vision

    png = tmp_path / "a.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)

    captured: dict = {}

    class _Resp:
        content = "题干：x"
        elapsed_ms = 1.0
        usage: dict = {}

    class _Client:
        def chat(self, messages, temperature=0.0):
            captured["content"] = messages[0]["content"]
            return _Resp()

    monkeypatch.setattr(vision, "_client", lambda ws, mt: _Client())
    out = vision.read_images([("题干", png), ("选项A", png)], prompt="读")
    assert out["text"] == "题干：x"
    kinds = [b.get("type") for b in captured["content"]]
    assert kinds.count("image_url") == 2
    assert [b["text"] for b in captured["content"] if b.get("type") == "text"] == [
        "读", "【题干】", "【选项A】"]
