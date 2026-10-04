# -*- coding: utf-8 -*-
"""命令行入口。

    python -m autopilot <子命令>

子命令一览：

============ ==========================================================
``run``      让 Agent 自主完成一个目标（需要 LLM）
``observe``  打印当前屏幕的文本快照
``do``       直接执行一个动作（不经过 LLM）
``actions``  列出所有可用动作及其参数
``flow``     确定性流程：list / run / record
``serve``    启动 Web 控制台
``selfcheck`` 各子系统自检
``doctor``   环境体检 + 给出修复建议
``config``   显示当前配置来源
============ ==========================================================
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__, perceive, psbridge, screen, winapi, windows
from .actions import build_registry
from .agent import Agent, AgentConfig
from .context import ActionContext, make_context
from .executor import AuditLog, Executor
from .guard import Policy
from .llm import LLMClient, LLMConfig, resolve_api_key, resolve_base_url, resolve_model
from .registry import Danger

ROOT = Path(__file__).resolve().parent.parent

C = {
    "reset": "\033[0m", "dim": "\033[2m", "bold": "\033[1m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[36m", "mag": "\033[35m",
}


def _init_console() -> None:
    """Windows 控制台按 UTF-8 输出，避免中文/emoji 报 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    if winapi.IS_WINDOWS:
        try:
            ctypes = __import__("ctypes")
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
            handle = ctypes.windll.kernel32.GetStdHandle(-11)
            mode = ctypes.c_uint32()
            if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        except Exception:
            pass


def _c(text: str, color: str = "", enabled: bool = True) -> str:
    if not enabled or not color or not sys.stdout.isatty():
        return text
    return f"{C[color]}{text}{C['reset']}"


def _log(level: str, message: str) -> None:
    icons = {"info": ("·", "dim"), "warn": ("!", "yellow"),
             "error": ("×", "red"), "debug": (" ", "dim")}
    icon, color = icons.get(level, ("·", ""))
    print(_c(f"  {icon} {message}", color))


# --------------------------------------------------------------------------
# 公共构件
# --------------------------------------------------------------------------


def _make_policy(args: argparse.Namespace) -> Policy:
    return Policy(
        mode=getattr(args, "mode", "auto") or "auto",
        allow_danger=bool(getattr(args, "allow_dangerous", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        max_steps=int(getattr(args, "max_steps", 30) or 30),
    )


def _make_context(args: argparse.Namespace) -> ActionContext:
    workspace = Path(getattr(args, "workspace", None) or ROOT).resolve()
    ctx = make_context(policy=_make_policy(args), workspace=workspace, log=_log,
                       hotkey=getattr(args, "hotkey", "ctrl+alt+q") or "ctrl+alt+q",
                       start_hotkey=not bool(getattr(args, "no_hotkey", False)))
    return ctx


def _make_executor(args: argparse.Namespace, ctx: ActionContext,
                   auto_observe: str = "full") -> Executor:
    registry = build_registry(ctx)
    audit = AuditLog(ctx.workspace / "var" / "logs", echo=None)
    return Executor(registry, ctx, audit=audit, auto_observe=auto_observe, log=_log)


# --------------------------------------------------------------------------
# 子命令实现
# --------------------------------------------------------------------------


def _resolve_goal(args: argparse.Namespace) -> str:
    """取任务目标：可以直接给文本，也可以从文件读。

    长任务说明写在命令行里非常脆弱——换行会被 PowerShell 当成新命令执行，
    引号和中文还容易被转义搞坏。所以支持两种从文件读的写法：

        apilot run --goal-file task.txt
        apilot run @task.txt          （@ 开头且文件存在时自动当文件读）
    """
    goal = (getattr(args, "goal", None) or "").strip()
    goal_file = getattr(args, "goal_file", None)

    if not goal_file and goal.startswith("@"):
        candidate = Path(goal[1:].strip().strip('"'))
        if candidate.is_file():
            goal, goal_file = "", str(candidate)

    if goal_file:
        p = Path(goal_file).expanduser()
        if not p.is_absolute():
            p = Path.cwd() / p
        if not p.is_file():
            raise FileNotFoundError(f"目标文件不存在：{p}")
        goal = p.read_text(encoding="utf-8").strip()
        if not goal:
            raise ValueError(f"目标文件是空的：{p}")
    if not goal:
        raise ValueError(
            "没有任务目标。用法：\n"
            '  apilot run "把今天的日期写进记事本"\n'
            "  apilot run --goal-file task.txt\n"
            "  apilot run @task.txt")
    return goal


def cmd_run(args: argparse.Namespace) -> int:
    goal = _resolve_goal(args)
    args.goal = goal
    ctx = _make_context(args)
    auto = "fast" if args.fast else "full"
    executor = _make_executor(args, ctx, auto_observe=auto)

    cfg = LLMConfig(
        model=args.model or resolve_model(ctx.workspace),
        base_url=args.base_url or resolve_base_url(ctx.workspace),
        api_key=resolve_api_key(ctx.workspace),
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    ).resolved()
    llm = LLMClient(cfg, workspace=ctx.workspace, log=_log)

    print(_c("AutoPilot 自主任务", "bold"))
    head = goal.replace("\n", " ")
    print(f"  目标：{head if len(head) <= 120 else head[:117] + '…'}")
    if len(goal) > 120:
        print(_c(f"  （完整目标 {len(goal)} 字符，见上方文件）", "dim"))
    print(f"  模型：{cfg.model} @ {cfg.base_url}")
    print(f"  步数上限：{args.max_steps} | 安全策略：{executor.ctx.guard.summary()}")
    if ctx.killswitch.hotkey_active:
        print(_c(f"  急停热键：{ctx.killswitch.hotkey}（随时可按）", "yellow"))
    else:
        print(_c("  提示：全局急停热键未注册；可创建 var/STOP 文件叫停", "yellow"))
    print()

    agent = Agent(llm, executor, ctx, AgentConfig(
        goal=goal,
        max_steps=args.max_steps,
        allow_danger=args.allow_dangerous,
        temperature=args.temperature,
        observe_every_step=True,
        include_screenshot=bool(args.vision),
        step_pause=args.pause,
    ), log=_log)

    started = time.time()

    def on_event(event: dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "llm":
            if event.get("content"):
                print(_c(f"\n💭 {event['content'][:400]}", "blue"))
            for call in event.get("tool_calls") or []:
                params = json.dumps(call["arguments"], ensure_ascii=False)
                print(_c(f"🎯 第 {event['step']} 步 → {call['name']} {params[:220]}", "mag"))
        elif kind == "result":
            data = event["result"]
            if data.get("denied"):
                print(_c(f"   ⛔ 被拒绝：{data.get('reason')}", "red"))
            elif data.get("ok"):
                out = (data.get("output") or "").replace("\n", " ")
                print(_c(f"   ✅ {out[:300]}", "green"))
            else:
                print(_c(f"   ❌ {data.get('error')}", "red"))
        elif kind == "narration":
            print(_c(f"   （模型未给动作）{event.get('text', '')[:200]}", "yellow"))
        elif kind == "error":
            print(_c(f"   ⚠ {event.get('message')}", "red"))
        elif kind == "finished":
            print(_c(f"\n🏁 {event.get('summary')}", "green"))
        elif kind == "stopped":
            print(_c(f"\n⏹ {event.get('reason')}", "yellow"))

    run = agent.run(on_event=on_event)
    elapsed = time.time() - started

    print("\n" + "─" * 62)
    status = _c("成功", "green") if run.success else _c("未完成", "yellow")
    hit = run.usage.get("prompt_cache_hit_tokens", 0)
    miss = run.usage.get("prompt_cache_miss_tokens", 0)
    total_in = run.usage.get("prompt_tokens", 0)
    cache = f" | 缓存命中 {hit}/{total_in}（{hit / total_in:.0%}）" if total_in else ""
    print(f"结果：{status} | 步骤 {run.steps} | 耗时 {elapsed:.1f}s | "
          f"tokens 输入 {total_in} / 输出 {run.usage.get('completion_tokens', 0)}")
    print(f"       其中未命中缓存（真正计费的输入）{miss}{cache}")
    print(f"说明：{run.summary}")
    print(f"审计日志：{executor.audit.path}")
    if run.needs_human:
        print(_c(f"需要你介入：{run.needs_human}", "yellow"))
    if getattr(args, "json", False):
        print(json.dumps(run.as_dict(), ensure_ascii=False, indent=2))
    return 0 if run.success else 1


def cmd_observe(args: argparse.Namespace) -> int:
    ctx = _make_context(args)
    ctx.killswitch.stop()
    snap = perceive.observe(
        include_uia=not args.no_uia, include_ocr=not args.no_ocr,
        save_image=args.save, shot_dir=ctx.workspace / "var" / "shots",
        ocr_region=args.ocr_region)
    if args.json:
        print(json.dumps(snap.as_dict(), ensure_ascii=False, indent=2, default=str))
    else:
        print(snap.render(max_windows=args.max_windows, max_elements=args.max_elements,
                          max_ocr_lines=args.max_ocr_lines))
        print(_c(f"\n（采集耗时 {snap.elapsed_ms:.0f}ms，元素 {len(snap.elements)} 个）", "dim"))
        if snap.image_path:
            print(_c(f"截图：{snap.image_path}", "dim"))
    return 0


def _coerce(value: str, type_name: str) -> Any:
    if type_name in ("integer", "int"):
        return int(value)
    if type_name in ("number", "float"):
        return float(value)
    if type_name in ("boolean", "bool"):
        return value.strip().lower() in ("1", "true", "yes", "y", "on", "是")
    if type_name in ("array", "list"):
        return json.loads(value)
    if type_name in ("object", "dict"):
        return json.loads(value)
    return value


def cmd_do(args: argparse.Namespace) -> int:
    ctx = _make_context(args)
    executor = _make_executor(args, ctx, auto_observe=args.auto_observe)
    action = executor.registry.get(args.action)
    if action is None:
        print(_c(f"未知动作：{args.action}", "red"))
        print("可用动作：", ", ".join(executor.registry.names()))
        ctx.killswitch.stop()
        return 2

    params: dict[str, Any] = {}
    spec = {p.name: p for p in action.params}
    for item in args.params or []:
        if "=" not in item:
            print(_c(f"参数格式应为 key=value：{item}", "red"))
            ctx.killswitch.stop()
            return 2
        key, _, raw = item.partition("=")
        key = key.strip()
        if key not in spec:
            print(_c(f"动作 {action.name} 没有参数 {key}", "yellow"))
            print(f"  可用参数：{', '.join(spec) or '（无）'}")
            continue
        params[key] = _coerce(raw, spec[key].type)

    result = executor.run(action.name, params)
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    else:
        print(result.to_text())
    ctx.killswitch.stop()
    return 0 if result.ok else 1


def cmd_actions(args: argparse.Namespace) -> int:
    ctx = _make_context(args)
    ctx.killswitch.stop()
    registry = build_registry(ctx)
    if args.json:
        print(json.dumps(registry.openai_tools(allow_danger=True),
                         ensure_ascii=False, indent=2))
        return 0
    for cat, acts in registry.categories().items():
        if args.category and args.category not in cat:
            continue
        print(_c(f"\n■ {cat}", "bold"))
        for act in acts:
            tag = _c(f"[{act.danger.label}]", "red" if act.danger == Danger.HIGH
                     else "yellow" if act.danger == Danger.MEDIUM else "dim")
            print(f"  {act.name}({act.signature_hint()}) {tag}")
            print(_c(f"      {act.description.splitlines()[0]}", "dim"))
            for p in act.params:
                default = "" if p.default is None else f" 默认={p.default!r}"
                enum = f" 可选值={p.enum}" if p.enum else ""
                flag = "必填" if p.required else "可选"
                print(_c(f"      - {p.name}: {p.type}（{flag}）{p.description}{enum}{default}",
                         "dim"))
    print(_c(f"\n共 {len(registry.names())} 个动作", "dim"))
    return 0


def cmd_flow(args: argparse.Namespace) -> int:
    ctx = _make_context(args)
    flows_dir = ctx.workspace / "flows"
    if args.flow_action == "list":
        items = __import__("autopilot.flows", fromlist=["list_flows"]).list_flows(flows_dir)
        if not items:
            print(f"{flows_dir} 下还没有流程文件")
            return 0
        for item in items:
            print(f"  {item['file']:28} {item['name']}  ({item['steps']} 步)")
            if item["description"]:
                print(_c(f"      {item['description']}", "dim"))
        ctx.killswitch.stop()
        return 0

    if args.flow_action == "run":
        from .flows import load_flow, run_flow

        if not args.file:
            print(_c("请用 --file 指定流程文件，或先 flow list 看看有哪些", "red"))
            ctx.killswitch.stop()
            return 2
        path = Path(args.file)
        if not path.is_absolute() and not path.is_file():
            path = flows_dir / args.file
        flow = load_flow(path)
        executor = _make_executor(args, ctx, auto_observe=args.auto_observe)
        print(_c(f"执行流程：{flow.get('name')}（{len(flow.get('steps') or [])} 步）", "bold"))

        def on_step(i: int, result: Any) -> None:
            mark = "✅" if result.ok else ("⛔" if result.denied else "❌")
            detail = (result.error or result.reason or
                      str(result.output).replace("\n", " ")[:160])
            print(f"  {mark} {i:2}. {result.action} — {detail}")

        results = run_flow(executor, flow, variables=dict(args.var or []),
                           stop_on_error=not args.keep_going, on_step=on_step)
        ok = sum(1 for r in results if r.ok)
        print(_c(f"\n完成 {ok}/{len(results)} 步 | 审计：{executor.audit.path}", "dim"))
        ctx.killswitch.stop()
        return 0 if ok == len(results) else 1

    if args.flow_action == "record":
        from .flows import Recorder

        recorder = Recorder()
        print(_c("开始录制。请在电脑上正常操作，按 Ctrl+C 结束。", "bold"))
        if not recorder.start():
            print(_c("录制器启动失败（全局鼠标钩子未注册）", "red"))
            ctx.killswitch.stop()
            return 1
        if not recorder.hooks_ok.get("key"):
            print(_c("  注意：键盘钩子未注册，本次只会录到鼠标操作", "yellow"))
        try:
            while True:
                time.sleep(1.0)
                raw = recorder.raw_events
                print(_c(f"\r  已记录 {recorder.event_count} 个操作"
                         f"（钩子命中 鼠标{raw['mouse']} / 键盘{raw['key']}）…", "dim"),
                      end="")
        except KeyboardInterrupt:
            print()
        events = recorder.stop()
        out = Path(args.output) if args.output else flows_dir / f"recorded-{int(time.time())}.json"
        if not out.is_absolute():
            out = ctx.workspace / out
        recorder.save(out, name=args.name or out.stem, description=args.description or "")
        print(_c(f"已保存 {len(events)} 个操作到 {out}", "green"))
        if not events:
            print(_c("  一个操作都没录到。如果上面显示钩子命中数为 0，"
                     "说明钩子没收到输入（可能被安全软件拦截）。", "yellow"))
        ctx.killswitch.stop()
        return 0

    print(_c("用法：flow {list|run|record}", "red"))
    ctx.killswitch.stop()
    return 2


def cmd_selfcheck(args: argparse.Namespace) -> int:
    winapi.enable_dpi_awareness()
    report: dict[str, Any] = {"version": __version__, "python": sys.version.split()[0]}

    print(_c("AutoPilot 自检", "bold"))
    report["screen"] = screen.self_check()
    print(f"  截屏：{report['screen']['backend']} 后端 "
          f"{report['screen']['size']} 耗时 {report['screen']['grab_ms']}ms")
    report["winapi"] = winapi.screen_summary()
    print(f"  DPI：{report['winapi']['dpi_awareness']} "
          f"{report['winapi']['dpi']}dpi (缩放 {report['winapi']['scale']}x)")

    print("  UI/OCR 桥接自检中…")
    report["bridge"] = psbridge.self_check()
    uia = report["bridge"].get("uia") or {}
    ocr = report["bridge"].get("ocr") or {}
    print(f"  UIA：{'可用' if uia.get('ok') else '不可用'}"
          f"（{uia.get('elements')} 个元素，{uia.get('ms')}ms）")
    print(f"  OCR：{'可用' if ocr.get('ok') else '不可用'}"
          f"（引擎 {ocr.get('engine')}，{ocr.get('lines')} 行，{ocr.get('ms')}ms）")

    fg = windows.get_foreground()
    report["foreground"] = fg.as_dict() if fg else None
    print(f"  前台窗口：{fg.one_line() if fg else '无'}")

    ctx = _make_context(args)
    print("  鼠标/键盘回环测试…")
    before = winapi.get_cursor_pos()
    winapi.move_to(before[0] + 37, before[1] + 21)
    time.sleep(0.12)
    moved = winapi.get_cursor_pos()
    winapi.move_to(*before)
    delta = (abs(moved[0] - before[0] - 37), abs(moved[1] - before[1] - 21))
    report["pointer"] = {"before": list(before), "after": list(moved), "error": list(delta)}
    print(f"  鼠标：误差 {delta} {'正常' if max(delta) <= 2 else '异常'}")

    probe = "autopilot-selfcheck-测试"
    winapi.clipboard_set_text(probe)
    got = winapi.clipboard_get_text()
    report["clipboard"] = got == probe
    print(f"  剪贴板：{'正常' if got == probe else '异常'}")

    ks = ctx.killswitch
    report["killswitch"] = {"hotkey": ks.hotkey, "registered": ks.hotkey_active,
                            "error": ks.register_error}
    if ks.hotkey_active:
        print(f"  急停热键：{ks.hotkey} 已注册")
    elif ks.register_error == 1409:  # ERROR_HOTKEY_ALREADY_REGISTERED
        print(_c(f"  急停热键：{ks.hotkey} 已被其他进程占用"
                 f"（通常意味着已经有一个 AutoPilot 在运行）", "yellow"))
    else:
        print(_c(f"  急停热键：{ks.hotkey} 未注册"
                 f"（错误码 {ks.register_error}）；仍可用 var/STOP 文件叫停", "yellow"))
    ks.stop()

    report["actions"] = len(build_registry(ctx).names())
    print(f"  动作数量：{report['actions']}")

    problems = report["bridge"].get("errors") or []
    print()
    if problems:
        print(_c(f"存在问题：{problems}", "yellow"))
    all_ok = (report["screen"]["size"][0] > 0 and not problems
              and max(delta) <= 2 and got == probe)
    print(_c("自检结论：全部正常 ✔" if all_ok else "自检结论：有项目需要关注 ⚠",
             "green" if all_ok else "yellow"))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if all_ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境体检。每项是 ``(名称, 状态, 补救提示)``，状态取 ok / bad / optional。"""
    print(_c("环境体检", "bold"))
    checks: list[tuple[str, str, str]] = []

    if winapi.IS_WINDOWS:
        checks.append(("Windows 系统", "ok", ""))
    else:
        checks.append(("Windows 系统", "bad", "本项目仅支持 Windows"))

    checks.append((f"Python {sys.version.split()[0]}",
                   "ok" if sys.version_info >= (3, 10) else "bad", "需要 Python 3.10+"))

    try:
        exe = psbridge.find_powershell()
        checks.append((f"PowerShell 5.1（{exe}）", "ok", ""))
    except Exception as exc:
        checks.append(("PowerShell 5.1", "bad", str(exc)))

    try:
        langs = psbridge.bridge().ocr_languages()
    except Exception as exc:
        checks.append(("系统 OCR", "bad", str(exc)))
    else:
        # 系统自带 OCR 是"看屏幕"的主要手段，缺语言包会明显影响能力
        checks.append((
            f"系统 OCR 语言包：{langs or '无'}" + ("" if langs else "（OCR 不可用）"),
            "ok" if langs else "bad",
            "设置 → 时间和语言 → 语言和区域 → 添加语言（勾选「光学字符识别」）",
        ))

    # 用 find_spec 探测可选依赖，避免为了检查而真的导入它们
    import importlib.util
    for module, label, hint in (
        ("PIL", "Pillow（加速图像缩放）", "pip install pillow"),
        ("mss", "mss（更快的截屏后端）", "pip install mss"),
        ("playwright", "Playwright（网页自动化）", "pip install playwright"),
        ("fastapi", "FastAPI（Web 控制台）", "pip install fastapi uvicorn"),
    ):
        found = importlib.util.find_spec(module) is not None
        checks.append((label, "ok" if found else "optional", hint))

    key = resolve_api_key(ROOT)
    checks.append(("LLM API 密钥", "ok" if key else "bad",
                   "设置 AUTOPILOT_API_KEY，或在 var/config.json 写 api_key"))

    print()
    failed = 0
    for name, state, hint in checks:
        if state == "ok":
            print(f"  {_c('✔', 'green')} {name}")
        elif state == "optional":
            print(f"  {_c('○', 'dim')} {name} {_c('（未安装，不影响核心功能）', 'dim')}")
            if hint:
                print(_c(f"      → {hint}", "dim"))
        else:
            failed += 1
            print(f"  {_c('✘', 'red')} {name}")
            if hint:
                print(_c(f"      → {hint}", "yellow"))

    print()
    if key:
        print("  测试模型连通性…")
        client = LLMClient(LLMConfig(api_key=key,
                                     base_url=resolve_base_url(ROOT),
                                     model=resolve_model(ROOT)),
                           workspace=ROOT)
        result = client.ping()
        if result.get("ok"):
            print(_c(f"  ✔ 模型 {result['model']} 可用（{result['ms']}ms）", "green"))
        else:
            failed += 1
            print(_c(f"  ✘ 模型不可用：{result.get('error')}", "red"))
        models = client.list_models()
        if models:
            print(_c(f"  可用模型：{', '.join(models)}", "dim"))

    print()
    if failed:
        print(_c(f"体检结论：{failed} 项需要处理", "yellow"))
        return 1
    print(_c("体检结论：环境就绪 ✔", "green"))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    ctx_workspace = Path(args.workspace or ROOT)
    key = resolve_api_key(ctx_workspace)
    info = {
        "工作目录": str(ctx_workspace),
        "配置目录": str(ctx_workspace / "var"),
        "base_url": resolve_base_url(ctx_workspace),
        "model": resolve_model(ctx_workspace),
        "api_key": (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("(未设置)" if not key else key),
        "api_key_来源": _key_source(ctx_workspace),
        "急停热键": getattr(args, "hotkey", "ctrl+alt+q"),
        "停止文件": str(ctx_workspace / "var" / "STOP"),
        "审计日志目录": str(ctx_workspace / "var" / "logs"),
        "截图目录": str(ctx_workspace / "var" / "shots"),
    }
    for k, v in info.items():
        print(f"  {k:14} {v}")
    return 0


def _key_source(workspace: Path) -> str:
    for env in ("AUTOPILOT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        if os.environ.get(env):
            return f"环境变量 {env}"
    from .llm import load_project_config
    if load_project_config(workspace).get("api_key"):
        return "var/config.json"
    if (workspace / ".env").is_file():
        return ".env 文件"
    if (Path.home() / ".dsh" / ".credentials.yaml").is_file():
        return "DSH 凭据库 ~/.dsh/.credentials.yaml"
    return "未找到"


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        from .server import serve
    except ImportError as exc:
        print(_c(f"启动 Web 控制台需要额外依赖：{exc}", "red"))
        print("  pip install fastapi uvicorn")
        return 1
    return serve(host=args.host, port=args.port, workspace=Path(args.workspace or ROOT),
                 allow_danger=args.allow_dangerous, open_browser=not args.no_browser)


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autopilot", description="AutoPilot —— Windows 全自动电脑操控框架",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               '  python -m autopilot selfcheck\n'
               '  python -m autopilot observe\n'
               '  python -m autopilot do screenshot\n'
               '  python -m autopilot run "打开记事本，写下今天的日期并保存到桌面"\n'
               '  python -m autopilot run --goal-file task.txt\n'
               '  python -m autopilot run @task.txt\n'
               '  python -m autopilot flow list\n'
               '  python -m autopilot serve\n')
    parser.add_argument("--version", action="version", version=f"autopilot {__version__}")

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--workspace", help="工作目录（默认项目根目录）")
    common.add_argument("--allow-dangerous", action="store_true",
                        help="允许执行删除/关机等危险动作")
    common.add_argument("--dry-run", action="store_true", help="只报告不执行")
    common.add_argument("--mode", choices=["auto", "confirm", "readonly"], default="auto",
                        help="安全模式")
    common.add_argument("--no-hotkey", action="store_true", help="不注册全局急停热键")
    common.add_argument("--hotkey", default="ctrl+alt+q", help="急停热键组合")

    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", parents=[common], help="让 Agent 自主完成目标")
    p_run.add_argument("goal", nargs="?", default="",
                       help="要完成的目标；多行长文本建议用 --goal-file 或 @文件")
    p_run.add_argument("--goal-file", "-f", dest="goal_file", default="",
                       help="从文件读取目标（长任务说明用这个，避免命令行转义问题）")
    p_run.add_argument("--max-steps", type=int, default=30, help="最多多少步")
    p_run.add_argument("--model", help="模型名")
    p_run.add_argument("--base-url", help="API 地址")
    p_run.add_argument("--temperature", type=float, default=0.0)
    p_run.add_argument("--max-tokens", type=int, default=4096)
    p_run.add_argument("--fast", action="store_true", help="每步不做 OCR，速度更快")
    p_run.add_argument("--vision", action="store_true", help="把截图一起发给模型（需视觉模型）")
    p_run.add_argument("--pause", type=float, default=0.0, help="每步之间的停顿秒数")
    p_run.add_argument("--json", action="store_true", help="最后输出 JSON 结果")
    p_run.set_defaults(func=cmd_run)

    p_obs = sub.add_parser("observe", parents=[common], help="打印当前屏幕快照")
    p_obs.add_argument("--json", action="store_true")
    p_obs.add_argument("--no-uia", action="store_true", help="跳过 UI Automation")
    p_obs.add_argument("--no-ocr", action="store_true", help="跳过 OCR")
    p_obs.add_argument("--save", action="store_true", help="同时保存截图")
    p_obs.add_argument("--ocr-region", choices=["window", "screen"], default="window")
    p_obs.add_argument("--max-windows", type=int, default=12)
    p_obs.add_argument("--max-elements", type=int, default=80)
    p_obs.add_argument("--max-ocr-lines", type=int, default=60)
    p_obs.set_defaults(func=cmd_observe)

    p_do = sub.add_parser("do", parents=[common], help="直接执行一个动作")
    p_do.add_argument("action", help="动作名，如 click / screenshot / launch_app")
    p_do.add_argument("params", nargs="*", help="参数，形如 key=value")
    p_do.add_argument("--auto-observe", choices=["full", "fast", "none"], default="none")
    p_do.add_argument("--json", action="store_true")
    p_do.set_defaults(func=cmd_do)

    p_act = sub.add_parser("actions", parents=[common], help="列出所有动作")
    p_act.add_argument("--category", help="只看某一类")
    p_act.add_argument("--json", action="store_true", help="输出 function-calling schema")
    p_act.set_defaults(func=cmd_actions)

    p_flow = sub.add_parser("flow", parents=[common], help="确定性流程")
    p_flow.add_argument("flow_action", choices=["list", "run", "record"])
    p_flow.add_argument("--file", help="流程文件名或路径")
    p_flow.add_argument("--var", action="append", help="变量 key=value，可重复")
    p_flow.add_argument("--output", help="录制输出路径")
    p_flow.add_argument("--name", help="流程名称")
    p_flow.add_argument("--description", help="流程说明")
    p_flow.add_argument("--keep-going", action="store_true", help="某步失败后继续")
    p_flow.add_argument("--auto-observe", choices=["full", "fast", "none"], default="fast")
    p_flow.set_defaults(func=cmd_flow)

    p_sc = sub.add_parser("selfcheck", parents=[common], help="各子系统自检")
    p_sc.add_argument("--json", action="store_true")
    p_sc.set_defaults(func=cmd_selfcheck)

    p_doc = sub.add_parser("doctor", parents=[common], help="环境体检")
    p_doc.set_defaults(func=cmd_doctor)

    p_cfg = sub.add_parser("config", parents=[common], help="显示当前配置")
    p_cfg.set_defaults(func=cmd_config)

    p_srv = sub.add_parser("serve", parents=[common], help="启动 Web 控制台")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=8787)
    p_srv.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    p_srv.set_defaults(func=cmd_serve)

    return parser


def main(argv: list[str] | None = None) -> int:
    _init_console()
    winapi.enable_dpi_awareness()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print(_c("\n已被用户中断", "yellow"))
        return 130
    except Exception as exc:
        print(_c(f"\n出错：{type(exc).__name__}: {exc}", "red"))
        if os.environ.get("AUTOPILOT_DEBUG"):
            import traceback
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
