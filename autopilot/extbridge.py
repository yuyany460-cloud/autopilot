# -*- coding: utf-8 -*-
"""浏览器扩展桥：一个用标准库手写的极小 WebSocket 服务，让浏览器里的扩展连上来。

**为什么需要它**：CDP 调试端口只能在浏览器*启动那一刻*打开，而 Edge 136+ 又禁止
对默认配置目录开这个端口（实测 Edge 155 显式传和隐式用都被拒绝——浏览器起来了
11 个进程，端口始终不开）。所以"接管用户**已经打开**的那个浏览器"只剩一条路：
装扩展。扩展跑在浏览器内部，用 ``chrome.debugger`` 就能拿到完整 CDP 能力，
不用重启、不用复制配置、不用重新登录。

**为什么自己写 WebSocket**：环境里没有 ``websockets`` / ``wsproto``；而 MV3 的
service worker 会被 30 秒空闲回收，只有 WebSocket 的收发能把它的存活计时器
一直顶回去（Chrome 116+ 起），长轮询在这个位置不可靠。协议本身只用到极小的
一个子集，自己实现比拉一个依赖干净。

**协议**（JSON 文本帧）::

    下发  {"id": 1, "op": "click", "params": {...}}
    回传  {"id": 1, "ok": true, "result": {...}}
          {"id": 1, "ok": false, "error": "..."}

同一时刻只保留**最新**的那条连接：浏览器重连时旧连接直接作废，
免得命令发到一个已经死掉的标签页上。
"""

from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import threading
import time
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DEFAULT_PORT = 8849
# 单条命令的默认超时。截图、导航这类会慢一些，调用方可以自己传。
DEFAULT_TIMEOUT = 30.0

# 扩展那边连不上时写进来的诊断信息（走普通 HTTP GET）
_DIAG: list[dict[str, Any]] = []
_DIAG_LOCK = threading.Lock()


class BridgeError(RuntimeError):
    """桥接不可用，或者扩展那边执行失败。"""


# --------------------------------------------------------------------------
# WebSocket 帧（只实现 RFC 6455 里必需的那部分）
# --------------------------------------------------------------------------


class _Conn:
    """带缓冲的帧读写。

    必须带缓冲：握手是 ``recv(4096)`` 读的，如果客户端把第一条帧和握手放在
    同一个 TCP 段里发过来，多读到的字节不能丢——丢了就是"扩展连上了但第一
    条命令永远没响应"这种极难查的问题。
    """

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""

    def take(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(max(4096, n - len(self.buf)))
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def read_until(self, marker: bytes, limit: int = 65536) -> bytes:
        while marker not in self.buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("连接已关闭")
            self.buf += chunk
            if len(self.buf) > limit:
                raise ConnectionError("请求头过大")
        head, self.buf = self.buf.split(marker, 1)
        return head

    def read_frame(self) -> tuple[int, bytes]:
        b0, b1 = self.take(2)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.take(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.take(8))[0]
        mask = self.take(4) if masked else b""
        payload = self.take(length) if length else b""
        if masked:
            payload = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        return opcode, payload

    def send_frame(self, payload: bytes, opcode: int = 1) -> None:
        """服务端发出的帧按协议**不加掩码**。"""
        n = len(payload)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack(">H", n)
        else:
            header += bytes([127]) + struct.pack(">Q", n)
        self.sock.sendall(header + payload)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def handshake(conn: _Conn) -> dict[str, str]:
    """完成 HTTP Upgrade，返回请求头。

    顺便当一次普通 HTTP 服务器：没有 ``Sec-WebSocket-Key`` 的请求按普通 GET 处理，
    回一小段 JSON 状态。扩展那边连不上时可以把原因写到这里，方便排查
    （也能顺手验证"扩展到底能不能访问这个端口"）。
    """
    head = conn.read_until(b"\r\n\r\n").decode("latin-1")
    lines = head.split("\r\n")
    request = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()

    key = headers.get("sec-websocket-key")
    if not key:
        from urllib.parse import parse_qs, unquote, urlparse

        parsed = urlparse(request.split(" ")[1] if " " in request else "/")
        note = (parse_qs(parsed.query).get("note") or [""])[0]
        if note:
            with _DIAG_LOCK:
                _DIAG.append({"t": time.time(), "note": unquote(note)[:500]})
        body = json.dumps({"ok": True, "websocket": False,
                           "diagnostics": list(_DIAG[-20:])},
                          ensure_ascii=False).encode("utf-8")
        conn.sock.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json; charset=utf-8\r\n"
            b"Access-Control-Allow-Origin: *\r\n"
            + f"Content-Length: {len(body)}\r\n".encode()
            + b"Connection: close\r\n\r\n" + body)
        raise ConnectionError("普通 HTTP 请求，已应答")

    accept = base64.b64encode(
        hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
    # 刻意不回 Sec-WebSocket-Extensions，这样浏览器不会启用 permessage-deflate
    conn.sock.sendall(
        ("HTTP/1.1 101 Switching Protocols\r\n"
         "Upgrade: websocket\r\n"
         "Connection: Upgrade\r\n"
         f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode("ascii"))
    return headers


# --------------------------------------------------------------------------
# 桥
# --------------------------------------------------------------------------


class ExtensionBridge:
    """监听本地端口，接收浏览器扩展连入，并支持下发命令。

    线程安全：``call`` 可以被多个线程同时调用，每条命令用独立 id 关联应答。
    """

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT) -> None:
        self.host = host
        self.port = int(port)
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._client: socket.socket | None = None
        self._client_lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 0
        self.hello: dict[str, Any] = {}
        self.connected_at: float = 0.0
        self.last_error = ""

    # -- 生命周期 --------------------------------------------------------
    def start(self) -> bool:
        """开始监听。端口被占用（比如另一个 AutoPilot 在跑）时返回 False。"""
        if self._server is not None:
            return True
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(4)
            srv.settimeout(1.0)
        except OSError as exc:
            self.last_error = f"端口 {self.port} 无法监听：{exc}"
            return False
        self._server = srv
        self._thread = threading.Thread(target=self._accept_loop, name="autopilot-ext-bridge",
                                        daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    @property
    def connected(self) -> bool:
        with self._client_lock:
            return self._client is not None

    def status(self) -> dict[str, Any]:
        with _DIAG_LOCK:
            diag = list(_DIAG[-10:])
        return {
            "listening": self._server is not None,
            "connected": self.connected,
            "port": self.port,
            "connected_for": round(time.time() - self.connected_at, 1) if self.connected else 0,
            "hello": dict(self.hello),
            "last_error": self.last_error,
            "diagnostics": diag,
        }

    # -- 连接处理 --------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._server is not None
        while self._server is not None:
            try:
                conn, _addr = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            # 必须清掉超时：监听 socket 上的 1 秒超时会被 accept 出来的连接继承，
            # 而 socket.timeout 是 OSError 的子类，会在读帧那层被当成"断线"处理，
            # 结果就是连接空闲 1 秒就自己断开（扩展心跳是 20 秒，根本活不下来）。
            conn.settimeout(None)
            threading.Thread(target=self._serve, args=(conn,),
                             name="autopilot-ext-conn", daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        conn = _Conn(sock)
        try:
            handshake(conn)
        except Exception as exc:
            self.last_error = f"握手失败：{exc}"
            conn.close()
            return

        # 新连接顶掉旧连接：浏览器重连后旧的那条已经没有意义
        with self._client_lock:
            if self._client is not None:
                self._client.close()
            self._client = conn
            self.connected_at = time.time()
            self.hello = {}
            self.last_error = ""
        self._fail_pending("浏览器重新连接，之前的命令作废")

        try:
            while True:
                opcode, payload = conn.read_frame()
                if opcode == 0x8:                       # close
                    break
                if opcode == 0x9:                       # ping
                    conn.send_frame(payload, opcode=0xA)
                    continue
                if opcode == 0xA:                       # pong
                    continue
                if opcode not in (0x0, 0x1, 0x2):
                    continue
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                self._dispatch(msg)
        except (ConnectionError, OSError):
            pass
        finally:
            with self._client_lock:
                if self._client is conn:
                    self._client = None
            conn.close()
            self._fail_pending("浏览器连接已断开")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        op = msg.get("op")
        if op == "__hello":
            self.hello = msg.get("params") or {}
            return
        if op == "__pong":                              # 扩展的保活应答
            return
        rid = msg.get("id")
        if rid is None:
            return
        with self._pending_lock:
            slot = self._pending.pop(rid, None)
        if slot is not None:
            slot["data"] = msg
            slot["event"].set()

    def _fail_pending(self, reason: str) -> None:
        with self._pending_lock:
            slots = list(self._pending.values())
            self._pending.clear()
        for slot in slots:
            slot["data"] = {"ok": False, "error": reason}
            slot["event"].set()

    # -- 下发命令 --------------------------------------------------------
    def call(self, op: str, params: dict[str, Any] | None = None,
             timeout: float = DEFAULT_TIMEOUT) -> Any:
        """下发一条命令并等结果；失败抛 :class:`BridgeError`。"""
        with self._client_lock:
            conn = self._client
        if conn is None:
            raise BridgeError("浏览器扩展没有连接")

        with self._pending_lock:
            self._next_id += 1
            rid = self._next_id
            slot: dict[str, Any] = {"event": threading.Event(), "data": None}
            self._pending[rid] = slot

        payload = json.dumps({"id": rid, "op": op, "params": params or {}},
                             ensure_ascii=False).encode("utf-8")
        try:
            with self._client_lock:
                if self._client is not conn:
                    raise BridgeError("浏览器已断开")
                conn.send_frame(payload)
        except OSError as exc:
            with self._pending_lock:
                self._pending.pop(rid, None)
            raise BridgeError(f"发送失败：{exc}") from exc

        if not slot["event"].wait(timeout):
            with self._pending_lock:
                self._pending.pop(rid, None)
            # service worker 被回收时，socket 常常是"半开"的——对端进程没了但
            # 没有 FIN，于是 connected 仍然是 True，命令却永远没回应。
            # 所以超时就直接把这条连接判死，别让后续命令继续干等。
            with self._client_lock:
                if self._client is conn:
                    try:
                        conn.close()
                    except OSError:
                        pass
                    self._client = None
            self.last_error = f"扩展执行 {op} 超时，已断开该连接"
            raise BridgeError(f"扩展执行 {op} 超时（{timeout:.0f}s），连接已重置")

        data = slot["data"] or {}
        if not data.get("ok", False):
            raise BridgeError(str(data.get("error") or f"{op} 执行失败"))
        return data.get("result")


_BRIDGE: ExtensionBridge | None = None
_BRIDGE_LOCK = threading.Lock()


def bridge(port: int | None = None) -> ExtensionBridge:
    """进程级共享的桥。首次调用时开始监听（失败也不抛，只是连不上）。"""
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            import os

            want = port if port is not None else int(os.environ.get("AUTOPILOT_EXT_PORT")
                                                    or DEFAULT_PORT)
            _BRIDGE = ExtensionBridge(port=want)
            _BRIDGE.start()
        return _BRIDGE


def connected() -> bool:
    return _BRIDGE is not None and _BRIDGE.connected


def wait_ready(timeout: float = 3.0) -> bool:
    """等扩展连上来（它可能在 service worker 被回收后正重连）。

    有这个等待很关键：扩展只是"睡了一下"的时候，如果直接判定为"没扩展"，
    调用方就会退回"另开一个浏览器"——用户明明装了扩展、浏览器也开着，
    却看到又弹出一个新窗口，完全莫名其妙。
    """
    if connected():
        return True
    if _BRIDGE is None:
        return False
    deadline = time.time() + max(0.0, timeout)
    while time.time() < deadline:
        if _BRIDGE.connected:
            return True
        time.sleep(0.2)
    return _BRIDGE.connected


def reset() -> None:
    """测试用：停掉并丢掉单例。"""
    global _BRIDGE
    with _BRIDGE_LOCK:
        if _BRIDGE is not None:
            _BRIDGE.stop()
        _BRIDGE = None
