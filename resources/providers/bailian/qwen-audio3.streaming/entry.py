#!/usr/bin/env python3
"""Cloud ASR provider script for Bailian Qwen-Audio-3.0-ASR-Flash-Streaming.

Implements the DashScope duplex realtime speech recognition protocol
(run-task / finish-task over a WebSocket with binary audio frames). The same
protocol is used by Fun-ASR-Realtime and Paraformer realtime models, so those
models can be selected with VINPUT_ASR_MODEL as well.
"""

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
# Context text is truncated from the end past this limit (server rule).
MAX_CONTEXT_TEXT_LENGTH = 400
DEFAULT_MODEL = "qwen-audio-3.0-asr-flash-streaming"
DEFAULT_SAMPLE_RATE = 16000
DEFAULT_FORMAT = "pcm"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 1.2
MAX_FRAME_SIZE = 16 * 1024 * 1024
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2


def write_stdout(event: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def write_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


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

    # Chinese text normally has no spaces between server-side sentences. Keep a
    # separator only where concatenating ASCII words would join them together.
    separator = (
        " "
        if committed[-1].isascii() and committed[-1].isalnum() and current[0].isascii() and current[0].isalnum()
        else ""
    )
    return committed + separator + current


def get_required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing {name}.")
    return value


def get_optional_env(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    return value or default


def get_optional_int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return int(value)


def get_optional_float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return float(value)


def get_optional_bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


@dataclass
class SessionState:
    task_id: str = ""
    session_started: bool = False
    error: str | None = None
    closed: bool = False
    server_finished: bool = False
    confirmed_text: str = ""
    current_partial_text: str = ""
    last_partial_visible: str = ""
    last_final_text: str = ""
    pending_audio: bytearray = field(default_factory=bytearray)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def visible_text(self) -> str:
        return combine_transcript(self.confirmed_text, self.current_partial_text)

    def has_usable_final(self) -> bool:
        return bool(normalize_transcript_text(self.confirmed_text))


def emit_partial(state: SessionState) -> bool:
    visible = state.visible_text()
    if not visible or visible == state.last_partial_visible:
        return False
    state.last_partial_visible = visible
    write_stdout({"type": "partial", "text": visible})
    return True


def emit_final(state: SessionState, text: str) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text or final_text == state.last_final_text:
        return False
    state.last_final_text = final_text
    write_stdout({"type": "final", "text": final_text, "segment_final": True})
    return True


def commit_sentence(state: SessionState, sentence_text: str) -> bool:
    sentence = normalize_transcript_text(sentence_text)
    if not sentence:
        return False
    new_confirmed = combine_transcript(state.confirmed_text, sentence)
    if not emit_final(state, new_confirmed):
        return False
    state.confirmed_text = new_confirmed
    state.current_partial_text = ""
    state.last_partial_visible = new_confirmed
    return True


def emit_fallback_final(state: SessionState) -> bool:
    if not state.current_partial_text:
        return False
    if not emit_final(state, state.visible_text()):
        return False
    state.confirmed_text = state.last_final_text
    state.current_partial_text = ""
    return True


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
        self._validate_handshake(response, key)
        return sock

    def _read_http_response(self, sock: socket.socket) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket handshake failed: empty response.")
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("WebSocket handshake failed: response too large.")
        return bytes(data)

    def _validate_handshake(self, response: bytes, key: str) -> None:
        header_blob = response.split(b"\r\n\r\n", 1)[0].decode("utf-8", errors="replace")
        lines = header_blob.split("\r\n")
        if not lines or "101" not in lines[0]:
            raise RuntimeError(f"WebSocket handshake failed: {lines[0] if lines else 'invalid response'}")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode("ascii")
        if accept != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        try:
            self.socket.close()
        finally:
            self._closed = True

    def send_text(self, payload: dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def recv_message(self) -> bytes | None:
        """Receive one complete data message, replying to control frames."""
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

            if not fin:
                continue
            return bytes(fragments)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._closed:
            return

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

        if length > MAX_FRAME_SIZE:
            raise RuntimeError("WebSocket frame is too large.")

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


def resolve_endpoint() -> str:
    explicit_url = get_optional_env("VINPUT_ASR_URL")
    if explicit_url:
        return explicit_url

    workspace_id = get_optional_env("VINPUT_ASR_WORKSPACE_ID")
    if workspace_id:
        return MAAS_URL_TEMPLATE.format(workspace_id=workspace_id)

    return DEFAULT_URL


def build_parameters() -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "sample_rate": DEFAULT_SAMPLE_RATE,
        "format": DEFAULT_FORMAT,
    }

    if get_optional_bool_env("VINPUT_ASR_ENABLE_PUNCTUATION", False):
        parameters["semantic_punctuation_enabled"] = True

    silence_ms = get_optional_env("VINPUT_ASR_VAD_SILENCE_DURATION_MS")
    if silence_ms:
        parameters["max_sentence_silence"] = int(silence_ms)

    language = get_optional_env("VINPUT_ASR_LANGUAGE")
    if language:
        parameters["language_hints"] = [language]

    return parameters


def build_context(prompt: str) -> list[dict[str, Any]]:
    text = normalize_transcript_text(prompt)[:MAX_CONTEXT_TEXT_LENGTH]
    if not text:
        return []
    return [
        {
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        }
    ]


def build_run_task(
    task_id: str,
    model: str,
    parameters: dict[str, Any],
    prompt: str = "",
) -> dict[str, Any]:
    run_task: dict[str, Any] = {
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
    context = build_context(prompt)
    if context:
        run_task["payload"]["input"]["context"] = context
    return run_task


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


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
    if sentence is None:
        return

    if bool(sentence.get("heartbeat", False)):
        return

    text = normalize_transcript_text(str(sentence.get("text", "")))
    if not text:
        return

    if bool(sentence.get("sentence_end", False)):
        commit_sentence(state, text)
    else:
        state.current_partial_text = text
        emit_partial(state)


def handle_task_failed(message: dict[str, Any], state: SessionState) -> None:
    if state.has_usable_final():
        return
    if emit_fallback_final(state):
        return

    header = message.get("header")
    error_message = "Bailian ASR task failed."
    if isinstance(header, dict):
        code = header.get("error_code")
        candidate = header.get("error_message")
        if isinstance(code, str) and code.strip():
            error_message = f"{code}: {candidate}" if isinstance(candidate, str) and candidate.strip() else code
        elif isinstance(candidate, str) and candidate.strip():
            error_message = candidate.strip()
    write_stdout({"type": "error", "message": error_message})
    state.error = error_message


def handle_server_message(message: dict[str, Any], state: SessionState) -> None:
    header = message.get("header")
    if not isinstance(header, dict):
        write_stderr(f"Ignoring unexpected Bailian message: {json.dumps(message, ensure_ascii=False)}")
        return

    event = str(header.get("event", "")).strip()
    if event == "task-started":
        if not state.session_started:
            write_stdout(
                {
                    "type": "session_started",
                    "session_id": state.task_id,
                    "config": {},
                }
            )
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

    write_stderr(f"Unhandled Bailian event: {event or '<missing>'}")


def run() -> int:
    api_key = get_required_env("VINPUT_ASR_API_KEY")
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS)
    endpoint = resolve_endpoint()

    client = WebSocketClient(endpoint, {"Authorization": f"Bearer {api_key}"}, timeout)
    state = SessionState(task_id=str(uuid.uuid4()))

    prompt = get_optional_env("VINPUT_ASR_PROMPT")
    client.send_text(build_run_task(state.task_id, model, build_parameters(), prompt=prompt))
    stop_event = threading.Event()

    def reader() -> None:
        try:
            while not stop_event.is_set():
                raw_message = client.recv_message()
                if raw_message is None:
                    if not stop_event.is_set() and not state.server_finished and not state.closed:
                        state.error = "WebSocket connection closed unexpectedly."
                        write_stdout({"type": "error", "message": state.error})
                    break
                try:
                    message = json.loads(raw_message.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # Servers may push binary control data; ignore non-JSON frames.
                    continue
                if not isinstance(message, dict):
                    continue
                started_before = state.session_started
                handle_server_message(message, state)
                if not started_before and state.session_started:
                    # Audio may only be sent after task-started; flush buffered audio.
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
                write_stdout({"type": "error", "message": str(exc)})
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
                raise ValueError(f"Invalid JSON input: {exc}") from exc

            event_type = str(event.get("type", "")).strip()
            if event_type == "audio":
                audio_base64 = event.get("audio_base64")
                if not isinstance(audio_base64, str) or not audio_base64:
                    raise ValueError("audio event requires non-empty audio_base64.")
                try:
                    audio = base64.b64decode(audio_base64)
                except ValueError as exc:
                    raise ValueError(f"audio event has invalid base64: {exc}") from exc
                if not audio:
                    raise ValueError("audio event decoded to empty audio.")
                # The commit flag has no server-side meaning in this protocol:
                # sentence boundaries are decided by the server (VAD / semantic).
                with state.lock:
                    if state.session_started:
                        client.send_binary(audio)
                    else:
                        state.pending_audio.extend(audio)
                continue

            if event_type == "finish":
                saw_finish = True
                client.send_text(build_finish_task(state.task_id))
                break

            if event_type == "cancel":
                stop_event.set()
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if saw_finish and not stop_event.is_set():
            thread.join(timeout=finish_grace_secs)
        emit_fallback_final(state)
        stop_event.set()
        client.close()
        thread.join(timeout=1.0)
        if not state.closed:
            write_stdout({"type": "closed"})
            state.closed = True

    if state.error:
        return EXIT_RUNTIME_ERROR
    return 0


def main() -> int:
    try:
        return run()
    except ValueError as exc:
        write_stderr(str(exc))
        return EXIT_USAGE_ERROR
    except Exception as exc:
        write_stderr(str(exc))
        return EXIT_RUNTIME_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
