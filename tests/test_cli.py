# -*- coding: utf-8 -*-
"""CLI 任务目标的解析。

存在的理由：长任务说明直接写在命令行里非常脆弱——换行会被 PowerShell
当成新命令执行，引号和中文还容易被转义搞坏。所以支持从文件读，
这里把两种写法和各种错误情况都钉住。
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from autopilot.cli import _resolve_goal


def _args(goal: str = "", goal_file: str = "") -> argparse.Namespace:
    return argparse.Namespace(goal=goal, goal_file=goal_file)


# --- 直接给文本 ---------------------------------------------------------


def test_goal_from_positional() -> None:
    assert _resolve_goal(_args(goal="打开记事本")) == "打开记事本"


def test_goal_is_stripped() -> None:
    assert _resolve_goal(_args(goal="  打开记事本 \n")) == "打开记事本"


def test_empty_goal_raises_with_usage() -> None:
    with pytest.raises(ValueError) as info:
        _resolve_goal(_args())
    msg = str(info.value)
    assert "--goal-file" in msg and "@" in msg


# --- 从文件读 -----------------------------------------------------------


def test_goal_from_file(tmp_path: Path) -> None:
    f = tmp_path / "task.txt"
    f.write_text("完成 PTA 单选题", encoding="utf-8")
    assert _resolve_goal(_args(goal_file=str(f))) == "完成 PTA 单选题"


def test_multiline_goal_preserved(tmp_path: Path) -> None:
    """多行说明必须原样保留换行——这正是命令行会搞坏的地方。"""
    text = "第一行要求\n第二行要求\n\n第三段"
    f = tmp_path / "task.txt"
    f.write_text(text, encoding="utf-8")
    assert _resolve_goal(_args(goal_file=str(f))) == text


def test_long_goal_survives(tmp_path: Path) -> None:
    text = "很长的要求。" * 500
    f = tmp_path / "task.txt"
    f.write_text(text, encoding="utf-8")
    assert _resolve_goal(_args(goal_file=str(f))) == text


def test_utf8_chinese_not_mangled(tmp_path: Path) -> None:
    text = "用 browser_debug 打开 pintia.cn，填完先问我是否提交 ✔"
    f = tmp_path / "task.txt"
    f.write_text(text, encoding="utf-8")
    assert _resolve_goal(_args(goal_file=str(f))) == text


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="目标文件不存在"):
        _resolve_goal(_args(goal_file=str(tmp_path / "没有这个.txt")))


def test_empty_file_raises(tmp_path: Path) -> None:
    f = tmp_path / "empty.txt"
    f.write_text("   \n\n", encoding="utf-8")
    with pytest.raises(ValueError, match="是空的"):
        _resolve_goal(_args(goal_file=str(f)))


def test_goal_file_wins_over_positional(tmp_path: Path) -> None:
    f = tmp_path / "task.txt"
    f.write_text("来自文件", encoding="utf-8")
    assert _resolve_goal(_args(goal="来自命令行", goal_file=str(f))) == "来自文件"


# --- @文件 简写 ---------------------------------------------------------


def test_at_file_shorthand(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "task.txt"
    f.write_text("用 @ 简写读到的目标", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _resolve_goal(_args(goal="@task.txt")) == "用 @ 简写读到的目标"


def test_at_prefix_without_file_is_treated_as_text() -> None:
    """``@某人 请帮我...`` 这种以 @ 开头但不是文件的，要当普通文本。"""
    goal = "@管理员 请帮我打开记事本"
    assert _resolve_goal(_args(goal=goal)) == goal


def test_at_file_quoted_path(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "my task.txt"
    f.write_text("带空格的文件名", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _resolve_goal(_args(goal='@"my task.txt"')) == "带空格的文件名"
