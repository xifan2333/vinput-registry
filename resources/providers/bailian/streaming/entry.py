#!/usr/bin/env python3
"""Aliyun Bailian / DashScope duplex streaming speech-to-text provider."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen-asr-realtime-v1"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 0.4
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Bailian Streaming ASR",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_network": "Bailian streaming network error: {detail}",
        "err_upstream": "Bailian streaming server error: {message}",
        "err_runtime": "Bailian streaming runtime error: {detail}",
        "err_invalid_event": "Invalid protocol event: {detail}",
    },
    "zh_CN": {
        "title": "百炼实时语音识别",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_network": "百炼流式语音识别网络错误：{detail}",
        "err_upstream": "百炼流式语音服务端报错：{message}",
        "err_runtime": "百炼流式语音运行异常：{detail}",
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


def normalize_transcript_text(text: str) -> str:
    return " ".join(text.split()).strip()


def combine_transcript(committed_text: str, current_text: str) -> str:
    committed = normalize_transcript_text(committed_text)
    current = normalize_transcript_text(current_text)

    if not committed:
        return current
    if not current:
        return committed
    if current == committed or current.startswith(committed):
        return current
    if committed.endswith(current):
        return committed
    return committed + " " + current


@dataclass(frozen=True)
class StreamingConfig:
    api_key: str
    model: str
    timeout: int
    finish_grace_secs: float
    url: str


def require_env(name: str) -> str:
    """Read a mandatory environment variable or raise ConfigError."""
    val = os.getenv(name, "").strip()
    if not val:
        raise ConfigError("err_missing_api_key")
    return val


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


def optional_bool_env(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() not in {"0", "false", "no", "off"}


def new_event_id() -> str:
    return "event_" + uuid.uuid4().hex


def build_url(model: str) -> str:
    base_url = optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunparse(parsed._replace(query=urlencode(query)))


def load_config() -> StreamingConfig:
    api_key = require_env("VINPUT_ASR_API_KEY")
    model = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace = optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    url = build_url(model)
    return StreamingConfig(
        api_key=api_key,
        model=model,
        timeout=timeout,
        finish_grace_secs=finish_grace,
        url=url,
    )


@dataclass
class SessionState:
    session_started: bool = False
    error: str | None = None
    closed: bool = False
    confirmed_text: str = ""
    latest_partial_text: str = ""
    last_final_text: str = ""
    last_item_id: str = ""
    partials: dict[str, str] = field(default_factory=dict)
    pending_audio_since_commit: bool = False
    server_finished: bool = False

    def get_last_final_text(self) -> str:
        return normalize_transcript_text(self.last_final_text)

    def has_usable_final(self) -> bool:
        return bool(self.get_last_final_text())

    def clear_partial_state(self) -> None:
        self.latest_partial_text = ""
        self.partials.clear()

    def record_final_text(self, text: str) -> str:
        final_text = normalize_transcript_text(text)
        self.last_final_text = final_text
        self.confirmed_text = final_text
        self.clear_partial_state()
        return final_text


def emit_final_text(
    state: SessionState,
    text: str,
    *,
    utterance_final: bool = False,
    extra_fields: dict[str, Any] | None = None,
) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text or final_text == state.get_last_final_text():
        return False

    event: dict[str, Any] = {
        "type": "final",
        "text": state.record_final_text(final_text),
        "segment_final": True,
    }
    if utterance_final:
        event["utterance_final"] = True
    if extra_fields:
        event.update(extra_fields)
    emit_event(event)
    return True


def emit_fallback_final(state: SessionState, utterance_final: bool = True) -> bool:
    return emit_final_text(
        state,
        state.latest_partial_text,
        utterance_final=utterance_final,
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


def build_session_update_event() -> dict[str, Any]:
    session: dict[str, Any] = {
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "input_audio_transcription": {},
    }

    prompt = optional_env("VINPUT_ASR_PROMPT")
    if prompt:
        session["input_audio_transcription"]["prompt"] = prompt

    language = optional_env("VINPUT_ASR_LANGUAGE")
    if language:
        session["input_audio_transcription"]["language"] = language

    if optional_bool_env("VINPUT_ASR_ENABLE_VAD", False):
        session["turn_detection"] = {
            "type": "server_vad",
            "threshold": optional_float_env("VINPUT_ASR_VAD_THRESHOLD", 0.0),
            "prefix_padding_ms": optional_int_env("VINPUT_ASR_VAD_PREFIX_PADDING_MS", 300),
            "silence_duration_ms": optional_int_env("VINPUT_ASR_VAD_SILENCE_DURATION_MS", 400),
        }
    else:
        session["turn_detection"] = None

    return {
        "event_id": new_event_id(),
        "type": "session.update",
        "session": session,
    }


def handle_server_message(message: dict[str, Any], state: SessionState) -> None:
    message_type = str(message.get("type", "")).strip()

    if message_type in {"session.created", "session.updated"}:
        session = message.get("session", {})
        session_id = ""
        if isinstance(session, dict):
            session_id = str(session.get("id", ""))
        if not state.session_started:
            emit_session_started(session_id)
        state.session_started = True
        return

    if message_type == "input_audio_buffer.committed":
        item_id = str(message.get("item_id", "")).strip()
        if item_id:
            state.last_item_id = item_id
        return

    if message_type == "conversation.item.input_audio_transcription.text":
        item_id = str(message.get("item_id", "")).strip() or str(state.last_item_id).strip()
        preview_text = str(message.get("text", "")) + str(message.get("stash", ""))
        if item_id:
            state.partials[item_id] = preview_text
        visible_text = combine_transcript(
            state.confirmed_text,
            preview_text,
        )
        state.latest_partial_text = visible_text
        emit_partial(visible_text)
        return

    if message_type == "conversation.item.input_audio_transcription.completed":
        item_id = str(message.get("item_id", "")).strip()
        transcript = str(message.get("transcript", "")).strip()
        full_text = combine_transcript(state.confirmed_text, transcript)
        if item_id:
            state.partials.pop(item_id, None)
        event: dict[str, Any] = {}
        language = message.get("language")
        if language is not None:
            event["language"] = language
        emotion = message.get("emotion")
        if emotion is not None:
            event["emotion"] = emotion
        emit_final_text(state, full_text, extra_fields=event)
        return

    if message_type == "conversation.item.input_audio_transcription.failed":
        if state.has_usable_final():
            return
        if emit_fallback_final(state):
            return
        error = message.get("error")
        error_message = "Input audio transcription failed."
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                error_message = candidate.strip()
        notify_error("err_upstream", message=error_message)
        state.error = error_message
        return

    if message_type == "session.finished":
        state.server_finished = True
        return

    if message_type == "error":
        if state.has_usable_final():
            return
        if emit_fallback_final(state):
            return
        error = message.get("error")
        error_message = "Unknown Bailian error."
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                error_message = candidate.strip()
        notify_error("err_upstream", message=error_message)
        state.error = error_message
        return


_CLIENT: WebSocketClient | None = None
_STATE: SessionState | None = None
_STOP_EVENT = threading.Event()


def connect(config: StreamingConfig) -> None:
    global _CLIENT
    _CLIENT = WebSocketClient(config.url, {"Authorization": f"Bearer {config.api_key}"}, config.timeout)
    _CLIENT.send_json(build_session_update_event())


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    if not _CLIENT or not _STATE:
        return
    audio_b64 = base64.b64encode(pcm_bytes).decode("ascii")
    _CLIENT.send_json(
        {
            "event_id": new_event_id(),
            "type": "input_audio_buffer.append",
            "audio": audio_b64,
        }
    )
    _STATE.pending_audio_since_commit = True
    if is_commit and _STATE.pending_audio_since_commit:
        _CLIENT.send_json(
            {
                "event_id": new_event_id(),
                "type": "input_audio_buffer.commit",
            }
        )
        _STATE.pending_audio_since_commit = False


def on_finish() -> None:
    if _CLIENT:
        _CLIENT.send_json({"event_id": new_event_id(), "type": "session.finish"})


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
        notify_error("err_missing_api_key" if "API_KEY" in str(exc) else "err_runtime", detail=str(exc))
        return EXIT_USAGE_ERROR

    try:
        connect(config)
    except Exception as exc:
        notify_error("err_network", detail=str(exc))
        return EXIT_RUNTIME_ERROR

    _STATE = SessionState()
    state = _STATE
    stop_event = _STOP_EVENT
    stop_event.clear()

    client = _CLIENT
    if not client:
        return EXIT_RUNTIME_ERROR

    def reader() -> None:
        try:
            while not stop_event.is_set():
                message = client.recv_json()
                if message is None:
                    break
                handle_server_message(message, state)
                if state.server_finished:
                    break
        except Exception as exc:
            if not stop_event.is_set():
                state.error = str(exc)
                notify_error("err_network", detail=str(exc))
        finally:
            stop_event.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    saw_finish = False
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

            event_type = str(event.get("type", "")).strip()
            if event_type == "audio":
                audio_base64 = event.get("audio_base64")
                if not isinstance(audio_base64, str) or not audio_base64:
                    raise VInputError("err_invalid_event", detail="audio event requires non-empty audio_base64.")
                pcm = base64.b64decode(audio_base64)
                on_audio_chunk(pcm, bool(event.get("commit", False)))
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
        if saw_finish and not stop_event.is_set():
            thread.join(timeout=config.finish_grace_secs)
        emit_fallback_final(state)
        cleanup()
        thread.join(timeout=1.0)
        if not state.closed:
            emit_closed()
            state.closed = True

    if state.error:
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
