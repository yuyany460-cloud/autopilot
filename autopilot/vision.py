# -*- coding: utf-8 -*-
"""图片理解：把图交给模型的视觉能力，而不是靠系统 OCR。

为什么要有这个模块——Windows OCR 对"代码 + 数学公式"基本不可用。
同一张自渲染的测试图（一段带缩进的 C 代码 + 一行公式）实测：

    Windows OCR    行命中  1/12  (8%)
                   #include 〈 st d io ． h >        ← 空格被拆散
                   S u m + = 1 ． 9 /               ← 0 读成 9，全角点号
                   return 9 ；                      ← 0 读成 9，全角分号
    视觉模型       行命中 11/12 (92%)
                   #include <stdio.h>               ← 缩进、符号全对
                   sum += 1.0 / (i * i);

后者唯一的"错"是把公式里的 U+2212 减号规范化成了 ASCII 连字符，属于正常化而非误读。

成本：每张图最多 1024 token，加提示词一次约 800 token，高峰时段约 0.004 元。
速度换准确率非常划算——OCR 那 0.5 秒省下来也读不对。

注意：`deepseek-flash` 支持图像理解（`deepseek-v4-pro` 不支持），
图片只能出现在 `user` 消息里。
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any, Sequence

from .llm import LLMClient, LLMConfig, resolve_api_key, resolve_base_url, resolve_model

# 支持的格式由文件实际内容判断，但 data URL 里的 MIME 还是得给对
_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
         ".gif": "image/gif", ".webp": "image/webp"}

# 读代码/公式的默认指令。要点：强调"一字不差"和"保留缩进"，
# 否则模型会很热心地帮你把代码重排、把公式"化简"。
# 最后一句"看不清写 [?]"很关键：允许它说不知道，幻觉会明显减少——
# 读错一个下标（√a²-x² 读成 √a²+x²）就足以让整道题答错，比承认读不清更糟。
DEFAULT_PROMPT = (
    "把这张图片里的内容一字不差地转录成文本。"
    "如果是代码，必须保留原有缩进和全部符号；"
    "如果是数学公式，用一行普通文本表示（根号写 √，平方写 ²，分数写 a / b），不要化简、不要计算。"
    "只输出内容本身，不要任何解释、不要加代码块标记。"
    "看不清的字写 [?]，不要猜。"
)


class VisionUnavailable(RuntimeError):
    """没有可用的视觉能力（缺 API key 或模型不支持）。"""


def image_block(path: str | Path, detail: str = "original") -> dict[str, Any]:
    """把本地图片文件变成 OpenAI 兼容格式的图片内容块。

    ``detail``：``original`` 保留原图（代码/公式必须用这个，缩放过就读不清了），
    ``low`` 会先缩到 512x512，只适合"图里大概是啥"这种粗看。
    """
    image_path = Path(path)
    if not image_path.is_file():
        raise VisionUnavailable(f"图片不存在：{image_path}")
    mime = _MIME.get(image_path.suffix.lower(), "image/png")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return {"type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def is_available(workspace: str | Path | None = None) -> bool:
    """当前配置能不能用视觉（主要看有没有 API key）。"""
    try:
        return bool(resolve_api_key(workspace))
    except Exception:
        return False


# --------------------------------------------------------------------------
# 识别结果缓存
#
# 同一张公式图在一份卷子里会反复出现（同一道题的选项、同题的多问、重跑一次），
# 而一次视觉调用要 0.9~1.2 秒。按"图片内容 + 提示词 + 模型"做键缓存，
# 内容一模一样就直接命中——重跑 --solve 时图片识别部分是 0 秒、0 token。
# --------------------------------------------------------------------------


def _cache_root(workspace: str | Path | None, cache_dir: str | Path | None) -> Path | None:
    if cache_dir is False:            # 显式关闭缓存
        return None
    if cache_dir:
        path = Path(cache_dir)
    elif workspace:
        path = Path(workspace) / "var" / "cache" / "vision"
    else:
        return None
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _digest(*parts: str) -> str:
    import hashlib

    hasher = hashlib.sha1()
    for part in parts:
        hasher.update(part.encode("utf-8", "replace"))
        hasher.update(b"\x00")
    return hasher.hexdigest()


def _file_digest(path: Path) -> str:
    import hashlib

    hasher = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _cache_key(paths: Sequence[Path], prompt: str, model: str, detail: str) -> str:
    return _digest(model, detail, prompt, *[_file_digest(p) for p in paths])


def _model_name(workspace: str | Path | None, client: Any = None) -> str:
    """缓存键里的模型名：优先用客户端真实的配置，取不到就退回全局解析。"""
    model = getattr(getattr(client, "config", None), "model", "")
    if model:
        return str(model)
    try:
        return str(resolve_model(workspace))
    except Exception:
        return "unknown"


def _cache_get(root: Path | None, key: str) -> str | None:
    if root is None:
        return None
    target = root / f"{key}.txt"
    try:
        if target.is_file():
            text = target.read_text(encoding="utf-8").strip()
            return text or None
    except OSError:
        return None
    return None


def _cache_put(root: Path | None, key: str, text: str) -> None:
    if root is None or not text:
        return
    try:
        (root / f"{key}.txt").write_text(text, encoding="utf-8")
    except OSError:
        pass


def _client(workspace: str | Path | None, max_tokens: int) -> LLMClient:
    cfg = LLMConfig(
        model=resolve_model(workspace),
        base_url=resolve_base_url(workspace),
        api_key=resolve_api_key(workspace),
        max_tokens=max_tokens,
        # 转录是"照着抄"，不需要思考。而 Flash **默认开着思考模式且 effort=high**，
        # 思维链是按 completion token 计费的——实测会出现推理把 max_tokens
        # 吃光、content 直接返回空的情况（真机上遇到过一次，模型只回了空串）。
        # 关掉之后既不会空，也快了不少。
        extra_body={"thinking": {"type": "disabled"}},
    ).resolved()
    if not cfg.api_key:
        raise VisionUnavailable("没有配置 API 密钥，无法调用视觉模型")
    return LLMClient(cfg, workspace=workspace)


def read_image(path: str | Path, prompt: str = "", workspace: str | Path | None = None,
               max_tokens: int = 1500, detail: str = "original",
               cache_dir: str | Path | None = None) -> dict[str, Any]:
    """用视觉模型读一张本地图片。

    返回 ``{"text": ..., "ms": ..., "usage": ..., "cached": bool}``；
    失败抛 ``VisionUnavailable``。同一张图（字节完全相同）读过就直接走缓存。
    """
    image_path = Path(path)
    if not image_path.is_file():
        raise VisionUnavailable(f"图片不存在：{image_path}")
    instruction = (str(prompt).strip() or DEFAULT_PROMPT)
    client = _client(workspace, max_tokens)
    root = _cache_root(workspace, cache_dir)
    key = _cache_key([image_path], instruction, _model_name(workspace, client), detail)
    hit = _cache_get(root, key)
    if hit is not None:
        return {"text": hit, "ms": 0.0, "usage": {}, "cached": True}

    response = client.chat(
        [{"role": "user", "content": [
            {"type": "text", "text": instruction},
            image_block(image_path, detail=detail),
        ]}],
        temperature=0.0,
    )
    text = (response.content or "").strip()
    if not text:
        raise VisionUnavailable("视觉模型没有返回内容")
    _cache_put(root, key, text)
    return {"text": text, "ms": round(response.elapsed_ms, 1), "usage": response.usage,
            "cached": False}


def read_bytes(data: bytes, suffix: str = ".png", prompt: str = "",
               workspace: str | Path | None = None, max_tokens: int = 1500) -> dict[str, Any]:
    """读内存里的图片字节（比如刚截的屏），省一次落盘。"""
    import tempfile

    tmp = Path(tempfile.gettempdir()) / "autopilot-vision"
    tmp.mkdir(parents=True, exist_ok=True)
    path = tmp / f"shot-{abs(hash(data)) & 0xFFFFFFFF:08x}{suffix}"
    path.write_bytes(data)
    try:
        return read_image(path, prompt=prompt, workspace=workspace, max_tokens=max_tokens)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def read_images(images: Sequence[tuple[str, str | Path]],
                prompt: str = "", workspace: str | Path | None = None,
                max_tokens: int = 2000, detail: str = "original",
                cache_dir: str | Path | None = None) -> dict[str, Any]:
    """一次读完多张图，每张带标签（``[("题干", p1), ("选项A", p2), ...]``）。

    为什么需要它：超星/学习通的题干和四个选项是**五张独立的 PNG**，
    分成五次调用既慢又会丢"哪张图对应哪个选项"的对应关系；一起送进去，
    模型能按标签逐段转录，还能顺手做点判断。

    速度账：5 张图分开读 = 5 × 1.0s ≈ 5s；合成一次 = 1.2s 左右，
    而且提示词只付一次（多图共享同一段指令）。
    """
    if not images:
        raise VisionUnavailable("没有要读的图片")
    instruction = (str(prompt).strip() or DEFAULT_PROMPT)
    paths = [Path(p) for _, p in images]
    for image_path in paths:
        if not image_path.is_file():
            raise VisionUnavailable(f"图片不存在：{image_path}")

    client = _client(workspace, max_tokens)
    root = _cache_root(workspace, cache_dir)
    key = _cache_key(paths, instruction, _model_name(workspace, client), detail)
    hit = _cache_get(root, key)
    if hit is not None:
        return {"text": hit, "ms": 0.0, "usage": {}, "cached": True}

    content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
    for label, path in images:
        if label:
            content.append({"type": "text", "text": f"【{label}】"})
        content.append(image_block(path, detail=detail))
    response = client.chat([{"role": "user", "content": content}], temperature=0.0)
    text = (response.content or "").strip()
    if not text:
        raise VisionUnavailable("视觉模型没有返回内容")
    _cache_put(root, key, text)
    return {"text": text, "ms": round(response.elapsed_ms, 1), "usage": response.usage,
            "cached": False}


__all__ = ["DEFAULT_PROMPT", "VisionUnavailable", "image_block", "is_available",
           "read_bytes", "read_image", "read_images"]
