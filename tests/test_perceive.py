# -*- coding: utf-8 -*-
"""感知层测试：元素编号、查询、渲染，以及 OCR 文本清洗。"""

from __future__ import annotations

import time

from autopilot.perceive import Element, OcrLine, Snapshot, _build_elements, normalize_ocr_text
from autopilot.winapi import Rect


def _raw(name: str, ctype: str, x: int, y: int, w: int = 80, h: int = 30,
         patterns: list[str] | None = None, **extra) -> dict:
    return {"name": name, "type": ctype, "class": "", "aid": "",
            "rect": [x, y, w, h], "enabled": True, "patterns": patterns or [],
            "value": extra.get("value"), "depth": extra.get("depth", 2)}


# --- 元素构建 -----------------------------------------------------------


def test_elements_get_reading_order_ids() -> None:
    raw = [
        _raw("右下", "Button", 800, 500, patterns=["Invoke"]),
        _raw("左上", "Button", 10, 10, patterns=["Invoke"]),
        _raw("右上", "Button", 800, 10, patterns=["Invoke"]),
    ]
    elements, _ = _build_elements(raw)
    assert [e.name for e in elements] == ["左上", "右上", "右下"]
    assert [e.id for e in elements] == ["E1", "E2", "E3"]


def test_large_layout_container_is_demoted() -> None:
    """占满屏幕的 Pane 不该抢走 E1，把可点击的控件挤到后面。"""
    raw = [
        _raw("大容器", "Pane", 0, 0, w=1800, h=1100),
        _raw("确定", "Button", 100, 200, patterns=["Invoke"]),
    ]
    elements, _ = _build_elements(raw)
    assert elements[0].name == "确定"
    assert elements[-1].name == "大容器"


def test_zero_size_and_empty_elements_filtered() -> None:
    raw = [
        _raw("", "Pane", 0, 0, w=0, h=0),               # 无尺寸
        _raw("", "Pane", 5, 5),                          # 无名字无操作 -> 丢掉
        _raw("有名字", "Text", 10, 10),
        _raw("", "Button", 20, 20, patterns=["Invoke"]),  # 无名字但有操作 -> 保留
    ]
    elements, _ = _build_elements(raw)
    names = [e.name for e in elements]
    assert "有名字" in names
    assert "" in names  # 那个可点击的无名按钮
    assert len(elements) == 2


def test_noise_control_types_dropped() -> None:
    raw = [_raw("装饰图", "Image", 10, 10), _raw("按钮", "Button", 10, 60, patterns=["Invoke"])]
    elements, _ = _build_elements(raw)
    assert [e.name for e in elements] == ["按钮"]


def test_duplicate_elements_deduped() -> None:
    raw = [_raw("确定", "Button", 10, 10, patterns=["Invoke"])] * 3
    elements, deduped = _build_elements(raw)
    assert len(elements) == 1 and deduped


def test_actionable_flag() -> None:
    elements, _ = _build_elements([
        _raw("可点", "Button", 0, 0, patterns=["Invoke"]),
        _raw("静态", "Text", 0, 100, patterns=["Text"]),
    ])
    assert elements[0].actionable and not elements[1].actionable


# --- OCR 文本清洗 -------------------------------------------------------


def test_cjk_spaces_are_removed() -> None:
    assert normalize_ocr_text("自 动 化 电 脑") == "自动化电脑"


def test_latin_spaces_preserved() -> None:
    assert normalize_ocr_text("hello world 123") == "hello world 123"


def test_mixed_text_keeps_latin_spacing() -> None:
    assert normalize_ocr_text("打开 Notepad 应用") == "打开 Notepad 应用"


def test_empty_text() -> None:
    assert normalize_ocr_text("") == ""


# --- 快照查询 -----------------------------------------------------------


def _snapshot() -> Snapshot:
    elements, _ = _build_elements([
        _raw("保存", "Button", 100, 200, patterns=["Invoke"]),
        _raw("取消", "Button", 200, 200, patterns=["Invoke"]),
        _raw("文件名", "Edit", 100, 100, patterns=["Value"], value="报告.txt"),
        _raw("大容器", "Pane", 0, 0, w=1000, h=800),
    ])
    return Snapshot(
        ts=time.time(), screen_size=(1920, 1200), cursor=(0, 0), scale=1.25,
        foreground=None, window_list=[], elements=elements,
        ocr_text="屏幕上的一些文字", ocr_lines=[
            OcrLine("一行文字", Rect(10, 20, 110, 40)),
        ], notes=["备注一条"],
    )


def test_element_ids_follow_reading_order() -> None:
    snap = _snapshot()
    # 大容器被沉到最后，其余按从上到下、从左到右
    assert [e.name for e in snap.elements] == ["文件名", "保存", "取消", "大容器"]


def test_element_lookup_by_id_and_name() -> None:
    snap = _snapshot()
    assert snap.element("E1").name == "文件名"
    assert snap.element("1").name == "文件名"
    assert snap.element("取消").name == "取消"
    assert snap.element("不存在") is None
    assert snap.element("E999") is None


def test_find_and_actionable_filter() -> None:
    snap = _snapshot()
    assert len(snap.find("文")) >= 1
    assert all(e.actionable for e in snap.find("", actionable_only=True))
    assert [e.name for e in snap.find("文件", control_type="Edit")] == ["文件名"]


def test_at_point_returns_smallest_first() -> None:
    snap = _snapshot()
    hits = snap.at(150, 210)
    assert hits[0].name == "保存"  # 小控件优先于大容器


def test_element_center_is_clickable() -> None:
    snap = _snapshot()
    # 文件名 Edit @(100,100)，默认尺寸 80x30 -> 中心 (140,115)
    assert snap.element("E1").center == (140, 115)
    assert snap.element("E1").label.startswith("E1 Edit")


def test_render_contains_key_sections() -> None:
    snap = _snapshot()
    text = snap.render()
    assert "# 屏幕快照" in text
    assert "## 可交互元素" in text
    assert "## 屏幕文字（OCR）" in text
    assert "E1 Edit" in text
    assert "备注一条" in text


def test_render_truncates_elements() -> None:
    snap = _snapshot()
    text = snap.render(max_elements=1)
    assert "其余 3 个已省略" in text


def test_as_dict_is_json_serialisable() -> None:
    import json

    payload = json.dumps(_snapshot().as_dict(), ensure_ascii=False, default=str)
    assert "elements" in payload and "ocr_lines" in payload


def test_long_element_name_truncated_in_label() -> None:
    el = Element(index=1, name="很长的名字" * 30, control_type="Button",
                 class_name="", rect=Rect(0, 0, 10, 10), patterns=["Invoke"])
    assert len(el.label) < len(el.name)
    assert "..." in el.label


def test_observe_runs_the_three_collectors_concurrently(monkeypatch) -> None:
    """UIA / OCR / 存图 必须并行。

    串行时一次观察 = 457 + 754 + 102 ≈ 1.28 秒，全花在"等子进程和等磁盘"上。
    并行后约等于最慢的那一路。这里把三路都换成 sleep(0.3)：
    并行应当 ≈0.3s，串行会到 0.9s。
    """
    import time

    from autopilot import perceive

    def slow_uia(*a, **k):
        time.sleep(0.3)
        return [], None, False, None

    def slow_ocr(*a, **k):
        time.sleep(0.3)
        return "", [], None

    def slow_shot(*a, **k):
        time.sleep(0.3)
        return None, None

    monkeypatch.setattr(perceive, "_collect_uia", slow_uia)
    monkeypatch.setattr(perceive, "_collect_ocr", slow_ocr)
    monkeypatch.setattr(perceive, "_save_shot", slow_shot)

    started = time.perf_counter()
    perceive.observe(include_uia=True, include_ocr=True, save_image=True)
    elapsed = time.perf_counter() - started
    assert elapsed < 0.75, f"三段串行要 0.9s，实际 {elapsed:.2f}s——没有并行"


# --------------------------------------------------------------------------
# 自适应 OCR
#
# 系统 OCR 一步 ~0.75 秒，但对**自绘界面**（游戏、UU加速器）它是唯一的信息
# 来源，不能一刀切关掉。所以按"上次看这个程序时 UIA 给了多少元素"来定：
# UIA 已经讲清楚界面的程序就跳过 OCR，读不出元素的程序照跑。
# --------------------------------------------------------------------------


def _fake_fg(process: str):
    from autopilot import windows

    return windows.WindowInfo(hwnd=1, title="测试窗口", class_name="Fake",
                              pid=1, rect=Rect(0, 0, 800, 600), process=process)


def _patch_perceive(monkeypatch, n_elements: int):
    from autopilot import perceive, windows

    monkeypatch.setattr(windows, "get_foreground", lambda: _fake_fg("testapp.exe"))
    monkeypatch.setattr(perceive, "_collect_uia", lambda *a, **k: (
        [Element(index=i, name=f"按钮{i}", control_type="Button", class_name="",
                 rect=Rect(0, i * 10, 80, 20)) for i in range(1, n_elements + 1)],
        None, False, None))
    ran: list[int] = []
    monkeypatch.setattr(perceive, "_collect_ocr",
                        lambda *a, **k: (ran.append(1), ("", [], None))[1])
    return perceive, ran


def test_ocr_runs_the_first_time_we_see_a_program(monkeypatch) -> None:
    """没见过的程序必须先跑 OCR——它可能就是自绘界面。"""
    perceive, ran = _patch_perceive(monkeypatch, n_elements=20)
    perceive.reset_ocr_history()
    perceive.observe(include_uia=True, include_ocr=True)
    assert ran, "第一次见到一个程序就跑不得 OCR，否则自绘界面会什么都看不到"
    assert perceive.known_uia_rich("testapp.exe") == 20, "要记下它的元素数供下次判断"


def test_ocr_is_skipped_once_uia_proved_rich(monkeypatch) -> None:
    perceive, ran = _patch_perceive(monkeypatch, n_elements=20)
    perceive.reset_ocr_history()
    perceive.observe(include_uia=True, include_ocr=True)   # 第一次：认识它
    ran.clear()
    snap = perceive.observe(include_uia=True, include_ocr=True)
    assert not ran, "UIA 已经能讲清界面，不该再花 0.75 秒做 OCR"
    assert any("跳过 OCR" in n for n in snap.notes), "要让模型知道这次没有屏幕文字"


def test_ocr_keeps_running_for_self_drawn_windows(monkeypatch) -> None:
    """UIA 读不出元素的自绘界面，OCR 一次都不能省。"""
    perceive, ran = _patch_perceive(monkeypatch, n_elements=3)
    perceive.reset_ocr_history()
    for _ in range(3):
        perceive.observe(include_uia=True, include_ocr=True)
    assert len(ran) == 3, "自绘界面必须每次都跑 OCR"
    assert perceive.known_uia_rich("testapp.exe") == 0


def test_adaptive_can_be_turned_off(monkeypatch) -> None:
    """显式要"读屏幕上的字"时（get_screen_text / 控制台 OCR 开关），不能被跳过。"""
    perceive, ran = _patch_perceive(monkeypatch, n_elements=20)
    perceive.reset_ocr_history()
    perceive.observe(include_uia=True, include_ocr=True)   # 让它记住
    ran.clear()
    perceive.observe(include_uia=True, include_ocr=True, adaptive_ocr=False)
    assert ran, "关闭自适应后必须照跑 OCR"
