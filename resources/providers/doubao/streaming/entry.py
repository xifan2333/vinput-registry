#!/usr/bin/env python3
"""Doubao (ByteDance OpenSpeech) duplex streaming speech-to-text provider via WebSocket binary protocol."""

from __future__ import annotations

import base64
import gzip
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
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
DEFAULT_MODEL = "bigmodel"
DEFAULT_RESOURCE_ID = "volc.seedasr.sauc.duration"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 1.0
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

EXIT_SUCCESS = 0
EXIT_RUNTIME_ERROR = 1
EXIT_USAGE_ERROR = 2

PROTOCOL_VERSION = 0x1
HEADER_SIZE_WORDS = 0x1

MESSAGE_TYPE_FULL_CLIENT_REQUEST = 0x1
MESSAGE_TYPE_AUDIO_ONLY_REQUEST = 0x2
MESSAGE_TYPE_FULL_SERVER_RESPONSE = 0x9
MESSAGE_TYPE_SERVER_ERROR = 0xF

FLAGS_NONE = 0x0
FLAGS_SEQUENCE = 0x1
FLAGS_LAST_PACKET = 0x2
FLAGS_LAST_PACKET_WITH_SEQUENCE = 0x3

SERIALIZATION_NONE = 0x0
SERIALIZATION_JSON = 0x1

COMPRESSION_NONE = 0x0
COMPRESSION_GZIP = 0x1

MESSAGES: dict[str, dict[str, str]] = {
    "en_US": {
        "title": "Doubao Streaming ASR",
        "err_missing_credentials": (
            "Missing Doubao credentials. Set VINPUT_ASR_APP_ID with VINPUT_ASR_ACCESS_TOKEN, "
            "or set VINPUT_ASR_API_KEY to 'app_id:access_key'."
        ),
        "err_invalid_event": "Invalid input event: {detail}",
        "err_network": "Doubao streaming network error: {detail}",
        "err_upstream": "Doubao streaming server error {code}: {detail}",
        "err_runtime": "Doubao streaming runtime error: {detail}",
    },
    "zh_CN": {
        "title": "豆包实时语音识别",
        "err_missing_credentials": (
            "缺少豆包认证信息。请配置 VINPUT_ASR_APP_ID 与 VINPUT_ASR_ACCESS_TOKEN，"
            "或将 VINPUT_ASR_API_KEY 设为 'app_id:access_key'。"
        ),
        "err_invalid_event": "输入事件格式错误：{detail}",
        "err_network": "豆包流式语音识别网络连接失败：{detail}",
        "err_upstream": "豆包流式语音服务端报错 {code}：{detail}",
        "err_runtime": "豆包流式语音运行异常：{detail}",
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


@dataclass
class ProviderState:
    error: str | None = None
    closed: bool = False
    cancelled: bool = False
    confirmed_text: str = ""
    latest_partial_text: str = ""
    last_final_text: str = ""
    final_received: bool = False
    final_emitted: bool = False
    upstream_error: bool = False
    session_started: bool = False

    def get_last_final_text(self) -> str:
        return normalize_transcript_text(self.last_final_text)

    def get_confirmed_text(self) -> str:
        return normalize_transcript_text(self.confirmed_text)

    def has_pending_partial(self) -> bool:
        return normalize_transcript_text(self.latest_partial_text) != self.get_confirmed_text()


@dataclass(frozen=True)
class StreamingConfig:
    app_id: str
    access_key: str
    url: str
    model: str
    resource_id: str
    user_id: str
    timeout: int
    finish_grace_secs: float
    request_payload: dict[str, Any]


def get_optional_env(name: str, default: str = "") -> str:
    return os.getenv(name, "").strip() or default


def get_optional_int_env(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be an integer.") from exc


def get_optional_float_env(name: str, default: float | None = None) -> float | None:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must be a number.") from exc


def get_optional_bool_env(name: str, default: bool | None = None) -> bool | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def get_optional_json_object_env(name: str) -> dict[str, Any] | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError("err_runtime", detail=f"{name} must contain valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ConfigError("err_runtime", detail=f"{name} must contain a JSON object.")
    return data


def resolve_credentials() -> tuple[str, str]:
    app_id = get_optional_env("VINPUT_ASR_APP_ID")
    access_key = get_optional_env("VINPUT_ASR_ACCESS_TOKEN")
    if app_id and access_key:
        return app_id, access_key

    combined = get_optional_env("VINPUT_ASR_API_KEY")
    if combined:
        if ":" in combined:
            candidate_app_id, candidate_access_key = combined.split(":", 1)
            candidate_app_id = candidate_app_id.strip()
            candidate_access_key = candidate_access_key.strip()
            if candidate_app_id and candidate_access_key:
                return candidate_app_id, candidate_access_key
        if app_id:
            return app_id, combined

    raise ConfigError("err_missing_credentials")


def coerce_result_type(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"0", "full"}:
        return "full"
    if lowered in {"1", "single"}:
        return "single"
    return value.strip()


def build_request_payload(model: str, user_id: str) -> dict[str, Any]:
    request: dict[str, Any] = {}
    request_override = get_optional_json_object_env("VINPUT_ASR_REQUEST_JSON")
    if request_override:
        request.update(request_override)

    request["model_name"] = model

    language = get_optional_env("VINPUT_ASR_LANGUAGE")
    enable_nonstream = get_optional_bool_env("VINPUT_ASR_ENABLE_NONSTREAM")
    enable_itn = get_optional_bool_env("VINPUT_ASR_ENABLE_ITN")
    enable_punc = get_optional_bool_env("VINPUT_ASR_ENABLE_PUNC")
    enable_ddc = get_optional_bool_env("VINPUT_ASR_ENABLE_DDC")
    show_utterances = get_optional_bool_env("VINPUT_ASR_SHOW_UTTERANCES")
    show_speech_rate = get_optional_bool_env("VINPUT_ASR_SHOW_SPEECH_RATE")
    show_volume = get_optional_bool_env("VINPUT_ASR_SHOW_VOLUME")
    enable_lid = get_optional_bool_env("VINPUT_ASR_ENABLE_LID")
    enable_emotion_detection = get_optional_bool_env("VINPUT_ASR_ENABLE_EMOTION_DETECTION")
    enable_gender_detection = get_optional_bool_env("VINPUT_ASR_ENABLE_GENDER_DETECTION")
    end_window_size = get_optional_int_env("VINPUT_ASR_END_WINDOW_SIZE")
    vad_segment_duration = get_optional_int_env("VINPUT_ASR_VAD_SEGMENT_DURATION")
    force_to_speech_time = get_optional_int_env("VINPUT_ASR_FORCE_TO_SPEECH_TIME")

    if end_window_size is None and get_optional_bool_env("VINPUT_ASR_ENABLE_VAD"):
        end_window_size = get_optional_int_env("VINPUT_ASR_VAD_SILENCE_DURATION_MS", 800)

    if enable_nonstream is not None:
        request["enable_nonstream"] = enable_nonstream
    if enable_itn is not None:
        request["enable_itn"] = enable_itn
    if enable_punc is not None:
        request["enable_punc"] = enable_punc
    if enable_ddc is not None:
        request["enable_ddc"] = enable_ddc
    if show_speech_rate is not None:
        request["show_speech_rate"] = show_speech_rate
    if show_volume is not None:
        request["show_volume"] = show_volume
    if enable_lid is not None:
        request["enable_lid"] = enable_lid
    if enable_emotion_detection is not None:
        request["enable_emotion_detection"] = enable_emotion_detection
    if enable_gender_detection is not None:
        request["enable_gender_detection"] = enable_gender_detection
    if end_window_size is not None:
        request["end_window_size"] = end_window_size
    if vad_segment_duration is not None:
        request["vad_segment_duration"] = vad_segment_duration
    if force_to_speech_time is not None:
        request["force_to_speech_time"] = force_to_speech_time

    result_type = get_optional_env("VINPUT_ASR_RESULT_TYPE")
    if result_type:
        request["result_type"] = coerce_result_type(result_type)

    if show_utterances is not None:
        request["show_utterances"] = show_utterances
    elif any(
        value is not None
        for value in (
            show_speech_rate,
            show_volume,
            enable_lid,
            enable_emotion_detection,
            enable_gender_detection,
            end_window_size,
            vad_segment_duration,
            force_to_speech_time,
        )
    ):
        request["show_utterances"] = True

    payload: dict[str, Any] = {
        "user": {
            "uid": user_id,
        },
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": request,
    }
    if language:
        payload["audio"]["language"] = language
    return payload


def load_config() -> StreamingConfig:
    app_id, access_key = resolve_credentials()
    url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    resource_id = get_optional_env("VINPUT_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID)
    user_id = get_optional_env("VINPUT_ASR_USER_ID", app_id)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT
    finish_grace_secs = (
        get_optional_float_env("VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS) or DEFAULT_FINISH_GRACE_SECS
    )

    request_payload = build_request_payload(model, user_id)
    return StreamingConfig(
        app_id=app_id,
        access_key=access_key,
        url=url,
        model=model,
        resource_id=resource_id,
        user_id=user_id,
        timeout=timeout,
        finish_grace_secs=finish_grace_secs,
        request_payload=request_payload,
    )


def pack_protocol_header(
    message_type: int,
    flags: int,
    serialization: int,
    compression: int,
) -> bytes:
    return bytes(
        [
            ((PROTOCOL_VERSION & 0x0F) << 4) | (HEADER_SIZE_WORDS & 0x0F),
            ((message_type & 0x0F) << 4) | (flags & 0x0F),
            ((serialization & 0x0F) << 4) | (compression & 0x0F),
            0x00,
        ]
    )


def build_full_client_request(payload: dict[str, Any]) -> bytes:
    raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    compressed = gzip.compress(raw_payload)
    return (
        pack_protocol_header(
            MESSAGE_TYPE_FULL_CLIENT_REQUEST,
            FLAGS_NONE,
            SERIALIZATION_JSON,
            COMPRESSION_GZIP,
        )
        + struct.pack("!I", len(compressed))
        + compressed
    )


def build_audio_request(audio_bytes: bytes, *, final: bool) -> bytes:
    compressed = gzip.compress(audio_bytes)
    flags = FLAGS_LAST_PACKET if final else FLAGS_NONE
    return (
        pack_protocol_header(
            MESSAGE_TYPE_AUDIO_ONLY_REQUEST,
            flags,
            SERIALIZATION_NONE,
            COMPRESSION_GZIP,
        )
        + struct.pack("!I", len(compressed))
        + compressed
    )


def extract_error_message(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        error = payload.get("error")
        if isinstance(error, dict):
            return extract_error_message(error)
    if isinstance(payload, bytes):
        text = payload.decode("utf-8", errors="replace").strip()
        if text:
            return text
    if isinstance(payload, str):
        text = payload.strip()
        if text:
            return text
    return "Unknown Doubao ASR error."


def extract_result_fields(
    payload: dict[str, Any],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    transcript = ""
    utterances: list[dict[str, Any]] = []
    words: list[dict[str, Any]] = []

    result = payload.get("result")
    if isinstance(result, dict):
        text = result.get("text")
        if isinstance(text, str):
            transcript = text.strip()
        candidate_utterances = result.get("utterances")
        if isinstance(candidate_utterances, list):
            utterances = [item for item in candidate_utterances if isinstance(item, dict)]
    elif isinstance(result, list):
        text_parts = []
        for item in result:
            if not isinstance(item, dict):
                continue
            candidate_text = item.get("text")
            if isinstance(candidate_text, str) and candidate_text.strip():
                text_parts.append(candidate_text.strip())
            if not utterances:
                candidate_utterances = item.get("utterances")
                if isinstance(candidate_utterances, list):
                    utterances = [utterance for utterance in candidate_utterances if isinstance(utterance, dict)]
        transcript = " ".join(text_parts).strip()

    top_level_words = payload.get("words")
    if isinstance(top_level_words, list):
        words = [item for item in top_level_words if isinstance(item, dict)]
    elif utterances:
        for utterance in utterances:
            candidate_words = utterance.get("words")
            if isinstance(candidate_words, list):
                words.extend([item for item in candidate_words if isinstance(item, dict)])

    audio_info = payload.get("audio_info")
    if not isinstance(audio_info, dict):
        audio_info = None

    return transcript, utterances, words, audio_info


class WebSocketClient:
    def __init__(self, url: str, headers: dict[str, str], timeout: int) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"ws", "wss"}:
            raise VInputError("err_network", detail="WebSocket URL must use ws:// or wss://.")
        if not parsed.hostname:
            raise VInputError("err_network", detail="WebSocket URL is missing a hostname.")

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

        response, overflow = self._read_http_response(sock)
        self._recv_buffer = overflow
        self._validate_handshake(response, key)
        return sock

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
            log_id = headers.get("x-tt-logid")
            if log_id:
                details.append(f"log_id={log_id}")
            connect_id = headers.get("x-api-connect-id")
            if connect_id:
                details.append(f"connect_id={connect_id}")
            raise RuntimeError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(hashlib.sha1((key + GUID).encode("utf-8")).digest()).decode("ascii")
        if accept != expected:
            raise RuntimeError("WebSocket handshake failed: invalid Sec-WebSocket-Accept header.")
        self.response_headers = headers

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

    def send_binary(self, payload: bytes) -> None:
        self._send_frame(0x2, payload)

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

            if not fin:
                continue

            if current_opcode is None:
                continue
            return current_opcode, bytes(fragments)

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


@dataclass
class DecodedServerPacket:
    message_type: int
    flags: int
    payload: Any = None
    sequence: int | None = None
    error_code: int | None = None
    is_final: bool = False


def decode_payload(payload: bytes, serialization: int, compression: int) -> Any:
    if compression == COMPRESSION_GZIP:
        payload = gzip.decompress(payload)
    elif compression != COMPRESSION_NONE:
        raise RuntimeError(f"Unsupported compression type from Doubao: {compression}")

    if serialization == SERIALIZATION_JSON:
        return json.loads(payload.decode("utf-8"))
    if serialization == SERIALIZATION_NONE:
        return payload
    raise RuntimeError(f"Unsupported serialization type from Doubao: {serialization}")


def decode_server_packet(raw: bytes) -> DecodedServerPacket:
    if len(raw) < 4:
        raise RuntimeError("Doubao server packet is shorter than the protocol header.")

    version = raw[0] >> 4
    header_words = raw[0] & 0x0F
    if version != PROTOCOL_VERSION:
        raise RuntimeError(f"Unsupported Doubao protocol version: {version}")

    header_size = header_words * 4
    if len(raw) < header_size:
        raise RuntimeError("Doubao server packet is shorter than the declared header.")

    message_type = raw[1] >> 4
    flags = raw[1] & 0x0F
    serialization = raw[2] >> 4
    compression = raw[2] & 0x0F
    offset = header_size

    if message_type == MESSAGE_TYPE_FULL_SERVER_RESPONSE:
        sequence = None
        if flags in {FLAGS_SEQUENCE, FLAGS_LAST_PACKET_WITH_SEQUENCE}:
            if len(raw) < offset + 4:
                raise RuntimeError("Doubao server packet is missing the sequence field.")
            sequence = struct.unpack("!i", raw[offset : offset + 4])[0]
            offset += 4

        if len(raw) < offset + 4:
            raise RuntimeError("Doubao server packet is missing the payload size field.")
        payload_size = struct.unpack("!I", raw[offset : offset + 4])[0]
        offset += 4

        payload = raw[offset : offset + payload_size]
        if len(payload) != payload_size:
            raise RuntimeError("Doubao server packet payload size does not match header.")

        return DecodedServerPacket(
            message_type=message_type,
            flags=flags,
            payload=decode_payload(payload, serialization, compression),
            sequence=sequence,
            is_final=flags in {FLAGS_LAST_PACKET, FLAGS_LAST_PACKET_WITH_SEQUENCE},
        )

    if message_type == MESSAGE_TYPE_SERVER_ERROR:
        if len(raw) < offset + 8:
            raise RuntimeError("Doubao error packet is missing error metadata.")

        error_code = struct.unpack("!I", raw[offset : offset + 4])[0]
        error_size = struct.unpack("!I", raw[offset + 4 : offset + 8])[0]
        offset += 8

        payload = raw[offset : offset + error_size]
        if len(payload) != error_size:
            raise RuntimeError("Doubao error packet payload size does not match header.")

        decoded_payload = decode_payload(payload, serialization, compression)
        return DecodedServerPacket(
            message_type=message_type,
            flags=flags,
            payload=decoded_payload,
            error_code=error_code,
        )

    raise RuntimeError(f"Unsupported Doubao server message type: {message_type}")


# Global runtime singletons
_CONFIG: StreamingConfig | None = None
_CLIENT: WebSocketClient | None = None
_STATE: ProviderState = ProviderState()
_STOP_EVENT = threading.Event()
_FINAL_EVENT = threading.Event()
_SENT_FINAL: bool = False


def connect(config: StreamingConfig) -> None:
    global _CONFIG, _CLIENT, _STATE, _STOP_EVENT, _FINAL_EVENT, _SENT_FINAL
    _CONFIG = config
    _STATE = ProviderState()
    _STOP_EVENT.clear()
    _FINAL_EVENT.clear()
    _SENT_FINAL = False

    connect_id = str(uuid.uuid4())
    headers = {
        "X-Api-App-Key": config.app_id,
        "X-Api-Access-Key": config.access_key,
        "X-Api-Resource-Id": config.resource_id,
        "X-Api-Connect-Id": connect_id,
    }
    _CLIENT = WebSocketClient(config.url, headers, config.timeout)
    session_id = _CLIENT.response_headers.get("x-api-connect-id") or connect_id
    emit_session_started(session_id)
    _STATE.session_started = True

    _CLIENT.send_binary(build_full_client_request(config.request_payload))


def on_audio_chunk(pcm_bytes: bytes, is_commit: bool) -> None:
    global _SENT_FINAL
    if not _CLIENT or _STATE.cancelled:
        return
    _CLIENT.send_binary(build_audio_request(pcm_bytes, final=is_commit))
    if is_commit:
        _SENT_FINAL = True


def on_finish() -> None:
    global _SENT_FINAL
    if not _CLIENT or _STATE.cancelled:
        return
    if not _SENT_FINAL:
        _CLIENT.send_binary(build_audio_request(b"", final=True))
        _SENT_FINAL = True


def on_cancel() -> None:
    _STATE.cancelled = True
    cleanup()


def on_upstream_message(raw_msg: str | bytes) -> None:
    if isinstance(raw_msg, str):
        raw_msg = raw_msg.encode("utf-8")

    packet = decode_server_packet(raw_msg)
    if packet.message_type == MESSAGE_TYPE_FULL_SERVER_RESPONSE:
        if not isinstance(packet.payload, dict):
            if packet.is_final:
                _STATE.final_received = True
                _FINAL_EVENT.set()
            return

        transcript, _utterances, _words, _audio_info = extract_result_fields(packet.payload)
        if packet.is_final:
            final_text = combine_transcript(_STATE.confirmed_text, transcript)
            if final_text:
                _STATE.latest_partial_text = final_text
                _STATE.confirmed_text = final_text
            _STATE.final_received = True
            _FINAL_EVENT.set()
            return

        if transcript:
            combined = combine_transcript(_STATE.confirmed_text, transcript)
            if combined and combined != _STATE.latest_partial_text:
                _STATE.latest_partial_text = combined
                emit_partial(combined)

    elif packet.message_type == MESSAGE_TYPE_SERVER_ERROR:
        error_message = extract_error_message(packet.payload)
        code = str(packet.error_code or "unknown")
        notify_error("err_upstream", code=code, detail=error_message)
        _STATE.upstream_error = True
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
    config = load_config()

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
                    break
                opcode, payload = message
                if opcode == 0x1:
                    text = payload.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    try:
                        decoded = json.loads(text)
                    except json.JSONDecodeError:
                        decoded = text
                    error_message = extract_error_message(decoded)
                    notify_error("err_upstream", code="1", detail=error_message)
                    _STATE.upstream_error = True
                    _FINAL_EVENT.set()
                    return

                if opcode != 0x2:
                    continue

                on_upstream_message(payload)
                if _STATE.final_received:
                    break
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
            if _STATE.cancelled or _STOP_EVENT.is_set():
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
        if saw_finish and not _STATE.cancelled:
            _FINAL_EVENT.wait(timeout=config.finish_grace_secs)

        if not _STATE.cancelled and _STATE.latest_partial_text and not _STATE.final_emitted:
            emit_final(_STATE.latest_partial_text)
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
