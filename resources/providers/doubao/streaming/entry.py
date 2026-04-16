#!/usr/bin/env python3

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
from typing import Any, Dict, Optional
from urllib.parse import urlparse

DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DEFAULT_MODEL = "bigmodel"
DEFAULT_RESOURCE_ID = "volc.bigasr.sauc.duration"
DEFAULT_TIMEOUT = 30
DEFAULT_FINISH_GRACE_SECS = 1.0
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
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


def get_optional_int_env(name: str, default: Optional[int] = None) -> Optional[int]:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return int(value)


def get_optional_float_env(
    name: str, default: Optional[float] = None
) -> Optional[float]:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    return float(value)


def get_optional_bool_env(
    name: str, default: Optional[bool] = None
) -> Optional[bool]:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def get_optional_json_object_env(name: str) -> Optional[Dict[str, Any]]:
    value = os.getenv(name, "").strip()
    if not value:
        return None

    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(f"{name} must contain a JSON object.")
    return data


def new_connect_id() -> str:
    return str(uuid.uuid4())


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

    raise ValueError(
        "Missing Doubao credentials. Set VINPUT_ASR_APP_ID with "
        "VINPUT_ASR_ACCESS_TOKEN, or set VINPUT_ASR_API_KEY to "
        "'app_id:access_key'."
    )


def coerce_result_type(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"0", "full"}:
        return "full"
    if lowered in {"1", "single"}:
        return "single"
    return value.strip()


def build_request_payload(model: str, user_id: str) -> Dict[str, Any]:
    request: Dict[str, Any] = {}
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
    enable_emotion_detection = get_optional_bool_env(
        "VINPUT_ASR_ENABLE_EMOTION_DETECTION"
    )
    enable_gender_detection = get_optional_bool_env(
        "VINPUT_ASR_ENABLE_GENDER_DETECTION"
    )
    end_window_size = get_optional_int_env("VINPUT_ASR_END_WINDOW_SIZE")
    vad_segment_duration = get_optional_int_env("VINPUT_ASR_VAD_SEGMENT_DURATION")
    force_to_speech_time = get_optional_int_env("VINPUT_ASR_FORCE_TO_SPEECH_TIME")

    # Compat: the previous edge-gateway script exposed a VAD silence env.
    if end_window_size is None and get_optional_bool_env("VINPUT_ASR_ENABLE_VAD"):
        end_window_size = get_optional_int_env(
            "VINPUT_ASR_VAD_SILENCE_DURATION_MS", 800
        )

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

    payload: Dict[str, Any] = {
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


def build_full_client_request(payload: Dict[str, Any]) -> bytes:
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


@dataclass
class ProviderState:
    error: Optional[str] = None
    closed: bool = False
    confirmed_text: str = ""
    latest_partial_text: str = ""
    last_final_text: str = ""

    def get_last_final_text(self) -> str:
        return normalize_transcript_text(self.last_final_text)

    def get_confirmed_text(self) -> str:
        return normalize_transcript_text(self.confirmed_text)

    def has_pending_partial(self) -> bool:
        return (
            normalize_transcript_text(self.latest_partial_text)
            != self.get_confirmed_text()
        )


def emit_partial_text(state: ProviderState, text: str) -> bool:
    partial_text = normalize_transcript_text(text)
    if not partial_text:
        return False
    if partial_text == normalize_transcript_text(state.latest_partial_text):
        return False

    state.latest_partial_text = partial_text
    write_stdout({"type": "partial", "text": partial_text})
    return True


def emit_final_event(
    state: ProviderState,
    text: str,
    *,
    utterances: Optional[list[Dict[str, Any]]] = None,
    words: Optional[list[Dict[str, Any]]] = None,
    audio_info: Optional[Dict[str, Any]] = None,
) -> bool:
    final_text = normalize_transcript_text(text)
    if not final_text or final_text == state.get_last_final_text():
        return False

    state.last_final_text = final_text
    state.confirmed_text = final_text
    state.latest_partial_text = final_text

    event: Dict[str, Any] = {
        "type": "final_timestamps" if utterances or words else "final",
        "text": final_text,
        "segment_final": True,
    }
    if utterances:
        event["utterances"] = utterances
    if words:
        event["words"] = words
    if audio_info:
        event["audio_info"] = audio_info
    write_stdout(event)
    return True


def emit_fallback_final(state: ProviderState) -> bool:
    if not state.has_pending_partial():
        return False
    return emit_final_event(state, state.latest_partial_text)


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
    payload: Dict[str, Any],
) -> tuple[str, list[Dict[str, Any]], list[Dict[str, Any]], Optional[Dict[str, Any]]]:
    transcript = ""
    utterances: list[Dict[str, Any]] = []
    words: list[Dict[str, Any]] = []

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
                    utterances = [
                        utterance
                        for utterance in candidate_utterances
                        if isinstance(utterance, dict)
                    ]
        transcript = " ".join(text_parts).strip()

    top_level_words = payload.get("words")
    if isinstance(top_level_words, list):
        words = [item for item in top_level_words if isinstance(item, dict)]
    elif utterances:
        for utterance in utterances:
            candidate_words = utterance.get("words")
            if isinstance(candidate_words, list):
                words.extend(
                    [item for item in candidate_words if isinstance(item, dict)]
                )

    audio_info = payload.get("audio_info")
    if not isinstance(audio_info, dict):
        audio_info = None

    return transcript, utterances, words, audio_info


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
        self.response_headers: Dict[str, str] = {}
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

        headers: Dict[str, str] = {}
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
            if " 403" in f" {status_line} ":
                details.append(
                    "check VINPUT_ASR_APP_ID / VINPUT_ASR_ACCESS_TOKEN and "
                    "make sure VINPUT_ASR_RESOURCE_ID matches an activated "
                    "OpenSpeech streaming ASR resource"
                )
            raise RuntimeError(", ".join(details))

        accept = headers.get("sec-websocket-accept")
        expected = base64.b64encode(
            hashlib.sha1((key + GUID).encode("utf-8")).digest()
        ).decode("ascii")
        if accept != expected:
            raise RuntimeError(
                "WebSocket handshake failed: invalid Sec-WebSocket-Accept header."
            )
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

    def recv_message(self) -> Optional[tuple[int, bytes]]:
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


@dataclass
class DecodedServerPacket:
    message_type: int
    flags: int
    payload: Any = None
    sequence: Optional[int] = None
    error_code: Optional[int] = None
    is_final: bool = False


def decode_payload(
    payload: bytes, serialization: int, compression: int
) -> Any:
    if compression == COMPRESSION_GZIP:
        payload = gzip.decompress(payload)
    elif compression != COMPRESSION_NONE:
        raise RuntimeError(f"Unsupported compression type from Doubao: {compression}")

    if serialization == SERIALIZATION_JSON:
        return json.loads(payload.decode("utf-8"))
    if serialization == SERIALIZATION_NONE:
        return payload
    raise RuntimeError(
        f"Unsupported serialization type from Doubao: {serialization}"
    )


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


class ActiveStream:
    def __init__(
        self,
        *,
        state: ProviderState,
        url: str,
        app_id: str,
        access_key: str,
        resource_id: str,
        timeout: int,
        request_payload: Dict[str, Any],
    ) -> None:
        self.state = state
        self.url = url
        self.timeout = timeout
        self.request_payload = request_payload
        self.connect_id = new_connect_id()
        self.client = WebSocketClient(
            url,
            {
                "X-Api-App-Key": app_id,
                "X-Api-Access-Key": access_key,
                "X-Api-Resource-Id": resource_id,
                "X-Api-Connect-Id": self.connect_id,
            },
            timeout,
        )
        self.session_id = (
            self.client.response_headers.get("x-api-connect-id") or self.connect_id
        )
        self.log_id = self.client.response_headers.get("x-tt-logid", "")
        self.final_received = threading.Event()
        self.stop_event = threading.Event()
        self.session_started = False
        self.sent_final = False

        self.client.send_binary(build_full_client_request(request_payload))
        self.thread = threading.Thread(target=self._reader, daemon=True)
        self.thread.start()

    def send_audio(self, audio_bytes: bytes, *, final: bool) -> None:
        self.client.send_binary(build_audio_request(audio_bytes, final=final))
        if final:
            self.sent_final = True

    def finish(self, grace_secs: float) -> bool:
        if not self.sent_final:
            self.client.send_binary(build_audio_request(b"", final=True))
            self.sent_final = True

        received_final = self.final_received.wait(timeout=max(grace_secs, 0.0))
        self.stop_event.set()
        self.client.close()
        self.thread.join(timeout=1.0)
        return received_final

    def cancel(self) -> None:
        self.stop_event.set()
        self.client.close()
        self.thread.join(timeout=1.0)

    def _emit_session_started(self) -> None:
        if self.session_started:
            return

        config: Dict[str, Any] = {
            "url": self.url,
            "request": self.request_payload,
            "connect_id": self.connect_id,
        }
        if self.log_id:
            config["log_id"] = self.log_id

        write_stdout(
            {
                "type": "session_started",
                "session_id": self.session_id,
                "config": config,
            }
        )
        self.session_started = True

    def _handle_server_response(self, packet: DecodedServerPacket) -> None:
        self._emit_session_started()

        if not isinstance(packet.payload, dict):
            if packet.is_final:
                emit_fallback_final(self.state)
                self.final_received.set()
            return

        transcript, utterances, words, audio_info = extract_result_fields(packet.payload)
        if packet.is_final:
            final_text = combine_transcript(self.state.confirmed_text, transcript)
            if not normalize_transcript_text(final_text):
                emit_fallback_final(self.state)
            else:
                emit_final_event(
                    self.state,
                    final_text,
                    utterances=utterances or None,
                    words=words or None,
                    audio_info=audio_info,
                )
            self.final_received.set()
            return

        if transcript:
            emit_partial_text(
                self.state,
                combine_transcript(self.state.confirmed_text, transcript),
            )

    def _handle_server_error(self, packet: DecodedServerPacket) -> None:
        if emit_fallback_final(self.state):
            self.final_received.set()
            return

        error_message = extract_error_message(packet.payload)
        if packet.error_code is not None:
            error_message = f"Doubao ASR error {packet.error_code}: {error_message}"
        write_stdout({"type": "error", "message": error_message})
        self.state.error = error_message
        self.final_received.set()

    def _reader(self) -> None:
        try:
            while not self.stop_event.is_set():
                message = self.client.recv_message()
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
                    if emit_fallback_final(self.state):
                        self.final_received.set()
                        return
                    error_message = extract_error_message(decoded)
                    write_stdout({"type": "error", "message": error_message})
                    self.state.error = error_message
                    self.final_received.set()
                    return

                if opcode != 0x2:
                    continue

                packet = decode_server_packet(payload)
                if packet.message_type == MESSAGE_TYPE_FULL_SERVER_RESPONSE:
                    self._handle_server_response(packet)
                    continue
                if packet.message_type == MESSAGE_TYPE_SERVER_ERROR:
                    self._handle_server_error(packet)
                    return
        except Exception as exc:
            if not self.stop_event.is_set():
                if emit_fallback_final(self.state):
                    self.final_received.set()
                else:
                    error_message = str(exc)
                    write_stdout({"type": "error", "message": error_message})
                    self.state.error = error_message
                    self.final_received.set()
        finally:
            self.stop_event.set()


def run() -> int:
    app_id, access_key = resolve_credentials()
    url = get_optional_env("VINPUT_ASR_URL", DEFAULT_URL)
    model = get_optional_env("VINPUT_ASR_MODEL", DEFAULT_MODEL)
    resource_id = get_optional_env("VINPUT_ASR_RESOURCE_ID", DEFAULT_RESOURCE_ID)
    user_id = get_optional_env("VINPUT_ASR_USER_ID", app_id)
    timeout = get_optional_int_env("VINPUT_ASR_TIMEOUT", DEFAULT_TIMEOUT)
    finish_grace_secs = get_optional_float_env(
        "VINPUT_ASR_FINISH_GRACE_SECS", DEFAULT_FINISH_GRACE_SECS
    )
    if timeout is None:
        raise ValueError("VINPUT_ASR_TIMEOUT must not be empty.")
    if finish_grace_secs is None:
        raise ValueError("VINPUT_ASR_FINISH_GRACE_SECS must not be empty.")

    request_payload = build_request_payload(model, user_id)
    state = ProviderState()
    active_stream: Optional[ActiveStream] = None

    try:
        for raw_line in sys.stdin:
            if state.error:
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
                    audio_bytes = base64.b64decode(audio_base64, validate=True)
                except ValueError as exc:
                    raise ValueError("audio_base64 is not valid base64.") from exc

                if not audio_bytes:
                    raise ValueError("audio event decoded to empty audio.")

                if active_stream is None:
                    active_stream = ActiveStream(
                        state=state,
                        url=url,
                        app_id=app_id,
                        access_key=access_key,
                        resource_id=resource_id,
                        timeout=timeout,
                        request_payload=request_payload,
                    )

                should_commit = bool(event.get("commit", False))
                active_stream.send_audio(audio_bytes, final=should_commit)
                if should_commit:
                    active_stream.finish(finish_grace_secs)
                    emit_fallback_final(state)
                    active_stream = None
                continue

            if event_type == "finish":
                if active_stream is not None:
                    active_stream.finish(finish_grace_secs)
                    emit_fallback_final(state)
                    active_stream = None
                break

            if event_type == "cancel":
                if active_stream is not None:
                    active_stream.cancel()
                    active_stream = None
                break

            raise ValueError(f"Unsupported event type: {event_type or '<missing>'}")
    finally:
        if active_stream is not None:
            if state.error:
                active_stream.cancel()
            else:
                active_stream.finish(finish_grace_secs)
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
