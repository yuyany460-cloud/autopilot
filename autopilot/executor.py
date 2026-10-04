# -*- coding: utf-8 -*-
"""动作执行器：安全审查 → 执行 → 审计 → 重新感知。

执行器是"模型意图"和"真实世界"之间唯一的通道。所有动作都必须经过它，
这样才能保证：

* 危险操作一定被护栏拦下（模型无法绕过）
* 每一步都留下可追溯的审计记录（含截图路径）
* 急停随时生效
* 动作执行后自动刷新感知，模型下一轮看到的是真实的新状态
"""

from __future__ import annotations

import json
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .context import ActionContext
from .guard import Decision
from .killswitch import EmergencyStop
from .perceive import Snapshot
from .registry import Registry


@dataclass
class ActionResult:
    """一次动作执行的结果。"""

    action: str
    params: dict[str, Any] = field(default_factory=dict)
    ok: bool = True
    output: Any = None
    error: str | None = None
    ms: float = 0.0
    denied: bool = False
    requires_confirm: bool = False
    dry_run: bool = False
    reason: str = ""
    observation: str = ""
    snapshot: Snapshot | None = None
    finished: bool = False
    success: bool = True

    def to_text(self, max_output: int = 3000) -> str:
        """渲染成喂给模型的观察文本。"""
        if self.denied:
            return f"❌ 动作 {self.action} 被安全策略拒绝：{self.reason}"
        if self.dry_run:
            return f"🧪 演练模式：{self.action} 未真正执行（参数 {_brief(self.params)}）"
        if not self.ok:
            return f"❌ 动作 {self.action} 执行失败：{self.error}"

        body = self.observation
        if not body:
            body = _render_output(self.output, max_output)
        return f"✅ {self.action} 完成（{self.ms:.0f}ms）\n{body}" if body else \
               f"✅ {self.action} 完成（{self.ms:.0f}ms）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "params": _brief(self.params), "ok": self.ok,
            "denied": self.denied, "dry_run": self.dry_run, "error": self.error,
            "reason": self.reason, "ms": round(self.ms, 1),
            "output": _render_output(self.output, 1500),
            "observation": self.observation[:1500],
            "finished": self.finished, "success": self.success,
        }


def _brief(params: dict[str, Any], limit: int = 200) -> dict[str, Any]:
    out = {}
    for k, v in (params or {}).items():
        text = str(v)
        out[k] = text[:limit] + ("…" if len(text) > limit else "")
    return out


def _render_output(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    if isinstance(value, Snapshot):
        return value.render()
    if isinstance(value, str):
        return value[:max_len]
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(value)
    return text[:max_len]


class AuditLog:
    """把每一步操作追加写入 JSONL，便于事后复盘。"""

    def __init__(self, directory: str | Path, echo: Callable[[str], None] | None = None) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.echo = echo
        self._lock = threading.Lock()
        self._path = self.dir / f"audit-{datetime.now():%Y%m%d}.jsonl"

    @property
    def path(self) -> Path:
        return self._path

    def write(self, record: dict[str, Any]) -> None:
        record = {"ts": datetime.now().isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        if self.echo:
            self.echo(line)

    def tail(self, limit: int = 50) -> list[dict[str, Any]]:
        if not self._path.is_file():
            return []
        lines = self._path.read_text(encoding="utf-8", errors="replace").splitlines()
        out = []
        for line in lines[-limit:]:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class Executor:
    """串起注册表、护栏、上下文和审计。"""

    def __init__(self, registry: Registry, ctx: ActionContext,
                 audit: AuditLog | None = None,
                 auto_observe: str = "full",
                 log: Callable[[str, str], None] | None = None) -> None:
        """
        ``auto_observe``：
            ``"full"`` 每个改变状态的动作后重新完整感知（含 OCR，最准但慢）
            ``"fast"`` 只刷新元素树，跳过 OCR
            ``"none"`` 不自动感知，由模型自己调用 observe
        """
        self.registry = registry
        self.ctx = ctx
        self.audit = audit or AuditLog(ctx.workspace / "var" / "logs")
        self.auto_observe = auto_observe
        self._log = log or ctx.log
        self.history: list[ActionResult] = []
        self.steps = 0

    # -- 主入口 ----------------------------------------------------------
    def run(self, name: str, params: dict[str, Any] | None = None) -> ActionResult:
        params = dict(params or {})
        start = time.perf_counter()

        try:
            action = self.registry.require(str(name))
        except KeyError as exc:
            return self._finish(ActionResult(
                action=str(name), params=params, ok=False, error=str(exc),
                ms=(time.perf_counter() - start) * 1000), record=False)

        # 1) 急停
        if self.ctx.killswitch.triggered:
            reason = self.ctx.killswitch.reason or "未知原因"
            return self._finish(ActionResult(
                action=action.name, params=params, ok=False, denied=True,
                error=f"任务已急停：{reason}", reason=f"急停：{reason}",
                ms=(time.perf_counter() - start) * 1000))

        # 2) 安全审查
        decision: Decision = self.ctx.guard.check(action, params)
        if not decision.allowed:
            self._log("warn", f"⛔ 拒绝 {action.name}：{decision.reason}")
            return self._finish(ActionResult(
                action=action.name, params=params, ok=False, denied=True,
                reason=decision.reason, error=decision.reason,
                ms=(time.perf_counter() - start) * 1000))

        # 3) 人工确认
        if decision.requires_confirm and self.ctx.confirm is not None:
            try:
                approved = bool(self.ctx.confirm(action.name, params))
            except Exception:
                approved = False
            if not approved:
                return self._finish(ActionResult(
                    action=action.name, params=params, ok=False, denied=True,
                    reason="人工拒绝", error="操作被人工拒绝",
                    ms=(time.perf_counter() - start) * 1000))

        # 4) 演练模式
        if decision.dry_run:
            return self._finish(ActionResult(
                action=action.name, params=params, ok=True, dry_run=True,
                reason=decision.reason,
                ms=(time.perf_counter() - start) * 1000))

        # 5) 真正执行
        result = ActionResult(action=action.name, params=params)
        try:
            self._log("info", f"▶ {action.name} {_short_params(params)}")
            output = action.call(**params)
            result.output = output
            result.ok = True
            if isinstance(output, Snapshot):
                self.ctx.set_snapshot(output)
                result.snapshot = output
                result.observation = output.render()
            if isinstance(output, dict) and output.get("finished"):
                result.finished = True
                result.success = bool(output.get("success", True))
        except EmergencyStop as exc:
            result.ok = False
            result.denied = True
            result.error = str(exc)
            result.reason = str(exc)
        except Exception as exc:  # 动作自身的失败不应中断整个任务
            result.ok = False
            result.error = f"{type(exc).__name__}: {exc}"
            self._log("error", f"✗ {action.name} 失败：{result.error}")
            self._log("debug", traceback.format_exc(limit=4))

        self.steps += 1

        # 6) 自动重新感知
        if (result.ok and action.mutates and self.auto_observe != "none"
                and not isinstance(result.output, Snapshot)):
            try:
                snap = self._observe()
                result.snapshot = snap
                result.observation = snap.render()
            except Exception as exc:
                self._log("warn", f"重新感知失败：{exc}")

        result.ms = (time.perf_counter() - start) * 1000
        return self._finish(result)

    def _observe(self) -> Snapshot:
        full = self.auto_observe == "full"
        snap = self.ctx.take_snapshot(include_uia=True, include_ocr=full, save_image=False)
        return snap

    def _finish(self, result: ActionResult, record: bool = True) -> ActionResult:
        result.ms = result.ms or 0.0
        self.history.append(result)
        if record:
            self.audit.write(result.as_dict())
        return result

    # -- 批量 ------------------------------------------------------------
    def run_steps(self, steps: list[dict[str, Any]],
                  stop_on_error: bool = True,
                  on_step: Callable[[int, ActionResult], None] | None = None
                  ) -> list[ActionResult]:
        """按顺序执行一批动作（确定性任务脚本用）。"""
        results: list[ActionResult] = []
        for i, step in enumerate(steps, 1):
            self.ctx.killswitch.raise_if_triggered()
            name = step.get("action") or step.get("do") or ""
            params = step.get("params") or {k: v for k, v in step.items()
                                            if k not in ("action", "do", "params")}
            result = self.run(str(name), params)
            results.append(result)
            if on_step:
                on_step(i, result)
            if not result.ok and stop_on_error and not result.dry_run:
                self._log("error", f"第 {i} 步失败，流程中止")
                break
        return results


def _short_params(params: dict[str, Any], limit: int = 120) -> str:
    if not params:
        return ""
    text = json.dumps(_brief(params, 60), ensure_ascii=False)
    return text if len(text) <= limit else text[:limit] + "…}"
