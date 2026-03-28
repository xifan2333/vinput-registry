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
from typing import Any, Dict, Optional
from urllib.parse import urlencode, urlparse

DEFAULT_MODEL_ID = "scribe_v2_realtime"
DEFAULT_URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 2.0
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2


def write_stdout(event: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(event, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def write_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


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
                raise RuntimeError(f"Invalid JSON message from ElevenLabs: {exc}") from exc

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


def build_url() -> str:
    base_url = get_optional_env("ELEVENLABS_STREAM_URL", DEFAULT_URL)
    params = {
        "model_id": get_optional_env("ELEVENLABS_MODEL_ID", DEFAULT_MODEL_ID),
        "include_timestamps": str(
            get_optional_bool_env("ELEVENLABS_INCLUDE_TIMESTAMPS", True)
        ).lower(),
        "include_language_detection": str(
            get_optional_bool_env("ELEVENLABS_INCLUDE_LANGUAGE_DETECTION", False)
        ).lower(),
        "audio_format": get_optional_env("ELEVENLABS_AUDIO_FORMAT", "pcm_16000"),
        "commit_strategy": get_optional_env("ELEVENLABS_COMMIT_STRATEGY", "manual"),
        "enable_logging": str(
            get_optional_bool_env("ELEVENLABS_ENABLE_LOGGING", True)
        ).lower(),
    }

    language = get_optional_env("ELEVENLABS_LANGUAGE")
    if language:
        params["language_code"] = language

    if params["commit_strategy"] == "vad":
        params["vad_silence_threshold_secs"] = str(
            get_optional_float_env("ELEVENLABS_VAD_SILENCE_THRESHOLD_SECS", 1.5)
        )
        params["vad_threshold"] = str(
            get_optional_float_env("ELEVENLABS_VAD_THRESHOLD", 0.4)
        )
        params["min_speech_duration_ms"] = str(
            get_optional_int_env("ELEVENLABS_MIN_SPEECH_DURATION_MS", 100)
        )
        params["min_silence_duration_ms"] = str(
            get_optional_int_env("ELEVENLABS_MIN_SILENCE_DURATION_MS", 100)
        )

    separator = "&" if "?" in base_url else "?"
    return base_url + separator + urlencode(params)


def handle_server_message(message: Dict[str, Any], state: Dict[str, Any]) -> None:
    message_type = str(message.get("message_type", "")).strip()

    if message_type == "session_started":
        state["session_started"] = True
        write_stdout(
            {
                "type": "session_started",
                "session_id": message.get("session_id", ""),
                "config": message.get("config", {}),
            }
        )
        return

    if message_type == "partial_transcript":
        write_stdout({"type": "partial", "text": str(message.get("text", ""))})
        return

    if message_type == "committed_transcript":
        write_stdout({"type": "final", "text": str(message.get("text", ""))})
        return

    if message_type == "committed_transcript_with_timestamps":
        event: Dict[str, Any] = {
            "type": "final_timestamps",
            "text": str(message.get("text", "")),
            "words": message.get("words", []),
        }
        language_code = message.get("language_code")
        if language_code is not None:
            event["language_code"] = language_code
        write_stdout(event)
        return

    if message_type.endswith("error") or message_type == "error":
        error_text = str(message.get("error", "Unknown ElevenLabs error."))
        event: Dict[str, Any] = {
            "type": "error",
            "message": error_text,
            "source_type": message_type,
        }
        if "code" in message:
            event["code"] = message["code"]
        write_stdout(event)
        state["error"] = error_text
        return

    write_stderr("Ignoring ElevenLabs message: " + json.dumps(message, ensure_ascii=False))


def run() -> int:
    api_key = get_required_env("ELEVENLABS_API_KEY")
    timeout = get_optional_int_env("ELEVENLABS_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env(
        "ELEVENLABS_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS
    )
    url = build_url()

    client = WebSocketClient(url, {"xi-api-key": api_key}, timeout)
    state: Dict[str, Any] = {"session_started": False, "error": None, "closed": False}
    stop_event = threading.Event()

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

                payload: Dict[str, Any] = {
                    "message_type": "input_audio_chunk",
                    "audio_base_64": audio_base64,
                    "commit": bool(event.get("commit", False)),
                    "sample_rate": int(event.get("sample_rate", 16000)),
                }
                previous_text = event.get("previous_text")
                if isinstance(previous_text, str) and previous_text:
                    payload["previous_text"] = previous_text
                client.send_json(payload)
                continue

            if event_type == "finish":
                saw_finish = True
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
