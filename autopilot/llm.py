# -*- coding: utf-8 -*-
"""LLM 客户端：OpenAI 兼容的 Chat Completions + Function Calling。

只用标准库 ``urllib``，不引入任何 HTTP 依赖。

密钥解析顺序（先找到先用）：
    1. 显式传入
    2. 环境变量 ``AUTOPILOT_API_KEY`` / ``DEEPSEEK_API_KEY`` / ``OPENAI_API_KEY``
    3. 本项目的 ``var/config.json``
    4. DSH 的凭据库 ``~/.dsh/.credentials.yaml``
    5. ``.env`` 文件

这样在本机可以直接复用已有的 DeepSeek 密钥，不用重复配置。
"""

from __future__ import annotations

import json
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-flash"

_ENV_KEYS = ("AUTOPILOT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY")
_ENV_BASE = ("AUTOPILOT_BASE_URL", "DEEPSEEK_BASE_URL", "OPENAI_BASE_URL")
_ENV_MODEL = ("AUTOPILOT_MODEL", "DEEPSEEK_MODEL", "OPENAI_MODEL")


class LLMError(RuntimeError):
    """LLM 调用失败。"""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]

    def as_message_part(self) -> dict[str, Any]:
        return {"id": self.id, "type": "function",
                "function": {"name": self.name,
                             "arguments": json.dumps(self.arguments, ensure_ascii=False)}}


@dataclass
class LLMResponse:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMConfig:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 180.0
    retries: int = 3
    extra_body: dict[str, Any] = field(default_factory=dict)

    def resolved(self) -> "LLMConfig":
        cfg = LLMConfig(**{**self.__dict__})
        cfg.api_key = cfg.api_key or resolve_api_key()
        cfg.base_url = (cfg.base_url or DEFAULT_BASE_URL).rstrip("/")
        cfg.model = cfg.model or DEFAULT_MODEL
        return cfg


# --------------------------------------------------------------------------
# 配置发现
# --------------------------------------------------------------------------


def _read_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def _read_dsh_credentials() -> dict[str, str]:
    """读取 DSH 的凭据库（只取需要的键，不修改原文件）。"""
    path = Path.home() / ".dsh" / ".credentials.yaml"
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    out: dict[str, str] = {}
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        match = re.search(rf"(?m)^\s*{key}:\s*(\S+)\s*$", text)
        if match:
            out[key] = match.group(1)
    return out


def load_project_config(workspace: str | Path | None = None) -> dict[str, Any]:
    """读取 ``var/config.json``（可选）。"""
    base = Path(workspace) if workspace else Path.cwd()
    for candidate in (base / "var" / "config.json", base / "config.json"):
        if candidate.is_file():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
    return {}


def resolve_api_key(workspace: str | Path | None = None) -> str:
    for key in _ENV_KEYS:
        if os.environ.get(key):
            return str(os.environ[key])
    cfg = load_project_config(workspace)
    for key in _ENV_KEYS + ("api_key", "apiKey"):
        if cfg.get(key):
            return str(cfg[key])
    dotenv = _read_dotenv((Path(workspace) if workspace else Path.cwd()) / ".env")
    for key in _ENV_KEYS:
        if dotenv.get(key):
            return dotenv[key]
    dsh = _read_dsh_credentials()
    for key in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY"):
        if dsh.get(key):
            return dsh[key]
    return ""


def resolve_base_url(workspace: str | Path | None = None) -> str:
    for key in _ENV_BASE:
        if os.environ.get(key):
            return str(os.environ[key])
    cfg = load_project_config(workspace)
    if cfg.get("base_url"):
        return str(cfg["base_url"])
    dotenv = _read_dotenv((Path(workspace) if workspace else Path.cwd()) / ".env")
    for key in _ENV_BASE:
        if dotenv.get(key):
            return dotenv[key]
    return DEFAULT_BASE_URL


def resolve_model(workspace: str | Path | None = None) -> str:
    for key in _ENV_MODEL:
        if os.environ.get(key):
            return str(os.environ[key])
    cfg = load_project_config(workspace)
    if cfg.get("model"):
        return str(cfg["model"])
    dotenv = _read_dotenv((Path(workspace) if workspace else Path.cwd()) / ".env")
    for key in _ENV_MODEL:
        if dotenv.get(key):
            return dotenv[key]
    return DEFAULT_MODEL


# 复核用的模型：比 flash 强一档，专门用来独立重做一遍关键判断。
# 一次复核大约 2000 输入 + 500 输出 token，空闲时段约 0.016 元——
# 用在"提交不可逆"和"拿不准"的场合非常划算。
REVIEW_MODEL = "deepseek-v4-pro"


def review(question: str, my_answer: str = "", context: str = "",
           workspace: str | Path | None = None,
           model: str = REVIEW_MODEL, max_tokens: int = 8000) -> LLMResponse:
    """让更强的模型**先独立作答**，再和 ``my_answer`` 对比。

    关键是顺序：必须让它先自己判断，再比对。反过来问"这个答案对吗"，
    它会顺着你的答案说"对"——那就白复核了。

    ``max_tokens`` 要给足：pro 默认开思考模式，思维链按 completion token 计费。
    实测给 1600 时，光推理就把额度用光、``content`` 直接返回空
    （usage 里 completion=1600 而 reasoning=1600，一个字答案都没有）。
    """
    prompt = (
        "你是一位严谨的阅卷老师，正在独立复核一道题。\n"
        "**必须按下面的顺序，不要跳步：**\n"
        "1. 先完全独立地解题，不要参考任何人给出的答案。把你的推理过程写清楚，"
        "每一步都要有依据（定义、定理、极限计算…）。\n"
        "2. 得出你自己的答案。\n"
        "3. 最后才和下面的「待复核答案」对比，明确写出是否一致；"
        "如果不一致，指出对方错在哪一步。\n\n"
        f"# 题目\n{question}\n"
    )
    if context:
        prompt += f"\n# 补充信息\n{context}\n"
    if my_answer:
        prompt += (f"\n# 待复核答案（第 3 步之前不要看它）\n{my_answer}\n"
                   f"\n最后一行必须严格用这个格式：`结论：一致` 或 `结论：不一致（正确答案是 X）`")
    else:
        prompt += "\n最后一行必须严格用这个格式：`结论：正确答案是 X`"

    cfg = LLMConfig(
        model=model or REVIEW_MODEL,
        base_url=resolve_base_url(workspace),
        api_key=resolve_api_key(workspace),
        max_tokens=max_tokens,
        # 复核需要思考（要真算一遍），但不需要 high。实测同一道题：
        #   effort=high  23.0s / 1778 推理 token
        #   effort=low   12.0s /  879 推理 token   ← 结论一样且正确
        extra_body={"reasoning_effort": "low"},
    ).resolved()
    if not cfg.api_key:
        raise LLMError("没有配置 API 密钥，无法复核")
    try:
        out = LLMClient(cfg, workspace=workspace).chat(
            [{"role": "user", "content": prompt}], temperature=0.0)
    except LLMError:
        if cfg.model == resolve_model(workspace):
            raise
        # 拿不到更强的模型就退回默认模型：复核弱一点，总比不复核强
        cfg.model = resolve_model(workspace)
        out = LLMClient(cfg, workspace=workspace).chat(
            [{"role": "user", "content": prompt}], temperature=0.0)
    if not (out.content or "").strip():
        # pro 的思考模式能把输出额度整个吃干净、content 返回空——实测给到 8000
        # 仍然 completion=8000 / reasoning=8000，一个字答案都没有（2026-10-19 22:10 两次都这样）。
        # 复核要的是**结论**不是推理过程：关掉思考再问一次，基本一定拿得到答案。
        # 这一步很关键：复核失败会卡住"提交前必须复核"的强制流程，整个任务就交不上去了。
        cfg.extra_body = {"thinking": {"type": "disabled"}}
        out = LLMClient(cfg, workspace=workspace).chat(
            [{"role": "user", "content": prompt}], temperature=0.0)
        if not (out.content or "").strip():
            raise LLMError(
                f"复核模型关掉思考后仍未给出答案（completion="
                f"{out.usage.get('completion_tokens')}）。换个模型或稍后重试。")
    return out


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class LLMClient:
    """OpenAI 兼容接口的最小实现。"""

    def __init__(self, config: LLMConfig | None = None,
                 workspace: str | Path | None = None,
                 log: Callable[[str, str], None] | None = None) -> None:
        self.workspace = workspace
        base = config or LLMConfig()
        if not base.api_key:
            base.api_key = resolve_api_key(workspace)
        if base.base_url == DEFAULT_BASE_URL:
            base.base_url = resolve_base_url(workspace)
        if base.model == DEFAULT_MODEL:
            base.model = resolve_model(workspace)
        self.config = base
        self._log = log or (lambda level, msg: None)
        self._ctx = ssl.create_default_context()
        self.calls = 0
        self.total_ms = 0.0

    # -- 底层请求 --------------------------------------------------------
    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.config.api_key}",
            "Accept": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, max(1, self.config.retries) + 1):
            request = urllib.request.Request(url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=self.config.timeout,
                                            context=self._ctx) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                last_error = LLMError(f"HTTP {exc.code} {exc.reason} {body}")
                if exc.code in (400, 401, 403, 404, 422):
                    raise last_error from exc
                if attempt < self.config.retries:
                    time.sleep(min(2 ** attempt, 8))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = LLMError(f"网络错误：{exc}")
                if attempt < self.config.retries:
                    time.sleep(min(2 ** attempt, 8))
        raise last_error or LLMError("请求失败")

    def _get(self, path: str) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self.config.api_key}"})
        with urllib.request.urlopen(request, timeout=self.config.timeout,
                                    context=self._ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # -- 公开接口 --------------------------------------------------------
    def chat(self, messages: list[dict[str, Any]],
             tools: list[dict[str, Any]] | None = None,
             tool_choice: str | dict[str, Any] | None = "auto",
             temperature: float | None = None,
             max_tokens: int | None = None) -> LLMResponse:
        """发一轮对话，返回文本和/或工具调用。"""
        if not self.config.api_key:
            raise LLMError(
                "没有找到 API 密钥。请任选一种方式配置：\n"
                "  1) set AUTOPILOT_API_KEY=sk-xxx\n"
                "  2) 在 var/config.json 里写 {\"api_key\": \"sk-xxx\"}\n"
                "  3) 在项目根目录建 .env 写入 AUTOPILOT_API_KEY=sk-xxx")

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        payload.update(self.config.extra_body)

        started = time.perf_counter()
        raw = self._post("/chat/completions", payload)
        elapsed = (time.perf_counter() - started) * 1000
        self.calls += 1
        self.total_ms += elapsed

        choices = raw.get("choices") or []
        if not choices:
            raise LLMError(f"响应里没有 choices：{str(raw)[:300]}")
        message = choices[0].get("message") or {}

        calls: list[ToolCall] = []
        for item in message.get("tool_calls") or []:
            fn = item.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                args = {"_raw": raw_args}
            calls.append(ToolCall(id=str(item.get("id") or f"call_{len(calls)}"),
                                  name=str(fn.get("name") or ""), arguments=args))

        return LLMResponse(
            content=str(message.get("content") or ""),
            reasoning=str(message.get("reasoning_content") or ""),
            tool_calls=calls,
            finish_reason=str(choices[0].get("finish_reason") or ""),
            usage=raw.get("usage") or {},
            elapsed_ms=round(elapsed, 1),
            raw=raw,
        )

    def list_models(self) -> list[str]:
        try:
            data = self._get("/models")
            return [str(m.get("id")) for m in (data.get("data") or [])]
        except Exception as exc:
            self._log("warn", f"获取模型列表失败：{exc}")
            return []

    def ping(self) -> dict[str, Any]:
        """连通性自检。"""
        started = time.perf_counter()
        try:
            resp = self.chat([{"role": "user", "content": "回复两个字：正常"}], max_tokens=512)
            return {
                "ok": True,
                "model": self.config.model,
                "reply": (resp.content or "").strip()[:60],
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "usage": resp.usage,
            }
        except Exception as exc:
            return {"ok": False, "model": self.config.model, "error": str(exc)}
