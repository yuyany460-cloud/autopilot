# -*- coding: utf-8 -*-
"""安全护栏：在动作真正落到系统上之前做一次判定。

一个能"完全自动化操控电脑"的程序，如果没有刹车就是灾难。
这里提供四层保护：

1. **风险分级**：HIGH 级动作默认直接拒绝，必须显式开启 ``allow_danger``。
2. **命令黑名单**：正则匹配破坏性命令（格式化、删盘、改注册表、关机…）。
3. **路径保护**：写入/删除系统目录一律拦截。
4. **模式开关**：``readonly`` 只允许只读动作；``dry_run`` 只报告不执行。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .registry import Action, Danger


# 破坏性命令模式。命中即拒绝，不看风险等级。
DANGEROUS_COMMAND_PATTERNS: list[tuple[str, str]] = [
    (r"\bformat\s+[a-z]:", "格式化磁盘"),
    (r"\bdiskpart\b", "磁盘分区工具"),
    (r"\bcipher\s+/w", "擦除磁盘空闲空间"),
    (r"\bmkfs\b", "格式化文件系统"),
    (r"\bvssadmin\b.*\bdelete\b", "删除卷影副本"),
    (r"\bwmic\b.*\bshadowcopy\b.*\bdelete\b", "删除卷影副本"),
    (r"\bwbadmin\b.*\bdelete\b", "删除备份"),
    (r"\bbcdedit\b", "修改启动配置"),
    (r"\bbootrec\b", "修改启动记录"),
    (r"\breg\s+delete\b", "删除注册表项"),
    (r"\breg\s+add\b.*\b/d\s+\"\"", "清空注册表值"),
    (r"\bnet\s+user\b.*\s/delete\b", "删除用户账户"),
    (r"\bnet\s+localgroup\s+administrators\b.*\b/(add|delete)\b", "修改管理员组"),
    (r"\btakeown\b.*\s/f\b", "夺取文件所有权"),
    (r"\bicacls\b.*\s/reset\b", "重置文件权限"),
    (r"\bshutdown\b", "关机指令（请改用 power 动作）"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork 炸弹"),
    (r"\bClear-Disk\b", "清空磁盘"),
    (r"\bInitialize-Disk\b", "初始化磁盘"),
    (r"\bRemove-Partition\b", "删除分区"),
    (r"\bSet-MpPreference\b.*-Disable", "关闭杀毒防护"),
    (r"\b(Stop|Disable)-(Service|WindowsOptionalFeature)\b.*"
     r"\b(WinDefend|wuauserv|EventLog|BFE|MpsSvc|TermService|LanmanServer)\b",
     "停用关键系统服务"),
    (r"\btaskkill\b.*\s/im\s+(explorer|winlogon|csrss|wininit|services|lsass|smss)\.",
     "结束关键系统进程"),
    # 下载后直接执行：典型的远程代码执行风险
    (r"(?i)\b(invoke-webrequest|iwr|curl|wget|bitsadmin)\b[^\n|]*\|\s*"
     r"(iex|invoke-expression|powershell|pwsh|cmd|bash)",
     "下载并直接执行远程代码"),
    (r"(?i)\bSet-ExecutionPolicy\b\s+(Unrestricted|Bypass)\b", "放宽脚本执行策略"),
    (r"(?i)\bNew-ItemProperty\b.*\bHKLM:", "写入 HKLM 注册表"),
    (r"(?i)\b(Remove|Set|New)-ItemProperty\b.*\bHKLM:\\?SYSTEM\b", "修改系统注册表项"),
    (r"(?i)\bsc(\.exe)?\s+delete\b", "删除系统服务"),
    (r"(?i)\bStart-Process\b.*-Verb\s+RunAs\b.*\bcmd\b", "提权启动命令行"),
]

# 删除类命令（与本项目的"受保护路径"叠加判定）
_DELETE_VERB = re.compile(
    r"(?i)(^|[\s;&|(])(del|erase|rmdir|rd|remove-item|ri|rm|shutil\.rmtree|unlink)(\s|$|[;&|)])")
_DRIVE_ROOT = re.compile(r"(?i)(^|[\s\"'])([a-z]):[\\/]?(\s|$|\*|[\"'])")


# 受保护的路径前缀（写入 / 删除会被拒绝）
PROTECTED_PATHS = [
    r"c:\windows",
    r"c:\program files",
    r"c:\program files (x86)",
    r"c:\programdata",
    r"c:\$recycle.bin",
    r"c:\system volume information",
    r"c:\recovery",
    r"c:\perflogs",
]

_BLOCKED = [(re.compile(p, re.IGNORECASE | re.DOTALL), why)
            for p, why in DANGEROUS_COMMAND_PATTERNS]


@dataclass
class Decision:
    """护栏判定结果。"""

    allowed: bool
    reason: str = ""
    requires_confirm: bool = False
    dry_run: bool = False

    def __bool__(self) -> bool:
        return self.allowed


@dataclass
class Policy:
    """安全策略配置。"""

    mode: str = "auto"                 # auto | confirm | readonly
    allow_danger: bool = False         # 是否允许 HIGH 级动作
    dry_run: bool = False              # 只报告不执行
    max_steps: int = 40                # 单次任务最多多少步
    action_timeout: float = 120.0
    extra_blocked: list[str] = field(default_factory=list)
    extra_blocked_reasons: dict[str, str] = field(default_factory=dict)
    denied_actions: list[str] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)
    protect_paths: bool = True
    workspace: Path | None = None       # 允许自由写入的工作目录
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode, "allow_danger": self.allow_danger,
            "dry_run": self.dry_run, "max_steps": self.max_steps,
            "denied_actions": list(self.denied_actions),
            "allowed_actions": list(self.allowed_actions),
            "protect_paths": self.protect_paths,
            "workspace": str(self.workspace) if self.workspace else None,
        }


def _norm(path: str) -> str:
    try:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))).lower()
    except Exception:
        return str(path).lower()


def _is_protected(path: str) -> str | None:
    p = _norm(path)
    for prefix in PROTECTED_PATHS:
        if p == prefix or p.startswith(prefix + os.sep):
            return prefix
    return None


class Guard:
    """执行前的安全判定器。"""

    def __init__(self, policy: Policy | None = None) -> None:
        self.policy = policy or Policy()
        self._blocked = list(_BLOCKED)
        for pattern in self.policy.extra_blocked:
            self._blocked.append((re.compile(pattern, re.IGNORECASE | re.DOTALL),
                                  self.policy.extra_blocked_reasons.get(pattern, "自定义规则")))
        self.denials: list[dict[str, Any]] = []

    # -- 判定 ------------------------------------------------------------
    def check(self, action: Action, params: dict[str, Any]) -> Decision:
        pol = self.policy
        name = action.name

        if name in pol.denied_actions:
            return self._deny(name, params, "该动作在拒绝列表中")

        if pol.mode == "readonly" and action.danger != Danger.SAFE:
            return self._deny(name, params, "只读模式：仅允许无副作用动作")

        if action.danger == Danger.HIGH and not pol.allow_danger:
            return self._deny(name, params,
                              f"{name} 属于危险动作，需要 --allow-dangerous 才可执行")

        if pol.allowed_actions and name not in pol.allowed_actions:
            return self._deny(name, params, "该动作不在白名单中")

        verdict = self._check_by_action(name, params)
        if verdict is not None:
            return self._deny(name, params, verdict)

        if pol.mode == "confirm" and action.danger.rank >= Danger.LOW.rank:
            return Decision(allowed=True, requires_confirm=True,
                            reason="确认模式：每次副作用操作都需要人工确认")

        if pol.dry_run:
            return Decision(allowed=True, dry_run=True, reason="演练模式：不会真正执行")
        return Decision(allowed=True)

    def _deny(self, name: str, params: dict[str, Any], reason: str) -> Decision:
        record = {"action": name, "params": _safe_params(params), "reason": reason}
        self.denials.append(record)
        return Decision(allowed=False, reason=reason)

    def _check_by_action(self, name: str, params: dict[str, Any]) -> str | None:
        """动作专属检查；返回非空字符串表示要拒绝。"""
        if name in ("run_command", "powershell"):
            cmd = str(params.get("command") or params.get("script") or "")
            hit = self.check_command_in_dir(cmd, str(params.get("cwd") or ""))
            if hit:
                return hit

        if name in ("write_file", "make_dir", "copy_path", "move_path"):
            for key in ("path", "dst", "dest", "destination", "src"):
                value = params.get(key)
                if not value:
                    continue
                if key in ("src",):
                    continue  # 读取源路径不拦
                why = self.check_path(str(value), write=True)
                if why:
                    return why

        if name in ("delete_path",):
            value = params.get("path")
            if value:
                why = self.check_path(str(value), write=True, delete=True)
                if why:
                    return why

        if name == "kill_process":
            target = str(params.get("target") or params.get("name") or "").lower()
            protected = ("explorer", "winlogon", "csrss", "wininit", "services",
                         "lsass", "smss", "system", "registry", "memory compression")
            if any(p in target for p in protected):
                return f"拒绝结束关键系统进程：{target}"

        if name == "power":
            mode = str(params.get("action") or "").lower()
            if mode in ("shutdown", "restart", "logoff", "sleep") and not self.policy.allow_danger:
                return f"电源操作 {mode} 需要 --allow-dangerous"
        return None

    def check_command(self, command: str) -> str | None:
        """检查命令行是否命中黑名单。"""
        text = str(command)
        for pattern, why in self._blocked:
            if pattern.search(text):
                return f"命令被安全策略拦截（{why}）"

        # 通用规则一：删除类命令 + 盘符根目录
        if _DELETE_VERB.search(text) and _DRIVE_ROOT.search(text):
            return "命令被安全策略拦截（删除盘符根目录）"

        # 通用规则二：删除类命令 + 受保护的系统路径
        if _DELETE_VERB.search(text):
            low = text.lower()
            for prefix in PROTECTED_PATHS:
                if prefix in low:
                    return f"命令被安全策略拦截（删除受保护路径 {prefix}）"
        return None

    def check_command_in_dir(self, command: str, cwd: str = "") -> str | None:
        """额外考虑工作目录：在系统目录里执行删除同样危险。"""
        hit = self.check_command(command)
        if hit:
            return hit
        if cwd and self.policy.protect_paths and _DELETE_VERB.search(str(command)):
            protected = _is_protected(cwd)
            if protected:
                return f"拒绝在受保护目录 {protected} 中执行删除命令"
        return None

    def check_path(self, path: str, write: bool = False, delete: bool = False) -> str | None:
        """检查路径是否允许操作。"""
        if not self.policy.protect_paths:
            return None
        protected = _is_protected(path)
        if protected and (write or delete):
            return f"受保护的系统路径不可修改：{protected}"
        if delete:
            p = _norm(path).rstrip("\\/")
            # 禁止删除盘符根目录和用户主目录本身
            if re.fullmatch(r"[a-z]:", p) or p in ("c:\\users", os.path.expanduser("~").lower()):
                return f"拒绝删除关键目录：{p}"
            if len(p) <= 3:
                return f"拒绝对过短的路径执行删除：{p}"
        return None

    def summary(self) -> str:
        pol = self.policy
        bits = [f"模式={pol.mode}"]
        if pol.dry_run:
            bits.append("演练")
        bits.append("允许危险操作" if pol.allow_danger else "拦截危险操作")
        if pol.protect_paths:
            bits.append("保护系统路径")
        if pol.denied_actions:
            bits.append(f"拒绝列表 {len(pol.denied_actions)} 项")
        return " | ".join(bits)


def _safe_params(params: dict[str, Any], limit: int = 300) -> dict[str, Any]:
    out = {}
    for k, v in (params or {}).items():
        text = str(v)
        out[k] = text[:limit] + ("…" if len(text) > limit else "")
    return out
