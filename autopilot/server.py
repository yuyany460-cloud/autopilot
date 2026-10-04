# -*- coding: utf-8 -*-
"""Web 控制台后端。

提供：
* 实时画面预览（轮询 JPEG 截图）
* 元素树 / OCR 文字查看
* 直接执行任意动作（带参数表单）
* 下发自然语言目标给 Agent，SSE 实时推送思考与动作
* 运行确定性流程
* 一键急停
* 审计日志查看

前端是一个单文件 HTML（``static/index.html``），不依赖任何 CDN。
"""

import asyncio
import json
import queue
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__, perceive, psbridge, screen, winapi, windows
from .actions import build_registry
from .agent import Agent, AgentConfig, AgentRun
from .context import ActionContext, make_context
from .executor import AuditLog, Executor
from .flows import iter_flow, list_flows, load_flow
from .guard import Policy
from .llm import LLMClient, LLMConfig, resolve_api_key, resolve_base_url, resolve_model

# 注意：本模块**不能**使用 `from __future__ import annotations`。
# FastAPI 靠真实的类型对象识别 `Request` 参数和请求体；
# 一旦注解变成字符串，它会在模块全局里查找，找不到就退化成查询参数，
# 表现为接口莫名返回 422。

STATIC = Path(__file__).resolve().parent / "static"


class Hub:
    """跨请求共享的运行状态：执行器、Agent、事件队列。"""

    def __init__(self, workspace: Path, allow_danger: bool = False) -> None:
        self.workspace = Path(workspace)
        self.policy = Policy(mode="auto", allow_danger=allow_danger,
                             max_steps=40, workspace=self.workspace)
        self.ctx: ActionContext = make_context(
            policy=self.policy, workspace=self.workspace,
            log=self._log, hotkey="ctrl+alt+q")
        self.registry = build_registry(self.ctx)
        self.audit = AuditLog(self.workspace / "var" / "logs")
        self.executor = Executor(self.registry, self.ctx, audit=self.audit,
                                 auto_observe="fast", log=self._log)
        self.events: "queue.Queue[dict[str, Any]]" = queue.Queue()
        self.log_lines: list[dict[str, Any]] = []
        self.agent: Agent | None = None
        self.agent_thread: threading.Thread | None = None
        self.last_run: AgentRun | None = None
        self.started_at = time.time()

    # -- 日志 ------------------------------------------------------------
    def _log(self, level: str, message: str) -> None:
        entry = {"ts": time.time(), "level": level, "message": str(message)}
        self.log_lines.append(entry)
        if len(self.log_lines) > 2000:
            del self.log_lines[:500]
        self.push({"kind": "log", **entry})

    def push(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except queue.Full:
            pass

    def drain(self, limit: int = 200) -> list[dict[str, Any]]:
        out = []
        while len(out) < limit:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                break
        return out

    # -- 状态 ------------------------------------------------------------
    @property
    def agent_running(self) -> bool:
        return bool(self.agent_thread and self.agent_thread.is_alive())

    def state(self) -> dict[str, Any]:
        fg = None
        try:
            fg = windows.get_foreground()
        except Exception:
            pass
        return {
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "agent_running": self.agent_running,
            "last_run": self.last_run.as_dict() if self.last_run else None,
            "foreground": fg.as_dict() if fg else None,
            "screen": {"size": list(winapi.primary_screen_size())},
            "policy": self.policy.to_dict(),
            "guard": self.ctx.guard.summary(),
            "killswitch": {
                "hotkey": self.ctx.killswitch.hotkey,
                "registered": self.ctx.killswitch.hotkey_active,
                "triggered": self.ctx.killswitch.triggered,
                "reason": self.ctx.killswitch.reason,
            },
            "steps": self.executor.steps,
            "llm": {
                "model": resolve_model(self.workspace),
                "base_url": resolve_base_url(self.workspace),
                "has_key": bool(resolve_api_key(self.workspace)),
            },
            "actions": len(self.registry.names()),
        }

    # -- Agent -----------------------------------------------------------
    def start_agent(self, goal: str, max_steps: int = 30,
                    allow_danger: bool | None = None,
                    fast: bool = True) -> bool:
        if self.agent_running:
            return False
        self.ctx.killswitch.reset()
        if allow_danger is not None:
            self.policy.allow_danger = bool(allow_danger)

        cfg = LLMConfig(
            model=resolve_model(self.workspace),
            base_url=resolve_base_url(self.workspace),
            api_key=resolve_api_key(self.workspace),
        ).resolved()
        llm = LLMClient(cfg, workspace=self.workspace, log=self._log)
        self.executor.auto_observe = "fast" if fast else "full"

        agent = Agent(llm, self.executor, self.ctx, AgentConfig(
            goal=goal, max_steps=max_steps, allow_danger=self.policy.allow_danger,
        ), log=self._log)
        self.agent = agent
        agent.subscribe(self._on_agent_event)

        def worker() -> None:
            try:
                self.last_run = agent.run()
            except Exception as exc:
                self.push({"kind": "error", "message": f"Agent 异常：{exc}"})
            finally:
                self.push({"kind": "agent_stopped"})

        self.agent_thread = threading.Thread(target=worker, name="autopilot-agent", daemon=True)
        self.agent_thread.start()
        return True

    def _on_agent_event(self, event: dict[str, Any]) -> None:
        payload = {k: v for k, v in event.items() if k != "snapshot"}
        if "snapshot" in event and event["snapshot"]:
            payload["snapshot_summary"] = {
                "elements": len(event["snapshot"].get("elements") or []),
                "windows": len(event["snapshot"].get("windows") or []),
            }
        self.push(payload)

    def stop(self, reason: str = "Web 控制台急停") -> None:
        self.ctx.killswitch.trigger(reason)
        self.push({"kind": "stopped", "reason": reason})

    def resume(self) -> None:
        self.ctx.killswitch.reset()

    # -- 动作 ------------------------------------------------------------
    def run_action(self, name: str, params: dict[str, Any]) -> dict[str, Any]:
        result = self.executor.run(name, params)
        self.push({"kind": "result", "step": self.executor.steps,
                   "result": result.as_dict()})
        return result.as_dict()

    def action_catalog(self) -> list[dict[str, Any]]:
        out = []
        for act in self.registry.all():
            out.append({
                "name": act.name, "category": act.category,
                "description": act.description, "danger": act.danger.value,
                "danger_label": act.danger.label,
                "mutates": act.mutates,
                "params": [
                    {"name": p.name, "type": p.type, "required": p.required,
                     "description": p.description, "default": p.default, "enum": p.enum}
                    for p in act.params
                ],
            })
        return out

    def observe_text(self, include_ocr: bool = True, max_elements: int = 60) -> str:
        snap = perceive.observe(include_uia=True, include_ocr=include_ocr,
                               save_image=False, ocr_region="window")
        self.ctx.set_snapshot(snap)
        return snap.render(max_elements=max_elements)

    def screenshot_jpeg(self, max_width: int = 1280, quality: int = 70) -> bytes:
        grab = screen.capture()
        scaled = grab.scaled(max_width, int(max_width * 0.75))
        try:
            return scaled.to_bytes("jpg", quality=quality)
        except Exception:
            return scaled.to_bytes("png")


# --------------------------------------------------------------------------
# FastAPI 应用
# --------------------------------------------------------------------------


def create_app(workspace: Path, allow_danger: bool = False):
    try:
        from fastapi import FastAPI, HTTPException, Request
        from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
    except ImportError as exc:  # pragma: no cover
        raise ImportError("需要安装 fastapi 与 uvicorn：pip install fastapi uvicorn") from exc

    hub = Hub(workspace, allow_danger=allow_danger)
    app = FastAPI(title="AutoPilot 控制台", version=__version__)

    # 请求体直接当 JSON 解析，不用 Pydantic 模型：
    # 本模块用了 `from __future__ import annotations`，而在函数内部定义的
    # Pydantic 模型类型注解是字符串，FastAPI 在局部作用域里解析不到，
    # 会把 body 误判成查询参数直接返回 422。动作层本来就会校验参数，
    # 这里手工解析更简单也更少一层耦合。
    async def read_json(request: "Request") -> dict[str, Any]:
        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="请求体必须是 JSON 对象")
        return payload

    # -- 页面 ------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index() -> Any:
        page = STATIC / "index.html"
        if not page.is_file():
            return HTMLResponse("<h1>缺少 static/index.html</h1>", status_code=500)
        return HTMLResponse(page.read_text(encoding="utf-8"))

    @app.get("/api/state")
    def api_state() -> Any:
        return JSONResponse(hub.state())

    @app.get("/api/actions")
    def api_actions() -> Any:
        return JSONResponse(hub.action_catalog())

    @app.get("/api/screen.jpg")
    def api_screen(max_width: int = 1280, quality: int = 70) -> Any:
        try:
            data = hub.screenshot_jpeg(max_width=max_width, quality=quality)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/observe")
    def api_observe(ocr: bool = True, max_elements: int = 60) -> Any:
        try:
            return JSONResponse({"text": hub.observe_text(ocr, max_elements)})
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/api/action")
    async def api_action(request: Request) -> Any:
        body = await read_json(request)
        name = str(body.get("name") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="缺少动作名 name")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(status_code=400, detail="params 必须是对象")
        return JSONResponse(hub.run_action(name, params))

    @app.post("/api/agent/start")
    async def api_agent_start(request: Request) -> Any:
        body = await read_json(request)
        goal = str(body.get("goal") or "").strip()
        if not goal:
            raise HTTPException(status_code=400, detail="目标不能为空")
        allow = body.get("allow_danger")
        ok = hub.start_agent(
            goal,
            max_steps=int(body.get("max_steps") or 30),
            allow_danger=None if allow is None else bool(allow),
            fast=bool(body.get("fast", True)),
        )
        if not ok:
            raise HTTPException(status_code=409, detail="已有任务在运行")
        return JSONResponse({"started": True, "goal": goal})

    @app.post("/api/agent/stop")
    def api_agent_stop() -> Any:
        hub.stop()
        return JSONResponse({"stopped": True})

    @app.post("/api/agent/resume")
    def api_agent_resume() -> Any:
        hub.resume()
        return JSONResponse({"resumed": True})

    @app.get("/api/flows")
    def api_flows() -> Any:
        return JSONResponse(list_flows(hub.workspace / "flows"))

    @app.post("/api/flow/run")
    async def api_flow_run(request: Request) -> Any:
        body = await read_json(request)
        name = str(body.get("file") or "").strip()
        if not name:
            raise HTTPException(status_code=400, detail="缺少流程文件名 file")
        variables = body.get("variables") or {}
        if not isinstance(variables, dict):
            raise HTTPException(status_code=400, detail="variables 必须是对象")
        path = Path(name)
        if not path.is_absolute():
            path = hub.workspace / "flows" / name
        try:
            flow = load_flow(path)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def generate() -> Any:
            hub.push({"kind": "flow_start", "name": flow.get("name"), "file": name})
            results: list[dict[str, Any]] = []

            def work() -> None:
                try:
                    for i, result in enumerate(iter_flow(hub.executor, flow, variables), 1):
                        data = result.as_dict()
                        results.append(data)
                        hub.push({"kind": "flow_step", "index": i, "result": data})
                except Exception as exc:
                    hub.push({"kind": "error", "message": f"流程异常：{exc}"})
                finally:
                    ok = sum(1 for r in results if r.get("ok"))
                    hub.push({"kind": "flow_done", "total": len(results), "ok": ok})

            thread = threading.Thread(target=work, daemon=True)
            thread.start()
            while thread.is_alive():
                await asyncio.sleep(0.3)
                while True:
                    try:
                        event = hub.events.get_nowait()
                    except queue.Empty:
                        break
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            thread.join(timeout=1)
            while True:
                try:
                    event = hub.events.get_nowait()
                except queue.Empty:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/events")
    async def api_events(request: Request) -> Any:
        async def generate() -> Any:
            yield f"data: {json.dumps({'kind': 'hello', 'version': __version__})}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                drained = hub.drain(100)
                if drained:
                    for event in drained:
                        yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})

    @app.get("/api/audit")
    def api_audit(limit: int = 100) -> Any:
        return JSONResponse(hub.audit.tail(limit))

    @app.get("/api/logs")
    def api_logs(limit: int = 300) -> Any:
        return JSONResponse(hub.log_lines[-limit:])

    @app.get("/api/check")
    def api_check() -> Any:
        return JSONResponse(psbridge.self_check())

    @app.on_event("shutdown")
    def _shutdown() -> None:
        try:
            hub.ctx.killswitch.stop()
        except Exception:
            pass

    app.state.hub = hub
    return app


def serve(host: str = "127.0.0.1", port: int = 8787, workspace: Path | None = None,
          allow_danger: bool = False, open_browser: bool = True) -> int:
    """启动 Web 控制台。"""
    try:
        import uvicorn
    except ImportError:
        print("需要安装 uvicorn：pip install fastapi uvicorn")
        return 1

    ws = Path(workspace or Path.cwd()).resolve()
    app = create_app(ws, allow_danger=allow_danger)
    url = f"http://{host}:{port}"

    print("AutoPilot Web 控制台")
    print(f"  地址：{url}")
    print(f"  工作目录：{ws}")
    print(f"  危险操作：{'已允许' if allow_danger else '已拦截（需 --allow-dangerous）'}")
    print("  急停热键：Ctrl+Alt+Q（页面里也有「急停」按钮）")
    print("  按 Ctrl+C 退出\n")

    if open_browser:
        def _open() -> None:
            time.sleep(1.2)
            try:
                import webbrowser
                webbrowser.open(url)
            except Exception:
                pass
        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
    return 0
