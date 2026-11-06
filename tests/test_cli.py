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

# --- 短命令别名 ---------------------------------------------------------


def test_aliases_resolve_to_the_same_command() -> None:
    """`ap a` / `ap r "目标"` 这类短写必须和完整子命令等价。

    理由很实际：cmd 里 `python -m autopilot do screenshot path=a.png` 太长，
    日常真正会敲的是 `ap d screenshot path=a.png`。
    """
    from autopilot.cli import COMMAND_ALIASES, build_parser

    parser = build_parser()
    for alias, canonical in COMMAND_ALIASES.items():
        args = [alias]
        if canonical == "do":
            args.append("screenshot")
        elif canonical == "flow":
            args.append("list")
        elif canonical == "run":
            args.append("打开记事本")
        parsed = parser.parse_args(args)
        assert parsed.command in (alias, canonical), f"{alias} 没解析成 {canonical}"
        assert callable(parsed.func), alias


def test_run_alias_keeps_goal_text() -> None:
    from autopilot.cli import build_parser

    parsed = build_parser().parse_args(["r", "把桌面上的 txt 重命名"])
    assert parsed.goal == "把桌面上的 txt 重命名"
    assert parsed.max_steps == 100

# --- 控制台输出落盘（cmd 窗口滚没了还能查） -----------------------------


def test_tee_duplicates_writes(tmp_path) -> None:
    from autopilot.cli import _Tee

    class _Sink:
        def __init__(self) -> None:
            self.data = ""

        def write(self, text):
            self.data += text
            return len(text)

        def flush(self):
            pass

        def isatty(self):
            return False

    stream, record = _Sink(), _Sink()
    tee = _Tee(stream, record)
    tee.write("你好")
    tee.flush()
    assert stream.data == "你好" and record.data == "你好"
    assert tee.isatty() is False


def test_run_log_contains_console_output(tmp_path) -> None:
    """`ap cfg` 这类命令也要留一份日志：事后能查"它到底打印了什么"。"""
    from datetime import datetime

    from autopilot.cli import main

    rc = main(["config", "--workspace", str(tmp_path)])
    assert rc == 0
    log = tmp_path / "var" / "logs" / f"run-{datetime.now():%Y%m%d}.log"
    assert log.is_file(), "应生成 var/logs/run-*.log"
    text = log.read_text(encoding="utf-8")
    assert "工作目录" in text and str(tmp_path) in text


def test_no_log_flag_skips_log(tmp_path) -> None:
    from autopilot.cli import main

    assert main(["config", "--workspace", str(tmp_path), "--no-log"]) == 0
    assert not list((tmp_path / "var" / "logs").glob("run-*.log"))