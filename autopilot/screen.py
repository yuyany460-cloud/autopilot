# -*- coding: utf-8 -*-
"""屏幕捕获与图像处理。

默认后端是 **GDI BitBlt（纯 ctypes）**，因此核心功能零第三方依赖；
检测到 ``mss`` / ``Pillow`` 时自动用它们加速与大图缩放。

PNG 编码在本模块内用 ``zlib`` 手写实现，所以即使没装 Pillow
也能把截图落盘、交给 OCR 或人眼查看。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as w
import hashlib
import io
import struct
import time
import zlib
from pathlib import Path
from typing import Any

from . import winapi

try:  # 可选加速
    from PIL import Image as _PILImage  # type: ignore

    HAS_PIL = True
except Exception:  # pragma: no cover
    _PILImage = None  # type: ignore
    HAS_PIL = False

try:  # 可选加速
    import mss as _mss  # type: ignore

    HAS_MSS = True
except Exception:  # pragma: no cover
    _mss = None  # type: ignore
    HAS_MSS = False


if winapi.IS_WINDOWS:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

    _user32.GetDC.argtypes = (ctypes.c_void_p,)
    _user32.GetDC.restype = ctypes.c_void_p
    _user32.ReleaseDC.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _gdi32.CreateCompatibleDC.argtypes = (ctypes.c_void_p,)
    _gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    _gdi32.CreateCompatibleBitmap.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int)
    _gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    _gdi32.SelectObject.argtypes = (ctypes.c_void_p, ctypes.c_void_p)
    _gdi32.SelectObject.restype = ctypes.c_void_p
    _gdi32.DeleteObject.argtypes = (ctypes.c_void_p,)
    _gdi32.DeleteDC.argtypes = (ctypes.c_void_p,)


SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", w.DWORD),
        ("biWidth", w.LONG),
        ("biHeight", w.LONG),
        ("biPlanes", w.WORD),
        ("biBitCount", w.WORD),
        ("biCompression", w.DWORD),
        ("biSizeImage", w.DWORD),
        ("biXPelsPerMeter", w.LONG),
        ("biYPelsPerMeter", w.LONG),
        ("biClrUsed", w.DWORD),
        ("biClrImportant", w.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [("rgbBlue", ctypes.c_ubyte), ("rgbGreen", ctypes.c_ubyte),
                ("rgbRed", ctypes.c_ubyte), ("rgbReserved", ctypes.c_ubyte)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


# argtypes 必须显式声明：否则 64 位下 GDI 句柄会被 ctypes 当成 c_int，直接溢出。
if winapi.IS_WINDOWS:
    _gdi32.BitBlt.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                              w.DWORD)
    _gdi32.BitBlt.restype = w.BOOL
    _gdi32.GetDIBits.argtypes = (ctypes.c_void_p, ctypes.c_void_p, w.UINT, w.UINT,
                                 ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), w.UINT)
    _gdi32.GetDIBits.restype = ctypes.c_int
    _gdi32.GetDeviceCaps.argtypes = (ctypes.c_void_p, ctypes.c_int)
    _gdi32.GetDeviceCaps.restype = ctypes.c_int
    _gdi32.DeleteObject.restype = w.BOOL
    _gdi32.DeleteDC.restype = w.BOOL

    _user32.GetWindowDC.restype = ctypes.c_void_p
    _user32.GetWindowDC.argtypes = (ctypes.c_void_p,)
    _user32.PrintWindow.argtypes = (ctypes.c_void_p, ctypes.c_void_p, w.UINT)
    _user32.PrintWindow.restype = w.BOOL


# --------------------------------------------------------------------------
# 图像容器
# --------------------------------------------------------------------------


class Grab:
    """一张截屏。像素为 **BGRA**、自上而下、紧密排列。"""

    __slots__ = ("origin", "width", "height", "bgra")

    def __init__(self, origin: tuple[int, int], width: int, height: int, bgra: bytes) -> None:
        self.origin = origin
        self.width = int(width)
        self.height = int(height)
        self.bgra = bgra

    # -- 属性 ------------------------------------------------------------
    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        """取相对本图的 ``(x, y)`` 处 RGB 值。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            raise IndexError("坐标超出截图范围")
        i = (y * self.width + x) * 4
        b, g, r = self.bgra[i], self.bgra[i + 1], self.bgra[i + 2]
        return r, g, b

    # -- 变换 ------------------------------------------------------------
    def to_pil(self):
        if not HAS_PIL:
            raise RuntimeError("未安装 Pillow，无法转换为 PIL.Image")
        return _PILImage.frombytes("RGB", (self.width, self.height), self.bgra, "raw", "BGRX")

    def scaled(self, max_width: int, max_height: int) -> "Grab":
        """等比缩放到不超过 ``max_width x max_height``（只缩不放）。"""
        if self.width <= max_width and self.height <= max_height:
            return self
        ratio = min(max_width / self.width, max_height / self.height)
        nw = max(1, int(self.width * ratio))
        nh = max(1, int(self.height * ratio))

        if HAS_PIL:
            img = self.to_pil().resize((nw, nh), _PILImage.LANCZOS)
            buf = img.convert("RGB").tobytes()
            # RGB -> BGRA
            bgra = bytearray(nw * nh * 4)
            bgra[0::4] = buf[2::3]
            bgra[1::4] = buf[1::3]
            bgra[2::4] = buf[0::3]
            bgra[3::4] = b"\xff" * (nw * nh)
            return Grab(self.origin, nw, nh, bytes(bgra))

        # 纯 Python 最近邻：用步进切片，速度由 C 层保证
        step_x = self.width / nw
        step_y = self.height / nh
        out = bytearray(nw * nh * 4)
        src = self.bgra
        for row in range(nh):
            sy = int(row * step_y) * self.width * 4
            line = src[sy:sy + self.width * 4]
            dst = row * nw * 4
            for col in range(nw):
                sx = int(col * step_x) * 4
                out[dst + col * 4: dst + col * 4 + 4] = line[sx:sx + 4]
        return Grab(self.origin, nw, nh, bytes(out))

    def fingerprint(self) -> str:
        return hashlib.blake2b(self.bgra, digest_size=8).hexdigest()

    def diff_ratio(self, other: "Grab", sample: int = 16) -> float:
        """粗略比较两张图差异比例（0.0 ~ 1.0），用于等待画面稳定。"""
        if (self.width, self.height) != (other.width, other.height):
            return 1.0
        a, b = self.bgra, other.bgra
        stride = 4 * sample
        total = 0
        changed = 0
        for i in range(0, len(a) - 4, stride):
            total += 1
            if abs(a[i] - b[i]) > 12 or abs(a[i + 1] - b[i + 1]) > 12:
                changed += 1
        return changed / total if total else 0.0

    # -- 编码 ------------------------------------------------------------
    def to_png_bytes(self) -> bytes:
        """手写 PNG（真彩色、无 alpha），零依赖。"""
        width, height, src = self.width, self.height, self.bgra
        raw = bytearray()
        stride = width * 4
        for y in range(height):
            row = src[y * stride:(y + 1) * stride]
            raw.append(0)  # filter type: None
            rgb = bytearray(width * 3)
            rgb[0::3] = row[2::4]
            rgb[1::3] = row[1::4]
            rgb[2::3] = row[0::4]
            raw += rgb

        def chunk(tag: bytes, payload: bytes) -> bytes:
            return (struct.pack(">I", len(payload)) + tag + payload
                    + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                + chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b""))

    def to_bytes(self, fmt: str = "png", quality: int = 80) -> bytes:
        fmt = fmt.lower().lstrip(".")
        if fmt == "png" and not HAS_PIL:
            return self.to_png_bytes()
        if not HAS_PIL:
            raise RuntimeError(f"保存 {fmt} 需要安装 Pillow：pip install pillow")
        img = self.to_pil()
        buf = io.BytesIO()
        if fmt in ("jpg", "jpeg"):
            img.save(buf, format="JPEG", quality=quality, optimize=True)
        else:
            img.save(buf, format=fmt.upper())
        return buf.getvalue()

    def save(self, path: str | Path, fmt: str | None = None, quality: int = 80) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        fmt = fmt or (p.suffix.lstrip(".") or "png")
        p.write_bytes(self.to_bytes(fmt, quality=quality))
        return p


# --------------------------------------------------------------------------
# 捕获
# --------------------------------------------------------------------------


def _gdi_grab(x: int, y: int, width: int, height: int) -> Grab:
    width, height = max(1, int(width)), max(1, int(height))
    hdc = _user32.GetDC(None)
    if not hdc:
        raise RuntimeError("GetDC 失败")
    memdc = bmp = old = None
    try:
        memdc = _gdi32.CreateCompatibleDC(hdc)
        bmp = _gdi32.CreateCompatibleBitmap(hdc, width, height)
        if not memdc or not bmp:
            raise RuntimeError("创建 GDI 位图失败")
        old = _gdi32.SelectObject(memdc, bmp)
        if not _gdi32.BitBlt(memdc, 0, 0, width, height, hdc, int(x), int(y), SRCCOPY):
            raise RuntimeError("BitBlt 失败")

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # 负值 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        buf = ctypes.create_string_buffer(width * height * 4)
        got = _gdi32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(bmi), DIB_RGB_COLORS)
        if not got:
            raise RuntimeError("GetDIBits 失败")
        return Grab((int(x), int(y)), width, height, buf.raw[: width * height * 4])
    finally:
        if memdc and old:
            _gdi32.SelectObject(memdc, old)
        if bmp:
            _gdi32.DeleteObject(bmp)
        if memdc:
            _gdi32.DeleteDC(memdc)
        _user32.ReleaseDC(None, hdc)


def _mss_grab(x: int, y: int, width: int, height: int) -> Grab:
    # mss 10 起把 mss.mss() 标记为弃用，改叫 mss.MSS()；两者都兼容
    factory = getattr(_mss, "MSS", None) or _mss.mss  # type: ignore[union-attr]
    with factory() as sct:
        shot = sct.grab({"left": int(x), "top": int(y),
                         "width": int(width), "height": int(height)})
        return Grab((int(x), int(y)), shot.width, shot.height, bytes(shot.bgra))


def capture(region: tuple[int, int, int, int] | winapi.Rect | None = None,
            backend: str = "auto") -> Grab:
    """截取屏幕。

    ``region`` 为 ``(x, y, width, height)``（物理像素）；省略则截整个主屏。
    ``backend`` 取 ``auto`` / ``gdi`` / ``mss``。
    """
    winapi.enable_dpi_awareness()
    if region is None:
        pw, ph = winapi.primary_screen_size()
        x, y, width, height = 0, 0, pw, ph
    elif isinstance(region, winapi.Rect):
        x, y, width, height = region.left, region.top, region.width, region.height
    else:
        x, y, width, height = (int(v) for v in region)

    if backend == "auto":
        backend = "mss" if HAS_MSS else "gdi"
    if backend == "mss":
        if not HAS_MSS:
            raise RuntimeError("未安装 mss：pip install mss")
        return _mss_grab(x, y, width, height)
    return _gdi_grab(x, y, width, height)


def wait_stable(region: tuple[int, int, int, int] | None = None,
                timeout: float = 5.0, interval: float = 0.25,
                threshold: float = 0.005, settle: int = 2) -> Grab:
    """等待画面稳定（连续 ``settle`` 次变化率低于阈值），返回最后一帧。"""
    deadline = time.time() + timeout
    prev = capture(region)
    stable = 0
    while time.time() < deadline:
        time.sleep(interval)
        cur = capture(region)
        if prev.diff_ratio(cur) <= threshold:
            stable += 1
            if stable >= settle:
                return cur
        else:
            stable = 0
        prev = cur
    return prev


def self_check() -> dict[str, Any]:
    """截屏子系统自检。"""
    t0 = time.time()
    shot = capture()
    dt = (time.time() - t0) * 1000
    png = shot.to_png_bytes()
    return {
        "backend": "mss" if HAS_MSS else "gdi",
        "pillow": HAS_PIL,
        "size": list(shot.size),
        "origin": list(shot.origin),
        "grab_ms": round(dt, 1),
        "png_bytes": len(png),
        "fingerprint": shot.fingerprint(),
        "center_pixel_rgb": list(shot.pixel(shot.width // 2, shot.height // 2)),
    }
