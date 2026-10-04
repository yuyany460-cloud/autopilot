# -*- coding: utf-8 -*-
"""pytest 共享夹具。

注意：这个项目是 Windows 桌面自动化，部分测试会真的动鼠标/截屏。
涉及输入的测试都会先保存并恢复光标位置，避免干扰使用者。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from autopilot import winapi  # noqa: E402
from autopilot.context import make_context  # noqa: E402
from autopilot.guard import Policy  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _dpi() -> None:
    winapi.enable_dpi_awareness()


@pytest.fixture
def policy() -> Policy:
    return Policy(mode="auto", allow_danger=False, protect_paths=True)


@pytest.fixture
def ctx(tmp_path: Path, policy: Policy):
    """不注册全局热键的上下文（避免测试进程互相抢热键）。"""
    context = make_context(policy=policy, workspace=tmp_path, start_hotkey=False)
    yield context
    try:
        context.killswitch.stop()
    except Exception:
        pass


@pytest.fixture
def cursor_guard():
    """保护光标位置：测试结束后复位。"""
    before = winapi.get_cursor_pos()
    yield before
    try:
        winapi.move_to(*before)
    except Exception:
        pass
    time.sleep(0.05)
