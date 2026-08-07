#!/usr/bin/env python3

import array
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
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

DEFAULT_URL = "wss://stt-rt.soniox.com/transcribe-websocket"
DEFAULT_MODEL = "stt-rt-v5"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 1.0
DEFAULT_TARGET_SAMPLE_RATE = 16000
DEFAULT_KEEPALIVE_SECS = 5.0
ENDPOINT_TOKEN = "<end>"
FINALIZE_TOKEN = "<fin>"
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


def get_optional_list_env(*names: str) -> List[str]:
    for name in names:
        value = os.getenv(name, "").strip()
        if not value:
            continue
        items = [item.strip() for item in value.replace(";", ",").split(",")]
        items = [item for item in items if item]
        if items:
            return items
    return []


def resample_pcm16_mono(
    pcm_audio: bytes,
    source_rate: int,
    target_rate: int,
) -> bytes:
    if source_rate == target_rate:
        return pcm_audio
    if source_rate <= 0 or target_rate <= 0:
        raise ValueError("Sample rate must be positive.")
    if len(pcm_audio) % 2 != 0:
        raise ValueError("PCM payload length must be even for 16-bit audio.")

    source = array.array("h")
    source.frombytes(pcm_audio)
    if sys.byteorder != "little":
        source.byteswap()

    if not source:
        return b""

    if len(source) == 1:
        repeated = array.array("h", [source[0]] * max(1, target_rate // source_rate))
        if sys.byteorder != "little":
            repeated.byteswap()
        return repeated.tobytes()

    target_length = max(1, int(round(len(source) * float(target_rate) / float(source_rate))))
    result = array.array("h")
    last_index = len(source) - 1

    for index in range(target_length):
        position = index * last_index / max(1, target_length - 1)
        left = int(position)
        right = min(left + 1, last_index)
        fraction = position - left
        value = int(round(source[left] * (1.0 - fraction) + source[right] * fraction))
        value = max(-32768, min(32767, value))
        result.append(value)

    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


@dataclass
class SessionState:
    session_started: bool = False
    error: Optional[str] = None
    closed: bool = False
    final_text: str = ""
    non_final_text: str = ""
    last_partial_text: str = ""
    last_final_text: str = ""
    server_finished: bool = False
    pending_audio_since_finalize: bool = False

    def get_visible_text(self) -> str:
        return normalize_transcript_text(self.final_text + self.non_final_text)

    def get_last_final_text(self) -> str:
        return normalize_transcript_text(self.last_final_text)

    def has_usable_final(self) -> bool:
        return bool(self.get_last_final_text())

    def record_final_text(self, text: str) -> str:
        final_text = normalize_transcript_text(text)
        self.last_final_text = final_text
        self.last_partial_text = final_text
        return final_text


def emit_partial(state: SessionState) -> None:
    visible_text = state.get_visible_text()
    if not visible_text or visible_text == state.last_partial_text:
        return
    state.last_partial_text = visible_text
    write_stdout({"type": "partial", "text": visible_text})


def emit_final_text(
    state: SessionState,
    text: str,
    *,
    utterance_final: bool = False,
    allow_same_text: bool = False,
) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text:
        return False
    if not allow_same_text and final_text == state.get_last_final_text():
        return False

    write_stdout(
        {
            "type": "final",
            "text": state.record_final_text(final_text),
            "segment_final": True,
            "utterance_final": utterance_final,
        }
    )
    return True


def emit_fallback_final(state: SessionState) -> bool:
    visible_text = state.get_visible_text()
    if not visible_text:
        return False
    return emit_final_text(state, visible_text, utterance_final=True, allow_same_text=True)


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
        self._recv_buffer = b""
        self._closed = False
        self._send_lock = threading.Lock()
        self.socket = self._connect()

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

    def disable_read_timeout(self) -> None:
        self.socket.settimeout(None)

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

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

    def send_end_of_stream(self) -> None:
        self._send_frame(0x1, b"")

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
                raise RuntimeError(f"Invalid JSON message from Soniox: {exc}") from exc

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
        with self._send_lock:
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
    base_url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    parsed = urlparse(base_url)
    scheme = parsed.scheme
    if scheme == "https":
        scheme = "wss"
    elif scheme == "http":
        scheme = "ws"
    return urlunparse(parsed._replace(scheme=scheme))


def build_context() -> Dict[str, Any]:
    context: Dict[str, Any] = {}

    text = get_optional_env("VINPUT_ASR_PROMPT") or get_optional_env("VINPUT_ASR_CONTEXT")
    if text:
        context["text"] = text

    terms = get_optional_list_env("VINPUT_ASR_CONTEXT_TERMS", "VINPUT_ASR_HOTWORDS")
    if terms:
        context["terms"] = terms

    return context


def build_start_request(api_key: str, model: str, sample_rate: int) -> Dict[str, Any]:
    request: Dict[str, Any] = {
        "api_key": api_key,
        "model": model,
        "audio_format": "pcm_s16le",
        "sample_rate": sample_rate,
        "num_channels": 1,
    }

    language_hints = get_optional_list_env(
        "VINPUT_ASR_LANGUAGE_HINTS", "VINPUT_ASR_LANGUAGE"
    )
    if language_hints:
        request["language_hints"] = language_hints
        if get_optional_bool_env("VINPUT_ASR_LANGUAGE_HINTS_STRICT", False):
            request["language_hints_strict"] = True

    context = build_context()
    if context:
        request["context"] = context

    if get_optional_bool_env("VINPUT_ASR_ENABLE_SPEAKER_DIARIZATION", False):
        request["enable_speaker_diarization"] = True

    if get_optional_bool_env("VINPUT_ASR_ENABLE_LANGUAGE_IDENTIFICATION", False):
        request["enable_language_identification"] = True

    if get_optional_bool_env("VINPUT_ASR_ENABLE_ENDPOINT_DETECTION", False):
        request["enable_endpoint_detection"] = True
        if get_optional_env("VINPUT_ASR_MAX_ENDPOINT_DELAY_MS"):
            request["max_endpoint_delay_ms"] = get_optional_int_env(
                "VINPUT_ASR_MAX_ENDPOINT_DELAY_MS", 2000
            )
        if get_optional_env("VINPUT_ASR_ENDPOINT_SENSITIVITY"):
            request["endpoint_sensitivity"] = get_optional_float_env(
                "VINPUT_ASR_ENDPOINT_SENSITIVITY", 0.0
            )
        if get_optional_env("VINPUT_ASR_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL"):
            request["endpoint_latency_adjustment_level"] = get_optional_int_env(
                "VINPUT_ASR_ENDPOINT_LATENCY_ADJUSTMENT_LEVEL", 0
            )

    client_reference_id = get_optional_env("VINPUT_ASR_CLIENT_REFERENCE_ID")
    if client_reference_id:
        request["client_reference_id"] = client_reference_id

    return request


def redact_start_request(request: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in request.items() if key != "api_key"}


def apply_tokens(tokens: List[Any], state: SessionState) -> None:
    non_final_parts: List[str] = []
    endpoint_reached = False
    finalize_reached = False

    for token in tokens:
        if not isinstance(token, dict):
            continue
        text = str(token.get("text", ""))
        if not text:
            continue
        if text == ENDPOINT_TOKEN:
            endpoint_reached = True
            continue
        if text == FINALIZE_TOKEN:
            finalize_reached = True
            continue
        if bool(token.get("is_final", False)):
            state.final_text += text
        else:
            non_final_parts.append(text)

    state.non_final_text = "".join(non_final_parts)

    if endpoint_reached or finalize_reached:
        emit_final_text(state, state.final_text, utterance_final=endpoint_reached)

    emit_partial(state)


def handle_server_message(message: Dict[str, Any], state: SessionState) -> None:
    error_code = message.get("error_code")
    error_message = str(message.get("error_message", "")).strip()

    if error_code is not None or error_message:
        state.server_finished = True
        if state.has_usable_final():
            return
        if emit_fallback_final(state):
            return
        if not error_message:
            error_message = f"Soniox error (code {error_code})."
        write_stdout({"type": "error", "message": error_message})
        state.error = error_message
        return

    tokens = message.get("tokens")
    if isinstance(tokens, list) and tokens:
        apply_tokens(tokens, state)

    if bool(message.get("finished", False)):
        state.server_finished = True


def run() -> int:
    api_key = get_required_env("VINPUT_ASR_API_KEY")
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env(
        "VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS
    )
    target_sample_rate = get_optional_int_env(
        "VINPUT_ASR_TARGET_SAMPLE_RATE", DEFAULT_TARGET_SAMPLE_RATE
    )
    keepalive_secs = get_optional_float_env(
        "VINPUT_ASR_KEEPALIVE_SECS", DEFAULT_KEEPALIVE_SECS
    )
    url = build_url()

    client = WebSocketClient(url, {}, timeout)
    start_request = build_start_request(api_key, model, target_sample_rate)
    client.send_json(start_request)
    client.disable_read_timeout()

    state = SessionState()
    state.session_started = True
    write_stdout(
        {
            "type": "session_started",
            "session_id": "",
            "config": redact_start_request(start_request),
        }
    )

    stop_event = threading.Event()
    last_send_time = [time.monotonic()]

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
            if stop_event.is_set():
                pass
            elif state.has_usable_final():
                write_stderr(f"Soniox terminal exception ignored after final result: {exc}")
            else:
                state.error = str(exc)
                write_stdout({"type": "error", "message": str(exc)})
        finally:
            stop_event.set()

    def keepalive() -> None:
        if keepalive_secs <= 0:
            return
        while not stop_event.wait(0.5):
            if time.monotonic() - last_send_time[0] < keepalive_secs:
                continue
            try:
                client.send_json({"type": "keepalive"})
            except OSError:
                return
            last_send_time[0] = time.monotonic()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    keepalive_thread = threading.Thread(target=keepalive, daemon=True)
    keepalive_thread.start()

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
                    pcm_audio = base64.b64decode(audio_base64)
                except Exception as exc:
                    raise ValueError(f"Invalid audio_base64 payload: {exc}") from exc

                source_rate = int(event.get("sample_rate", DEFAULT_TARGET_SAMPLE_RATE))
                payload_audio = resample_pcm16_mono(
                    pcm_audio=pcm_audio,
                    source_rate=source_rate,
                    target_rate=target_sample_rate,
                )
                if payload_audio:
                    client.send_binary(payload_audio)
                    last_send_time[0] = time.monotonic()
                    state.pending_audio_since_finalize = True

                if bool(event.get("commit", False)) and state.pending_audio_since_finalize:
                    client.send_json({"type": "finalize"})
                    last_send_time[0] = time.monotonic()
                    state.pending_audio_since_finalize = False
                continue

            if event_type == "finish":
                saw_finish = True
                client.send_end_of_stream()
                break

            if event_type == "cancel":
                stop_event.set()
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if saw_finish and not stop_event.is_set():
            thread.join(timeout=finish_grace_secs)
        stop_event.set()
        try:
            client.close()
        finally:
            thread.join(timeout=1.0)
            keepalive_thread.join(timeout=1.0)
        emit_fallback_final(state)
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
