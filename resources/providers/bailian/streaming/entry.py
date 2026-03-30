#!/usr/bin/env python3

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
from typing import Any, Dict, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/realtime"
DEFAULT_MODEL = "qwen-asr-realtime-v1"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 0.4
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2


def write_stdout(event: Dict[str, Any]) -> None:
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
    return committed + " " + current


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


def new_event_id() -> str:
    return "event_" + uuid.uuid4().hex


class WebSocketClient:
    def __init__(self, url: str, headers: Dict[str, str], timeout: int) -> None:
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
            raise RuntimeError(
                f"WebSocket handshake failed: {lines[0] if lines else 'invalid response'}"
            )

        headers: Dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode("utf-8")).digest()
        ).decode("ascii")
        if accept != expected:
            raise RuntimeError(
                "WebSocket handshake failed: invalid Sec-WebSocket-Accept header."
            )

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

    def send_json(self, payload: Dict[str, Any]) -> None:
        self._send_frame(0x1, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def recv_json(self) -> Optional[Dict[str, Any]]:
        fragments = bytearray()
        current_opcode: Optional[int] = None

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
            if opcode not in {0x0, 0x1}:
                continue

            if opcode == 0x1:
                current_opcode = opcode
                fragments = bytearray(payload)
            else:
                if current_opcode is None:
                    continue
                fragments.extend(payload)

            if not fin:
                continue

            text = fragments.decode("utf-8", errors="replace")
            try:
                return json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON message from Bailian: {exc}") from exc

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

    def _recv_frame(self) -> Optional[tuple[int, bytes, bool]]:
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

        payload = self._recv_exact(length)
        if payload is None:
            return None

        if masked:
            payload = bytes(payload[i] ^ mask_key[i % 4] for i in range(length))

        return opcode, payload, fin

    def _recv_exact(self, size: int) -> Optional[bytes]:
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


def build_url(model: str) -> str:
    base_url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    parsed = urlparse(base_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("model", model)
    return urlunparse(parsed._replace(query=urlencode(query)))


def build_session_update_event() -> Dict[str, Any]:
    session: Dict[str, Any] = {
        "input_audio_format": "pcm",
        "sample_rate": 16000,
        "input_audio_transcription": {},
    }

    prompt = get_optional_env("VINPUT_ASR_PROMPT")
    if prompt:
        session["input_audio_transcription"]["prompt"] = prompt

    language = get_optional_env("VINPUT_ASR_LANGUAGE")
    if language:
        session["input_audio_transcription"]["language"] = language

    if get_optional_bool_env("VINPUT_ASR_ENABLE_VAD", False):
        session["turn_detection"] = {
            "type": "server_vad",
            "threshold": get_optional_float_env("VINPUT_ASR_VAD_THRESHOLD", 0.0),
            "prefix_padding_ms": get_optional_int_env(
                "VINPUT_ASR_VAD_PREFIX_PADDING_MS", 300
            ),
            "silence_duration_ms": get_optional_int_env(
                "VINPUT_ASR_VAD_SILENCE_DURATION_MS", 400
            ),
        }
    else:
        session["turn_detection"] = None

    return {
        "event_id": new_event_id(),
        "type": "session.update",
        "session": session,
    }


def handle_server_message(message: Dict[str, Any], state: Dict[str, Any]) -> None:
    message_type = str(message.get("type", "")).strip()

    if message_type in {"session.created", "session.updated"}:
        session = message.get("session", {})
        session_id = ""
        if isinstance(session, dict):
            session_id = str(session.get("id", ""))
        if not state.get("session_started"):
            write_stdout(
                {
                    "type": "session_started",
                    "session_id": session_id,
                    "config": session if isinstance(session, dict) else {},
                }
            )
        state["session_started"] = True
        return

    if message_type == "input_audio_buffer.committed":
        item_id = str(message.get("item_id", "")).strip()
        if item_id:
            state["last_item_id"] = item_id
        return

    if message_type == "conversation.item.input_audio_transcription.text":
        item_id = str(message.get("item_id", "")).strip() or str(
            state.get("last_item_id", "")
        ).strip()
        preview_text = str(message.get("text", "")) + str(message.get("stash", ""))
        if item_id:
            state["partials"][item_id] = preview_text
        write_stdout(
            {
                "type": "partial",
                "text": combine_transcript(
                    str(state.get("confirmed_text", "")),
                    preview_text,
                ),
            }
        )
        return

    if message_type == "conversation.item.input_audio_transcription.completed":
        item_id = str(message.get("item_id", "")).strip()
        transcript = str(message.get("transcript", "")).strip()
        full_text = combine_transcript(str(state.get("confirmed_text", "")), transcript)
        state["confirmed_text"] = full_text
        if item_id:
            state["partials"].pop(item_id, None)
        event: Dict[str, Any] = {"type": "final", "text": full_text, "segment_final": True}
        language = message.get("language")
        if language is not None:
            event["language"] = language
        emotion = message.get("emotion")
        if emotion is not None:
            event["emotion"] = emotion
        write_stdout(event)
        return

    if message_type == "conversation.item.input_audio_transcription.failed":
        error = message.get("error")
        error_message = "Input audio transcription failed."
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                error_message = candidate.strip()
        write_stdout({"type": "error", "message": error_message})
        state["error"] = error_message
        return

    if message_type == "session.finished":
        state["server_finished"] = True
        return

    if message_type == "error":
        error = message.get("error")
        error_message = "Unknown Bailian error."
        if isinstance(error, dict):
            candidate = error.get("message")
            if isinstance(candidate, str) and candidate.strip():
                error_message = candidate.strip()
        write_stdout({"type": "error", "message": error_message})
        state["error"] = error_message
        return


def run() -> int:
    api_key = get_required_env("VINPUT_ASR_API_KEY")
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env(
        "VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS
    )
    url = build_url(model)

    client = WebSocketClient(url, {"Authorization": f"Bearer {api_key}"}, timeout)
    client.send_json(build_session_update_event())

    state: Dict[str, Any] = {
        "session_started": False,
        "error": None,
        "closed": False,
        "confirmed_text": "",
        "last_item_id": "",
        "partials": {},
        "server_finished": False,
    }
    stop_event = threading.Event()

    def reader() -> None:
        try:
            while not stop_event.is_set():
                message = client.recv_json()
                if message is None:
                    break
                handle_server_message(message, state)
                if state.get("server_finished"):
                    break
        except Exception as exc:
            if not stop_event.is_set():
                state["error"] = str(exc)
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

                client.send_json(
                    {
                        "event_id": new_event_id(),
                        "type": "input_audio_buffer.append",
                        "audio": audio_base64,
                    }
                )
                if bool(event.get("commit", False)):
                    client.send_json(
                        {
                            "event_id": new_event_id(),
                            "type": "input_audio_buffer.commit",
                        }
                    )
                continue

            if event_type == "finish":
                saw_finish = True
                client.send_json({"event_id": new_event_id(), "type": "session.finish"})
                break

            if event_type == "cancel":
                stop_event.set()
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if saw_finish and not stop_event.is_set():
            thread.join(timeout=finish_grace_secs)
        stop_event.set()
        client.close()
        thread.join(timeout=1.0)
        if not state["closed"]:
            write_stdout({"type": "closed"})
            state["closed"] = True

    if state.get("error"):
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
