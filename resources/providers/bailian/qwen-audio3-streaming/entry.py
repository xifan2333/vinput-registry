#!/usr/bin/env python3
"""Cloud ASR provider script for Bailian Qwen-Audio-3.0-ASR-Flash-Streaming.

Implements the DashScope duplex realtime speech recognition protocol
(run-task / finish-task over a WebSocket with binary audio frames). The same
protocol is used by Fun-ASR-Realtime and Paraformer realtime models, so those
models can be selected with VINPUT_ASR_MODEL as well.
"""

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
from urllib.parse import urlparse

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
MAAS_URL_TEMPLATE = "wss://{workspace_id}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference"
MAX_CONTEXT_TEXT_LENGTH = 400
DEFAULT_MODEL = "qwen-audio-3.1-asr-flash-streaming"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FORMAT = "pcm"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 1.2
MAX_FRAME_SIZE = 16 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Bailian Qwen-Audio3 Streaming ASR",
        "err_missing_api_key": "Missing VINPUT_ASR_API_KEY.",
        "err_network": "Bailian Qwen-Audio streaming network error: {detail}",
        "err_upstream": "Bailian Qwen-Audio streaming server error: {message}",
        "err_runtime": "Bailian Qwen-Audio streaming runtime error: {detail}",
        "err_invalid_event": "Invalid protocol event: {detail}",
    },
    "zh_CN": {
        "title": "百炼 Qwen-Audio3 实时语音识别",
        "err_missing_api_key": "缺少 VINPUT_ASR_API_KEY 环境变量。",
        "err_network": "百炼通义语音识别流式连接失败：{detail}",
        "err_upstream": "百炼通义语音服务端报错：{message}",
        "err_runtime": "百炼通义语音运行异常：{detail}",
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

    has_cjk = any("\u4e00" <= char <= "\u9fff" for char in committed[-1:] + current[:1])
    delimiter = "" if has_cjk else " "
    return committed + delimiter + current


@dataclass(frozen=True)
class StreamingConfig:
    api_key: str
    model: str
    timeout: int
    finish_grace_secs: float
    endpoint: str
    prompt: str | None


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


def resolve_endpoint() -> str:
    explicit = optional_env("VINPUT_ASR_URL")
    if explicit:
        return explicit
    workspace_id = optional_env("VINPUT_ASR_WORKSPACE_ID")
    if workspace_id:
        return MAAS_URL_TEMPLATE.format(workspace_id=workspace_id)
    return DEFAULT_URL


def load_config() -> StreamingConfig:
    api_key = require_env("VINPUT_ASR_API_KEY")
    model = optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace = optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    endpoint = resolve_endpoint()
    prompt = optional_env("VINPUT_ASR_PROMPT") or None
    return StreamingConfig(
        api_key=api_key,
        model=model,
        timeout=timeout,
        finish_grace_secs=finish_grace,
        endpoint=endpoint,
        prompt=prompt,
    )


@dataclass
class SessionState:
    task_id: str
    session_started: bool = False
    error: str | None = None
    closed: bool = False
    committed_text: str = ""
    latest_sentence_text: str = ""
    server_finished: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)
    pending_audio: bytearray = field(default_factory=bytearray)

    def current_transcript(self) -> str:
        return combine_transcript(self.committed_text, self.latest_sentence_text)

    def has_usable_final(self) -> bool:
        return bool(normalize_transcript_text(self.committed_text))


def emit_partial_state(state: SessionState) -> bool:
    visible = state.current_transcript()
    if not visible:
        return False
    emit_partial(visible)
    return True


def emit_final_state(state: SessionState, text: str) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text:
        return False
    emit_event(
        {
            "type": "final",
            "text": final_text,
            "segment_final": True,
            "utterance_final": False,
        }
    )
    return True


def commit_sentence(state: SessionState, sentence_text: str) -> bool:
    cleaned = normalize_transcript_text(sentence_text)
    if not cleaned:
        state.latest_sentence_text = ""
        return False
    state.committed_text = combine_transcript(state.committed_text, cleaned)
    state.latest_sentence_text = ""
    return emit_final_state(state, state.committed_text)


def emit_fallback_final(state: SessionState) -> bool:
    if state.latest_sentence_text:
        commit_sentence(state, state.latest_sentence_text)
        return True
    return False


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

    def send_binary(self, payload: bytes) -> None:
        self.send_frame(0x2, payload)

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

    def recv_message(self) -> bytes | None:
        fragments = bytearray()
        first_opcode: int | None = None
        while True:
            header = self._recv_exact(2)
            if header is None:
                return None

            first, second = struct.unpack("!BB", header)
            fin = bool(first & 0x80)
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

            if length > MAX_FRAME_SIZE:
                raise RuntimeError(f"WebSocket frame size {length} exceeds {MAX_FRAME_SIZE} limit.")

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

            if opcode != 0x0:
                if first_opcode is not None:
                    raise RuntimeError("Received new frame without FIN on previous fragmented frame.")
                first_opcode = opcode

            fragments.extend(payload)
            if fin:
                return bytes(fragments)

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


def build_parameters() -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "sample_rate": optional_int_env("VINPUT_ASR_SAMPLE_RATE", DEFAULT_SAMPLE_RATE),
        "format": optional_env("VINPUT_ASR_FORMAT", DEFAULT_FORMAT),
    }
    language = optional_env("VINPUT_ASR_LANGUAGE")
    if language:
        parameters["language_hints"] = [language]

    disfluency = optional_bool_env("VINPUT_ASR_FILTER_DISFLUENCY", True)
    parameters["disfluency_removal_enabled"] = disfluency
    return parameters


def build_context(prompt: str) -> list[dict[str, Any]]:
    clean = prompt.strip()
    if len(clean) > MAX_CONTEXT_TEXT_LENGTH:
        clean = clean[-MAX_CONTEXT_TEXT_LENGTH:]
    return [{"text": clean}]


def build_run_task(task_id: str, model: str, parameters: dict[str, Any], prompt: str | None = None) -> str:
    body: dict[str, Any] = {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }
    if prompt:
        body["payload"]["input"]["context"] = build_context(prompt)
    return json.dumps(body, ensure_ascii=False)


def build_finish_task(task_id: str) -> str:
    return json.dumps(
        {
            "header": {
                "action": "finish-task",
                "task_id": task_id,
                "streaming": "duplex",
            },
            "payload": {"input": {}},
        },
        ensure_ascii=False,
    )


def sentence_from_message(message: dict[str, Any]) -> dict[str, Any] | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if not isinstance(output, dict):
        return None
    sentence = output.get("sentence")
    if not isinstance(sentence, dict):
        return None
    return sentence


def handle_result_generated(message: dict[str, Any], state: SessionState) -> None:
    sentence = sentence_from_message(message)
    if not sentence:
        return

    text = sentence.get("text")
    if not isinstance(text, str):
        return

    cleaned = normalize_transcript_text(text)
    if not cleaned:
        return

    if sentence.get("end_time") is not None:
        commit_sentence(state, cleaned)
        return

    state.latest_sentence_text = cleaned
    emit_partial_state(state)


def handle_task_failed(message: dict[str, Any], state: SessionState) -> None:
    header = message.get("header")
    error_message = "Bailian task failed."
    if isinstance(header, dict):
        code = header.get("error_code")
        candidate = header.get("error_message")
        if isinstance(code, str) and code.strip():
            error_message = f"{code}: {candidate}" if isinstance(candidate, str) and candidate.strip() else code
        elif isinstance(candidate, str) and candidate.strip():
            error_message = candidate.strip()
    notify_error("err_upstream", message=error_message)
    state.error = error_message


def handle_server_message(message: dict[str, Any], state: SessionState) -> None:
    header = message.get("header")
    if not isinstance(header, dict):
        return

    event = str(header.get("event", "")).strip()
    if event == "task-started":
        if not state.session_started:
            emit_session_started(state.task_id)
            state.session_started = True
        return

    if event == "result-generated":
        handle_result_generated(message, state)
        return

    if event == "task-finished":
        state.server_finished = True
        return

    if event == "task-failed":
        handle_task_failed(message, state)
        return


_CLIENT: WebSocketClient | None = None
_STATE: SessionState | None = None
_STOP_EVENT = threading.Event()


def connect(config: StreamingConfig) -> None:
    global _CLIENT, _STATE
    _CLIENT = WebSocketClient(config.endpoint, {"Authorization": f"Bearer {config.api_key}"}, config.timeout)
    _STATE = SessionState(task_id=str(uuid.uuid4()))
    _CLIENT.send_text(build_run_task(_STATE.task_id, config.model, build_parameters(), prompt=config.prompt))


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    if not _CLIENT or not _STATE:
        return
    with _STATE.lock:
        if _STATE.session_started:
            _CLIENT.send_binary(pcm_bytes)
        else:
            _STATE.pending_audio.extend(pcm_bytes)


def on_finish() -> None:
    if _CLIENT and _STATE:
        _CLIENT.send_text(build_finish_task(_STATE.task_id))


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

    state = _STATE
    client = _CLIENT
    stop_event = _STOP_EVENT
    stop_event.clear()

    if not client or not state:
        return EXIT_RUNTIME_ERROR

    def reader() -> None:
        try:
            while not stop_event.is_set():
                raw_message = client.recv_message()
                if raw_message is None:
                    if not stop_event.is_set() and not state.server_finished and not state.closed:
                        state.error = "WebSocket connection closed unexpectedly."
                        notify_error("err_network", detail=state.error)
                    break
                try:
                    message = json.loads(raw_message.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if not isinstance(message, dict):
                    continue
                started_before = state.session_started
                handle_server_message(message, state)
                if not started_before and state.session_started:
                    with state.lock:
                        audio = bytes(state.pending_audio)
                        state.pending_audio.clear()
                    if audio:
                        client.send_binary(audio)
                if state.server_finished:
                    break
        except Exception as exc:
            if not stop_event.is_set() and not state.closed:
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
                try:
                    audio = base64.b64decode(audio_base64)
                except ValueError as exc:
                    raise VInputError("err_invalid_event", detail=f"audio event has invalid base64: {exc}") from exc
                if not audio:
                    raise VInputError("err_invalid_event", detail="audio event decoded to empty audio.")
                on_audio_chunk(audio, bool(event.get("commit", False)))
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
