#!/usr/bin/env python3
"""iFlytek IME (讯飞输入法) duplex streaming speech-to-text provider via WebSocket protocol."""

from __future__ import annotations

import base64
import email.utils
import hashlib
import hmac
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlparse

DEFAULT_TIMEOUT = 15.0
DEFAULT_FINISH_GRACE_SECS = 5.0
MAX_FINISH_GRACE_SECS = 9.0
MAX_WEBSOCKET_MESSAGE_BYTES = 4 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

HOST = "100ime-iat-api.xfyun.cn"
PATH = "/v2/iat"
APP_ID = "100IME"
API_KEY = "15461402826c8360ebcf1270cbba88e5"
API_SECRET = "sH8v2wSSxxHTy1UNfN6dFaSqVjlmR7BB"
CHUNK_BYTES = 1280  # 16 kHz mono S16LE 40 ms
AUDIO_FORMAT = "audio/L16;rate=16000"

BUSINESS: dict[str, Any] = {
    "language": "zh_cn",
    "domain": "iat",
    "accent": "mandarin",
    "vad_eos": 3000,
    "dwa": "wpgs",
    "nunum": 1,
    "ptt": 1,
}

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "iFlytek IME Streaming ASR",
        "err_invalid_event": "Invalid input event: {detail}",
        "err_network": "iFlytek IME streaming network error: {detail}",
        "err_upstream": "iFlytek IME streaming server error {code}: {detail}",
        "err_runtime": "iFlytek IME streaming runtime error: {detail}",
    },
    "zh_CN": {
        "title": "讯飞输入法实时语音识别",
        "err_invalid_event": "输入事件格式错误：{detail}",
        "err_network": "讯飞输入法流式语音识别网络连接失败：{detail}",
        "err_upstream": "讯飞输入法流式语音服务端报错 {code}：{detail}",
        "err_runtime": "讯飞输入法流式语音运行异常：{detail}",
    },
}


class VInputError(Exception):
    """Base exception for structured vinput provider errors."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Configuration error resulting in usage exit code."""

    exit_code = EXIT_USAGE_ERROR


def detect_locale() -> str:
    """Detect system locale from LC_ALL, LC_MESSAGES, or LANG."""
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.getenv(var, "").strip()
        if not val:
            continue
        base = val.split(".")[0].split("@")[0]
        if base in MESSAGES:
            return base
        prefix = base.split("_")[0]
        for key in MESSAGES:
            if key.startswith(prefix + "_"):
                return key
    return "en_US"


def get_message(key: str, **kwargs: object) -> str:
    """Retrieve localized message string with fallback chain."""
    loc = detect_locale()
    tpl = MESSAGES.get(loc, {}).get(key)
    if not tpl:
        tpl = MESSAGES.get("en_US", {}).get(key, key)
    try:
        return tpl.format(**kwargs)
    except Exception:
        return tpl


def notify_error(key: str, **kwargs: object) -> None:
    """Emit JSONL user desktop notification to stderr."""
    title = get_message("title")
    msg = get_message(key, **kwargs)
    payload = json.dumps({"title": title, "body": msg}, ensure_ascii=False)
    sys.stderr.write(f"{payload}\n")
    sys.stderr.flush()


def emit_event(payload: dict[str, Any]) -> None:
    """Write protocol JSON event to stdout."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def emit_session_started(session_id: str = "") -> None:
    """Emit session_started event to stdout."""
    event: dict[str, Any] = {"type": "session_started"}
    if session_id:
        event["session_id"] = session_id
    emit_event(event)


def emit_partial(text: str) -> None:
    """Emit partial recognition event to stdout."""
    emit_event({"type": "partial", "text": text})


def emit_final(text: str) -> None:
    """Emit committed final transcript event to stdout."""
    emit_event(
        {
            "type": "final",
            "text": text,
            "segment_final": True,
            "utterance_final": True,
        }
    )


def emit_closed() -> None:
    """Emit closed event to stdout."""
    emit_event({"type": "closed"})


def build_auth_url(endpoint: str = "") -> str:
    if endpoint:
        return endpoint
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


def build_frame(audio: bytes, status: int, include_business: bool) -> dict[str, Any]:
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
    return frame


@dataclass
class ProviderState:
    error: str | None = None
    closed: bool = False
    cancelled: bool = False
    latest_text: str = ""
    final_received: bool = False
    final_emitted: bool = False
    upstream_error: bool = False
    session_started: bool = False
    segments: dict[int, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StreamingConfig:
    endpoint: str
    timeout: float
    finish_grace_secs: float


def load_config() -> StreamingConfig:
    endpoint = os.getenv("VINPUT_ASR_ENDPOINT", "").strip()
    timeout_str = os.getenv("VINPUT_ASR_TIMEOUT", "").strip()
    grace_str = os.getenv("VINPUT_ASR_FINISH_GRACE_SECS", "").strip()

    try:
        timeout = float(timeout_str) if timeout_str else DEFAULT_TIMEOUT
    except ValueError as exc:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_TIMEOUT must be a number.") from exc

    try:
        finish_grace_secs = float(grace_str) if grace_str else DEFAULT_FINISH_GRACE_SECS
    except ValueError as exc:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_FINISH_GRACE_SECS must be a number.") from exc

    if not 0 < finish_grace_secs <= MAX_FINISH_GRACE_SECS:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_FINISH_GRACE_SECS must be between 0 and 9 seconds.")
    if not 0 < timeout <= 60:
        raise ConfigError("err_runtime", detail="VINPUT_ASR_TIMEOUT must be between 0 and 60 seconds.")

    return StreamingConfig(
        endpoint=endpoint,
        timeout=timeout,
        finish_grace_secs=finish_grace_secs,
    )


class WebSocketClient:
    def __init__(self, url: str, timeout: float) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
            raise VInputError("err_network", detail="Authenticated URL must use ws:// or wss://.")

        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self._recv_buffer = b""
        self._closed = False
        self._send_lock = threading.Lock()
        self.socket = self._connect()

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + self.timeout
        addresses = socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM)
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
                host_header = self.host if self.port in {80, 443} else f"{self.host}:{self.port}"
                request = "\r\n".join(
                    [
                        f"GET {self.path} HTTP/1.1",
                        f"Host: {host_header}",
                        "Upgrade: websocket",
                        "Connection: Upgrade",
                        f"Sec-WebSocket-Key: {key}",
                        "Sec-WebSocket-Version: 13",
                        "\r\n",
                    ]
                )
                sock.sendall(request.encode("utf-8"))
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
        raise RuntimeError(f"Unable to connect to {self.host}:{self.port}: {last_error}")

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
        lines = response.decode("utf-8", errors="replace").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            status = lines[0] if lines else "invalid response"
            raise RuntimeError(f"WebSocket handshake failed: {status}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        if headers.get("sec-websocket-accept") != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def close(self) -> None:
        with self._send_lock:
            if self._closed:
                return
            try:
                self._send_frame_locked(0x8, b"")
            except OSError:
                pass
            try:
                self.socket.close()
            finally:
                self._closed = True

    def recv_message(self) -> tuple[int, bytes] | None:
        fragments = bytearray()
        current_opcode: int | None = None
        fragment_bytes = 0
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
                fragment_bytes = len(payload)
            elif current_opcode is not None:
                fragment_bytes += len(payload)
                if fragment_bytes > MAX_WEBSOCKET_MESSAGE_BYTES:
                    raise RuntimeError("WebSocket message exceeds 4 MiB.")
                fragments.extend(payload)

            if current_opcode is not None and fragment_bytes > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise RuntimeError("WebSocket message exceeds 4 MiB.")

            if not fin:
                continue
            if current_opcode is None:
                continue
            return current_opcode, bytes(fragments)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        with self._send_lock:
            self._send_frame_locked(opcode, payload)

    def _send_frame_locked(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        mask_key = secrets.token_bytes(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        masked = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(bytes(header) + mask_key + masked)

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

        mask_key = b""
        if masked:
            mask_key = self._recv_exact(4)
            if mask_key is None:
                return None
        if length > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise RuntimeError("WebSocket frame exceeds 4 MiB.")
        payload = self._recv_exact(length)
        if payload is None:
            return None
        if masked:
            payload = bytes(value ^ mask_key[index % 4] for index, value in enumerate(payload))
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


# Global runtime singletons
_CONFIG: StreamingConfig | None = None
_CLIENT: WebSocketClient | None = None
_STATE: ProviderState = ProviderState()
_STOP_EVENT = threading.Event()
_FINAL_EVENT = threading.Event()
_PENDING_AUDIO = bytearray()
_FIRST_FRAME: bool = True
_SENT_FINISH: bool = False


def connect(config: StreamingConfig) -> None:
    global _CONFIG, _CLIENT, _STATE, _STOP_EVENT, _FINAL_EVENT, _PENDING_AUDIO, _FIRST_FRAME, _SENT_FINISH
    _CONFIG = config
    _STATE = ProviderState()
    _STOP_EVENT.clear()
    _FINAL_EVENT.clear()
    _PENDING_AUDIO.clear()
    _FIRST_FRAME = True
    _SENT_FINISH = False

    url = build_auth_url(config.endpoint)
    _CLIENT = WebSocketClient(url, config.timeout)
    session_id = str(uuid.uuid4())
    emit_session_started(session_id)
    _STATE.session_started = True


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    del is_commit
    global _FIRST_FRAME
    if not _CLIENT:
        return
    _PENDING_AUDIO.extend(pcm_bytes)
    while len(_PENDING_AUDIO) >= CHUNK_BYTES:
        frame_bytes = bytes(_PENDING_AUDIO[:CHUNK_BYTES])
        del _PENDING_AUDIO[:CHUNK_BYTES]
        frame = build_frame(frame_bytes, 0 if _FIRST_FRAME else 1, _FIRST_FRAME)
        _CLIENT.send_text(json.dumps(frame, separators=(",", ":")))
        _FIRST_FRAME = False


def on_finish() -> None:
    global _FIRST_FRAME, _SENT_FINISH
    if not _CLIENT:
        return
    if not _STATE.upstream_error and not _STATE.final_received and not _SENT_FINISH:
        if _PENDING_AUDIO:
            frame_bytes = bytes(_PENDING_AUDIO)
            _PENDING_AUDIO.clear()
            frame = build_frame(frame_bytes, 0 if _FIRST_FRAME else 1, _FIRST_FRAME)
            _CLIENT.send_text(json.dumps(frame, separators=(",", ":")))
            _FIRST_FRAME = False
        final_frame = build_frame(b"", 2, _FIRST_FRAME)
        _CLIENT.send_text(json.dumps(final_frame, separators=(",", ":")))
        _SENT_FINISH = True


def on_cancel() -> None:
    _STATE.cancelled = True
    cleanup()


def on_upstream_message(raw_msg: str | bytes) -> None:
    if isinstance(raw_msg, str):
        payload = raw_msg.encode("utf-8")
    else:
        payload = raw_msg

    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid iFlytek IME response JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Invalid iFlytek IME response: expected a JSON object.")

    code = data.get("code", 0)
    if code != 0:
        details = str(data.get("message", "unknown error"))
        notify_error("err_upstream", code=str(code), detail=details)
        raise RuntimeError(f"iFlytek IME error {code}: {details}")

    body = data.get("data")
    if not isinstance(body, dict):
        return

    result = body.get("result")
    if isinstance(result, dict):
        sn = int(result.get("sn") or 0)
        words = "".join(chunk.get("w", "") for group in result.get("ws") or [] for chunk in (group.get("cw") or [])[:1])
        if result.get("pgs") == "rpl":
            rg = result.get("rg") or []
            if len(rg) == 2:
                low, high = int(rg[0]), int(rg[1])
                _STATE.segments = {k: v for k, v in _STATE.segments.items() if not low <= k <= high}
        _STATE.segments[sn] = words
        current_text = "".join(_STATE.segments[key] for key in sorted(_STATE.segments))
        if current_text and current_text != _STATE.latest_text:
            _STATE.latest_text = current_text
            emit_partial(current_text)

    if body.get("status") == 2:
        _STATE.final_received = True
        _FINAL_EVENT.set()


def cleanup() -> None:
    global _CLIENT
    _STOP_EVENT.set()
    _FINAL_EVENT.set()
    if _CLIENT:
        _CLIENT.close()
        _CLIENT = None


def run() -> int:
    try:
        config = load_config()
    except ValueError as exc:
        notify_error("err_runtime", detail=str(exc))
        return EXIT_USAGE_ERROR

    try:
        connect(config)
    except Exception as exc:
        notify_error("err_network", detail=str(exc))
        return EXIT_RUNTIME_ERROR

    client = _CLIENT
    if not client:
        return EXIT_RUNTIME_ERROR

    def reader() -> None:
        try:
            while not _STOP_EVENT.is_set():
                message = client.recv_message()
                if message is None:
                    if not _STATE.final_received and not _STOP_EVENT.is_set():
                        _STATE.upstream_error = True
                        notify_error("err_network", detail="iFlytek IME connection closed unexpectedly.")
                        _FINAL_EVENT.set()
                    break
                opcode, payload = message
                if opcode != 0x1:
                    continue
                on_upstream_message(payload)
        except Exception as exc:
            if not _STOP_EVENT.is_set():
                _STATE.upstream_error = True
                notify_error("err_runtime", detail=str(exc))
                _FINAL_EVENT.set()
        finally:
            _STOP_EVENT.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    saw_finish = False
    try:
        for raw_line in sys.stdin:
            if _STOP_EVENT.is_set():
                break

            line = raw_line.strip()
            if not line:
                continue

            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VInputError("err_invalid_event", detail=f"Invalid JSON input: {exc}") from exc

            event_type = str(event.get("type", "")).strip()
            if event_type == "audio":
                audio_base64 = event.get("audio_base64")
                if not isinstance(audio_base64, str) or not audio_base64:
                    raise VInputError("err_invalid_event", detail="audio event requires non-empty audio_base64.")
                try:
                    pcm_audio = base64.b64decode(audio_base64)
                except Exception as exc:
                    raise VInputError("err_invalid_event", detail=f"Invalid audio_base64 payload: {exc}") from exc
                on_audio_chunk(pcm_audio, bool(event.get("commit", False)))
                continue

            if event_type == "finish":
                saw_finish = True
                on_finish()
                break

            if event_type == "cancel":
                on_cancel()
                break

            raise VInputError("err_invalid_event", detail=f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if saw_finish and not _STOP_EVENT.is_set():
            _FINAL_EVENT.wait(timeout=config.finish_grace_secs)

        if not _STATE.cancelled and _STATE.latest_text and not _STATE.final_emitted:
            emit_final(_STATE.latest_text)
            _STATE.final_emitted = True

        cleanup()
        reader_thread.join(timeout=1.0)

        if not _STATE.closed:
            emit_closed()
            _STATE.closed = True

    if _STATE.error or _STATE.upstream_error:
        return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS


def main() -> int:
    try:
        return run()
    except VInputError as exc:
        notify_error(exc.key, **exc.kwargs)
        return exc.exit_code
    except Exception as exc:
        notify_error("err_runtime", detail=str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    sys.exit(main())
