# -*- coding: utf-8 -*-
"""动作注册表：把"电脑能做的事"定义成模型可调用的工具。

每个动作包含参数声明，可直接导出成 OpenAI / DeepSeek 的
function-calling schema，也可以渲染成纯文本清单给不支持工具调用的模型。

危险等级驱动安全策略（见 ``guard.py``）：

====== ==========================================================
等级   含义
====== ==========================================================
safe   只读或纯感知，绝无副作用
low    有副作用但容易撤销（移动鼠标、点击、切换窗口）
medium 影响系统状态（写文件、关窗口、执行命令、结束进程）
high   不可逆或影响全局（删除、关机、改注册表、结束系统进程）
====== ==========================================================
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


class Danger(str, Enum):
    SAFE = "safe"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

    @property
    def rank(self) -> int:
        return {"safe": 0, "low": 1, "medium": 2, "high": 3}[self.value]

    @property
    def label(self) -> str:
        return {"safe": "只读", "low": "轻微", "medium": "中等", "high": "危险"}[self.value]


_JSON_TYPES = {
    "string": "string", "str": "string", "text": "string",
    "integer": "integer", "int": "integer",
    "number": "number", "float": "number",
    "boolean": "boolean", "bool": "boolean",
    "object": "object", "dict": "object",
    "array": "array", "list": "array",
}


@dataclass
class Param:
    """一个动作参数。"""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    default: Any = None
    enum: list[Any] | None = None
    items: str | None = None

    def to_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": _JSON_TYPES.get(self.type, "string")}
        if self.description:
            schema["description"] = self.description
        if self.enum:
            schema["enum"] = list(self.enum)
        if schema["type"] == "array":
            schema["items"] = {"type": _JSON_TYPES.get(self.items or "string", "string")}
        if self.default is not None:
            schema["default"] = self.default
        return schema


@dataclass
class Action:
    """一个可执行动作。"""

    name: str
    description: str
    params: list[Param]
    handler: Callable[..., Any]
    danger: Danger = Danger.SAFE
    category: str = "general"
    returns: str = ""
    aliases: list[str] = field(default_factory=list)
    mutates: bool = True      # 是否改变系统状态（决定执行后要不要重新感知）
    long_running: bool = False  # 是否可能耗时较久（用更长的超时）

    def schema(self) -> dict[str, Any]:
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in self.params:
            props[p.name] = p.to_schema()
            if p.required:
                required.append(p.name)
        params: dict[str, Any] = {"type": "object", "properties": props,
                                  "additionalProperties": False}
        if required:
            params["required"] = required
        return params

    def openai_tool(self) -> dict[str, Any]:
        desc = self.description
        if self.returns:
            desc = f"{desc}\n返回：{self.returns}"
        if self.danger.rank >= Danger.MEDIUM.rank:
            desc = f"{desc}\n⚠ 风险等级：{self.danger.label}"
        return {
            "type": "function",
            "function": {"name": self.name, "description": desc,
                         "parameters": self.schema()},
        }

    def call(self, **kwargs: Any) -> Any:
        """按声明清洗参数后调用（丢弃未声明参数、填充默认值）。"""
        accepted = {p.name for p in self.params}
        clean = {k: v for k, v in kwargs.items() if k in accepted}
        for p in self.params:
            if p.name not in clean and p.default is not None:
                clean[p.name] = p.default
        missing = [p.name for p in self.params if p.required and clean.get(p.name) in (None, "")]
        if missing:
            raise ValueError(f"动作 {self.name} 缺少必填参数：{', '.join(missing)}")
        return self.handler(**clean)

    def signature_hint(self) -> str:
        bits = []
        for p in self.params:
            marker = "" if p.required else "?"
            bits.append(f"{p.name}{marker}")
        return ", ".join(bits)


class Registry:
    """动作集合。"""

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}
        self._alias: dict[str, str] = {}
        self._categories: dict[str, list[str]] = {}

    # -- 注册 ------------------------------------------------------------
    def register(self, action: Action) -> Action:
        if action.name in self._actions:
            raise ValueError(f"动作重名：{action.name}")
        _verify_handler(action)
        self._actions[action.name] = action
        for alias in action.aliases:
            self._alias[alias] = action.name
        self._categories.setdefault(action.category, []).append(action.name)
        return action

    def action(self, name: str, description: str, params: Iterable[Param] | None = None,
               danger: Danger = Danger.SAFE, category: str = "general",
               returns: str = "", aliases: Iterable[str] | None = None,
               mutates: bool = True, long_running: bool = False):
        """装饰器写法：把函数注册成动作。"""

        def wrapper(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.register(Action(
                name=name, description=description, params=list(params or []),
                handler=fn, danger=danger, category=category, returns=returns,
                aliases=list(aliases or []), mutates=mutates, long_running=long_running,
            ))
            return fn

        return wrapper

    # -- 查询 ------------------------------------------------------------
    def get(self, name: str) -> Action | None:
        key = str(name).strip()
        if key in self._actions:
            return self._actions[key]
        resolved = self._alias.get(key)
        if resolved:
            return self._actions[resolved]
        # 容错：模型偶尔会写成 click_element / Click
        low = key.lower()
        for candidate, act in self._actions.items():
            if candidate.lower() == low or candidate.lower().replace("_", "") == low.replace("_", ""):
                return act
        return None

    def require(self, name: str) -> Action:
        action = self.get(name)
        if action is None:
            known = ", ".join(sorted(self._actions))
            raise KeyError(f"未知动作 {name!r}。可用动作：{known}")
        return action

    def names(self) -> list[str]:
        return sorted(self._actions)

    def all(self) -> list[Action]:
        return [self._actions[n] for n in self.names()]

    def categories(self) -> dict[str, list[Action]]:
        return {cat: [self._actions[n] for n in names]
                for cat, names in sorted(self._categories.items())}

    def openai_tools(self, allow_danger: bool = False,
                     only: Iterable[str] | None = None) -> list[dict[str, Any]]:
        subset = set(only) if only else None
        out = []
        for act in self.all():
            if subset and act.name not in subset:
                continue
            if not allow_danger and act.danger.rank >= Danger.HIGH.rank:
                continue
            out.append(act.openai_tool())
        return out


# 全局默认注册表
REGISTRY = Registry()


def _verify_handler(action: Action) -> None:
    """注册时校验参数声明与处理函数签名一致。

    声明了一个 handler 不接受的参数，会在任务跑到一半时才炸出
    ``TypeError: unexpected keyword argument``；在注册阶段就拦下来，
    问题会立刻暴露（本项目就踩过一次）。
    """
    import inspect

    params = inspect.signature(action.handler).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return  # **kwargs 形态，随便传

    accepted = {name for name, p in params.items()
                if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD,
                              inspect.Parameter.KEYWORD_ONLY)}
    declared = {p.name for p in action.params}

    extra = declared - accepted
    if extra:
        raise ValueError(
            f"动作 {action.name} 声明了 handler 不接受的参数：{sorted(extra)}；"
            f"handler 实际接受 {sorted(accepted)}")
