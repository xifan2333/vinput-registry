#!/usr/bin/env python3
"""讯飞输入法（iFlytek IME）流式语音识别 provider，遵循 Vinput provider 契约。

协议来源：J3n5en/voicekey 仓库 crates/core/src/ifly.rs（Rust 实现），移植到
Vinput 的 stdin/stdout JSONL 契约（基础设施照抄本机 doubaoime/streaming 模板）。

协议要点：
- 端点 wss://100ime-iat-api.xfyun.cn/v2/iat，讯飞输入法 App 内置的 IAT 通道
- 鉴权：URL 携带 HMAC-SHA256 签名（host/date/request-line），凭证为 App 内嵌的
  "100IME" 应用参数，与用户账号无关，无需登录、无需设备注册
- 音频：16 kHz 单声道 S16LE 裸 PCM，base64 后按 1280 字节（40ms）一帧走 JSON 文本帧
- 流式结果：dwa=wpgs 渐进式返回，pgs=rpl 区间替换，按 sn 有序合并
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import email.utils
import hashlib
import hmac
import json
import os
import secrets
import signal
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

HOST = "100ime-iat-api.xfyun.cn"
PATH = "/v2/iat"
APP_ID = "100IME"
API_KEY = "15461402826c8360ebcf1270cbba88e5"
API_SECRET = "sH8v2wSSxxHTy1UNfN6dFaSqVjlmR7BB"
CHUNK_BYTES = 1280  # 16 kHz 单声道 S16LE 的 40 ms
AUDIO_FORMAT = "audio/L16;rate=16000"

BUSINESS = {
    "language": "zh_cn",
    "domain": "iat",
    "accent": "mandarin",
    "vad_eos": 3000,
    "dwa": "wpgs",
    "nunum": 1,
    "ptt": 1,
}


def log(message: str) -> None:
    """本地排障用。daemon 会把 provider 的每一行 stderr 当桌面通知弹给用户，
    所以默认必须静默，只有显式设置 VINPUT_ASR_DEBUG 时才输出。"""
    if os.getenv("VINPUT_ASR_DEBUG"):
        print(f"[iflyime] {message}", file=sys.stderr, flush=True)


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: float) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise ValueError("WebSocket URL must use ws:// or wss://.")
        if not parsed.hostname:
            raise ValueError("WebSocket URL is missing a hostname.")

        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.scheme = parsed.scheme
        self.timeout = timeout
        self.headers = headers
        self.response_headers: dict[str, str] = {}
        self._recv_buffer = b""
        self._closed = False
        self._send_lock = threading.Lock()
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self.timeout
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
        # IPv4 优先 + 单地址短超时：某条路由（如 IPv6/代理瞬断）不可达时
        # 立即换下一条，避免把整个连接超时耗完而被 daemon 判定失败
        addresses.sort(key=lambda info: info[0] == socket.AF_INET6)

        last_error: Exception | None = None
        for family, socktype, proto, _, address in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            raw_sock = socket.socket(family, socktype, proto)
            try:
                raw_sock.settimeout(min(2.0, remaining))
                raw_sock.connect(address)
                raw_sock.settimeout(min(3.0, remaining))
                if self.scheme == "wss":
                    context = ssl.create_default_context()
                    sock = context.wrap_socket(raw_sock, server_hostname=self.host)
                else:
                    sock = raw_sock

                key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
                lines = [
                    f"GET {self.path} HTTP/1.1",
                    f"Host: {self.host}:{self.port}",
                    "Upgrade: websocket",
                    "Connection: Upgrade",
                    f"Sec-WebSocket-Key: {key}",
                    "Sec-WebSocket-Version: 13",
                ]
                for name, value in self.headers.items():
                    lines.append(f"{name}: {value}")
                sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))

                response, overflow = self._read_http_response(sock)
                self._recv_buffer = overflow
                self._validate_handshake(response, key)
                return sock
            except OSError as error:
                last_error = error
                try:
                    raw_sock.close()
                except OSError:
                    pass
        raise RuntimeError(f"无法连接 {self.host}:{self.port}: {last_error}")

    def _read_http_response(self, sock: socket.socket) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed: empty response.")
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("WebSocket handshake failed: response too large.")

        response, overflow = bytes(data).split(b"\r\n\r\n", 1)
        return response, overflow

    def _validate_handshake(self, response: bytes, key: str) -> None:
        header_blob = response.decode("utf-8", errors="replace")
        lines = header_blob.split("\r\n")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        if not lines or "101" not in lines[0]:
            status_line = lines[0] if lines else "invalid response"
            details = [f"WebSocket handshake failed: {status_line}"]
            if " 401" in f" {status_line} ":
                details.append("鉴权被拒绝（检查系统时间是否准确）")
            raise RuntimeError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode("ascii")
        if accept != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")
        self.response_headers = headers

    def close(self):
        self._closed = True
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            raise ConnectionError("WebSocket connection is closed")

        first = 0x80 | (opcode & 0x0F)
        mask_key = secrets.token_bytes(4)
        length = len(payload)

        header = bytearray([first])
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))

        masked = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))
        with self._send_lock:
            self.socket.sendall(bytes(header) + mask_key + masked)

    def send_text(self, payload: str) -> None:
        self._send_frame(0x1, payload.encode("utf-8"))

    def recv_message(self) -> tuple[int, bytes] | None:
        fragments = bytearray()
        current_opcode: int | None = None

        while True:
            frame = self._recv_frame()
            if frame is None:
                return None

            opcode, payload, fin = frame
            if opcode == 0x8:
                self._closed = True
                return None
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode not in {0x0, 0x1, 0x2}:
                continue

            if opcode in {0x1, 0x2}:
                current_opcode = opcode
                fragments = bytearray(payload)
            else:
                if current_opcode is None:
                    continue
                fragments.extend(payload)

            if len(fragments) > 4 * 1024 * 1024:
                raise ValueError("WebSocket message exceeds 4 MB")
            if not fin:
                continue

            if current_opcode is None:
                continue
            return current_opcode, bytes(fragments)

    def _recv_frame(self) -> tuple[int, bytes, bool] | None:
        header = self._recv_exact(2)
        if header is None:
            return None

        first, second = header
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            raw_length = self._recv_exact(2)
            if raw_length is None:
                return None
            length = struct.unpack("!H", raw_length)[0]
        elif length == 127:
            raw_length = self._recv_exact(8)
            if raw_length is None:
                return None
            length = struct.unpack("!Q", raw_length)[0]

        if length > 4 * 1024 * 1024:
            raise ValueError("WebSocket frame exceeds 4 MB")

        mask_key = b""
        if masked:
            mask_key = self._recv_exact(4)
            if mask_key is None:
                return None

        payload = self._recv_exact(length)
        if payload is None:
            return None

        if masked:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))

        return opcode, payload, fin

    def _recv_exact(self, size: int) -> bytes | None:
        while len(self._recv_buffer) < size:
            chunk = self.socket.recv(4096)
            if not chunk:
                if not self._recv_buffer and size > 0:
                    return None
                raise RuntimeError("WebSocket connection closed unexpectedly.")
            self._recv_buffer += chunk

        data = self._recv_buffer[:size]
        self._recv_buffer = self._recv_buffer[size:]
        return data


async def background(function, *args):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def complete(value=None, error=None):
        if not future.done():
            if error is None:
                future.set_result(value)
            else:
                future.set_exception(error)

    def work():
        try:
            value = function(*args)
        except BaseException as error:
            try:
                loop.call_soon_threadsafe(complete, None, error)
            except RuntimeError:
                pass
        else:
            try:
                loop.call_soon_threadsafe(complete, value)
            except RuntimeError:
                pass

    threading.Thread(target=work, daemon=True).start()
    return await future


@asynccontextmanager
async def connect_ws(url: str, headers: dict[str, str], timeout: float):
    client = None
    abandoned = threading.Event()

    def open_connection():
        nonlocal client
        client = WebSocketClient(url, headers, timeout)
        client.socket.settimeout(None)
        if abandoned.is_set():
            client.close()
            raise ConnectionError("Connection cancelled")
        return client

    class Connection:
        def __init__(self, connected: WebSocketClient):
            self.client = connected

        async def send(self, data: str):
            await background(self.client.send_text, data)

        async def recv(self) -> bytes:
            message = await background(self.client.recv_message)
            if message is None:
                raise ConnectionError("WebSocket closed before final result")
            opcode, data = message
            if opcode not in {0x1, 0x2}:
                raise ValueError("Expected a text WebSocket message")
            return data

    try:
        connected = await background(open_connection)
        yield Connection(connected)
    finally:
        abandoned.set()
        if client is not None:
            client.close()


def auth_url() -> str:
    date = email.utils.formatdate(usegmt=True)
    origin = f"host: {HOST}\ndate: {date}\nGET {PATH} HTTP/1.1"
    signature = base64.b64encode(
        hmac.new(API_SECRET.encode("utf-8"), origin.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    authorization = base64.b64encode(
        f'api_key="{API_KEY}", algorithm="hmac-sha256", headers="host date request-line", '
        f'signature="{signature}"'.encode()
    ).decode("ascii")
    return f"wss://{HOST}{PATH}?authorization={quote(authorization, safe='')}&date={quote(date, safe='')}&host={HOST}"


class PgsTranscript:
    """按 sn 有序合并 wpgs 渐进式结果；pgs=rpl 时用 rg 区间替换旧段。"""

    def __init__(self):
        self.segments: dict[int, str] = {}

    def apply(self, result: dict[str, Any]) -> str:
        sn = int(result.get("sn") or 0)
        words = "".join(chunk.get("w", "") for group in result.get("ws") or [] for chunk in (group.get("cw") or [])[:1])
        if result.get("pgs") == "rpl":
            rg = result.get("rg") or []
            if len(rg) == 2:
                low, high = int(rg[0]), int(rg[1])
                self.segments = {k: v for k, v in self.segments.items() if not low <= k <= high}
        self.segments[sn] = words
        return "".join(self.segments[key] for key in sorted(self.segments))


class IatError(RuntimeError):
    pass


class IatClient:
    def __init__(self, open_timeout: float, response_timeout: float):
        self.open_timeout = open_timeout
        self.response_timeout = response_timeout

    @staticmethod
    def endpoint() -> str:
        # 仅测试用：允许覆盖端点以模拟断网（默认走真实鉴权 URL）
        return os.getenv("VINPUT_ASR_ENDPOINT") or auth_url()

    def _build_frame(self, audio: bytes, status: int, include_business: bool) -> str:
        frame: dict[str, Any] = {
            "data": {
                "status": status,
                "format": AUDIO_FORMAT,
                "encoding": "raw",
                "audio": base64.b64encode(audio).decode("ascii"),
            }
        }
        if include_business:
            frame["common"] = {"app_id": APP_ID}
            frame["business"] = BUSINESS
        return json.dumps(frame, separators=(",", ":"))

    async def transcribe_stream(
        self,
        source: AsyncIterable[bytes],
        on_update: Callable[[str], None] | None = None,
        on_start: Callable[[], None] | None = None,
    ) -> str:
        transcript = PgsTranscript()
        state = {"text": ""}
        began = time.monotonic()

        async with connect_ws(self.endpoint(), {}, self.open_timeout) as ws:
            log("connected to " + HOST)

            async def receiver() -> None:
                while True:
                    raw = await ws.recv()
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as error:
                        raise ValueError(f"讯飞返回了无法解析的消息: {error}") from error
                    code = payload.get("code", -1)
                    if code != 0:
                        raise IatError(f"讯飞错误 code={code}: {payload.get('message') or '未知错误'}")
                    data = payload.get("data") or {}
                    result = data.get("result")
                    if result:
                        text = transcript.apply(result)
                        if text and text != state["text"]:
                            state["text"] = text
                            if on_update:
                                on_update(text)
                    if data.get("status") == 2:
                        log(f"识别定稿: segs={len(transcript.segments)} 用时{time.monotonic() - began:.2f}s")
                        return

            async def sender() -> None:
                pending = bytearray()
                first = True
                frames = 0
                total = 0

                async for chunk in source:
                    pending.extend(chunk)
                    while len(pending) >= CHUNK_BYTES:
                        frame = bytes(pending[:CHUNK_BYTES])
                        del pending[:CHUNK_BYTES]
                        await ws.send(self._build_frame(frame, 0 if first else 1, first))
                        first = False
                        frames += 1
                        total += len(frame)
                if pending:
                    await ws.send(self._build_frame(bytes(pending), 0 if first else 1, first))
                    first = False
                    frames += 1
                    total += len(pending)
                await ws.send(self._build_frame(b"", 2, first))
                log(f"上传完成: {frames}帧 {total}字节 {total / 32000:.2f}秒")

            if on_start is not None:
                on_start()
            receiver_task = asyncio.create_task(receiver())
            sender_task = asyncio.create_task(sender())
            try:
                while True:
                    if sender_task.done():
                        await sender_task
                        await asyncio.wait_for(receiver_task, timeout=self.response_timeout)
                        break
                    done, _ = await asyncio.wait({sender_task, receiver_task}, return_when=asyncio.FIRST_COMPLETED)
                    if receiver_task in done:
                        # 服务端提前结束（例如 vad_eos 静音判定），余下音频不再上传
                        sender_task.cancel()
                        await asyncio.gather(sender_task, return_exceptions=True)
                        await receiver_task
                        break
            finally:
                for task in (receiver_task, sender_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(receiver_task, sender_task, return_exceptions=True)
            return state["text"]


MAX_BUFFER_BYTES = 320000


class AudioInput:
    def __init__(self):
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=1001)
        self.buffered = 0
        self.finish_deadline: float | None = None
        self.cancelled = False
        self.changed = asyncio.Event()

    def finish(self, grace: float):
        if self.finish_deadline is None:
            self.finish_deadline = asyncio.get_running_loop().time() + grace
            self.queue.put_nowait(None)
            self.changed.set()

    async def read(self, lines: AsyncIterable[bytes], grace: float):
        async for line in lines:
            if not line.strip():
                continue
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("Provider input must be a JSON object")
            kind = message.get("type")
            if kind == "cancel":
                self.cancelled = True
                self.changed.set()
                return
            if kind == "finish":
                self.finish(grace)
            elif kind == "audio":
                if self.finish_deadline is not None:
                    raise ValueError("Audio received after finish")
                audio = base64.b64decode(message["audio_base64"], validate=True)
                if len(audio) % 2:
                    raise ValueError("PCM must contain complete 16-bit samples")
                if audio:
                    if self.buffered + len(audio) > MAX_BUFFER_BYTES or self.queue.qsize() >= 1000:
                        raise ValueError("Audio buffer exceeded 10 seconds while the provider was unavailable")
                    self.buffered += len(audio)
                    self.queue.put_nowait(audio)
                if message.get("commit") is True:
                    self.finish(grace)
            else:
                raise ValueError(f"Unsupported provider input: {kind}")
        self.finish(grace)

    async def chunks(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            self.buffered -= len(item)
            yield item


async def run_provider(
    lines: AsyncIterable[bytes], emit: Callable[[dict], None], *, finish_grace=5.0, open_timeout=15.0
):
    if not 0 < finish_grace <= 9:
        raise ValueError("Finish grace must be greater than zero and at most 9 seconds")
    audio = AudioInput()

    def partial(text: str):
        if text:
            emit({"type": "partial", "text": text})

    async def recognize():
        client = IatClient(open_timeout=open_timeout, response_timeout=finish_grace)
        return await client.transcribe_stream(
            audio.chunks(), partial, on_start=lambda: emit({"type": "session_started", "session_id": str(uuid.uuid4())})
        )

    reader = asyncio.create_task(audio.read(lines, finish_grace))
    recognition = asyncio.create_task(recognize())
    changed = asyncio.create_task(audio.changed.wait())
    try:
        while True:
            tasks: set[asyncio.Task[Any]] = {recognition}
            if not reader.done():
                tasks.add(reader)
            if not changed.done():
                tasks.add(changed)
            timeout = (
                None if audio.finish_deadline is None else audio.finish_deadline - asyncio.get_running_loop().time()
            )
            if timeout is not None and timeout <= 0:
                raise TimeoutError("Timed out waiting for the final transcript")
            done, _ = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise TimeoutError("Timed out waiting for the final transcript")
            if reader.done():
                await reader
            if audio.cancelled:
                return 0
            if recognition in done:
                text = await recognition
                if not text:
                    raise ValueError("No speech was recognized")
                emit({"type": "final", "text": text})
                return 0
    except asyncio.CancelledError:
        raise
    except Exception as error:
        emit({"type": "error", "message": f"{type(error).__name__}: {error}"})
        return 1
    finally:
        for task in (reader, recognition, changed):
            task.cancel()
        await asyncio.gather(reader, recognition, changed, return_exceptions=True)
        emit({"type": "closed"})


async def stdin_lines():
    reader = asyncio.StreamReader(limit=1_000_000)
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    try:
        while line := await reader.readline():
            yield line
    finally:
        transport.close()


def main():
    parser = argparse.ArgumentParser(description="iFlytek IME (讯飞输入法) streaming provider")
    parser.add_argument("--finish-grace", type=float, default=float(os.getenv("VINPUT_ASR_FINISH_GRACE_SECS") or "5"))
    parser.add_argument(
        "--timeout",
        type=float,
        default=float(os.getenv("VINPUT_ASR_TIMEOUT") or "15"),
        help="WebSocket 连接超时秒数",
    )
    args = parser.parse_args()
    if not 0 < args.finish_grace <= 9:
        parser.error("finish grace must be greater than zero and at most 9 seconds")
    if not 0 < args.timeout <= 60:
        parser.error("timeout must be greater than zero and at most 60 seconds")

    async def run():
        task = asyncio.current_task()
        assert task is not None
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
        return await run_provider(
            stdin_lines(),
            lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
            finish_grace=args.finish_grace,
            open_timeout=args.timeout,
        )

    try:
        raise SystemExit(asyncio.run(run()))
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
