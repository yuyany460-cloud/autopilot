# -*- coding: utf-8 -*-
"""浏览器扩展的静态校验。

**为什么需要这个文件**：扩展的 background.js 里有**两个同名函数 ``send``**
（一个发 WebSocket 消息，一个发 CDP 命令）。普通脚本允许重复声明，但 manifest
里写了 ``"type": "module"``，ES module 是严格模式 —— 重复声明直接语法错误，
整个 service worker 加载失败。

而浏览器的表现是：卡片显示"已启用"、一切正常，只在角落里淡淡写一句
**"服务工作进程（不活动）"**，一个字的原因都不给。没有这个测试，就只能在
浏览器里反复盲试。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

EXT_DIR = Path(__file__).resolve().parents[1] / "extensions" / "autopilot-bridge"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="没有 node，跳过 JS 语法校验")


def _manifest() -> dict:
    return json.loads((EXT_DIR / "manifest.json").read_text(encoding="utf-8"))


# --- 清单 ---------------------------------------------------------------


def test_extension_files_exist() -> None:
    for name in ("manifest.json", "background.js", "keepalive.js"):
        assert (EXT_DIR / name).is_file(), f"缺少 {name}"


def test_manifest_is_clean_utf8_json() -> None:
    raw = (EXT_DIR / "manifest.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), \
        "manifest.json 带 BOM——PowerShell 的 Set-Content -Encoding UTF8 会加，用编辑器重写"
    data = json.loads(raw.decode("utf-8"))
    assert data["manifest_version"] == 3
    assert data["background"]["service_worker"] in ("background.js",)
    assert "debugger" in data["permissions"], "没有 debugger 权限就驱动不了标签页"


def test_manifest_referenced_files_all_exist() -> None:
    m = _manifest()
    refs = [m["background"]["service_worker"]]
    for cs in m.get("content_scripts", []):
        refs.extend(cs.get("js") or [])
    for rel in refs:
        assert (EXT_DIR / rel).is_file(), f"manifest 引用了不存在的文件：{rel}"


def test_content_script_covers_http_pages() -> None:
    """内容脚本要能注入普通网页——它是让 service worker 别被回收的关键。"""
    m = _manifest()
    cs = m.get("content_scripts") or []
    assert cs, "没有内容脚本，service worker 会在 30 秒空闲后被回收，连接就断了"
    matches = " ".join(match for c in cs for match in c.get("matches", []))
    assert "http://*/*" in matches and "https://*/*" in matches


# --- 语法：必须按 ES module 校验 ----------------------------------------


@needs_node
@pytest.mark.parametrize("name", ["background.js", "keepalive.js"])
def test_scripts_parse_as_es_module(name: str, tmp_path) -> None:
    """必须当成 **ES module** 校验，不能当普通脚本。

    命令行里 ``node --check foo.js`` 按 CommonJS 解析，允许重复声明同名函数；
    而浏览器按 module 解析（manifest 里写了 type: module），会直接报
    ``SyntaxError: Identifier 'x' has already been declared``。
    实测这个差异让扩展"看起来装好了、其实从没启动过"。
    """
    src = EXT_DIR / name
    probe = tmp_path / (src.stem + ".mjs")      # .mjs 才会被当成 ES module
    probe.write_bytes(src.read_bytes())
    proc = subprocess.run([NODE, "--check", str(probe)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, (
        f"{name} 作为 ES module 语法不通过：\n{proc.stderr[:800]}")


@needs_node
def test_no_duplicate_top_level_function_names(tmp_path) -> None:
    """直接点名这个坑：同名函数重复声明（普通脚本能过，模块模式必挂）。"""
    import re

    src = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    names = re.findall(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", src, re.M)
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"background.js 里重复声明的顶层函数：{sorted(dupes)}"


def test_type_op_sends_real_enter_for_newlines() -> None:
    """回归：逐键通道把 ``\\n`` 当普通字符发，代码会被挤成一行。

    实测（2026-10-16 的 PTA 运行）：``method="type"`` 写 20 多行代码，
    读回 ``.cm-line`` 只有 1 行——CDP 的 ``keyDown{key:"\\n"}`` 不是 Enter。
    """
    src = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    body = src.split('case "type":', 1)[1].split('case "press":', 1)[0]
    assert 'pressKey(tabId, "Enter")' in body, "换行必须走真正的 Enter 键事件"
    assert 'pressKey(tabId, "Tab")' in body, r"制表符同理，不能当普通字符发"
    assert 'replace(/\\r\\n?/g, "\\n")' in body, "先归一化 CRLF，否则一个换行会按两次 Enter"


def test_screenshot_has_bounded_timeout() -> None:
    """回归：整页截图在长页面上会挂死，实测卡满 60 秒桥超时。

    现在的策略是三级：① 用 Emulation.setDeviceMetricsOverride 骗出一个离屏视口，
    后台/最小化也能出图；② 直接截；③ 把标签页和窗口顶到前台再截可视区。
    三级都必须落在 withTimeout 里，否则任何一级挂住都会拖满整个桥的 60 秒。
    """
    src = (EXT_DIR / "background.js").read_text(encoding="utf-8")
    body = src.split('case "screenshot":', 1)[1].split('case "reload":', 1)[0]
    assert "withTimeout(" in body, "截图必须带超时上限"
    assert "captureBeyondViewport" in body, "整页截图要用 captureBeyondViewport"
    assert "setDeviceMetricsOverride" in body, "后台截图靠离屏视口，不能只看前台"
    assert '"foreground"' in body, "离屏和直接截图都失败时，还要能顶到前台再试一次"

