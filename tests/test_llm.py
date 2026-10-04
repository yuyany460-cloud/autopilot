# -*- coding: utf-8 -*-
"""LLM 客户端测试：密钥解析、请求构造、响应解析、错误处理。

用假的 ``_post``/``_get`` 打桩，不发出任何真实网络请求。
"""

from __future__ import annotations

import json

import pytest

from autopilot.llm import (LLMClient, LLMConfig, LLMError, LLMResponse, ToolCall,
                           load_project_config, resolve_api_key, resolve_base_url,
                           resolve_model)


# --- 配置解析 -----------------------------------------------------------


def test_env_var_wins(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AUTOPILOT_API_KEY", "sk-from-env")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-lower-priority")
    assert resolve_api_key(tmp_path) == "sk-from-env"


def test_project_config_used_when_no_env(monkeypatch, tmp_path) -> None:
    for key in ("AUTOPILOT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "var").mkdir(exist_ok=True)
    (tmp_path / "var" / "config.json").write_text(
        json.dumps({"api_key": "sk-from-file", "model": "my-model",
                    "base_url": "https://example.test/v1"}), encoding="utf-8")
    monkeypatch.setattr("autopilot.llm._read_dsh_credentials", lambda: {})
    assert resolve_api_key(tmp_path) == "sk-from-file"
    assert resolve_model(tmp_path) == "my-model"
    assert resolve_base_url(tmp_path) == "https://example.test/v1"


def test_dotenv_used_as_fallback(monkeypatch, tmp_path) -> None:
    for key in ("AUTOPILOT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("autopilot.llm._read_dsh_credentials", lambda: {})
    (tmp_path / ".env").write_text(
        "# 注释\nAUTOPILOT_API_KEY=sk-dotenv\nAUTOPILOT_MODEL=dotenv-model\n",
        encoding="utf-8")
    assert resolve_api_key(tmp_path) == "sk-dotenv"
    assert resolve_model(tmp_path) == "dotenv-model"


def test_dsh_credentials_as_last_resort(monkeypatch, tmp_path) -> None:
    for key in ("AUTOPILOT_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "LLM_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr("autopilot.llm._read_dsh_credentials",
                        lambda: {"DEEPSEEK_API_KEY": "sk-dsh"})
    assert resolve_api_key(tmp_path) == "sk-dsh"


def test_defaults() -> None:
    assert resolve_base_url(None) .startswith("http")
    assert isinstance(resolve_model(None), str)


def test_load_project_config_missing(tmp_path) -> None:
    assert load_project_config(tmp_path) == {}


def test_resolved_fills_defaults(tmp_path) -> None:
    cfg = LLMConfig(api_key="sk-x").resolved()
    assert cfg.model and cfg.base_url and not cfg.base_url.endswith("/")


# --- 请求与响应 ---------------------------------------------------------


class _Stub(LLMClient):
    """拦截 HTTP 层，返回构造好的响应。"""

    def __init__(self, payload, **kw):
        super().__init__(LLMConfig(api_key="sk-test", model="m"), **kw)
        self.payload = payload
        self.sent: list[dict] = []

    def _post(self, path, body):
        self.sent.append({"path": path, "body": body})
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _reply(content=None, tool_calls=None, usage=None):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {"choices": [{"message": message, "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 1, "completion_tokens": 2}}


def test_plain_text_reply() -> None:
    client = _Stub(_reply(content="你好"))
    resp = client.chat([{"role": "user", "content": "hi"}])
    assert resp.content == "你好"
    assert resp.tool_calls == []
    assert resp.usage["prompt_tokens"] == 1


def test_tool_call_parsing() -> None:
    client = _Stub(_reply(tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "click", "arguments": '{"element": "E7"}'}}]))
    resp = client.chat([{"role": "user", "content": "点它"}], tools=[{"type": "function"}])
    assert len(resp.tool_calls) == 1
    call = resp.tool_calls[0]
    assert call.name == "click" and call.arguments == {"element": "E7"}
    assert call.as_message_part()["function"]["name"] == "click"


def test_malformed_tool_arguments_do_not_crash() -> None:
    client = _Stub(_reply(tool_calls=[{
        "id": "c", "function": {"name": "click", "arguments": "{不是合法 json"}}]))
    resp = client.chat([{"role": "user", "content": "x"}])
    assert resp.tool_calls[0].arguments.get("_raw")


def test_tools_are_included_in_request() -> None:
    client = _Stub(_reply(content="ok"))
    tools = [{"type": "function", "function": {"name": "click", "description": "d",
                                               "parameters": {"type": "object"}}}]
    client.chat([{"role": "user", "content": "x"}], tools=tools)
    body = client.sent[0]["body"]
    assert body["tools"] == tools
    assert body["tool_choice"] == "auto"
    assert body["model"] == "m"


def test_no_tools_means_no_tools_key() -> None:
    client = _Stub(_reply(content="ok"))
    client.chat([{"role": "user", "content": "x"}])
    assert "tools" not in client.sent[0]["body"]


def test_reasoning_content_captured() -> None:
    payload = _reply(content="答案")
    payload["choices"][0]["message"]["reasoning_content"] = "先想一下"
    assert _Stub(payload).chat([{"role": "user", "content": "x"}]).reasoning == "先想一下"


def test_empty_choices_raises() -> None:
    with pytest.raises(LLMError, match="没有 choices"):
        _Stub({"choices": []}).chat([{"role": "user", "content": "x"}])


def test_missing_api_key_gives_helpful_error(tmp_path) -> None:
    client = LLMClient(LLMConfig(api_key="", base_url="https://x", model="m"),
                       workspace=tmp_path)
    client.config.api_key = ""
    with pytest.raises(LLMError, match="没有找到 API 密钥"):
        client.chat([{"role": "user", "content": "x"}])


def test_usage_accumulates_over_calls() -> None:
    client = _Stub(_reply(content="a"))
    client.chat([{"role": "user", "content": "1"}])
    client.chat([{"role": "user", "content": "2"}])
    assert client.calls == 2 and client.total_ms >= 0


def test_ping_reports_failure_not_raises() -> None:
    result = _Stub(LLMError("boom")).ping()
    assert result["ok"] is False and "boom" in result["error"]


def test_list_models_handles_error() -> None:
    client = _Stub(_reply(content="x"))
    client._get = lambda path: (_ for _ in ()).throw(OSError("no net"))  # type: ignore
    assert client.list_models() == []


def test_tool_call_dataclass_shape() -> None:
    call = ToolCall(id="c1", name="n", arguments={"a": 1})
    part = call.as_message_part()
    assert part["type"] == "function"
    assert json.loads(part["function"]["arguments"]) == {"a": 1}


def test_response_holds_raw_payload() -> None:
    payload = _reply(content="x")
    assert _Stub(payload).chat([{"role": "user", "content": "y"}]).raw == payload


def test_llm_response_defaults() -> None:
    resp = LLMResponse()
    assert resp.content == "" and resp.tool_calls == [] and resp.usage == {}
