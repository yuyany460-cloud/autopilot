# -*- coding: utf-8 -*-
"""动作执行所需的共享上下文。

动作处理函数通过闭包拿到这个对象，从而访问安全策略、急停开关、
上一次的感知快照（用于把 ``E7`` 解析成坐标）以及浏览器会话等状态。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .guard import Guard, Policy
from .killswitch import KillSwitch
from .perceive import Snapshot


@dataclass
class ActionContext:
    """一次运行期间共享的状态。"""

    guard: Guard
    killswitch: KillSwitch
    workspace: Path
    log: Callable[[str, str], None] = lambda level, msg: None
    last_snapshot: Snapshot | None = None
    browser: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    confirm: Callable[[str, dict[str, Any]], bool] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # -- 快照 ------------------------------------------------------------
    def set_snapshot(self, snapshot: Snapshot) -> Snapshot:
        with self._lock:
            self.last_snapshot = snapshot
        return snapshot

    def require_snapshot(self) -> Snapshot:
        """拿到最近一次快照；没有就先采一次。"""
        from . import perceive

        with self._lock:
            snap = self.last_snapshot
        if snap is None:
            snap = self.set_snapshot(perceive.observe(save_image=False, include_ocr=False))
        return snap

    def take_snapshot(self, **kwargs: Any) -> Snapshot:
        from . import perceive

        return self.set_snapshot(perceive.observe(**kwargs))

    # -- 事件流 ----------------------------------------------------------
    def emit(self, kind: str, **payload: Any) -> None:
        self.events.append({"kind": kind, **payload})

    def info(self, message: str) -> None:
        self.log("info", message)


def make_context(policy: Policy | None = None, workspace: str | Path | None = None,
                 log: Callable[[str, str], None] | None = None,
                 confirm: Callable[[str, dict[str, Any]], bool] | None = None,
                 hotkey: str = "ctrl+alt+q", start_hotkey: bool = True) -> ActionContext:
    """按配置创建上下文（含安全策略与急停开关）。"""
    ws = Path(workspace) if workspace else Path.cwd()
    pol = policy or Policy(workspace=ws)
    if pol.workspace is None:
        pol.workspace = ws
    ks = KillSwitch(stop_file=ws / "var" / "STOP", hotkey=hotkey)
    if start_hotkey:
        ks.start()
    return ActionContext(guard=Guard(pol), killswitch=ks, workspace=ws,
                         log=log or (lambda level, msg: None), confirm=confirm)
