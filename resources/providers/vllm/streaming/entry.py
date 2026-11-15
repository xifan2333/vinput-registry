#!/usr/bin/env python3
"""vLLM-omni streaming ASR provider via OpenAI Realtime WebSocket protocol."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import socket
import ssl
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

DEFAULT_URL = "ws://localhost:8000/v1/realtime"
DEFAULT_MODEL = "fixie-ai/ultravox-v0_5-llama-3_2-1b"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 0.5
DEFAULT_CHUNK_MS = 20
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "vLLM Streaming ASR",
        "err_network": "vLLM streaming network error: {detail}",
        "err_upstream": "vLLM streaming server error: {message}",
        "err_runtime": "vLLM streaming runtime error: {detail}",
        "err_invalid_event": "Invalid protocol event: {detail}",
    },
    "zh_CN": {
        "title": "vLLM 实时语音识别",
        "err_network": "vLLM 流式语音识别网络连接失败：{detail}",
        "err_upstream": "vLLM 流式语音服务端报错：{message}",
        "err_runtime": "vLLM 流式语音运行异常：{detail}",
        "err_invalid_event": "协议事件格式错误：{detail}",
    },
}


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


class VInputError(Exception):
    """Base exception carrying exit code and message key."""

    exit_code = EXIT_RUNTIME_ERROR

    def __init__(self, key: str, **kwargs: object) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


class ConfigError(VInputError):
    """Raised when a required environment variable is missing or invalid."""

    exit_code = EXIT_USAGE_ERROR


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


@dataclass(frozen=True)
class StreamingConfig:
    url: str
    model: str
    timeout: int
    finish_grace_secs: float
    chunk_ms: int


def optional_env(name: str, default: str = "") -> str:
    val = os.getenv(name, "").strip()
    return val if val else default


def optional_int_env(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def optional_float_env(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def load_config() -> StreamingConfig:
    url = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    model = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace = optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    chunk_ms = optional_int_env("VINPUT_ASR_CHUNK_MS", DEFAULT_CHUNK_MS)
    return StreamingConfig(
        url=url,
        model=model,
        timeout=timeout,
        finish_grace_secs=finish_grace,
        chunk_ms=chunk_ms,
    )


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: int) -> None:
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
        self.socket = self._connect()
        self._recv_buffer = b""
        self._closed = False

    def _connect(self) -> socket.socket:
        raw_sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        raw_sock.settimeout(self.timeout)

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
        request = "\r\n".join(lines) + "\r\n\r\n"
        sock.sendall(request.encode("utf-8"))

        response = self._read_http_response(sock)
        status_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
        if not (status_line.startswith("HTTP/1.1 101") or status_line.startswith("HTTP/1.0 101")):
            sock.close()
            raise RuntimeError(f"WebSocket handshake failed: {status_line}")

        expected_accept = base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")
        headers = self._parse_headers(response)
        actual_accept = headers.get("sec-websocket-accept")
        if actual_accept != expected_accept:
            sock.close()
            raise RuntimeError("Sec-WebSocket-Accept header mismatch.")
        return sock

    def _parse_headers(self, response: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        parts = response.split(b"\r\n\r\n", 1)[0].split(b"\r\n")
        for line in parts[1:]:
            if not line:
                continue
            name, _, value = line.partition(b":")
            headers[name.strip().decode("latin1").lower()] = value.strip().decode("latin1")
        return headers

    def _read_http_response(self, sock: socket.socket) -> bytes:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake closed early.")
            data += chunk
        return data

    def send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return
        frame = bytearray()
        frame.append(0x80 | (opcode & 0x0F))
        length = len(payload)
        mask_bit = 0x80
        if length < 126:
            frame.append(mask_bit | length)
        elif length < 65536:
            frame.append(mask_bit | 126)
            frame.extend(struct.pack("!H", length))
        else:
            frame.append(mask_bit | 127)
            frame.extend(struct.pack("!Q", length))

        mask = secrets.token_bytes(4)
        frame.extend(mask)
        masked = bytearray(payload)
        for index in range(len(masked)):
            masked[index] ^= mask[index % 4]
        frame.extend(masked)
        self.socket.sendall(frame)

    def send_text(self, text: str) -> None:
        self.send_frame(0x1, text.encode("utf-8"))

    def send_json(self, payload: dict[str, Any]) -> None:
        self.send_text(json.dumps(payload, ensure_ascii=False))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.send_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.socket.close()
        except Exception:
            pass

    def recv_frame(self) -> tuple[int, bytes] | None:
        while True:
            header = self._recv_exact(2)
            if header is None:
                return None

            first, second = struct.unpack("!BB", header)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F

            if length == 126:
                ext = self._recv_exact(2)
                if ext is None:
                    return None
                (length,) = struct.unpack("!H", ext)
            elif length == 127:
                ext = self._recv_exact(8)
                if ext is None:
                    return None
                (length,) = struct.unpack("!Q", ext)

            mask = b""
            if masked:
                raw_mask = self._recv_exact(4)
                if raw_mask is None:
                    return None
                mask = raw_mask

            payload = self._recv_exact(length)
            if payload is None:
                return None

            if masked:
                unmasked = bytearray(payload)
                for index in range(len(unmasked)):
                    unmasked[index] ^= mask[index % 4]
                payload = bytes(unmasked)

            if opcode == 0x8:
                self.close()
                return None
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            return opcode, payload

    def recv_text(self) -> str | None:
        result = self.recv_frame()
        if result is None:
            return None
        _, payload = result
        return payload.decode("utf-8")

    def recv_json(self) -> dict[str, Any] | None:
        raw = self.recv_text()
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Invalid JSON received from server: {exc}") from exc

    def _recv_exact(self, size: int) -> bytes | None:
        while len(self._recv_buffer) < size:
            try:
                chunk = self.socket.recv(max(4096, size - len(self._recv_buffer)))
            except TimeoutError:
                raise
            except OSError:
                return None
            if not chunk:
                if not self._recv_buffer:
                    return None
                raise RuntimeError("WebSocket connection closed unexpectedly.")
            self._recv_buffer += chunk

        data = self._recv_buffer[:size]
        self._recv_buffer = self._recv_buffer[size:]
        return data


def _strip_trailing_lang_prefix(seg: str) -> str:
    cleaned = seg.rstrip()
    for tag in ("zh", "en", "yue", "ja", "ko"):
        pattern = r"(?:^|\s)<\|" + tag + r"\|>$"
        new_cleaned = re.sub(pattern, "", cleaned)
        if new_cleaned != cleaned:
            return new_cleaned.rstrip()
    return seg


def strip_prefix(text: str) -> str:
    segments = re.split(r"(<\|(?:startoftranscript|transcribe|notimestamps)\|>\s*)", text)
    if len(segments) > 1:
        text = segments[-1]
    text = re.sub(r"^<\|[a-z]{2,3}\|>\s*", "", text)
    text = re.sub(r"<\|(?:startoftranscript|transcribe|notimestamps)\|>", "", text)
    text = re.sub(r"<\|[a-z]{2,3}\|>$", "", text)
    text = _strip_trailing_lang_prefix(text)
    return text.strip()


def build_session_update(model: str) -> dict[str, Any]:
    return {"type": "session.update", "session": {"model": model}}


def build_append(audio_b64: str) -> dict[str, Any]:
    return {"type": "input_audio_buffer.append", "audio": audio_b64}


def build_commit(final: bool) -> dict[str, Any]:
    return {"type": "input_audio_buffer.commit", "final": final}


def handle_server_message(message: dict[str, Any], state: dict[str, Any]) -> None:
    mtype = str(message.get("type", "")).strip()
    if mtype in ("session.created", "session.updated"):
        state["session_started"] = True
        return
    if mtype == "transcription.delta":
        delta = str(message.get("delta", ""))
        state["raw"] += delta
        cleaned = strip_prefix(state["raw"])
        if cleaned:
            emit_partial(cleaned)
        return
    if mtype == "transcription.done":
        text = str(message.get("text", ""))
        final = strip_prefix(text) if text else strip_prefix(state["raw"])
        if final and not state.get("final_sent"):
            state["final_sent"] = True
            emit_final(final)
        state["done"] = True
        return
    if mtype == "error":
        err = message.get("error")
        err_msg = str(err.get("message", "ASR error")) if isinstance(err, dict) else str(err or "ASR error")
        notify_error("err_upstream", message=err_msg)
        state["error"] = err_msg
        return


def wait_for_session_ready(state: dict[str, Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if state.get("session_started"):
            return
        time.sleep(0.02)
    raise RuntimeError("Timed out waiting for session.created")


_CLIENT: WebSocketClient | None = None
_STATE: dict[str, Any] = {}
_STOP_EVENT = threading.Event()
_CONFIG: StreamingConfig | None = None


def connect(config: StreamingConfig) -> None:
    global _CLIENT, _CONFIG, _STATE
    _CONFIG = config
    _CLIENT = WebSocketClient(config.url, {}, config.timeout)
    emit_session_started()
    _CLIENT.send_json(build_session_update(config.model))
    _CLIENT.send_json(build_commit(final=False))
    _STATE = {
        "session_started": False,
        "error": None,
        "done": False,
        "raw": "",
        "closed": False,
        "final_sent": False,
    }


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    if not _CLIENT or not _CONFIG:
        return
    wait_for_session_ready(_STATE, _CONFIG.timeout)
    step = max(2, int(16000 * _CONFIG.chunk_ms / 1000) * 2)
    for i in range(0, len(pcm_bytes), step):
        chunk = pcm_bytes[i : i + step]
        _CLIENT.send_json(build_append(base64.b64encode(chunk).decode("ascii")))
    if is_commit:
        _CLIENT.send_json(build_commit(final=True))


def on_finish() -> None:
    if _CLIENT:
        _CLIENT.send_json(build_commit(final=True))


def on_cancel() -> None:
    _STOP_EVENT.set()
    if _CLIENT:
        _CLIENT.close()


def on_upstream_message(raw_msg: str | bytes) -> None:
    if not _STATE:
        return
    if isinstance(raw_msg, bytes):
        raw_msg = raw_msg.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw_msg)
        if isinstance(data, dict):
            handle_server_message(data, _STATE)
    except Exception as exc:
        notify_error("err_runtime", detail=str(exc))


def cleanup() -> None:
    global _CLIENT
    _STOP_EVENT.set()
    if _CLIENT:
        _CLIENT.close()
        _CLIENT = None


def run() -> int:
    global _STATE
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

    state = _STATE
    client = _CLIENT
    stop_event = _STOP_EVENT
    stop_event.clear()

    if not client:
        return EXIT_RUNTIME_ERROR

    def reader() -> None:
        try:
            while not stop_event.is_set():
                message = client.recv_json()
                if message is None:
                    break
                handle_server_message(message, state)
        except Exception as exc:
            if not stop_event.is_set():
                state["error"] = str(exc)
                notify_error("err_network", detail=str(exc))
        finally:
            stop_event.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    saw_finish = False
    pending_commit = False
    try:
        for raw_line in sys.stdin:
            if stop_event.is_set():
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise VInputError("err_invalid_event", detail=f"Invalid JSON input: {exc}") from exc
            etype = str(event.get("type", "")).strip()
            if etype == "audio":
                b64 = event.get("audio_base64")
                if not isinstance(b64, str) or not b64:
                    raise VInputError("err_invalid_event", detail="audio event requires non-empty audio_base64.")
                pcm_audio = base64.b64decode(b64)
                pending_commit = True
                on_audio_chunk(pcm_audio, bool(event.get("commit", False)))
                if bool(event.get("commit", False)):
                    pending_commit = False
                continue
            if etype == "finish":
                saw_finish = True
                if pending_commit:
                    on_finish()
                break
            if etype == "cancel":
                on_cancel()
                break
            raise VInputError("err_invalid_event", detail=f"Unsupported event type: {etype or ''}")
    finally:
        if saw_finish and not stop_event.is_set():
            thread.join(timeout=config.finish_grace_secs)
        cleanup()
        thread.join(timeout=1.0)
        if state.get("raw") and not state.get("final_sent") and not state.get("error") and state.get("done"):
            final = strip_prefix(state["raw"])
            if final:
                state["final_sent"] = True
                emit_final(final)
        if not state.get("closed"):
            emit_closed()
            state["closed"] = True
    if state.get("error"):
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
