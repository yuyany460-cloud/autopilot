# -*- coding: utf-8 -*-
"""扩展桥测试：用真 socket 跑一遍握手 + 命令往返。

没装扩展的环境里也要能测，所以这里自己实现一个最小的 WebSocket 客户端
（客户端发的帧必须带掩码，服务端的不带），冒充浏览器里的扩展。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time

import pytest

from autopilot import extbridge
from autopilot.extbridge import _Conn


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeExtension:
    """冒充浏览器扩展：连上桥，按脚本应答命令。

    注意顺序：**必须先握手再起读线程**。两者共用同一个 socket，
    读线程会抢先把 HTTP 响应当成 WebSocket 帧吃掉（这就是第一版测试自己踩的坑）。
    """

    def __init__(self, port: int, reply=None, handshake: bool = True) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.sock.settimeout(10)
        self.conn = _Conn(self.sock)
        self.received: list[dict] = []
        self.reply = reply or (lambda op, params: {"ok": True, "result": f"做了 {op}"})
        self._stop = threading.Event()
        self.mute = False            # 只收不回（测超时用）
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.head = self.handshake() if handshake else ""

    def handshake(self) -> str:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self.sock.sendall(
            (f"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
             f"Sec-WebSocket-Version: 13\r\n\r\n").encode("ascii"))
        head = self.conn.read_until(b"\r\n\r\n").decode("latin-1")
        self.key = key
        return head

    def send(self, obj: dict) -> None:
        """客户端帧必须带掩码。"""
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        mask = os.urandom(4)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
        n = len(payload)
        header = bytes([0x81, 0x80 | n]) if n < 126 else \
            bytes([0x81, 0x80 | 126]) + n.to_bytes(2, "big")
        self.sock.sendall(header + mask + masked)

    def send_raw(self, data: bytes) -> None:
        """把原始字节直接写进去（用来测"握手和第一帧挤在同一个包里"）。"""
        self.sock.sendall(data)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                opcode, payload = self.conn.read_frame()
            except Exception:
                return
            if opcode != 0x1:
                continue
            msg = json.loads(payload.decode("utf-8"))
            self.received.append(msg)
            if self.mute:
                continue
            out = dict(self.reply(msg.get("op"), msg.get("params") or {}))
            out["id"] = msg.get("id")
            self.send(out)

    def start(self) -> "FakeExtension":
        self.thread.start()
        return self

    def close(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


def wait_connected(bridge, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if bridge.connected:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def bridge():
    port = free_port()
    b = extbridge.ExtensionBridge(port=port)
    assert b.start(), "桥启动失败"
    # 等accept线程真正跑起来
    time.sleep(0.2)
    yield b
    b.stop()


# --- 握手 ---------------------------------------------------------------


def test_handshake_returns_correct_accept(bridge) -> None:
    import hashlib

    ext = FakeExtension(bridge.port)
    assert "101" in ext.head, repr(ext.head)
    expect = base64.b64encode(
        hashlib.sha1((ext.key + extbridge.GUID).encode()).digest()).decode()
    assert expect in ext.head, "Sec-WebSocket-Accept 按 RFC 6455 算错了"
    assert "permessage-deflate" not in ext.head, "不该启用压缩扩展"
    ext.close()


def test_handshake_tolerates_frame_in_same_packet(bridge) -> None:
    """握手和第一条帧挤在同一个 TCP 段里时，第一条帧不能丢。

    这是个很容易踩的坑：握手用 recv(4096) 读，多读到的字节如果丢掉，
    表现就是"扩展明明连上了，但第一条命令永远没响应"。
    """
    ext = FakeExtension(bridge.port, handshake=False)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    head = (f"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n")
    payload = json.dumps({"op": "__hello", "params": {"version": "9.9"}}).encode()
    mask = os.urandom(4)
    masked = bytes(payload[i] ^ mask[i % 4] for i in range(len(payload)))
    frame = bytes([0x81, 0x80 | len(payload)]) + mask + masked
    ext.send_raw(head.encode() + frame)      # 一次写出去

    for _ in range(60):
        if bridge.hello:
            break
        time.sleep(0.05)
    assert bridge.hello.get("version") == "9.9", "同一包里的第一帧被吃掉了"
    ext.close()


# --- 命令往返 -----------------------------------------------------------


def test_call_round_trip(bridge) -> None:
    ext = FakeExtension(bridge.port).start()
    assert wait_connected(bridge)

    result = bridge.call("tabs", {"x": 1})
    assert result == "做了 tabs"
    assert ext.received[0]["op"] == "tabs"
    assert ext.received[0]["params"] == {"x": 1}
    ext.close()


def test_call_reports_extension_error(bridge) -> None:
    ext = FakeExtension(bridge.port, reply=lambda op, p: {"ok": False,
                                                          "error": "页面上找不到「登录」"}).start()
    assert wait_connected(bridge)
    with pytest.raises(extbridge.BridgeError, match="找不到"):
        bridge.call("click", {"target": "登录"})
    ext.close()


def test_call_without_extension_raises(bridge) -> None:
    with pytest.raises(extbridge.BridgeError, match="没有连接"):
        bridge.call("tabs")


def test_call_times_out(bridge) -> None:
    """扩展不回应时要超时，不能永远挂住 Agent。"""
    ext = FakeExtension(bridge.port).start()
    assert wait_connected(bridge)
    ext.mute = True                       # 收下命令但不回应，模拟卡死
    t0 = time.perf_counter()
    with pytest.raises(extbridge.BridgeError, match="超时"):
        bridge.call("tabs", timeout=0.5)
    assert time.perf_counter() - t0 < 3
    ext.close()


def test_idle_connection_survives(bridge) -> None:
    """空闲连接不能被误判成断线。

    监听 socket 上有 1 秒超时，accept 出来的连接会继承它；而 socket.timeout
    是 OSError 的子类，会在读帧那层被当成"连接已关闭"。
    扩展的心跳是 20 秒一次，中了这个坑连接根本活不下来。
    """
    ext = FakeExtension(bridge.port).start()
    assert wait_connected(bridge)
    time.sleep(2.5)                       # 远超过监听 socket 的 1 秒超时
    assert bridge.connected, "空闲 2.5 秒后连接不该断"
    assert bridge.call("tabs") == "做了 tabs"
    ext.close()


# --- 连接管理 -----------------------------------------------------------


def test_new_connection_replaces_old(bridge) -> None:
    """浏览器重连后旧连接作废，命令必须发到新的那条上。"""
    a = FakeExtension(bridge.port, reply=lambda op, p: {"ok": True, "result": "旧"}).start()
    assert wait_connected(bridge)

    b = FakeExtension(bridge.port, reply=lambda op, p: {"ok": True, "result": "新"}).start()
    time.sleep(0.4)
    assert bridge.call("tabs") == "新"
    a.close()
    b.close()


def test_disconnect_fails_pending(bridge) -> None:
    """扩展中途断线时，等待中的命令要立刻失败，而不是干等到超时。"""
    ext = FakeExtension(bridge.port, reply=lambda op, p: {}).start()
    assert wait_connected(bridge)

    box: dict = {}

    def waiter():
        try:
            bridge.call("tabs", timeout=10)
        except Exception as exc:
            box["err"] = str(exc)

    t = threading.Thread(target=waiter, daemon=True)
    t.start()
    time.sleep(0.3)
    ext.close()
    t.join(timeout=5)
    assert "err" in box, "断线后等待中的命令应该立刻失败"
    assert not t.is_alive()


def test_status_reports_listening(bridge) -> None:
    st = bridge.status()
    assert st["listening"] is True
    assert st["port"] == bridge.port
    assert st["connected"] is False
